"""DefiLlama API — chain & protocol TVL, DEX volumes, stablecoins, yield pools.

Free, no API key. DefiLlama splits its API across three subdomains:
api.llama.fi (TVL/protocols/DEX), stablecoins.llama.fi, yields.llama.fi.
This module hits all three behind a uniform interface.

Network ids match tckr's canonical form (`base`, `solana`, `eth`);
DefiLlama's chain *names* (capitalized) and path *slugs* (lowercase) are
resolved internally so callers never have to know.

Docs: https://defillama.com/docs/api
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from tckr import _http, settings
from tckr.cache import TTLCache

log = logging.getLogger("tckr.defillama")

_BASE_API     = "https://api.llama.fi"
_BASE_STABLES = "https://stablecoins.llama.fi"
_BASE_YIELDS  = "https://yields.llama.fi"

_cache = TTLCache()

# canonical id -> (path slug, display name)
_DL_CHAIN_INFO: dict[str, tuple[str, str]] = {
    "base":    ("base",     "Base"),
    "solana":  ("solana",   "Solana"),
    "eth":     ("ethereum", "Ethereum"),
}


def _dl_info(network: str | None) -> tuple[str, str] | None:
    if not network:
        return None
    canon = settings.normalize_network(network)
    if not canon:
        return None
    return _DL_CHAIN_INFO.get(canon, (canon, canon.title()))


def _dl_slug(network: str | None) -> str | None:
    info = _dl_info(network)
    return info[0] if info else None


def _dl_name(network: str | None) -> str | None:
    info = _dl_info(network)
    return info[1] if info else None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _f(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _ts_to_iso(ts) -> str | None:
    """DefiLlama uses unix seconds for `date`."""
    try:
        return datetime.fromtimestamp(int(ts), tz=UTC).isoformat()
    except (TypeError, ValueError, OSError):
        return None


# --------------------------- chains ---------------------------

def _parse_chain(raw: dict) -> dict:
    return {
        "name": raw.get("name"),
        "tvl_usd": _f(raw.get("tvl")),
        "token_symbol": raw.get("tokenSymbol"),
        "gecko_id": raw.get("gecko_id"),
        "chain_id": raw.get("chainId"),
        "cmc_id": raw.get("cmcId"),
    }


async def chains() -> list[dict]:
    """TVL snapshot for every chain DefiLlama tracks (sorted by TVL desc)."""
    ck = ("chains",)
    cached = _cache.get(ck, settings.TVL_TTL_S)
    if cached is not None:
        return cached
    body = await _http.get_json(f"{_BASE_API}/v2/chains", label="defillama chains")
    rows = [_parse_chain(r) for r in body or [] if isinstance(r, dict)]
    rows.sort(key=lambda r: r.get("tvl_usd") or 0, reverse=True)
    _cache.put(ck, rows)
    return rows


async def chain(name: str) -> dict | None:
    """TVL snapshot for one chain by name or canonical network id."""
    target = (_dl_name(name) or (name or "").title()).lower()
    for c in await chains():
        if (c.get("name") or "").lower() == target:
            return c
    return None


async def chain_tvl_history(name: str) -> list[dict]:
    """Historical TVL series for a chain: [{t, tvl_usd}, ...] chronological."""
    target = _dl_name(name) or (name or "").title()
    if not target:
        return []
    ck = ("chain_tvl_history", target)
    cached = _cache.get(ck, settings.TVL_TTL_S)
    if cached is not None:
        return cached
    body = await _http.get_json(
        f"{_BASE_API}/v2/historicalChainTvl/{target}",
        label=f"defillama historicalChainTvl {target}",
    )
    out = []
    for r in body or []:
        if isinstance(r, dict):
            out.append({"t": _ts_to_iso(r.get("date")), "tvl_usd": _f(r.get("tvl"))})
    out.sort(key=lambda x: x["t"] or "")
    _cache.put(ck, out)
    return out


# --------------------------- protocols ---------------------------

def _parse_protocol(raw: dict) -> dict:
    return {
        "id": raw.get("id"),
        "slug": raw.get("slug") or raw.get("name"),
        "name": raw.get("name"),
        "category": raw.get("category"),
        "chain": raw.get("chain"),
        "chains": raw.get("chains") or [],
        "tvl_usd": _f(raw.get("tvl")),
        "change_1h": _f(raw.get("change_1h")),
        "change_1d": _f(raw.get("change_1d")),
        "change_7d": _f(raw.get("change_7d")),
        "mcap_usd": _f(raw.get("mcap")),
        "url": raw.get("url"),
        "twitter": raw.get("twitter"),
        "logo": raw.get("logo"),
        "symbol": raw.get("symbol"),
    }


async def protocols(chain: str | None = None, *, min_tvl_usd: float = 0,
                    limit: int | None = None) -> list[dict]:
    """All protocols, optionally narrowed to one chain and/or a TVL floor.

    Sorted by TVL descending. The full list is ~3000 protocols, fetched once
    per TVL_TTL_S and filtered client-side on every call.
    """
    ck = ("protocols",)
    rows = _cache.get(ck, settings.TVL_TTL_S)
    if rows is None:
        body = await _http.get_json(f"{_BASE_API}/protocols", label="defillama protocols")
        rows = [_parse_protocol(r) for r in body or [] if isinstance(r, dict)]
        _cache.put(ck, rows)
    dl_name = _dl_name(chain)
    out = []
    for r in rows:
        if dl_name and dl_name not in (r.get("chains") or []) and r.get("chain") != dl_name:
            continue
        if min_tvl_usd and (r.get("tvl_usd") or 0) < min_tvl_usd:
            continue
        out.append(r)
    out.sort(key=lambda r: r.get("tvl_usd") or 0, reverse=True)
    return out[:limit] if limit else out


async def protocol(slug: str) -> dict | None:
    """Detailed protocol snapshot by slug (e.g. 'aerodrome-v1')."""
    slug = (slug or "").strip().lower()
    if not slug:
        return None
    ck = ("protocol", slug)
    cached = _cache.get(ck, settings.TVL_TTL_S)
    if cached is not None:
        return cached
    body = await _http.get_json(f"{_BASE_API}/protocol/{slug}", label=f"defillama protocol {slug}")
    if not isinstance(body, dict) or not body:
        return None
    # `tvl` on /protocol/{slug} is a historical timeseries; pull the latest point.
    tvl_field = body.get("tvl")
    if isinstance(tvl_field, list) and tvl_field:
        last = tvl_field[-1]
        tvl_usd = _f(last.get("totalLiquidityUSD") if isinstance(last, dict) else None)
    elif isinstance(tvl_field, (int, float)):
        tvl_usd = _f(tvl_field)
    else:
        tvl_usd = None
    out = {
        "slug": slug,
        "name": body.get("name"),
        "symbol": body.get("symbol"),
        "category": body.get("category"),
        "chains": body.get("chains") or [],
        "tvl_usd": tvl_usd,
        "current_chain_tvls": {k: _f(v) for k, v in (body.get("currentChainTvls") or {}).items()},
        "url": body.get("url"),
        "twitter": body.get("twitter"),
        "description": body.get("description"),
        "logo": body.get("logo"),
    }
    _cache.put(ck, out)
    return out


# --------------------------- DEX overview ---------------------------

async def dex_overview(chain: str) -> dict | None:
    """DEX volume overview for a chain.

    Returns {chain, total_24h, total_7d, total_30d, total_all_time,
             change_1d, change_7d, change_1m, protocols: [...]}, with
    protocols sorted by 24h volume desc.
    """
    slug = _dl_slug(chain) or (chain or "").lower()
    if not slug:
        return None
    ck = ("dex_overview", slug)
    cached = _cache.get(ck, settings.TVL_TTL_S)
    if cached is not None:
        return cached
    body = await _http.get_json(
        f"{_BASE_API}/overview/dexs/{slug}",
        params={"excludeTotalDataChart": "true",
                "excludeTotalDataChartBreakdown": "true"},
        label=f"defillama dex_overview {slug}",
    )
    if not isinstance(body, dict):
        return None
    parsed_prots = []
    for p in body.get("protocols") or []:
        if not isinstance(p, dict):
            continue
        parsed_prots.append({
            "name": p.get("name"),
            "logo": p.get("logo"),
            "total_24h": _f(p.get("total24h")),
            "total_7d": _f(p.get("total7d")),
            "change_1d": _f(p.get("change_1d")),
            "change_7d": _f(p.get("change_7d")),
            "chains": p.get("chains") or [],
        })
    parsed_prots.sort(key=lambda r: r.get("total_24h") or 0, reverse=True)
    out = {
        "chain": body.get("chain"),
        "total_24h": _f(body.get("total24h")),
        "total_7d": _f(body.get("total7d")),
        "total_30d": _f(body.get("total30d")),
        "total_all_time": _f(body.get("totalAllTime")),
        "change_1d": _f(body.get("change_1d")),
        "change_7d": _f(body.get("change_7d")),
        "change_1m": _f(body.get("change_1m")),
        "protocols": parsed_prots,
        "ts": _now_iso(),
    }
    _cache.put(ck, out)
    return out


# --------------------------- stablecoins ---------------------------

def _parse_stablecoin(raw: dict, chain_name: str | None) -> dict:
    circ = (raw.get("circulating") or {}).get("peggedUSD")
    out = {
        "id": raw.get("id"),
        "name": raw.get("name"),
        "symbol": raw.get("symbol"),
        "peg_type": raw.get("pegType"),
        "peg_mechanism": raw.get("pegMechanism"),
        "circulating_usd": _f(circ),
        "price": _f(raw.get("price")),
    }
    if chain_name:
        cc = ((raw.get("chainCirculating") or {}).get(chain_name) or {})
        cur = cc.get("current") or {}
        out["chain_circulating_usd"] = _f(cur.get("peggedUSD"))
    return out


async def stablecoins(chain: str | None = None) -> list[dict]:
    """Stablecoin circulating supply.

    With `chain`: filtered to assets present on that chain (with
    `chain_circulating_usd` populated), sorted by chain circulating desc.
    Without: every stablecoin sorted by global circulating desc.
    """
    ck = ("stablecoins",)
    raw_rows = _cache.get(ck, settings.TVL_TTL_S)
    if raw_rows is None:
        body = await _http.get_json(
            f"{_BASE_STABLES}/stablecoins",
            params={"includePrices": "true"},
            label="defillama stablecoins",
        )
        raw_rows = (body or {}).get("peggedAssets") or []
        _cache.put(ck, raw_rows)
    dl_name = _dl_name(chain)
    parsed = [_parse_stablecoin(r, dl_name) for r in raw_rows if isinstance(r, dict)]
    if dl_name:
        parsed = [p for p in parsed if p.get("chain_circulating_usd")]
        parsed.sort(key=lambda r: r.get("chain_circulating_usd") or 0, reverse=True)
    else:
        parsed.sort(key=lambda r: r.get("circulating_usd") or 0, reverse=True)
    return parsed


# --------------------------- yields ---------------------------

def _parse_yield(raw: dict) -> dict:
    return {
        "pool": raw.get("pool"),
        "chain": raw.get("chain"),
        "project": raw.get("project"),
        "symbol": raw.get("symbol"),
        "tvl_usd": _f(raw.get("tvlUsd")),
        "apy": _f(raw.get("apy")),
        "apy_base": _f(raw.get("apyBase")),
        "apy_reward": _f(raw.get("apyReward")),
        "apy_pct_1d": _f(raw.get("apyPct1D")),
        "apy_pct_7d": _f(raw.get("apyPct7D")),
        "apy_pct_30d": _f(raw.get("apyPct30D")),
        "stablecoin": raw.get("stablecoin"),
        "il_risk": raw.get("ilRisk"),
        "exposure": raw.get("exposure"),
    }


async def yields(chain: str | None = None, *, project: str | None = None,
                 min_tvl_usd: float = 0, limit: int | None = None) -> list[dict]:
    """Yield pools, optionally narrowed by chain / project / TVL floor.

    Sorted by APY descending.
    """
    ck = ("yields",)
    rows = _cache.get(ck, settings.TVL_TTL_S)
    if rows is None:
        body = await _http.get_json(f"{_BASE_YIELDS}/pools", label="defillama yields")
        raw_rows = (body or {}).get("data") or []
        rows = [_parse_yield(r) for r in raw_rows if isinstance(r, dict)]
        _cache.put(ck, rows)
    dl_name = _dl_name(chain)
    out = []
    for r in rows:
        if dl_name and r.get("chain") != dl_name:
            continue
        if project and (r.get("project") or "").lower() != project.lower():
            continue
        if min_tvl_usd and (r.get("tvl_usd") or 0) < min_tvl_usd:
            continue
        out.append(r)
    out.sort(key=lambda r: r.get("apy") or 0, reverse=True)
    return out[:limit] if limit else out
