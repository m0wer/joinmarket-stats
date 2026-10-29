"""Exact maker fee intervals for CoinJoin-shaped transactions, in integer arithmetic.

Conditional model (constraints from joinmarket_analyzer): one taker; every maker
gains g = denomination + change - inputs within [0, B], B = max(E // 200, 10,000).
There are n or n - 1 change outputs, so each change identifies one participant
and at most one participant has none. Given disjoint maker input groups, the
taker owns the remaining inputs and, by conservation, pays at least the network
fee. The total maker fee is M = sum of maker gains = taker inputs - taker change
- denomination - network fee, so identifying the taker identifies M.

The search is exhaustive within deterministic step budgets, so every machine gets
the same result. Exceeding a budget is "unresolved", never a guess. Found
extremes are rechecked as explicit decompositions before they are returned.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

Sats = Annotated[int, Field(strict=True, ge=0)]
METHOD = "exact-interval-v1"
# Deterministic work limits per transaction (roughly a few seconds of CPython).
CANDIDATE_LIMIT = 50_000
STEP_LIMIT = 30_000_000
DECOMPOSITION_LIMIT = 10_000


def ceiling(denomination: int) -> int:
    """The largest maker gain the model allows for a denomination."""
    return max(denomination // 200, 10_000)


class Amounts(BaseModel):
    inputs: list[Sats]
    outputs: list[Sats]
    denomination: Annotated[int, Field(strict=True, gt=0)]
    participants: Annotated[int, Field(strict=True, ge=3)]

    @model_validator(mode="after")
    def shaped(self):
        if sum(self.inputs) < sum(self.outputs):
            raise ValueError("Transaction outputs exceed inputs")
        if len(self.inputs) < self.participants:
            raise ValueError("Not enough participant inputs")
        if self.outputs.count(self.denomination) != self.participants or len(self.outputs) not in (
            2 * self.participants - 1,
            2 * self.participants,
        ):
            raise ValueError("Transaction does not match the archived CoinJoin shape")
        return self

    @property
    def network_fee(self) -> int:
        return sum(self.inputs) - sum(self.outputs)

    @property
    def changes(self) -> list[int]:
        return [value for value in self.outputs if value != self.denomination]

    @property
    def ceiling(self) -> int:
        return ceiling(self.denomination)


class Result(BaseModel):
    """Bounds on the total maker fee M over every decomposition the model allows.

    identified: low == high. ambiguous: low < high. inconsistent: no decomposition
    exists, so the transaction contradicts the model (usually not JoinMarket).
    unresolved: a budget was exceeded; nothing is known. ``gains`` holds the sorted
    maker gains that are equal in every decomposition (a subset of the makers).
    """

    status: Literal["identified", "ambiguous", "unresolved", "inconsistent"]
    low: Sats | None = None
    high: Sats | None = None
    gains: list[Sats] = []
    reason: Literal["candidates", "search"] | None = None

    @model_validator(mode="after")
    def consistent(self):
        bounded = self.status in ("identified", "ambiguous")
        if bounded != (self.low is not None and self.high is not None):
            raise ValueError("Only resolved transactions have fee bounds")
        if bounded and (self.low == self.high) != (self.status == "identified"):
            raise ValueError("Fee bounds do not match their status")
        if (self.status == "unresolved") != (self.reason is not None):
            raise ValueError("Only unresolved transactions have a budget reason")
        if self.gains and not bounded or self.gains != sorted(self.gains):
            raise ValueError("Identified maker gains need resolved bounds")
        return self

    def check(self, denomination: int, participants: int) -> None:
        """Cheap invariants against the candidate a stored result claims to describe."""
        makers, top = participants - 1, ceiling(denomination)
        if self.high is not None and self.high > makers * top:
            raise ValueError("Fee bound exceeds the model ceiling")
        if len(self.gains) > makers or any(gain > top for gain in self.gains):
            raise ValueError("Identified maker gains exceed the model")
        # Every decomposition shares the fixed gains and the others are nonnegative.
        if self.low is not None and sum(self.gains) > self.low:
            raise ValueError("Identified maker gains exceed the fee bound")
        if len(self.gains) == makers and sum(self.gains) != self.low:
            raise ValueError("Identified maker gains do not match the total")


class _Budget(Exception):
    def __init__(self, reason: str):
        self.reason = reason


class _Search:
    def __init__(self, amounts: Amounts):
        self.amounts = amounts
        self.steps = 0
        owners = amounts.changes
        owners += [0] * (amounts.participants - len(owners))
        # Slot s owns change s; a trailing zero is the participant without change.
        self.targets = [amounts.denomination + change for change in owners]
        self.full = (1 << len(amounts.inputs)) - 1

    def step(self, reason: str) -> None:
        self.steps += 1
        if self.steps > STEP_LIMIT:
            raise _Budget(reason)

    def groups(self, target: int) -> list[tuple[int, int]]:
        """Every nonempty input group (bitmask, gain) with 0 <= gain <= ceiling."""
        inputs = self.amounts.inputs
        order = sorted(range(len(inputs)), key=lambda i: -inputs[i])
        values = [inputs[i] for i in order]
        suffix = [0] * (len(values) + 1)
        for k in range(len(values) - 1, -1, -1):
            suffix[k] = suffix[k + 1] + values[k]
        low, found = target - self.amounts.ceiling, []
        # Iterative depth-first search over include/exclude choices.
        stack = [(0, 0, 0)]
        while stack:
            self.step("candidates")
            k, total, mask = stack.pop()
            if total > target or total + suffix[k] < low:
                continue
            if k == len(values):
                if mask:
                    found.append((mask, target - total))
                    if len(found) > CANDIDATE_LIMIT:
                        raise _Budget("candidates")
                continue
            stack.append((k + 1, total, mask))
            stack.append((k + 1, total + values[k], mask | 1 << order[k]))
        return found

    def solve(self) -> Result:
        try:
            candidates = [self.groups(target) for target in self.targets]
            low, high, extremes = self.bounds(candidates)
            if low is None:
                return Result(status="inconsistent")
            for extreme in extremes:
                self.verify(*extreme)
        except _Budget as exc:
            return Result(status="unresolved", reason=exc.reason)
        try:
            gains = self.fixed_gains(candidates)
        except _Budget:
            gains = []
        status = "identified" if low == high else "ambiguous"
        return Result(status=status, low=low, high=high, gains=gains)

    def takers(self, candidates):
        """Yield (taker, maker slots by fewest groups) for each possible taker slot."""
        empty = [s for s, groups in enumerate(candidates) if not groups]
        for taker in range(len(candidates)):
            if any(s != taker for s in empty):
                continue
            yield (
                taker,
                sorted(
                    (s for s in range(len(candidates)) if s != taker),
                    key=lambda s: len(candidates[s]),
                ),
            )

    def bounds(self, candidates):
        """Branch and bound for min and max M over all taker slots."""
        best = {"low": None, "high": None, "extremes": {}}
        for taker, makers in self.takers(candidates):
            self._bound_taker(candidates, taker, makers, best)
        return best["low"], best["high"], list(best["extremes"].values())

    def _bound_taker(self, candidates, taker: int, makers: list[int], best: dict) -> None:
        least = [min(gain for _, gain in candidates[s]) for s in makers]
        most = [max(gain for _, gain in candidates[s]) for s in makers]
        floor = [sum(least[k:]) for k in range(len(makers) + 1)]
        top = [sum(most[k:]) for k in range(len(makers) + 1)]
        chosen = [0] * len(makers)

        def search(k, used, total):
            # Prune subtrees that can improve neither extreme.
            if best["low"] is not None and (
                total + floor[k] >= best["low"] and total + top[k] <= best["high"]
            ):
                return
            if k == len(makers):
                if used == self.full:
                    return  # The taker needs an input.
                record = (taker, dict(zip(makers, chosen, strict=True)), total)
                if best["low"] is None or total < best["low"]:
                    best["low"], best["extremes"]["low"] = total, record
                if best["high"] is None or total > best["high"]:
                    best["high"], best["extremes"]["high"] = total, record
                return
            for mask, gain in candidates[makers[k]]:
                self.step("search")
                if not mask & used:
                    chosen[k] = mask
                    search(k + 1, used | mask, total + gain)

        search(0, 0, 0)

    def fixed_gains(self, candidates) -> list[int]:
        """Maker gains equal across every decomposition, by bounded full enumeration."""
        seen: dict[int, set[int | None]] = {s: set() for s in range(len(candidates))}
        count = [0]
        for taker, makers in self.takers(candidates):
            self._enumerate_taker(candidates, taker, makers, seen, count)
        return sorted(next(iter(v)) for v in seen.values() if len(v) == 1 and None not in v)

    def _enumerate_taker(self, candidates, taker, makers, seen, count) -> None:
        chosen = [0] * len(makers)

        def visit(k, used):
            if k == len(makers):
                if used == self.full:
                    return
                count[0] += 1
                if count[0] > DECOMPOSITION_LIMIT:
                    raise _Budget("search")
                seen[taker].add(None)
                for s, mask in zip(makers, chosen, strict=True):
                    seen[s].add(self.gain(s, mask))
                return
            for mask, _ in candidates[makers[k]]:
                self.step("search")
                if not mask & used:
                    chosen[k] = mask
                    visit(k + 1, used | mask)

        visit(0, 0)

    def gain(self, slot: int, mask: int) -> int:
        inputs = self.amounts.inputs
        return self.targets[slot] - sum(inputs[i] for i in range(len(inputs)) if mask >> i & 1)

    def verify(self, taker: int, makers: dict[int, int], total: int) -> None:
        """Recheck one decomposition from scratch with plain integer arithmetic."""
        a, used = self.amounts, 0
        if sorted([taker, *makers]) != list(range(a.participants)):
            raise AssertionError("Decomposition does not cover every participant")
        gains = []
        for slot, mask in makers.items():
            if not mask or mask & used:
                raise AssertionError("Decomposition reuses or omits maker inputs")
            used |= mask
            gains.append(self.gain(slot, mask))
        if used == self.full or any(not 0 <= g <= a.ceiling for g in gains):
            raise AssertionError("Decomposition violates the maker model")
        taker_gain = self.gain(taker, self.full & ~used)
        if sum(gains) != total or taker_gain != -a.network_fee - total:
            raise AssertionError("Decomposition violates fee conservation")


def analyze(amounts: Amounts) -> Result:
    """Exact bounds on the total maker fee, or an explicit unresolved/inconsistent result."""
    result = _Search(amounts).solve()
    result.check(amounts.denomination, amounts.participants)
    return result
