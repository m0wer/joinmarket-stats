import math
import random

import pytest

from jmstats.fee_stats import monthly_maker_fees
from jmstats.solver import Result, ceiling

UNRESOLVED = Result(status="unresolved", reason="search")


def tx(index, denomination=1_000_000, participants=3, fee=None):
    return [f"{index:064x}", denomination, participants, fee, None, None]


def identified(total, gains=()):
    return Result(status="identified", low=total, high=total, gains=sorted(gains))


def ambiguous(low, high, gains=(), options=()):
    return Result(
        status="ambiguous",
        low=low,
        high=high,
        gains=sorted(gains),
        options=[sorted(o) for o in sorted(options)],
        multiplicity=[1] * len(options),
    )


def donors(start, count, gains, denomination=1_000_000):
    """Identified full-profile CoinJoins with the same maker gains."""
    candidates = [tx(start + i, denomination, len(gains) + 1) for i in range(count)]
    return candidates, {c[0]: identified(sum(gains), gains) for c in candidates}


def test_identified_month_is_exact_and_excludes_inconsistent_and_unanalyzed():
    candidates = [tx(0, fee=900), tx(1, fee=700), tx(2), tx(3)]
    analyses = {
        candidates[0][0]: identified(100, [40, 60]),
        candidates[1][0]: identified(300, [100, 200]),
        candidates[2][0]: Result(status="inconsistent"),
    }
    row = monthly_maker_fees({"2025-01": candidates}, analyses)["2025-01"]
    assert row["maker_fee_estimate_sats"] == row["maker_fee_exact_sats"] == 400
    assert row["maker_fee_interval_low_sats"] == row["maker_fee_interval_high_sats"] == 400
    assert row["maker_fee_floor_sats"] == 400
    assert row["maker_fee_unresolved_sats"] == 0
    assert row["maker_fee_mean_sats"] == 100
    assert row["maker_fee_mean_percent"] == pytest.approx(0.01)
    assert (row["maker_fee_median_sats"], row["maker_fee_p90_sats"]) == (60, 200)
    assert row["maker_fee_median_percent"] == pytest.approx(0.006)
    assert row["maker_fee_p90_percent"] == pytest.approx(0.02)
    assert (row["maker_fee_slots"], row["maker_fee_fixed_slots"]) == (4, 4)
    assert (row["maker_fee_identified"], row["maker_fee_unanalyzed"]) == (2, 1)
    assert (row["maker_fee_ambiguous"], row["maker_fee_unresolved"]) == (0, 0)
    assert row["inconsistent"] == 1
    assert row["maker_fee_partial"] is True
    # Taker cost (M + network fee) / E: 0.1% and 0.1%.
    assert row["taker_cost_median_percent"] == pytest.approx(0.1)
    assert row["taker_cost_p25_percent"] == row["taker_cost_p75_percent"] == pytest.approx(0.1)
    assert row["taker_cost_samples"] == 2


def test_unknown_makers_resample_donor_rates_within_proven_bounds():
    candidates, analyses = donors(0, 30, [1000, 1000])
    targets = [tx(100 + i, fee=1000) for i in range(4)]
    candidates += targets
    analyses[targets[0][0]] = UNRESOLVED  # Donor rates: 2 * 1000.
    analyses[targets[1][0]] = ambiguous(1200, 4000, [200])  # 200 + 1000.
    # No donor draw fits these bounds (2000), so totals are uniform within them.
    analyses[targets[2][0]] = ambiguous(500, 1500)
    analyses[targets[3][0]] = ambiguous(2500, 3000)
    row = monthly_maker_fees({"2025-01": candidates}, analyses)["2025-01"]
    certain = 30 * 2000 + 2000 + 1200
    assert certain + 500 + 2500 < row["maker_fee_interval_low_sats"]
    assert row["maker_fee_interval_high_sats"] < certain + 1500 + 3000
    assert row["maker_fee_estimate_sats"] == pytest.approx(certain + 1000 + 2750, abs=100)
    assert row["maker_fee_unresolved_sats"] == 2000
    assert row["maker_fee_floor_sats"] == 30 * 2000 + 500 + 2500 + 1200
    assert row["taker_cost_samples"] == 4
    assert row["taker_cost_p25_percent"] == pytest.approx(0.22)  # (1200 + 1000) / 1M


