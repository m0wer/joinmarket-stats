import json
import sqlite3

import pytest

from jmstats.archive import Archive, acquire_lock
from jmstats.cli import main
from jmstats.collect import MAINNET_GENESIS, analyze_fees, initialize
from jmstats.fee_analysis import Analysis, SolutionHistory
from jmstats.http import Deferred
from jmstats.solver import METHOD, Outcome, Witness


def captured(**overrides):
    values = dict(
        txid="ab" * 32,
        height=1,
        block_hash="cd" * 32,
        timestamp=1736000000,
        denomination=1_000_000,
        participants=3,
    )
    values.update(overrides)
    return Analysis.capture(
        {
            "txid": values["txid"],
            "fee": 20,
            "vin": [
                {"prevout": {"value": value}} for value in [600_000, 410_050, 1_099_990, 1_899_980]
            ],
            "vout": [{"value": value} for value in [1_000_000] * 3 + [10_000, 100_000, 900_000]],
        },
        **values,
    )


def solved(*args):
    return Outcome(
        status="solved",
        witness=Witness(input_groups=[[0, 1], [2], [3]], change_indices=[0, 1, 2]),
    )


def test_absent_history_does_not_create_state_or_work(tmp_path):
    assert SolutionHistory(tmp_path).records == {}
    assert not (tmp_path / "fee-solutions").exists()


def test_amounts_and_started_are_durable_before_solve_and_interrupt_is_not_retried(
    tmp_path, monkeypatch
):
    from jmstats import fee_analysis

    def interrupted(amounts, seconds):
        loaded = SolutionHistory(tmp_path).records["ab" * 32]
        assert loaded.status == "started"
        assert loaded.amounts == amounts
        raise KeyboardInterrupt

    monkeypatch.setattr(fee_analysis, "solve", interrupted)
    with pytest.raises(KeyboardInterrupt):
        SolutionHistory(tmp_path).run(captured())
    history = SolutionHistory(tmp_path)
    assert history.run(captured()).status == "started"
    monkeypatch.setattr(fee_analysis, "solve", solved)
    assert history.run(captured(), retry=True).maker_gains == [10, 20]
    assert SolutionHistory(tmp_path).records["ab" * 32].maker_gains == [10, 20]


def test_success_and_infeasible_are_not_downgraded_or_retried(tmp_path, monkeypatch):
    from jmstats import fee_analysis

    monkeypatch.setattr(fee_analysis, "solve", solved)
    history = SolutionHistory(tmp_path)
    first = history.run(captured())

    def unexpected(*args):
        raise AssertionError("Successful analysis must not be repeated")

    monkeypatch.setattr(fee_analysis, "solve", unexpected)
    assert history.run(captured(height=2, timestamp=1739000000), retry=True) == first
    other = captured(txid="ef" * 32)
    monkeypatch.setattr(fee_analysis, "solve", lambda *args: Outcome(status="infeasible"))
    history.run(other)
    monkeypatch.setattr(fee_analysis, "solve", unexpected)
    assert history.run(other, retry=True).status == "infeasible"


def test_conflicting_amounts_and_unknown_metadata_fail_closed(tmp_path, monkeypatch):
    from jmstats import fee_analysis

    monkeypatch.setattr(fee_analysis, "solve", solved)
    history = SolutionHistory(tmp_path)
    record = captured()
    history.run(record)
    changed = record.model_copy(
        update={"amounts": record.amounts.model_copy(update={"inputs": [0]})}
    )
    with pytest.raises(ValueError, match="immutable"):
        history.run(changed)
    metadata = tmp_path / "fee-solutions" / "state.json"
    metadata.write_text(json.dumps({"schema_version": 2, "method": METHOD}))
    with pytest.raises(ValueError, match="format"):
        SolutionHistory(tmp_path)
    metadata.unlink()
    with pytest.raises(ValueError, match="missing"):
        SolutionHistory(tmp_path)


def test_solver_lock_is_independent_of_chain_writer_lock(tmp_path):
    with acquire_lock(tmp_path), acquire_lock(tmp_path / "fee-solutions"):
        with pytest.raises(ValueError, match="Another process"):
            acquire_lock(tmp_path / "fee-solutions")


