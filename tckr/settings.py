"""tckr configuration — environment-driven, with sane defaults.

This module deliberately depends on nothing else. The package is meant to be
`pip install -e`'d into multiple projects, each of which may set different
environment variables. Override any value by exporting the matching env var
before first import, or by mutating the attribute at runtime.
"""
from __future__ import annotations

import os


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _env_str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


# ---------- Networks ----------
NETWORK_BASE = "base"
NETWORK_SOLANA = "solana"
NETWORK_ETH = "eth"
SUPPORTED_NETWORKS = {NETWORK_BASE, NETWORK_SOLANA, NETWORK_ETH}

# Common user spellings -> canonical GeckoTerminal network ids.
_NETWORK_ALIASES = {
    "sol": NETWORK_SOLANA, "solana": NETWORK_SOLANA,
    "base": NETWORK_BASE,
    "eth": NETWORK_ETH, "ethereum": NETWORK_ETH, "mainnet": NETWORK_ETH,
}


def normalize_network(name: str) -> str:
    """Map a loose network name to a canonical GeckoTerminal network id."""
    cleaned = (name or "").strip().lower()
    return _NETWORK_ALIASES.get(cleaned, cleaned)


# ---------- Cache TTLs (seconds) ----------
DEX_TTL_S       = _env_int("TCKR_DEX_TTL_S", 60)
DEX_OHLCV_TTL_S = _env_int("TCKR_DEX_OHLCV_TTL_S", 300)
PERPS_TTL_S     = _env_int("TCKR_PERPS_TTL_S", 30)
TVL_TTL_S       = _env_int("TCKR_TVL_TTL_S", 600)
ONCHAIN_TTL_S   = _env_int("TCKR_ONCHAIN_TTL_S", 120)
# Token contract metadata is immutable — cache it for a day by default.
TOKEN_METADATA_TTL_S = _env_int("TCKR_TOKEN_METADATA_TTL_S", 86400)
# Cross-exchange perps aggregates (Coinalyze) — slightly slower-moving than HL.
FUNDING_AGG_TTL_S    = _env_int("TCKR_FUNDING_AGG_TTL_S", 60)
# Liquidation bins update every interval; cache short.
LIQUIDATION_TTL_S    = _env_int("TCKR_LIQUIDATION_TTL_S", 120)
# Contract security (Goplus) — rarely changes for a given contract.
SECURITY_TTL_S       = _env_int("TCKR_SECURITY_TTL_S", 3600)
# Honeypot simulation result — short cache to catch fast-evolving rug attempts.
HONEYPOT_TTL_S       = _env_int("TCKR_HONEYPOT_TTL_S", 300)
# Birdeye token data — fast-moving for new launches.
BIRDEYE_TTL_S        = _env_int("TCKR_BIRDEYE_TTL_S", 60)

# ---------- HTTP ----------
HTTP_TIMEOUT_S   = _env_float("TCKR_HTTP_TIMEOUT_S", 15.0)
HTTP_MAX_RETRIES = _env_int("TCKR_HTTP_MAX_RETRIES", 2)

# ---------- API keys ----------
# On-chain modules
ALCHEMY_API_KEY   = os.environ.get("ALCHEMY_API_KEY", "").strip()
BASESCAN_API_KEY  = os.environ.get("BASESCAN_API_KEY", "").strip()
HELIUS_API_KEY    = os.environ.get("HELIUS_API_KEY", "").strip()
# Cross-exchange perps aggregator (free signup, no card)
COINALYZE_API_KEY = os.environ.get("COINALYZE_API_KEY", "").strip()
# Solana-focused token data (free tier with key)
BIRDEYE_API_KEY   = os.environ.get("BIRDEYE_API_KEY", "").strip()
# Pump.fun discovery via Moralis Solana gateway (free tier).
MORALIS_API_KEY   = os.environ.get("MORALIS_API_KEY", "").strip()
# Pump.fun discovery fallback / richer queries via Bitquery (free tier).
BITQUERY_API_KEY  = os.environ.get("BITQUERY_API_KEY", "").strip()
# Neynar / Farcaster: cast search, channel feeds, trending fungibles, user data.
NEYNAR_API_KEY    = os.environ.get("NEYNAR_API_KEY", "").strip()
# CoinGecko — pro is paid, demo is free-with-key (higher RL than no-key public).
COINGECKO_API_KEY      = os.environ.get("COINGECKO_API_KEY", "").strip()
COINGECKO_DEMO_API_KEY = os.environ.get("COINGECKO_DEMO_API_KEY", "").strip()
# Etherscan V2 unified — one key covers ~70 EVM chains via chainid param.
# Falls back to BASESCAN_API_KEY (declared above) for backward-compat.
ETHERSCAN_API_KEY      = os.environ.get("ETHERSCAN_API_KEY", "").strip()
# Solscan — Pro tier optional (public endpoints work without it).
SOLSCAN_API_KEY        = os.environ.get("SOLSCAN_API_KEY", "").strip()
# LunarCrush — required for any /public/* call (Bearer token).
LUNARCRUSH_API_KEY     = os.environ.get("LUNARCRUSH_API_KEY", "").strip()
# Messari — most endpoints free-tier-throttled or paywalled.
MESSARI_API_KEY        = os.environ.get("MESSARI_API_KEY", "").strip()
# Token Terminal — Bearer token, free tier has limited metric coverage.
TOKENTERMINAL_API_KEY  = os.environ.get("TOKENTERMINAL_API_KEY", "").strip()
# The Graph — optional; without it we use the public gateway (throttled).
THEGRAPH_API_KEY       = os.environ.get("THEGRAPH_API_KEY", "").strip()
# Bankr — launchpad feed is keyless; key unlocks resolve_address + search_users.
BANKR_API_KEY          = os.environ.get("BANKR_API_KEY", "").strip()
# Alpaca — US equity/ETF options chains + greeks. Free signup (no funding) at
# alpaca.markets gives both keys; data uses the free `indicative` feed by
# default. ALPACA_OPTIONS_FEED=opra switches to real-time once subscribed.
ALPACA_API_KEY         = os.environ.get("ALPACA_API_KEY", "").strip()
ALPACA_API_SECRET      = os.environ.get("ALPACA_API_SECRET", "").strip()
ALPACA_OPTIONS_FEED    = _env_str("ALPACA_OPTIONS_FEED", "indicative")

