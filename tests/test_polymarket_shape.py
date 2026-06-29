"""Regression tests for polymarket._shape_market YES/NO mapping (audit P1 fix).

Polymarket doesn't guarantee outcome order is ["Yes","No"], and multi-outcome
markets have no YES/NO at all. _shape_market must map YES/NO by the outcome
labels, not by a hardcoded index 0/1. Pure tests (no network).
"""
from __future__ import annotations

import json


def _market(outcomes, prices, tokens):
    # The Gamma API serializes these as JSON strings.
    return {
        "id": "1", "conditionId": "0xabc", "slug": "s", "question": "q?",
        "outcomes": json.dumps(outcomes),
        "outcomePrices": json.dumps([str(p) for p in prices]),
        "clobTokenIds": json.dumps(tokens),
    }


def test_standard_yes_no_order():
    from tckr.polymarket import _shape_market
    s = _shape_market(_market(["Yes", "No"], [0.62, 0.38], ["tokYES", "tokNO"]))
    assert s["yes_price"] == 0.62
    assert s["no_price"] == 0.38
    assert s["yes_token_id"] == "tokYES"
    assert s["no_token_id"] == "tokNO"


def test_reversed_yes_no_order_maps_by_label():
    """The core fix: when outcomes are ["No","Yes"], YES must come from the
    'Yes' index (1), not index 0."""
    from tckr.polymarket import _shape_market
    s = _shape_market(_market(["No", "Yes"], [0.30, 0.70], ["tokNO", "tokYES"]))
    assert s["yes_price"] == 0.70
    assert s["no_price"] == 0.30
    assert s["yes_token_id"] == "tokYES"
    assert s["no_token_id"] == "tokNO"


def test_multi_outcome_has_no_yes_no():
    """A >2-outcome market has no YES/NO — yes_price/no_price are None, but the
    full outcomes/prices are still surfaced."""
    from tckr.polymarket import _shape_market
    s = _shape_market(_market(["Trump", "Biden", "Other"], [0.5, 0.45, 0.05],
                              ["t1", "t2", "t3"]))
    assert s["yes_price"] is None
    assert s["no_price"] is None
    assert s["outcomes"] == ["Trump", "Biden", "Other"]
    # outcome_prices is passed through as parsed (string) values; the point is it
    # is still surfaced in full for multi-outcome markets.
    assert len(s["outcome_prices"]) == 3
