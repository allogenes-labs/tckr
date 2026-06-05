"""Platform-agnostic tool definitions for the tckr agent toolkit.

Each tool is a thin async wrapper over a `tckr` function that returns a
plain dict / list / scalar — NO MCP envelope, NO SDK-specific decorators. The
per-platform adapters in `tckr.agent_toolkit.adapters.*` consume the
`TOOLS` list and translate to the platform's tool API.

Why platform-neutral: the toolkit started life as Claude-only (see commit
history). When it became clear the underlying tools were valuable across LLM
platforms, this layer was extracted so the same registry could feed Claude SDK
in-process MCP, the universal MCP stdio protocol, OpenAI function-calling, and
LangChain.

A tool function:
- takes a single `args: dict` (matching its JSON Schema)
- returns a JSON-serializable result on success
- raises Exception on failure — adapters decide how to surface errors

The `@register_tool(...)` decorator captures `name`, `description`, `module`
(the tckr module name used for tier-tag lookup), and the input JSON
schema, appending a `ToolSpec` to the global `TOOLS` list.

Result truncation: list-returning tools cap output at `MAX_ROWS` (default 25)
to keep tool results within the agent's context budget. The agent can re-call
with a narrower filter if it needs more.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from tckr import registry

log = logging.getLogger("tckr.agent_toolkit")

MAX_ROWS = 25                # default cap for list-returning tools


# ============================================================================
# ToolSpec + registration decorator
# ============================================================================

@dataclass(frozen=True)
class ToolSpec:
    """Adapter-neutral description of one tool.

    Attributes
    ----------
    name        — short identifier; adapters may namespace it (e.g. MCP uses
                  `mcp__crypto__<name>`).
    description — raw human-readable description WITHOUT the tier tag.
                  Use `augment_description(spec)` to get the tagged version.
    module      — tckr module name (key in `registry.REGISTRY`). Drives
                  the tier-tag prefix. Use "" for tools that aren't tied to a
                  single data source (e.g. `capabilities`).
    schema      — JSON Schema dict for input arguments.
    callable    — `async (args: dict) -> Any` — the tool body. Returns
                  JSON-serializable data; raises on error.
    """
    name: str
    description: str
    module: str
    schema: dict
    callable: Callable[[dict], Awaitable[Any]]


TOOLS: list[ToolSpec] = []


def register_tool(name: str, description: str, module: str, schema: dict):
    """Decorator: register an async tool function in the global TOOLS list."""
    def deco(fn: Callable[[dict], Awaitable[Any]]) -> Callable[[dict], Awaitable[Any]]:
        spec = ToolSpec(
            name=name,
            description=description.strip(),
            module=module,
            schema=schema,
            callable=fn,
        )
        TOOLS.append(spec)
        return fn
    return deco


def augment_description(spec: ToolSpec) -> str:
    """Return `spec.description` with the registry tier tag prepended.

    For tools without a module (e.g. `capabilities`), returns the description
    unchanged.
    """
    if not spec.module:
        return spec.description
    tag = registry.tier_tag(spec.module)
    return f"{tag} {spec.description}"


def get_tool(name: str) -> ToolSpec | None:
    """Look up a tool by name."""
    for t in TOOLS:
        if t.name == name:
            return t
    return None


def _cap(rows: list | None, limit: int | None = None) -> list:
    """Truncate a list to `limit` (default MAX_ROWS) for prompt-size safety."""
    rows = rows or []
    n = int(limit) if limit else MAX_ROWS
    n = max(1, min(n, MAX_ROWS))
    return rows[:n]


# ============================================================================
# Capabilities introspection tool — agents call this once to learn what's set up
# ============================================================================

@register_tool(
    "capabilities",
    "List every tckr module + its access tier (keyless / keyed-free / "
    "keyed-paid) and whether it is currently configured in this environment. "
    "Call this ONCE at session start to learn which tools will actually work — "
    "unconfigured tools still appear in the tool list but will error when called.",
    module="",
    schema={"type": "object", "properties": {}},
)
async def _t_capabilities(args: dict) -> dict:
    return registry.capabilities()


# ============================================================================
# Hyperliquid: perps marks, funding, OI, orderbook
# ============================================================================

@register_tool(
    "hl_perp",
    "Hyperliquid perp snapshot for one symbol — mark price, 24h change, funding "
    "rate (hourly + annualized APR), open interest (base and USD), 24h volume, "
    "max leverage. Use to read perps sentiment: extreme funding APR (>50% or "
    "<-50%) signals crowded positioning.",
    module="hyperliquid",
    schema={
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "Perp coin name, e.g. BTC, ETH, SOL, HYPE, ARB"},
        },
        "required": ["symbol"],
    },
)
async def _t_hl_perp(args: dict) -> dict:
    from tckr import hyperliquid as hl
    return await hl.perp(args["symbol"])


@register_tool(
    "hl_funding_history",
    "Recent hourly funding rates for a Hyperliquid perp. Returns chronological "
    "list of {t, funding_rate_hourly, premium}. Use to spot funding regime "
    "changes (sustained positive funding = longs paying = bullish crowding).",
    module="hyperliquid",
    schema={
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "Perp coin name, e.g. BTC, ETH"},
            "hours":  {"type": "integer", "description": "Lookback window in hours (default 24, max 168)", "default": 24},
        },
        "required": ["symbol"],
    },
)
async def _t_hl_funding_history(args: dict):
    from tckr import hyperliquid as hl
    hours = max(1, min(int(args.get("hours", 24)), 168))
    return await hl.funding_history(args["symbol"], hours=hours)


@register_tool(
    "hl_orderbook",
    "Top-of-book L2 orderbook for a Hyperliquid perp. Returns {symbol, ts, bids, asks} "
    "with each level as {px, sz, n}. Use to gauge book depth / spread before sizing "
    "a trade thesis around an instrument that's also tradable on Hyperliquid.",
    module="hyperliquid",
    schema={
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "Perp coin name"},
            "depth":  {"type": "integer", "description": "Levels per side (default 5, max 10)", "default": 5},
        },
        "required": ["symbol"],
    },
)
async def _t_hl_orderbook(args: dict):
    from tckr import hyperliquid as hl
    depth = max(1, min(int(args.get("depth", 5)), 10))
    return await hl.l2_book(args["symbol"], depth=depth)


@register_tool(
    "hl_candles",
    "OHLCV candle history for a Hyperliquid perp. Returns "
    "{symbol, interval, candles: [{t, o, h, l, c, v}, ...]} chronological. "
    "interval ∈ {1m,5m,15m,30m,1h,4h,1d,1w}. Lighter than full charting — use "
    "for short technical reads (recent breakouts, 14-day momentum).",
    module="hyperliquid",
    schema={
        "type": "object",
        "properties": {
            "symbol":   {"type": "string", "description": "Perp coin name, e.g. BTC, ETH, HYPE"},
            "interval": {"type": "string", "description": "Candle interval (default 1d)", "default": "1d"},
            "limit":    {"type": "integer", "description": "Number of candles back from now (default 30, max 500)", "default": 30},
        },
        "required": ["symbol"],
    },
)
async def _t_hl_candles(args: dict):
    from tckr import hyperliquid as hl
    limit = max(1, min(int(args.get("limit", 30)), 500))
    interval = args.get("interval") or "1d"
    return await hl.candles(args["symbol"], interval=interval, limit=limit)


# ============================================================================
# Unified cascade tools — best-effort price/history across providers
# ============================================================================

@register_tool(
    "quote",
    "Best-effort USD spot price for one or more symbols, cascading "
    "CoinGecko → Hyperliquid so a rate-limited CG falls through to HL marks. "
    "Prefer this over `cg_simple_price` or `hl_perp` when you only need a price "
    "and don't care which source answers. Returns {symbol: {symbol, price, "
    "source, ts}}; unresolvable symbols absent.",
    module="",  # cascade — not tied to a single registry module
    schema={
        "type": "object",
        "properties": {
            "symbols": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of upper-case ticker symbols, e.g. ['BTC','ETH','NEAR']",
            },
        },
        "required": ["symbols"],
    },
)
async def _t_quote(args: dict) -> dict:
    from tckr import quotes
    symbols = args.get("symbols") or []
    if isinstance(symbols, str):
        symbols = [symbols]
    return await quotes.get(symbols)


@register_tool(
    "candles",
    "Best-effort daily candle history for one or more symbols, cascading "
    "CoinGecko `market_chart` → Hyperliquid `candles`. Prefer this when you "
    "just want closes + volumes and don't care which source answers. Returns "
    "{symbol: {symbol, interval, closes, volumes, source}}; symbols no source "
    "could resolve are absent. Volume scale depends on source — check `source`.",
    module="",
    schema={
        "type": "object",
        "properties": {
            "symbols": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of upper-case symbols",
            },
            "days": {"type": "integer", "description": "Lookback in days (default 30, max 365)", "default": 30},
        },
        "required": ["symbols"],
    },
)
async def _t_candles(args: dict) -> dict:
    from tckr import history
    symbols = args.get("symbols") or []
    if isinstance(symbols, str):
        symbols = [symbols]
    days = max(1, min(int(args.get("days", 30)), 365))
    return await history.candles(symbols, days=days)


@register_tool(
    "health",
    "Per-provider HTTP health snapshot — counts, last status code, last error, "
    "and last rate-limit timestamp. Useful to diagnose 'why is my data thin' "
    "(e.g., CoinGecko 429s right now → expect HL fallback to be doing the work).",
    module="",
    schema={"type": "object", "properties": {}},
)
async def _t_health(args: dict) -> dict:
    import tckr
    return tckr.health()


# ============================================================================
# DefiLlama: TVL, DEX volume, yields
# ============================================================================

@register_tool(
    "dl_chain_tvl",
    "TVL snapshot for one chain by name or canonical id (base, solana, ethereum). "
    "Returns {name, tvl_usd, token_symbol, gecko_id, chain_id}.",
    module="defillama",
    schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Chain name or canonical id (base, solana, ethereum)"},
        },
        "required": ["name"],
    },
)
async def _t_dl_chain_tvl(args: dict):
    from tckr import defillama as dl
    return await dl.chain(args["name"])


@register_tool(
    "dl_protocols",
    "Top protocols by TVL, optionally filtered to one chain. Returns list of "
    "{name, slug, category, chain, chains, tvl_usd, change_1d, change_7d, mcap_usd}.",
    module="defillama",
    schema={
        "type": "object",
        "properties": {
            "chain":       {"type": "string",  "description": "Optional chain filter (base, solana, ethereum)"},
            "limit":       {"type": "integer", "description": f"Max rows (default 20, max {MAX_ROWS})", "default": 20},
            "min_tvl_usd": {"type": "number",  "description": "Minimum TVL floor in USD", "default": 0},
        },
    },
)
async def _t_dl_protocols(args: dict):
    from tckr import defillama as dl
    rows = await dl.protocols(
        chain=args.get("chain"),
        min_tvl_usd=float(args.get("min_tvl_usd", 0)),
    )
    return _cap(rows, args.get("limit", 20))


@register_tool(
    "dl_protocol",
    "Detailed snapshot for one protocol by slug (e.g. 'aerodrome-v1', 'uniswap-v3', "
    "'jito-liquid-staking'). Returns {slug, name, symbol, category, chains, tvl_usd, "
    "current_chain_tvls, url, description}.",
    module="defillama",
    schema={
        "type": "object",
        "properties": {
            "slug": {"type": "string", "description": "DefiLlama protocol slug"},
        },
        "required": ["slug"],
    },
)
async def _t_dl_protocol(args: dict):
    from tckr import defillama as dl
    return await dl.protocol(args["slug"])


@register_tool(
    "dl_dex_overview",
    "DEX volume aggregate for one chain: 24h/7d/30d totals plus per-protocol "
    "breakdown sorted by 24h volume. Use to spot the dominant DEX(es) on a chain "
    "and where flow is rotating.",
    module="defillama",
    schema={
        "type": "object",
        "properties": {
            "chain": {"type": "string", "description": "Chain (base, solana, ethereum)"},
        },
        "required": ["chain"],
    },
)
async def _t_dl_dex_overview(args: dict):
    from tckr import defillama as dl
    result = await dl.dex_overview(args["chain"])
    if isinstance(result, dict) and isinstance(result.get("protocols"), list):
        result = dict(result)
        result["protocols"] = result["protocols"][:MAX_ROWS]
    return result


@register_tool(
    "dl_yields",
    "Top yield pools sorted by APY, optionally filtered by chain/project/TVL floor. "
    "Returns {pool, chain, project, symbol, tvl_usd, apy, apy_base, apy_reward, "
    "stablecoin, il_risk, exposure}. Default filter: $1M+ TVL to avoid degen dust.",
    module="defillama",
    schema={
        "type": "object",
        "properties": {
            "chain":       {"type": "string",  "description": "Optional chain filter"},
            "project":     {"type": "string",  "description": "Optional project filter (e.g. 'aave-v3')"},
            "min_tvl_usd": {"type": "number",  "description": "Minimum TVL floor (default 1_000_000)", "default": 1_000_000},
            "limit":       {"type": "integer", "description": f"Max rows (default 20, max {MAX_ROWS})", "default": 20},
        },
    },
)
async def _t_dl_yields(args: dict):
    from tckr import defillama as dl
    rows = await dl.yields(
        chain=args.get("chain"),
        project=args.get("project"),
        min_tvl_usd=float(args.get("min_tvl_usd", 1_000_000)),
    )
    return _cap(rows, args.get("limit", 20))


# ============================================================================
# GeckoTerminal: DEX pools, tokens, OHLCV
# ============================================================================

@register_tool(
    "gt_trending_pools",
    "Pools trending on a network right now (strongest signal first). Returns "
    "list of {network, pool_address, name, dex, base_token, quote_token, "
    "price_usd, fdv_usd, reserve_usd, volume_24h_usd, price_change_pct, transactions, created_at}.",
    module="geckoterminal",
    schema={
        "type": "object",
        "properties": {
            "network": {"type": "string",  "description": "Network id (base, solana, eth)", "default": "base"},
            "limit":   {"type": "integer", "description": f"Max rows (default 10, max {MAX_ROWS})", "default": 10},
        },
    },
)
async def _t_gt_trending_pools(args: dict):
    from tckr import geckoterminal as gt
    rows = await gt.trending_pools(network=args.get("network", "base"))
    return _cap(rows, args.get("limit", 10))


@register_tool(
    "gt_new_pools",
    "Most recently created pools on a network — a new-launch radar. Same shape "
    "as gt_trending_pools. Use to discover plays BEFORE they hit CoinGecko.",
    module="geckoterminal",
    schema={
        "type": "object",
        "properties": {
            "network": {"type": "string",  "description": "Network id (base, solana, eth)", "default": "base"},
            "limit":   {"type": "integer", "description": f"Max rows (default 10, max {MAX_ROWS})", "default": 10},
        },
    },
)
async def _t_gt_new_pools(args: dict):
    from tckr import geckoterminal as gt
    rows = await gt.new_pools(network=args.get("network", "base"))
    return _cap(rows, args.get("limit", 10))


@register_tool(
    "gt_top_pools",
    "Highest liquidity/volume pools on a network. Use to find the deepest venues "
    "for a quote-token (USDC, SOL, WETH) on a given chain.",
    module="geckoterminal",
    schema={
        "type": "object",
        "properties": {
            "network": {"type": "string",  "description": "Network id", "default": "base"},
            "limit":   {"type": "integer", "description": f"Max rows (default 10, max {MAX_ROWS})", "default": 10},
        },
    },
)
async def _t_gt_top_pools(args: dict):
    from tckr import geckoterminal as gt
    rows = await gt.top_pools(network=args.get("network", "base"))
    return _cap(rows, args.get("limit", 10))


@register_tool(
    "gt_token_info",
    "Token snapshot by contract address on a specific network. Returns {network, "
    "address, symbol, name, decimals, price_usd, fdv_usd, market_cap_usd, "
    "total_reserve_usd, volume_24h_usd, total_supply, coingecko_id}.",
    module="geckoterminal",
    schema={
        "type": "object",
        "properties": {
            "network": {"type": "string", "description": "Network id (base, solana, eth)"},
            "address": {"type": "string", "description": "Token contract address"},
        },
        "required": ["network", "address"],
    },
)
async def _t_gt_token_info(args: dict):
    from tckr import geckoterminal as gt
    return await gt.token_info(args["network"], args["address"])


@register_tool(
    "gt_pool_ohlcv",
    "OHLCV candles for a DEX pool. Returns {network, pool_address, timeframe, "
    "base, quote, candles[]} where each candle is {t, o, h, l, c, v}. Use for "
    "technical analysis on a specific pool (e.g. confirming a breakout on a "
    "newly-trending token).",
    module="geckoterminal",
    schema={
        "type": "object",
        "properties": {
            "network":      {"type": "string",  "description": "Network id"},
            "pool_address": {"type": "string",  "description": "Pool address (from gt_trending_pools etc)"},
            "timeframe":    {"type": "string",  "description": "day | hour | minute", "default": "hour"},
            "limit":        {"type": "integer", "description": "Number of candles (default 24, max 100)", "default": 24},
        },
        "required": ["network", "pool_address"],
    },
)
async def _t_gt_pool_ohlcv(args: dict):
    from tckr import geckoterminal as gt
    return await gt.pool_ohlcv(
        args["network"],
        args["pool_address"],
        timeframe=args.get("timeframe", "hour"),
        limit=max(1, min(int(args.get("limit", 24)), 100)),
    )


# ============================================================================
# Dexscreener: cross-source pairs, search, new launches
# ============================================================================

@register_tool(
    "ds_search",
    "Search DEX pairs by free-text query (symbol, name, or address). Cross-chain "
    "by default; filter with `chain`. Use when you want a second source on a "
    "token you saw trending on GeckoTerminal, or to look up a symbol whose chain "
    "you don't know.",
    module="dexscreener",
    schema={
        "type": "object",
        "properties": {
            "query": {"type": "string",  "description": "Symbol, name, or address"},
            "chain": {"type": "string",  "description": "Optional chain filter (base, solana, ethereum)"},
            "limit": {"type": "integer", "description": f"Max rows (default 15, max {MAX_ROWS})", "default": 15},
        },
        "required": ["query"],
    },
)
async def _t_ds_search(args: dict):
    from tckr import dexscreener as ds
    rows = await ds.search(args["query"], chain=args.get("chain"))
    return _cap(rows, args.get("limit", 15))


@register_tool(
    "ds_token_pairs",
    "All DEX pairs for one token contract address (every venue / quote-token "
    "pairing). Use to find the deepest pair for a token before trading the "
    "underlying. Filter to one chain with `chain`.",
    module="dexscreener",
    schema={
        "type": "object",
        "properties": {
            "address": {"type": "string",  "description": "Token contract address"},
            "chain":   {"type": "string",  "description": "Optional chain filter"},
            "limit":   {"type": "integer", "description": f"Max rows (default 15, max {MAX_ROWS})", "default": 15},
        },
        "required": ["address"],
    },
)
async def _t_ds_token_pairs(args: dict):
    from tckr import dexscreener as ds
    rows = await ds.token_pairs(args["address"], chain=args.get("chain"))
    return _cap(rows, args.get("limit", 15))


@register_tool(
    "ds_pair",
    "Single DEX pair lookup by chain + pair address. Cheaper than ds_token_pairs "
    "when you already know the specific pair you want. Returns {chain, dex, "
    "pair_address, base_token, quote_token, price_usd, liquidity_usd, "
    "volume, price_change_pct, txns, created_at}.",
    module="dexscreener",
    schema={
        "type": "object",
        "properties": {
            "chain":        {"type": "string", "description": "Chain id (base, solana, ethereum, bsc)"},
            "pair_address": {"type": "string", "description": "DEX pair contract address"},
        },
        "required": ["chain", "pair_address"],
    },
)
async def _t_ds_pair(args: dict):
    from tckr import dexscreener as ds
    return await ds.pair(args["chain"], args["pair_address"])


@register_tool(
    "ds_latest_profiles",
    "Most recently listed token profiles from Dexscreener. A second-source new-launch "
    "radar that complements gt_new_pools (Dexscreener-specific listings). Returns "
    "{chain, token_address, url, description, icon, links}. Filter to one chain with `chain`.",
    module="dexscreener",
    schema={
        "type": "object",
        "properties": {
            "chain": {"type": "string",  "description": "Optional chain filter (base, solana, ethereum)"},
            "limit": {"type": "integer", "description": f"Max rows (default 15, max {MAX_ROWS})", "default": 15},
        },
    },
)
async def _t_ds_latest_profiles(args: dict):
    from tckr import dexscreener as ds
    rows = await ds.latest_token_profiles(chain=args.get("chain"))
    return _cap(rows, args.get("limit", 15))


# ============================================================================
# Coinalyze: cross-exchange perps (funding spread, OI, liquidations)
# ============================================================================

@register_tool(
    "cz_funding_aggregate",
    "Cross-exchange funding spread for one coin. Discovers every perp market for "
    "the base symbol across exchanges (Binance, Bybit, OKX, Hyperliquid, ...), "
    "queries current funding, and rolls up {per_exchange: [...], aggregate: "
    "{min_apr_pct, max_apr_pct, median_apr_pct, mean_apr_pct, spread_apr_pct, "
    "n_exchanges}}. A wide spread = structural sentiment dispersion across venues "
    "= often a trade. Use when hl_perp shows extreme Hyperliquid funding and you "
    "want to know if other exchanges agree.",
    module="coinalyze",
    schema={
        "type": "object",
        "properties": {
            "base": {"type": "string", "description": "Base coin symbol (e.g. BTC, ETH, SOL, HYPE)"},
        },
        "required": ["base"],
    },
)
async def _t_cz_funding_aggregate(args: dict):
    from tckr import coinalyze as cz
    return await cz.funding_aggregate(args["base"])


@register_tool(
    "cz_funding_extremes",
    "Discovery tool: scan the major-coin universe and return the biggest funding "
    "outliers across exchanges. Returns {most_positive, most_negative, biggest_spread} "
    "lists. Use as a sentiment-screening pass at the start of a turn — "
    "extreme funding is where crowded trades live, and crowded trades have edge "
    "in both directions (squeeze risk or mean-reversion).",
    module="coinalyze",
    schema={
        "type": "object",
        "properties": {
            "top_n": {"type": "integer", "description": "How many entries per ranking (default 5, max 15)", "default": 5},
        },
    },
)
async def _t_cz_funding_extremes(args: dict):
    from tckr import coinalyze as cz
    top_n = max(1, min(int(args.get("top_n", 5)), 15))
    return await cz.funding_extremes(top_n=top_n)


@register_tool(
    "cz_liquidations",
    "Recent liquidation aggregates for a perp symbol across all exchanges. Returns "
    "bins of {t, long_liquidations_usd, short_liquidations_usd}. Big long-liquidation "
    "spikes often mark capitulation lows; big short-liquidation spikes mark squeeze tops. "
    "Use to time entries after a cascade.",
    module="coinalyze",
    schema={
        "type": "object",
        "properties": {
            "base":     {"type": "string",  "description": "Base coin symbol (e.g. BTC, ETH)"},
            "hours":    {"type": "integer", "description": "Lookback hours (default 24, max 168)", "default": 24},
            "interval": {"type": "string",  "description": "Bin size: 5min, 15min, 30min, 1hour, 4hour, daily", "default": "1hour"},
        },
        "required": ["base"],
    },
)
async def _t_cz_liquidations(args: dict):
    from tckr import coinalyze as cz
    base = (args["base"] or "").strip().upper()
    if not base:
        raise ValueError("base symbol required")
    mkts = await cz.markets(base=base)
    perp_syms = [m["symbol"] for m in (mkts or []) if m.get("is_perpetual") and m.get("symbol")]
    if not perp_syms:
        raise LookupError(f"no perp markets found for base {base!r}")
    syms = perp_syms[:3]
    hours = max(1, min(int(args.get("hours", 24)), 168))
    interval = str(args.get("interval", "1hour"))
    rows = await cz.liquidations(syms, interval=interval, hours=hours)
    return _cap(rows, MAX_ROWS)


# ============================================================================
# CoinGecko: canonical spot/market/historical (Phase 5)
# ============================================================================

@register_tool(
    "cg_simple_price",
    "Fast multi-coin spot price lookup via CoinGecko. Use when you have a list "
    "of coin ids (NOT symbols — use cg_search to translate symbols first) and "
    "just want current price + optional market cap / 24h vol / 24h change.",
    module="coingecko",
    schema={
        "type": "object",
        "properties": {
            "ids":                 {"type": "string", "description": "Comma-separated CoinGecko coin ids (e.g. 'bitcoin,ethereum,solana')"},
            "vs_currencies":       {"type": "string", "description": "Comma-separated fiat/crypto vs currencies (e.g. 'usd,eur,btc')", "default": "usd"},
            "include_market_cap":  {"type": "boolean", "default": False},
            "include_24h_vol":     {"type": "boolean", "default": False},
            "include_24h_change":  {"type": "boolean", "default": True},
        },
        "required": ["ids"],
    },
)
async def _t_cg_simple_price(args: dict):
    from tckr import coingecko as cg
    return await cg.simple_price(
        args["ids"],
        vs_currencies=args.get("vs_currencies", "usd"),
        include_market_cap=bool(args.get("include_market_cap", False)),
        include_24h_vol=bool(args.get("include_24h_vol", False)),
        include_24h_change=bool(args.get("include_24h_change", True)),
    )


@register_tool(
    "cg_coin_markets",
    "Top coins by market cap with full data (rank, mcap, FDV, 24h vol, "
    "price-change for 1h/24h/7d, ATH/ATL). Optionally filter by category "
    "(e.g. 'layer-1', 'meme-token', 'real-world-assets-rwa', 'artificial-"
    "intelligence'). Use as a discovery pass when you want the current top-N "
    "or to compare names in one sector.",
    module="coingecko",
    schema={
        "type": "object",
        "properties": {
            "vs_currency": {"type": "string",  "default": "usd"},
            "category":    {"type": "string",  "description": "Optional CoinGecko category id"},
            "ids":         {"type": "string",  "description": "Optional comma-separated coin ids"},
            "order":       {"type": "string",  "default": "market_cap_desc"},
            "per_page":    {"type": "integer", "default": 25, "description": f"Max rows (1-{MAX_ROWS}); upstream cap 250"},
        },
    },
)
async def _t_cg_coin_markets(args: dict):
    from tckr import coingecko as cg
    rows = await cg.coin_markets(
        vs_currency=args.get("vs_currency", "usd"),
        category=args.get("category"),
        ids=args.get("ids"),
        order=args.get("order", "market_cap_desc"),
        per_page=int(args.get("per_page", 25)),
    )
    return _cap(rows, args.get("per_page", 25))


@register_tool(
    "cg_market_chart",
    "Historical price / mcap / volume timeseries for a coin. Use for trend "
    "analysis across days/weeks (sub-daily granularity auto-selected: 1d=5min, "
    "2-90d=hourly, 91+d=daily). Returns {prices, market_caps, total_volumes} "
    "each as [[ts_ms, value], ...].",
    module="coingecko",
    schema={
        "type": "object",
        "properties": {
            "coin_id":     {"type": "string",  "description": "CoinGecko id (e.g. 'bitcoin')"},
            "days":        {"type": "integer", "default": 30, "description": "Lookback days (or pass 365/'max' for long history)"},
            "vs_currency": {"type": "string",  "default": "usd"},
        },
        "required": ["coin_id"],
    },
)
async def _t_cg_market_chart(args: dict):
    from tckr import coingecko as cg
    return await cg.market_chart(
        args["coin_id"],
        days=int(args.get("days", 30)),
        vs_currency=args.get("vs_currency", "usd"),
    )


@register_tool(
    "cg_search",
    "Search CoinGecko for matching coins/exchanges/categories by symbol or "
    "name. Use to translate a ticker (e.g. 'BTC', 'HYPE') into a CoinGecko id "
    "(needed by cg_simple_price / cg_coin / cg_market_chart).",
    module="coingecko",
    schema={
        "type": "object",
        "properties": {
            "query": {"type": "string"},
        },
        "required": ["query"],
    },
)
async def _t_cg_search(args: dict):
    from tckr import coingecko as cg
    return await cg.search(args["query"])


@register_tool(
    "cg_trending",
    "Currently trending search queries on CoinGecko (top 7 coins, top NFTs, "
    "top categories). A second-source signal alongside the equity-style pulse "
    "feed — what retail is currently searching for.",
    module="coingecko",
    schema={"type": "object", "properties": {}},
)
async def _t_cg_trending(args: dict):
    from tckr import coingecko as cg
    return await cg.trending()


@register_tool(
    "cg_global",
    "Global crypto market stats: total market cap, total 24h volume, BTC + ETH "
    "dominance percentages, active coins / markets, exchange count. Use as a "
    "macro-context check before risking on alts.",
    module="coingecko",
    schema={"type": "object", "properties": {}},
)
async def _t_cg_global(args: dict):
    from tckr import coingecko as cg
    return await cg.global_stats()


# ============================================================================
# Polymarket: prediction-market odds (Phase 5)
# ============================================================================

@register_tool(
    "pm_top_volume",
    "Active Polymarket prediction markets sorted by 24h volume — a discovery "
    "pass for what the prediction market is currently pricing. Returns {slug, "
    "question, yes_price, volume, end_date, ...}. NOTE: 24h volume is "
    "cumulative over the last 24 hours; a market with strong 24h volume can "
    "still have a wide instantaneous spread RIGHT NOW because activity died "
    "off. Always run `pm_touch` (or `pm_size_to_fill` for your intended size) "
    "on any candidate before treating it as fillable.",
    module="polymarket",
    schema={
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "default": 20, "description": f"Max rows (max {MAX_ROWS})"},
        },
    },
)
async def _t_pm_top_volume(args: dict):
    from tckr import polymarket as pm
    rows = await pm.top_volume(limit=int(args.get("limit", 20)))
    return _cap(rows, args.get("limit", 20))


@register_tool(
    "pm_market",
    "Single Polymarket market by slug or numeric id. Returns the full market "
    "with current YES price, outcomes, volume, liquidity, end date, tags. "
    "NOTE: `yes_price` / `no_price` here are gamma's AMM midpoints — they can "
    "diverge wildly from the live CLOB touch on thin markets (we've seen a "
    "gamma midpoint of 0.52 against a real best_ask of 0.96). Use `pm_book` "
    "or `pm_touch` to read the actual fillable price before sizing into any "
    "Polymarket position.",
    module="polymarket",
    schema={
        "type": "object",
        "properties": {
            "slug_or_id": {"type": "string", "description": "Market slug (e.g. 'will-fed-cut-rates-by-q3-2026') or numeric id"},
        },
        "required": ["slug_or_id"],
    },
)
async def _t_pm_market(args: dict):
    from tckr import polymarket as pm
    return await pm.market(args["slug_or_id"])


@register_tool(
    "pm_book",
    "Live CLOB orderbook touch for ONE outcome of a Polymarket market, keyed "
    "by slug + outcome ('yes' or 'no'). Returns best_bid, best_ask, midpoint, "
    "spread, last_trade_price, tick_size, min_order_size, and the raw top-of-"
    "book bid/ask levels. This is what your order will ACTUALLY fill against — "
    "use it before sizing any position. YES and NO each have independent "
    "orderbooks; a tight YES book doesn't imply a tight NO book. If best_bid "
    "and best_ask are far apart (>5% spread is normal-ish on prediction "
    "markets; >20% means the venue is essentially dead), don't size up.",
    module="polymarket",
    schema={
        "type": "object",
        "properties": {
            "slug_or_id": {"type": "string", "description": "Market slug or numeric id"},
            "outcome":    {"type": "string", "enum": ["yes", "no"], "default": "yes",
                           "description": "Which outcome's book — 'yes' or 'no'"},
        },
        "required": ["slug_or_id"],
    },
)
async def _t_pm_book(args: dict):
    from tckr import polymarket as pm
    return await pm.outcome_book(args["slug_or_id"], outcome=args.get("outcome", "yes"))


@register_tool(
    "pm_touch",
    "Compact YES + NO touch summary for one Polymarket market — both outcome "
    "books in a single call. Returns yes_bid/yes_ask/yes_mid/yes_spread, "
    "no_bid/no_ask/no_mid/no_spread, yes_last_trade/no_last_trade, tick_size, "
    "min_order_size, plus market-level liquidity / volume_24h. Use this as a "
    "single-glance 'is this market fillable on either side?' check before "
    "deciding whether to dig deeper. Cheaper than two pm_book calls when you "
    "want a fast read.",
    module="polymarket",
    schema={
        "type": "object",
        "properties": {
            "slug_or_id": {"type": "string", "description": "Market slug or numeric id"},
        },
        "required": ["slug_or_id"],
    },
)
async def _t_pm_touch(args: dict):
    from tckr import polymarket as pm
    return await pm.outcome_touches(args["slug_or_id"])


@register_tool(
    "pm_size_to_fill",
    "Walk the CLOB book to estimate what `qty` shares would ACTUALLY fill at — "
    "the touch price only quotes the first level; once you size up you walk "
    "deeper. Returns effective_price (volume-weighted), touch_price, "
    "slippage_from_touch_bps (signed: positive = adverse), qty_filled "
    "(may be < requested if the book exhausts), qty_unfilled, fully_filled, "
    "levels_consumed, min_order_size, below_min_order_size, and total_notional. "
    "Why this matters on Polymarket: books are routinely thin. A market showing "
    "midpoint 0.52 might fill the first 50 shares at 0.55 then jump to 0.75 — "
    "your effective cost on a 1000-share position can be 20+ cents above the "
    "touch. Call this BEFORE any non-trivial position to know what you're "
    "actually paying. If `fully_filled` is false, the venue cannot absorb your "
    "size — split the order, size down, or look elsewhere.",
    module="polymarket",
    schema={
        "type": "object",
        "properties": {
            "slug_or_id": {"type": "string", "description": "Market slug or numeric id"},
            "outcome":    {"type": "string", "enum": ["yes", "no"], "default": "yes",
                           "description": "Which outcome — 'yes' or 'no'"},
            "side":       {"type": "string", "enum": ["buy", "sell"], "default": "buy",
                           "description": "'buy' walks asks; 'sell' walks bids (for closing a long)"},
            "qty":        {"type": "number", "description": "Shares to fill (must be > 0)"},
        },
        "required": ["slug_or_id", "qty"],
    },
)
async def _t_pm_size_to_fill(args: dict):
    from tckr import polymarket as pm
    return await pm.effective_fill(
        args["slug_or_id"],
        outcome=args.get("outcome", "yes"),
        side=args.get("side", "buy"),
        qty=float(args["qty"]),
    )


@register_tool(
    "pm_markets",
    "List Polymarket markets with filters. Use `tag` to narrow to a topic "
    "(e.g. 'politics', 'crypto', 'sports', 'macro').",
    module="polymarket",
    schema={
        "type": "object",
        "properties": {
            "limit":  {"type": "integer", "default": 25},
            "active": {"type": "boolean", "default": True},
            "closed": {"type": "boolean", "default": False},
            "tag":    {"type": "string",  "description": "Optional tag filter"},
            "order":  {"type": "string",  "default": "volume", "description": "volume | liquidity | endDate | startDate"},
        },
    },
)
async def _t_pm_markets(args: dict):
    from tckr import polymarket as pm
    rows = await pm.markets(
        limit=int(args.get("limit", 25)),
        active=bool(args.get("active", True)),
        closed=bool(args.get("closed", False)),
        tag=args.get("tag"),
        order=args.get("order", "volume"),
    )
    return _cap(rows, args.get("limit", 25))


# ============================================================================
# Pyth: on-chain oracle prices (Phase 5b)
# ============================================================================

@register_tool(
    "py_latest_price",
    "Pyth Network on-chain oracle prices for one or more symbols (e.g. "
    "'BTC/USD', 'ETH/USD', 'SOL/USD', 'NVDA/USD'). Returns parsed price + "
    "confidence interval + publish_time per symbol. Use as a tamper-resistant "
    "cross-check vs CEX prices, or for non-crypto assets (Pyth covers ~400 "
    "feeds incl. equities, FX, metals, rates).",
    module="pyth",
    schema={
        "type": "object",
        "properties": {
            "symbols": {
                "type": "string",
                "description": "Comma-separated symbols (e.g. 'BTC/USD,ETH/USD,SOL/USD')",
            },
        },
        "required": ["symbols"],
    },
)
async def _t_py_latest_price(args: dict):
    from tckr import pyth
    syms = [s.strip() for s in args["symbols"].split(",") if s.strip()]
    return await pyth.latest_price_for_symbols(syms)


@register_tool(
    "py_feeds",
    "List Pyth price feeds, optionally filtered by query (substring on base/symbol) "
    "and asset_type ∈ {crypto, equity, fx, metal, rates}. Use to discover feed "
    "ids for assets py_latest_price doesn't know how to resolve symbolically.",
    module="pyth",
    schema={
        "type": "object",
        "properties": {
            "query":      {"type": "string"},
            "asset_type": {"type": "string", "description": "crypto | equity | fx | metal | rates"},
            "limit":      {"type": "integer", "default": 15, "description": f"Max rows (max {MAX_ROWS})"},
        },
    },
)
async def _t_py_feeds(args: dict):
    from tckr import pyth
    rows = await pyth.feeds(query=args.get("query"), asset_type=args.get("asset_type"))
    return _cap(rows, args.get("limit", 15))


# ============================================================================
# Etherscan V2: EVM block explorer across ~70 chains (Phase 5b)
# ============================================================================

@register_tool(
    "es_gas_oracle",
    "Current EVM gas oracle: safe / propose / fast gas prices in gwei + suggested "
    "base fee. Use to time tx submissions or to assess whether a chain is congested. "
    "Default chain is ethereum; pass chain= for others (base, arbitrum, optimism, "
    "polygon, bnb, avalanche, zksync).",
    module="etherscan",
    schema={
        "type": "object",
        "properties": {
            "chain": {"type": "string", "default": "ethereum"},
        },
    },
)
async def _t_es_gas_oracle(args: dict):
    from tckr import etherscan as es
    return await es.gas_oracle(args.get("chain", "ethereum"))


@register_tool(
    "es_contract_source",
    "Verified-contract source code + ABI + compiler version + license + proxy "
    "implementation address (if applicable). Use to vet an unknown contract or "
    "decode an interaction. Heavy payload — prefer es_contract_abi when you only "
    "need the ABI.",
    module="etherscan",
    schema={
        "type": "object",
        "properties": {
            "address": {"type": "string"},
            "chain":   {"type": "string", "default": "ethereum"},
        },
        "required": ["address"],
    },
)
async def _t_es_contract_source(args: dict):
    from tckr import etherscan as es
    return await es.contract_source(args["address"], chain=args.get("chain", "ethereum"))


@register_tool(
    "es_token_transfers",
    "ERC20 token transfer history for an EVM address (both directions). Returns "
    "list of {ts, hash, from, to, value, token_symbol, contract, block}. Use to "
    "audit a wallet's recent activity or trace token flows.",
    module="etherscan",
    schema={
        "type": "object",
        "properties": {
            "address": {"type": "string"},
            "chain":   {"type": "string",  "default": "ethereum"},
            "offset":  {"type": "integer", "default": 25, "description": f"Max rows (max {MAX_ROWS})"},
        },
        "required": ["address"],
    },
)
async def _t_es_token_transfers(args: dict):
    from tckr import etherscan as es
    rows = await es.token_transfers(args["address"], chain=args.get("chain", "ethereum"),
                                     offset=int(args.get("offset", 25)))
    return _cap(rows, args.get("offset", 25))


# ============================================================================
# Solscan: Solana block explorer (Phase 5b)
# ============================================================================

@register_tool(
    "sol_token_meta",
    "Solscan token metadata by SPL mint address: symbol, name, decimals, holder "
    "count, market cap. Public endpoint (no key); set SOLSCAN_API_KEY and pass "
    "pro=true for the richer Pro response.",
    module="solscan",
    schema={
        "type": "object",
        "properties": {
            "mint": {"type": "string"},
            "pro":  {"type": "boolean", "default": False},
        },
        "required": ["mint"],
    },
)
async def _t_sol_token_meta(args: dict):
    from tckr import solscan as sc
    return await sc.token_meta(args["mint"], pro=bool(args.get("pro", False)))


@register_tool(
    "sol_token_holders",
    "Top holders of an SPL token by balance. Use to gauge concentration risk "
    "(top-10 holding > 50% of supply = high rug / dump risk).",
    module="solscan",
    schema={
        "type": "object",
        "properties": {
            "mint":  {"type": "string"},
            "limit": {"type": "integer", "default": 20, "description": f"Max rows (max {MAX_ROWS})"},
            "pro":   {"type": "boolean", "default": False},
        },
        "required": ["mint"],
    },
)
async def _t_sol_token_holders(args: dict):
    from tckr import solscan as sc
    rows = await sc.token_holders(args["mint"], limit=int(args.get("limit", 20)),
                                   pro=bool(args.get("pro", False)))
    return _cap(rows, args.get("limit", 20))


# ============================================================================
# LunarCrush: social sentiment (Phase 5b)
# ============================================================================

@register_tool(
    "lc_coins_list",
    "All LunarCrush-tracked coins with Galaxy Score, AltRank, social volume, "
    "sentiment score, social dominance. Use as a social-attention screen: high "
    "Galaxy Score + rising AltRank often precedes retail-driven moves.",
    module="lunarcrush",
    schema={
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "default": 25, "description": f"Max rows (max {MAX_ROWS})"},
        },
    },
)
async def _t_lc_coins_list(args: dict):
    from tckr import lunarcrush as lc
    rows = await lc.coins_list()
    return _cap(rows, args.get("limit", 25))


@register_tool(
    "lc_topic",
    "Topic-level social metrics on LunarCrush (e.g. 'ai', 'memecoins', 'rwa', "
    "'bitcoin', 'defi'). Returns aggregate sentiment + interaction velocity for "
    "the entire topic, not just one coin.",
    module="lunarcrush",
    schema={
        "type": "object",
        "properties": {
            "topic": {"type": "string"},
        },
        "required": ["topic"],
    },
)
async def _t_lc_topic(args: dict):
    from tckr import lunarcrush as lc
    return await lc.topic(args["topic"])


# ============================================================================
# Messari: research-grade profiles + news (Phase 5b)
# ============================================================================

@register_tool(
    "ms_news_feed",
    "Messari research/news feed (curated). Returns {title, url, content, "
    "published_at, source, tags}. Use as a higher-signal news source than "
    "general crypto-news firehoses.",
    module="messari",
    schema={
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "default": 15, "description": f"Max rows (max {MAX_ROWS})"},
        },
    },
)
async def _t_ms_news_feed(args: dict):
    from tckr import messari
    rows = await messari.news_feed(limit=int(args.get("limit", 15)))
    return _cap(rows, args.get("limit", 15))


@register_tool(
    "ms_asset_metrics",
    "Full metrics for one Messari asset by slug (e.g. 'bitcoin', 'ethereum', "
    "'solana'): price, mcap, supply, ROI breakdown, mining stats, ATH/ATL, "
    "marketcap dominance. NOTE: most fields require a paid Messari plan; returns "
    "limited data on the free 'Hobbyist' tier.",
    module="messari",
    schema={
        "type": "object",
        "properties": {
            "slug": {"type": "string", "description": "Messari asset slug (lowercase, e.g. 'bitcoin')"},
        },
        "required": ["slug"],
    },
)
async def _t_ms_asset_metrics(args: dict):
    from tckr import messari
    return await messari.asset_metrics(args["slug"])


# ============================================================================
# Token Terminal: protocol fundamentals (Phase 5b)
# ============================================================================

@register_tool(
    "tt_projects",
    "Token Terminal project catalog with current-snapshot metrics (revenue_24h, "
    "fees_24h, market_cap, treasury, etc.). Optionally filter by market_sector "
    "('Blockchains (L1)', 'DeFi', 'NFT marketplaces', ...).",
    module="tokenterminal",
    schema={
        "type": "object",
        "properties": {
            "market_sector": {"type": "string"},
            "limit":         {"type": "integer", "default": 25, "description": f"Max rows (max {MAX_ROWS})"},
        },
    },
)
async def _t_tt_projects(args: dict):
    from tckr import tokenterminal as tt
    rows = await tt.projects(market_sector=args.get("market_sector"))
    return _cap(rows, args.get("limit", 25))


# ============================================================================
# The Graph: GraphQL subgraph queries (Phase 5b)
# ============================================================================

@register_tool(
    "tg_query_subgraph",
    "Run a GraphQL query against any subgraph by id. Most flexible tool for "
    "EVM on-chain analytics — Uniswap V3, Aave V3, Compound, Lido, etc. all "
    "publish subgraphs. Pass the subgraph id and a GraphQL query string with "
    "optional variables. Without THEGRAPH_API_KEY uses the public gateway "
    "(heavily throttled).",
    module="thegraph",
    schema={
        "type": "object",
        "properties": {
            "subgraph_id": {"type": "string"},
            "query":       {"type": "string", "description": "GraphQL query body"},
            "variables":   {"type": "string", "description": "JSON-encoded variables (optional)"},
        },
        "required": ["subgraph_id", "query"],
    },
)
async def _t_tg_query_subgraph(args: dict):
    import json as _json

    from tckr import thegraph as tg
    variables = None
    raw = args.get("variables")
    if raw:
        try:
            variables = _json.loads(raw) if isinstance(raw, str) else raw
        except _json.JSONDecodeError as e:
            raise ValueError(f"invalid variables JSON: {e}") from None
    return await tg.query_subgraph(args["subgraph_id"], args["query"], variables=variables)


@register_tool(
    "tg_uniswap_v3_top_pools",
    "Convenience: top Uniswap V3 pools on Ethereum by TVL. Each row has token0, "
    "token1, feeTier, liquidity, totalValueLockedUSD, volumeUSD. Use as a "
    "depth check when planning to trade an ETH-side token via Uniswap.",
    module="thegraph",
    schema={
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "default": 10, "description": f"Max rows (max {MAX_ROWS})"},
        },
    },
)
async def _t_tg_uniswap_v3_top_pools(args: dict):
    from tckr import thegraph as tg
    rows = await tg.uniswap_v3_top_pools(first=int(args.get("limit", 10)))
    return _cap(rows, args.get("limit", 10))


# ============================================================================
# Options (Alpaca) — US equity/ETF option chains, quotes, greeks
# ============================================================================

@register_tool(
    "opt_chain",
    "Option chain for a US stock/ETF/index (e.g. AAPL, SPY, SPX) with quotes "
    "AND model greeks (delta/gamma/theta/vega/rho) + implied volatility per "
    "contract — the supported replacement for yfinance options (which has no "
    "greeks). Cascades Alpaca (if ALPACA_API_KEY+SECRET are set; opra-capable) "
    "→ keyless CBOE delayed feed, so it works with NO key out of the box. "
    "Returns {underlying, feed, source, count, contracts:[{symbol, expiration, "
    "dte, type, strike, bid, ask, mid, last, iv, delta, gamma, theta, vega, "
    "rho, open_interest?, volume?}, ...]}; `source` is 'alpaca' or 'cboe'. Both "
    "feeds are delayed ~15m on the free tier. ALWAYS pass `expiration` (or "
    "exp_gte/exp_lte) for a liquid name — the all-expiry chain is thousands of "
    "contracts. Use opt_expirations first if you don't know valid expiry dates.",
    module="",  # cascade — Alpaca (keyed) → CBOE (keyless), not one registry module
    schema={
        "type": "object",
        "properties": {
            "underlying":  {"type": "string", "description": "Stock/ETF ticker, e.g. AAPL, SPY, NVDA"},
            "expiration":  {"type": "string", "description": "Exact expiry YYYY-MM-DD (strongly recommended)"},
            "exp_gte":     {"type": "string", "description": "Min expiry YYYY-MM-DD (range alternative)"},
            "exp_lte":     {"type": "string", "description": "Max expiry YYYY-MM-DD (range alternative)"},
            "type":        {"type": "string", "description": "Filter to 'call' or 'put' (default: both)"},
            "strike_gte":  {"type": "number", "description": "Min strike price"},
            "strike_lte":  {"type": "number", "description": "Max strike price"},
            "limit":       {"type": "integer", "description": "Contracts per page (default 100, max 1000)", "default": 100},
        },
        "required": ["underlying"],
    },
)
async def _t_opt_chain(args: dict):
    from tckr import options as opt
    return await opt.chain_cascade(
        args["underlying"],
        expiration=args.get("expiration"),
        exp_gte=args.get("exp_gte"),
        exp_lte=args.get("exp_lte"),
        type=args.get("type"),
        strike_gte=args.get("strike_gte"),
        strike_lte=args.get("strike_lte"),
        limit=max(1, min(int(args.get("limit", 100)), 1000)),
    )


@register_tool(
    "opt_snapshot",
    "Snapshots for one or more explicit OCC option contract symbols (e.g. "
    "'AAPL260619C00150000'). Returns the same per-contract rows as opt_chain "
    "(quote, last, IV, greeks). Cascades Alpaca (if keyed) → keyless CBOE. Use "
    "when you already have specific contract symbols and want just those, "
    "instead of pulling a whole chain.",
    module="",  # cascade — Alpaca (keyed) → CBOE (keyless)
    schema={
        "type": "object",
        "properties": {
            "symbols": {
                "type": "array",
                "items": {"type": "string"},
                "description": "OCC contract symbols, e.g. ['AAPL260619C00150000']",
            },
        },
        "required": ["symbols"],
    },
)
async def _t_opt_snapshot(args: dict):
    from tckr import options as opt
    return await opt.snapshot_cascade(args.get("symbols") or [])


@register_tool(
    "opt_expirations",
    "Available option expiration dates and strike range for a US stock/ETF/"
    "index. Returns {underlying, expirations:[YYYY-MM-DD,...], strikes:{min,max}, "
    "source}. Cascades Alpaca (if keyed) → keyless CBOE, so it works with no "
    "key. Call this before opt_chain to pick a valid expiry rather than guessing.",
    module="",  # cascade — Alpaca (keyed) → CBOE (keyless)
    schema={
        "type": "object",
        "properties": {
            "underlying": {"type": "string", "description": "Stock/ETF/index ticker, e.g. AAPL, SPY, SPX"},
        },
        "required": ["underlying"],
    },
)
async def _t_opt_expirations(args: dict):
    from tckr import options as opt
    return await opt.expirations_cascade(args["underlying"])


# ============================================================================
# Prompt-injection helpers
# ============================================================================

def render_tools_doc() -> str:
    """Compact tool reference for a system prompt, grouped by tier.

    Each tool gets one indented line: `  - <name>: <description>`. Sections are
    separated by a one-line header per tier so the agent can see at a glance
    which subset is available without keys.
    """
    from tckr.registry import REGISTRY

    # Bucket tools by their module's tier; tools without a module (e.g.
    # `capabilities`) get their own "Meta" section.
    buckets: dict[str, list[ToolSpec]] = {
        "keyless-free": [],
        "keyed-free":   [],
        "keyed-paid":   [],
        "meta":         [],
    }
    for spec in TOOLS:
        if not spec.module:
            buckets["meta"].append(spec)
            continue
        mod_spec = REGISTRY.get(spec.module)
        if mod_spec is None:
            buckets["meta"].append(spec)
            continue
        buckets[mod_spec.tier.value].append(spec)

    headers = {
        "meta":         "Meta / introspection",
        "keyless-free": "Keyless (always available)",
        "keyed-free":   "Keyed-free tier (require a free API key)",
        "keyed-paid":   "Paid tier (require a paid plan to be useful)",
    }
    lines: list[str] = []
    for tier_key in ("meta", "keyless-free", "keyed-free", "keyed-paid"):
        bucket = buckets[tier_key]
        if not bucket:
            continue
        lines.append(f"  [{headers[tier_key]}]")
        for spec in bucket:
            lines.append(f"    - {spec.name}: {augment_description(spec)}")
        lines.append("")
    return "\n".join(lines).rstrip()
