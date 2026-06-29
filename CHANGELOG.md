# Changelog

All notable changes to `tckr` are documented here. Format roughly follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/).

## [Unreleased]

### Added
- **News & events — three new data sources + a unified cascade.** A new
  "News & events" category covering crypto-native and market-moving tradfi news:
  - **`cryptonews`** (keyless) — crypto headline aggregator over major outlet
    RSS feeds (Cointelegraph, Decrypt, The Block, CoinDesk), parsed with the
    standard library (no new dependency), merged + de-duplicated, with a
    client-side topic filter. No signup, no key.
  - **`gdelt`** (keyless) — GDELT DOC 2.0 global news/event firehose across ~65
    languages. `articles(query)` for macro/tradfi market-movers by keyword and
    `tone_timeline(query)` for coverage-sentiment trends. Respects GDELT's soft
    ~1 req/5s limit via the per-source cache.
  - **`finnhub`** (keyed-free, `FINNHUB_API_KEY`) — tradfi + crypto market news
    (general/forex/crypto/merger) and per-ticker company news. Free signup,
    ~60 req/min.
  - **`tckr.news`** cascade — `news.latest(query)` fans out across every
    available provider, de-duplicates by URL, sorts newest-first, and tags each
    item with the `provider` that produced it (mirrors `quotes`/`history`).
- **Agent tools:** `news`, `cryptonews_latest`, `gdelt_articles`,
  `gdelt_tone_timeline`, `finnhub_market_news`, `finnhub_company_news`.
- **`_http.get_text`** — raw-text GET helper (shares retry/redirect/health
  tracking with `get_json`) for non-JSON upstreams like RSS feeds.

## [0.3.4] — 2026-06-17

### Added
- **`tckr.aliases`** — curated identifier equivalence classes for resolving the
  same asset across venues/naming schemes.
- **Hyperliquid spot market data** — `spot_universe()` / `spot()` plus `hl_spot`
  agent tools, with suspected spot-name-collision flagging in `spot()`.
- **`cz_oi_aggregate` agent tool** — cross-exchange open-interest rollup for one
  coin (the OI sibling of `cz_funding_aggregate`), wrapping the existing
  `coinalyze.open_interest_aggregate`. Returns per-exchange OI plus
  `{total_open_interest_usd, n_exchanges, top_exchange_share_pct}`.
- **`hl_universe` / `hl_spot_universe`: `sort` + `desc` args.** The perps
  universe can now be ranked by `volume` (default), `oi`, `funding`,
  `funding_excess` (baseline-adjusted), or `change`; spot by `volume`, `change`,
  or `px`. `desc=false` surfaces the bottom (e.g. biggest losers). Omitting
  `sort` preserves the previous volume-ranked behavior. Turns the universe tools
  into one-call screeners instead of volume-only listings.

## [0.3.3] — 2026-06-10

Bug-fix release from a full-codebase audit. Several fixes change returned
values — they correct numbers that were materially wrong.

### Fixed
- **coinalyze: funding APRs were inflated up to ~800×.** Coinalyze returns
  funding `value` in **percent per the exchange's native funding interval**
  (verified live against OKX), not as an hourly fraction. `_parse_funding_row`
  now annualizes per-venue (8h default; 1h for Hyperliquid/dYdX/Kraken/Vertex/
  Lighter) and returns `funding_rate_pct` + `funding_interval_hours` instead of
  the misnamed `funding_rate_hourly`. `funding_aggregate` /
  `funding_extremes` / `cz_*` agent tools inherit the corrected APRs.
- **coinalyze: exchange code map was mostly wrong.** 7 of 11 hardcoded entries
  mislabeled venues (K labeled Hyperliquid but is Kraken; H is Hyperliquid, not
  Huobi; C is Coinbase, not Deribit; F is Bitfinex, not Bitget; Y is Gate.io,
  not Kraken; G is Gemini; D is Bitforex). Replaced with the full 28-entry map
  from the authoritative `/exchanges` endpoint — cross-exchange funding reads
  were attributing rates to the wrong venues.
