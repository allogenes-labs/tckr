# tckr

[![PyPI](https://img.shields.io/pypi/v/tckr.svg)](https://pypi.org/project/tckr/)
[![CI](https://github.com/allogenes-labs/tckr/actions/workflows/ci.yml/badge.svg)](https://github.com/allogenes-labs/tckr/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**An async, cached, gracefully-degrading aggregator over the major free crypto data APIs — plus an agent toolkit that exposes the data to any LLM platform.**

Stitches 26 sources (DEX pools, perps, TVL, on-chain wallets, contract safety, launchpads, MEV, social, prediction markets, oracles, ...) into one clean, typed Python interface. Every call is `async`, TTL-cached per source, and returns `None` / `[]` rather than raising when an upstream fails or a key is missing — so a partial install still works.

```bash
pip install tckr              # data layer only
pip install tckr[agent-mcp]   # + universal MCP stdio server for any LLM
pip install tckr[agent-all]   # + all four agent adapters
```

## Quickstart

### As a data layer

```python
import asyncio
from tckr import geckoterminal, coinalyze, coingecko

async def main():
    # Spot prices via CoinGecko
    px = await coingecko.simple_price("bitcoin,ethereum,solana")
    print(px)  # {'bitcoin': {'usd': 67234.5}, ...}

    # Trending DEX pools on Base
    pools = await geckoterminal.trending_pools("base", limit=5)
    for p in pools:
        print(p["name"], p["price_usd"], p["volume_24h_usd"])

    # Cross-exchange perps funding spread (requires COINALYZE_API_KEY)
    agg = await coinalyze.funding_aggregate("BTC")
    if agg:
        a = agg["aggregate"]
        print(f"BTC funding APR: min {a['min_apr_pct']:.1f}% / "
              f"max {a['max_apr_pct']:.1f}% / spread {a['spread_apr_pct']:.1f}%")

asyncio.run(main())
```

### As an agent toolkit

Spawn the universal MCP server from any MCP-compatible client (Claude Desktop, Claude Code, Cline, Continue.dev, custom orchestrators):

```bash
pip install tckr[agent-mcp]
tckr-mcp        # listens on stdio for the MCP protocol
```

Or wire it into a specific platform:

```python
# Claude Agent SDK
from tckr.agent_toolkit.adapters.claude_sdk import build_crypto_mcp_server

# OpenAI / Anthropic function calling
from tckr.agent_toolkit.adapters.openai import get_openai_tools, get_anthropic_tools, execute_tool

# LangChain
from tckr.agent_toolkit.adapters.langchain import get_langchain_tools
```

All adapters serve the same 44 tools from one platform-neutral core. Each tool description carries a tier tag (`[keyless]`, `[keyed-free: needs X]`, `[paid: Y required]`) auto-injected from the capability registry, so the agent knows what will work in the current environment before it tries.

There's also a `capabilities` introspection tool — agents can call it once to learn what's configured:

```python
import tckr
print(tckr.capabilities()["summary"])
# {'total': 26, 'configured': 14, 'by_tier': {'keyless-free': 13, 'keyed-free': 10, 'keyed-paid': 3}}
```

Or from the shell:

```bash
tckr status
```

## Sources

| Module | Tier | Key env var(s) | Provides |
|---|---|---|---|
| `geckoterminal` | keyless | — | DEX pools, tokens, OHLCV (Base / Solana / ETH / …) |
| `dexscreener` | keyless | — | DEX pairs, search, new-pair listings, paid-boost rankings |
| `hyperliquid` | keyless | — | Single-exchange perps: funding, OI, marks |
| `defillama` | keyless | — | Chain / protocol TVL, DEX volume, stablecoins, yields |
| `goplus` | keyless | — | EVM contract security scans (honeypot, taxes, holders) |
| `honeypot` | keyless | — | EVM sell-simulation (ETH / BSC / Base) |
| `virtuals` | keyless | — | Virtuals Protocol AI-agent launchpad (Base) |
| `clanker` | keyless | — | Clanker Farcaster-native token launcher |
| `coingecko` | keyless / keyed | `COINGECKO_DEMO_API_KEY`, `COINGECKO_API_KEY` | Spot / market / historical prices; trending; categories |
| `polymarket` | keyless | — | Prediction-market odds (binary YES/NO) |
| `pyth` | keyless | — | On-chain oracle prices: ~400 feeds (crypto, equities, FX, metals) |
| `solscan` | keyless / keyed | `SOLSCAN_API_KEY` (Pro) | Solana block explorer |
| `thegraph` | keyless / keyed | `THEGRAPH_API_KEY` | Generic GraphQL access to indexed subgraphs |
| `alchemy` | keyed-free | `ALCHEMY_API_KEY` | EVM (Base, ETH) wallet balances + transfers |
| `helius` | keyed-free | `HELIUS_API_KEY` | Solana RPC convenience layer |
| `coinalyze` | keyed-free | `COINALYZE_API_KEY` | Cross-exchange perps: funding spread, OI, liquidations |
| `birdeye` | keyed-free | `BIRDEYE_API_KEY` | Solana token analytics (overview, holders, security) |
| `etherscan` | keyed-free | `ETHERSCAN_API_KEY` | Unified EVM block explorer (~70 chains via `chainid`) |
| `lunarcrush` | keyed-free | `LUNARCRUSH_API_KEY` | Social sentiment: Galaxy Score, AltRank, topic feeds |
| `lp_lock` | keyed-free | `ALCHEMY_API_KEY` | LP-lock detection: Uniswap V2 / V3 / V4 on Base / ETH |
| `jito` | keyed-free | `HELIUS_API_KEY` | Solana MEV: tip floor, bundle status, snipe-score |
| `pumpfun` | keyed-free | one of `MORALIS_API_KEY` / `BITQUERY_API_KEY` (+ `HELIUS_API_KEY` for state) | Pump.fun launchpad: discovery + bonding curve + analytics |
| `wallet_pnl` | keyed-free | composite (Helius / Alchemy / Moralis / Birdeye) | FIFO PnL across Solana + Base wallets |
| `neynar` | keyed-paid | `NEYNAR_API_KEY` | Farcaster cast search, channel feeds, trending |
| `messari` | keyed-paid | `MESSARI_API_KEY` | Research-grade asset profiles, metrics, news |
| `tokenterminal` | keyed-paid | `TOKENTERMINAL_API_KEY` | Protocol fundamentals: revenue, fees, P/E, treasury |

Tiers: **keyless** = no signup required; **keyed-free** = free signup, key required; **keyed-paid** = useful endpoints require a paid plan (free tiers degrade gracefully).

## Composition

Sources are designed to chain. A few examples:

- `pumpfun.live_trades(mint)` → `jito.snipe_score(sigs)` — how bot-sniped is this launch?
- `clanker.new_tokens()[i]["requestor_fid"]` → `neynar.user_popular_casts(fid)` — what's the deployer saying about their token?
- `clanker.new_tokens()[i]["pool_address"]` (V4 PoolId) → `lp_lock(pool_id)` — is the Clanker LP locked?
- `pumpfun.top_traders(mint)` → `wallet_pnl(wallet)` — is the top buyer actually profitable across their other trades?

## Configuration

All API keys are optional. Modules without a key still work in keyless mode (or skip if they require one). Set the keys for sources you want to use; `tckr status` (or `tckr.capabilities()`) shows what's configured.

Cache TTLs and HTTP behavior (timeouts, retries) are tunable via `TCKR_*` env vars — see [`tckr/settings.py`](tckr/settings.py) for the full list of knobs.

## Agent toolkit details

The agent toolkit (`tckr.agent_toolkit`) wraps each useful function as a read-only tool with a JSON Schema, then exposes the same registry through four adapters:

| Extra | Adapter | What it gives you |
|---|---|---|
| `tckr[agent-mcp]` | `adapters.mcp_stdio` | Universal MCP stdio server (`tckr-mcp` console script). Works with any MCP-compatible client. |
| `tckr[agent-claude]` | `adapters.claude_sdk` | In-process MCP server for the Claude Agent SDK (`build_crypto_mcp_server()`). |
| `tckr[agent-openai]` | `adapters.openai` | OpenAI function-calling shapes (`get_openai_tools()`, also `get_anthropic_tools()`) + `execute_tool(name, args)` dispatcher. |
| `tckr[agent-langchain]` | `adapters.langchain` | LangChain `StructuredTool` instances (`get_langchain_tools()`). |

`tckr[agent]` is a convenience meta-extra installing the two most-used adapters (`agent-claude` + `agent-mcp`); `tckr[agent-all]` installs all four.

A single `core.augment_description(spec)` function reads `tckr.registry.tier_tag(module)` and prepends the tier marker to each tool description, so every adapter shows the agent the same tier-aware metadata.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development workflow, release process, and how to add new data-source modules or agent tools.

## License

MIT — see [LICENSE](LICENSE).
