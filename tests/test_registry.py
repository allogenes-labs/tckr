"""Registry typo-guard tests.

The registry declares which env vars unlock each module. Those names must
match the attributes actually read in `tckr.settings`. A typo here means
a tool the user provisioned silently stays offline — these tests catch that
at import time.
"""
from __future__ import annotations

import pytest


def test_registry_env_vars_exist_in_settings():
    """Every env-var name in REGISTRY must be a real attribute on settings."""
    from tckr import registry, settings

    declared = set()
    for spec in registry.REGISTRY.values():
        declared.update(spec.required_env)
        declared.update(spec.optional_env)

    missing = [name for name in declared if not hasattr(settings, name)]
    assert not missing, (
        f"REGISTRY references env vars not declared in tckr.settings: {missing}. "
        f"Either add them to settings.py or fix the spelling in registry.py."
    )


def test_every_data_source_module_in_registry():
    """Adding a data-source module without a registry entry is a process bug."""
    from tckr import registry

    # Known modules as of 0.1.0. Update this list when shipping new modules
    # AND adding the corresponding registry entry (the assert below enforces
    # both happen together).
    expected = {
        "geckoterminal", "dexscreener", "hyperliquid", "defillama",
        "coinalyze", "goplus", "honeypot", "birdeye",
        "pumpfun", "neynar", "wallet_pnl", "lp_lock",
        "virtuals", "clanker", "jito",
        "alchemy", "helius",
        # Phase 5 sweep:
        "coingecko", "polymarket",
        # Phase 5b sweep:
        "pyth", "etherscan", "solscan", "lunarcrush", "messari",
        "tokenterminal", "thegraph",
        # Bankr launchpad integration:
        "bankr",
        # Equity/ETF options (Alpaca):
        "options",
        # Keyless options fallback (CBOE delayed):
        "cboe",
    }
    actual = set(registry.REGISTRY.keys())
    missing = expected - actual
    assert not missing, f"Missing registry entries: {missing}"


def test_capabilities_returns_serializable_dict():
    """capabilities() output must be JSON-serializable for the introspection tool."""
    import json

    from tckr import registry

    caps = registry.capabilities()
    assert isinstance(caps, dict)
    # Round-trip through JSON to catch non-serializable values.
    json.dumps(caps)


def test_capabilities_exposes_expansion_fields():
    """The status dashboard relies on per-module expansion_keys + the summary count."""
    from tckr import registry

    caps = registry.capabilities()
    assert "expandable" in caps["summary"]
    for name, mod in caps["modules"].items():
        assert "expansion_keys" in mod, f"{name} missing expansion_keys"
        # expansion only applies to already-usable modules
        if mod["expansion_keys"]:
            assert mod["configured"], f"{name} reports expansion but isn't configured"
    n_expandable = sum(1 for m in caps["modules"].values() if m["expansion_keys"])
    assert caps["summary"]["expandable"] == n_expandable


def test_expansion_keys_disjoint_from_missing_keys():
    """A key is either enabling (missing) or expanding — never both at once."""
    from tckr import registry

    for name in registry.REGISTRY:
        overlap = set(registry.expansion_keys(name)) & set(registry.missing_keys(name))
        assert not overlap, f"{name}: {overlap} counted as both missing and expanding"


@pytest.mark.parametrize("module_name,expected_tier", [
    ("geckoterminal", "keyless-free"),
    ("hyperliquid",   "keyless-free"),
    ("coinalyze",     "keyed-free"),
    ("birdeye",       "keyed-free"),
    ("neynar",        "keyed-paid"),
])
def test_known_tier_classifications(module_name, expected_tier):
    """Pin the tier for a sample of modules so an accidental retag is caught."""
    from tckr import registry
    spec = registry.REGISTRY[module_name]
    assert spec.tier.value == expected_tier
