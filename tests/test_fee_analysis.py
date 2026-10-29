import json
import sqlite3

import pytest

from jmstats import collect
from jmstats.archive import Archive, acquire_lock
from jmstats.cli import main
from jmstats.collect import MAINNET_GENESIS, analyze_fees, initialize
from jmstats.fee_analysis import FeeStore, capture, load_analyses
from jmstats.http import Deferred
from jmstats.solver import METHOD, Result

TXID, OTHER, BLOCK = "ab" * 32, "ef" * 32, "cd" * 32
TIMESTAMP = 1736000000  # 2025-01
INPUTS = [600_000, 410_050, 1_099_990, 1_899_980]
OUTPUTS = [1_000_000] * 3 + [10_000, 100_000, 900_000]
IDENTIFIED = Result(status="identified", low=30, high=30, gains=[10, 20])


def esplora(txid=TXID, inputs=INPUTS, outputs=OUTPUTS, block=BLOCK):
    return {
        "txid": txid,
        "status": {"block_hash": block},
        "fee": sum(inputs) - sum(outputs),
        "vin": [{"prevout": {"value": v}} for v in inputs],
        "vout": [{"value": v} for v in outputs],
    }


def test_absent_stores_do_not_create_state_or_work(tmp_path):
    assert load_analyses(tmp_path) == {}
    store = FeeStore(tmp_path)
    store.flush()
    assert store.amounts == store.analyses == {}
    assert list(tmp_path.iterdir()) == []


def test_capture_checks_txid_and_reported_fee():
    assert capture(esplora(), TXID) == (INPUTS, OUTPUTS)
    with pytest.raises(ValueError, match="does not match"):
        capture(esplora(), OTHER)
    with pytest.raises(ValueError, match="fee"):
        capture(dict(esplora(), fee=21), TXID)


def test_round_trip_is_compact_and_deterministic(tmp_path):
    store = FeeStore(tmp_path)
    store.add_amounts(TXID, TIMESTAMP, INPUTS, OUTPUTS)
    store.add_analysis(TXID, IDENTIFIED)
    store.flush()
    assert (tmp_path / "amounts" / "2025-01.jsonl").read_text() == json.dumps(
        [TXID, TIMESTAMP, INPUTS, OUTPUTS], separators=(",", ":")
    ) + "\n"
    assert json.loads((tmp_path / "fee-analyses" / "2025-01.jsonl").read_text()) == {
        "txid": TXID,
        "status": "identified",
        "low": 30,
        "high": 30,
        "gains": [10, 20],
    }
    assert json.loads((tmp_path / "fee-analyses" / "state.json").read_text()) == {
        "schema_version": 1,
        "method": METHOD,
    }
    reloaded = FeeStore(tmp_path)
    assert reloaded.analyses == {TXID: IDENTIFIED}
    assert reloaded.get(TXID, 1_000_000, 3).network_fee == 20
    # Identical facts are idempotent and a re-mined copy keeps its first month.
    reloaded.add_amounts(TXID, TIMESTAMP + 40 * 86400, INPUTS, OUTPUTS)
    reloaded.add_analysis(TXID, IDENTIFIED)
    assert reloaded.dirty == set()


def test_amounts_are_durable_before_analyses(tmp_path, monkeypatch):
    from jmstats import fee_analysis

    store = FeeStore(tmp_path)
    store.add_amounts(TXID, TIMESTAMP, INPUTS, OUTPUTS)
    store.add_analysis(TXID, IDENTIFIED)
    original = fee_analysis.atomic_write

    def interrupted(path, text):
        if path.parent.name == "fee-analyses":
            raise KeyboardInterrupt
        original(path, text)

    monkeypatch.setattr(fee_analysis, "atomic_write", interrupted)
    with pytest.raises(KeyboardInterrupt):
        store.flush()
    reloaded = FeeStore(tmp_path)
    assert set(reloaded.amounts) == {TXID}
    assert reloaded.analyses == {}


