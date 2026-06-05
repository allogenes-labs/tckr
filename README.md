# tckr

[![PyPI](https://img.shields.io/pypi/v/tckr.svg)](https://pypi.org/project/tckr/)
[![CI](https://github.com/allogenes-labs/tckr/actions/workflows/ci.yml/badge.svg)](https://github.com/allogenes-labs/tckr/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**Free crypto data in one pip install. No keys required to start.**

Building anything with crypto data usually means signing up for five APIs, writing the same retry loop five times, juggling rate limits, and watching it all break when one provider goes down. tckr collapses that into one Python package: 13 of the most-used sources work the moment you install it, and another 10 unlock with free signups. The same registry doubles as an agent toolkit for Claude, OpenAI, MCP, and LangChain — so if you're plugging crypto data into an LLM, it's one install away.

```bash
pip install tckr              # data layer
pip install tckr[agent-mcp]   # + universal MCP server for any LLM
pip install tckr[agent-all]   # + all four agent adapters
```

```python
import asyncio
from tckr import quotes

async def main():
    print(await quotes.get(["BTC", "ETH", "SOL"]))
    # {'BTC': {'price': 77150.0, 'source': 'coingecko', ...},
    #  'ETH': {'price': 2890.4,  'source': 'coingecko', ...},
    #  'SOL': {'price': 178.2,   'source': 'coingecko', ...}}

asyncio.run(main())
```

No signup, no key, no rate-limit boilerplate. If CoinGecko 429s, the call quietly falls back to Hyperliquid.

## What's in the box

26 data sources, grouped by the corners of crypto people usually glue together by hand:

- **Prices & oracles** — CoinGecko, Hyperliquid, Pyth
- **DEX & on-chain** — GeckoTerminal, DexScreener, Alchemy (EVM), Helius (Solana)
- **Perps** — Hyperliquid, Coinalyze (cross-exchange funding, OI, liquidations)
- **Launchpads** — Pump.fun, Clanker, Virtuals
- **Token safety** — GoPlus, Honeypot, LP-lock detection
- **DeFi data** — DefiLlama (TVL, yields), Etherscan, The Graph
- **Social & prediction** — LunarCrush, Neynar (Farcaster), Polymarket

13 sources are keyless. 10 more unlock with free signups. 3 paid keys add deeper coverage. Modules without their key gracefully no-op, so a partial install still works — and `tckr status` tells you what's live right now.

A second, fuller example:

```python
import asyncio
from tckr import quotes, geckoterminal, defillama

async def main():
    print(await quotes.get(["BTC", "ETH", "SOL", "HYPE"]))

    # Trending DEX pools on Base
    for p in await geckoterminal.trending_pools("base", limit=5):
        print(p["name"], p["price_usd"], p["volume_24h_usd"])

    # Base chain TVL + top protocols
    print(await defillama.chain("base"))

asyncio.run(main())
```

## Wire it into agents

Spawn the universal MCP server from any MCP-compatible client (Claude Desktop, Claude Code, Cline, Continue.dev, custom orchestrators):

```bash
pip install tckr[agent-mcp]
tckr-mcp        # listens on stdio
```

Or import the adapter for your platform:

```python
# Claude Agent SDK
from tckr.agent_toolkit.adapters.claude_sdk import build_crypto_mcp_server

# OpenAI / Anthropic function calling
from tckr.agent_toolkit.adapters.openai import get_openai_tools, get_anthropic_tools, execute_tool

# LangChain
from tckr.agent_toolkit.adapters.langchain import get_langchain_tools
```

All adapters serve the same 44 tools from one platform-neutral core. Each tool description auto-injects a tier tag (`[keyless]`, `[keyed-free: needs X]`, `[paid OK]`) from the capability registry, so the model knows what'll work before it tries. A `capabilities` introspection tool lets the agent self-discover the live state:

```python
import tckr
print(tckr.capabilities()["summary"])
# {'total': 26, 'configured': 14, 'by_tier': {'keyless-free': 13, 'keyed-free': 10, 'keyed-paid': 3}}
```

## Unlock more with free API keys

Free signups, no credit cards. Ranked by what we actually use in production:

| Key | What it unlocks | Sign up |
|---|---|---|
| `ALCHEMY_API_KEY` | EVM wallet balances + LP-lock detection on Base / ETH (2 modules) | [alchemy.com](https://alchemy.com) |
| `HELIUS_API_KEY` | Solana RPC + Jito MEV intel + wallet PnL (3 modules) | [helius.dev](https://helius.dev) |
| `COINALYZE_API_KEY` | Cross-exchange perps: funding spread, OI, liquidations (Binance / Bybit / OKX / HL) | [coinalyze.net](https://coinalyze.net) |
| `BIRDEYE_API_KEY` | Solana token analytics, top holders, contract security | [birdeye.so](https://birdeye.so) |
| `ALPACA_API_KEY` + `ALPACA_API_SECRET` | US equity/ETF option chains + greeks + IV (free `indicative` feed; no account funding needed) | [alpaca.markets](https://alpaca.markets) |
| `MORALIS_API_KEY` *or* `BITQUERY_API_KEY` | Pump.fun discovery (new / about-to-bond / graduated). Either one alone is sufficient | [moralis.io](https://moralis.io) / [bitquery.io](https://bitquery.io) |
| `COINGECKO_DEMO_API_KEY` | Higher rate limit on the most-used price endpoint — free tier 429s under any real load | [coingecko.com](https://coingecko.com) |
| `ETHERSCAN_API_KEY` | ~70 EVM chains via the unified V2 API (one key covers ETH, Base, Arb, Op, Polygon, BNB, …) | [etherscan.io](https://etherscan.io) |
| `LUNARCRUSH_API_KEY` | Galaxy Score, AltRank, topic feeds, social sentiment | [lunarcrush.com](https://lunarcrush.com) |
| `THEGRAPH_API_KEY` | Higher-quota subgraph access (Uniswap, Aave, etc.) — public gateway works keyless but throttles fast | [thegraph.com](https://thegraph.com) |

**Tip:** Alchemy + Helius alone open up everything on-chain (EVM + Solana). Add Coinalyze if you care about perps; Birdeye if you focus on Solana memecoins.

## Paid keys for deeper work

These actually buy you something beyond rate-limit bumps:

| Key | What you get |
|---|---|
| `NEYNAR_API_KEY` | Farcaster cast search, channel feeds, trending fungibles — keyless tier only has user lookup |
| `COINGECKO_API_KEY` (Pro) | 500+ req/min plus Pro-only endpoints (top movers, NFT, full historical OHLC) |
| `MESSARI_API_KEY` | Research-grade asset profiles + deep metrics — most useful endpoints moved paid in 2024 |
| `TOKENTERMINAL_API_KEY` | Protocol fundamentals: revenue, fees, P/E, treasury, full historical series |
| `SOLSCAN_API_KEY` (Pro) | Richer Solana transaction parsing, higher rate limit |

All keys are optional. Modules without their key gracefully no-op (return `None` / `[]`); `tckr status` shows what's configured right now.

## Sources

| Module | Tier | Key env var(s) | Provides |
|---|---|---|---|
| `geckoterminal` | keyless | — | DEX pools, tokens, OHLCV (Base / Solana / ETH / …) |
| `dexscreener` | keyless | — | DEX pairs, search, new-pair listings, paid-boost rankings |
| `hyperliquid` | keyless | — | Single-exchange perps: funding, OI, marks, candle history |
| `defillama` | keyless | — | Chain / protocol TVL, DEX volume, stablecoins, yields |
| `goplus` | keyless | — | EVM contract security scans (honeypot, taxes, holders) |
| `honeypot` | keyless | — | EVM sell-simulation (ETH / BSC / Base) |
| `virtuals` | keyless | — | Virtuals Protocol AI-agent launchpad (Base) |
| `clanker` | keyless | — | Clanker Farcaster-native token launcher |
| `coingecko` | keyless / keyed | `COINGECKO_DEMO_API_KEY`, `COINGECKO_API_KEY` | Spot / market / historical prices; trending; categories |
| `polymarket` | keyless | — | Prediction-market odds (binary YES/NO) |
| `cboe` | keyless | — | Option chains + greeks + IV + OI/volume, incl. indices (CBOE delayed ~15m; unofficial). Keyless fallback under the `options` cascade |
| `pyth` | keyless | — | On-chain oracle prices: ~400 feeds (crypto, equities, FX, metals) |
| `solscan` | keyless / keyed | `SOLSCAN_API_KEY` (Pro) | Solana block explorer |
| `thegraph` | keyless / keyed | `THEGRAPH_API_KEY` | Generic GraphQL access to indexed subgraphs |
| `alchemy` | keyed-free | `ALCHEMY_API_KEY` | EVM (Base, ETH) wallet balances + transfers |
| `helius` | keyed-free | `HELIUS_API_KEY` | Solana RPC convenience layer |
| `coinalyze` | keyed-free | `COINALYZE_API_KEY` | Cross-exchange perps: funding spread, OI, liquidations |
| `birdeye` | keyed-free | `BIRDEYE_API_KEY` | Solana token analytics (overview, holders, security) |
| `options` | keyed-free | `ALPACA_API_KEY` + `ALPACA_API_SECRET` | US equity/ETF option chains + greeks + IV (Alpaca; free `indicative` feed) |
| `etherscan` | keyed-free | `ETHERSCAN_API_KEY` | Unified EVM block explorer (~70 chains via `chainid`) |
| `lunarcrush` | keyed-free | `LUNARCRUSH_API_KEY` | Social sentiment: Galaxy Score, AltRank, topic feeds |
| `lp_lock` | keyed-free | `ALCHEMY_API_KEY` | LP-lock detection: Uniswap V2 / V3 / V4 on Base / ETH |
| `jito` | keyed-free | `HELIUS_API_KEY` | Solana MEV: tip floor, bundle status, snipe-score |
| `pumpfun` | keyed-free | `MORALIS_API_KEY` *or* `BITQUERY_API_KEY` (+ `HELIUS_API_KEY` for state) | Pump.fun launchpad: discovery + bonding curve + analytics |
| `wallet_pnl` | keyed-free | composite (Helius / Alchemy / Moralis / Birdeye) | FIFO PnL across Solana + Base wallets |
| `neynar` | keyed-paid | `NEYNAR_API_KEY` | Farcaster cast search, channel feeds, trending |
| `messari` | keyed-paid | `MESSARI_API_KEY` | Research-grade asset profiles, metrics, news |
| `tokenterminal` | keyed-paid | `TOKENTERMINAL_API_KEY` | Protocol fundamentals: revenue, fees, P/E, treasury |

## Composition

Sources are designed to chain. A few examples:

- `pumpfun.live_trades(mint)` → `jito.snipe_score(sigs)` — how bot-sniped is this launch?
- `clanker.new_tokens()[i]["requestor_fid"]` → `neynar.user_popular_casts(fid)` — what's the deployer saying about their token?
- `clanker.new_tokens()[i]["pool_address"]` (V4 PoolId) → `lp_lock(pool_id)` — is the Clanker LP locked?
- `pumpfun.top_traders(mint)` → `wallet_pnl(wallet)` — is the top buyer actually profitable across their other trades?

## Fallback cascades: `tckr.quotes` and `tckr.history`

Real consumers usually want best-available data, not a specific provider. Two cascade modules wrap the common pattern so callers don't reimplement it:

```python
from tckr import quotes, history

# USD spot price, CoinGecko → Hyperliquid fallback
q = await quotes.get(["BTC", "ETH", "NEAR", "HYPE"])
# {'BTC': {'price': 77150.0, 'source': 'coingecko', ...}, 'HYPE': {'price': 63.6, 'source': 'hyperliquid', ...}}

# 30-day daily candles, CoinGecko market_chart → Hyperliquid candleSnapshot
h = await history.candles(["BTC", "HYPE"], days=30)
```

Each result carries a `source` field so callers can detect when fallback ran. The same cascades are exposed as agent tools (`quote`, `candles`).

Hyperliquid is the canonical free-tier fallback: keyless, no rate limit at typical reading volume, and covers ~230 majors + mid-caps — almost the entire CoinGecko "interesting" universe minus long-tail alts (which belong in DEX pool contexts anyway).

## Diagnostics: `tckr.health()` and `tckr update`

Every HTTP call updates a per-provider rolling summary. Read it to see which sources are currently rate-limited or down:

```python
import tckr
print(tckr.health())
# {"coingecko":  {"ok_count": 14, "fail_count": 3, "last_status": 429, "last_429_ts": "..."},
#  "hyperliquid": {"ok_count": 22, "fail_count": 0, "last_status": 200, ...}}
```

Exposed as an agent tool too (`health`) — useful when an agent is reasoning about why data looks thin.

The CLI checks PyPI for new releases once a day and shows an upgrade banner in `tckr status`. To install:

```bash
tckr update              # one-step upgrade
tckr update --check      # dry-run; just report if a new version exists
```

Detects pipx / `uv tool` / PEP 668 system-managed installs and suggests the right command instead of failing. Set `TCKR_NO_UPDATE_CHECK=1` to silence the implicit banner.

## Configuration

All API keys are optional. Set them as env vars (or via a `.env` file picked up by your shell); `tckr status` shows what's configured right now. Cache TTLs and HTTP behavior (timeouts, retries) are tunable via `TCKR_*` env vars — see [`tckr/settings.py`](tckr/settings.py).

## Agent toolkit adapters

The agent toolkit (`tckr.agent_toolkit`) wraps each useful function as a read-only tool with a JSON Schema, then exposes the same registry through four adapters:

| Extra | Adapter | What it gives you |
|---|---|---|
| `tckr[agent-mcp]` | `adapters.mcp_stdio` | Universal MCP stdio server (`tckr-mcp` console script). Works with any MCP-compatible client. |
| `tckr[agent-claude]` | `adapters.claude_sdk` | In-process MCP server for the Claude Agent SDK (`build_crypto_mcp_server()`). |
| `tckr[agent-openai]` | `adapters.openai` | OpenAI function-calling shapes (`get_openai_tools()`, also `get_anthropic_tools()`) + `execute_tool(name, args)` dispatcher. |
| `tckr[agent-langchain]` | `adapters.langchain` | LangChain `StructuredTool` instances (`get_langchain_tools()`). |

`tckr[agent]` installs the two most-used adapters (`agent-claude` + `agent-mcp`); `tckr[agent-all]` installs all four.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development workflow, release process, and how to add new data-source modules or agent tools.

## License

MIT — see [LICENSE](LICENSE).
