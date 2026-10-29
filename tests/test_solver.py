import signal
import subprocess

import pytest

from jmstats.solver import Amounts, Witness, solve


def example():
    return Amounts(
        inputs=[600_000, 410_050, 1_099_990, 1_899_980],
        outputs=[1_000_000] * 3 + [10_000, 100_000, 900_000],
        denomination=1_000_000,
        participants=3,
        network_fee=20,
    )


def test_general_solver_handles_multi_input_taker_with_change():
    amounts = example()
    outcome = solve(amounts)
    assert outcome.status == "solved", outcome.detail
    assert sorted(outcome.witness.gains(amounts)) == [10, 20]
    assert outcome.witness.input_groups[0] == [0, 1]


def test_general_solver_handles_multi_input_maker():
    amounts = example().model_copy(update={"inputs": [1_010_050, 600_000, 499_990, 1_899_980]})
    outcome = solve(amounts)
    assert outcome.status == "solved", outcome.detail
    assert sorted(outcome.witness.gains(amounts)) == [10, 20]
    assert any(len(group) == 2 for group in outcome.witness.input_groups[1:])


def test_solved_zero_gain_is_not_unknown():
    amounts = Amounts(
        inputs=[101_100, 150_000, 1_100_000],
        outputs=[100_000] * 3 + [1000, 50_000, 1_000_000],
        denomination=100_000,
        participants=3,
        network_fee=100,
    )
    outcome = solve(amounts)
    assert outcome.status == "solved", outcome.detail
    assert outcome.witness.gains(amounts) == [0, 0]


def test_proven_infeasibility_is_not_zero():
    amounts = Amounts(
        inputs=[50_000, 50_000, 300_000],
        outputs=[100_000] * 3 + [10_000] * 3,
        denomination=100_000,
        participants=3,
        network_fee=70_000,
    )
    outcome = solve(amounts)
    assert outcome.status == "infeasible"
    assert outcome.witness is None


def test_original_integer_amounts_validate_witness_and_taker_change():
    witness = Witness(input_groups=[[0, 1], [2], [3]], change_indices=[0, 1, 2])
    assert sum(witness.gains(example())) == 30
    with pytest.raises(ValueError, match="exactly once"):
        witness.model_copy(update={"input_groups": [[0, 1], [2], [2]]}).gains(example())
    with pytest.raises(ValueError, match="bounds"):
        witness.model_copy(update={"change_indices": [1, 0, 2]}).gains(example())
    with pytest.raises(ValueError, match="balance"):
        Amounts.model_validate({**example().model_dump(), "network_fee": 21})


def test_one_feasible_total_does_not_prove_uniqueness():
    amounts = Amounts(
        inputs=[60_000, 50_050, 100_990, 101_980],
        outputs=[100_000] * 3 + [10_000, 1000, 2000],
        denomination=100_000,
        participants=3,
        network_fee=20,
    )
    first = Witness(input_groups=[[0, 1], [2], [3]], change_indices=[0, 1, 2])
    second = Witness(input_groups=[[0, 1], [2], [3]], change_indices=[1, 0, 2])
    assert sum(first.gains(amounts)) == 30
    assert sum(second.gains(amounts)) == 9030


def test_solver_supports_missing_taker_change_and_thirty_participants():
    n, denomination = 30, 10_000_000
    changes = [index * 1_000_000 + 1000 for index in range(1, n)]
    gains = list(range(1, n))
    amounts = Amounts(
        inputs=[denomination + 1000 + sum(gains)]
        + [denomination + change - gain for change, gain in zip(changes, gains, strict=True)],
        outputs=[denomination] * n + changes,
        denomination=denomination,
        participants=n,
        network_fee=1000,
    )
    outcome = solve(amounts)
    assert outcome.status == "solved", outcome.detail
    assert sum(outcome.witness.gains(amounts)) == sum(gains)
    assert outcome.witness.change_indices[0] is None


def test_hard_timeout_kills_python_and_cbc_process_group(monkeypatch):
    from jmstats import solver

    class Process:
        pid = 12345
        calls = 0

        def communicate(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired("solver", 10)
            return "", ""

    killed = []
    monkeypatch.setattr(solver.subprocess, "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(solver.os, "killpg", lambda *args: killed.append(args))
    outcome = solve(example())
    assert outcome.status == "timeout"
    assert outcome.witness is None
    assert killed == [(12345, signal.SIGKILL)]


@pytest.mark.parametrize("seconds", [0, -1, float("nan"), float("inf")])
def test_invalid_solver_budget(seconds):
    with pytest.raises(ValueError, match="seconds"):
        solve(example(), seconds)