def test_conflicts_and_unknown_metadata_fail_closed(tmp_path):
    store = FeeStore(tmp_path)
    with pytest.raises(ValueError, match="before"):
        store.add_analysis(TXID, IDENTIFIED)
    store.add_amounts(TXID, TIMESTAMP, INPUTS, OUTPUTS)
    with pytest.raises(ValueError, match="immutable"):
        store.add_amounts(TXID, TIMESTAMP, INPUTS[:-1], OUTPUTS)
    store.add_analysis(TXID, IDENTIFIED)
    with pytest.raises(ValueError, match="migration"):
        store.add_analysis(TXID, Result(status="inconsistent"))
    store.flush()
    analyses_state = tmp_path / "fee-analyses" / "state.json"
    analyses_state.write_text(json.dumps({"schema_version": 1, "method": "other"}))
    with pytest.raises(ValueError, match="method"):
        load_analyses(tmp_path)
    analyses_state.unlink()
    with pytest.raises(ValueError, match="missing"):
        FeeStore(tmp_path)
    analyses_state.write_text(json.dumps({"schema_version": 1, "method": METHOD}))
    amounts_state = tmp_path / "amounts" / "state.json"
    amounts_state.write_text(json.dumps({"schema_version": 2}))
    with pytest.raises(ValueError, match="format"):
        FeeStore(tmp_path)
    amounts_state.write_text(json.dumps({"schema_version": 1}))
    shard = tmp_path / "amounts" / "2025-01.jsonl"
    shard.rename(tmp_path / "amounts" / "2025-02.jsonl")
    with pytest.raises(ValueError, match="misplaced"):
        FeeStore(tmp_path)
    (tmp_path / "amounts" / "2025-02.jsonl").unlink()
    with pytest.raises(ValueError, match="without stored amounts"):
        FeeStore(tmp_path)


def test_fee_lock_is_independent_of_chain_writer_lock(tmp_path):
    with acquire_lock(tmp_path), acquire_lock(tmp_path / "fee-analyses"):
        with pytest.raises(ValueError, match="Another process"):
            acquire_lock(tmp_path / "fee-analyses")


def archived(tmp_path, cached=True):
    root = tmp_path / "data"
    initialize(root, 1)
    archive = Archive(root)
    archive.add(
        [
            1,
            BLOCK,
            TIMESTAMP,
            3,
            [
                [TXID, 1_000_000, 3, 20, 1000, None],
                [OTHER, 1_000_000, 3, 20, 1000, 123],
                ["01" * 32, 50_000, 3, None, 1000, None],
            ],
        ]
    )
    archive.flush()
    cache = tmp_path / "cache.sqlite3"
    with sqlite3.connect(cache) as db:
        db.execute("CREATE TABLE analyses(txid,payload)")
        if cached:
            db.execute(
                "INSERT INTO analyses VALUES (?,?)", (TXID, json.dumps({"raw_tx": esplora()}))
            )
    return root, cache


class NoNetwork:
    def get(self, path):
        raise AssertionError("Offline analysis must not query the API")

    text = get


def test_cached_cli_runs_beside_chain_writer_without_modifying_its_files(tmp_path, monkeypatch):
    from jmstats import cli

    root, cache = archived(tmp_path)
    protected = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    monkeypatch.setattr(cli, "JsonClient", lambda *args, **kwargs: NoNetwork())
    with acquire_lock(root):
        assert (
            main(["--data", str(root), "analyze-fees", "--cache", str(cache), "--cache-only"]) == 0
        )
    assert all(path.read_bytes() == before for path, before in protected.items())
    assert load_analyses(root) == {TXID: IDENTIFIED}
    # Done work is never repeated; the cache miss and the sub-floor tx stay unanalyzed.
    result = analyze_fees(root, NoNetwork(), cache=cache, cache_only=True)
    assert result == {"status": "ok", "outcomes": {}, "sources": {"cache_miss": 1}}