- **goplus: holder-concentration and LP-lock risk warnings were broken in
  opposite directions.** GoPlus reports holder `percent` as fractions of 1, but
  `_risk_summary` compared the sums against 70/50 — so the top-10 concentration
  warning could *never* fire and the "only X% of LP locked" warning *always*
  fired. `top10_holder_pct` and `lp_locked_pct` are now 0–100 as the `_pct`
  suffix implies, and stay `None` (unknown) instead of `0.0` when GoPlus omits
  percent data. `birdeye.token_security` scales its `top10_holder_pct` /
  `top10_user_pct` the same way for cross-module consistency.
- **pumpfun: `live_trades()` returned only buys.** The Bitquery query filtered
  on `Buy.Currency == mint` and hard-coded `side: "buy"`, so sell-side trades
  (dumps) were invisible and buy/sell pressure reads were meaningless. Now runs
  mirrored buy/sell queries concurrently and merges them newest-first.
- **wallet_pnl: unknown cost basis no longer booked as $0.** Token-for-token
  swap legs (no SOL/stable side) have unknowable USD value; FIFO previously
  treated them as $0 basis, booking the full proceeds of the eventual sale as
  realized gain (or a full loss on unpriced sells). Unknown-value quantities
  are now excluded from realized/unrealized PnL and surfaced via
  `qty_unknown_pnl` + `basis_incomplete` per token and in the wallet summary.
- **neynar: `price_usd` was `None` for bare-number `price` fields.** An
  operator-precedence bug made the fallback branch dead code, dropping the
  price on trending-feed rows.
- **options/cboe: DTE now uses the US/Eastern trading date** instead of UTC,
  which overstated `dte` by one between midnight UTC and ET market hours. Adds
  a `tzdata` dependency on Windows.
- **options: cascades no longer discard a valid empty Alpaca result** when CBOE
  can't answer either; empty-chain fallback to CBOE (Alpaca has no index
  options) is now documented.
- **agent_toolkit: `cg_market_chart` crashed on `days='max'`** despite its own
  schema advertising it (`int()` cast). Note CoinGecko's public tier now caps
  history at 365 days; the description says so.
- **agent_toolkit: MCP server now signals tool failures via `isError`.**
  Exceptions are raised (the SDK converts them to
  `CallToolResult(isError=True)`) instead of returned as ordinary text an LLM
  could mistake for data.
- **agent_toolkit: LangChain adapter maps `array` schemas to `list[...]`**
  (was `Any`, degrading the tool schemas LangChain shows the model) and
  handles union types like `["integer", "string"]`.
- **agent_toolkit: `limit=0` no longer means "give me the max"** in `_cap`.
- **polymarket: `book()` no longer caches a fully empty book** for the whole
  TTL (thin CLOB books clear and refill between fills).
- **lunarcrush: 200-OK error envelopes are no longer cached** as if they were
  data.
- **etherscan: `token_transfers` rows** return `ts` as ISO 8601 (was a raw
  epoch string, unlike every other module) and `block` as an int.
- **jito: `HELIUS_API_KEY` moved out of the URL string** into request params,
  matching `helius.py` (keeps the key out of exception URLs and URL logs).
  `_http.post_json` gained a `params=` passthrough.
- **registry: a typo'd `required_env`/`optional_env` name now raises** instead
  of silently reporting the module as permanently unconfigured.
- **cli: update-check cache reads/writes pin `encoding="utf-8"`** (was
  locale-dependent on Windows).

## [0.3.2] — 2026-06-09

### Changed
- **`tckr status` dashboard polish.** The logo is now a single green hue fading
  light→dark instead of the cyan→magenta rainbow. Each source shows a short,
  general blurb instead of its verbose registry note. Rows are grouped into
  data-domain categories (Prices & oracles, DEX & tokens, Perps & funding,
  On-chain & DeFi, Launchpads, Security, Social & research, TradFi & prediction)
  — each with a ready/total count and sorted alphabetically — replacing the flat
  ACTIVE/LOCKED split; usable/locked state is now the per-row ✓/✗ marker plus an
  inline `needs KEY` / `↑ add KEY` hint.

