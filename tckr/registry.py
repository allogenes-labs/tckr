"""Capability registry — single source of truth for module tiers + auth state.

Every data-source module is registered here with its access tier (keyless /
keyed-free / keyed-paid), the env vars it depends on, and human-readable notes.

This registry drives:
- Agent toolkit: tool descriptions get a tier tag auto-prepended so the model
  sees `[keyed-free: needs COINALYZE_API_KEY]` next to each tool.
- CLI: `tckr status` reads it to print what's configured right now.
- Introspection: the `capabilities` MCP tool serializes it to JSON so agents
  can self-discover what works in this environment.

Adding a new data-source module? Add an entry here. The `tests/test_registry.py`
typo-guard will fail until you do.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from tckr import settings


class Tier(StrEnum):
    """Access tier — drives the tag shown in tool descriptions."""
    KEYLESS_FREE = "keyless-free"   # no signup, no key
    KEYED_FREE   = "keyed-free"     # free signup, key required
    KEYED_PAID   = "keyed-paid"     # paid plan required for meaningful usage


@dataclass(frozen=True)
class ModuleSpec:
    """One row in the capability registry.

    Semantics of `required_env` vs `optional_env`:

    - `required_env` — ALL of these must be set for the module to be considered
      configured. (e.g. `coinalyze` needs `COINALYZE_API_KEY`.)

    - `optional_env` — interpreted differently depending on whether
      `required_env` is empty:
        * If `required_env` is non-empty: optional keys unlock EXTRA features
          but are not required (e.g. `pumpfun` requires Helius for state but
          optionally unlocks more discovery sources via Moralis/Bitquery).
        * If `required_env` is empty: ANY ONE of these is sufficient (e.g.
          `wallet_pnl` works if you have keys for at least one supported chain).
    """
    name: str
    tier: Tier
    required_env: tuple[str, ...] = ()
    optional_env: tuple[str, ...] = ()
    notes: str = ""
    extras: dict = field(default_factory=dict)  # free-form per-module metadata


# Order chosen to match the README sources table.
REGISTRY: dict[str, ModuleSpec] = {
    # ---------- Keyless ----------
    "geckoterminal": ModuleSpec(
        "geckoterminal", Tier.KEYLESS_FREE,
        notes="DEX pools, tokens by contract, OHLCV (Base/Solana/ETH).",
    ),
    "dexscreener": ModuleSpec(
        "dexscreener", Tier.KEYLESS_FREE,
        notes="DEX pairs, search, latest token profiles, paid-boost rankings.",
    ),
    "hyperliquid": ModuleSpec(
        "hyperliquid", Tier.KEYLESS_FREE,
        notes="Single-exchange perps: funding, OI, marks, candle history. "
              "Canonical free-tier fallback when CoinGecko is rate-limited.",
    ),
    "defillama": ModuleSpec(
        "defillama", Tier.KEYLESS_FREE,
        notes="Chain/protocol TVL, DEX volume, stablecoins, yields.",
    ),
    "goplus": ModuleSpec(
        "goplus", Tier.KEYLESS_FREE,
        notes="EVM token contract security scans. Soft ~30 req/min.",
    ),
    "honeypot": ModuleSpec(
        "honeypot", Tier.KEYLESS_FREE,
        notes="Sell-simulation backstop on ETH / BSC / Base.",
    ),
    "virtuals": ModuleSpec(
        "virtuals", Tier.KEYLESS_FREE,
        notes="Virtuals Protocol AI-agent launchpad on Base.",
    ),
    "clanker": ModuleSpec(
        "clanker", Tier.KEYLESS_FREE,
        notes="Clanker Farcaster-native token launcher; carries requestor_fid "
              "for cross-link with neynar.",
    ),

    # ---------- Keyed-free ----------
    "alchemy": ModuleSpec(
        "alchemy", Tier.KEYED_FREE,
        required_env=("ALCHEMY_API_KEY",),
        notes="EVM (Base, ETH) wallet balances + transfers. Free tier at alchemy.com.",
    ),
    "helius": ModuleSpec(
        "helius", Tier.KEYED_FREE,
        required_env=("HELIUS_API_KEY",),
        notes="Solana RPC convenience layer. Free tier at helius.dev.",
    ),
    "coinalyze": ModuleSpec(
        "coinalyze", Tier.KEYED_FREE,
        required_env=("COINALYZE_API_KEY",),
        notes="Cross-exchange perps aggregator (funding spread, OI, liquidations). "
              "Free signup at coinalyze.net, no card.",
    ),
    "birdeye": ModuleSpec(
        "birdeye", Tier.KEYED_FREE,
        required_env=("BIRDEYE_API_KEY",),
        notes="Solana-focused token analytics. Free tier ~30 req/min at birdeye.so.",
    ),
    "pumpfun": ModuleSpec(
        "pumpfun", Tier.KEYED_FREE,
        # Discovery needs at least one of Moralis/Bitquery; on-chain state
        # uses Helius. Treat all three as optional with notes — registry's
        # `configured()` returns True iff ALL required (none) AND at least one
        # optional is set per the any-of rule.
        optional_env=("MORALIS_API_KEY", "BITQUERY_API_KEY", "HELIUS_API_KEY"),
        notes="Pump.fun Solana memecoin launchpad. Either Moralis or Bitquery "
              "alone enables discovery; Helius unlocks on-chain bonding-curve state. "
              "Bitquery unlocks 5 exclusive analytics fns.",
    ),
    "lp_lock": ModuleSpec(
        "lp_lock", Tier.KEYED_FREE,
        required_env=("ALCHEMY_API_KEY",),
        notes="LP-lock detection for Uniswap V2/V3/V4 on Base + ETH. "
              "Auto-detects pool type from input shape.",
    ),
    "wallet_pnl": ModuleSpec(
        "wallet_pnl", Tier.KEYED_FREE,
        # Composite: any one chain's key is enough to use the module on that chain.
        optional_env=("HELIUS_API_KEY", "ALCHEMY_API_KEY", "MORALIS_API_KEY", "BIRDEYE_API_KEY"),
        notes="FIFO PnL across Sol + Base. Auto-resolves ATA-to-owner. "
              "Filters wSOL/WETH/stables as counter assets by default.",
    ),
    "jito": ModuleSpec(
        "jito", Tier.KEYED_FREE,
        # Tip-floor and bundle status are keyless; snipe_score / tx_jito_info
        # need Helius. Listed as required because the headline functions need it.
        required_env=("HELIUS_API_KEY",),
        notes="Solana MEV intel. tip_floor() and bundle_status() are keyless; "
              "tx_jito_info / snipe_score need HELIUS_API_KEY for tx parsing.",
    ),

    # ---------- Keyed-paid ----------
    "neynar": ModuleSpec(
        "neynar", Tier.KEYED_PAID,
        required_env=("NEYNAR_API_KEY",),
        notes="Farcaster API. Free tier only enables user_by_username; "
              "search_casts / channel_feed / trending_fungibles / 5 others "
              "return 402 until paid plan (as of 2026-05).",
    ),

    # ---------- New in this release (Phase 5 sweep) ----------
    "coingecko": ModuleSpec(
        "coingecko", Tier.KEYLESS_FREE,
        # Public endpoints work no-key (~10-30 req/min). DEMO key bumps quota;
        # PRO key unlocks pro-api endpoints. The module picks the right path
        # automatically.
        optional_env=("COINGECKO_API_KEY", "COINGECKO_DEMO_API_KEY"),
        notes="Spot/market/historical via CoinGecko v3. Works keyless; add "
              "COINGECKO_DEMO_API_KEY for higher RL; COINGECKO_API_KEY uses Pro.",
    ),
    "polymarket": ModuleSpec(
        "polymarket", Tier.KEYLESS_FREE,
        notes="Polymarket Gamma API — binary prediction-market odds (YES/NO). "
              "Composes well with the macro snapshot.",
    ),
    "pyth": ModuleSpec(
        "pyth", Tier.KEYLESS_FREE,
        notes="Pyth Network Hermes — on-chain oracle prices (~400 feeds: "
              "crypto, equities, FX, metals, rates). Sub-second cadence.",
    ),
    "etherscan": ModuleSpec(
        "etherscan", Tier.KEYED_FREE,
        # ETHERSCAN_API_KEY is preferred; BASESCAN_API_KEY accepted as fallback
        # because the V2 unified API uses one key across all chains.
        optional_env=("ETHERSCAN_API_KEY", "BASESCAN_API_KEY"),
        notes="Etherscan V2 unified API — one key covers ~70 EVM chains "
              "(ETH=1, Base=8453, Arb=42161, Op=10, Polygon=137, BNB=56, ...).",
    ),
    "solscan": ModuleSpec(
        "solscan", Tier.KEYLESS_FREE,
        # Public endpoints work no-key; SOLSCAN_API_KEY unlocks Pro endpoints
        # with richer payloads.
        optional_env=("SOLSCAN_API_KEY",),
        notes="Solana block explorer. Public endpoints work keyless; "
              "SOLSCAN_API_KEY unlocks Pro (richer parsing, higher RL).",
    ),
    "lunarcrush": ModuleSpec(
        "lunarcrush", Tier.KEYED_FREE,
        required_env=("LUNARCRUSH_API_KEY",),
        notes="Social-sentiment scoring (Galaxy Score, AltRank). Free tier "
              "~100 req/day on /public/* endpoints.",
    ),
    "messari": ModuleSpec(
        "messari", Tier.KEYED_PAID,
        required_env=("MESSARI_API_KEY",),
        notes="Research-grade asset profiles + metrics + news. Most metric "
              "and profile endpoints moved behind paid plans in 2024-2025; "
              "free 'Hobbyist' tier limits to ~20 req/min on a subset.",
    ),
    "tokenterminal": ModuleSpec(
        "tokenterminal", Tier.KEYED_PAID,
        required_env=("TOKENTERMINAL_API_KEY",),
        notes="Protocol fundamentals (revenue, fees, P/E, treasury). Free "
              "tier exposes project catalog + limited metrics; detailed "
              "historical series are paid.",
    ),
    "thegraph": ModuleSpec(
        "thegraph", Tier.KEYLESS_FREE,
        # Optional key unlocks the higher-quota decentralized gateway; without
        # it the public gateway works but is heavily throttled.
        optional_env=("THEGRAPH_API_KEY",),
        notes="GraphQL access to indexed subgraphs (Uniswap, Aave, etc). "
              "Keyless via public gateway (throttled); THEGRAPH_API_KEY uses "
              "the decentralized gateway with higher quota.",
    ),
}


def _has(name: str) -> bool:
    """Truthy check on a settings attribute (env var presence)."""
    return bool(getattr(settings, name, "") or "")


def configured(name: str) -> bool:
    """Return True iff the module's auth requirements are satisfied.

    - For keyless modules: always True.
    - For modules with `required_env`: every required key must be set; optional
      keys are not part of the check (they unlock extras, not the basic path).
    - For modules with empty `required_env` but non-empty `optional_env`:
      at least one optional key must be set (any-of).
    """
    spec = REGISTRY.get(name)
    if spec is None:
        return False
    if spec.tier == Tier.KEYLESS_FREE:
        return True
    if spec.required_env:
        return all(_has(k) for k in spec.required_env)
    if spec.optional_env:
        return any(_has(k) for k in spec.optional_env)
    return True


def missing_keys(name: str) -> list[str]:
    """Return the list of env vars that, if set, would change `configured()`
    from False to True. Empty list iff already configured."""
    spec = REGISTRY.get(name)
    if spec is None or configured(name):
        return []
    if spec.required_env:
        return [k for k in spec.required_env if not _has(k)]
    # any-of: missing means all optional are unset
    return list(spec.optional_env)


def tier_tag(name: str) -> str:
    """Compact tag prefix for a tool description.

    Examples:
      "[keyless]"
      "[keyed-free: needs COINALYZE_API_KEY]"
      "[keyed-free ✓]"            (configured)
      "[paid: NEYNAR_API_KEY required]"
    """
    spec = REGISTRY.get(name)
    if spec is None:
        return "[unknown]"
    is_configured = configured(name)
    if spec.tier == Tier.KEYLESS_FREE:
        return "[keyless]"
    if spec.tier == Tier.KEYED_FREE:
        if is_configured:
            return "[keyed-free OK]"
        keys = " or ".join(missing_keys(name)) if not spec.required_env else \
               " + ".join(missing_keys(name))
        return f"[keyed-free: needs {keys}]"
    if spec.tier == Tier.KEYED_PAID:
        if is_configured:
            return "[paid OK]"
        keys = " + ".join(missing_keys(name) or spec.required_env)
        return f"[paid: {keys} required]"
    return "[?]"


def capabilities() -> dict:
    """Serialize the full registry + current configured-state to a JSON-safe dict.

    Shape:
      {
        "modules": {
          "coinalyze": {
            "tier": "keyed-free",
            "required_env": ["COINALYZE_API_KEY"],
            "optional_env": [],
            "configured": true,
            "missing_keys": [],
            "notes": "Cross-exchange perps aggregator…"
          },
          ...
        },
        "summary": {
          "total": 17,
          "configured": 9,
          "by_tier": {"keyless-free": 8, "keyed-free": 8, "keyed-paid": 1},
        }
      }
    """
    modules: dict[str, dict] = {}
    by_tier: dict[str, int] = {}
    n_configured = 0
    for name, spec in REGISTRY.items():
        is_cfg = configured(name)
        if is_cfg:
            n_configured += 1
        by_tier[spec.tier.value] = by_tier.get(spec.tier.value, 0) + 1
        modules[name] = {
            "tier": spec.tier.value,
            "required_env": list(spec.required_env),
            "optional_env": list(spec.optional_env),
            "configured": is_cfg,
            "missing_keys": missing_keys(name),
            "notes": spec.notes,
        }
    return {
        "modules": modules,
        "summary": {
            "total": len(REGISTRY),
            "configured": n_configured,
            "by_tier": by_tier,
        },
    }


def format_status() -> str:
    """Human-readable status table for the CLI."""
    lines = ["module           tier         configured  missing keys / notes"]
    lines.append("-" * 90)
    for name, spec in REGISTRY.items():
        cfg = "yes" if configured(name) else "no"
        mk = missing_keys(name)
        suffix = ""
        if mk:
            joiner = " or " if not spec.required_env else " + "
            suffix = joiner.join(mk)
        elif spec.notes:
            suffix = spec.notes[:60] + ("..." if len(spec.notes) > 60 else "")
        lines.append(f"{name:<16} {spec.tier.value:<12} {cfg:<11} {suffix}")
    caps = capabilities()["summary"]
    lines.append("-" * 90)
    lines.append(
        f"{caps['configured']}/{caps['total']} modules configured  |  "
        + "  ".join(f"{t}: {n}" for t, n in caps["by_tier"].items())
    )
    return "\n".join(lines)
