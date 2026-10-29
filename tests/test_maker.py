import math
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from conftest import model_history

from jmstats.inference import FeeRecord
from jmstats.maker import (
    FEE_GRID,
    MAX_BOND,
    MAX_REALISTIC_REL,
    MIN_BOND,
    YEAR_DAYS,
    fee_eligibility,
    inclusion_probability,
    optimize_maker,
    plausible_fee_evidence,
    selection_paths,
)
from jmstats.market import REL_GRID


def test_one_slot_default_weighted_allowance_matches_closed_form():
    paths = selection_paths((10, 20), 1)
    assert inclusion_probability(15, paths, 1) == pytest.approx(0.95 * 15 / (30 + 15))
    assert inclusion_probability(0, paths, 1) == 0


def test_without_replacement_fractional_slots_and_saturation():
    paths = selection_paths((10, 10, 10), 2)
    p_one = 5 / 35
    p_two = 1 - 30 / 35 * 20 / 25
    expected = 2 * 0.95 * 0.05 * p_one + 0.95**2 * p_two
    assert inclusion_probability(5, paths, 2) == pytest.approx(expected)
    assert inclusion_probability(5, paths, 1.5) == pytest.approx((0.95 * p_one + expected) / 2)
    assert inclusion_probability(10**20, paths, 2) == pytest.approx(1 - 0.05**2)
    values = [inclusion_probability(weight, paths, 2) for weight in (1, 10, 100, 1000)]
    assert values == sorted(values)
    with pytest.raises(ValueError, match="Insufficient"):
        selection_paths((100,), 2)


def test_simulation_is_deterministic():
    assert selection_paths((10, 20, 30), 2) == selection_paths((10, 20, 30), 2)


def sample(index, amount, ceiling):
    return FeeRecord(
        txid=f"{index:064x}",
        height=index,
        timestamp=1736000000,
        denomination=amount,
        makers=2,
        status="inferred",
        ceiling_sats=ceiling,
        maker_gains=[0, ceiling],
        network_fee=100,
    )


def test_relative_only_eligibility_treats_every_ceiling_as_a_rate():
    records = [sample(1, 500_000, 1001), sample(2, 5_000_000, 5001)]
    assert fee_eligibility(records, Decimal(".001")) == 1
    assert fee_eligibility(records, Decimal(".002")) == 0.5
    assert fee_eligibility(records, Decimal(".005")) == 0
    assert fee_eligibility([], Decimal(".001")) == 0
    # A realized fee is rounded half-even, so 1,000 sat at 1M sat proves only 0.09995%.
    rounding_edge = [sample(3, 1_000_000, 1000)]
    assert fee_eligibility(rounding_edge, Decimal(".001")) == 0
    assert fee_eligibility(rounding_edge, Decimal(".0005")) == 1


def test_implied_rates_of_one_percent_or_more_are_outliers_and_never_offered():
    assert plausible_fee_evidence(sample(1, 1_000_000, 10_000))
    assert not plausible_fee_evidence(sample(2, 1_000_000, 10_001))
    assert plausible_fee_evidence(sample(3, 20_000_000, 200_000))
    assert not plausible_fee_evidence(sample(4, 20_000_000, 200_001))
    assert max(FEE_GRID) == Decimal(".005")
    assert all(rate < MAX_REALISTIC_REL for rate in FEE_GRID)
    assert set(FEE_GRID) <= REL_GRID


def test_typical_round_uses_history_medians_and_fee_tradeoff(tmp_path):
    archive, records, points = model_history(tmp_path)
    result = optimize_maker(archive, records, points)
    assert result["basis"] == "historical_cjs"
    assert result["window_days"] == 90
    assert result["median_cj_sats"] == 1_000_000
    assert result["median_makers"] == 3
    assert result["activity_cjs"] == 200
    assert result["monthly_activity"] == pytest.approx(200 * YEAR_DAYS / (12 * 90))
    # Charging 0.1% to 20% earns more than 0.01% to all sampled takers.
    assert result["relative_fee"] == "0.001"
    assert result["relative_fee_percent"] == pytest.approx(0.1)
    assert "0.1%," in result["fee_description"]
    assert not any("ppm" in key for key in result)
    assert result["fee_eligibility"] == 0.2
    assert result["per_fill_sats"] == 1000
    assert "absolute_fee_sats" not in result
    assert result["fee_outliers"] == 0
    assert MIN_BOND < result["bond_sats"] < MAX_BOND
    assert result["boundary"] is None
    assert result["monthly_cjs"] == pytest.approx(
        result["monthly_activity"] * 0.2 * result["selection_probability"]
    )
    assert result["monthly_net_sats"] == pytest.approx(result["monthly_cjs"] * 1000)
    assert result["annual_roi_percent"] == pytest.approx(
        1200 * result["monthly_net_sats"] / result["bond_sats"]
    )
    assert result["windows"][1]["monthly_activity"] == pytest.approx(200 * YEAR_DAYS / (12 * 365))


