from copy import deepcopy

import pytest

from jmstats.archive import write_json
from jmstats.bond_history import load_bond_history, merge_bond_history
from jmstats.snapshots import reduce_bond_snapshot


def test_active_weight_reduction_dedupes_outpoints_not_numeric_values(watcher_payload):
    duplicate = deepcopy(watcher_payload["offers"][0])
    duplicate["counterparty"] = "sibling"
    watcher_payload["offers"].append(duplicate)
    first = reduce_bond_snapshot(watcher_payload)
    assert first["weights"] == [10_000]
    assert first["mapping_complete"]
    other = deepcopy(watcher_payload["fidelitybonds"][0])
    other["utxo"]["txid"] = "cd" * 32
    other["counterparty"] = "another"
    watcher_payload["fidelitybonds"].append(other)
    offer = deepcopy(duplicate)
    offer["counterparty"] = "another"
    offer["fidelity_bond_data"]["utxo_txid"] = "cd" * 32
    watcher_payload["offers"].append(offer)
    assert reduce_bond_snapshot(watcher_payload)["weights"] == [10_000, 10_000]


def test_matured_positive_weights_survive_and_no_fee_inputs_are_needed(watcher_payload):
    watcher_payload["fidelitybonds"][0]["locktime"] = 1
    for offer in watcher_payload["offers"]:
        for key in ("cjfee", "minsize", "maxsize", "txfee", "ordertype"):
            offer.pop(key)
    assert reduce_bond_snapshot(watcher_payload)["weights"] == [10_000]


def test_missing_verification_is_reported_and_known_invalid_is_excluded(watcher_payload):
    watcher_payload["offers"][0].pop("fidelity_bond_verified")
    watcher_payload["fidelitybonds"][0].pop("verification_valid")
    point = reduce_bond_snapshot(watcher_payload)
    assert point["weights"] == [10_000]
    assert point["verified_count"] == 0
    watcher_payload["offers"][0]["fidelity_bond_verification_stale"] = True
    assert reduce_bond_snapshot(watcher_payload)["weights"] == []


def test_missing_mapping_is_not_zero_competition(watcher_payload):
    watcher_payload["fidelitybonds"] = []
    point = reduce_bond_snapshot(watcher_payload)
    assert point["weights"] == []
    assert not point["mapping_complete"]


def test_absent_and_unknown_bond_history_do_not_trigger_reconstruction(tmp_path, watcher_payload):
    assert load_bond_history(tmp_path) == []
    point = reduce_bond_snapshot(watcher_payload)
    assert merge_bond_history(tmp_path, [point, point]) == 1
    assert load_bond_history(tmp_path)[0]["weights"] == [10_000]
    write_json(tmp_path / "bond-history.json", {"schema_version": 99, "kind": "unknown"})
    with pytest.raises(ValueError, match="Unknown"):
        merge_bond_history(tmp_path, [point])