def test_network_amounts_are_stored_and_reused_offline(tmp_path, monkeypatch):
    root, _ = archived(tmp_path, cached=False)

    class Api:
        def text(self, path):
            assert path == "/block-height/0"
            return MAINNET_GENESIS

        def get(self, path):
            txid = path.removeprefix("/tx/")
            return esplora(txid)

    monkeypatch.setattr(collect, "analyze", lambda amounts: (_ for _ in ()).throw(RuntimeError))
    result = analyze_fees(root, Api())
    assert result["outcomes"] == {"error": 2}
    assert result["sources"] == {"network": 2}
    assert set(FeeStore(root).amounts) == {TXID, OTHER}
    assert load_analyses(root) == {}
    monkeypatch.undo()
    result = analyze_fees(root, NoNetwork(), jobs=2)
    assert result["outcomes"] == {"identified": 2}
    assert result["sources"] == {}


def test_cli_exit_code_reports_analysis_errors(tmp_path, monkeypatch):
    from jmstats import cli

    root, cache = archived(tmp_path)
    monkeypatch.setattr(cli, "JsonClient", lambda *args, **kwargs: NoNetwork())
    monkeypatch.setattr(collect, "analyze", lambda amounts: (_ for _ in ()).throw(RuntimeError))
    args = ["--data", str(root), "analyze-fees", "--cache", str(cache), "--cache-only"]
    assert main(args) == 1
    assert load_analyses(root) == {}


def test_wrong_block_and_invalid_scope_fail(tmp_path):
    root, cache = archived(tmp_path)
    with sqlite3.connect(cache) as db:
        db.execute(
            "INSERT INTO analyses VALUES (?,?)",
            (OTHER, json.dumps({"raw_tx": esplora(OTHER, block="ff" * 32)})),
        )
    with pytest.raises(ValueError, match="expected block"):
        analyze_fees(root, NoNetwork(), cache=cache, cache_only=True)
    with pytest.raises(ValueError, match="scope"):
        analyze_fees(root, NoNetwork(), cache_only=True)


def test_transaction_limit_bounds_the_pass(tmp_path):
    root, _ = archived(tmp_path, cached=False)
    store = FeeStore(root)
    store.add_amounts(TXID, TIMESTAMP, INPUTS, OUTPUTS)
    store.add_amounts(OTHER, TIMESTAMP, INPUTS, OUTPUTS)
    store.flush()
    result = analyze_fees(root, NoNetwork(), max_transactions=1)
    assert result == {"status": "bounded", "outcomes": {"identified": 1}, "sources": {}}
    assert len(load_analyses(root)) == 1


def test_fee_throttle_never_updates_chain_cooldown_or_state(tmp_path):
    root, _ = archived(tmp_path, cached=False)
    before = (root / "state.json").read_bytes()

    class Throttled:
        def text(self, path):
            return MAINNET_GENESIS

        def get(self, path):
            raise Deferred("429", 3600)

    result = analyze_fees(root, Throttled())
    assert result["status"] == "deferred"
    assert result["cooldown"] == 3600
    assert result["outcomes"] == {}
    assert (root / "state.json").read_bytes() == before


def test_update_surfaces_fee_analysis_error_after_rendering(tmp_path, monkeypatch):
    from jmstats import cli

    root, output = tmp_path / "data", tmp_path / "site"
    initialize(root, 1)
    monkeypatch.setattr(cli, "collect_market", lambda *args: {"status": "ok"})
    monkeypatch.setattr(
        cli,
        "collect_chain",
        lambda *args, **kwargs: {"status": "ok", "blocks": 1, "solver_outcomes": {"error": 1}},
    )
    assert main(["--data", str(root), "update", "--output", str(output)]) == 1
    assert (output / "index.html").exists()
    assert (output / "data.json").exists()


def test_update_acquires_fee_lock_before_collection(tmp_path, monkeypatch):
    from jmstats import cli

    root, _ = archived(tmp_path)
    before = (root / "state.json").read_bytes()

    def unexpected(*args, **kwargs):
        raise AssertionError("Collection must not start while a fee writer is active")

    monkeypatch.setattr(cli, "collect_market", unexpected)
    monkeypatch.setattr(cli, "collect_chain", unexpected)
    with acquire_lock(root / "fee-analyses"):
        assert main(["--data", str(root), "update"]) == 1
    assert (root / "state.json").read_bytes() == before
    acquire_lock(root).close()
