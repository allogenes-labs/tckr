# tckr

A reusable async crypto data layer. Stitches together free (and free-tier) public
APIs into one clean, cached, typed interface — built to be `pip install -e`'d
into multiple local projects rather than copy-pasted between them.

Every call is async, cached per-source (TTL), and degrades gracefully — a dead
or rate-limited upstream returns `None` / `[]` rather than raising. Modules that
need an API key log a warning and return empty when the key is absent rather
than crashing the caller.

## Sources

| Module | Source | Key? | Provides |
|---|---|---|---|
| `geckoterminal` | GeckoTerminal API v2 | no | DEX pools, tokens by contract address, OHLCV (Base, Solana, ETH, …) |
| `dexscreener` | Dexscreener API | no | DEX pairs, search, latest token profiles, paid-boost rankings |
| `hyperliquid` | Hyperliquid API | no | Single-exchange perps: funding, open interest, marks |
| `coinalyze` | Coinalyze API | free key | Cross-exchange perps: funding spread, OI, liquidations across Binance/Bybit/OKX/Hyperliquid/etc |
| `defillama` | DefiLlama API | no | Chain/protocol TVL, DEX volume, stablecoins, yields |
| `goplus` | GoPlus Security API | no | EVM token contract security scans (honeypot detection, taxes, owner privileges, holder dist) |
| `honeypot` | honeypot.is API | no | EVM sell-simulation backstop (ETH / BSC / Base) — actually attempts a swap to verify exit |
| `birdeye` | Birdeye public API | free key | Solana-focused token analytics: overview, top holders, trader PnL, security |
| `pumpfun` | Moralis / Bitquery (discovery + analytics) + Helius (state) | free key(s) | Solana memecoin launchpad. Discovery: new / about-to-bond / graduated lists. On-chain: bonding-curve state via SPL balance. Bitquery-exclusive analytics: top_traders, live_trades, migration_events, curve_trajectory, holder_distribution. |
| `neynar` | Neynar (Farcaster) | paid for most | Cast search, channel feeds, trending casts + fungibles, KOL helpers. 8 functions wired; only `user_by_username` works on free tier as of May 2026 — others return 402 until upgrade (degrade gracefully). |
| `wallet_pnl` | Helius (Solana) + Moralis (Base) + birdeye (prices) | uses existing keys | FIFO PnL across Sol+Base wallets. Auto-resolves ATA → owner. Per-token realized + unrealized USD. Filters wSOL/WETH/stables as counter assets by default. |
| `lp_lock` | Alchemy (EVM) | uses existing key | LP-lock detection for Uniswap V2 pairs, V3 positions, and V4 positions on Base / ETH. Auto-detects from input shape (40-hex-char address vs 64-hex-char PoolId). V2 returns `locked_pct`; V3/V4 return `n_locked_positions` + per-position detail. Covers UNCX V2, V3, V4 lockers; Team Finance on ETH. Team Finance Base + per-lock unlock_at are TODOs. |
| `virtuals` | api.virtuals.io | no key | Virtuals Protocol AI-agent launchpad on Base. `new_tokens` / `about_to_graduate` / `recently_graduated` / `genesis_launches` / `token_info`. Tracks the 42K-VIRTUAL bonding-curve graduation threshold. |
| `clanker` | clanker.world/api | no key | Clanker Farcaster-native token launcher (Base + multi-chain). `new_tokens` / `trending_tokens` / `tokens_by_fid` / `tokens_by_deployer` / `holders` / `token_info`. Carries `requestor_fid` for direct cross-link with [[neynar]]. |
| `jito` | block-engine.jito.wtf + Helius | uses HELIUS_API_KEY | Solana MEV intel. `tip_floor()`, `tip_accounts()`, `bundle_status()`, `inflight_bundle_status()`, `tx_jito_info(sig)`, `snipe_score(sigs)`. Killer use case: feed `pumpfun.live_trades` signatures into `snipe_score` to quantify how heavily a launch was bot-sniped. |
| `alchemy` | Alchemy RPC | free key | EVM (Base, ETH) on-chain wallet balances + transfers |
| `helius` | Helius RPC | free key | Solana RPC convenience layer (balances, transfers) |
| `coingecko` | CoinGecko v3 | no key (rate-limited) / free demo key / paid Pro | Canonical spot / market / historical prices; `simple_price`, `coin_markets`, `coin`, `market_chart`, `search`, `trending`, `global_stats`, `categories`. |
| `polymarket` | Polymarket Gamma API | no | Prediction-market odds (binary YES/NO). `markets`, `top_volume`, `market`, `events`. |
| `pyth` | Pyth Hermes | no | On-chain oracle prices for ~400 feeds (crypto, equities, FX, metals, rates). `feeds`, `latest_price`, `latest_price_for_symbols`. Sub-second cadence. |
| `etherscan` | Etherscan V2 | free key | Unified EVM block explorer across ~70 chains (ETH=1, Base=8453, Arb, Op, Polygon, BNB, ...). `balance`, `token_transfers`, `contract_source`, `contract_abi`, `gas_oracle`, `eth_supply`. Backwards-compat with `BASESCAN_API_KEY`. |
| `solscan` | Solscan | no (public) / free key (Pro) | Solana block explorer. Public endpoints (`token_meta`, `account_info`, `account_tokens`, `token_holders`, `tx_detail`) work keyless; `SOLSCAN_API_KEY` upgrades to Pro for richer parsing + higher RL. |
| `lunarcrush` | LunarCrush API4 | free key | Social-sentiment scoring: Galaxy Score, AltRank, social volume, topic feeds. `coins_list`, `coin`, `coin_time_series`, `topic`, `topics_list`. Free tier ~100 req/day. |
| `messari` | Messari API v1/v2 | paid (mostly) | Research-grade asset profiles, metrics, news. `asset`, `asset_metrics`, `asset_profile`, `news_feed`, `assets`. Most endpoints moved behind paid plans in 2024-2025. |
| `tokenterminal` | Token Terminal API v2 | paid (mostly) | Protocol fundamentals: revenue, fees, P/E, treasury. `projects`, `project`, `project_metrics`, `metric_history`, `market_sectors`. Free tier exposes catalog; historical series are paid. |
| `thegraph` | The Graph | no (public gateway, throttled) / free key (decentralized) | Generic GraphQL access to indexed subgraphs (Uniswap, Aave, Compound, Lido, etc.). `query_subgraph(id, query, vars)`, `uniswap_v3_top_pools`. |

