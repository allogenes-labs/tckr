"""Bankr — natural-language on-chain agent + token launchpad.

Bankr is the AI-agent platform behind @bankrbot on X / Farcaster; users tag
the bot to swap, transfer, or deploy tokens. The launchpad side of that
flow surfaces here: every token deployed *through* Bankr (Doppler on Base,
Raydium on Solana) lands in a public feed with rich social attribution —
the deployer's X handle and profile image, the tweet URL, IPFS metadata.

Pre-DEX-indexer value: Bankr is the X analogue of [[clanker]]. Where
Clanker carries `requestor_fid` for Farcaster cross-link, Bankr carries
`x_username` / `x_profile_image_url` for the X side of the same KOL
deployment thesis. Catching a launch here means catching it before any
aggregator picks up the pool.

Public surface (keyless — no API key required):

    new_launches(limit=25)                  recently deployed Bankr tokens
    launch(token_address)                   one launch's details by token contract
    launches_by_deployer(addr, limit=25)    launches by a deployer wallet
    launches_by_x_user(handle, limit=25)    launches by an X username

Speculative (require BANKR_API_KEY — wired but unverified against a live key
as of 0.2.3; see docs.bankr.bot for sign-up):

    resolve_address(handle)                 ENS / social handle -> EVM address
    search_users(query)                     search Bankr users by social handle

Endpoint quirks worth knowing:

- The `/token-launches` list endpoint **takes no query params** — it always
  returns the latest 50 deployed rows. `chain`, `limit`, `before`, etc. are
  all no-ops server-side. Filtering by chain / deployer / handle happens
  client-side here.
- The X handle on a launch can live on EITHER `deployer.xUsername` OR
  `feeRecipient.xUsername`. `launches_by_x_user` checks both fields.
- All 50 most-recent rows in the public feed at the time of writing are
  Base/Doppler; Solana support is newer and may surface here as Bankr
  expands the listed launches.

Rate limit: published as `RateLimit-Policy: 120;w=60` (120 req/min).
Tested at 15 back-to-back requests with no throttling.

API base: `https://api.bankr.bot`. Docs: `https://docs.bankr.bot`.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from tckr import _http, settings
from tckr.cache import TTLCache

log = logging.getLogger("tckr.bankr")

_BASE = "https://api.bankr.bot"
_cache = TTLCache()


# ---------- small parse helpers ----------

def _i(v) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _ts_to_iso(ms: object) -> str | None:
    """Bankr timestamps are JS-style ms-since-epoch."""
    n = _i(ms)
    if n is None:
        return None
    try:
        return datetime.fromtimestamp(n / 1000.0, tz=UTC).isoformat()
    except (OSError, ValueError):
        return None


def _x_username_of(row: dict) -> str | None:
    """Bankr puts the X handle on EITHER side — prefer feeRecipient since
    that's the creator's payout identity, fall back to deployer."""
    fr = row.get("feeRecipient") or {}
    if isinstance(fr, dict) and fr.get("xUsername"):
        return fr.get("xUsername")
    dep = row.get("deployer") or {}
    if isinstance(dep, dict) and dep.get("xUsername"):
        return dep.get("xUsername")
    return None


def _x_profile_image_of(row: dict) -> str | None:
    fr = row.get("feeRecipient") or {}
    if isinstance(fr, dict) and fr.get("xProfileImageUrl"):
        return fr.get("xProfileImageUrl")
    dep = row.get("deployer") or {}
    if isinstance(dep, dict) and dep.get("xProfileImageUrl"):
        return dep.get("xProfileImageUrl")
    return None


def _parse_row(r: dict) -> dict:
    """Flatten one Bankr launch row to a unified, tckr-style schema.

    Preserves the deployer + feeRecipient wallet addresses separately —
    they're the same in most cases but legitimately diverge when a creator
    routes fees to a different wallet than the one that signed the deploy
    tx. Callers chasing KOL identity should use `x_username`; callers
    auditing on-chain provenance should use `deployer_address`.
    """
    deployer = r.get("deployer") or {}
    fee_recipient = r.get("feeRecipient") or {}
    return {
        "activity_id": r.get("activityId"),
        "status": r.get("status"),                  # 'deployed', 'pending', etc.
        "launch_type": r.get("launchType"),         # 'doppler' (Base), 'raydium' (Solana), ...
        "name": r.get("tokenName"),
        "symbol": r.get("tokenSymbol"),
        "chain": r.get("chain"),                    # 'base' or 'solana'
        "token_address": r.get("tokenAddress"),
        "pool_id": r.get("poolId"),
        "tx_hash": r.get("txHash"),
        "image_uri": r.get("imageUri"),             # ipfs://...
        "metadata_uri": r.get("metadataUri"),       # ipfs://...
        "tweet_url": r.get("tweetUrl"),
        "website_url": r.get("websiteUrl"),
        "timestamp_ms": _i(r.get("timestamp")),
        "deployed_at_iso": _ts_to_iso(r.get("timestamp")),
        # Provenance — these may diverge.
        "deployer_address":      (deployer or {}).get("walletAddress"),
        "fee_recipient_address": (fee_recipient or {}).get("walletAddress"),
        # Top-level X identity — checks both fields, prefers feeRecipient.
        # Pipe into agents that look up X / Twitter followers, or compose
        # with Farcaster identity tools if the user crosslinks the handles.
        "x_username":          _x_username_of(r),
        "x_profile_image_url": _x_profile_image_of(r),
        "_raw": r,
    }


# ---------- shared HTTP ----------

async def _get(path: str, *, params: dict | None = None,
               headers: dict | None = None,
               label: str = "") -> object | None:
    return await _http.get_json(f"{_BASE}{path}", params=params, headers=headers,
                                label=label or f"bankr {path}")


def _extract_rows(body) -> list[dict]:
    if isinstance(body, dict):
        launches = body.get("launches")
        if isinstance(launches, list):
            return [_parse_row(r) for r in launches if isinstance(r, dict)]
    elif isinstance(body, list):
        return [_parse_row(r) for r in body if isinstance(r, dict)]
    return []


# ---------- public functions (keyless) ----------

async def new_launches(limit: int = 25, *,
                        status: str | None = None) -> list[dict]:
    """Recently deployed Bankr launches, newest first.

    The upstream endpoint always returns the latest 50 rows regardless of
    query params; `limit` here just caps client-side. To page deeper you'd
    need a key + a different endpoint (Bankr doesn't expose pagination on
    the public feed).

    The feed mixes lifecycle states — pass `status="deployed"` to drop
    pending / failed rows, or `status="pending"` to only see launches whose
    deploy tx has been submitted but not confirmed yet. Default returns
    everything as-is.
    """
    capped = max(1, min(int(limit), 50))
    want_status = (status or "").strip().lower() or None
    ck = ("new", capped, want_status)
    cached = _cache.get(ck, settings.LAUNCHPAD_DISCOVERY_TTL_S)
    if cached is not None:
        return cached
    body = await _get("/token-launches", label="bankr new")
    rows = _extract_rows(body)
    if want_status:
        rows = [r for r in rows if (r.get("status") or "").lower() == want_status]
    rows = rows[:capped]
    _cache.put(ck, rows)
    return rows


async def launch(token_address: str) -> dict | None:
    """Full launch record by token contract address.

    The path is `/token-launches/{tokenAddress}`. Pass an EVM 0x-address for
    Base launches; the Solana mint format for Solana launches.
    """
    addr = (token_address or "").strip()
    if not addr:
        return None
    ck = ("launch", addr.lower())
    cached = _cache.get(ck, settings.LAUNCHPAD_TOKEN_TTL_S)
    if cached is not None:
        return cached
    body = await _get(f"/token-launches/{addr}",
                       label=f"bankr launch {addr[:10]}")
    if isinstance(body, dict):
        # Endpoint may return either the bare row or a {launch: {...}} envelope.
        row = body.get("launch") if "launch" in body else body
        out = _parse_row(row) if isinstance(row, dict) else None
    else:
        out = None
    _cache.put(ck, out)
    return out


async def launches_by_deployer(deployer_address: str, *,
                                limit: int = 25) -> list[dict]:
    """Launches by a specific deployer wallet (client-side filter).

    No server-side endpoint for this — we pull `new_launches()` and filter.
    Means we can only see launches that fall within the public 50-row
    window. For deeper history you'd need an API key.
    """
    addr = (deployer_address or "").strip().lower()
    if not addr:
        return []
    capped = max(1, min(int(limit), 50))
    rows = await new_launches(limit=50)
    out = [r for r in rows if (r.get("deployer_address") or "").lower() == addr]
    return out[:capped]


async def launches_by_x_user(x_username: str, *,
                              limit: int = 25) -> list[dict]:
    """Launches associated with an X (Twitter) username (client-side filter).

    Checks both `deployer.xUsername` and `feeRecipient.xUsername` — Bankr
    populates either field depending on the deploy path, and we don't know
    a priori which the caller is asking about. Matching is case-insensitive.

    Note: limited to the latest 50 launches in the public feed (see
    `launches_by_deployer` for why).
    """
    handle = (x_username or "").strip().lstrip("@").lower()
    if not handle:
        return []
    capped = max(1, min(int(limit), 50))
    rows = await new_launches(limit=50)
    out = [r for r in rows if (r.get("x_username") or "").lower() == handle]
    return out[:capped]


# ---------- public functions (keyed-free, speculative) ----------
#
# These endpoints are described in the Bankr Skills SKILL.md but have not
# been validated against a live `bk_...` API key as of 0.2.3. The shapes
# below are best-guesses from the docs; if the live response differs, the
# `_raw` passthrough in `_parse_resolved` / `_parse_user` lets callers
# inspect the raw envelope while we adjust.

def _parse_resolved(body) -> dict | None:
    if not isinstance(body, dict):
        return None
    # Docs imply a single resolved-address envelope; cover the common shapes.
    addr = body.get("walletAddress") or body.get("address") or body.get("resolved")
    return {
        "input": body.get("input") or body.get("handle"),
        "wallet_address": addr,
        "chain": body.get("chain"),
        "_raw": body,
    } if addr else None


def _parse_user(u: dict) -> dict:
    return {
        "wallet_address": u.get("walletAddress"),
        "x_username":     u.get("xUsername"),
        "x_profile_image_url": u.get("xProfileImageUrl"),
        "_raw": u,
    }


async def resolve_address(handle: str) -> dict | None:
    """Resolve an ENS / social handle to a wallet address via Bankr's
    identity index.

    Requires `BANKR_API_KEY`. Returns None when the key is missing
    (logged at WARNING) or the handle does not resolve.

    **Unverified against a live key as of 0.2.3** — the path
    `/addresses/resolve` and the response shape are documented but
    haven't been exercised here. If you hit this with a real key and
    the shape differs, `_raw` will carry the upstream envelope.
    """
    if not settings.BANKR_API_KEY:
        log.warning("BANKR_API_KEY not set — bankr.resolve_address skipped")
        return None
    h = (handle or "").strip()
    if not h:
        return None
    ck = ("resolve", h.lower())
    cached = _cache.get(ck, settings.LAUNCHPAD_TOKEN_TTL_S)
    if cached is not None:
        return cached
    body = await _get(
        "/addresses/resolve",
        params={"handle": h},
        headers={"X-API-Key": settings.BANKR_API_KEY},
        label=f"bankr resolve {h[:20]}",
    )
    out = _parse_resolved(body)
    _cache.put(ck, out)
    return out


async def search_users(query: str, *, limit: int = 25) -> list[dict]:
    """Search Bankr users by social username (X handle, etc.).

    Requires `BANKR_API_KEY`. Returns [] when the key is missing.

    **Unverified against a live key as of 0.2.3** — same caveat as
    `resolve_address`.
    """
    if not settings.BANKR_API_KEY:
        log.warning("BANKR_API_KEY not set — bankr.search_users skipped")
        return []
    q = (query or "").strip()
    if not q:
        return []
    capped = max(1, min(int(limit), 50))
    ck = ("search_users", q.lower(), capped)
    cached = _cache.get(ck, settings.LAUNCHPAD_DISCOVERY_TTL_S)
    if cached is not None:
        return cached
    body = await _get(
        "/users/search",
        params={"q": q},
        headers={"X-API-Key": settings.BANKR_API_KEY},
        label=f"bankr search_users {q[:20]}",
    )
    users: list[dict] = []
    if isinstance(body, dict):
        items = body.get("users") or body.get("results") or body.get("data")
        if isinstance(items, list):
            users = [_parse_user(u) for u in items if isinstance(u, dict)]
    elif isinstance(body, list):
        users = [_parse_user(u) for u in body if isinstance(u, dict)]
    users = users[:capped]
    _cache.put(ck, users)
    return users
