"""Company news from two brokers: Futu (through a running OpenD) and Longbridge (its CLI).

Futu's news search (``get_search_news``) needs OpenD running and logged in;
its host and port come from ``FUTU_OPEND_HOST`` / ``FUTU_OPEND_PORT``. Longbridge
news comes from the ``longbridge`` CLI and its stored login; ``LONGBRIDGE_CLI``
names the binary when it is not on PATH. Both cover US, Hong Kong and A-share
listings (Longbridge also Singapore); any other instrument goes to the next vendor.

Both return their latest articles whatever window is asked for, and Futu's search
is relevance-ranked, so every article is trimmed to the window and an empty window
is reported unavailable, never as an absence of news, unless the window is today.

Futu dates an article by day only ("11/12": no year, no time, timezone unstated).
The year is the latest that does not put the day in the future, so a guessed date
is never earlier than the real one. The day is read as UTC midnight: whatever zone
Futu labels in (Beijing, New York, the host's), an article labeled with the run's
date was published before the next session opened, and a decision is entered at
the first close after its date.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import UTC, date, datetime, timedelta

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.date_window import coverage_gap, in_window
from tradingagents.dataflows.errors import (
    VendorError,
    VendorNotConfiguredError,
    VendorUnavailableError,
)
from tradingagents.dataflows.symbols import crypto_base, normalize_symbol

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 600
_FAILURE_TTL_SECONDS = 90
_LONGBRIDGE_LIST_COUNT = 50
_LONGBRIDGE_DETAILS = 8
_FUTU_KINDS = {"RATING": "analyst rating", "NOTICE": "company notice"}
# Yahoo suffix -> broker market code
_MARKETS = {"HK": "HK", "SS": "SH", "SZ": "SZ", "SI": "SG"}

_cache: dict[tuple, tuple[float, object]] = {}
_locks: dict[tuple, threading.Lock] = {}
_locks_guard = threading.Lock()


def _cached(key: tuple, fetch, keep=lambda value: True):
    """``fetch()`` once per key every few minutes, one caller at a time per key.

    Analysts run in parallel and each may ask; a failure is remembered briefly so
    they don't each wait out the same timeout, and a value ``keep`` rejects (a
    partial answer) is served but not cached.
    """
    with _locks_guard:
        lock = _locks.setdefault(key, threading.Lock())
    with lock:
        hit = _cache.get(key)
        if hit:
            age, value = time.monotonic() - hit[0], hit[1]
            if isinstance(value, VendorError):
                if age < _FAILURE_TTL_SECONDS:
                    raise type(value)(str(value))
            elif age < _CACHE_TTL_SECONDS:
                return value
        try:
            value = fetch()
        except VendorError as e:
            _cache[key] = (time.monotonic(), e)
            raise
        if keep(value):
            _cache[key] = (time.monotonic(), value)
        else:
            _cache.pop(key, None)
        return value


def _text(value) -> str:
    """A row field as text: pandas gives NaN for a missing one."""
    return value.strip() if isinstance(value, str) else ""


def _listing(canonical: str) -> tuple[str, str]:
    """``(code, market)`` for an equity the brokers can name, e.g. ("BRK.B", "US"), ("700", "HK")."""
    if crypto_base(canonical) or not re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]*", canonical):
        raise VendorUnavailableError(f"broker news covers listed equities only, not {canonical}")
    code, dot, suffix = canonical.rpartition(".")
    if not dot or (suffix in ("A", "B", "C") and code.isalpha()):  # NVDA, BRK-B, BRK.B
        return canonical.replace("-", "."), "US"
    market = _MARKETS.get(suffix)
    if market is None:
        raise VendorUnavailableError(f"broker news does not cover {canonical}")
    if market == "HK" and code.isdigit():
        code = str(int(code))
    return code, market


def _futu_date(text, today: date) -> datetime | None:
    """A Futu ``publish_time`` ("11/12", "2025/11/12", "14:30", epoch seconds) as a date."""
    text = _text(text)
    if text.isdigit() and len(text) >= 9:
        return datetime.fromtimestamp(int(text), tz=UTC)
    if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", text):
        return datetime(today.year, today.month, today.day)
    m = re.fullmatch(r"(?:(\d{4})[/-])?(\d{1,2})[/-](\d{1,2})(?:[ T].*)?", text)
    if not m:
        return None
    year, month, day = m.groups()
    if year:
        with contextlib.suppress(ValueError):
            return datetime(int(year), int(month), int(day))
        return None
    # Futu labels in its own timezone, up to a day ahead of this host's.
    latest = today + timedelta(days=1)
    for y in (latest.year, latest.year - 1, latest.year - 2):
        try:
            guess = datetime(y, int(month), int(day))
        except ValueError:  # 29 Feb in a common year, or not a date
            continue
        if guess.date() <= latest:
            return guess
    return None


def _futu_keyword(canonical: str) -> str:
    code, market = _listing(canonical)
    if market == "HK" and code.isdigit():
        return code.zfill(5)
    if market == "SG" or (market == "SH" and code.startswith("000")):
        # Futu search can't tell 000001.SS (an index) from 000001.SZ (a bank).
        raise VendorUnavailableError(f"Futu news search can't single out {canonical}")
    return code


def _futu_search(keyword: str) -> tuple[list[dict], str | None]:
    """Rows from Futu's news search, and a note when the latest-news half failed."""
    try:
        from futu import RET_OK, NewsSubType, OpenQuoteContext, SysConfig
    except ImportError as e:
        raise VendorNotConfiguredError("futu-api is not installed: pip install futu-api") from e

    host = os.getenv("FUTU_OPEND_HOST", "127.0.0.1")
    port = int(os.getenv("FUTU_OPEND_PORT", "11111"))
    with contextlib.suppress(Exception):
        from futu.common.ft_logger import logger as futu_logger

        futu_logger.console_level = logging.ERROR  # keep OpenD chatter out of the CLI
    try:
        SysConfig.set_all_thread_daemon(True)
        ctx = OpenQuoteContext(host=host, port=port, is_async_connect=True)
        ctx.set_sync_query_connect_timeout(10)
    except Exception as e:
        raise VendorUnavailableError(f"Futu OpenD unreachable at {host}:{port}: {e}") from e
    try:
        # ALL reaches back weeks (mostly analyst ratings); NEWS is the latest 100 stories.
        ret, data = ctx.get_search_news(keyword, max_count=100, news_sub_type=NewsSubType.ALL)
        if ret != RET_OK:
            raise VendorUnavailableError(f"Futu OpenD news search failed at {host}:{port}: {data}")
        rows = data.to_dict("records")
        ret, data = ctx.get_search_news(keyword, max_count=100, news_sub_type=NewsSubType.NEWS)
        if ret != RET_OK:
            return rows, f"Futu latest-news search failed: {data}"
        return rows + data.to_dict("records"), None
    except VendorError:
        raise
    except Exception as e:
        raise VendorUnavailableError(f"Futu news search failed: {e}") from e
    finally:
        with contextlib.suppress(Exception):
            ctx.close()


