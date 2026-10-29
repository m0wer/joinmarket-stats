"""Bounded, conditional CoinJoin decompositions, validated with integer arithmetic.

The taker is participant zero. Maker labels are interchangeable. This is a
feasibility model, not proof of a unique decomposition or observed gross fees.
"""

from __future__ import annotations

import json
import math
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

Sats = Annotated[int, Field(strict=True, ge=0)]
METHOD = "conditional-cbc-v1"
DEFAULT_SECONDS = 5.0


class Amounts(BaseModel):
    inputs: list[Sats]
    outputs: list[Sats]
    denomination: Annotated[int, Field(strict=True, gt=0)]
    participants: Annotated[int, Field(strict=True, ge=3)]
    network_fee: Sats

    @model_validator(mode="after")
    def balanced(self):
        if sum(self.inputs) - sum(self.outputs) != self.network_fee:
            raise ValueError("Transaction amounts do not balance")
        if len(self.inputs) < self.participants:
            raise ValueError("Not enough participant inputs")
        if self.outputs.count(self.denomination) != self.participants or len(self.outputs) not in (
            2 * self.participants - 1,
            2 * self.participants,
        ):
            raise ValueError("Transaction does not match the archived CoinJoin shape")
        return self

    @property
    def changes(self) -> list[int]:
        return [value for value in self.outputs if value != self.denomination]

    @property
    def ceiling(self) -> int:
        return max(self.denomination // 200, 10_000)


class Witness(BaseModel):
    input_groups: list[list[Sats]]
    change_indices: list[Sats | None]

    def gains(self, amounts: Amounts) -> list[int]:
        """Validate ownership and balances without trusting solver tolerances/status."""
        n = amounts.participants
        if len(self.input_groups) != n or len(self.change_indices) != n:
            raise ValueError("Incomplete participant assignment")
        if any(not group for group in self.input_groups):
            raise ValueError("Every participant needs an input")
        inputs = [index for group in self.input_groups for index in group]
        changes = [index for index in self.change_indices if index is not None]
        if any(type(index) is not int for index in inputs + changes):
            raise ValueError("Noninteger ownership assignment")
        if sorted(inputs) != list(range(len(amounts.inputs))) or sorted(changes) != list(
            range(len(amounts.changes))
        ):
            raise ValueError("Inputs and changes must each be assigned exactly once")
        gains = [
            amounts.denomination
            + (amounts.changes[change] if change is not None else 0)
            - sum(amounts.inputs[index] for index in group)
            for group, change in zip(self.input_groups, self.change_indices, strict=True)
        ]
        if gains[0] > -amounts.network_fee or any(
            not 0 <= gain <= amounts.ceiling for gain in gains[1:]
        ):
            raise ValueError("Assignment violates conditional maker fee bounds")
        if -gains[0] != amounts.network_fee + sum(gains[1:]):
            raise ValueError("Assignment violates fee conservation")
        return gains[1:]


class Outcome(BaseModel):
    status: Literal["solved", "timeout", "infeasible", "error", "invalid"]
    witness: Witness | None = None
    detail: str = ""

    @model_validator(mode="after")
    def evidence(self):
        if (self.status == "solved") != (self.witness is not None):
            raise ValueError("Only solved outcomes have a witness")
        return self


def _solve(amounts: Amounts, seconds: float, directory: str) -> Outcome:
    import pulp

    n, changes = amounts.participants, amounts.changes
    # Bound model construction as well as native solving. Unknown is never zero.
    if n * (len(amounts.inputs) + len(changes)) > 100_000:
        return Outcome(status="timeout", detail="Model exceeds variable budget")
    model = pulp.LpProblem("conditional_maker_gains", pulp.LpMinimize)
    x = pulp.LpVariable.dicts("input", (range(len(amounts.inputs)), range(n)), cat="Binary")
    c = pulp.LpVariable.dicts("change", (range(n), range(len(changes))), cat="Binary")
    for i in range(len(amounts.inputs)):
        model += pulp.lpSum(x[i][p] for p in range(n)) == 1
    for j in range(len(changes)):
        model += pulp.lpSum(c[p][j] for p in range(n)) == 1
    for p in range(n):
        model += pulp.lpSum(x[i][p] for i in range(len(amounts.inputs))) >= 1
        model += pulp.lpSum(c[p][j] for j in range(len(changes))) <= 1
        gain = (
            amounts.denomination
            + pulp.lpSum(changes[j] * c[p][j] for j in range(len(changes)))
            - pulp.lpSum(value * x[i][p] for i, value in enumerate(amounts.inputs))
        )
        if p == 0:
            model += gain <= -amounts.network_fee
        else:
            model += gain >= 0
            model += gain <= amounts.ceiling
    # Maker labels have no meaning. Order their first inputs, without fixing any
    # ownership heuristic or excluding a feasible decomposition.
    for p in range(2, n):
        for i in range(len(amounts.inputs)):
            model += x[i][p] <= pulp.lpSum(x[j][p - 1] for j in range(i))
    model += 0
    solver = pulp.PULP_CBC_CMD(msg=False, timeLimit=seconds, threads=1)
    solver.tmpDir = directory
    model.solve(solver)

    def assigned(variable):
        value = variable.value()
        if value is None or not math.isfinite(value) or min(abs(value), abs(value - 1)) > 1e-6:
            raise ValueError("No integer incumbent")
        return value > 0.5

    try:
        groups = [[i for i in range(len(amounts.inputs)) if assigned(x[i][p])] for p in range(n)]
        matches = [[j for j in range(len(changes)) if assigned(c[p][j])] for p in range(n)]
        if any(len(match) > 1 for match in matches):
            raise ValueError("Multiple changes assigned to one participant")
        witness = Witness(
            input_groups=groups, change_indices=[match[0] if match else None for match in matches]
        )
        witness.gains(amounts)
        return Outcome(status="solved", witness=witness)
    except ValueError:
        if model.status == pulp.LpStatusInfeasible:
            return Outcome(status="infeasible")
        return Outcome(status="timeout", detail="No validated integer solution within budget")


def _worker_main() -> None:
    import resource

    try:
        request = json.load(sys.stdin)
        seconds = request["seconds"]
        resource.setrlimit(resource.RLIMIT_AS, (1024**3, 1024**3))
        cpu = math.ceil(seconds) + 5
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        outcome = _solve(Amounts.model_validate(request["amounts"]), seconds, os.getcwd())
    except Exception as exc:
        outcome = Outcome(status="error", detail=f"{type(exc).__name__}: {exc}"[:300])
    print(outcome.model_dump_json())


def solve(amounts: Amounts, seconds: float = DEFAULT_SECONDS) -> Outcome:
    """One isolated attempt, including a hard deadline for model building and CBC."""
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("Solver seconds must be positive and finite")
    Path("tmp").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="fee-solver-", dir="tmp") as directory:
        directory = str(Path(directory).resolve())
        process = subprocess.Popen(
            [sys.executable, "-m", "jmstats.solver"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            cwd=directory,
            env={**os.environ, "TMPDIR": directory, "TMP": directory},
        )
        try:
            stdout, stderr = process.communicate(
                json.dumps({"amounts": amounts.model_dump(), "seconds": seconds}),
                timeout=seconds + 5,
            )
        except BaseException as exc:
            # Also stop CBC on Ctrl+C, not just its Python parent.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
            if not isinstance(exc, subprocess.TimeoutExpired):
                raise
            return Outcome(status="timeout", detail="Hard solver deadline exceeded")
        if process.returncode:
            return Outcome(
                status="error", detail=f"Solver exited {process.returncode}: {stderr}"[:300]
            )
        try:
            outcome = Outcome.model_validate_json(stdout)
            if outcome.witness:
                outcome.witness.gains(amounts)
            return outcome
        except ValueError as exc:
            return Outcome(status="error", detail=f"Invalid solver response: {exc}"[:300])


if __name__ == "__main__":
    _worker_main()
