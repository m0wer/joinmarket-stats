import time

import pytest
from conftest import raw_block

from jmstats.archive import Archive
from jmstats.collect import MAINNET_GENESIS, collect_chain, infer_historical_fees, initialize
from jmstats.fee_analysis import FeeStore, load_analyses
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
        if path.startswith("/blocks/"):
            return [{"height": self.start, "id": self.block_hash}]
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
    original_get = api.get
    api.get = lambda path: (
        {"previousblockhash": "ff" * 32} if path.startswith("/block/") else original_get(path)
    )
    with pytest.raises(ValueError, match="join ancestry"):
        collect_chain(Archive(tmp_path), api, backfill=True, from_height=1)
    assert set(Archive(tmp_path).blocks) == {2}


def test_new_candidates_get_prior_free_fee_evidence(tmp_path):
    initialize(tmp_path, 1)
    api = FakeAPI()
    original_get = api.get
    tx = {
        "txid": api.txid,
        "fee": 100,
        "status": {"block_hash": api.block_hash},
        "vin": [{"prevout": {"value": value}} for value in (101130, 100991, 100982)],
        "vout": [{"value": value} for value in (100000, 100000, 100000, 1000, 1001, 1002)],
    }
    api.get = lambda path: original_get(path) if path.startswith("/blocks/") else tx
    result = collect_chain(Archive(tmp_path), api, backfill=True)
    record = FeeHistory(tmp_path).records[api.txid]
    assert record.status == "inferred"
    assert record.ceiling_sats == 18
    assert record.ceiling_sats == max(record.maker_gains)
    assert result["solver_outcomes"] == {"ambiguous": 1}
    analysis = load_analyses(tmp_path)[api.txid]
    assert (analysis.status, analysis.low, analysis.high) == ("ambiguous", 28, 30)
    assert FeeStore(tmp_path).get(api.txid, 100_000, 3).network_fee == 100
    # Collecting the same block again (after a rollback) reuses the analysis.
    Archive(tmp_path).rollback(0)
    again = collect_chain(Archive(tmp_path), api, backfill=True)
    assert again["solver_outcomes"] == {"retained": 1}


def test_unusable_amounts_do_not_stop_collection(tmp_path):
    initialize(tmp_path, 1)
    result = collect_chain(Archive(tmp_path), FakeAPI(), backfill=True)
    assert result["blocks"] == 1
    assert result["solver_outcomes"] == {"invalid": 1}
    assert not (tmp_path / "amounts").exists()
    assert not (tmp_path / "fee-analyses").exists()


def test_collection_can_leave_solver_to_independent_process(tmp_path):
    initialize(tmp_path, 1)
    result = collect_chain(Archive(tmp_path), FakeAPI(), backfill=True, solve_fees=False)
    assert result["solver_outcomes"] == {}
    assert not (tmp_path / "amounts").exists()
    assert not (tmp_path / "fee-analyses").exists()


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


class ChainAPI:
    """A valid chain of blocks, each with one distinct, analyzable candidate."""

    def __init__(self, count, page_size=15, fork_at=None, amount_base=100_000):
        self.blocks, previous = {}, MAINNET_GENESIS
        self.requests = []
        self.page_size, self.previous = page_size, {}
        for height in range(1, count + 1):
            amount = amount_base + height
            timestamp = 1738368000 + 600 * height + int(fork_at is not None and height >= fork_at)
            raw, block_hash, txid = raw_block(previous=previous, timestamp=timestamp, amount=amount)
            self.blocks[height] = (raw, block_hash, txid, amount)
            self.previous[block_hash] = previous
            previous = block_hash
        self.by_hash = {b[1]: b for b in self.blocks.values()}
        self.by_txid = {b[2]: b for b in self.blocks.values()}

    def text(self, path):
        self.requests.append(("text", path))
        if path == "/block-height/0":
            return MAINNET_GENESIS
        if path == "/blocks/tip/height":
            return str(len(self.blocks) + 6)
        return self.blocks[int(path.removeprefix("/block-height/"))][1]

    def fetch(self, path):
        self.requests.append(("raw", path))
        return self.by_hash[path.split("/")[2]][0]

    def get(self, path):
        self.requests.append(("json", path))
        if path.startswith("/blocks/"):
            last = int(path.removeprefix("/blocks/"))
            return [
                {"height": height, "id": self.blocks[height][1]}
                for height in range(last, max(0, last - self.page_size), -1)
            ]
        if path.startswith("/block/"):
            return {"previousblockhash": self.previous[path.removeprefix("/block/")]}
        _, block_hash, txid, amount = self.by_txid[path.removeprefix("/tx/")]
        return {
            "txid": txid,
            "fee": 100,
            "status": {"block_hash": block_hash},
            "vin": [{"prevout": {"value": amount + v}} for v in (1130, 991, 982)],
            "vout": [{"value": v} for v in [amount] * 3 + [1000, 1001, 1002]],
        }