### Added
- **`registry.category(name)` / `registry.blurb(name)`** + `CATEGORY_ORDER`,
  backed by a centralized dashboard-metadata map and surfaced per module in
  `capabilities()` (`category`, `blurb`). The verbose `notes` are unchanged and
  still drive agent tool descriptions.

## [0.3.1] — 2026-06-09

### Changed
- **`tckr status` is now a colored onboarding dashboard.** Replaces the flat
  monochrome table with a grouped capability view designed to be the first
  thing a new user runs: a gradient block logo, an **ACTIVE** section (usable
  right now) with inline "↑ add KEY for more" hints on modules an optional key
  would expand, and a **LOCKED** section listing exactly which key each
  unconfigured module needs. Footer shows ready/keyless/keyed/paid counts plus
  how many modules are expandable. Color auto-disables for non-TTY / `NO_COLOR`;
  `--no-color` forces plain text. Zero new dependencies.

### Added
- **`registry.expansion_keys(name)`** — unset optional env vars that *expand* an
  already-usable module (distinct from `missing_keys`, which *enable* an
  unconfigured one). `capabilities()` now carries `expansion_keys` per module
  and an `expandable` count in its summary.

## [0.3.0] — 2026-06-05

### Added
- **Listed options layer** — US equity/ETF/index option chains with model
  greeks (delta/gamma/theta/vega/rho) and implied volatility per contract: the
  supported replacement for unofficial yfinance options scraping, which has no
  greeks and rate-limits aggressively.
  - `options` (`tckr.options`, keyed-free) — Alpaca options snapshots:
    `option_chain()`, `option_snapshot()`, `expirations()`, plus OCC symbol
    `parse_occ()` / `build_occ()`. Free signup at alpaca.markets (no funding);
    the free `indicative` feed is delayed ~15m, `ALPACA_OPTIONS_FEED=opra`
    switches to real-time once subscribed.
  - `cboe` (`tckr.cboe`, keyless) — CBOE public delayed-quote feed as a
    zero-signup fallback. Covers indices (SPX/VIX/NDX/RUT) that Alpaca omits,
    and adds `open_interest` / `volume` / `theo` per contract. Reuses the OCC
    parser from `tckr.options` and emits the same flattened row shape.
  - **Cascade** — `options.chain_cascade()` / `snapshot_cascade()` /
    `expirations_cascade()` use Alpaca when keyed, else fall back to keyless
    CBOE; each result carries a `source` field.
  - **CLI** — `tckr options <ticker>` with `--exp`, `--type`, `--source`
    (auto/alpaca/cboe), `--expirations`, `--limit`, `--top`.
  - **Agent toolkit** — three new tools: `opt_chain`, `opt_snapshot`,
    `opt_expirations` (all cascade Alpaca → keyless CBOE, so they work with no
    key out of the box).
- **Hyperliquid funding baseline** — perp snapshots and `funding_history()`
  rows now expose `funding_above_baseline_apr_pct`, which subtracts
  Hyperliquid's built-in ~+10.95% APR interest-rate floor so the demand-driven
  component is directly readable; `funding_history()` also gains
  `funding_apr_pct`. The `hl_perp` / `hl_funding_history` tool descriptions
  explain reading it together with `premium` for real directional crowding
  (raw funding near +10.95% with premium ≈ 0 is mechanical, not crowded longs).

## [0.2.4] — 2026-05-28

### Added
- **Polymarket CLOB layer** — `book(token_id)`, `outcome_book(slug, outcome)`,
  `outcome_touches(slug)`, `effective_fill(slug, outcome, side, qty)`. The
  Gamma API's `bestBid` / `bestAsk` are AMM midpoints that can diverge
  wildly from the live CLOB on thin markets (observed: gamma 0.52 vs CLOB
  best_ask 0.96 on a real market). The new functions hit
  `clob.polymarket.com` directly:
  - `book()` returns normalized `{best_bid, best_ask, midpoint, spread,
    last_trade_price, tick_size, min_order_size, bids[], asks[]}` for one
    outcome token.
  - `outcome_book()` keys the same data by slug + "yes"/"no" so callers
    don't need to handle 75-digit ERC1155 token ids.
  - `outcome_touches()` returns YES + NO touches in parallel (single
    gamma + two CLOB calls) — the right call for "is this fillable?"
    checks before sizing in.
  - `effective_fill()` walks the book to compute a volume-weighted fill
    price, signed `slippage_from_touch_bps` (positive = adverse), and
    `qty_unfilled` / `fully_filled` flags so the caller knows whether the
    venue can actually absorb their size.
