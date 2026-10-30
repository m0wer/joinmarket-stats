"""Monthly maker fee totals, per-maker fee quantiles, and taker costs by Monte Carlo.

Every candidate (denomination E >= 75,000 sats, n participants) falls into one class:
identified (the total maker fee M is the same in every decomposition, for example
whenever the taker's inputs are determined), ambiguous (M within proven [low,
high]), unresolved (search budget exceeded, so only the model range [0, (n - 1) B]),
unanalyzed (no analysis yet, not imputed), or inconsistent (no decomposition exists
under the JoinMarket model, so it is excluded).

Unknown maker gains are drawn from donors: identified transactions with every maker
gain known, in the same denomination decade and month (else the trailing months,
else all months of the nearest decades). A donor's level is its mean log maker
gain, adjusted within the pool for E (elasticity in [0, 1]: absolute offers are
flat in E, relative ones proportional) and for maker-count buckets, so a cell does
not give large or many-maker rounds the fees of small three-party ones. Levels are
shrunk toward the pool mean by their estimation noise, since makers of one CoinJoin
share the taker's fee limits (a large part of the variance of log gains is between
CoinJoins). One draw for a transaction

- proposes donors from this draw's bootstrap of the pool and accepts one with a
  Gaussian kernel on the difference between its level and that of the target's
  fixed positive maker gains,
- resamples the donor's makers for the target's unknown ones: zero stays zero, and
  a positive gain keeps its own deviation from the donor's level (scaled for the
  few makers it was estimated from), at the target's E and maker count, capped at
  the model ceiling, and
- retries outside the proven bounds (bounded tries, then a reservoir of accepted
  draws from the whole pool, then uniform within the bounds).

When the solver enumerated every feasible gain multiset of the unknown makers
(options), one is drawn with probability proportional to its likelihood under the
pool: a mixture over binned donor levels (weighted by their compatibility with the
fixed gains) of the gains' deviation densities, with a point mass at zero.

Each of DRAWS draws bootstraps every donor pool once (lazily), shared by all
transactions of the month, so the 90% model interval includes donor sampling
uncertainty. Totals are means of the draws (they add across months). The taker's
cost of a CoinJoin is (M + network fee) / E. Seeds are fixed per month, so renders
are reproducible.

Checked against simulations (calibrated per-transaction draws) and by masking a
quarter of the real 2020-2026 identified CoinJoins as unresolved: their imputed
total was within about 10% of the truth per period, and the monthly 90% intervals
covered it in 43 of 49 months.
"""

from __future__ import annotations

import math
import random
from bisect import bisect_left, bisect_right
from itertools import accumulate
from statistics import median
from typing import NamedTuple

from .solver import Result, ceiling

DRAWS = 100
# Draws whose unknown maker gains feed the per-maker quantiles (sorting cost).
MAKER_DRAWS = 20
TRIES = 64
# Larger budget and sample, once per target, when the bootstrapped tries miss.
RESERVOIR_TRIES = 20_000
RESERVOIR_SIZE = 64
MIN_CELL = 30
TRAILING_MONTHS = 3
BIN = 0.1  # log-rate bin width of the within-CoinJoin deviation density
# Ridge strength of the gain slopes on log E (toward 1) and log makers (toward 0),
# in units of summed squared covariate deviations.
SLOPE_PRIOR = 1.0
# Maker-count buckets (upper bounds: 2-3, 4-6, 7-9, 10+ makers) and their ridge,
# in donors.
MAKER_BUCKETS = (3, 6, 9)
BUCKET_PRIOR = 10.0
FIT_ROUNDS = 20
CENTER_BIN = 0.25  # log-rate bin width of donor centers for option weights
FLOOR = -30.0  # log density far outside the observed deviations
COUNTS = ("identified", "ambiguous", "unresolved", "unanalyzed")


def _band(denomination: int) -> int:
    return len(str(denomination)) - 1  # floor(log10) without float rounding


def _index(month: str) -> int:
    year, number = month.split("-")
    return int(year) * 12 + int(number) - 1


def _center(gains, denomination: int) -> tuple[float | None, int]:
    """Mean log rate of the positive gains, and their count."""
    logs = [math.log(g / denomination) for g in gains if g > 0]
    return (sum(logs) / len(logs) if logs else None), len(logs)


