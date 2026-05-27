"""Polymarket Gamma API — prediction-market odds (binary YES/NO markets).

Public, keyless. Composes with macro context: a Fed-rates Polymarket and DXY
trend together is a richer signal than either alone.

Endpoints:
- `markets(...)` — list markets, filter by tag / closed / volume
- `events(...)` — events grouping related markets
- `market(slug_or_id)` — full single-market data; cascades through three
  resolution paths so resolved markets and slug renames are still findable
- `market_status(slug)` — primitive for settlement loops: returns one of
  {"alive", "resolved_yes", "resolved_no", "ambiguous", "ghost"}
- `top_volume(limit)` — convenience filter on `markets` sorted by 24h volume

The Gamma API returns prices in [0, 1] for YES tokens; NO = 1 - YES. Volume
fields are in USDC.

Gotchas worth knowing (learned the hard way):

  1. The default `/markets?slug=X` query *filters out resolved markets*. A
     market that has paid out (outcomePrices collapsed to [0,1] or [1,0])
     returns zero rows on the default query — you must pass `closed=true`
     to see it. tckr does this automatically inside `market()`.

  2. Polymarket sometimes renames a market's slug, appending a numeric
     disambiguator (e.g. `...-537` → `...-537-597`). The on-chain
     `conditionId` is stable across renames. tckr persists a slug ->
     conditionId alias on every successful fetch so the next lookup against
     the old slug can recover via `?condition_ids=<id>`. Set
     `TCKR_POLYMARKET_ALIASES_PATH` to a writable JSON path to persist
     this map across processes; otherwise it lives in-process only.

  3. The query param is `condition_ids` (plural, underscore). `conditionId`
     and `condition_id` are silently ignored by the API.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from tckr import _http, settings
from tckr.cache import TTLCache

log = logging.getLogger("tckr.polymarket")

_BASE = "https://gamma-api.polymarket.com"

_cache = TTLCache()

# slug -> {"condition_id": "0x...", "canonical_slug": "...", "updated_at": iso}
# Populated by every successful `market()` fetch; consulted when a slug query
# returns nothing so we can fall back to conditionId-based lookup.
_aliases: dict[str, dict] = {}
_aliases_loaded = False


def _aliases_path() -> Path | None:
    """Resolve the configured aliases file path, or None for in-memory only."""
    raw = settings.POLYMARKET_ALIASES_PATH
    return Path(raw) if raw else None


def _load_aliases() -> None:
    """Load the alias map from disk (if configured). Idempotent."""
    global _aliases, _aliases_loaded
    if _aliases_loaded:
        return
    _aliases_loaded = True
    p = _aliases_path()
    if p is None or not p.exists():
        return
    try:
        data = json.loads(p.read_text())
        if isinstance(data, dict):
            _aliases = data
    except (OSError, json.JSONDecodeError) as e:
        log.warning("polymarket aliases load failed (%s) — starting empty", e)


def _remember_alias(slug: str, condition_id: str | None,
                    canonical_slug: str | None) -> None:
    """Persist (slug -> conditionId, canonical_slug). Best-effort disk write."""
    if not slug or not condition_id:
        return
    cur = _aliases.get(slug)
    if (cur and cur.get("condition_id") == condition_id
            and cur.get("canonical_slug") == canonical_slug):
        return
    _aliases[slug] = {
        "condition_id": condition_id,
        "canonical_slug": canonical_slug or slug,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    p = _aliases_path()
    if p is None:
        return
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(_aliases, indent=2))
    except OSError as e:  # pragma: no cover — never block a fetch on disk
        log.warning("polymarket alias persist failed: %s", e)


async def _get(path: str, params: dict | None = None, label: str | None = None):
    ttl = settings.POLYMARKET_TTL_S
    key = (path, tuple(sorted((params or {}).items())))
    cached = _cache.get(key, ttl)
    if cached is not None:
        return cached
    async with _cache.lock(key):
        cached = _cache.get(key, ttl)
        if cached is not None:
            return cached
        data = await _http.get_json(f"{_BASE}{path}", params=params,
                                    label=label or f"polymarket {path}")
        if data is not None:
            _cache.put(key, data)
        return data


def _shape_market(m: dict) -> dict:
    """Pick the fields actually useful for a trading agent; drop the long tail."""
    outcomes = m.get("outcomes")
    outcome_prices = m.get("outcomePrices")
    # The API serializes these as JSON strings — parse if so.
    if isinstance(outcomes, str):
        try:
            import json
            outcomes = json.loads(outcomes)
        except Exception:
            outcomes = None
    if isinstance(outcome_prices, str):
        try:
            import json
            outcome_prices = json.loads(outcome_prices)
        except Exception:
            outcome_prices = None
    # YES price = the first outcome's price by convention.
    yes_price = None
    if isinstance(outcome_prices, list) and outcome_prices:
        try:
            yes_price = float(outcome_prices[0])
        except (TypeError, ValueError):
            yes_price = None
    return {
        "id":            m.get("id"),
        "condition_id":  m.get("conditionId"),
        "slug":          m.get("slug"),
        "question":      m.get("question"),
        "description":   (m.get("description") or "")[:400],
        "yes_price":     yes_price,
        "outcomes":      outcomes,
        "outcome_prices": outcome_prices,
        "volume":        _to_float(m.get("volume")),
        "volume_24h":    _to_float(m.get("volumeNum") or m.get("volume24hr")),
        "liquidity":     _to_float(m.get("liquidity")),
        "end_date":      m.get("endDate"),
        "closed":        bool(m.get("closed")),
        "active":        bool(m.get("active")),
        "category":      m.get("category"),
        "tags":          m.get("tags") or [],
    }


def _to_float(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


# ============================================================================
# Public API
# ============================================================================

async def markets(*, limit: int = 50, offset: int = 0,
                  active: bool | None = True, closed: bool | None = False,
                  tag: str | None = None,
                  order: str = "volume", ascending: bool = False) -> list[dict] | None:
    """List markets. Filter on active/closed; sort by `order` (volume, liquidity,
    endDate, startDate). Returns shaped market dicts."""
    params: dict = {
        "limit": min(max(int(limit), 1), 500),
        "offset": max(int(offset), 0),
        "order": order,
        "ascending": "true" if ascending else "false",
    }
    if active is not None:
        params["active"] = "true" if active else "false"
    if closed is not None:
        params["closed"] = "true" if closed else "false"
    if tag:
        params["tag"] = tag
    data = await _get("/markets", params=params, label="polymarket markets")
    if not isinstance(data, list):
        return None
    return [_shape_market(m) for m in data]


async def market(slug_or_id: str) -> dict | None:
    """Single market by slug or numeric id, with rename/resolution resilience.

    Resolution cascade (per slug):

      1. `/markets?slug=X` — the default, fast path. Only returns *active*
         markets (the gamma-api filters out closed ones here).
      2. `/markets?slug=X&closed=true` — resolved markets reappear. Without
         this step, any market that has paid out is invisible.
      3. `/markets?condition_ids=<id>` — uses the slug -> conditionId alias
         we persisted on an earlier successful fetch. Recovers from
         polymarket renaming a slug (appending a numeric disambiguator while
         keeping the same on-chain conditionId).

    Step 3 only fires if we have a previously-cached alias for the slug. If
    you've never fetched the market before and polymarket has since renamed
    it, you need to seed `TCKR_POLYMARKET_ALIASES_PATH` manually (one entry
    per stranded slug). Every successful fetch — including via alias —
    auto-updates the map so future calls go straight through.

    The returned dict's `slug` is relabeled to match the requested slug, so
    callers keying off the original slug (positions dict, quote cache) keep
    working transparently. Inspect `condition_id` if you need the canonical
    identifier.

    Numeric id input falls through to the `/markets/{id}` path as before.
    """
    _load_aliases()

    # 1) Default: active markets only.
    rows = await _get("/markets", params={"slug": slug_or_id},
                      label=f"polymarket market {slug_or_id}")
    if isinstance(rows, list) and rows:
        m = _shape_market(rows[0])
        _remember_alias(slug_or_id, m.get("condition_id"), m.get("slug"))
        return m

    # 2) closed=true catches resolved markets that the default filter hides.
    rows = await _get("/markets", params={"slug": slug_or_id, "closed": "true"},
                      label=f"polymarket market {slug_or_id} closed")
    if isinstance(rows, list) and rows:
        m = _shape_market(rows[0])
        _remember_alias(slug_or_id, m.get("condition_id"), m.get("slug"))
        return m

    # 3) conditionId alias — recovers when polymarket renamed the slug.
    alias = _aliases.get(slug_or_id)
    cid = alias.get("condition_id") if alias else None
    if cid:
        rows = await _get("/markets", params={"condition_ids": cid},
                          label=f"polymarket market by cid {cid[:10]}")
        if isinstance(rows, list) and rows:
            raw = rows[0]
            canonical = raw.get("slug")
            m = _shape_market(raw)
            # Relabel so callers keyed on the original slug still resolve.
            # The conditionId in the returned dict still identifies the market
            # unambiguously for anything that needs canonical state.
            m["slug"] = slug_or_id
            _remember_alias(slug_or_id, cid, canonical)
            return m

    # 4) Numeric-id fallback path — only meaningful when slug_or_id is a
    # numeric market id. Skip for slug-shaped input (otherwise the gamma-api
    # 422s and spams the log on every ghost lookup).
    if slug_or_id.isdigit():
        data = await _get(f"/markets/{slug_or_id}",
                          label=f"polymarket market id {slug_or_id}")
        if isinstance(data, dict):
            return _shape_market(data)
    return None


async def market_status(slug: str) -> str:
    """Classify a market's state for settlement / monitoring loops.

    Returns one of:

      - "alive"          — market is open and trading
      - "resolved_yes"   — closed, YES paid $1
      - "resolved_no"    — closed, NO paid $1 (i.e., YES paid $0)
      - "ambiguous"      — closed but outcomePrices not at an extreme (rare;
                            e.g., disputed-but-frozen)
      - "ghost"          — slug returns nothing on any cascade step; usually
                            a polymarket rename we don't have an alias for
                            yet. Operators should investigate.

    This is the primitive a settlement loop should branch on: settle on
    resolved_*, log+alert on ghost, no-op on alive/ambiguous.
    """
    m = await market(slug)
    if m is None:
        return "ghost"
    if not m.get("closed"):
        return "alive"
    yp = m.get("yes_price")
    if yp is None:
        return "ambiguous"
    if yp >= 0.99:
        return "resolved_yes"
    if yp <= 0.01:
        return "resolved_no"
    return "ambiguous"


async def top_volume(limit: int = 20) -> list[dict] | None:
    """Active markets sorted by 24h volume — a discovery pass for what's hot."""
    return await markets(limit=limit, active=True, closed=False, order="volume")


async def events(*, limit: int = 25, active: bool | None = True,
                 closed: bool | None = False, tag: str | None = None) -> list[dict] | None:
    """Events grouping related markets. Less useful for trading than `markets`
    but exposed for completeness."""
    params: dict = {
        "limit": min(max(int(limit), 1), 500),
    }
    if active is not None:
        params["active"] = "true" if active else "false"
    if closed is not None:
        params["closed"] = "true" if closed else "false"
    if tag:
        params["tag"] = tag
    data = await _get("/events", params=params, label="polymarket events")
    if not isinstance(data, list):
        return None
    return data