- **`_shape_market` now exposes `no_price`, `yes_token_id`, `no_token_id`,
  `clob_token_ids`** — the on-chain ERC1155 token ids needed to query the
  CLOB book layer above.
- **Agent toolkit** — three new MCP tools: `pm_book`, `pm_touch`,
  `pm_size_to_fill`. Tool descriptions on `pm_top_volume` and `pm_market`
  also gained explicit warnings that those gamma fields are NOT live
  fillable prices — agents should run `pm_touch` or `pm_size_to_fill`
  before sizing into any position.

### Fixed
- **`polymarket.markets()` / `polymarket.market()` `volume_24h` field was
  silently returning lifetime volume.** `_shape_market` mapped `volumeNum`
  (the numeric form of total volume on the Gamma API) to `volume_24h`,
  falling through to `volume24hr` only when `volumeNum` was missing — which
  it almost never is. Anyone sorting or filtering by `volume_24h` was
  scoring on total volume instead. The mapping now prefers `volume24hr`
  with `volume24hrClob` as fallback; `volume` (total) is unchanged. Caught
  while exploring sports markets where every row reported identical
  `volume` and `volume_24h`.

## [0.2.3] — 2026-05-27

### Added
- **`polymarket.market_status(slug)`** — settlement-loop primitive. Returns
  one of `"alive"`, `"resolved_yes"`, `"resolved_no"`, `"ambiguous"`, or
  `"ghost"` so callers can branch (settle on resolved, alert on ghost,
  no-op on alive/ambiguous) without re-deriving the state from raw fields.
- **`tckr.bankr`** — new keyless module for the Bankr launchpad feed
  (Doppler on Base, Raydium on Solana). Public surface: `new_launches`,
  `launch`, `launches_by_deployer`, `launches_by_x_user`. Carries
  `x_username` + `x_profile_image_url` — the X-side analogue of
  `clanker.requestor_fid` for cross-link with social-graph tools. Optional
  `BANKR_API_KEY` unlocks speculative `resolve_address` + `search_users`
  endpoints (wired but not yet validated against a live key).

### Changed
- **`polymarket.market(slug)` cascade hardened against rename + resolution.**
  Three-step lookup: default `?slug=` → `?slug=&closed=true` (so resolved
  markets stay findable) → `?condition_ids=<id>` via a persisted slug ↔
  conditionId alias map. Catches the case where polymarket appends a
  numeric disambiguator to a slug while the underlying conditionId stays
  stable. Returned `slug` is relabeled to the requested slug so callers
  keyed off the original keep resolving; `condition_id` carries the
  canonical identifier. Numeric-id fallback now only fires for digit-only
  inputs (previously spammed 422s on every ghost slug lookup).
- **`tckr.quotes` / `tckr.history` cascade order flipped to Hyperliquid →
  CoinGecko.** For the ~230 symbols HL covers, HL's live perp mark is
  fresher than CG's spot and isn't subject to the free-tier 429 cliff —
  previously CG-first was wasting requests on rate-limited majors when HL
  could have answered. CG handles the long tail and backstops transient HL
  misses.
- **`tckr.history` volumes are now USD across both sources.** Hyperliquid
  base-asset volume is multiplied by each bar's close, so `volume_last` /
  `volume_avg_20d` are comparable across symbols even when the cascade
  picks different sources per symbol. CoinGecko already returned USD.

### Added — env vars
- **`TCKR_POLYMARKET_ALIASES_PATH`** — optional path to a JSON file where
  polymarket persists slug → conditionId aliases. Unset (default) keeps
  the map in-process only; setting it lets the rename-recovery survive
  process restarts and be seeded manually for known-stranded slugs.

## [0.2.2] — 2026-05-26