class _Donor(NamedTuple):
    rates: tuple[float, ...]  # every maker gain / E
    center: float | None  # mean log rate of the positive gains (a level in a pool)
    positive: int
    deviations: tuple[float, ...]  # from the center, scaled by sqrt(m / (m - 1))
    size: float  # log E


def _donor(gains: list[int], denomination: int) -> _Donor:
    center, positive = _center(gains, denomination)
    deviations = ()
    if positive >= 2:
        unit = math.sqrt(positive / (positive - 1))
        deviations = tuple((math.log(g / denomination) - center) * unit for g in gains if g > 0)
    return _Donor(
        tuple(g / denomination for g in gains), center, positive, deviations, math.log(denomination)
    )


class _Pool:
    """Donor CoinJoins of one cell and a random-effects model of their maker gains.

    A positive maker's log gain is its CoinJoin's level (after the E and maker
    count adjustments of ``lift``) plus a within-CoinJoin deviation; zero gains are
    a point mass. Donor levels are means of few makers, so they are shrunk toward
    the pool mean by their estimation noise (within / m), and deviations are scaled
    by sqrt(m / (m - 1)), so both keep their own variance.
    """

    def __init__(self, donors: list[_Donor]):
        squares = degrees = 0.0
        self.deviations: list[float] = []
        zeros = makers = 0
        for donor in donors:
            makers += len(donor.rates)
            zeros += len(donor.rates) - donor.positive
            if donor.deviations:
                self.deviations += donor.deviations
                squares += (
                    sum(d * d for d in donor.deviations) * (donor.positive - 1) / donor.positive
                )
                degrees += donor.positive - 1
        within = squares / degrees if degrees else 1.0
        # Floored so identical donors still give a finite kernel.
        self.within = max(within, 0.01)
        self.zero = (zeros + 0.5) / (makers + 1)
        self.elasticity, self.effects = _fit(donors)
        # Levels: log gain - elasticity * log E - maker-count effect, so that
        # log rate = level - lift(E, makers).
        donors = [
            d if d.center is None else d._replace(center=d.center + self.lift(d.size, len(d.rates)))
            for d in donors
        ]
        centers = [d.center for d in donors if d.center is not None]
        mean = sum(centers) / len(centers) if centers else 0.0
        spread = sum((c - mean) ** 2 for c in centers) / len(centers) if centers else 0.0
        self.donors = [
            d
            if d.center is None
            else d._replace(
                center=mean
                + (d.center - mean) * math.sqrt(max(0.0, 1 - within / d.positive / spread))
                if spread > 0
                else mean
            )
            for d in donors
        ]
        groups: dict[int, int] = {}
        for donor in self.donors:
            if donor.center is not None:
                key = math.floor(donor.center / CENTER_BIN)
                groups[key] = groups.get(key, 0) + 1
        self.groups = sorted(((b + 0.5) * CENTER_BIN, count) for b, count in groups.items())
        self.offset, self.density = _log_density(self.deviations)

    def lift(self, size: float, makers: int) -> float:
        return (1 - self.elasticity) * size - self.effects[_bucket(makers)]

    def log_deviation(self, deviation: float) -> float:
        index = math.floor(deviation / BIN) - self.offset
        return self.density[index] if 0 <= index < len(self.density) else FLOOR


def _bucket(makers: int) -> int:
    return bisect_left(MAKER_BUCKETS, makers)


