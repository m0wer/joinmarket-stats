import time

import pytest
from conftest import raw_block

from jmstats.archive import Archive
from jmstats.collect import MAINNET_GENESIS, collect_chain, infer_historical_fees, initialize
from jmstats.http import Deferred
from jmstats.inference import FeeHistory


class FakeAPI:
    def __init__(self, start=1):
        self.raw, self.block_hash, self.txid = raw_block()
        self.start = start
        self.rate_limit = False
        self.network = MAINNET_GENESIS

    def text(self, path):
        if path == "/block-height/0":
            return self.network
        if path == "/blocks/tip/height":
            return str(self.start + 6)
        return self.block_hash

    def fetch(self, path):
        return self.raw

    def get(self, path):
        if self.rate_limit:
            raise Deferred("429", 3600)
        return {"txid": self.txid, "fee": 100, "status": {"block_hash": self.block_hash}}


def test_empty_does_not_scan_implicitly_and_explicit_scan_resumes(tmp_path):
    initialize(tmp_path, 1)
    api = FakeAPI()
    with pytest.raises(ValueError, match="Empty"):
        collect_chain(Archive(tmp_path), api)
    result = collect_chain(Archive(tmp_path), api, backfill=True)
    assert result["blocks"] == 1
    assert Archive(tmp_path).next_height() == 2
    assert Archive(tmp_path).blocks[1][4][0][3] == 100


def test_429_mid_block_saves_cooldown_not_incomplete_block(tmp_path):
    initialize(tmp_path, 1)
    api = FakeAPI()
    api.rate_limit = True
    assert collect_chain(Archive(tmp_path), api, backfill=True)["status"] == "deferred"
    loaded = Archive(tmp_path)
    assert loaded.blocks == {}
    assert loaded.state.cooldown_until >= time.time() + 3590
    assert collect_chain(loaded, api, backfill=True)["status"] == "cooldown"


def test_wrong_network_and_reorg_fail_without_overwrite(tmp_path):
    initialize(tmp_path, 1)
    api = FakeAPI()
    api.network = "wrong-chain"
    with pytest.raises(ValueError, match="not Bitcoin mainnet"):
        collect_chain(Archive(tmp_path), api, backfill=True)
    api.network = MAINNET_GENESIS
    collect_chain(Archive(tmp_path), api, backfill=True)
    before = next((tmp_path / "chain").glob("*.jsonl")).read_bytes()
    api.block_hash = "cd" * 32
    with pytest.raises(ValueError, match="Reorganization"):
        collect_chain(Archive(tmp_path), api)
    assert next((tmp_path / "chain").glob("*.jsonl")).read_bytes() == before


def test_backfill_join_checked_and_verified_floor_promoted(tmp_path):
    initialize(tmp_path, 2)
    api = FakeAPI()
    a = Archive(tmp_path)
    a.add([2, api.block_hash, 1738368001, 1, []])
    a.flush()
    original_get = api.get

    def get(path):
        return (
            {"previousblockhash": api.block_hash}
            if path.startswith("/block/")
            else original_get(path)
        )

    api.get = get
    collect_chain(Archive(tmp_path), api, backfill=True, from_height=1, max_blocks=1)
    assert Archive(tmp_path).state.scan_start == 1


def test_bad_backfill_join_does_not_add_block(tmp_path):
    initialize(tmp_path, 2)
    api = FakeAPI()
    a = Archive(tmp_path)
    a.add([2, api.block_hash, 1738368001, 1, []])
    a.flush()
    api.get = lambda path: {"previousblockhash": "ff" * 32}
    with pytest.raises(ValueError, match="join ancestry"):
        collect_chain(Archive(tmp_path), api, backfill=True, from_height=1)
    assert set(Archive(tmp_path).blocks) == {2}


def test_new_candidates_get_prior_free_fee_evidence(tmp_path):
    initialize(tmp_path, 1)
    api = FakeAPI()
    api.get = lambda path: {
        "txid": api.txid,
        "fee": 100,
        "status": {"block_hash": api.block_hash},
        "vin": [{"prevout": {"value": value}} for value in (101130, 100991, 100982)],
        "vout": [{"value": value} for value in (100000, 100000, 100000, 1000, 1001, 1002)],
    }
    collect_chain(Archive(tmp_path), api, backfill=True)
    record = FeeHistory(tmp_path).records[api.txid]
    assert record.status == "inferred"
    assert record.ceiling_sats == 18
    assert record.ceiling_sats == max(record.maker_gains)


def test_explicit_cached_inference_does_not_use_legacy_solver_prices(tmp_path):
    import json
    import sqlite3

    initialize(tmp_path, 1)
    api = FakeAPI()
    archive = Archive(tmp_path)
    archive.add([1, api.block_hash, 1738368000, 1, [[api.txid, 100000, 3, 100, 1000, None]]])
    archive.add([2, "ef" * 32, 1738378800, 1, []])
    archive.flush()
    raw = {
        "txid": api.txid,
        "fee": 100,
        "status": {"block_hash": api.block_hash},
        "vin": [{"prevout": {"value": value}} for value in (101130, 100991, 100982)],
        "vout": [{"value": value} for value in (100000, 100000, 100000, 1000, 1001, 1002)],
    }
    cache = tmp_path / "source.sqlite3"
    database = sqlite3.connect(cache)
    database.execute("CREATE TABLE analyses(txid,payload)")
    database.execute(
        "INSERT INTO analyses VALUES (?,?)",
        (
            api.txid,
            json.dumps(
                {
                    "raw_tx": raw,
                    "solutions": [{"total_maker_fees": 999999999}],
                }
            ),
        ),
    )
    database.commit()
    database.close()

    class NoNetwork:
        def get(self, path):
            raise AssertionError("Cached inference must not query the network")

        text = get

    result = infer_historical_fees(archive, NoNetwork(), cache=cache, cache_only=True)
    assert result["attempted"] == 1
    assert FeeHistory(tmp_path).records[api.txid].ceiling_sats == 18
