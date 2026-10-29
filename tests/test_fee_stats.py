import random

import pytest

from jmstats.fee_stats import monthly_maker_fees
from jmstats.solver import Result, ceiling

UNRESOLVED = Result(status="unresolved", reason="search")


def tx(index, denomination=1_000_000, participants=3):
    return [f"{index:064x}", denomination, participants, None, None, None]


def identified(total, gains=()):
    return Result(status="identified", low=total, high=total, gains=sorted(gains))


def ambiguous(low, high, gains=()):
    return Result(status="ambiguous", low=low, high=high, gains=sorted(gains))


def test_identified_month_is_exact_and_excludes_inconsistent_and_unanalyzed():
    candidates = [tx(i) for i in range(4)]
    analyses = {
        candidates[0][0]: identified(100, [40, 60]),
        candidates[1][0]: identified(300, [100, 200]),
        candidates[2][0]: Result(status="inconsistent"),
    }
    row = monthly_maker_fees({"2025-01": candidates}, analyses)["2025-01"]
    assert row["maker_fee_estimate_sats"] == row["maker_fee_exact_sats"] == 400
    assert row["maker_fee_low_sats"] == row["maker_fee_high_sats"] == 400
    assert row["maker_fee_floor_sats"] == 400
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


def test_imputation_uses_the_conditional_mean_inside_proven_bounds():
    candidates = [tx(i) for i in range(33)]
    analyses = {candidates[i][0]: identified((100, 1000, 5000)[i // 10]) for i in range(30)}
    analyses[candidates[30][0]] = ambiguous(500, 2000)  # Only the 1000s fit: 1000.
    analyses[candidates[31][0]] = ambiguous(6000, 8000)  # None fit: clip mean to 6000.
    analyses[candidates[32][0]] = UNRESOLVED  # Cell mean: 61000 / 30.
    row = monthly_maker_fees({"2025-01": candidates}, analyses)["2025-01"]
    mean = 61000 / 30
    assert row["maker_fee_exact_sats"] == 61000
    assert row["maker_fee_estimate_sats"] == round(61000 + 1000 + 6000 + mean)
    assert row["maker_fee_low_sats"] == round(61000 + 500 + 6000 + mean)
    assert row["maker_fee_high_sats"] == round(61000 + 2000 + 8000 + mean)
    assert row["maker_fee_floor_sats"] == 67500
    assert row["maker_fee_mean_sats"] == pytest.approx((68000 + mean) / 66, abs=0.01)
    assert row["maker_fee_partial"] is False


def test_sparse_cells_fall_back_to_trailing_months_then_nearest_decades():
    months = {key: [] for key in ("2024-10", "2025-01", "2025-02", "2025-03", "2025-04")}
    analyses = {}
    index = 0
    for key, count, total, denomination in (
        ("2024-10", 10, 8000, 1_000_000),
        ("2024-10", 30, 15_000, 75_000),  # Small rounds pay 10% rates.
        ("2025-01", 20, 2000, 1_000_000),
        ("2025-02", 10, 4000, 1_000_000),
    ):
        for _ in range(count):
            months[key].append(tx(index, denomination))
            analyses[months[key][-1][0]] = identified(total)
            index += 1
    for key, denomination in (
        ("2025-03", 1_000_000),
        ("2025-03", 100_000),
        ("2025-04", 1_000_000),
        ("2025-04", 10_000_000),
    ):
        months[key].append(tx(index, denomination))
        analyses[months[key][-1][0]] = UNRESOLVED
        index += 1
    rows = monthly_maker_fees(months, analyses)
    # March, 10^6: January to March has 30 rates, mean rate 1/750.
    # March, 10^5: no rates of its own; both neighbors (70 rates, mean 0.044).
    assert rows["2025-03"]["maker_fee_estimate_sats"] == round(2_000_000 / 750 + 8800)
    assert rows["2025-03"]["maker_fee_unresolved_sats"] == round(2_000_000 / 750 + 8800)
    # April, 10^6: February to April has only 10 rates, so all months of the decade.
    # April, 10^7: the nearest decade only (mean 1/500), never the 10% small rounds.
    assert rows["2025-04"]["maker_fee_estimate_sats"] == 4000 + 40_000
    assert rows["2025-03"]["maker_fee_exact_sats"] == 0


def test_without_identified_evidence_only_proven_floor_is_reported():
    candidates = [tx(1)]
    row = monthly_maker_fees({"2025-01": candidates}, {candidates[0][0]: ambiguous(10, 50)})[
        "2025-01"
    ]
    assert row["maker_fee_estimate_sats"] is None
    assert row["maker_fee_low_sats"] is row["maker_fee_high_sats"] is None
    assert (row["maker_fee_exact_sats"], row["maker_fee_floor_sats"]) == (0, 10)
    empty = monthly_maker_fees({"2025-02": []}, {})["2025-02"]
    assert empty["maker_fee_estimate_sats"] is empty["maker_fee_median_sats"] is None
    assert empty["maker_fee_partial"] is False


def test_fixed_gain_quantiles_weight_each_decade_by_its_maker_slots():
    small, large = tx(1, 100_000, 6), tx(2)
    analyses = {small[0]: ambiguous(50, 500, [50]), large[0]: identified(300, [100, 200])}
    row = monthly_maker_fees({"2025-01": [small, large]}, analyses)["2025-01"]
    # Weights: 100k decade 5 slots / 1 fixed gain, 1M decade 2 / 2. Unweighted: 100.
    assert (row["maker_fee_median_sats"], row["maker_fee_p90_sats"]) == (50, 200)
    assert row["maker_fee_median_percent"] == pytest.approx(0.05)
    assert (row["maker_fee_slots"], row["maker_fee_fixed_slots"]) == (7, 3)


def test_analysis_inconsistent_with_its_candidate_fails_closed():
    candidate = tx(1)
    with pytest.raises(ValueError, match="ceiling"):
        monthly_maker_fees({"2025-01": [candidate]}, {candidate[0]: identified(20_001)})


def simulated(seed):
    """Heterogeneous fee rates by decade; identification depends only on the decade.

    Ambiguous intervals are cells of a random grid independent of the true total,
    so the coarsening is at random, like spurious input subsets in real data.
    """
    rng = random.Random(seed)
    decades = {100_000: (3e-3, 0.9), 1_000_000: (3e-4, 0.6), 10_000_000: (3e-5, 0.3)}
    months, analyses, truth = {}, {}, {}
    for month in range(1, 7):
        key = f"2025-{month:02}"
        months[key], truth[key] = [], 0
        for index in range(300):
            denomination = rng.choice(list(decades))
            rate, identified_share = decades[denomination]
            participants = rng.randint(3, 10)
            makers, cap = participants - 1, (participants - 1) * ceiling(denomination)
            total = min(round(rng.expovariate(1 / rate) * makers * denomination), cap)
            candidate = tx(month * 1000 + index, denomination, participants)
            months[key].append(candidate)
            draw = rng.random()
            if draw < 0.05:
                analyses[candidate[0]] = Result(status="inconsistent")
                continue
            truth[key] += total
            if draw < 0.05 + 0.95 * identified_share:
                analyses[candidate[0]] = identified(total)
            elif draw < 0.97:
                width, offset = rng.randint(2, cap), rng.randrange(cap)
                low = offset + (total - offset) // width * width
                low, high = max(0, low), min(cap, low + width)
                if low == high:
                    low -= 1
                analyses[candidate[0]] = ambiguous(low, high)
            else:
                analyses[candidate[0]] = UNRESOLVED
    return months, analyses, truth


@pytest.mark.parametrize("seed", range(3))
def test_stratified_estimate_recovers_simulated_totals(seed):
    months, analyses, truth = simulated(seed)
    rows = monthly_maker_fees(months, analyses)
    estimate = sum(row["maker_fee_estimate_sats"] for row in rows.values())
    assert estimate == pytest.approx(sum(truth.values()), rel=0.05)
    for key, row in rows.items():
        assert row["maker_fee_floor_sats"] <= truth[key]
        assert row["maker_fee_exact_sats"] <= row["maker_fee_estimate_sats"]
    # The population is heterogeneous enough that unstratified imputation fails.
    rates = [
        a.low / ((c[2] - 1) * c[1])
        for c in (c for cs in months.values() for c in cs)
        if (a := analyses[c[0]]).status == "identified"
    ]
    pooled = sum(rates) / len(rates)
    naive = sum(
        a.low if a.status == "identified" else pooled * (c[2] - 1) * c[1]
        for c in (c for cs in months.values() for c in cs)
        if (a := analyses[c[0]]).status != "inconsistent"
    )
    assert naive > 1.5 * sum(truth.values())