# ---------- Pump.fun ----------
PUMPFUN_DISCOVERY_TTL_S = _env_int("TCKR_PUMPFUN_DISCOVERY_TTL_S", 30)
PUMPFUN_STATE_TTL_S     = _env_int("TCKR_PUMPFUN_STATE_TTL_S", 15)

# ---------- Neynar / Farcaster ----------
# Trending and channel data change on the order of seconds-to-minutes; user
# data and token metadata change more slowly.
NEYNAR_FEED_TTL_S       = _env_int("TCKR_NEYNAR_FEED_TTL_S", 60)
NEYNAR_USER_TTL_S       = _env_int("TCKR_NEYNAR_USER_TTL_S", 300)
NEYNAR_TOKEN_TTL_S      = _env_int("TCKR_NEYNAR_TOKEN_TTL_S", 120)

# ---------- Wallet PnL ----------
# Wallet transfer history rarely changes for past transactions — cache long.
# Price lookups for the unrealized leg should refresh more often.
WALLET_HISTORY_TTL_S    = _env_int("TCKR_WALLET_HISTORY_TTL_S", 300)
WALLET_PRICE_TTL_S      = _env_int("TCKR_WALLET_PRICE_TTL_S", 60)

# ---------- Base launchpads (Virtuals, Clanker) ----------
# Newly created tokens stream in continuously; cache short for discovery.
LAUNCHPAD_DISCOVERY_TTL_S = _env_int("TCKR_LAUNCHPAD_DISCOVERY_TTL_S", 60)
LAUNCHPAD_TOKEN_TTL_S     = _env_int("TCKR_LAUNCHPAD_TOKEN_TTL_S", 120)

# ---------- CoinGecko ----------
# Spot / market data moves second-by-second; history is immutable so caches long.
COINGECKO_TTL_S         = _env_int("TCKR_COINGECKO_TTL_S", 30)
COINGECKO_HISTORY_TTL_S = _env_int("TCKR_COINGECKO_HISTORY_TTL_S", 600)

# ---------- Polymarket ----------
# Odds shift continuously; cache short.
POLYMARKET_TTL_S        = _env_int("TCKR_POLYMARKET_TTL_S", 30)
# Polymarket occasionally renames a market's slug while keeping the same
# on-chain conditionId. When set, tckr persists slug -> conditionId mappings
# to this file so the next-fetch can recover the canonical slug via the
# stable conditionId. Unset (default) keeps the alias map in-memory only.
POLYMARKET_ALIASES_PATH = _env_str("TCKR_POLYMARKET_ALIASES_PATH", "")

# ---------- Pyth (Hermes) ----------
# Price moves are sub-second on-chain; cache very short for prices, much
# longer for the catalog (rarely changes).
PYTH_PRICE_TTL_S        = _env_int("TCKR_PYTH_PRICE_TTL_S", 10)
PYTH_CATALOG_TTL_S      = _env_int("TCKR_PYTH_CATALOG_TTL_S", 3600)

# ---------- Etherscan V2 ----------
# Tx history changes per block; balances change per tx; contract
# verifications are write-once. Bucket TTLs accordingly.
ETHERSCAN_TTL_S          = _env_int("TCKR_ETHERSCAN_TTL_S", 30)
ETHERSCAN_CONTRACT_TTL_S = _env_int("TCKR_ETHERSCAN_CONTRACT_TTL_S", 86400)
ETHERSCAN_GAS_TTL_S      = _env_int("TCKR_ETHERSCAN_GAS_TTL_S", 15)
ETHERSCAN_STATS_TTL_S    = _env_int("TCKR_ETHERSCAN_STATS_TTL_S", 600)

# ---------- Solscan ----------
SOLSCAN_TTL_S            = _env_int("TCKR_SOLSCAN_TTL_S", 60)

# ---------- LunarCrush ----------
LUNARCRUSH_TTL_S         = _env_int("TCKR_LUNARCRUSH_TTL_S", 120)

# ---------- Messari ----------
MESSARI_TTL_S            = _env_int("TCKR_MESSARI_TTL_S", 300)

# ---------- Token Terminal ----------
TOKENTERMINAL_TTL_S         = _env_int("TCKR_TOKENTERMINAL_TTL_S", 300)
TOKENTERMINAL_HISTORY_TTL_S = _env_int("TCKR_TOKENTERMINAL_HISTORY_TTL_S", 3600)

# ---------- The Graph ----------
THEGRAPH_TTL_S           = _env_int("TCKR_THEGRAPH_TTL_S", 60)

# ---------- Options (Alpaca) ----------
# Quotes/greeks move continuously, but the free `indicative` feed is delayed
# ~15m anyway, so a short cache is plenty. The expiration ladder changes slowly.
OPTIONS_TTL_S             = _env_int("TCKR_OPTIONS_TTL_S", 30)
OPTIONS_EXPIRATIONS_TTL_S = _env_int("TCKR_OPTIONS_EXPIRATIONS_TTL_S", 3600)
