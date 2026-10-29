from copy import deepcopy
from datetime import UTC, datetime

import pytest

from jmstats.market import eligible_offers, normalize_market, realized_fee

NOW = datetime(2026, 10, 29, 10, tzinfo=UTC)


def test_bonds_count_once_per_verified_outpoint(watcher_payload):
    duplicate = deepcopy(watcher_payload["fidelitybonds"][0])
    duplicate["counterparty"] = "maker-b"
    watcher_payload["fidelitybonds"].append(duplicate)
    market = normalize_market(watcher_payload, NOW)
    assert market["locked_sats"] == 100_000_000
    assert market["bond_count"] == 1
    assert "maker-a" not in str(market)
    assert "ab" * 32 not in str(market)


def test_unknown_stale_and_conflicting_bonds_not_zero(watcher_payload):
    watcher_payload["fidelitybonds"][0]["verification_stale"] = True
    market = normalize_market(watcher_payload, NOW)
    assert market["unknown_bonds"] == 1
    assert market["locked_sats"] == 0
    assert market["bonded_makers"] == 0


def test_relative_offer_not_subject_to_absolute_cap(watcher_payload):
    market = normalize_market(watcher_payload, NOW)
    selected = eligible_offers(market["offers"], 100_000_000, absolute=500, relative=0.001)
    assert len(selected) == 1
    assert realized_fee(selected[0], 100_000_000) == 100_000
    assert eligible_offers(market["offers"], 100_000_000, relative=0.0005) == []


def test_paid_bondless_excluded_and_free_offer_retained(watcher_payload):
    watcher_payload["offers"][0]["fidelity_bond_verified"] = None
    market = normalize_market(watcher_payload, NOW)
    assert eligible_offers(market["offers"], 1_000_000) == []
    watcher_payload["offers"][0]["cjfee"] = "0"
    assert len(eligible_offers(normalize_market(watcher_payload, NOW)["offers"], 1_000_000)) == 1


def test_invalid_offer_rejects_whole_snapshot(watcher_payload):
    watcher_payload["offers"][0]["maxsize"] = 1
    with pytest.raises(ValueError):
        normalize_market(watcher_payload, NOW)


def test_half_even_fees():
    assert realized_fee({"kind": "relative", "fee": "0.0001"}, 105_000) == 10
    assert realized_fee({"kind": "relative", "fee": "0.0001"}, 115_000) == 12
