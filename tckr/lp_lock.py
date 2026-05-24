"""LP Lock — detect locked liquidity on Uniswap V2 pairs, V3 pools, V4 pools.

Closes the "is this rugproof for N days?" question that contract-security
tools like Goplus only partially answer. Auto-detects from the input shape:

    20-byte address (40 hex chars)  → V2 pair or V3 pool (probed for which)
    32-byte PoolId  (64 hex chars)  → V4 pool

Public surface:

    lp_lock(pool_or_id, network="base")
        -> {pool_type: "v2"|"v3"|"v4", is_locked: bool, ...}

Returns None when the input can't be classified or RPC fails. Response shape
varies by pool_type because each version answers a different question:

## V2 (fungible LP tokens)

For Uniswap V2-style pairs, LP is an ERC-20 token. "Locked" = held by a
known locker contract. Single `locked_pct` metric is meaningful.

    {pool_type: "v2", locked_pct, locked_lp_raw, total_supply_raw,
     lockers: [{protocol, version, address, balance_raw, pct_of_supply}],
     is_locked, ts}

`is_locked` = `locked_pct >= 50.0` (default — caller can override).

## V3 (concentrated liquidity NFTs)

For Uniswap V3 pools, liquidity is held as NFT positions (one per LP). The
locker holds NFTs that target this pool. There's no clean "locked_pct"
analog — positions have different liquidity at different price ranges; a
small in-range position can dominate trading while a giant out-of-range
position contributes nothing right now. Instead we report position counts +
raw liquidity per locker.

    {pool_type: "v3", token0, token1, fee, n_locked_positions,
     total_locked_liquidity_raw, lockers: [{protocol, version, address,
     n_positions, total_liquidity_raw, positions: [{token_id, liquidity,
     tick_lower, tick_upper}]}], is_locked, ts}

`is_locked` = `n_locked_positions >= 1` (any locked position is a positive
signal — V3 devs typically lock all positions or none).

## V4 (singleton PoolManager + PositionManager NFTs)

V4 ditches per-pool contracts: a singleton `PoolManager` holds all pools,
identified by `PoolId = keccak256(abi.encode(PoolKey))`. LP positions are
NFTs in a separate `PositionManager` contract. Same "is the NFT held by a
locker?" model as V3, but matching by computed PoolId instead of pool address.

    {pool_type: "v4", pool_id, currency0, currency1, fee, tick_spacing, hooks,
     n_locked_positions, lockers: [{..., n_positions,
     positions: [{token_id, tick_lower, tick_upper, has_subscriber}]}],
     is_locked, ts}

V4 supports **native ETH** (currency address `0x0000...`) and **dynamic-fee
hooks** (fee sentinel `0x800000 = 8388608`) — both appear in real positions.
No liquidity-amount field yet (would need an extra PoolManager call per
position).

## Known lockers (per chain)

| Chain | V2 | V3 | V4 |
|---|---|---|---|
| Base | UNCX V2 | UNCX V3 | UNCX V4 |
| ETH  | UNCX V2, Team Finance V1 | UNCX V3 | UNCX V4 |

Team Finance V3/V4 and Team Finance Base are TODOs.

## v1 limitations

- **No `unlock_at` field.** Per-lock detail (when does it unlock) requires
  ABI-encoded calls to each locker's per-lock view (`tokenLocks` in UNCX V2,
  `getLock` in UNCX V3, etc.) — deferred.
- **V3/V4 positions report raw liquidity / tick range only**, not USD value.
  Computing USD value requires reading the pool's current tick + price and
  applying tick math to derive token amounts. Deferred.

Reuses existing `tckr.alchemy` for RPC — no new keys required. Uses
`eth-abi` + `eth-hash[pycryptodome]` for V3/V4 struct decoding + PoolId hashing.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from eth_abi import decode as abi_decode, encode as abi_encode
from eth_utils import keccak

from tckr import _http, alchemy, settings
from tckr.cache import TTLCache

log = logging.getLogger("tckr.lp_lock")


# ---------- known contracts ----------

# V2 LP lockers — addresses lowercased (EVM addresses are case-insensitive).
_KNOWN_V2_LOCKERS: dict[str, list[dict]] = {
    "base": [
        {"protocol": "UNCX", "version": "V2",
         "name": "UNCX UniswapV2 Locker",
         "address": "0xc4e637d37113192f4f1f060daebd7758de7f4131"},
        # TODO: Team Finance Base address (not in their public docs).
    ],
    "eth": [
        {"protocol": "UNCX", "version": "V2",
         "name": "UNCX UniswapV2 Locker",
         "address": "0x663a5c229c09b049e36dcc11a9b0d4a8eb9db214"},
        {"protocol": "Team Finance", "version": "V1",
         "name": "TrustSwap Team Finance Lock",
         "address": "0xe2fe530c047f2d85298b07d9333c05737f1435fb"},
    ],
}

# V3 NFT lockers + the Uniswap V3 NonfungiblePositionManager (NFPM) per chain.
# The NFPM holds the position NFTs; the locker is an NFT holder. To check
# if a position is locked we ask "is this NFT owned by the locker?"
_KNOWN_V3_LOCKERS: dict[str, list[dict]] = {
    "base": [
        {"protocol": "UNCX", "version": "V3",
         "name": "UNCX UniswapV3 NFT Locker",
         "address": "0x231278edd38b00b07fbd52120cef685b9baebcc1"},
        # TODO: Team Finance V3 on Base.
    ],
    "eth": [
        {"protocol": "UNCX", "version": "V3",
         "name": "UNCX UniswapV3 NFT Locker",
         "address": "0xff4945c1d4cfa46d51ed5e6c50d34c4cb5d92c81"},
    ],
}

# Uniswap V3 NonfungiblePositionManager addresses per chain.
_UNISWAP_V3_NFPM: dict[str, str] = {
    "base": "0x03a520b32c04bf3beef7beb72e919cf822ed34f1",
    "eth":  "0xc36442b4a4522e871399cd717abdd847ab11fe88",
}

# V4 NFT lockers (Uniswap V4 launched 2025; UNCX added V4 locker support Feb 2025).
# Same NFT-locking model as V3 but against the V4 PositionManager.
_KNOWN_V4_LOCKERS: dict[str, list[dict]] = {
    "base": [
        {"protocol": "UNCX", "version": "V4",
         "name": "UNCX UniswapV4 NFT Locker",
         "address": "0xff908ded2a6c68226d3f834b25d803a815bdb28b"},
    ],
    "eth": [
        {"protocol": "UNCX", "version": "V4",
         "name": "UNCX UniswapV4 NFT Locker",
         "address": "0x147aeca171a79466fe9e2c03f21b45155ff403f8"},
    ],
}

# Uniswap V4 PositionManager (canonical ERC-721 holding V4 LP positions).
_UNISWAP_V4_POSITION_MANAGER: dict[str, str] = {
    "base": "0x7c5f5a4bbd8fd63184577525326123b519429bdc",
    "eth":  "0xbd216513d74c8cf14cf4747e6aaa6420ff64ee9e",
}


# ---------- function selectors (keccak256(sig)[:4]) ----------
# Computed once, hardcoded — see comment for the source signature.

_SEL_TOTAL_SUPPLY = "0x18160ddd"  # totalSupply()
_SEL_FEE          = "0xddca3f43"  # fee()              uint24
_SEL_TOKEN0       = "0x0dfe1681"  # token0()           address
_SEL_TOKEN1       = "0xd21220a7"  # token1()           address
_SEL_POSITIONS    = "0x99fbab88"  # positions(uint256) -> tuple (V3 NFPM)
_SEL_BALANCE_OF   = "0x70a08231"  # balanceOf(address) — used for ERC-721 balance
_SEL_GET_POOL_AND_POSITION_INFO = "0x7ba03aad"  # getPoolAndPositionInfo(uint256) — V4 PosM


# Lock-detection thresholds.
_DEFAULT_V2_LOCK_THRESHOLD_PCT = 50.0


_cache = TTLCache()
_meta_cache = TTLCache()  # for slow-changing things like (token0,token1,fee)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hex_to_int(h) -> int | None:
    if not isinstance(h, str):
        return None
    try:
        return int(h, 16) if h.startswith("0x") else int(h)
    except ValueError:
        return None


# ---------- low-level eth_call helpers ----------

async def _eth_call(to: str, data: str, *, network: str) -> str | None:
    """Raw eth_call returning hex string (or None on failure)."""
    return await alchemy._rpc(
        "eth_call",
        [{"to": to, "data": data}, "latest"],
        network=network,
        label=f"lp_lock eth_call {to[:8]} {data[:10]}",
    )


def _decode_address(hex_word: str | None) -> str | None:
    """A 32-byte word from eth_call where the last 20 bytes are an address."""
    if not hex_word or len(hex_word) < 42:
        return None
    return "0x" + hex_word[-40:].lower()


def _encode_address(addr: str) -> str:
    """Pack an address into a 32-byte zero-padded hex slot (no 0x prefix)."""
    return addr.lower().replace("0x", "").rjust(64, "0")


def _encode_uint256(n: int) -> str:
    return f"{n:064x}"


# ---------- pool kind detection ----------

def _input_kind(s: str) -> str:
    """Classify the raw input: 'address' (20 bytes), 'poolid' (32 bytes), or 'unknown'.

    V4 pools are identified by a bytes32 PoolId, not an EVM address. V2 / V3
    pools have regular 20-byte addresses.
    """
    norm = (s or "").strip().lower()
    if not norm.startswith("0x"):
        norm = "0x" + norm
    hex_len = len(norm) - 2
    if hex_len == 40:
        return "address"
    if hex_len == 64:
        return "poolid"
    return "unknown"


async def _detect_pool_type(pool_address: str, network: str) -> str | None:
    """For a 20-byte address, return 'v2', 'v3', or None.

    V2 pairs are themselves ERC-20s with `totalSupply()` returning > 0.
    V3 pools are not ERC-20s but have a `fee()` view returning a uint24.
    """
    ts_hex = await _eth_call(pool_address, _SEL_TOTAL_SUPPLY, network=network)
    ts = _hex_to_int(ts_hex)
    if ts and ts > 0:
        return "v2"
    fee_hex = await _eth_call(pool_address, _SEL_FEE, network=network)
    fee = _hex_to_int(fee_hex)
    if fee is not None and 0 < fee < 1_000_000:
        return "v3"
    return None


# ---------- V2 path ----------

async def _v2_lock_report(pool_address: str, *, network: str,
                           threshold: float) -> dict | None:
    lockers = _KNOWN_V2_LOCKERS.get(network) or []
    if not lockers:
        log.warning("lp_lock: no V2 lockers configured for %r", network)
        return None

    ts_hex = await _eth_call(pool_address, _SEL_TOTAL_SUPPLY, network=network)
    total_supply = _hex_to_int(ts_hex)
    if not total_supply:
        return None

    locker_rows: list[dict] = []
    locked_total = 0
    for L in lockers:
        bal_result = await alchemy._rpc(
            "alchemy_getTokenBalances",
            [L["address"], [pool_address]],
            network=network,
            label=f"lp_lock v2 bal {L['address'][:8]}/{pool_address[:8]}",
        )
        if not isinstance(bal_result, dict):
            continue
        balances = bal_result.get("tokenBalances") or []
        if not balances:
            continue
        bal = _hex_to_int(balances[0].get("tokenBalance")) or 0
        if bal <= 0:
            continue
        pct = bal / total_supply * 100.0
        locker_rows.append({
            "protocol": L["protocol"], "version": L["version"],
            "name": L["name"], "address": L["address"],
            "balance_raw": bal, "pct_of_supply": pct,
        })
        locked_total += bal

    locker_rows.sort(key=lambda r: r["pct_of_supply"], reverse=True)
    locked_pct = locked_total / total_supply * 100.0
    return {
        "pool_address": pool_address,
        "network": network,
        "pool_type": "v2",
        "total_supply_raw": total_supply,
        "locked_lp_raw": locked_total,
        "locked_pct": locked_pct,
        "is_locked": locked_pct >= threshold,
        "lockers": locker_rows,
        "ts": _now_iso(),
    }


# ---------- V3 path ----------

async def _v3_pool_params(pool_address: str, network: str) -> tuple[str, str, int] | None:
    """Read (token0, token1, fee) for a V3 pool. Cached — immutable per pool."""
    ck = ("v3_pool_params", network, pool_address.lower())
    cached = _meta_cache.get(ck, settings.TOKEN_METADATA_TTL_S)
    if cached is not None:
        return cached
    t0_hex = await _eth_call(pool_address, _SEL_TOKEN0, network=network)
    t1_hex = await _eth_call(pool_address, _SEL_TOKEN1, network=network)
    fee_hex = await _eth_call(pool_address, _SEL_FEE, network=network)
    t0 = _decode_address(t0_hex)
    t1 = _decode_address(t1_hex)
    fee = _hex_to_int(fee_hex)
    if not (t0 and t1 and fee is not None):
        return None
    out = (t0, t1, fee)
    _meta_cache.put(ck, out)
    return out


async def _get_locker_nft_ids(locker_address: str, nfpm_address: str,
                               network: str) -> list[int]:
    """List all NFT tokenIds (from the V3 NFPM contract) owned by `locker_address`.

    Uses Alchemy's NFT API (`getNFTsForOwner`) — one paginated call, much
    faster than ERC-721 Enumerable iteration. Caches the list aggressively
    (lockers acquire NFTs at the rate of new lock events — minutes timescale).
    """
    ck = ("locker_nfts", network, locker_address.lower(), nfpm_address.lower())
    cached = _cache.get(ck, settings.TOKEN_METADATA_TTL_S)
    if cached is not None:
        return cached

    canon = settings.normalize_network(network)
    slug = alchemy._ALCHEMY_NETWORKS.get(canon)
    if not slug or not settings.ALCHEMY_API_KEY:
        return []
    base_url = f"https://{slug}.g.alchemy.com/nft/v3/{settings.ALCHEMY_API_KEY}/getNFTsForOwner"

    out: list[int] = []
    page_key: str | None = None
    # Cap pages at 10 (each is up to 100 NFTs = 1000 total). Lockers may hold
    # more but this keeps the call bounded; the warning surfaces if hit.
    for _page in range(10):
        params: dict = {
            "owner": locker_address,
            "contractAddresses[]": nfpm_address,
            "withMetadata": "false",
            "pageSize": 100,
        }
        if page_key:
            params["pageKey"] = page_key
        body = await _http.get_json(base_url, params=params,
                                     label=f"alchemy NFTs {locker_address[:8]}")
        if not isinstance(body, dict):
            break
        for nft in body.get("ownedNfts") or []:
            tid_str = (nft.get("tokenId") or "")
            try:
                tid = int(tid_str, 16) if tid_str.startswith("0x") else int(tid_str)
                out.append(tid)
            except (TypeError, ValueError):
                continue
        page_key = body.get("pageKey")
        if not page_key:
            break
    else:
        log.warning("lp_lock: hit 10-page cap on %s; may have missed NFTs", locker_address[:8])

    _cache.put(ck, out)
    return out


async def _read_v3_position(nfpm_address: str, token_id: int,
                             network: str) -> dict | None:
    """Call NFPM.positions(tokenId) and decode the result struct.

    Solidity tuple returned:
        (uint96 nonce, address operator, address token0, address token1,
         uint24 fee, int24 tickLower, int24 tickUpper, uint128 liquidity,
         uint256 feeGrowthInside0LastX128, uint256 feeGrowthInside1LastX128,
         uint128 tokensOwed0, uint128 tokensOwed1)

    Cached per (chain, token_id) since (token0,token1,fee,ticks) are
    immutable; only liquidity changes (and we cache short for that reason).
    """
    ck = ("v3_position", network, nfpm_address.lower(), token_id)
    cached = _meta_cache.get(ck, settings.ONCHAIN_TTL_S)
    if cached is not None:
        return cached

    call_data = "0x" + _SEL_POSITIONS[2:] + _encode_uint256(token_id)
    raw_hex = await _eth_call(nfpm_address, call_data, network=network)
    if not isinstance(raw_hex, str) or len(raw_hex) < 4:
        return None
    try:
        raw_bytes = bytes.fromhex(raw_hex[2:])
        decoded = abi_decode(
            ["uint96", "address", "address", "address", "uint24",
             "int24", "int24", "uint128", "uint256", "uint256",
             "uint128", "uint128"],
            raw_bytes,
        )
    except Exception as e:  # noqa: BLE001 — never crash caller
        log.warning("lp_lock: position(%d) decode failed: %s", token_id, e)
        return None

    out = {
        "token_id": token_id,
        "token0": decoded[2].lower(),
        "token1": decoded[3].lower(),
        "fee": int(decoded[4]),
        "tick_lower": int(decoded[5]),
        "tick_upper": int(decoded[6]),
        "liquidity": int(decoded[7]),
    }
    _meta_cache.put(ck, out)
    return out


async def _v3_lock_report(pool_address: str, *, network: str) -> dict | None:
    params = await _v3_pool_params(pool_address, network)
    if not params:
        return None
    t0, t1, fee = params

    nfpm = _UNISWAP_V3_NFPM.get(network)
    if not nfpm:
        log.warning("lp_lock: no V3 NFPM configured for %r", network)
        return None

    lockers_meta = _KNOWN_V3_LOCKERS.get(network) or []
    if not lockers_meta:
        log.warning("lp_lock: no V3 lockers configured for %r", network)

    locker_rows: list[dict] = []
    total_locked_liq = 0
    total_locked_positions = 0
    for L in lockers_meta:
        nft_ids = await _get_locker_nft_ids(L["address"], nfpm, network)
        if not nft_ids:
            continue
        matching_positions: list[dict] = []
        for tid in nft_ids:
            pos = await _read_v3_position(nfpm, tid, network)
            if not pos:
                continue
            if pos["token0"] == t0 and pos["token1"] == t1 and pos["fee"] == fee:
                matching_positions.append({
                    "token_id": pos["token_id"],
                    "liquidity": pos["liquidity"],
                    "tick_lower": pos["tick_lower"],
                    "tick_upper": pos["tick_upper"],
                })
        if not matching_positions:
            continue
        # Sort by liquidity descending — caller usually wants the biggest first.
        matching_positions.sort(key=lambda p: p["liquidity"], reverse=True)
        loc_total = sum(p["liquidity"] for p in matching_positions)
        locker_rows.append({
            "protocol": L["protocol"], "version": L["version"],
            "name": L["name"], "address": L["address"],
            "n_positions": len(matching_positions),
            "total_liquidity_raw": loc_total,
            "positions": matching_positions,
        })
        total_locked_liq += loc_total
        total_locked_positions += len(matching_positions)

    locker_rows.sort(key=lambda r: r["total_liquidity_raw"], reverse=True)
    return {
        "pool_address": pool_address,
        "network": network,
        "pool_type": "v3",
        "token0": t0,
        "token1": t1,
        "fee": fee,
        "n_locked_positions": total_locked_positions,
        "total_locked_liquidity_raw": total_locked_liq,
        "is_locked": total_locked_positions > 0,
        "lockers": locker_rows,
        "ts": _now_iso(),
    }


# ---------- V4 path ----------

async def _read_v4_position(posm_address: str, token_id: int,
                             network: str) -> dict | None:
    """Read a V4 NFT's PoolKey + PositionInfo via getPoolAndPositionInfo.

    Returns:
        {token_id, currency0, currency1, fee, tick_spacing, hooks,
         pool_id (computed bytes32 hex), tick_lower, tick_upper, has_subscriber}
    """
    ck = ("v4_position", network, posm_address.lower(), token_id)
    cached = _meta_cache.get(ck, settings.ONCHAIN_TTL_S)
    if cached is not None:
        return cached

    call_data = _SEL_GET_POOL_AND_POSITION_INFO + _encode_uint256(token_id)
    raw_hex = await _eth_call(posm_address, call_data, network=network)
    if not isinstance(raw_hex, str) or len(raw_hex) < 4:
        return None
    try:
        raw_bytes = bytes.fromhex(raw_hex[2:])
        # Returns (PoolKey, PositionInfo) where:
        #   PoolKey      = tuple(address, address, uint24, int24, address)
        #   PositionInfo = uint256 (custom-wrapped, but decodes as uint256)
        decoded = abi_decode(
            ["(address,address,uint24,int24,address)", "uint256"],
            raw_bytes,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("v4 position(%d) decode failed: %s", token_id, e)
        return None

    (currency0, currency1, fee, tick_spacing, hooks) = decoded[0]
    position_info = int(decoded[1])

    # Compute the full bytes32 PoolId = keccak256(abi.encode(PoolKey)).
    encoded_key = abi_encode(
        ["address", "address", "uint24", "int24", "address"],
        [currency0, currency1, fee, tick_spacing, hooks],
    )
    pool_id_bytes = keccak(encoded_key)

    # PositionInfo bit layout (per v4-periphery PositionInfoLibrary):
    #   bits   0..7   : hasSubscriber (uint8)
    #   bits   8..31  : tickLower      (int24)
    #   bits  32..55  : tickUpper      (int24)
    #   bits  56..255 : poolId (truncated bytes25) — we already have the full
    #                   poolId from the PoolKey hash above, so ignore this.
    tick_lower_raw = (position_info >> 8) & 0xFFFFFF
    tick_upper_raw = (position_info >> 32) & 0xFFFFFF
    if tick_lower_raw & 0x800000:  # int24 sign extension
        tick_lower_raw -= 0x1000000
    if tick_upper_raw & 0x800000:
        tick_upper_raw -= 0x1000000

    out = {
        "token_id": token_id,
        "currency0": currency0.lower(),
        "currency1": currency1.lower(),
        "fee": int(fee),
        "tick_spacing": int(tick_spacing),
        "hooks": hooks.lower(),
        "pool_id": "0x" + pool_id_bytes.hex(),
        "tick_lower": tick_lower_raw,
        "tick_upper": tick_upper_raw,
        "has_subscriber": bool(position_info & 0xFF),
    }
    _meta_cache.put(ck, out)
    return out


async def _v4_lock_report(pool_id: str, *, network: str) -> dict | None:
    target = pool_id.lower()
    if not target.startswith("0x"):
        target = "0x" + target

    posm = _UNISWAP_V4_POSITION_MANAGER.get(network)
    if not posm:
        log.warning("lp_lock: no V4 PositionManager configured for %r", network)
        return None

    lockers_meta = _KNOWN_V4_LOCKERS.get(network) or []
    if not lockers_meta:
        log.warning("lp_lock: no V4 lockers configured for %r", network)

    locker_rows: list[dict] = []
    total_positions = 0
    matched_pool_key: tuple | None = None
    for L in lockers_meta:
        nft_ids = await _get_locker_nft_ids(L["address"], posm, network)
        if not nft_ids:
            continue
        matched_positions: list[dict] = []
        for tid in nft_ids:
            pos = await _read_v4_position(posm, tid, network)
            if not pos:
                continue
            if pos["pool_id"].lower() == target:
                matched_positions.append({
                    "token_id": pos["token_id"],
                    "tick_lower": pos["tick_lower"],
                    "tick_upper": pos["tick_upper"],
                    "has_subscriber": pos["has_subscriber"],
                })
                if matched_pool_key is None:
                    matched_pool_key = (pos["currency0"], pos["currency1"],
                                         pos["fee"], pos["tick_spacing"],
                                         pos["hooks"])
        if not matched_positions:
            continue
        locker_rows.append({
            "protocol": L["protocol"], "version": L["version"],
            "name": L["name"], "address": L["address"],
            "n_positions": len(matched_positions),
            "positions": matched_positions,
        })
        total_positions += len(matched_positions)

    locker_rows.sort(key=lambda r: r["n_positions"], reverse=True)
    out = {
        "pool_id": target,
        "network": network,
        "pool_type": "v4",
        "n_locked_positions": total_positions,
        "is_locked": total_positions > 0,
        "lockers": locker_rows,
        "ts": _now_iso(),
    }
    if matched_pool_key:
        out["currency0"], out["currency1"], out["fee"], out["tick_spacing"], out["hooks"] = matched_pool_key
    return out


# ---------- public entry point ----------

async def lp_lock(pool_or_id: str, *, network: str = "base",
                   v2_lock_threshold_pct: float = _DEFAULT_V2_LOCK_THRESHOLD_PCT) -> dict | None:
    """Detect locks on a V2 pair, V3 pool, or V4 pool. Auto-detects from input.

    Input format:
        - 20-byte address (40 hex chars after 0x) → V2 or V3 pool contract
        - 32-byte PoolId  (64 hex chars after 0x) → V4 pool identifier

    Returns shape depends on pool_type — see module docstring for details.
    Returns None if the pool can't be classified, RPC fails, or no known
    lockers are configured for the network.
    """
    raw = (pool_or_id or "").strip()
    if not raw:
        return None
    canon = settings.normalize_network(network)
    ck = ("lp_lock", canon, raw.lower())
    cached = _cache.get(ck, settings.ONCHAIN_TTL_S)
    if cached is not None:
        return cached

    kind = _input_kind(raw)
    out: dict | None = None
    if kind == "address":
        pool_type = await _detect_pool_type(raw, canon)
        if pool_type == "v2":
            out = await _v2_lock_report(raw, network=canon,
                                         threshold=v2_lock_threshold_pct)
        elif pool_type == "v3":
            out = await _v3_lock_report(raw, network=canon)
        else:
            log.warning("lp_lock: address %s classified as neither V2 nor V3",
                        raw[:10])
            return None
    elif kind == "poolid":
        out = await _v4_lock_report(raw, network=canon)
    else:
        log.warning("lp_lock: %r is neither a 20-byte address nor a 32-byte PoolId",
                    raw[:20])
        return None

    if out is not None:
        _cache.put(ck, out)
    return out
