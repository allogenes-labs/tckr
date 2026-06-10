"""Polymarket Gamma API — prediction-market odds (binary YES/NO markets).

Public, keyless. Composes with macro context: a Fed-rates Polymarket and DXY
trend together is a richer signal than either alone.

Gamma (discovery / metadata):
- `markets(...)` — list markets, filter by tag / closed / volume
- `events(...)` — events grouping related markets
- `market(slug_or_id)` — full single-market data; cascades through three
  resolution paths so resolved markets and slug renames are still findable
- `market_status(slug)` — primitive for settlement loops: returns one of
  {"alive", "resolved_yes", "resolved_no", "ambiguous", "ghost"}
- `top_volume(limit)` — convenience filter on `markets` sorted by 24h volume

CLOB (live fillable prices — NOT the same as gamma's AMM midpoints):
- `book(token_id)` — raw CLOB orderbook for one outcome token
- `outcome_book(slug, outcome)` — book() keyed by slug + "yes"/"no"
- `outcome_touches(slug)` — compact YES + NO touch summary in one call
- `effective_fill(slug, outcome, side, qty)` — volume-weighted fill walk

The Gamma midpoint can diverge wildly from the live CLOB on thin markets
(we've seen gamma 0.52 against a CLOB best_ask of 0.96). Sizing into a
position without checking the CLOB book is how retail eats 300+ bps of
hidden slippage. Use `outcome_touches` for a single-glance fillability
check; use `effective_fill` before placing any non-trivial order.

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

  4. Tag filtering on `/markets` and `/events` is by numeric `tag_id`, NOT the
     free-text `tag` slug the docs imply. Passing `tag=crypto` is silently
     ignored and you get the *unfiltered* top-volume list. tckr resolves the
     label/slug to an id via `/tags/slug/{slug}` (see `_resolve_tag_id`).
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from tckr import _http, settings
from tckr.cache import TTLCache

log = logging.getLogger("tckr.polymarket")

_BASE = "https://gamma-api.polymarket.com"
_CLOB_BASE = "https://clob.polymarket.com"

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
        "updated_at": datetime.now(UTC).isoformat(),
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


# label/slug -> numeric tag id (or None when the tag doesn't exist). The gamma
# `/markets` and `/events` endpoints filter by numeric `tag_id`; the free-text
# `tag` param they advertise is silently ignored, so an unresolved tag would
# otherwise return the *unfiltered* top-volume list (mostly sports/politics).
_tag_id_cache: dict[str, int | None] = {}


async def _resolve_tag_id(tag: str) -> int | None:
    """Resolve a tag label/slug (e.g. 'crypto', 'Politics') to its numeric id.

    Numeric input passes straight through. Otherwise the tag is normalized to a
    gamma slug (lowercased, spaces -> hyphens) and looked up via
    `/tags/slug/{slug}`. Results — including misses — are cached for the process.
    Returns None if the tag can't be resolved.
    """
    t = (tag or "").strip()
    if not t:
        return None
    if t.isdigit():
        return int(t)
    if t in _tag_id_cache:
        return _tag_id_cache[t]
    slug = t.lower().replace(" ", "-")
    row = await _get(f"/tags/slug/{slug}", label=f"polymarket tag {slug}")
    tid: int | None = None
    if isinstance(row, dict) and row.get("id") is not None:
        try:
            tid = int(row["id"])
        except (TypeError, ValueError):
            tid = None
    _tag_id_cache[t] = tid
    return tid


def _shape_market(m: dict) -> dict:
    """Pick the fields actually useful for a trading agent; drop the long tail."""
    outcomes = m.get("outcomes")
    outcome_prices = m.get("outcomePrices")
    clob_token_ids = m.get("clobTokenIds")
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
    if isinstance(clob_token_ids, str):
        try:
            import json
            clob_token_ids = json.loads(clob_token_ids)
        except Exception:
            clob_token_ids = None
    # YES price = the first outcome's price by convention; NO is the second.
    yes_price = None
    no_price = None
    if isinstance(outcome_prices, list) and outcome_prices:
        try:
            yes_price = float(outcome_prices[0])
        except (TypeError, ValueError):
            yes_price = None
        if len(outcome_prices) > 1:
            try:
                no_price = float(outcome_prices[1])
            except (TypeError, ValueError):
                no_price = None
    yes_token_id = None
    no_token_id = None
    if isinstance(clob_token_ids, list) and clob_token_ids:
        yes_token_id = str(clob_token_ids[0]) if clob_token_ids[0] else None
        if len(clob_token_ids) > 1:
            no_token_id = str(clob_token_ids[1]) if clob_token_ids[1] else None
    return {
        "id":            m.get("id"),
        "condition_id":  m.get("conditionId"),
        "slug":          m.get("slug"),
        "question":      m.get("question"),
        "description":   (m.get("description") or "")[:400],
        "yes_price":     yes_price,
        "no_price":      no_price,
        "outcomes":      outcomes,
        "outcome_prices": outcome_prices,
        "yes_token_id":  yes_token_id,
        "no_token_id":   no_token_id,
        "clob_token_ids": clob_token_ids,
        "volume":        _to_float(m.get("volume")),
        "volume_24h":    _to_float(m.get("volume24hr") or m.get("volume24hrClob")),
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
        tid = await _resolve_tag_id(tag)
        if tid is not None:
            params["tag_id"] = tid
        else:
            log.warning("polymarket markets: unknown tag %r — ignoring filter", tag)
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


async def book(token_id: str) -> dict | None:
    """Fetch the CLOB orderbook for a single outcome token.

    Each Polymarket market has TWO outcome tokens (YES and NO), each with its
    own CLOB orderbook keyed by `token_id` (the ERC1155 token id from
    `clob_token_ids[0]` / `[1]`). Returns:

        {
          "best_bid":  float | None,   # highest buy price (None if no bids)
          "best_ask":  float | None,   # lowest  sell price (None if no asks)
          "midpoint":  float | None,   # (best_bid + best_ask) / 2 when both
          "spread":    float | None,   # best_ask - best_bid when both
          "last_trade_price": float | None,
          "tick_size": float | None,
          "min_order_size": float | None,
          "bids": list[{"price","size"}],   # raw book (small, capped server-side)
          "asks": list[{"price","size"}],
        }

    Returns None if the upstream call fails. The CLOB endpoint is public/keyless
    but a missing User-Agent (e.g. urllib default) gets 403 — httpx's default
    `python-httpx/<v>` is accepted, so no header tweaks needed here.

    Note: gamma-api already exposes `bestBid`/`bestAsk` for the YES side via
    AMM but those are stale relative to CLOB and only cover YES. The CLOB
    `book` is per-token-id and reflects the live limit-order book, which is
    the right thing to fill against.
    """
    if not token_id:
        return None
    ttl = settings.POLYMARKET_TTL_S
    key = (f"/book/{token_id}", ())
    cached = _cache.get(key, ttl)
    if cached is not None:
        return cached
    async with _cache.lock(key):
        cached = _cache.get(key, ttl)
        if cached is not None:
            return cached
        data = await _http.get_json(
            f"{_CLOB_BASE}/book",
            params={"token_id": token_id},
            label=f"polymarket clob_book {token_id[:12]}",
        )
        if not isinstance(data, dict):
            return None
        bids_raw = data.get("bids") or []
        asks_raw = data.get("asks") or []
        # Normalize: book entries are {"price": "0.07", "size": "120"}; convert
        # to floats and sort defensively (the API doesn't guarantee an order
        # consumers can depend on).
        bids: list[dict] = []
        asks: list[dict] = []
        for b in bids_raw:
            try:
                bids.append({"price": float(b["price"]), "size": float(b["size"])})
            except (KeyError, TypeError, ValueError):
                continue
        for a in asks_raw:
            try:
                asks.append({"price": float(a["price"]), "size": float(a["size"])})
            except (KeyError, TypeError, ValueError):
                continue
        # Best bid = highest price among bids; best ask = lowest among asks.
        best_bid = max((b["price"] for b in bids), default=None)
        best_ask = min((a["price"] for a in asks), default=None)
        midpoint = None
        spread = None
        if best_bid is not None and best_ask is not None:
            midpoint = (best_bid + best_ask) / 2.0
            spread = best_ask - best_bid
        try:
            ltp = float(data["last_trade_price"]) if data.get("last_trade_price") else None
        except (TypeError, ValueError):
            ltp = None
        try:
            tick = float(data["tick_size"]) if data.get("tick_size") else None
        except (TypeError, ValueError):
            tick = None
        try:
            mos = float(data["min_order_size"]) if data.get("min_order_size") else None
        except (TypeError, ValueError):
            mos = None
        shaped = {
            "token_id": token_id,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "midpoint": midpoint,
            "spread": spread,
            "last_trade_price": ltp,
            "tick_size": tick,
            "min_order_size": mos,
            "bids": bids,
            "asks": asks,
        }
        # Don't cache a fully empty book: thin markets clear and refill
        # between fills, and a cached empty book makes effective_fill report
        # total illiquidity for the rest of the TTL.
        if bids or asks:
            _cache.put(key, shaped)
        return shaped


async def outcome_book(slug_or_id: str, outcome: str = "yes") -> dict | None:
    """CLOB book for one outcome of a market, keyed by slug (not raw token_id).

    Composes `market()` (to resolve slug → token_id) with `book()`. Returns
    the same fields as `book()` plus `slug` / `outcome` / `question` /
    `end_date` for context. Returns None if the market or its book is
    unfindable.

    Why this exists separately from `book()`: agents and most callers think
    in slugs and "yes/no", not 75-digit ERC1155 token ids. This wrapper hides
    the token-id lookup.
    """
    outcome = (outcome or "yes").strip().lower()
    if outcome not in ("yes", "no"):
        return None
    m = await market(slug_or_id)
    if not m:
        return None
    token_id = m.get("yes_token_id") if outcome == "yes" else m.get("no_token_id")
    if not token_id:
        return None
    b = await book(token_id)
    if not b:
        return None
    return {
        "slug":      m.get("slug"),
        "question":  m.get("question"),
        "outcome":   outcome,
        "end_date":  m.get("end_date"),
        **b,
    }


async def _none() -> None:
    """Awaitable that returns None — used to keep asyncio.gather symmetric
    when one outcome has no token_id."""
    return None


async def outcome_touches(slug_or_id: str) -> dict | None:
    """Compact YES + NO touch summary for one market. One gamma + two CLOB calls.

    Returns:
        {
          "slug":          str,
          "question":      str,
          "end_date":      str | None,
          "yes_bid":       float | None,
          "yes_ask":       float | None,
          "yes_mid":       float | None,
          "yes_spread":    float | None,
          "no_bid":        float | None,
          "no_ask":        float | None,
          "no_mid":        float | None,
          "no_spread":     float | None,
          "yes_last_trade": float | None,
          "no_last_trade":  float | None,
          "tick_size":     float | None,    # both outcomes share tick size
          "min_order_size": float | None,
          "liquidity":     float | None,    # market-level USD orderbook depth
          "volume_24h":    float | None,    # market-level
        }

    Lets a caller ask "is this market fillable?" in a single line of output
    without parsing two separate book responses. Returns None if the market
    or both outcome books are unreachable.
    """
    m = await market(slug_or_id)
    if not m:
        return None
    yes_tid = m.get("yes_token_id")
    no_tid = m.get("no_token_id")
    yes_book_task = book(yes_tid) if yes_tid else _none()
    no_book_task = book(no_tid) if no_tid else _none()
    yes_b, no_b = await asyncio.gather(yes_book_task, no_book_task)
    if yes_b is None and no_b is None:
        return None
    yes_b = yes_b or {}
    no_b = no_b or {}
    # Tick size and min_order_size are market-level; prefer whichever side has it.
    tick = yes_b.get("tick_size") or no_b.get("tick_size")
    mos = yes_b.get("min_order_size") or no_b.get("min_order_size")
    return {
        "slug":            m.get("slug"),
        "question":        m.get("question"),
        "end_date":        m.get("end_date"),
        "yes_bid":         yes_b.get("best_bid"),
        "yes_ask":         yes_b.get("best_ask"),
        "yes_mid":         yes_b.get("midpoint"),
        "yes_spread":      yes_b.get("spread"),
        "no_bid":          no_b.get("best_bid"),
        "no_ask":          no_b.get("best_ask"),
        "no_mid":          no_b.get("midpoint"),
        "no_spread":       no_b.get("spread"),
        "yes_last_trade":  yes_b.get("last_trade_price"),
        "no_last_trade":   no_b.get("last_trade_price"),
        "tick_size":       tick,
        "min_order_size":  mos,
        "liquidity":       m.get("liquidity"),
        "volume_24h":      m.get("volume_24h"),
    }


async def effective_fill(
    slug_or_id: str,
    outcome: str = "yes",
    side: str = "buy",
    qty: float = 0.0,
) -> dict | None:
    """Volume-weighted fill price for `qty` shares walked through the CLOB book.

    Polymarket books are often thin — a 5000-share order on a market that
    shows midpoint 0.52 might actually fill at 0.71 because the touch is only
    deep for the first few hundred shares. This walks the appropriate side of
    the book to give a realistic effective price BEFORE the order is sent.

    Walks `asks` ascending for buys, `bids` descending for sells, consuming
    each level's size until `qty` is filled or the book is exhausted.

    Args:
        slug_or_id: market slug or numeric id
        outcome:    'yes' or 'no' — which outcome token to trade
        side:       'buy' or 'sell' — buy walks asks, sell walks bids
        qty:        shares requested

    Returns:
        {
          "slug":            str,
          "outcome":         "yes" | "no",
          "side":            "buy" | "sell",
          "qty_requested":   float,
          "qty_filled":      float,       # may be < requested if book exhausted
          "qty_unfilled":    float,
          "fully_filled":    bool,
          "effective_price": float | None, # weighted avg; None if 0 filled
          "touch_price":     float | None, # best_ask for buy, best_bid for sell
          "slippage_from_touch_bps": float | None,  # signed bps vs touch (positive = adverse)
          "levels_consumed": int,
          "tick_size":       float | None,
          "min_order_size":  float | None, # so caller can flag too-small orders
          "below_min_order_size": bool,    # qty < min_order_size
          "total_notional":  float | None, # qty_filled × effective_price
        }

    Returns None if the slug/outcome can't be resolved or the book fetch fails.
    """
    outcome = (outcome or "yes").strip().lower()
    side = (side or "buy").strip().lower()
    if outcome not in ("yes", "no"):
        return None
    if side not in ("buy", "sell"):
        return None
    try:
        qty = float(qty)
    except (TypeError, ValueError):
        return None
    if qty <= 0:
        return None

    m = await market(slug_or_id)
    if not m:
        return None
    token_id = m.get("yes_token_id") if outcome == "yes" else m.get("no_token_id")
    if not token_id:
        return None
    b = await book(token_id)
    if not b:
        return None

    # Buys lift offers (asks); sells hit bids. Walk best-price-first.
    if side == "buy":
        levels = sorted(b.get("asks") or [], key=lambda x: x["price"])
        touch = b.get("best_ask")
    else:
        levels = sorted(b.get("bids") or [], key=lambda x: -x["price"])
        touch = b.get("best_bid")

    remaining = qty
    filled = 0.0
    notional = 0.0
    levels_consumed = 0
    for lvl in levels:
        if remaining <= 0:
            break
        take = min(remaining, lvl["size"])
        if take <= 0:
            continue
        notional += take * lvl["price"]
        filled += take
        remaining -= take
        levels_consumed += 1

    effective = (notional / filled) if filled > 0 else None
    slippage_bps: float | None = None
    if effective is not None and touch and touch > 0:
        # Buys: effective ≥ touch (we lift higher offers as we walk).
        # Sells: effective ≤ touch. Sign the bps so positive = adverse.
        if side == "buy":
            slippage_bps = (effective / touch - 1.0) * 10000.0
        else:
            slippage_bps = (1.0 - effective / touch) * 10000.0

    mos = b.get("min_order_size")
    below_min = bool(mos is not None and qty < mos)

    return {
        "slug":             m.get("slug"),
        "outcome":          outcome,
        "side":             side,
        "qty_requested":    qty,
        "qty_filled":       filled,
        "qty_unfilled":     max(0.0, qty - filled),
        "fully_filled":     filled >= qty - 1e-9,
        "effective_price":  effective,
        "touch_price":      touch,
        "slippage_from_touch_bps": slippage_bps,
        "levels_consumed":  levels_consumed,
        "tick_size":        b.get("tick_size"),
        "min_order_size":   mos,
        "below_min_order_size": below_min,
        "total_notional":   notional if filled > 0 else None,
    }


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
        tid = await _resolve_tag_id(tag)
        if tid is not None:
            params["tag_id"] = tid
        else:
            log.warning("polymarket events: unknown tag %r — ignoring filter", tag)
    data = await _get("/events", params=params, label="polymarket events")
    if not isinstance(data, list):
        return None
    return data