def _fit(donors: list[_Donor]) -> tuple[float, tuple[float, ...]]:
    """Elasticity of a CoinJoin's mean log maker gain to E, and maker-count effects.

    Maker offers are absolute (gain flat in E) or relative (proportional to E), and
    takers pick the cheapest for their amount, so gains grow with E by a power in
    [0, 1] (ridge toward 1, rate scaling). Per-maker gains also differ by maker
    count, not monotonically across cells, so by bucket (ridge toward 0). Without
    these, a cell gives large rounds the rates of small ones and many-maker rounds
    the fees of three-party ones. Ridge regression by coordinate descent.
    """
    points = [
        (d.size, _bucket(len(d.rates)), d.center + d.size) for d in donors if d.center is not None
    ]
    effects = [0.0] * (len(MAKER_BUCKETS) + 1)
    if not points:
        return 1.0, tuple(effects)
    mean = sum(x for x, _, _ in points) / len(points)
    points = [(x - mean, k, y) for x, k, y in points]
    squares = sum(x * x for x, _, _ in points) + SLOPE_PRIOR
    counts = [0] * len(effects)
    for _, k, _ in points:
        counts[k] += 1
    elasticity = 1.0
    for _ in range(FIT_ROUNDS):
        intercept = sum(y - elasticity * x - effects[k] for x, k, y in points) / len(points)
        slope = sum(x * (y - intercept - effects[k]) for x, k, y in points) + SLOPE_PRIOR
        elasticity = min(1.0, max(0.0, slope / squares))
        sums = [0.0] * len(effects)
        for x, k, y in points:
            sums[k] += y - intercept - elasticity * x
        effects = [
            total / (count + BUCKET_PRIOR) for total, count in zip(sums, counts, strict=True)
        ]
    return elasticity, tuple(effects)


