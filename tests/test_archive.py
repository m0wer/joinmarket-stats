import json

import pytest

from jmstats.archive import Archive, write_json
from jmstats.collect import initialize


def record(height, timestamp=1738368000):
    return [height, f"{height:064x}", timestamp, 1, []]


def test_absent_and_unknown_state_fail_closed(tmp_path):
    with pytest.raises(ValueError, match="No chain state"):
        Archive(tmp_path)
    initialize(tmp_path, 10)
    a = Archive(tmp_path)
    with pytest.raises(ValueError, match="Empty"):
        a.next_height()
    state = json.loads((tmp_path / "state.json").read_text())
    state["schema_version"] = 99
    write_json(tmp_path / "state.json", state)
    before = (tmp_path / "state.json").read_bytes()
    with pytest.raises(ValueError, match="Unknown"):
        Archive(tmp_path)
    assert (tmp_path / "state.json").read_bytes() == before


def test_one_dirty_shard_and_nonmonotonic_timestamps(tmp_path):
    initialize(tmp_path, 10)
    a = Archive(tmp_path)
    a.add(record(10, 1738367990))  # January
    a.add(record(11, 1738368010))  # February, January must already be durable
    assert Archive(tmp_path).next_height() == 11
    a.add(record(12, 1738367995))  # A timestamp can go backward across a month
    a.flush()
    loaded = Archive(tmp_path)
    assert loaded.next_height() == 13
    assert len(loaded.shards) == 2


def test_gaps_and_explicit_independent_backfill_floor(tmp_path):
    initialize(tmp_path, 20)
    a = Archive(tmp_path)
    for h in [10, 11, 20, 21]:
        a.add(record(h))
    a.flush()
    assert a.next_height() == 22
    assert a.next_height(True, 10) == 12
    with pytest.raises(ValueError, match="requires"):
        a.next_height(False, 10)
    a.add(record(23))
    with pytest.raises(ValueError, match="gap at 22"):
        a.next_height()


def test_rollback_idempotent_and_duplicate_rejected(tmp_path):
    initialize(tmp_path, 10)
    a = Archive(tmp_path)
    for h in range(10, 14):
        a.add(record(h))
    a.flush()
    a.rollback(11)
    a.rollback(11)
    assert Archive(tmp_path).next_height() == 12
    path = next((tmp_path / "chain").glob("*.jsonl"))
    with path.open("a") as stream:
        stream.write(json.dumps(record(10)) + "\n")
    with pytest.raises(ValueError, match="Duplicate"):
        Archive(tmp_path)


def test_lower_prefix_does_not_authorize_rebuilding_missing_automatic_range(tmp_path):
    initialize(tmp_path, 20)
    a = Archive(tmp_path)
    for height in (10, 11):
        a.add(record(height))
    a.flush()
    with pytest.raises(ValueError, match="Empty automatic range"):
        a.next_height()
    assert a.next_height(True) == 20