def test_options_are_weighted_by_maker_gain_likelihood():
    candidates, analyses = donors(0, 30, [1000, 1000])
    target = tx(100)
    candidates.append(target)
    # One option matches the donors' gains; the other is far outside them.
    analyses[target[0]] = ambiguous(2000, 9300, options=[[1000, 1000], [300, 9000]])
    row = monthly_maker_fees({"2025-01": candidates}, analyses)["2025-01"]
    assert row["maker_fee_estimate_sats"] == row["maker_fee_interval_high_sats"] == 62_000


def test_fixed_gains_select_compatible_donor_coinjoins():
    low, high = donors(0, 30, [100, 100, 100])
    more, rest = donors(30, 30, [5000, 5000, 5000])
    candidates, analyses = low + more, {**high, **rest}
    known, unknown = tx(100, participants=4), tx(101, participants=4)
    candidates += [known, unknown]
    analyses[known[0]] = ambiguous(5000, 25_000, [5000])
    analyses[unknown[0]] = UNRESOLVED
    exact = 30 * 300 + 30 * 15_000
    row = monthly_maker_fees({"2025-01": candidates}, analyses)["2025-01"]
    # One known maker at 5000 sats rules out the low-fee CoinJoins.
    assert row["maker_fee_interval_low_sats"] == exact + 15_000 + 300
    assert row["maker_fee_interval_high_sats"] == exact + 15_000 + 15_000
    known_only = monthly_maker_fees(
        {"2025-01": candidates[:-1]}, {k: v for k, v in analyses.items() if k != unknown[0]}
    )["2025-01"]
    assert known_only["maker_fee_interval_low_sats"] == exact + 15_000
    assert known_only["maker_fee_interval_high_sats"] == exact + 15_000


def test_sparse_cells_fall_back_to_trailing_months_then_nearest_decades():
    keys = ("2024-10", "2025-01", "2025-02", "2025-03", "2025-04", "2025-05", "2025-06")
    months, analyses = {key: [] for key in keys}, {}
    for key, start, count, gain, denomination in (
        ("2024-10", 0, 10, 4000, 1_000_000),
        ("2024-10", 100, 30, 3000, 75_000),  # Small rounds pay 4% rates.
        ("2025-01", 200, 20, 1000, 1_000_000),
        ("2025-02", 300, 10, 1000, 1_000_000),
    ):
        candidates, results = donors(start, count, [gain, gain], denomination)
        months[key] += candidates
        analyses.update(results)
    for key, index, denomination in (
        ("2025-03", 900, 1_000_000),
        ("2025-04", 901, 1_000_000),
        ("2025-05", 902, 10_000_000),
        ("2025-06", 903, 100_000),
    ):
        months[key].append(tx(index, denomination))
        analyses[months[key][-1][0]] = UNRESOLVED
    rows = monthly_maker_fees(months, analyses)
    # March, 10^6: January to March has 30 donors at rate 1/1000.
    assert rows["2025-03"]["maker_fee_interval_low_sats"] == 2000
    assert rows["2025-03"]["maker_fee_interval_high_sats"] == 2000
    assert rows["2025-03"]["maker_fee_exact_sats"] == 0
    # April, 10^6: February to April has only 10 donors, so all months of the decade.
    assert rows["2025-04"]["maker_fee_interval_low_sats"] == 2000
    assert rows["2025-04"]["maker_fee_interval_high_sats"] == 8000
    # May, 10^7: the nearest decade (rates 1/1000, 4/1000), never the 4% rounds.
    assert rows["2025-05"]["maker_fee_interval_low_sats"] == 20_000
    assert rows["2025-05"]["maker_fee_interval_high_sats"] == 80_000
    # June, 10^5: both neighboring decades, whose gains do not grow with E (slope
    # clipped to 0), so their gains in sats.
    assert rows["2025-06"]["maker_fee_interval_low_sats"] == 2000
    assert rows["2025-06"]["maker_fee_interval_high_sats"] == 8000


