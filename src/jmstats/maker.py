"""Conservative typical-round ROI, calibrated only from historical observations.

Fees come from price-prior-free CJ inference. Competition comes exclusively
from time-matched historical bond weights. No current offers, advertised fees,
or offer-size limits enter this calculation.
"""

from __future__ import annotations

import hashlib
import math
import random
from bisect import bisect_right
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from functools import lru_cache
from statistics import median

from .archive import Archive
from .inference import FeeRecord
from .market import REL_GRID, realized_fee

MIN_BOND = 1_000_000  # Approved 0.01 to 1,000 BTC, one-year lock.
MAX_BOND = 1000 * 100_000_000
MIN_EVIDENCE = 100
TRIALS = 128
YEAR_DAYS = 365.2425
# Realistic relative fees are below 1%. Higher implied rates mostly come from
# absolute fees on small CJs, or from transactions that only look like CJs.
MAX_REALISTIC_REL = Decimal(".01")
# Public grid rates (NG takers ignore off-grid offers by default).
FEE_GRID = tuple(sorted(rate for rate in REL_GRID if rate < MAX_REALISTIC_REL))
CURVE_BONDS = tuple(
    round(btc * 100_000_000) for btc in (0.1, 0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500)
)


def bond_weight(sats: int) -> int:
    return int((sats * math.expm1(0.015)) ** 1.3)


def selection_paths(weights: tuple[int, ...], slots: int, trials=TRIALS) -> tuple:
    """Others-only weighted draws, conditional on the new maker surviving."""
    if slots < 1 or len(weights) < slots or min(weights, default=0) <= 0:
        raise ValueError("Insufficient known positive bond competition")
    if len(set(weights)) == 1:
        return (tuple((len(weights) - i) * weights[0] for i in range(slots)),)
    if slots == 1:
        return ((sum(weights),),)
    seed = int.from_bytes(hashlib.sha256(repr((weights, slots)).encode()).digest()[:8], "big")
    rng = random.Random(seed)
    result = []
    for _ in range(trials):
        remaining, path = list(weights), []
        total = sum(remaining)
        for _ in range(slots):
            path.append(total)
            draw = rng.randrange(total)
            for index, other in enumerate(remaining):
                draw -= other
                if draw < 0:
                    total -= remaining.pop(index)
                    break
        result.append(tuple(path))
    return tuple(result)


@lru_cache(maxsize=64)
def _weighted_slot_probabilities(slots: int) -> tuple[float, ...]:
    # Without advertised fee identities, model the default 5% allowance as
    # slots outside the bonded pool. Integrate this binomial exactly.
    return tuple(math.comb(slots, n) * 0.95**n * 0.05 ** (slots - n) for n in range(slots + 1))


def inclusion_probability(weight: int, paths: tuple, slots: float) -> float:
    if weight <= 0:
        return 0.0

    def integer_slots(count: int) -> float:
        probabilities = _weighted_slot_probabilities(count)
        included = 0.0
        for path in paths:
            survival, mixture = 1.0, probabilities[0]
            for i in range(count):
                survival *= path[i] / (path[i] + weight)
                mixture += probabilities[i + 1] * survival
            included += 1 - mixture
        return min(1.0, max(0.0, included / len(paths)))

    low, high = math.floor(slots), math.ceil(slots)
    if low == high:
        return integer_slots(low)
    fraction = slots - low
    return (1 - fraction) * integer_slots(low) + fraction * integer_slots(high)


def implied_rate_at_least(sample: FeeRecord, relative: Decimal) -> bool:
    """Approximation: every historical maker fee is treated as relative.

    A net gain g at size a then implies a taker cap >= (g - 0.5)/a, allowing
    for half-even rounding of the realized fee.
    """
    return Decimal(sample.ceiling_sats) - Decimal(".5") >= relative * sample.denomination


def plausible_fee_evidence(sample: FeeRecord) -> bool:
    """Exclude implied relative rates of 1% or more as outliers.

    These inflate relative-only estimates: absolute fees on small CJs look
    like high rates, and some equal-output non-CoinJoins show gains of up to
    twice the CJ amount.
    """
    return not implied_rate_at_least(sample, MAX_REALISTIC_REL)


def fee_eligibility(samples: list[FeeRecord], relative: Decimal) -> float:
    """Share of CJs whose taker provably accepted at least this relative rate."""
    if not samples:
        return 0.0
    return sum(implied_rate_at_least(sample, relative) for sample in samples) / len(samples)


def _best_fee(samples: list[FeeRecord], size: int) -> dict | None:
    best = None
    for relative in FEE_GRID:
        q = fee_eligibility(samples, relative)
        per_fill = realized_fee({"kind": "relative", "fee": str(relative)}, size)
        score = q * per_fill
        if best is None or score > best["score"]:
            best = {
                "relative_fee": str(relative),
                "relative_fee_percent": float(relative * 100),
                "per_fill_sats": per_fill,
                "fee_eligibility": q,
                "score": score,
            }
    return best if best and best["score"] > 0 else None


