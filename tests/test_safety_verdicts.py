"""Regression tests for token-safety verdict semantics (audit P0 fixes).

The token-safety modules must never let missing/partial upstream data read as a
"safe" verdict. These tests pin that contract:

- goplus._risk_summary: a partial response that omits the hard-blocker fields
  must not settle at "low".
- honeypot.is_honeypot: is_honeypot / can_buy / can_sell are None (unknown) when
  the simulation didn't succeed or honeypotResult is absent — never a hard False.

goplus tests are pure; honeypot tests monkeypatch the HTTP layer (no network/key).
"""
from __future__ import annotations

import pytest

_CRITICAL = ("is_honeypot", "cannot_sell_all", "hidden_owner",
             "can_take_back_ownership", "selfdestruct")


# --------------------------- goplus (pure) ---------------------------

def test_goplus_empty_response_is_unknown():
    from tckr.goplus import _risk_summary
    assert _risk_summary({"raw": {}})["risk_level"] == "unknown"


def test_goplus_partial_missing_critical_is_not_low():
    """Looks clean (source verified) but omits the hard-blocker fields → unknown,
    not low. This is the core P0 bug: a partial body must not read as safe."""
    from tckr.goplus import _risk_summary
    row = {"raw": {"is_open_source": "1"}, "is_open_source": True}
    out = _risk_summary(row)
    assert out["risk_level"] == "unknown"
    assert any("incomplete" in w.lower() for w in out["soft_warnings"])


def test_goplus_complete_clean_is_low():
    from tckr.goplus import _risk_summary
    raw = {k: "0" for k in _CRITICAL}
    raw["is_open_source"] = "1"
    row = {"raw": raw, "is_open_source": True}
    assert _risk_summary(row)["risk_level"] == "low"


def test_goplus_honeypot_is_critical():
    from tckr.goplus import _risk_summary
    raw = {k: "0" for k in _CRITICAL}
    raw["is_honeypot"] = "1"
    row = {"raw": raw, "is_honeypot": True, "is_open_source": True}
    out = _risk_summary(row)
    assert out["risk_level"] == "critical"
    assert out["hard_blockers"]


# --------------------------- honeypot (monkeypatched HTTP) ---------------------------


async def _run_is_honeypot(monkeypatch, body, addr):
    from tckr import _http, honeypot

    async def fake_get_json(*args, **kwargs):
        return body

    monkeypatch.setattr(_http, "get_json", fake_get_json)
    return await honeypot.is_honeypot("base", addr)


@pytest.mark.asyncio
async def test_honeypot_unknown_when_no_result(monkeypatch):
    """No honeypotResult + failed simulation → unknown (None), never False."""
    body = {"simulationSuccess": False}
    out = await _run_is_honeypot(monkeypatch, body,
                                 "0x1111111111111111111111111111111111111111")
    assert out is not None
    assert out["is_honeypot"] is None
    assert out["can_buy"] is None
    assert out["can_sell"] is None
    assert out["simulation_success"] is False


@pytest.mark.asyncio
async def test_honeypot_confirmed_forces_cannot_trade(monkeypatch):
    body = {"honeypotResult": {"isHoneypot": True, "honeypotReason": "blocks sell"},
            "simulationResult": {"buyGas": "100000"}, "simulationSuccess": True}
    out = await _run_is_honeypot(monkeypatch, body,
                                 "0x2222222222222222222222222222222222222222")
    assert out["is_honeypot"] is True
    assert out["can_buy"] is False
    assert out["can_sell"] is False


@pytest.mark.asyncio
async def test_honeypot_clean_allows_trade(monkeypatch):
    body = {"honeypotResult": {"isHoneypot": False},
            "simulationResult": {"buyGas": "100000", "sellGas": "120000",
                                 "buyTax": 0.0, "sellTax": 0.0},
            "simulationSuccess": True}
    out = await _run_is_honeypot(monkeypatch, body,
                                 "0x3333333333333333333333333333333333333333")
    assert out["is_honeypot"] is False
    assert out["can_buy"] is True
    assert out["can_sell"] is True
