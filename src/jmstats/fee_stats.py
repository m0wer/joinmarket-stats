"""Monthly maker fee totals and per-maker distributions from exact per-tx analyses.

Every candidate (denomination E >= 75,000 sats, n participants) falls into one class:
identified (total maker fee M known), ambiguous (M within proven [low, high]),
unresolved (search budget exceeded, so only the model range [0, (n - 1) B]),
unanalyzed (no analysis yet), or inconsistent (no decomposition exists under the
JoinMarket model, so it is excluded).

Missing totals are imputed from the maker fee rate r = M / ((n - 1) E) of identified
transactions in the same denomination decade. Whether a transaction is identified
depends on whether unrelated input subsets happen to fit the maker windows, which the
amounts fix independently of the true decomposition. Fee rates differ by orders of
magnitude across denominations, and ambiguous transactions skew large, so
stratification matters more than the period. The imputed value is the mean of the
cell's identified rates inside the transaction's proven rate interval (the
conditional mean); if none fall inside, the cell mean clipped to the interval.

Per-maker medians and p90 come from maker gains that are equal in every
decomposition, weighted per decade so each decade counts with its total maker slots.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from fractions import Fraction
from itertools import accumulate

from .solver import Result, ceiling

MIN_CELL = 30
TRAILING_MONTHS = 3
COUNTS = ("identified", "ambiguous", "unresolved", "unanalyzed")


def _band(denomination: int) -> int:
    return len(str(denomination)) - 1  # floor(log10) without float rounding


def _index(month: str) -> int:
    year, number = month.split("-")
    return int(year) * 12 + int(number) - 1


class _Pool:
    def __init__(self, rates: list[float]):
        self.rates = sorted(rates)
        self.prefix = [0.0, *accumulate(self.rates)]

    def impute(self, low: float, high: float, scale: int) -> float:
        """Conditional mean of rates in [low, high] / scale, in sats."""
        i = bisect_left(self.rates, low / scale)
        j = bisect_right(self.rates, high / scale)
        if j > i:
            rate = (self.prefix[j] - self.prefix[i]) / (j - i)
        else:
            rate = self.prefix[-1] / len(self.rates)
        return min(max(rate * scale, low), high)


class _Cells:
    """Identified rates by (month, decade) with deterministic fallbacks."""

    def __init__(self, rates: dict[tuple[int, int], list[float]]):
        self.rates = rates
        self.decades: dict[int, list[float]] = {}
        for (_, band), values in rates.items():
            self.decades.setdefault(band, []).extend(values)
        self.pools: dict[tuple, _Pool | None] = {}
        self.chosen: dict[tuple[int, int], _Pool | None] = {}

    def _pool(self, key: tuple, rates: list[float]) -> _Pool | None:
        if key not in self.pools:
            self.pools[key] = _Pool(rates) if rates else None
        return self.pools[key]

    def pool(self, month: int, band: int) -> _Pool | None:
        if (month, band) not in self.chosen:
            self.chosen[month, band] = self._choose(month, band)
        return self.chosen[month, band]

    def _choose(self, month: int, band: int) -> _Pool | None:
        """Same month, else trailing months, else all months of the nearest decades."""
        same = self.rates.get((month, band), [])
        if len(same) >= MIN_CELL:
            return self._pool(("month", month, band), same)
        trailing = [
            r
            for m in range(month - TRAILING_MONTHS + 1, month + 1)
            for r in self.rates.get((m, band), [])
        ]
        if len(trailing) >= MIN_CELL:
            return self._pool(("trailing", month, band), trailing)
        # Rates fall steeply with denomination, so widen to the nearest decades
        # rather than pooling everything (a 30 BTC round at small-round rates).
        distance = 0
        while True:
            near = [b for b in self.decades if abs(b - band) <= distance]
            rates = [r for b in near for r in self.decades[b]]
            if len(rates) >= MIN_CELL or len(near) == len(self.decades):
                return self._pool(("decades", band, distance), rates)
            distance += 1


def _quantile(weighted: list[tuple[float, Fraction]], q: Fraction) -> float | None:
    """Smallest value whose cumulative weight reaches q of the total."""
    if not weighted:
        return None
    weighted = sorted(weighted)
    target, cumulative = q * sum(w for _, w in weighted), Fraction(0)
    for value, weight in weighted:
        cumulative += weight
        if cumulative >= target:
            return value
    return weighted[-1][0]


def monthly_maker_fees(
    months: dict[str, list[list]], analyses: dict[str, Result]
) -> dict[str, dict]:
    """Per-month maker fee fields for archive candidates of at least 75,000 sats."""
    rates: dict[tuple[int, int], list[float]] = {}
    for month, candidates in months.items():
        for txid, denomination, participants, *_ in candidates:
            result = analyses.get(txid)
            if result is None:
                continue
            result.check(denomination, participants)
            if result.status == "identified":
                rates.setdefault((_index(month), _band(denomination)), []).append(
                    result.low / ((participants - 1) * denomination)
                )
    cells = _Cells(rates)
    return {
        month: _month(_index(month), candidates, analyses, cells)
        for month, candidates in months.items()
    }


def _month(index: int, candidates, analyses, cells: _Cells) -> dict:
    counts = dict.fromkeys(COUNTS, 0)
    inconsistent = 0
    exact = ambiguous_low = ambiguous_high = 0
    imputed_ambiguous = imputed_unresolved = 0.0
    relative = 0.0
    slots = 0
    band_slots: dict[int, int] = {}
    fixed: dict[int, list[tuple[int, float]]] = {}
    missing_pool = False
    for txid, denomination, participants, *_ in candidates:
        result = analyses.get(txid)
        if result is None:
            counts["unanalyzed"] += 1
            continue
        if result.status == "inconsistent":
            inconsistent += 1
            continue
        counts[result.status] += 1
        makers, band = participants - 1, _band(denomination)
        slots += makers
        band_slots[band] = band_slots.get(band, 0) + makers
        fixed.setdefault(band, []).extend((g, 100 * g / denomination) for g in result.gains)
        if result.status == "identified":
            exact += result.low
            relative += result.low / denomination
            continue
        if result.status == "ambiguous":
            low, high = result.low, result.high
            ambiguous_low += low
            ambiguous_high += high
        else:
            low, high = 0, makers * ceiling(denomination)
        pool = cells.pool(index, band)
        if pool is None:
            missing_pool = True
            continue
        value = pool.impute(low, high, makers * denomination)
        relative += value / denomination
        if result.status == "ambiguous":
            imputed_ambiguous += value
        else:
            imputed_unresolved += value
    floor = exact + ambiguous_low
    resolved = slots > 0 and not missing_pool
    weighted_sats, weighted_percent = [], []
    for band, gains in fixed.items():
        if gains:
            weight = Fraction(band_slots[band], len(gains))
            weighted_sats += [(sats, weight) for sats, _ in gains]
            weighted_percent += [(pct, weight) for _, pct in gains]
    half, p90 = Fraction(1, 2), Fraction(9, 10)
    return {
        "maker_fee_estimate_sats": round(exact + imputed_ambiguous + imputed_unresolved)
        if resolved
        else None,
        "maker_fee_exact_sats": exact if slots else None,
        "maker_fee_low_sats": round(floor + imputed_unresolved) if resolved else None,
        "maker_fee_high_sats": round(exact + ambiguous_high + imputed_unresolved)
        if resolved
        else None,
        "maker_fee_floor_sats": floor if slots else None,
        "maker_fee_unresolved_sats": round(imputed_unresolved) if resolved else None,
        "maker_fee_mean_sats": round((exact + imputed_ambiguous + imputed_unresolved) / slots, 2)
        if resolved
        else None,
        "maker_fee_mean_percent": round(100 * relative / slots, 6) if resolved else None,
        "maker_fee_median_sats": _quantile(weighted_sats, half),
        "maker_fee_median_percent": _rounded(_quantile(weighted_percent, half)),
        "maker_fee_p90_sats": _quantile(weighted_sats, p90),
        "maker_fee_p90_percent": _rounded(_quantile(weighted_percent, p90)),
        "maker_fee_slots": slots,
        "maker_fee_fixed_slots": sum(len(g) for g in fixed.values()),
        **{f"maker_fee_{name}": count for name, count in counts.items()},
        "maker_fee_partial": counts["unanalyzed"] > 0,
        "inconsistent": inconsistent,
    }


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 6)