def _percent(rate: Decimal) -> str:
    return f"{(rate * 100).normalize():f}%"


def _date(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%d")


def _one_window(
    archive: Archive,
    days: int,
    fee_records: dict[str, FeeRecord],
    bond_points: list[dict],
    paths_cache: dict,
    probability_cache: dict,
) -> dict:
    ordered = sorted(archive.blocks.values(), key=lambda b: b[0])
    end = max(b[2] for b in ordered) - 7200
    start = end - days * 86400
    result = {
        "status": "unavailable",
        "window_days": days,
        "period_start": _date(start),
        "period_end": _date(end),
    }
    spans = []
    previous = None
    for block in ordered:
        if previous is None or block[0] != previous + 1:
            spans.append([block[2], block[2]])
        else:
            spans[-1][0] = min(spans[-1][0], block[2])
            spans[-1][1] = max(spans[-1][1], block[2])
        previous = block[0]
    rows = [(b[2], c) for b in ordered if start < b[2] <= end for c in b[4] if c[1] >= 75_000]
    if not rows or not any(a <= start - 7200 and z >= end + 7200 for a, z in spans):
        return dict(result, reason="The activity window is not fully scanned.")
    size = round(median(c[1] for _, c in rows))
    makers = float(median(c[2] - 1 for _, c in rows))
    monthly = len(rows) * YEAR_DAYS / (12 * days)
    result.update(
        activity_cjs=len(rows), median_cj_sats=size, median_makers=makers, monthly_activity=monthly
    )
    samples, dates, outcomes, outliers = [], [], Counter(), 0
    for timestamp, candidate in rows:
        record = fee_records.get(candidate[0])
        if (
            record is None
            or record.denomination != candidate[1]
            or record.makers != candidate[2] - 1
        ):
            continue
        outcomes[record.status] += 1
        if record.status != "inferred":
            continue
        if not plausible_fee_evidence(record):
            outliers += 1
            continue
        samples.append(record)
        dates.append(timestamp)
    result.update(
        fee_samples=len(samples),
        fee_outliers=outliers,
        inference_outcomes=dict(outcomes),
        fee_sample_start=_date(min(dates)) if dates else None,
        fee_sample_end=_date(max(dates)) if dates else None,
    )
    points = sorted(
        (
            (datetime.fromisoformat(p["as_of"].replace("Z", "+00:00")).timestamp(), p)
            for p in bond_points
        ),
        key=lambda pair: pair[0],
    )
    points = [(timestamp, p) for timestamp, p in points if timestamp <= end]
    if not points:
        return dict(result, reason="Historical bond-weight observations are unavailable.")
    times = [timestamp for timestamp, _ in points]
    gaps = [b - a for a, b in zip(times, times[1:], strict=False) if b > a]
    tolerance = max(7200, 2 * median(gaps)) if gaps else 7200
    groups, matched_dates, in_span = Counter(), [], 0
    for timestamp, _ in rows:
        if times[0] <= timestamp <= times[-1] + tolerance:
            in_span += 1
        index = bisect_right(times, timestamp) - 1
        if index < 0 or timestamp - times[index] > tolerance:
            continue
        point = points[index][1]
        weights = tuple(point["weights"])
        if (
            not point["mapping_complete"]
            or len(weights) < math.ceil(makers)
            or min(weights, default=0) <= 0
        ):
            continue
        groups[weights] += 1
        matched_dates.append(timestamp)
    matched = sum(groups.values())
    result.update(
        bond_matches=matched,
        bond_covered_cjs=in_span,
        bond_match_rate=matched / in_span if in_span else 0,
        bond_sample_start=_date(min(matched_dates)) if matched_dates else None,
        bond_sample_end=_date(max(matched_dates)) if matched_dates else None,
        max_snapshot_age_hours=tolerance / 3600,
    )
    if len(samples) < MIN_EVIDENCE or matched < MIN_EVIDENCE:
        return dict(result, reason=f"At least {MIN_EVIDENCE} fee and bond observations are needed.")
    if result["bond_match_rate"] < 0.8:
        return dict(result, reason="Historical bond snapshots have too many coverage gaps.")
    fee = _best_fee(samples, size)
    if fee is None:
        return dict(result, reason="No positive fee ceiling is identified in this sample.")
    cohorts = []
    for weights, count in groups.items():
        key = weights, math.ceil(makers)
        if key not in paths_cache:
            paths_cache[key] = selection_paths(*key)
        cohorts.append((weights, count, paths_cache[key]))
    evaluated = {}

    def evaluate(bond: int):
        if bond in evaluated:
            return
        included = 0.0
        weight = bond_weight(bond)
        for weights, count, paths in cohorts:
            key = weights, makers, bond
            if key not in probability_cache:
                probability_cache[key] = inclusion_probability(weight, paths, makers)
            included += count * probability_cache[key]
        probability = included / matched
        fills = monthly * fee["fee_eligibility"] * probability
        net = fills * fee["per_fill_sats"]
        evaluated[bond] = (12 * net / bond, probability, fills, net)

    grid = sorted(
        {round(MIN_BOND * (MAX_BOND / MIN_BOND) ** (i / 63)) for i in range(64)}
        | {MIN_BOND, MAX_BOND}
    )
    for bond in grid:
        evaluate(bond)
    best = max(grid, key=lambda b: evaluated[b][0])
    position = grid.index(best)
    low, high = grid[max(0, position - 1)], grid[min(len(grid) - 1, position + 1)]
    for i in range(1, 20):
        evaluate(max(MIN_BOND, min(MAX_BOND, round(low * (high / low) ** (i / 20)))))
    best = max(evaluated, key=lambda b: evaluated[b][0])
    # Do not imply sat-level precision in a statistical capital recommendation.
    best = max(MIN_BOND, min(MAX_BOND, round(best / 1_000_000) * 1_000_000))
    evaluate(best)
    roi, probability, fills, net = evaluated[best]
    curve = []
    for bond in sorted(set(CURVE_BONDS) | {best}):
        evaluate(bond)
        point_roi, point_probability, point_fills, point_net = evaluated[bond]
        curve.append(
            {
                "bond_sats": bond,
                "selection_probability": point_probability,
                "monthly_cjs": point_fills,
                "monthly_net_sats": point_net,
                "annual_roi_percent": 100 * point_roi,
                "optimal": bond == best,
            }
        )
    fee.pop("score")
    result.update(fee)
    result.update(
        status="ok",
        bond_sats=best,
        lock_years=1,
        selection_probability=probability,
        monthly_cjs=fills,
        monthly_net_sats=net,
        annual_roi_percent=100 * roi,
        boundary="lower" if best == MIN_BOND else "upper" if best == MAX_BOND else None,
        bond_curve=curve,
    )
    result["fee_description"] = (
        f"Relative offer at {_percent(Decimal(fee['relative_fee']))}, "
        f"{fee['per_fill_sats']:,} sat per typical CJ. "
        f"{fee['fee_eligibility']:.1%} of historical takers accepted at least this rate."
    )
    result["driver_note"] = (
        f"{monthly:,.1f} CJs/month × {fee['fee_eligibility']:.1%} inferred fee eligibility "
        f"× {probability:.1%} selection probability = {fills:.2f} modeled fills/month. "
        f"Typical CJ: {size:,} sat, {makers:g} makers."
    )
    result["bond_note"] = (
        "Selection weight grows as bond^1.3, so each extra BTC buys more than proportional "
        "selection probability until the bond dominates the competing weight. "
        "ROI per BTC therefore rises with bond size up to the optimum, then falls as "
        "selection saturates."
    )
    result["assumptions"] = (
        f"{days}-day activity ending {_date(end)}. "
        f"Fee evidence: {len(samples):,} one-input-per-participant CJs "
        f"({result['fee_sample_start']} to {result['fee_sample_end']}); "
        "all maker fees treated as relative; assumes nonnegative maker gains. "
        f"{outliers:,} CJs implying rates of {_percent(MAX_REALISTIC_REL)} or more excluded as "
        "outliers (absolute fees on small CJs, non-JoinMarket transactions); "
        f"fees of {_percent(MAX_REALISTIC_REL)} or more not evaluated. "
        f"Bond evidence: {matched:,}/{len(rows):,} CJs matched to earlier snapshots "
        f"({result['bond_sample_start']} to {result['bond_sample_end']}). "
        "All active historical bonds, 95% weighted slots; "
        "fee limits and competition assumed independent. "
        "One-year lock, 1.5% valuation rate, exponent 1.3, bond range 0.01 to 1,000 BTC. "
        "Unlimited liquidity, zero maker miner contribution; operating and bond costs excluded. "
        "Typical-round estimate, not a large-transaction-weighted forecast."
    )
    if result["boundary"]:
        result["assumptions"] += f" Best result is at the {result['boundary']} bond bound."
    return result


def optimize_maker(
    archive: Archive | None, fee_records: dict[str, FeeRecord], bond_points: list[dict]
) -> dict:
    if archive is None or not archive.blocks:
        return {
            "status": "unavailable",
            "reason": "Historical CJ activity is unavailable.",
            "windows": [],
        }
    paths_cache, probability_cache = {}, {}
    windows = [
        _one_window(archive, days, fee_records, bond_points, paths_cache, probability_cache)
        for days in (90, 365)
    ]
    primary = next((window for window in windows if window["status"] == "ok"), windows[0])
    return dict(primary, basis="historical_cjs", windows=windows)