@pytest.mark.parametrize(("small", "large", "expected"), [(500, 500, 1000), (500, 4500, 9000)])
def test_gains_scale_with_the_denomination_as_the_donors_do(small, large, expected):
    # Absolute fees (gains flat in E) or relative fees (proportional to E).
    candidates, analyses = donors(0, 20, [small, small], 1_000_000)
    more, results = donors(20, 20, [large, large], 9_000_000)
    target = tx(100, 9_000_000)
    candidates += more + [target]
    analyses |= results | {target[0]: UNRESOLVED}
    row = monthly_maker_fees({"2025-01": candidates}, analyses)["2025-01"]
    exact = 20 * 2 * (small + large)
    assert row["maker_fee_interval_low_sats"] - exact == pytest.approx(expected, rel=0.05)
    assert row["maker_fee_interval_high_sats"] - exact == pytest.approx(expected, rel=0.05)


def test_without_identified_evidence_only_proven_floor_is_reported():
    candidates = [tx(1)]
    row = monthly_maker_fees({"2025-01": candidates}, {candidates[0][0]: ambiguous(10, 50)})[
        "2025-01"
    ]
    assert row["maker_fee_estimate_sats"] is None
    assert row["maker_fee_interval_low_sats"] is row["maker_fee_interval_high_sats"] is None
    assert row["taker_cost_median_percent"] is None
    assert (row["maker_fee_exact_sats"], row["maker_fee_floor_sats"]) == (0, 10)
    empty = monthly_maker_fees({"2025-02": []}, {})["2025-02"]
    assert empty["maker_fee_estimate_sats"] is empty["maker_fee_median_sats"] is None
    assert empty["maker_fee_partial"] is False


def test_analysis_inconsistent_with_its_candidate_fails_closed():
    candidate = tx(1)
    with pytest.raises(ValueError, match="ceiling"):
        monthly_maker_fees({"2025-01": [candidate]}, {candidate[0]: identified(20_001)})


def test_draws_are_reproducible_and_seeded_per_month():
    first, results = donors(0, 20, [100, 900])
    second, more = donors(20, 20, [3000, 50])
    targets = [tx(100 + i, fee=500) for i in range(10)]
    analyses = {**results, **more, **{t[0]: UNRESOLVED for t in targets}}
    months = {"2025-01": first + second + targets}
    rows = monthly_maker_fees(months, analyses)
    assert rows == monthly_maker_fees(months, analyses)
    assert (
        rows["2025-01"]["maker_fee_interval_low_sats"]
        < rows["2025-01"]["maker_fee_interval_high_sats"]
    )
    later = monthly_maker_fees({**months, "2025-02": [tx(999)]}, analyses)
    assert later["2025-01"] == rows["2025-01"]


