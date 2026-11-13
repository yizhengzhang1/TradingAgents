# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Fork

Fork of TauricResearch/TradingAgents: `origin` = yizhengzhang1/TradingAgents (the fork, `main` tracks `origin/main`), `upstream` = TauricResearch. Sync with `git fetch upstream && git merge upstream/main`. `git log upstream/main..main` lists fork-only work (the Futu/Longbridge news vendors, see Data vendors); README, CHANGELOG and `.env.example` don't document it. `cli/updates.py` checks upstream releases (opt-out `TRADINGAGENTS_NO_UPDATE_CHECK=1`); its suggested upgrade command drops the `[futu]` extra.

A real `.env` (0600, gitignored, API keys plus `TRADINGAGENTS_*` settings) sits at the repo root, so anything run from here uses it. Never print it.

## Commands

```bash
uv pip install -e ".[dev,futu]"     # .venv is a uv editable install (Python 3.13); CI: pip install -e ".[dev]"
.venv/bin/python -m pytest -q       # offline suite, ~10 s; 1 skip expected (no langchain_aws); addopts add -m "not integration"
.venv/bin/python -m pytest -q tests/test_vendor_routing.py
.venv/bin/python -m pytest -q "tests/test_cli_headless.py::test_flags_skip_exactly_their_own_steps"
.venv/bin/python -m pytest -m integration    # only when asked; reaches the network
.venv/bin/ruff check .              # CI lint; never run a repo-wide `ruff format`
.venv/bin/python -c "import tradingagents, cli.main"   # CI smoke-install check (plain `pip install .`, no extras)
```

Running the app (real LLM and data-vendor calls):

```bash
.venv/bin/tradingagents             # interactive CLI (= .venv/bin/python -m cli.main; Typer app cli.main:app)
.venv/bin/tradingagents --ticker NVDA --date 2026-11-12 --analysts market,news,fundamentals --save --no-show   # headless
.venv/bin/tradingagents backtest NVDA,AAPL --start 2026-06-01 --end 2026-08-01 --every 7 [--run-id <id to resume>]
.venv/bin/tradingagents --clear-checkpoints   # deletes every checkpoint DB, then still starts an analysis
.venv/bin/tradingagents login chatgpt         # also: logout chatgpt
.venv/bin/python main.py            # demo: propagate("NVDA", ...) with debug=True
```

Without a TTY, `unattended_gaps()` (cli/selections.py) exits 1 before any model call unless `--ticker/--date/--analysts`, `--save|--no-save`, `--show|--no-show` and the env vars `TRADINGAGENTS_LLM_PROVIDER`, `TRADINGAGENTS_{QUICK|DEEP}_THINK_LLM`, `TRADINGAGENTS_OUTPUT_LANGUAGE`, `TRADINGAGENTS_MAX_DEBATE_ROUNDS` + `TRADINGAGENTS_MAX_RISK_ROUNDS` are set (the local `.env` sets them). `--analysts` takes `sentiment`, which maps to the internal key `social`. Other flags: `--checkpoint/--no-checkpoint`, `--portfolio FILE.json`, `--html/--no-html`.

CI (`.github/workflows/ci.yml`) tests Python 3.11-3.14, plus a 3.12 job with `TZ=America/New_York` that catches tests passing only in UTC. The smoke-install job has no extras, so optional deps (`futu`, `langchain_aws`) must be imported lazily. Non-`.py` assets must be listed in `[tool.setuptools.package-data]`. The version lives only in `tradingagents/__init__.py` `__version__`; release-assets.yml fails unless it equals the tag without `v`. Commits are Conventional Commits (`fix(scope): plain sentence (#issue)`); releases are `release: X.Y.Z` then `chore: start X.Y.Z development (X.Y.Z.dev0)`. User-facing changes get a CHANGELOG.md line (Keep a Changelog), action-needed ones under "Upgrading from <prev>".

## Run lifecycle

Two entry paths; a change to run setup or teardown must go into both:
- `TradingAgentsGraph.propagate(ticker, date, asset_type="stock", portfolio=None)`: `create_run_state` → `graph.invoke` (or `stream_run` when debug) → `record_decision` → `clear_checkpoint_on_success`. Returns `(final_state, run_rating(final_state))`; it does not write the report tree (Python callers call `save_reports`).
- `cli/run.py` never calls `propagate`: `create_run_state`, `begin_checkpoint`, `stream_run(checkpoint_input(init_state))`, merges streamed chunks, then `record_decision` and `clear_checkpoint_on_success`, with `end_checkpoint` in a `finally`.