def archived(tmp_path):
    root = tmp_path / "data"
    record = captured()
    initialize(root, 1)
    archive = Archive(root)
    archive.add(
        [
            record.height,
            record.block_hash,
            record.timestamp,
            2,
            [[record.txid, 1_000_000, 3, 20, 1000, None], ["ef" * 32, 1_000_000, 3, 20, 1000, 123]],
        ]
    )
    archive.flush()
    cache = tmp_path / "cache.sqlite3"
    with sqlite3.connect(cache) as db:
        db.execute("CREATE TABLE analyses(txid,payload)")
        db.execute(
            "INSERT INTO analyses VALUES (?,?)",
            (
                record.txid,
                json.dumps(
                    {
                        "raw_tx": {
                            "txid": record.txid,
                            "status": {"block_hash": record.block_hash},
                            "fee": record.amounts.network_fee,
                            "vin": [{"prevout": {"value": v}} for v in record.amounts.inputs],
                            "vout": [{"value": v} for v in record.amounts.outputs],
                        }
                    }
                ),
            ),
        )
    return root, cache


class NoNetwork:
    def get(self, path):
        raise AssertionError("Offline analysis must not query the API")

    text = get


def test_cached_solver_cli_runs_beside_chain_writer_without_modifying_its_files(
    tmp_path, monkeypatch
):
    from jmstats import cli, fee_analysis

    root, cache = archived(tmp_path)
    protected = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    monkeypatch.setattr(cli, "JsonClient", lambda *args, **kwargs: NoNetwork())
    monkeypatch.setattr(fee_analysis, "solve", solved)
    with acquire_lock(root):
        assert (
            main(["--data", str(root), "analyze-fees", "--cache", str(cache), "--cache-only"]) == 0
        )
    assert all(path.read_bytes() == before for path, before in protected.items())
    history = SolutionHistory(root)
    assert set(history.records) == {"ab" * 32}
    assert sum(history.records["ab" * 32].maker_gains) == 30
    result = analyze_fees(root, NoNetwork(), cache=cache, cache_only=True)
    assert result["attempted"] == 0
    assert result["retained_imported"] == 1


def test_explicit_retry_uses_persisted_amounts_without_cache_or_network(tmp_path, monkeypatch):
    from jmstats import fee_analysis

    root, _ = archived(tmp_path)
    monkeypatch.setattr(fee_analysis, "solve", lambda *args: Outcome(status="timeout"))
    SolutionHistory(root).run(captured())
    assert analyze_fees(root, NoNetwork())["attempted"] == 0
    monkeypatch.setattr(fee_analysis, "solve", solved)
    assert analyze_fees(root, NoNetwork(), retry=True)["outcomes"] == {"solved": 1}


def test_solver_throttle_never_updates_chain_cooldown_or_state(tmp_path):
    root, _ = archived(tmp_path)
    before = (root / "state.json").read_bytes()

    class Throttled:
        def text(self, path):
            return MAINNET_GENESIS

        def get(self, path):
            raise Deferred("429", 3600)

    result = analyze_fees(root, Throttled())
    assert result["status"] == "deferred"
    assert result["cooldown"] == 3600
    assert result["attempted"] == 0
    assert (root / "state.json").read_bytes() == before


def test_update_surfaces_solver_execution_error_after_rendering(tmp_path, monkeypatch):
    from jmstats import cli

    root, output = tmp_path / "data", tmp_path / "site"
    initialize(root, 1)
    monkeypatch.setattr(cli, "collect_market", lambda *args: {"status": "ok"})
    monkeypatch.setattr(
        cli,
        "collect_chain",
        lambda *args, **kwargs: {
            "status": "ok",
            "blocks": 1,
            "solver_outcomes": {"error": 1},
        },
    )
    assert main(["--data", str(root), "update", "--output", str(output)]) == 1
    assert (output / "index.html").exists()
    assert (output / "data.json").exists()


def test_update_acquires_solver_lock_before_collection(tmp_path, monkeypatch):
    from jmstats import cli

    root, _ = archived(tmp_path)
    before = (root / "state.json").read_bytes()

    def unexpected(*args, **kwargs):
        raise AssertionError("Collection must not start while a solver writer is active")

    monkeypatch.setattr(cli, "collect_market", unexpected)
    monkeypatch.setattr(cli, "collect_chain", unexpected)
    with acquire_lock(root / "fee-solutions"):
        assert main(["--data", str(root), "update"]) == 1
    assert (root / "state.json").read_bytes() == before
    acquire_lock(root).close()