def simulated(seed, months=6, size=300):
    """CoinJoins whose makers share a taker-level fee effect (half the log variance).

    Identification depends only on the decade, like spurious input subsets that the
    amounts fix independently of the fees. Ambiguous transactions know a random
    subset of their makers, and half of them list the true and spurious options.
    """
    rng = random.Random(seed)
    decades = {100_000: (-7.0, 0.7), 1_000_000: (-8.5, 0.5), 10_000_000: (-10.0, 0.3)}
    candidates, analyses, truth = {}, {}, {}  # truth: total maker fee per txid
    for month in range(1, months + 1):
        key = f"2025-{month:02}"
        candidates[key] = []
        for index in range(size):
            denomination = rng.choice(list(decades))
            location, identified_share = decades[denomination]
            participants, top = rng.randint(3, 8), ceiling(denomination)
            effect = location + rng.gauss(0, 1)
            gains = sorted(
                0
                if rng.random() < 0.05
                else min(round(math.exp(effect + rng.gauss(0, 1)) * denomination), top)
                for _ in range(participants - 1)
            )
            total = sum(gains)
            candidate = tx(month * 10_000 + index, denomination, participants, fee=500)
            candidates[key].append(candidate)
            draw = rng.random()
            if draw < 0.05:
                analyses[candidate[0]] = Result(status="inconsistent")
                continue
            truth[candidate[0]] = total
            if draw < 0.05 + 0.95 * identified_share:
                analyses[candidate[0]] = identified(total, gains)
                continue
            if draw > 0.97:
                analyses[candidate[0]] = UNRESOLVED
                continue
            shuffled = rng.sample(gains, len(gains))
            known = sorted(shuffled[: rng.randrange(len(gains))])
            unknown = sorted(shuffled[len(known) :])
            fixed, slots = sum(known), len(unknown)
            if rng.random() < 0.5:
                spurious = [
                    sorted(rng.randint(0, top) for _ in range(slots))
                    for _ in range(rng.randint(1, 3))
                ]
                options = sorted({tuple(o) for o in [unknown, *spurious]})
                totals = [fixed + sum(o) for o in options]
                if min(totals) < max(totals):
                    analyses[candidate[0]] = ambiguous(min(totals), max(totals), known, options)
                    continue
            # A cell of a random grid, independent of the total (coarsening at random).
            span = slots * top
            width, offset = rng.randint(2, span), rng.randrange(span)
            low = offset + (total - fixed - offset) // width * width
            low, high = max(0, low), min(span, low + width)
            if low == high:
                low -= 1
            analyses[candidate[0]] = ambiguous(fixed + low, fixed + high, known)
    return candidates, analyses, truth


def monthly_truth(months, truth):
    return {k: sum(truth.get(c[0], 0) for c in cs) for k, cs in months.items()}


def test_simulated_totals_are_recovered_with_honest_intervals():
    covered = months_seen = 0
    for seed in range(4):
        months, analyses, truth = simulated(seed)
        rows = monthly_maker_fees(months, analyses)
        totals = monthly_truth(months, truth)
        estimate = sum(row["maker_fee_estimate_sats"] for row in rows.values())
        assert estimate == pytest.approx(sum(totals.values()), rel=0.05)
        for key, row in rows.items():
            low, high = row["maker_fee_interval_low_sats"], row["maker_fee_interval_high_sats"]
            assert row["maker_fee_floor_sats"] <= totals[key]
            assert row["maker_fee_exact_sats"] <= low <= row["maker_fee_estimate_sats"] <= high
            covered += low <= totals[key] <= high
            months_seen += 1
    # The 90% interval covers about 80% to 88% of months in long simulations
    # (heavy tails and order statistics of 100 draws); a regression falls far below.
    assert covered >= 0.7 * months_seen


def test_known_makers_and_options_narrow_the_interval():
    months, analyses, _ = simulated(7)
    stripped = {
        txid: Result(status="ambiguous", low=r.low, high=r.high) if r.status == "ambiguous" else r
        for txid, r in analyses.items()
    }

    def width(results):
        rows = monthly_maker_fees(months, results).values()
        return sum(
            r["maker_fee_interval_high_sats"] - r["maker_fee_interval_low_sats"] for r in rows
        )

    assert width(analyses) < 0.8 * width(stripped)


def test_maker_count_effects_keep_three_party_fees_from_many_maker_rounds():
    few, results = donors(0, 20, [3000, 3000])
    many, more = donors(20, 20, [300] * 8)
    target = tx(100, participants=9)
    candidates = few + many + [target]
    analyses = results | more | {target[0]: UNRESOLVED}
    row = monthly_maker_fees({"2025-01": candidates}, analyses)["2025-01"]
    exact = 20 * 6000 + 20 * 2400
    # Many-maker donors give 8 * 300. Three-party donors, adjusted by the ridge
    # (20 donors per bucket against a prior of 10) to a third of the log gap, give
    # 8 * 300 * (3000 / 300) ** (1 / 3) instead of 8 * 3000.
    assert row["maker_fee_interval_low_sats"] - exact == 2400
    high = row["maker_fee_interval_high_sats"] - exact
    assert high == pytest.approx(2400 * 10 ** (1 / 3), rel=0.01)
