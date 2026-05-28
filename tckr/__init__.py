"""tckr — a reusable async crypto data layer + agent toolkit.

Import source modules directly:

    geckoterminal   DEX pools, tokens by address, OHLCV (Base, Solana, …)
    dexscreener     DEX pairs, search, new-pair discovery, paid-boost rankings
    hyperliquid     perps: funding, open interest, marks, candle history
    coinalyze       perps cross-exchange: funding spread, OI, liquidations
    defillama       chain/protocol TVL, DEX volume, stablecoins, yields
    goplus          EVM token contract security scans (honeypot, taxes, holders)
    honeypot        EVM sell-simulation backstop (subset of chains)
    birdeye         Solana-focused token analytics (holders, trades, security)
    pumpfun         Solana memecoin launchpad: discovery + bonding-curve state
    neynar          Farcaster API: cast search, channel feeds, trending fungibles
    wallet_pnl      FIFO position tracking across Solana + Base wallets
    lp_lock         LP-lock detection (Base / EVM) via known locker contracts
    virtuals        Virtuals Protocol AI-agent launchpad (Base, multi-chain)
    clanker         Clanker Farcaster-native token launcher (Base, multi-chain)
    bankr           Bankr launchpad feed (Doppler/Base + Raydium/Solana); X social attribution
    jito            Solana MEV: tip floor, bundle status, snipe-score for txs
    alchemy         on-chain wallet balances + transfers
    helius          Solana RPC convenience layer
    coingecko       canonical spot / market / historical prices (v3 + Pro)
    polymarket      Polymarket Gamma API: prediction-market odds
    pyth            Pyth Network on-chain oracle prices (~400 feeds, keyless)
    etherscan       Etherscan V2 unified EVM block explorer (~70 chains, one key)
    solscan         Solana block explorer (public + Pro paths)
    lunarcrush      social sentiment: Galaxy Score, AltRank, topic feeds
    messari         research-grade asset profiles, metrics, news
    tokenterminal   protocol fundamentals (revenue, P/E, treasury)
    thegraph        GraphQL access to indexed subgraphs (Uniswap, Aave, ...)

Unified cascades (best-effort across providers):

    quotes          USD spot price cascade: CoinGecko → Hyperliquid
    history         daily candle cascade:   CoinGecko market_chart → HL candleSnapshot

Use the cascades when you want "best available" data without choosing a
provider. They carry a `source` field on each result so the caller can tell
which upstream answered.

Every network call is async, cached (tckr.cache.TTLCache), and degrades
gracefully — it returns None / [] rather than raising when an upstream fails.
Modules that need an API key log a warning and return empty when the key is
absent rather than crashing the caller.

Per-provider health: `tckr.health()` returns a rolling summary (ok_count,
fail_count, last_status, last_error, last_429_ts) for every provider the
process has touched. Useful when an agent is reasoning about why data looks
thin ("CoinGecko is rate-limited right now → HL fallback is doing the work").

Capability registry: `tckr.registry` tracks per-module tier and which
env vars unlock each. `capabilities()` returns the live state as JSON; the CLI
`tckr status` prints it. The same registry powers tier tags on tool
descriptions in `tckr.agent_toolkit`.

Agent toolkit (optional extras): `pip install tckr[agent-claude]` for
the Claude Agent SDK in-process MCP server, `tckr[agent-mcp]` for the
universal stdio MCP server (console script `tckr-mcp`, works with any
MCP client), `tckr[agent-openai]` for OpenAI function-calling, or
`tckr[agent-langchain]` for LangChain `StructuredTool` wrappers.
"""
from __future__ import annotations

from tckr._http import health  # re-exported for convenience
from tckr.registry import capabilities  # re-exported for convenience

__version__ = "0.2.4"

__all__ = ["capabilities", "health", "__version__"]