`trade_date` must be canonical `YYYY-MM-DD` and not in the future (`_validate_trade_date`). `stream_run` uses `subgraphs=True, stream_mode=["values","tasks"]` and surfaces each analyst report from its subgraph's finished task, because the parent state receives reports only after the slowest analyst.

`TradingAgentsGraph.__init__` calls `set_config` (process-wide), creates the results and cache dirs, builds both LLM tiers, and raises `ValueError` unless `2*max_tool_rounds + 2 < max_recur_limit`. Tests that only need methods skip it with `object.__new__(TradingAgentsGraph)`.

## Graph (`tradingagents/graph/setup.py`, `conditional_logic.py`)

- `START` fans out in parallel to the selected analysts plus a `"Memory Log"` node. A multi-source join feeds `"Bull Researcher"`; Bull and Bear alternate until `count >= 2*max_debate_rounds`, then `"Research Manager"` → `"Trader"`. Aggressive → Conservative → Neutral repeat until `count >= 3*max_risk_discuss_rounds`, then `"Portfolio Manager"` → `END`.
- Routers read labels the agents write: `current_response.startswith("Bull")` ("Bull Analyst: " / "Bear Analyst: ") and `latest_speaker` ("Aggressive" / "Conservative" / "Neutral"; the PM writes "Judge"). Every edge uses the full `DEBATE_PATH_MAP` / `RISK_ANALYSIS_PATH_MAP`, so a renamed label silently breaks the alternation instead of crashing.
- Each tool-using analyst is a compiled subgraph (agent ⇄ ToolNode + `wrap_up`) with a private message history and an `output_schema` holding only its report key. Its tools are the module's `TOOLS` tuple, used for both `bind_tools` and the ToolNode in `ANALYST_NODE_SPECS`. After `max_tool_rounds`, `take_turn` makes a final call without tools, with prior tool traffic rewritten as text (`_as_text`).
- The Sentiment Analyst (`social` → `sentiment_report`) has no tools: it fetches `get_news.func`, StockTwits and Reddit into the prompt (screened by `agents/post_screen.py` when `TYPESAFE_API_KEY` is set). Social feeds are not point-in-time.
- Debate nodes return full replacement dicts for their sub-state. If you change the graph layout, bump the `"analysts=parallel"` / `"memory=parallel"` markers in `_run_signature`.

## Agents and prompts (`tradingagents/agents/`)

- Prompts are inline strings in each agent module. Use the helpers in `agents/context.py` (`report_or_absent`, `opponent_argument_or_opening`, `get_portfolio_context_from_state`); never interpolate an empty string into a labelled section.
- Information flow: bull/bear never see `portfolio_context`, and Trader, risk debaters and PM must (tests/test_portfolio_context.py). By code, not tested: the Research Manager reads only `instrument_context` + debate history, and only the PM reads `past_context`; keep it that way.
- Structured output (Research Manager `ResearchPlan`, Trader `TraderProposal`, PM `PortfolioDecision`, Sentiment `SentimentReport`): `bind_structured` at factory time, then one plain `llm.invoke` free-text fallback; rendered to markdown by `render_*` in `agents/schemas.py`. Prompts on this path append `NO_EXTERNAL_TOOLS`, and each decision prompt keeps a `## Output` section naming `**Rating**` / `**Recommendation**` / `**Action**`.
- Rating (`agents/rating.py`, tested by `tests/test_signal_processing.py`): `run_rating` prefers `final_state["final_rating"]`; `extract_rating` is label-based and returns `None` on disagreement; unreadable decisions become `RATING_REVIEW` ("REVIEW"), never Hold, so check `is_review()` first. PM and Research Manager use the 5-tier `PortfolioRating`, the Trader the 3-tier `TraderAction`.
- Every report-producing agent must call `get_language_instruction()` and be listed in `REPORT_AGENTS` in `tests/test_i18n_coverage.py`. Labelled lines stay in English for the parser.
- Analysts must never contain the text `FINAL TRANSACTION PROPOSAL`. A `system_message` must not be a tuple (watch trailing commas).

## LLM providers (`tradingagents/llm_clients/`)

