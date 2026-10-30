import itertools
import random
from collections import Counter

import pytest

from jmstats import solver
from jmstats.solver import Amounts, Result, analyze


def brute_force(a: Amounts):
    """Reference: every input-to-participant assignment, every taker, no pruning.

    Returns (low, high, fixed gains, options with multiplicity) or None.
    """
    owners = a.changes + [0] * (a.participants - len(a.changes))
    targets = [a.denomination + change for change in owners]
    decompositions = []
    for assignment in itertools.product(range(a.participants), repeat=len(a.inputs)):
        if len(set(assignment)) != a.participants:
            continue
        gains = [targets[s] for s in range(a.participants)]
        for value, slot in zip(a.inputs, assignment, strict=True):
            gains[slot] -= value
        for taker in range(a.participants):
            makers = [s for s in range(a.participants) if s != taker]
            if all(0 <= gains[s] <= a.ceiling for s in makers):
                decompositions.append({s: gains[s] for s in makers})
    if not decompositions:
        return None
    fixed = {
        s
        for s in range(a.participants)
        if all(s in d for d in decompositions) and len({d[s] for d in decompositions}) == 1
    }
    totals = [sum(d.values()) for d in decompositions]
    options = Counter(
        tuple(sorted(g for s, g in d.items() if s not in fixed)) for d in decompositions
    )
    if () in options or len(options) > solver.OPTION_LIMIT:
        options = Counter()
    return (
        min(totals),
        max(totals),
        sorted(decompositions[0][s] for s in fixed),
        sorted((list(o), c) for o, c in options.items()),
    )


def observed(result: Result):
    options = sorted(zip(result.options, result.multiplicity, strict=True))
    return (result.low, result.high, result.gains, [(o, c) for o, c in options])