## Install

```
pip install -e path/to/tckr
```

## Usage

```python
import asyncio
from tckr import geckoterminal, coinalyze

async def main():
    pools = await geckoterminal.trending_pools("base", limit=5)
    for p in pools:
        print(p["name"], p["price_usd"], p["volume_24h_usd"])

    # Cross-exchange funding spread for one coin — the killer Coinalyze use case
    agg = await coinalyze.funding_aggregate("BTC")
    if agg:
        a = agg["aggregate"]
        print(f"BTC funding APR — min {a['min_apr_pct']:.1f}%  "
              f"max {a['max_apr_pct']:.1f}%  spread {a['spread_apr_pct']:.1f}%")

asyncio.run(main())
```

## Configuration

All env vars are optional — modules without keys still work; modules with keys
no-op until set. See `tckr/settings.py` for the full list.

API keys (only needed for the modules that declare them in the table above):

- `COINALYZE_API_KEY` — free signup at coinalyze.net (no card).
- `BIRDEYE_API_KEY` — free tier at birdeye.so (~30 req/min on the endpoints used here).
- `MORALIS_API_KEY` — free tier at moralis.com; primary source for `pumpfun` discovery (`new_tokens`, `about_to_bond`, `recently_graduated`).
- `BITQUERY_API_KEY` — free tier at bitquery.io; fallback for `pumpfun.new_tokens` (Bitquery has the richest Pump.fun-specific schema). Either Moralis or Bitquery alone gets you working discovery. On-chain `bonding_state` only needs `HELIUS_API_KEY`. Also unlocks the 5 Bitquery-exclusive analytics functions (top_traders, live_trades, migration_events, curve_trajectory, holder_distribution).
- `NEYNAR_API_KEY` — free signup at dev.neynar.com. As of May 2026 the free tier only includes `user_by_username` — `search_casts`, `channel_feed`, `trending_fungibles` and the other 5 require a paid plan.
- `ALCHEMY_API_KEY` — free tier covers Base + ETH at app.alchemy.com.
- `HELIUS_API_KEY` — free tier at helius.dev.
- `BASESCAN_API_KEY` — currently declared but not consumed by any shipped module; reserved for the planned etherscan/basescan module.
- `COINGECKO_API_KEY` — CoinGecko Pro plan (paid). Uses `pro-api.coingecko.com` and unlocks Pro-only endpoints + 500 req/min+.
- `COINGECKO_DEMO_API_KEY` — CoinGecko Demo plan (free signup). Same public endpoints with a slightly higher rate-limit than no-key.
- `ETHERSCAN_API_KEY` — Etherscan V2 (free signup at etherscan.io). One key works across all V2-supported chains via the `chainid` parameter (ETH, Base, Arbitrum, Optimism, Polygon, BNB, Avalanche, zkSync, ...). The legacy `BASESCAN_API_KEY` env is also accepted as a fallback.
- `SOLSCAN_API_KEY` — Solscan Pro (paid). Public endpoints work without it.
- `LUNARCRUSH_API_KEY` — required for any LunarCrush call. Free signup at lunarcrush.com (~100 req/day on free tier).
- `MESSARI_API_KEY` — Messari. Free 'Hobbyist' tier limited to ~20 req/min on a small subset; most useful endpoints are paid (Pro / Enterprise).
- `TOKENTERMINAL_API_KEY` — Token Terminal. Free tier covers project catalog + limited metrics; full historical series + most metrics are paid.
- `THEGRAPH_API_KEY` — optional. Without it the public gateway is used (heavily throttled); with it the decentralized network gateway gives much higher quota.