def _log_density(values: list[float]) -> tuple[int, list[float]]:
    """Binned Gaussian kernel density (Silverman bandwidth) as log density per bin."""
    if not values:
        return 0, []
    values = sorted(values)
    n = len(values)
    mean = sum(values) / n
    spread = math.sqrt(sum((y - mean) ** 2 for y in values) / n)
    iqr = values[(3 * n) // 4] - values[n // 4]
    width = max(BIN, 0.9 * min(spread, iqr / 1.34 or spread) * n**-0.2)
    counts: dict[int, int] = {}
    for y in values:
        counts[math.floor(y / BIN)] = counts.get(math.floor(y / BIN), 0) + 1
    reach = math.ceil(4 * width / BIN)
    offset = min(counts) - reach
    kernel = [math.exp(-0.5 * (k * BIN / width) ** 2) for k in range(-reach, reach + 1)]
    mass = [0.0] * (max(counts) - offset + reach + 1)
    for b, count in counts.items():
        for k, weight in enumerate(kernel, b - offset - reach):
            mass[k] += count * weight
    norm = n * width * math.sqrt(2 * math.pi)
    return offset, [max(math.log(m / norm) if m > 0 else FLOOR, FLOOR) for m in mass]


class _Cells:
    """Donors by (month, decade) with deterministic fallbacks."""

    def __init__(self, donors: dict[tuple[int, int], list[_Donor]]):
        self.donors = donors
        self.decades: dict[int, list[_Donor]] = {}
        for (_, band), values in sorted(donors.items()):
            self.decades.setdefault(band, []).extend(values)
        self.pools: dict[tuple, _Pool | None] = {}
        self.chosen: dict[tuple[int, int], _Pool | None] = {}

    def _pool(self, key: tuple, donors: list[_Donor]) -> _Pool | None:
        if key not in self.pools:
            self.pools[key] = _Pool(donors) if donors else None
        return self.pools[key]

    def pool(self, month: int, band: int) -> _Pool | None:
        if (month, band) not in self.chosen:
            self.chosen[month, band] = self._choose(month, band)
        return self.chosen[month, band]

    def _choose(self, month: int, band: int) -> _Pool | None:
        """Same month, else trailing months, else all months of the nearest decades."""
        same = self.donors.get((month, band), [])
        if len(same) >= MIN_CELL:
            return self._pool(("month", month, band), same)
        trailing = [
            d
            for m in range(month - TRAILING_MONTHS + 1, month + 1)
            for d in self.donors.get((m, band), [])
        ]
        if len(trailing) >= MIN_CELL:
            return self._pool(("trailing", month, band), trailing)
        # Rates fall steeply with denomination, so widen to the nearest decades
        # rather than pooling everything (a 30 BTC round at small-round rates).
        distance = 0
        while True:
            near = [b for b in sorted(self.decades) if abs(b - band) <= distance]
            donors = [d for b in near for d in self.decades[b]]
            if len(donors) >= MIN_CELL or len(near) == len(self.decades):
                return self._pool(("decades", band, distance), donors)
            distance += 1


class _Target(NamedTuple):
    """A transaction whose total maker fee (or some maker gains) must be drawn."""

    status: str
    pool: _Pool
    denomination: int
    top: int  # maker gain ceiling
    fixed: int  # sum of fixed gains
    unknown: int  # makers without a fixed gain
    center: float | None  # level of the fixed positive gains
    positive: int
    lift: float  # level - log rate at this denomination
    low: int
    high: int
    fee: int | None  # network fee
    options: list[list[int]]
    cumulative: list[float]  # option weights, cumulative


def option_weights(result: Result, pool: _Pool, denomination: int) -> list[float]:
    """Relative posterior probability of each option, uniform over distinct multisets.

    P(option) is proportional to the sum over binned donor centers c of P(c | fixed
    gains) times the product of P(gain | c) over the option's gains, so the fixed
    makers and the option's own gains both inform the CoinJoin's fee level.
    """
    lift = pool.lift(math.log(denomination), len(result.gains) + len(result.options[0]))
    center, positive = _center(result.gains, denomination)
    if positive:
        center += lift
    merged = [
        (
            math.log(count)
            - (((center - value) ** 2) / (2 * pool.within / positive) if positive else 0.0),
            value,
        )
        for value, count in pool.groups
    ]
    if merged:
        top = max(w for w, _ in merged)
        merged = [(w, v) for w, v in merged if w > top - 30]
    zero, nonzero = math.log(pool.zero), math.log1p(-pool.zero)
    logs = []
    for option in result.options:
        rates = [math.log(g / denomination) + lift for g in option if g > 0]
        # Probability per sat: density per unit log rate divided by the gain.
        base = (len(option) - len(rates)) * zero + len(rates) * nonzero
        base -= sum(math.log(g) for g in option if g > 0)
        terms = [w + sum(pool.log_deviation(y - v) for y in rates) for w, v in merged]
        logs.append(base + (_logsumexp(terms) if terms else 0.0))
    top = max(logs)
    return [math.exp(value - top) for value in logs]


def _logsumexp(values: list[float]) -> float:
    top = max(values)
    return top + math.log(sum(math.exp(v - top) for v in values))


def _draw(
    rng: random.Random, target: _Target, boot: dict, reservoir: list
) -> tuple[int, list[int]]:
    """One total maker fee and unknown maker gains for a target.

    ``boot`` is this draw's lazy bootstrap of the target's donor pool and
    ``reservoir`` the target's own cache of accepted draws from the whole pool.
    """
    if target.options:
        point = rng.random() * target.cumulative[-1]
        option = target.options[
            min(bisect_right(target.cumulative, point), len(target.options) - 1)
        ]
        return target.fixed + sum(option), option
    donors, count = target.pool.donors, len(target.pool.donors)
    for _ in range(TRIES):
        slot = int(rng.random() * count)
        donor = boot.get(slot)
        if donor is None:
            donor = boot[slot] = donors[int(rng.random() * count)]
        if target.positive and rng.random() >= _compatibility(target, donor):
            continue
        gains = _resample(rng, donor, target)
        total = target.fixed + sum(gains)
        if target.low <= total <= target.high:
            return total, gains
    # The fixed gains or proven bounds hold little predictive mass: reuse draws
    # accepted from the whole pool with a larger budget (computed once), else
    # ignore the fixed gains, else draw uniformly within the bounds.
    for kernel in (True, False):
        if reservoir:
            break
        for _ in range(RESERVOIR_TRIES):
            donor = donors[int(rng.random() * count)]
            if kernel and target.positive and rng.random() >= _compatibility(target, donor):
                continue
            gains = _resample(rng, donor, target)
            total = target.fixed + sum(gains)
            if target.low <= total <= target.high:
                reservoir.append((total, gains))
                if len(reservoir) == RESERVOIR_SIZE:
                    break
    if not reservoir:
        reservoir.append(None)
    choice = reservoir[int(rng.random() * len(reservoir))]
    if choice is None:
        return target.low + int(rng.random() * (target.high - target.low + 1)), []
    return choice


def _compatibility(target: _Target, donor: _Donor) -> float:
    """Kernel acceptance of a donor given the target's fixed positive gains."""
    if donor.center is None:
        return 0.0
    spread = target.pool.within / target.positive
    return math.exp(-((target.center - donor.center) ** 2) / (2 * spread))


def _resample(rng: random.Random, donor: _Donor, target: _Target) -> list[int]:
    """Unknown maker gains: the donor's makers, resampled around its shrunk level.

    A drawn maker with zero gain gives zero; a positive one gives exp(level + its
    scaled deviation - lift), so a donor keeps its own spread (heteroscedastic,
    heavy-tailed in practice). A single positive maker has no observed deviation,
    so one is drawn from the pool.
    """
    pool, rates, positives = target.pool, donor.rates, donor.deviations
    zeros, count = len(rates) - donor.positive, len(rates)
    gains = []
    for _ in range(target.unknown):
        index = int(rng.random() * count) - zeros
        if index < 0:
            gains.append(0)
            continue
        if positives:
            deviation = positives[index]
        elif pool.deviations:
            deviation = pool.deviations[int(rng.random() * len(pool.deviations))]
        else:
            deviation = 0.0
        rate = math.exp(donor.center + deviation - target.lift)
        gains.append(min(round(rate * target.denomination), target.top))
    return gains


def _pick(ordered: list, q: float):
    return ordered[round(q * (len(ordered) - 1))]


def _weighted_quantile(weighted: list[tuple[float, int]], q: float) -> float | None:
    """Smallest value whose cumulative weight reaches q of the total."""
    if not weighted:
        return None
    weighted.sort()
    target, cumulative = q * sum(w for _, w in weighted), 0
    for value, weight in weighted:
        cumulative += weight
        if cumulative >= target:
            return value
    return weighted[-1][0]


def monthly_maker_fees(
    months: dict[str, list[list]], analyses: dict[str, Result]
) -> dict[str, dict]:
    """Per-month maker fee and taker cost fields for candidates of at least 75,000 sats."""
    donors: dict[tuple[int, int], list[_Donor]] = {}
    for month, candidates in months.items():
        for txid, denomination, participants, *_ in candidates:
            result = analyses.get(txid)
            if result is None:
                continue
            result.check(denomination, participants)
            if result.status == "identified" and len(result.gains) == participants - 1:
                donors.setdefault((_index(month), _band(denomination)), []).append(
                    _donor(result.gains, denomination)
                )
    cells = _Cells(donors)
    return {
        month: _month(month, candidates, analyses, cells) for month, candidates in months.items()
    }


def _month(month: str, candidates, analyses, cells: _Cells) -> dict:
    index = _index(month)
    counts = dict.fromkeys(COUNTS, 0)
    inconsistent = slots = fixed_slots = exact = floor = 0
    exact_relative = 0.0
    known: list[tuple[int, float]] = []  # fixed gains (sats, percent)
    costs: list[float] = []  # taker cost % of identified transactions
    targets: list[_Target] = []
    missing_pool = False
    for txid, denomination, participants, fee, *_ in candidates:
        result = analyses.get(txid)
        if result is None:
            counts["unanalyzed"] += 1
            continue
        if result.status == "inconsistent":
            inconsistent += 1
            continue
        counts[result.status] += 1
        makers = participants - 1
        slots += makers
        fixed_slots += len(result.gains)
        known += [(g, 100 * g / denomination) for g in result.gains]
        if result.status == "identified":
            exact += result.low
            floor += result.low
            exact_relative += result.low / denomination
            if fee is not None:
                costs.append(100 * (result.low + fee) / denomination)
            if len(result.gains) == makers or not result.options:
                continue  # Nothing to draw (unknown slots without options are skipped).
        elif result.status == "ambiguous":
            floor += result.low
        pool = cells.pool(index, _band(denomination))
        if pool is None:
            missing_pool = True
            continue
        center, positive = _center(result.gains, denomination)
        lift = pool.lift(math.log(denomination), makers)
        top = ceiling(denomination)
        targets.append(
            _Target(
                result.status,
                pool,
                denomination,
                top,
                sum(result.gains),
                makers - len(result.gains),
                None if center is None else center + lift,
                positive,
                lift,
                0 if result.low is None else result.low,
                makers * top if result.high is None else result.high,
                fee,
                result.options,
                list(accumulate(option_weights(result, pool, denomination)))
                if result.options
                else [],
            )
        )
    resolved = slots > 0 and not missing_pool
    row = {
        "maker_fee_estimate_sats": None,
        "maker_fee_interval_low_sats": None,
        "maker_fee_interval_high_sats": None,
        "maker_fee_exact_sats": exact if slots else None,
        "maker_fee_floor_sats": floor if slots else None,
        "maker_fee_unresolved_sats": None,
        "maker_fee_mean_sats": None,
        "maker_fee_mean_percent": None,
        "maker_fee_median_sats": None,
        "maker_fee_median_percent": None,
        "maker_fee_p90_sats": None,
        "maker_fee_p90_percent": None,
        "maker_fee_slots": slots,
        "maker_fee_fixed_slots": fixed_slots,
        **{f"maker_fee_{name}": count for name, count in counts.items()},
        "maker_fee_partial": counts["unanalyzed"] > 0,
        "inconsistent": inconsistent,
        "taker_cost_p25_percent": None,
        "taker_cost_median_percent": None,
        "taker_cost_p75_percent": None,
        "taker_cost_samples": 0,
    }
    if not resolved:
        return row
    rng = random.Random(f"jmstats maker fees {month}")
    totals, unresolved, relative = [], [], []
    quartiles: list[tuple[float, float, float]] = []
    drawn: list[tuple[float, float]] = []
    reservoirs: list[list] = [[] for _ in targets]
    for draw in range(DRAWS):
        boots: dict[int, dict] = {}
        total, missing, share, cost = exact, 0, exact_relative, list(costs)
        for position, target in enumerate(targets):
            if target.status == "identified" and draw >= MAKER_DRAWS:
                continue  # Only its unknown gains vary, for the per-maker quantiles.
            value, gains = _draw(
                rng, target, boots.setdefault(id(target.pool), {}), reservoirs[position]
            )
            if draw < MAKER_DRAWS:
                drawn += [(g, 100 * g / target.denomination) for g in gains]
            if target.status == "identified":
                continue
            total += value
            share += value / target.denomination
            if target.status == "unresolved":
                missing += value
            if target.fee is not None:
                cost.append(100 * (value + target.fee) / target.denomination)
        totals.append(total)
        unresolved.append(missing)
        relative.append(share)
        if cost:
            cost.sort()
            quartiles.append((_pick(cost, 0.25), _pick(cost, 0.5), _pick(cost, 0.75)))
    totals.sort()
    # Means, not medians: totals are right-skewed and must add up across months.
    estimate = round(sum(totals) / DRAWS)
    sats = [(g, MAKER_DRAWS) for g, _ in known] + [(g, 1) for g, _ in drawn]
    percent = [(p, MAKER_DRAWS) for _, p in known] + [(p, 1) for _, p in drawn]
    row.update(
        maker_fee_estimate_sats=estimate,
        maker_fee_interval_low_sats=_pick(totals, 0.05),
        maker_fee_interval_high_sats=_pick(totals, 0.95),
        maker_fee_unresolved_sats=round(sum(unresolved) / DRAWS),
        maker_fee_mean_sats=round(estimate / slots, 2),
        maker_fee_mean_percent=round(100 * sum(relative) / DRAWS / slots, 6),
        maker_fee_median_sats=_weighted_quantile(sats, 0.5),
        maker_fee_median_percent=_rounded(_weighted_quantile(percent, 0.5)),
        maker_fee_p90_sats=_weighted_quantile(sats, 0.9),
        maker_fee_p90_percent=_rounded(_weighted_quantile(percent, 0.9)),
    )
    if quartiles:
        drawn_costs = sum(t.fee is not None for t in targets if t.status != "identified")
        row.update(
            taker_cost_p25_percent=_rounded(median(q[0] for q in quartiles)),
            taker_cost_median_percent=_rounded(median(q[1] for q in quartiles)),
            taker_cost_p75_percent=_rounded(median(q[2] for q in quartiles)),
            taker_cost_samples=len(costs) + drawn_costs,
        )
    return row


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 6)