def test_bond_curve_shows_superlinear_weight_favoring_larger_bonds(tmp_path):
    archive, records, points = model_history(tmp_path)
    result = optimize_maker(archive, records, points)
    curve = result["bond_curve"]
    optimal = [point for point in curve if point["optimal"]]
    assert [point["bond_sats"] for point in optimal] == [result["bond_sats"]]
    assert optimal[0]["annual_roi_percent"] == result["annual_roi_percent"]
    assert max(p["annual_roi_percent"] for p in curve) <= result["annual_roi_percent"] * 1.001
    below = [p["annual_roi_percent"] for p in curve if p["bond_sats"] <= result["bond_sats"]]
    assert below == sorted(below)
    assert len(below) > 1
    above = [p["annual_roi_percent"] for p in curve if p["bond_sats"] >= result["bond_sats"]]
    assert above == sorted(above, reverse=True)
    assert "bond^1.3" in result["bond_note"]


def test_outliers_are_excluded_from_relative_fee_evidence(tmp_path):
    # 3% implied rates (absolute fees on small CJs) would otherwise set the price.
    archive, records, points = model_history(tmp_path / "outliers", high_gain=30_000)
    result = optimize_maker(archive, records, points)
    assert result["fee_outliers"] == 40
    assert result["fee_samples"] == 160
    assert result["relative_fee"] == "0.0001"
    assert result["fee_eligibility"] == 1
    assert "40 CJs implying rates of 1% or more excluded" in result["assumptions"]
    archive, records, points = model_history(tmp_path / "high", high_gain=9_000)
    result = optimize_maker(archive, records, points)
    # 0.9% is realistic, but the public grid's highest rate below 1% is 0.5%.
    assert result["fee_outliers"] == 0
    assert result["relative_fee"] == "0.005"
    assert result["fee_eligibility"] == 0.2


def test_no_legacy_solver_or_underfilled_pool_fallback(tmp_path):
    archive, records, points = model_history(tmp_path)
    assert optimize_maker(archive, {}, points)["status"] == "unavailable"
    thin = [dict(point, weights=[100]) for point in points]
    result = optimize_maker(archive, records, thin)
    assert result["status"] == "unavailable"
    assert "monthly_net_sats" not in result
    assert result["median_cj_sats"] == 1_000_000


def test_insufficient_samples_and_gapped_windows_are_unavailable(tmp_path):
    archive, records, points = model_history(tmp_path)
    result = optimize_maker(archive, dict(list(records.items())[:99]), points)
    assert result["status"] == "unavailable"
    assert result["fee_samples"] == 99
    del archive.blocks[50]
    assert optimize_maker(archive, records, points)["status"] == "unavailable"


def test_future_bond_snapshot_cannot_affect_an_earlier_cj(tmp_path):
    archive, records, _ = model_history(tmp_path)
    # Put all observations in one block-time epoch, with one preceding snapshot.
    when = min(record.timestamp for record in records.values())
    for block in archive.blocks.values():
        if block[0] >= 4 and block[4]:
            block[2] = when
    point = {
        "as_of": datetime.fromtimestamp(when - 60, UTC).isoformat(),
        "weights": [100_000_000] * 10,
        "mapping_complete": True,
    }
    baseline = optimize_maker(archive, records, [point])
    future = dict(
        point, as_of=datetime.fromtimestamp(when + 1, UTC).isoformat(), weights=[10**15] * 10
    )
    result = optimize_maker(archive, records, [point, future])
    assert result["bond_sats"] == baseline["bond_sats"]
    assert result["annual_roi_percent"] == baseline["annual_roi_percent"]
    assert math.isfinite(result["annual_roi_percent"])
