"""Price-prior-free fee evidence for one-input-per-participant CoinJoins.

This finds the minimum compatible maximum maker net gain across every feasible
input/change assignment. It is a conditional lower bound on the effective fee
ceiling, not a recovered private taker configuration. Makers are assumed not to
lose sats in the transaction. Multi-input ownership remains unknown.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .archive import atomic_write, month_of, write_json

METHOD = "single-input-minimax-v1"
MAX_PARTICIPANTS = 32
MAX_EDGE_VISITS = 100_000


class FeeRecord(BaseModel):
    txid: str = Field(pattern=r"^[0-9a-f]{64}$")
    height: int = Field(ge=0)
    timestamp: int = Field(ge=0)
    denomination: int = Field(gt=0)
    makers: int = Field(ge=2)
    status: Literal["inferred", "multi_input", "too_large", "infeasible", "budget", "invalid"]
    ceiling_sats: int | None = Field(default=None, ge=0)
    maker_gains: list[int] | None = None
    network_fee: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def evidence_consistent(self):
        if self.status == "inferred":
            if self.ceiling_sats is None or self.maker_gains is None or self.network_fee is None:
                raise ValueError("Incomplete fee evidence")
            if len(self.maker_gains) != self.makers or min(self.maker_gains) < 0:
                raise ValueError("Invalid maker gain witness")
            if max(self.maker_gains) != self.ceiling_sats:
                raise ValueError("Fee ceiling does not match its witness")
        elif self.ceiling_sats is not None or self.maker_gains is not None:
            raise ValueError("Unknown fees must not be recorded as zero")
        return self


class _BudgetExceeded(Exception):
    pass


def infer_fee_record(
    raw: dict,
    *,
    height: int,
    timestamp: int,
    denomination: int,
    participants: int,
    max_edge_visits: int = MAX_EDGE_VISITS,
) -> FeeRecord:
    record = FeeRecord(
        txid=raw["txid"],
        height=height,
        timestamp=timestamp,
        denomination=denomination,
        makers=participants - 1,
        status="invalid",
    )
    try:
        inputs = [vin["prevout"]["value"] for vin in raw["vin"]]
        outputs = [out["value"] for out in raw["vout"]]
    except (KeyError, TypeError):
        return record
    if any(type(value) is not int or value < 0 for value in inputs + outputs):
        return record
    fee = sum(inputs) - sum(outputs)
    if fee < 0 or type(raw.get("fee")) is not int or raw["fee"] != fee:
        return record
    record.network_fee = fee
    if Counter(outputs)[denomination] != participants:
        return record
    changes = [value for value in outputs if value != denomination]
    if len(changes) not in (participants - 1, participants):
        return record
    if len(inputs) != participants:
        record.status = "multi_input"
        return record
    if participants > MAX_PARTICIPANTS:
        record.status = "too_large"
        return record
    if len(changes) == participants - 1:
        changes.append(0)
    # Rows/columns are input UTXOs and change outputs, including a missing change.
    gains = [[denomination + change - value for change in changes] for value in inputs]
    thresholds = sorted({gain for row in gains for gain in row if gain >= 0})
    takers = [
        (i, c) for i in range(participants) for c in range(participants) if -gains[i][c] >= fee
    ]
    visits = 0

    def feasible(limit: int):
        nonlocal visits
        edges = [[c for c, gain in enumerate(row) if 0 <= gain <= limit] for row in gains]
        for taker, taker_change in takers:
            makers = sorted(
                (i for i in range(participants) if i != taker), key=lambda i: len(edges[i])
            )
            if any(not edges[i] for i in makers):
                continue
            matching = [-1] * participants

            def augment(row: int, seen: set[int], assignment: list[int], excluded: int) -> bool:
                nonlocal visits
                for column in edges[row]:
                    visits += 1
                    if visits > max_edge_visits:
                        raise _BudgetExceeded
                    if column == excluded or column in seen:
                        continue
                    seen.add(column)
                    if assignment[column] == -1 or augment(
                        assignment[column], seen, assignment, excluded
                    ):
                        assignment[column] = row
                        return True
                return False

            if all(augment(row, set(), matching, taker_change) for row in makers):
                return taker, taker_change, matching
        return None

    if not thresholds or not takers:
        record.status = "infeasible"
        return record
    try:
        if feasible(thresholds[-1]) is None:
            record.status = "infeasible"
            return record
        low, high = 0, len(thresholds) - 1
        while low < high:
            middle = (low + high) // 2
            if feasible(thresholds[middle]) is None:
                low = middle + 1
            else:
                high = middle
        witness = feasible(thresholds[low])
    except _BudgetExceeded:
        record.status = "budget"
        return record
    if witness is None:
        raise AssertionError("Monotone matching lost its feasible witness")
    taker, taker_change, matching = witness
    maker_gains = sorted(gains[row][column] for column, row in enumerate(matching) if row != -1)
    # Validate using original integer amounts, not an optimizer's float output.
    if len(maker_gains) != participants - 1 or -gains[taker][taker_change] != fee + sum(
        maker_gains
    ):
        raise AssertionError("Fee assignment violates integer conservation")
    record.status = "inferred"
    record.ceiling_sats = thresholds[low]
    record.maker_gains = maker_gains
    return FeeRecord.model_validate(record.model_dump())


class FeeHistory:
    """Atomic derived-evidence shards; absent evidence never starts a backfill."""

    def __init__(self, root: Path):
        self.root = root / "fee-inferences"
        self.records: dict[str, FeeRecord] = {}
        self.shards: dict[str, dict[str, FeeRecord]] = {}
        self.dirty: str | None = None
        self.pending = 0
        state = self.root / "state.json"
        paths = sorted(self.root.glob("*.jsonl"))
        if state.exists():
            metadata = json.loads(state.read_text())
            if metadata != {"schema_version": 1, "method": METHOD}:
                raise ValueError("Unknown CJ fee-inference format")
        elif paths:
            raise ValueError("CJ fee-inference metadata is missing")
        for path in paths:
            shard = self.shards.setdefault(path.stem, {})
            for line in path.read_text().splitlines():
                record = FeeRecord.model_validate_json(line)
                if month_of(record.timestamp) != path.stem or record.txid in self.records:
                    raise ValueError("Duplicate or misplaced CJ fee evidence")
                self.records[record.txid] = record
                shard[record.txid] = record

    def add(self, record: FeeRecord) -> None:
        month = month_of(record.timestamp)
        if self.dirty is not None and self.dirty != month:
            self.flush()
        previous = self.records.get(record.txid)
        if previous is not None:
            if previous != record:
                raise ValueError("Conflicting CJ fee evidence; explicit migration required")
            return
        self.records[record.txid] = record
        self.shards.setdefault(month, {})[record.txid] = record
        self.dirty = month
        self.pending += 1
        if self.pending >= 20:
            self.flush()

    def flush(self) -> None:
        if self.dirty is None:
            return
        state = self.root / "state.json"
        if not state.exists():
            write_json(state, {"schema_version": 1, "method": METHOD})
        records = sorted(self.shards[self.dirty].values(), key=lambda r: (r.height, r.txid))
        atomic_write(
            self.root / f"{self.dirty}.jsonl", "".join(r.model_dump_json() + "\n" for r in records)
        )
        self.dirty, self.pending = None, 0