### Documentation
- README rewritten around the new-user journey: punchier hook, zero-key
  "works out of the box" example, agent-wiring section, and a ranked table
  of free API keys ordered by actual production impact (Alchemy + Helius
  first, then Coinalyze / Birdeye / Moralis-or-Bitquery for vertical depth).
  Paid keys table now describes what each one *buys* you, not just rate
  limits. Diagnostics + `tckr update` consolidated into one section.

## [0.2.1] — 2026-05-26

### Added — CLI
- **`tckr update`** — upgrade tckr to the latest PyPI release in one step.
  Detects pipx / uv-tool / PEP 668 system-managed installs and prints the
  right command instead of failing. `--check` flag does a dry-run (just
  reports whether a newer version exists). Always honors explicit invocation
  even when `TCKR_NO_UPDATE_CHECK` is set (that env var silences the implicit
  status-banner check, not user-initiated commands).

## [0.2.0] — 2026-05-26

### Added
- **`hyperliquid.candles(symbol, interval, limit)`** — wraps HL's
  `candleSnapshot` info payload. Returns `{symbol, interval, candles: [{t,o,h,l,c,v}, ...]}`
  chronological, matching `geckoterminal.pool_ohlcv`'s shape. Intervals from
  `1m` to `1M`. No auth, no observed rate limit at typical reading volume.
- **`tckr.quotes`** — unified USD price cascade. `quotes.get(symbols)` tries
  CoinGecko `simple_price` first, falls through to Hyperliquid `perp` marks
  on miss / rate-limit. Each result carries `source` so callers can detect
  fallback. `quotes.get_one(symbol)` for single-symbol convenience.
- **`tckr.history`** — unified daily candle cascade. `history.candles(symbols, days)`
  tries CoinGecko `market_chart` first, falls through to `hyperliquid.candles`
  on miss / rate-limit. Same `source` convention.
- **`tckr.health()`** — per-provider HTTP health snapshot (ok/fail counts,
  last status, last error, last-429 timestamp). Instrumented at the `_http`
  layer, so every provider is covered automatically. Useful for diagnosing
  "why is my data thin" and for agents reasoning about degraded mode.
- **Agent toolkit additions**: `hl_candles`, `quote` (cascade), `candles`
  (cascade), `health` — four new MCP tools. The cascade tools let agents
  skip the provider-routing decision; the health tool surfaces upstream state.
- Capability / tier registry (`tckr.registry`): per-module tier
  (`keyless-free` / `keyed-free` / `keyed-paid`), required env vars, and an
  `is_configured()` check. Drives MCP tool descriptions, CLI status, and the
  `capabilities` introspection tool.

### Added — CLI
- **PyPI update check in `tckr status`** — soft, opt-out check against PyPI
  for a newer version of `tckr`. Disk-cached for 24h; uses stdlib urllib (no
  extra deps); fails silently offline. Set `TCKR_NO_UPDATE_CHECK=1` to skip.

### Fixed
- **`history.candles` returned hours, not days, for `days < 91`.** CoinGecko's
  free tier returns hourly granularity for sub-90-day windows; the previous
  code sliced the last N hourly points thinking they were daily. Now
  downsamples to one close per UTC date before slicing, so `days=30` really
  returns 30 daily closes regardless of CG tier.
- **`_http._provider_of` bucketed unlabelled calls under their full URL.**
  Modules that omit the `label=` kwarg (a handful of older fetchers) caused
  `health()` to show keys like `https://api.dexscreener.com/...` instead of
  `dexscreener`. Now extracts the second-level hostname for URL fallbacks.
- **`quotes._hl_price` would swap a legitimate 0.0 mark for the mid via
  `or`-fallback.** Now uses an explicit `is not None` check. (Extremely rare
  edge state, but technically wrong.)

### Documentation
- README: new "Fallback cascade" section explaining when to use `tckr.quotes`
  / `tckr.history` vs. direct provider modules; per-provider failure-mode
  table; rationale for HL as the canonical free-tier fallback.
- Module docstrings: `coingecko` and `hyperliquid` now flag failure modes
  (CG free-tier 429s, HL coverage limited to ~230 perp-listed tokens).
