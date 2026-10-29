import itertools
import random

import pytest

from jmstats.archive import write_json
from jmstats.inference import FeeHistory, infer_fee_record


def transaction(inputs, changes, *, amount=100_000, participants=3):
    outputs = [amount] * participants + changes
    return {
        "txid": "ab" * 32,
        "vin": [{"prevout": {"value": value}} for value in inputs],
        "vout": [{"value": value} for value in outputs],
        "fee": sum(inputs) - sum(outputs),
    }


def infer(raw, **kwargs):
    return infer_fee_record(
        raw,
        height=1,
        timestamp=1736000000,
        denomination=100_000,
        participants=len(raw["vout"]) // 2 + len(raw["vout"]) % 2,
        **kwargs,
    )


def brute_ceiling(inputs, changes, amount, network_fee):
    best = None
    for permutation in itertools.permutations(changes):
        net = [value - amount - change for value, change in zip(inputs, permutation, strict=True)]
        for taker, value in enumerate(net):
            makers = [-gain for i, gain in enumerate(net) if i != taker]
            if value >= network_fee and min(makers) >= 0:
                ceiling = max(makers)
                best = ceiling if best is None else min(best, ceiling)
    return best


def test_exact_matching_agrees_with_exhaustive_assignments():
    rng = random.Random(7)
    for participants in (3, 4, 5):
        for _ in range(20):
            changes = rng.sample(range(1000, 90000), participants)
            gains = [rng.randrange(1000) for _ in range(participants - 1)]
            inputs = [100_000 + changes[0] + 100 + sum(gains)] + [
                100_000 + change - gain for change, gain in zip(changes[1:], gains, strict=True)
            ]
            rng.shuffle(inputs)
            raw = transaction(inputs, changes, participants=participants)
            record = infer(raw)
            assert record.status == "inferred"
            assert record.ceiling_sats == brute_ceiling(inputs, changes, 100_000, 100)
            assert record.ceiling_sats <= max(gains)
            assert max(record.maker_gains) == record.ceiling_sats


def test_missing_taker_change_and_zero_ceiling_are_valid_evidence():
    raw = transaction([100_100, 101_000, 102_000], [1000, 2000])
    record = infer(raw)
    assert record.status == "inferred"
    assert record.ceiling_sats == 0
    assert record.maker_gains == [0, 0]


def test_unsupported_and_budget_exhaustion_are_unknown_not_zero():
    raw = transaction([101_310, 101_900, 102_800], [1000, 2000, 3000])
    assert infer(raw).ceiling_sats == 200
    exhausted = infer(raw, max_edge_visits=0)
    assert exhausted.status == "budget"
    assert exhausted.ceiling_sats is None
    assert exhausted.maker_gains is None
    raw["vin"].append({"prevout": {"value": 100}})
    raw["fee"] += 100
    assert infer(raw).status == "multi_input"


def test_negative_actual_maker_gain_is_an_explicit_identification_assumption():
    # An alternate nonnegative assignment can fit a transaction whose REAL maker
    # lost 100 sat. Thus feasibility alone cannot prove the assumption about roles.
    raw = transaction([101_010, 102_100, 102_900], [1000, 2000, 3000])
    result = infer(raw)
    assert result.status == "inferred"
    assert result.ceiling_sats == 990


def test_invalid_values_and_missing_prevouts_stay_unknown():
    raw = transaction([101_310, 101_900, 102_800], [1000, 2000, 3000])
    raw["fee"] += 1
    assert infer(raw).status == "invalid"
    raw["vin"][0]["prevout"] = None
    assert infer(raw).ceiling_sats is None


def test_no_feasible_role_assignment_and_large_rounds_remain_unknown():
    impossible = infer(transaction([104000, 105000, 97010], [1000, 2000, 3000]))
    assert impossible.status == "infeasible"
    assert impossible.ceiling_sats is None
    changes = list(range(1000, 1033))
    raw = transaction(
        [101100] + [100000 + change for change in changes[1:]], changes, participants=33
    )
    too_large = infer(raw)
    assert too_large.status == "too_large"
    assert too_large.ceiling_sats is None


def test_inference_history_is_versioned_idempotent_and_conservative(tmp_path):
    history = FeeHistory(tmp_path)
    assert history.records == {}
    assert not (tmp_path / "fee-inferences").exists()
    record = infer(transaction([101_310, 101_900, 102_800], [1000, 2000, 3000]))
    history.add(record)
    history.add(record)
    history.flush()
    assert FeeHistory(tmp_path).records == {record.txid: record}
    (tmp_path / "fee-inferences" / "state.json").unlink()
    with pytest.raises(ValueError, match="metadata is missing"):
        FeeHistory(tmp_path)
    write_json(tmp_path / "fee-inferences" / "state.json", {"schema_version": 99})
    with pytest.raises(ValueError, match="Unknown"):
        FeeHistory(tmp_path)
