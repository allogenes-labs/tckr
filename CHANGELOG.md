# Changelog

All notable changes to `tckr` are documented here. Format roughly follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/).

## [Unreleased]

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