- Two tiers per graph from `create_tier_client(config, "deep"|"quick")`: deep serves only the Research Manager and PM; quick serves everything else, including the `Reflector`. `{tier}_think_provider` falls back to `llm_provider`; a tier on another provider uses only `{tier}_think_backend_url`, never `backend_url`.
- `factory.create_llm_client` checks the native `anthropic`/`google`/`azure`/`bedrock` clients first (lazy imports). Any other name must be a key of `openai_client.OPENAI_COMPATIBLE_PROVIDERS` (`ProviderSpec`), the single registry for the OpenAI-compatible family; wire quirks go in `chat_class` subclasses.
- Registry keys come from env only (`api_key_env.PROVIDER_API_KEY_ENV`); an `api_key` kwarg does not satisfy the required-key check. The Responses API is used only for `openai`/`chatgpt` when `base_url` is unset or `*.openai.com`. `chatgpt` is the only signed-in provider, pinned to `https://api.openai.com/v1`.
- Per-model structured-output quirks belong in `capabilities.py`. Reasoning knobs are mapped in `factory.build_llm_kwargs` and gated per model inside each client. The CLI picker reads `model_catalog.MODEL_OPTIONS`. Adding an OpenAI-compatible provider touches the registry, `PROVIDER_API_KEY_ENV`, `MODEL_OPTIONS`, `cli/prompts._llm_provider_table` and `tests/test_provider_registry.py`.

## Config precedence

- `import tradingagents` loads `.env`, then `.env.enterprise` (Azure vars), found by walking up from the CWD, never overriding exported vars. `DEFAULT_CONFIG` is built once at import; `build_default_config()` re-reads the env.
- `_ENV_OVERRIDES` in `default_config.py` is the only `TRADINGAGENTS_*` → key table; add a row to make a key env-settable. Values are coerced to the default's type (a bad bool/int raises at import); keys with `None` defaults stay raw strings and are coerced in `build_llm_kwargs`. Paths come from `TRADINGAGENTS_RESULTS_DIR` / `_CACHE_DIR` / `_MEMORY_LOG_PATH` (default `~/.tradingagents/{logs,cache,memory/trading_memory.md}`). `data_vendors` / `tool_vendors` have no env vars.
- `TradingAgentsGraph(config=...)` uses the dict as-is (no merge), so a partial dict raises `KeyError`. `DEFAULT_CONFIG.copy()` (main.py, cli/run.py, README) is shallow, so mutating `config["data_vendors"][...]` changes the global default; use `copy.deepcopy`.
- `dataflows/config.py`: `get_config()` returns a deepcopy of the ContextVar run config bound by `run_config` / `run_config_context`, else the global. `set_config` merges dict keys one level deep.
- CLI: a set `TRADINGAGENTS_*` var skips its prompt; otherwise answers from `~/.tradingagents/cli_prefs.json` prefill the prompts. An env round count beats research depth. A missing key at a TTY is prompted for and written to `.env` (chmod 0600).
- A new config key changes the checkpoint signature (sha256 of the config minus `_NOT_IN_SIGNATURE`) unless added there. It reaches reports only via the `run_settings()` allowlist, which must never include URLs, keys or paths.

## Data vendors and point-in-time (`tradingagents/dataflows/`)

