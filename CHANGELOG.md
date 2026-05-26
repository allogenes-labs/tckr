# Changelog

All notable changes to `tckr` are documented here. Format roughly follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/).

## [Unreleased]

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
- Agent toolkit (`tckr.agent_toolkit`) — extracted from the
  Market-Research-Comp sibling project and refactored into a platform-neutral
  core + per-platform adapters:
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