def _futu_articles(canonical: str) -> tuple[list[dict], str | None]:
    keyword = _futu_keyword(canonical)
    rows, partial = _cached(("futu", keyword), lambda: _futu_search(keyword), keep=lambda v: v[1] is None)
    today = date.today()
    articles = [
        {
            "title": _text(row.get("title")) or "No title",
            "summary": "",
            "publisher": _text(row.get("source")) or "Futu",
            "kind": _FUTU_KINDS.get(_text(row.get("news_sub_type")), ""),
            "link": _text(row.get("url")),
            "pub_date": _futu_date(row.get("publish_time"), today),
        }
        for row in rows
    ]
    return articles, partial


def _longbridge_symbol(canonical: str) -> str:
    code, market = _listing(canonical)
    return f"{code}.{market}"


def _longbridge(*args: str, timeout: float = 30):
    exe = os.getenv("LONGBRIDGE_CLI") or shutil.which("longbridge")
    if not exe:
        raise VendorNotConfiguredError("longbridge CLI not found on PATH (set LONGBRIDGE_CLI)")
    try:
        proc = subprocess.run(
            [exe, *args, "--format", "json"],
            capture_output=True, text=True, errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise VendorUnavailableError(f"longbridge {args[0]} timed out") from e
    except (FileNotFoundError, PermissionError) as e:
        raise VendorNotConfiguredError(f"longbridge CLI at {exe} can't run: {e}") from e
    except OSError as e:
        raise VendorUnavailableError(f"longbridge {args[0]} failed to start: {e}") from e
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()[-300:]
        raise VendorUnavailableError(f"longbridge {' '.join(args)} failed: {detail}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise VendorUnavailableError(f"longbridge {args[0]} returned non-JSON output") from e


def _iso(text) -> datetime | None:
    try:
        return datetime.fromisoformat(_text(text).replace("Z", "+00:00"))
    except ValueError:
        return None


def _longbridge_articles(canonical: str) -> tuple[list[dict], None]:
    symbol = _longbridge_symbol(canonical)
    items = _cached(
        ("longbridge", symbol),
        lambda: _longbridge("news", symbol, "--count", str(_LONGBRIDGE_LIST_COUNT)),
    )
    if not isinstance(items, list):
        raise VendorUnavailableError(f"longbridge news {symbol} returned an unexpected shape")
    articles = [
        {
            "id": str(item.get("id") or ""),
            "title": _text(item.get("title")) or "No title",
            "summary": "",
            "publisher": "Longbridge",
            "kind": "",
            "link": _text(item.get("url")),
            "pub_date": _iso(item.get("published_at")),
        }
        for item in items
        if isinstance(item, dict)
    ]
    return articles, None


def _add_longbridge_summaries(articles: list[dict]) -> None:
    """Fill the first few Longbridge articles' summaries; the first failure stops it."""
    for a in [a for a in articles if a.get("id")][:_LONGBRIDGE_DETAILS]:
        try:
            detail = _cached(("longbridge-detail", a["id"]),
                             lambda i=a["id"]: _longbridge("news", "detail", i, timeout=10))
        except Exception as e:  # summaries are optional
            logger.warning("Longbridge news detail %s unavailable: %s", a["id"], e)
            return
        if isinstance(detail, dict):
            a["summary"] = _text(detail.get("description"))


def _title_key(title: str) -> str:
    return re.sub(r"\W+", "", title).lower()


def _newest_first(article: dict) -> float:
    pub = article["pub_date"]
    if pub is None:
        return float("-inf")
    return (pub if pub.tzinfo else pub.replace(tzinfo=UTC)).timestamp()


def _spread(articles: list[dict], cap: int) -> list[dict]:
    """Up to ``cap`` articles taken a day at a time, so a day of heavy news can't crowd out the rest."""
    by_day: dict[object, list[dict]] = {}
    for a in articles:  # already newest first
        by_day.setdefault(a["pub_date"].date() if a["pub_date"] else None, []).append(a)
    picked: list[dict] = []
    while len(picked) < cap and any(by_day.values()):
        for day_articles in by_day.values():
            if day_articles and len(picked) < cap:
                picked.append(day_articles.pop(0))
    return sorted(picked, key=_newest_first, reverse=True)


def _format(articles: list[dict]) -> str:
    out = ""
    for a in articles:
        when = f"{a['pub_date']:%Y-%m-%d}" if a["pub_date"] else "undated"
        kind = f", {a['kind']}" if a["kind"] else ""
        out += f"### {a['title']} ({when}, source: {a['publisher']}{kind})\n"
        if a["summary"]:
            out += f"{a['summary']}\n"
        if a["link"]:
            out += f"Link: {a['link']}\n"
        out += "\n"
    return out


def _news(ticker: str, start_date: str, end_date: str, sources: list[tuple[str, object]]) -> str:
    canonical = normalize_symbol(ticker)
    resolved = "" if canonical == ticker else f" (resolved to {canonical})"
    subject = f"news for {ticker}{resolved}"
    label = " + ".join(name for name, _ in sources)

    articles: list[dict] = []
    errors: list[VendorError] = []
    for name, fetch in sources:
        try:
            got, partial = fetch(canonical)
        except VendorError as e:
            logger.warning("%s news unavailable for %s: %s", name, canonical, e)
            errors.append(e)
            continue
        except Exception as e:
            logger.warning("%s news failed for %s: %s", name, canonical, e)
            errors.append(VendorUnavailableError(f"{name} news failed: {e}"))
            continue
        articles.extend(got)
        if partial:
            errors.append(VendorUnavailableError(partial))
    if not articles and errors:
        if all(isinstance(e, VendorNotConfiguredError) for e in errors):
            raise VendorNotConfiguredError("; ".join(map(str, errors)))
        if len(errors) == 1:
            raise errors[0]
        raise VendorUnavailableError(f"{label} news unavailable: " + "; ".join(map(str, errors)))

    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    seen, in_range = set(), []
    for a in sorted(articles, key=_newest_first, reverse=True):
        key = _title_key(a["title"])
        if in_window(a["pub_date"], start_dt, end_dt) and key not in seen:
            seen.add(key)
            in_range.append(a)
    in_range = _spread(in_range, get_config()["news_article_limit"] * len(sources))

    if not in_range:
        if errors:
            raise VendorUnavailableError(f"{label} news incomplete: " + "; ".join(map(str, errors)))
        # Relevance-ranked, merged results prove no continuous coverage: only a
        # window that is today can be called empty.
        gap = coverage_gap((), start_date, end_date, f"{label} news", subject)
        if gap:
            raise VendorUnavailableError(gap)
        return f"No news found for {ticker}{resolved} between {start_date} and {end_date} ({label})"

    _add_longbridge_summaries(in_range)
    note = f"\n_Not every source answered: {'; '.join(map(str, errors))}_\n" if errors else ""
    return (f"## {ticker}{resolved} News, from {start_date} to {end_date} (sources: {label}):\n"
            f"{note}\n{_format(in_range)}")


def get_news_futu(ticker: str, start_date: str, end_date: str) -> str:
    """News about ``ticker`` in the window from Futu's news search (needs OpenD)."""
    return _news(ticker, start_date, end_date, [("Futu", _futu_articles)])


def get_news_longbridge(ticker: str, start_date: str, end_date: str) -> str:
    """News about ``ticker`` in the window from the Longbridge CLI."""
    return _news(ticker, start_date, end_date, [("Longbridge", _longbridge_articles)])


def get_news_futu_longbridge(ticker: str, start_date: str, end_date: str) -> str:
    """Futu and Longbridge news merged, deduplicated by title, newest first.

    One broker failing still serves the other's articles, with a note naming
    the failure; an empty window or both failing hands the call to the next
    configured vendor.
    """
    return _news(ticker, start_date, end_date,
                 [("Futu", _futu_articles), ("Longbridge", _longbridge_articles)])