- Tools in `agents/tools.py` take ticker and date from state via `InjectedState`, clamp model-supplied dates with `as_of` / `as_of_window`, return invalid windows as text (a raised exception ends the run), and reach vendors only through `router.route_to_vendor`. `get_verified_market_snapshot` bypasses routing and always uses Yahoo.
- Chain lookup: `tool_vendors[method]` → `data_vendors[category]` → `"default"` (all of `VENDOR_METHODS[method]`, in dict order). An explicit chain is exact: unregistered names are dropped, and a chain with no name registered for the method raises `ValueError`. A category chain covers every tool in it: `news_data` also serves `get_global_news` and `get_insider_transactions` (Yahoo/Alpha Vantage only), so keep `yfinance` in a broker chain or pin brokers via `tool_vendors["get_news"]`.
- Every exception tries the next vendor. Afterwards: any `VendorUnavailableError`, or an untyped error alongside a `NoMarketDataError`, → `"DATA_UNAVAILABLE: ..."`; otherwise any `NoMarketDataError` → `"NO_DATA_AVAILABLE: ..."`; only when no vendor answered no-data/unavailable is the first not-configured/untyped error re-raised for core categories (optional `macro_data`, `prediction_markets` return a DATA_UNAVAILABLE string).
- Vendors raise the `errors.py` hierarchy (`NoMarketDataError`, `VendorUnavailableError`, `VendorNotConfiguredError`, which is also a `ValueError`) instead of returning empty prose. A new vendor goes under `dataflows/vendors/` with the positional signature `tools.py` passes, is added to `VENDOR_METHODS` (a new method also needs `TOOLS_CATEGORIES` and a `@tool`), and applies the `date_window.py` helpers. Only `tradingagents/dataflows/` may import `yfinance` (`tests/test_layering.py`).
- Point-in-time: `in_window` is half-open UTC; `coverage_gap` emits an "unavailable, not an absence" placeholder for latest-only feeds. Historical runs withhold undated Yahoo/Alpha Vantage profiles, statements and insider trades (`withhold_*`). `sec_edgar` filters by filing date, hence `fundamental_data="sec_edgar,yfinance"`. `load_ohlcv` filters to `<= as_of` and raises when the data is more than 10 days stale.
- `symbols.normalize_symbol` maps to Yahoo symbols (aliases, crypto `BASE-USD`, forex `=X`, HK zero-padding, `.SH`→`.SS`). Any ticker interpolated into a path goes through `safe_ticker_component`.
- `broker_news` (`futu`, `longbridge`, `futu_longbridge`; default `news_data="futu_longbridge,yfinance"`): Futu needs a logged-in OpenD at `FUTU_OPEND_HOST`/`FUTU_OPEND_PORT` (default `127.0.0.1:11111`) and the `futu` extra (imported lazily in `_futu_search`); Longbridge shells out to `LONGBRIDGE_CLI` or `longbridge` on PATH. A missing extra/CLI raises `VendorNotConfiguredError` and the chain falls to `yfinance` with only a warning (the Docker image always ends up on Yahoo). Markets other than US/HK/SH/SZ/SG raise `VendorUnavailableError`. In-process `_cache`, single-flight per key: hits 600 s, failures 90 s, partial answers not cached.

## Memory, checkpoints, outputs

- `"Memory Log"` (`_memory_step`) runs `settle_all_pending` for every ticker, then `get_past_context(ticker, as_of=...)`; failures set `memory_note` and never end the run. `record_decision` writes the state-log JSON and appends a pending memory-log entry, unless an entry for that ticker+date already exists (pending or settled), in which case nothing is recorded; entries settle `holding_period_days` trading days later.
- The log format (`<!-- ENTRY_END -->` separators, `[date | ticker | rating | pending]` tag lines) is parsed only by `TradingMemoryLog` (memory/log.py); `backtest.summarize` and `--run-id` resume (skip cells already logged) read it through `load_entries()`, so keep that output stable.
- Checkpoints are opt-in (`checkpoint_enabled`) at `<data_cache_dir>/checkpoints/<TICKER>.db` (SqliteSaver). Resume by invoking with `None` (`checkpoint_input`), never the initial state. Cleared on success, kept on failure.
- Outputs: `<results_dir>/<TICKER>/TradingAgentsStrategy_logs/full_states_log_<date>.json`; reports in `<results_dir>/reports/<TICKER>_<YYYYmmdd_HHMMSS>/` (`write_report_tree`); backtests in `<results_dir>/backtest/<run_id>/` with their own memory log. The CLI also streams `<results_dir>/<TICKER>/<date>/message_tool.log` and `reports/<section>.md` during every run, saved or not. `complete_report.html` must stay inert: no script, CSP `default-src 'none'`, every value escaped.

## Test-suite conventions (`tests/conftest.py`)

- Before import, every `TRADINGAGENTS_*` var (including those in `.env`) is blanked and the path vars point to a temp dir.
- Autouse fixtures block sockets, DNS and `curl_cffi` unless a test is marked `integration`; fake `stdin.isatty()` as True (unattended tests set False); point `cli.prefs._PREFS_PATH` at tmp; keep yfinance's tz cache in a temp dir; reset `dataflows.config._config` to a deepcopy of `DEFAULT_CONFIG`.
- Placeholder API keys are set only for `_API_KEY_ENV_VARS` (OpenAI, Google, Anthropic, xAI, DeepSeek, DashScope, Zhipu, MiniMax, OpenRouter, Azure, Alpha Vantage); tests needing others (Mistral, Groq, FRED, ...) set their own.
- `_upstream_news_default` forces `news_data="yfinance"` everywhere except `test_broker_news`. Subprocesses are not blocked: broker tests stub both `bn._futu_search` and `bn._longbridge` and clear `bn._cache` before and after each test.
- Markers are `unit`, `integration`, `smoke` (`--strict-markers`). Graph tests use `ScriptedModel` in `tests/test_graph_end_to_end.py`, monkeypatch `trading_graph.create_tier_client` and stub `router.VENDOR_METHODS`.