Cache TTLs (override only if you know why):

- `TCKR_DEX_TTL_S`, `TCKR_DEX_OHLCV_TTL_S`, `TCKR_PERPS_TTL_S`,
  `TCKR_TVL_TTL_S`, `TCKR_ONCHAIN_TTL_S`, `TCKR_FUNDING_AGG_TTL_S`,
  `TCKR_LIQUIDATION_TTL_S`, `TCKR_SECURITY_TTL_S`,
  `TCKR_HONEYPOT_TTL_S`, `TCKR_BIRDEYE_TTL_S`, `TCKR_TOKEN_METADATA_TTL_S`,
  `TCKR_COINGECKO_TTL_S` (default 30), `TCKR_COINGECKO_HISTORY_TTL_S` (default 600),
  `TCKR_POLYMARKET_TTL_S` (default 30),
  `TCKR_PYTH_PRICE_TTL_S` (default 10), `TCKR_PYTH_CATALOG_TTL_S` (default 3600),
  `TCKR_ETHERSCAN_TTL_S` (default 30), `TCKR_ETHERSCAN_CONTRACT_TTL_S` (default 86400),
  `TCKR_ETHERSCAN_GAS_TTL_S` (default 15), `TCKR_ETHERSCAN_STATS_TTL_S` (default 600),
  `TCKR_SOLSCAN_TTL_S` (default 60),
  `TCKR_LUNARCRUSH_TTL_S` (default 120),
  `TCKR_MESSARI_TTL_S` (default 300),
  `TCKR_TOKENTERMINAL_TTL_S` (default 300), `TCKR_TOKENTERMINAL_HISTORY_TTL_S` (default 3600),
  `TCKR_THEGRAPH_TTL_S` (default 60).

HTTP behavior:

- `TCKR_HTTP_TIMEOUT_S` (default 15.0), `TCKR_HTTP_MAX_RETRIES` (default 2).

## New-pair / early-stage strategies (Sol + Base)

The thesis: the highest-asymmetry crypto trades are early entries on new
tokens that grow. The full toolkit for that is now shipped — `pumpfun` and
`virtuals` / `clanker` cover discovery on each chain's dominant launchpad;
`bonding_state` + `lp_lock` cover safety; `wallet_pnl` covers smart-money
tracking; `jito` quantifies bot-sniping intensity; `neynar` adds the
Farcaster social signal on Base. The modules compose:

- `pumpfun.live_trades(mint)` signatures → `jito.snipe_score(sigs)` →
  "how bot-sniped is this launch?"
- `clanker.new_tokens()` `requestor_fid` → `neynar.user_popular_casts(fid)` →
  "what is this deployer saying about their token?"
- `clanker.new_tokens()` `pool_address` (V4 PoolId) → `lp_lock(pool_id)` →
  "is this Clanker token's LP locked?"
- `pumpfun.top_traders(mint)` wallets → `wallet_pnl(wallet)` → "is the top
  buyer of this token actually profitable across their other trades?"

Future open items (no fixed timeline): per-lock `unlock_at` decoding for
UNCX V2/V3/V4, Team Finance Base address, V3/V4 USD-value-of-locked-liquidity
(needs tick math), neynar paid-tier endpoints when budgeted.

## Consumers

`tckr` is currently consumed by:

- **Market-Research-Comp** (sibling repo) — uses the fundamental-trading subset
  (`geckoterminal`, `dexscreener` basics, `hyperliquid`, `coinalyze`, `defillama`).
  Contract-safety + Solana-analytics modules ship in the library but are not
  exposed to those agents — reserved for the future new-pair app sketched above.
