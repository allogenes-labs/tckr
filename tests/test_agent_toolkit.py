"""Agent-toolkit tests: the sort helper, sortable universe tools, and the
cz_oi_aggregate tool.

The registry/helper checks are pure (no network, no keys). The hl_universe
ordering check hits live Hyperliquid (keyless) and is tolerant of upstream
flakiness, matching tests/test_keyless_smoke.py.
"""
from __future__ import annotations

import pytest

# --------------------------- _sort_by (pure) ---------------------------

def test_sort_by_descending_and_none_last():
    from tckr.agent_toolkit.core import _sort_by
    rows = [{"v": 3}, {"v": None}, {"v": 10}, {"v": 1}, {}]  # {} == missing
    out = _sort_by(rows, "v", desc=True)
    vals = [r.get("v") for r in out]
    assert vals[:3] == [10, 3, 1], vals
    # both the None value and the missing-key row land at the bottom
    assert vals[3:] == [None, None], vals


def test_sort_by_ascending_keeps_none_last():
    from tckr.agent_toolkit.core import _sort_by
    rows = [{"v": 3}, {"v": None}, {"v": 10}, {"v": 1}]
    out = _sort_by(rows, "v", desc=False)
    vals = [r.get("v") for r in out]
    assert vals[:3] == [1, 3, 10], vals
    assert vals[3] is None, "None must sort last even when ascending"


def test_sort_by_handles_non_numeric():
    from tckr.agent_toolkit.core import _sort_by
    rows = [{"v": "oops"}, {"v": 5}]
    out = _sort_by(rows, "v", desc=True)
    # the un-coercible value is treated as missing → sorts last
    assert out[0]["v"] == 5


# --------------------------- registry guards (pure) ---------------------------

def test_cz_oi_aggregate_registered():
    from tckr.agent_toolkit.core import get_tool
    spec = get_tool("cz_oi_aggregate")
    assert spec is not None, "cz_oi_aggregate is not registered in TOOLS"
    # module must resolve in the registry so the tier-tag lookup works
    assert spec.module == "coinalyze"
    assert spec.schema.get("required") == ["base"]


def test_universe_tools_expose_sort_enum():
    from tckr.agent_toolkit.core import get_tool
    perp = get_tool("hl_universe")
    spot = get_tool("hl_spot_universe")
    assert perp and spot
    perp_sorts = perp.schema["properties"]["sort"]["enum"]
    assert {"volume", "oi", "funding", "funding_excess", "change"} == set(perp_sorts)
    spot_sorts = spot.schema["properties"]["sort"]["enum"]
    assert {"volume", "change", "px"} == set(spot_sorts)


# --------------------------- live ordering (keyless) ---------------------------

@pytest.mark.asyncio
async def test_hl_universe_sort_by_oi_is_descending():
    from tckr.agent_toolkit.core import get_tool
    spec = get_tool("hl_universe")
    rows = await spec.callable({"sort": "oi", "limit": 10})
    if not rows:
        pytest.skip("upstream returned no rows — likely transient")
    ois = [r.get("open_interest_usd") for r in rows if r.get("open_interest_usd") is not None]
    assert ois == sorted(ois, reverse=True), f"rows not OI-descending: {ois}"


@pytest.mark.asyncio
async def test_hl_universe_default_is_volume_ranked():
    from tckr.agent_toolkit.core import get_tool
    spec = get_tool("hl_universe")
    rows = await spec.callable({"limit": 10})  # no sort → volume desc (legacy behavior)
    if not rows:
        pytest.skip("upstream returned no rows — likely transient")
    vols = [r.get("day_notional_volume_usd") for r in rows
            if r.get("day_notional_volume_usd") is not None]
    assert vols == sorted(vols, reverse=True), f"default not volume-descending: {vols}"