def joinmarket(rng, n, denomination=100_000, changeless=False):
    """A random model-consistent transaction on a coarse grid that invites ambiguity."""
    inputs, changes = [], []
    taker_fee = rng.choice([0, 1000, 3000])
    gains = [rng.choice([0, 1000, 2000, 5000]) for _ in range(n - 1)]
    for gain in gains:
        change = rng.choice([1000, 2000, 5000, 10_000, 50_000])
        changes.append(change)
        total = denomination + change - gain
        split = rng.choice([0, 0, total // 2])
        inputs += [total - split, split] if split else [total]
    taker_change = 0 if changeless else rng.choice([1000, 5000, 20_000])
    if not changeless:
        changes.append(taker_change)
    taker_total = denomination + taker_change + sum(gains) + taker_fee
    split = rng.choice([0, 3000, taker_total // 3])
    inputs += [taker_total - split, split] if split else [taker_total]
    rng.shuffle(inputs)
    outputs = [denomination] * n + changes
    rng.shuffle(outputs)
    return Amounts(inputs=inputs, outputs=outputs, denomination=denomination, participants=n)


@pytest.mark.parametrize("seed", range(60))
def test_exact_bounds_and_fixed_gains_match_brute_force(seed):
    rng = random.Random(seed)
    amounts = joinmarket(rng, rng.choice([3, 4]), changeless=seed % 3 == 0)
    expected = brute_force(amounts)
    result = analyze(amounts)
    assert expected is not None
    assert observed(result) == expected
    assert result.status == ("identified" if expected[0] == expected[1] else "ambiguous")


@pytest.mark.parametrize("seed", range(30))
def test_random_shapes_agree_with_brute_force_including_inconsistent(seed):
    rng = random.Random(seed)
    n = 3
    outputs = [100_000] * n + [rng.randrange(1, 60_000) for _ in range(n - rng.choice([0, 1]))]
    inputs = [rng.randrange(1, 80_000) for _ in range(rng.choice([3, 4, 5]))]
    inputs.append(sum(outputs) - sum(inputs) + rng.randrange(0, 3000) + 100_000)
    try:
        amounts = Amounts(inputs=inputs, outputs=outputs, denomination=100_000, participants=n)
    except ValueError:
        pytest.skip("Generated outputs exceed inputs")
    expected = brute_force(amounts)
    result = analyze(amounts)
    if expected is None:
        assert result.status == "inconsistent"
        assert result.low is result.high is None
    else:
        assert observed(result) == expected


def test_multi_input_taker_with_change_is_identified_when_unique():
    amounts = Amounts(
        inputs=[60_000, 50_050, 100_990, 101_980],
        outputs=[100_000] * 3 + [10_000, 1000, 2000],
        denomination=100_000,
        participants=3,
    )
    result = analyze(amounts)
    assert brute_force(amounts) == observed(result)
    assert amounts.network_fee == 20


def test_dust_batch_payout_is_inconsistent_not_zero():
    amounts = Amounts(
        inputs=[294, 294, 294, 294, 562_604],
        outputs=[75_000] * 4 + [0, 546, 3600, 258_908],
        denomination=75_000,
        participants=4,
    )
    result = analyze(amounts)
    assert result == Result(status="inconsistent")


def test_solved_zero_fee_is_a_real_zero():
    amounts = Amounts(
        inputs=[101_100, 150_000, 1_100_000],
        outputs=[100_000] * 3 + [1000, 50_000, 1_000_000],
        denomination=100_000,
        participants=3,
    )
    result = analyze(amounts)
    assert result.status == "identified"
    assert result.low == result.high == 0
    assert result.gains == [0, 0]


def test_thirty_participants_without_taker_change():
    n, denomination = 30, 10_000_000
    changes = [index * 1_000_000 + 1000 for index in range(1, n)]
    gains = list(range(1, n))
    amounts = Amounts(
        inputs=[denomination + 1000 + sum(gains)]
        + [denomination + change - gain for change, gain in zip(changes, gains, strict=True)],
        outputs=[denomination] * n + changes,
        denomination=denomination,
        participants=n,
    )
    result = analyze(amounts)
    assert result.status == "identified"
    assert result.low == sum(gains)
    assert result.gains == gains


def test_budget_overrun_is_unresolved_and_deterministic(monkeypatch):
    amounts = joinmarket(random.Random(3), 4)
    assert analyze(amounts) == analyze(amounts)
    monkeypatch.setattr(solver, "STEP_LIMIT", 5)
    assert analyze(amounts) == Result(status="unresolved", reason="candidates")
    monkeypatch.setattr(solver, "STEP_LIMIT", 10_000_000)
    monkeypatch.setattr(solver, "CANDIDATE_LIMIT", 0)
    assert analyze(amounts).reason == "candidates"


def test_fixed_gain_enumeration_budget_keeps_bounds(monkeypatch):
    amounts = joinmarket(random.Random(7), 4)
    full = analyze(amounts)
    monkeypatch.setattr(solver, "DECOMPOSITION_LIMIT", 0)
    limited = analyze(amounts)
    assert (limited.status, limited.low, limited.high) == (full.status, full.low, full.high)
    assert limited.gains == limited.options == limited.multiplicity == []


def test_option_limit_keeps_bounds_and_fixed_gains(monkeypatch):
    amounts = next(
        a
        for seed in range(100)
        if len(analyze(a := joinmarket(random.Random(seed), 4)).options) > 1
    )
    full = analyze(amounts)
    monkeypatch.setattr(solver, "OPTION_LIMIT", len(full.options) - 1)
    limited = analyze(amounts)
    assert limited.model_dump(exclude={"options", "multiplicity"}) == full.model_dump(
        exclude={"options", "multiplicity"}
    )
    assert limited.options == limited.multiplicity == []


@pytest.mark.parametrize(
    "fields",
    [
        {"status": "identified", "low": 1, "high": 2},
        {"status": "ambiguous", "low": 2, "high": 2},
        {"status": "unresolved"},
        {"status": "inconsistent", "low": 0, "high": 0},
        {"status": "inconsistent", "gains": [1]},
        {"status": "ambiguous", "low": 1, "high": 3, "gains": [2, 1]},
        {"status": "unresolved", "reason": "search", "options": [[1]], "multiplicity": [1]},
        {"status": "ambiguous", "low": 1, "high": 3, "options": [[1], [3]], "multiplicity": [1]},
        {"status": "ambiguous", "low": 1, "high": 3, "options": [[1]], "multiplicity": [0]},
        {"status": "ambiguous", "low": 1, "high": 3, "options": [[3], [1]], "multiplicity": [1, 1]},
        {"status": "ambiguous", "low": 1, "high": 3, "options": [[1], [1]], "multiplicity": [1, 1]},
        {"status": "ambiguous", "low": 1, "high": 3, "options": [[2, 1]], "multiplicity": [1]},
        {
            "status": "ambiguous",
            "low": 1,
            "high": 3,
            "options": [[1], [1, 2]],
            "multiplicity": [1, 1],
        },
        {"status": "identified", "low": 1, "high": 1, "options": [[]], "multiplicity": [1]},
    ],
)
def test_invalid_results_fail_closed(fields):
    with pytest.raises(ValueError):
        Result(**fields)


def test_stored_result_checked_against_candidate():
    with pytest.raises(ValueError, match="ceiling"):
        Result(status="identified", low=20_001, high=20_001).check(100_000, 3)
    with pytest.raises(ValueError, match="total"):
        Result(status="identified", low=5, high=5, gains=[1, 2]).check(100_000, 3)
    with pytest.raises(ValueError, match="exceed the fee bound"):
        Result(status="ambiguous", low=2, high=9, gains=[3]).check(100_000, 3)
    options = {"options": [[1, 2], [1, 5]], "multiplicity": [2, 1]}
    Result(status="ambiguous", low=3, high=6, **options).check(100_000, 3)
    for bad in (
        Result(status="ambiguous", low=3, high=7, **options),  # does not span the bounds
        Result(status="ambiguous", low=3, high=6, **options, gains=[0]),  # too many makers
        Result(
            status="identified", low=10_002, high=10_002, options=[[1, 10_001]], multiplicity=[1]
        ),
    ):
        with pytest.raises(ValueError, match="options"):
            bad.check(100_000, 3)


def test_amounts_reject_mismatched_shapes():
    with pytest.raises(ValueError, match="exceed"):
        Amounts(inputs=[1, 1, 1], outputs=[1, 1, 1, 1], denomination=1, participants=3)
    with pytest.raises(ValueError, match="shape"):
        Amounts(inputs=[9] * 3, outputs=[2, 2, 2, 2], denomination=2, participants=3)
