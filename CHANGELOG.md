# Changelog

All notable changes to `tckr` are documented here. Format roughly follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/).

## [Unreleased]

### Added
- Capability / tier registry (`tckr.registry`): per-module tier
  (`keyless-free` / `keyed-free` / `keyed-paid`), required env vars, and an
  `is_configured()` check. Drives MCP tool descriptions, CLI status, and the
  `capabilities` introspection tool.
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