@pytest.mark.parametrize("page_size", [10, 15])
def test_block_hash_pages_reduce_requests_without_changing_detection_or_coverage(
    tmp_path, page_size
):
    initialize(tmp_path, 1)
    api = ChainAPI(23, page_size=page_size)
    result = collect_chain(Archive(tmp_path), api, backfill=True)
    blocks = Archive(tmp_path).blocks
    assert result["blocks"] == 23
    assert list(blocks) == list(range(1, 24))
    for height, block in blocks.items():
        _, block_hash, txid, amount = api.blocks[height]
        assert block[1] == block_hash
        assert block[3] == 1
        assert block[4][0][:4] == [txid, amount, 3, 100]
    pages = [path for kind, path in api.requests if kind == "json" and path.startswith("/blocks/")]
    assert pages == ["/blocks/10", "/blocks/20", "/blocks/23"]
    assert len([path for kind, path in api.requests if kind == "raw"]) == 23
    assert [path for kind, path in api.requests if kind == "text" and "/block-height/" in path] == [
        "/block-height/0"
    ]


def test_hash_batch_respects_block_limit_without_downloading_extra_blocks(tmp_path):
    initialize(tmp_path, 8)
    api = ChainAPI(25)
    result = collect_chain(Archive(tmp_path), api, backfill=True, max_blocks=3)
    assert result["blocks"] == 3
    assert set(Archive(tmp_path).blocks) == {8, 9, 10}
    pages = [path for kind, path in api.requests if kind == "json" and path.startswith("/blocks/")]
    assert pages == ["/blocks/17"]
    assert len([path for kind, path in api.requests if kind == "raw"]) == 3


@pytest.mark.parametrize("malformed", ["object", "empty", "missing", "duplicate", "hash", "height"])
def test_invalid_hash_batch_is_not_archived(tmp_path, malformed):
    initialize(tmp_path, 1)
    api = ChainAPI(12)
    original_get = api.get

    def get(path):
        page = original_get(path)
        if not path.startswith("/blocks/"):
            return page
        if malformed == "object":
            return {}
        if malformed == "empty":
            return []
        if malformed == "missing":
            return page[:-1]
        if malformed == "duplicate":
            return page + [page[-1]]
        page[-1]["id" if malformed == "hash" else "height"] = "invalid"
        return page

    api.get = get
    with pytest.raises(ValueError, match="block hash batch"):
        collect_chain(Archive(tmp_path), api, backfill=True)
    assert Archive(tmp_path).blocks == {}
    assert not any(kind == "raw" for kind, _ in api.requests)


def test_batch_metadata_does_not_replace_raw_header_validation(tmp_path):
    initialize(tmp_path, 1)
    api = FakeAPI()
    original_get = api.get
    api.get = lambda path: (
        [{"height": 1, "id": "ff" * 32}] if path.startswith("/blocks/") else original_get(path)
    )
    with pytest.raises(ValueError, match="raw header"):
        collect_chain(Archive(tmp_path), api, backfill=True)
    assert Archive(tmp_path).blocks == {}