- Agent toolkit (`tckr.agent_toolkit`) — extracted from an internal
  agent project and refactored into a platform-neutral core +
  per-platform adapters:
  - `agent_toolkit.core` — 20+ tool functions and `ToolSpec` registry; no SDK deps.
  - `adapters/claude_sdk` — Claude Agent SDK in-process MCP server (`tckr[agent-claude]`).
  - `adapters/mcp_stdio` — universal MCP stdio server, console-script `tckr-mcp`
    (`tckr[agent-mcp]`). Works with any MCP-compatible client.
  - `adapters/openai` — OpenAI function-calling shape (`tckr[agent-openai]`).
  - `adapters/langchain` — LangChain `StructuredTool` wrappers (`tckr[agent-langchain]`).

## [0.1.0] — 2026-05-24

First tagged release. Inventory of what shipped during the build-up
(2026-05-22 through 2026-05-24):

### Added — data-source modules

- `geckoterminal` — DEX pools, tokens by contract, OHLCV (Base / Solana / ETH). Keyless.
- `dexscreener` — DEX pairs, search, latest token profiles, paid-boost rankings. Keyless.
- `hyperliquid` — single-exchange perps: funding, OI, marks. Keyless.
- `defillama` — chain/protocol TVL, DEX volume, stablecoins, yields. Keyless.
- `coinalyze` — cross-exchange perps aggregator (funding spread, OI, liquidations
  across Binance / Bybit / OKX / Hyperliquid). Requires `COINALYZE_API_KEY` (free).
- `goplus` — EVM token contract security scans (honeypot detection, taxes, owner
  privileges, holder distribution). Keyless.
- `honeypot` — sell-simulation backstop on ETH / BSC / Base. Keyless.
- `birdeye` — Solana-focused token analytics (overview, top holders, security).
  Requires `BIRDEYE_API_KEY` (free tier).
- `pumpfun` — Solana memecoin launchpad. Discovery (new / about-to-bond /
  graduated) via Moralis or Bitquery; on-chain bonding-curve state via Helius.
  Adds Bitquery-exclusive analytics (top_traders, live_trades, migration_events,
  curve_trajectory, holder_distribution). Requires `MORALIS_API_KEY` or
  `BITQUERY_API_KEY` for discovery; `HELIUS_API_KEY` for state.
- `neynar` — Farcaster API (cast search, channel feeds, trending fungibles).
  Requires `NEYNAR_API_KEY`; most endpoints require paid tier as of 2026-05.
- `wallet_pnl` — FIFO PnL across Solana + Base wallets. Auto-resolves ATA →
  owner. Per-token realized + unrealized USD; filters wSOL/WETH/stables.
  Composite — reuses Helius / Alchemy / Moralis / Birdeye keys.
- `lp_lock` — LP-lock detection for Uniswap V2 pairs, V3 positions, V4 positions
  on Base / ETH. Auto-detects pool type from input shape. Covers UNCX V2/V3/V4
  and Team Finance ETH lockers. Requires `ALCHEMY_API_KEY` (free).
- `virtuals` — Virtuals Protocol AI-agent launchpad on Base. Tracks the
  42K-VIRTUAL bonding-curve graduation threshold. Keyless.
- `clanker` — Clanker Farcaster-native token launcher (Base, multi-chain).
  Carries `requestor_fid` for direct cross-link with `neynar`. Keyless.
- `jito` — Solana MEV intel. `tip_floor`, `bundle_status`, and the headline
  `snipe_score(sigs)` — feeds e.g. `pumpfun.live_trades` signatures to quantify
  bot-sniping intensity on a launch. Uses `HELIUS_API_KEY`.
- `alchemy` — EVM (Base, ETH) wallet balances + transfers. Requires `ALCHEMY_API_KEY`.
- `helius` — Solana RPC convenience layer. Requires `HELIUS_API_KEY`.

### Added — infrastructure

- `_http` — shared httpx-based fetch helper with retry on 429/5xx.
- `cache` — async TTL cache, instantiated per-module.
- `settings` — env-driven config (API keys + per-source TTLs), no other deps.
- `cli` — `tckr <subcommand>` for ad-hoc queries (dex / token / perps /
  tvl / wallet).
