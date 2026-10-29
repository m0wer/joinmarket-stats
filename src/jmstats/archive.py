"""Versioned ledger, atomic single-shard checkpoints, and conservative recovery."""

from __future__ import annotations

import fcntl
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

DETECTOR = "equal-outputs-half-v1-min1"


def acquire_lock(root: Path):
    """Local CLI exclusion complements GitHub's cross-run concurrency group."""
    root.mkdir(parents=True, exist_ok=True)
    handle = (root / ".writer.lock").open("a")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise ValueError("Another process is using this archive. Wait for it to finish.") from exc
    return handle


class State(BaseModel):
    schema_version: int
    detector: str
    scan_start: int = Field(ge=0)
    cooldown_until: float = 0
    provenance: list[str] = Field(default_factory=list)


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def write_json(path: Path, value) -> None:
    atomic_write(path, json.dumps(value, indent=2, ensure_ascii=True, allow_nan=False) + "\n")


def month_of(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m")


class Archive:
    def __init__(self, root: Path):
        self.root = root
        state_file = root / "state.json"
        if not state_file.exists():
            raise ValueError("No chain state. Run init or import-analyzer explicitly first.")
        self.state = State.model_validate_json(state_file.read_text())
        if self.state.schema_version != 1 or self.state.detector != DETECTOR:
            raise ValueError("Unknown archive schema or detector. Refusing to modify data.")
        self.blocks = {}
        self.shards: dict[str, dict[int, list]] = {}
        self.dirty: str | None = None
        self.pending = 0
        for path in sorted((root / "chain").glob("*.jsonl")):
            records = self.shards.setdefault(path.stem, {})
            for line in path.read_text().splitlines():
                block = json.loads(line)
                self.validate(block)
                height = block[0]
                if height in self.blocks:
                    raise ValueError(f"Duplicate block height {height}")
                if month_of(block[2]) != path.stem:
                    raise ValueError("Block is in the wrong monthly shard")
                records[height] = block
                self.blocks[height] = block

    @staticmethod
    def validate(block: list) -> None:
        if not isinstance(block, list) or len(block) != 5:
            raise ValueError("Invalid block record")
        height, block_hash, timestamp, count, candidates = block
        if not all(type(x) is int and x >= 0 for x in (height, timestamp, count)):
            raise ValueError("Invalid block integers")
        if not isinstance(block_hash, str) or len(block_hash) != 64:
            raise ValueError("Invalid block hash")
        int(block_hash, 16)
        seen = set()
        for candidate in candidates:
            if len(candidate) != 6:
                raise ValueError("Invalid candidate record")
            txid, amount, participants, fee, weight, inferred = candidate
            if not isinstance(txid, str) or len(txid) != 64 or txid in seen:
                raise ValueError("Invalid or duplicate candidate txid")
            int(txid, 16)
            seen.add(txid)
            if not (
                type(amount) is int
                and amount > 0
                and type(participants) is int
                and participants >= 3
            ):
                raise ValueError("Invalid denomination or equal-output count")
            for value in (fee, weight, inferred):
                if value is not None and (type(value) is not int or value < 0):
                    raise ValueError("Invalid candidate fee or weight")

    def save_state(self) -> None:
        write_json(self.root / "state.json", self.state.model_dump())

    def next_height(self, backfill: bool = False, from_height: int | None = None) -> int:
        if from_height is not None and (not backfill or from_height < 0):
            raise ValueError("A nonnegative from-height requires explicit --backfill")
        next_height = self.state.scan_start if from_height is None else from_height
        for height in sorted(self.blocks):
            if height < next_height:
                continue
            if height != next_height:
                if not backfill:
                    raise ValueError(f"Archive gap at {next_height}. Explicit --backfill required.")
                return next_height
            next_height += 1
        if not backfill and next_height == self.state.scan_start:
            raise ValueError("Empty automatic range. Explicit --backfill required.")
        return next_height

    def add(self, block: list) -> None:
        self.validate(block)
        if block[0] in self.blocks:
            if self.blocks[block[0]] != block:
                raise ValueError("Conflicting block, explicit rollback required")
            return
        month = month_of(block[2])
        if self.dirty is not None and month != self.dirty:
            self.flush()
        self.blocks[block[0]] = block
        self.shards.setdefault(month, {})[block[0]] = block
        self.dirty = month
        self.pending += 1
        if self.pending >= 20:
            self.flush()

    def flush(self) -> None:
        if self.dirty is None:
            return
        records = self.shards[self.dirty]
        text = "".join(
            json.dumps(records[h], separators=(",", ":")) + "\n" for h in sorted(records)
        )
        atomic_write(self.root / "chain" / f"{self.dirty}.jsonl", text)
        self.dirty, self.pending = None, 0

    def rollback(self, height: int) -> None:
        """Explicit, idempotent truncation, one atomic shard at a time."""
        self.flush()
        # Interrupted explicit repair can be rerun, ordinary sync still fails closed.
        affected = sorted(self.shards, key=lambda m: max(self.shards[m], default=-1), reverse=True)
        for month in affected:
            removed = [h for h in self.shards[month] if h > height]
            if not removed:
                continue
            for h in removed:
                del self.shards[month][h]
                del self.blocks[h]
            self.dirty = month
            self.flush()
