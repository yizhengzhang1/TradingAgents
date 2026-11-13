"""Futu + Longbridge news: Futu's yearless dates, symbol mapping, and the merged
vendor's window, dedupe and failure handling. Both brokers are stubbed."""
from datetime import date, datetime, timedelta

import pytest

from tradingagents.dataflows import router
from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.vendors import broker_news as bn


@pytest.fixture(autouse=True)
def _clear_cache():
    bn._cache.clear()
    yield
    bn._cache.clear()


def test_futu_date_takes_latest_year_not_in_the_future():
    today = date(2026, 11, 12)
    assert bn._futu_date("11/12", today) == datetime(2026, 11, 12)
    assert bn._futu_date("11/13", today) == datetime(2026, 11, 13)  # Futu's clock may run a day ahead
    assert bn._futu_date("12/30", today) == datetime(2025, 12, 30)
    assert bn._futu_date("2024/03/01", today) == datetime(2024, 3, 1)
    assert bn._futu_date("14:30", today) == datetime(2026, 11, 12)
    assert bn._futu_date(float("nan"), today) is None


def test_symbol_mapping():
    assert bn._longbridge_symbol("NVDA") == "NVDA.US"
    assert bn._longbridge_symbol("BRK-B") == bn._longbridge_symbol("BRK.B") == "BRK.B.US"
    assert bn._longbridge_symbol("0700.HK") == "700.HK"
    assert bn._longbridge_symbol("600519.SS") == "600519.SH"
    assert bn._futu_keyword("0700.HK") == "00700"
    for other_market in ("7203.T", "AZN.L", "BTC-USD", "^GSPC"):
        with pytest.raises(VendorUnavailableError):
            bn._futu_keyword(other_market)


def _md(d: date) -> str:
    return f"{d.month}/{d.day}"


def test_merged_news_windows_dedupes_and_survives_one_broker(monkeypatch):
    today = date.today()
    end = today - timedelta(days=1)
    futu_rows = [
        {"title": "Stale rating", "news_sub_type": "RATING", "source": "WSJ",
         "publish_time": _md(end - timedelta(days=90)), "url": "f0"},
        {"title": "Old rating", "news_sub_type": "RATING", "source": "WSJ",
         "publish_time": _md(end - timedelta(days=3)), "url": "f1"},
        {"title": "Same story!", "news_sub_type": "NEWS", "source": "Benzinga",
         "publish_time": _md(end), "url": "f2"},
        {"title": "After the run date", "news_sub_type": "NEWS", "source": "WSJ",
         "publish_time": _md(today), "url": "f3"},
    ]
    lb_items = [{"id": "9", "title": "same story", "url": "l1",
                 "published_at": f"{end:%Y-%m-%d}T15:00:00Z"}]
    monkeypatch.setattr(bn, "_futu_search", lambda keyword: (futu_rows, None))

    def longbridge(*args, **kwargs):
        if args[:2] == ("news", "detail"):
            return {"description": "Body"}
        return lb_items

    monkeypatch.setattr(bn, "_longbridge", longbridge)
    start = f"{end - timedelta(days=7):%Y-%m-%d}"
    out = router.route_to_vendor("get_news", "NVDA", start, f"{end:%Y-%m-%d}")
    assert "Old rating" in out and "analyst rating" in out
    assert out.lower().count("same story") == 1
    assert "After the run date" not in out

    # A past window between their articles is unobserved, not empty: a stale
    # relevance hit before it proves nothing about coverage.
    with pytest.raises(VendorUnavailableError):
        bn.get_news_futu_longbridge("NVDA", f"{end - timedelta(days=60):%Y-%m-%d}",
                                    f"{end - timedelta(days=50):%Y-%m-%d}")

    bn._cache.clear()

    def down(*args, **kwargs):
        raise VendorUnavailableError("login expired")

    monkeypatch.setattr(bn, "_longbridge", down)
    out = bn.get_news_futu_longbridge("NVDA", start, f"{end:%Y-%m-%d}")
    assert "Old rating" in out and "login expired" in out

    bn._cache.clear()
    monkeypatch.setattr(bn, "_futu_search", down)
    with pytest.raises(VendorUnavailableError):
        bn.get_news_futu_longbridge("NVDA", start, f"{end:%Y-%m-%d}")