def test_cached_branch_reorg_still_fails_at_the_next_batch_boundary(tmp_path):
    initialize(tmp_path, 1)
    api = ChainAPI(25, amount_base=1)
    fork = ChainAPI(25, fork_at=3, amount_base=1)
    api.by_hash.update(fork.by_hash)  # The API can still serve the old blocks by hash.
    original_get = api.get
    api.get = lambda path: original_get(path) if path == "/blocks/10" else fork.get(path)
    with pytest.raises(ValueError, match="Block ancestry mismatch"):
        collect_chain(Archive(tmp_path), api, backfill=True)
    blocks = Archive(tmp_path).blocks
    assert set(blocks) == set(range(1, 11))
    assert all(blocks[height][1] == api.blocks[height][1] for height in blocks)


def test_batched_backfill_skips_existing_blocks_and_still_checks_the_join(tmp_path):
    initialize(tmp_path, 5)
    api = ChainAPI(12)
    archive = Archive(tmp_path)
    for height in (5, 6):
        archive.add([height, api.blocks[height][1], 1738368000 + 600 * height, 1, []])
    archive.flush()
    result = collect_chain(Archive(tmp_path), api, backfill=True, from_height=1)
    loaded = Archive(tmp_path)
    assert result["blocks"] == 10
    assert set(loaded.blocks) == set(range(1, 13))
    assert loaded.state.scan_start == 1
    assert ("json", f"/block/{api.blocks[5][1]}") in api.requests
    raw = [path for kind, path in api.requests if kind == "raw"]
    assert len(raw) == 10
    assert all(f"/block/{api.blocks[height][1]}/raw" not in raw for height in (5, 6))


def test_throttled_second_hash_page_checkpoints_the_first_then_resumes(tmp_path):
    initialize(tmp_path, 1)
    api = ChainAPI(25)
    original_get = api.get

    def get(path):
        if path == "/blocks/20":
            raise Deferred("429", 3600)
        return original_get(path)

    api.get = get
    result = collect_chain(Archive(tmp_path), api, backfill=True)
    loaded = Archive(tmp_path)
    assert result["status"] == "deferred"
    assert result["blocks"] == 10
    assert set(loaded.blocks) == set(range(1, 11))
    assert loaded.state.cooldown_until >= time.time() + 3590
    loaded.state.cooldown_until = 0
    loaded.save_state()
    api.get = original_get
    assert collect_chain(loaded, api)["blocks"] == 15
    assert set(Archive(tmp_path).blocks) == set(range(1, 26))
    assert len([path for kind, path in api.requests if kind == "raw"]) == 25


def test_interrupted_batch_is_discarded_and_only_unfinished_blocks_are_downloaded_again(tmp_path):
    initialize(tmp_path, 1)
    api = ChainAPI(12)
    original_get = api.get
    unfinished = api.blocks[3]

    def get(path):
        if path == f"/tx/{unfinished[2]}":
            raise Deferred("Request budget reached")
        return original_get(path)

    api.get = get
    result = collect_chain(Archive(tmp_path), api, backfill=True)
    assert result["status"] == "deferred"
    assert result["blocks"] == 2
    api.get = original_get
    assert collect_chain(Archive(tmp_path), api)["blocks"] == 10
    assert set(Archive(tmp_path).blocks) == set(range(1, 13))
    raw = [path for kind, path in api.requests if kind == "raw"]
    assert len(raw) == 13
    assert raw.count(f"/block/{unfinished[1]}/raw") == 2
    for height in (1, 2):
        assert raw.count(f"/block/{api.blocks[height][1]}/raw") == 1


def test_fee_evidence_is_durable_before_every_chain_checkpoint(tmp_path, monkeypatch):
    import json

    from jmstats import archive as archive_module

    initialize(tmp_path, 1)
    original, checkpoints = archive_module.atomic_write, []

    def checked(path, text):
        if path.parent.name == "chain":
            # A kill right after this write must not leave archived candidates
            # without stored amounts (update never revisits archived blocks).
            stored = FeeStore(tmp_path).amounts
            txids = [c[0] for line in text.splitlines() for c in json.loads(line)[4]]
            assert txids and all(txid in stored for txid in txids)
            checkpoints.append(len(txids))
        original(path, text)

    monkeypatch.setattr(archive_module, "atomic_write", checked)
    result = collect_chain(Archive(tmp_path), ChainAPI(25), backfill=True)
    assert result["blocks"] == 25
    assert result["solver_outcomes"] == {"ambiguous": 25}
    assert checkpoints == [20, 25]
