"""Durable solver attempts and reusable transaction amounts, separate from chain writes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .archive import atomic_write, month_of, write_json
from .solver import DEFAULT_SECONDS, METHOD, Amounts, Outcome, solve


class Analysis(BaseModel):
    txid: str = Field(pattern=r"^[0-9a-f]{64}$")
    height: int = Field(ge=0)
    block_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    timestamp: int = Field(ge=0)
    method: Literal["conditional-cbc-v1"] = METHOD
    seconds: float = Field(default=DEFAULT_SECONDS, gt=0, allow_inf_nan=False)
    amounts: Amounts | None = None
    status: Literal["started", "solved", "timeout", "infeasible", "error", "invalid"]
    outcome: Outcome | None = None

    @model_validator(mode="after")
    def consistent(self):
        if self.status == "started":
            if self.amounts is None or self.outcome is not None:
                raise ValueError("Started attempts need cached amounts, not a result")
        elif self.outcome is None or self.outcome.status != self.status:
            raise ValueError("Analysis status does not match its outcome")
        if self.status != "invalid" and self.amounts is None:
            raise ValueError("Analysis is missing reusable transaction amounts")
        if self.outcome and self.outcome.witness:
            self.outcome.witness.gains(self.amounts)
        return self

    @property
    def maker_gains(self) -> list[int] | None:
        if self.outcome and self.outcome.witness:
            return self.outcome.witness.gains(self.amounts)
        return None

    @classmethod
    def capture(
        cls,
        raw: dict,
        *,
        txid: str,
        height: int,
        block_hash: str,
        timestamp: int,
        denomination: int,
        participants: int,
        seconds: float = DEFAULT_SECONDS,
    ) -> Analysis:
        if raw.get("txid") != txid:
            raise ValueError("Solver transaction ID does not match the archived candidate")
        if raw.get("status", {}).get("block_hash", block_hash) != block_hash:
            raise ValueError("Solver transaction is not in the archived block")
        metadata = dict(
            txid=txid, height=height, block_hash=block_hash, timestamp=timestamp, seconds=seconds
        )
        try:
            amounts = Amounts(
                inputs=[vin["prevout"]["value"] for vin in raw["vin"]],
                outputs=[out["value"] for out in raw["vout"]],
                denomination=denomination,
                participants=participants,
                network_fee=raw["fee"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            return cls(
                **metadata,
                status="invalid",
                outcome=Outcome(status="invalid", detail=str(exc)[:300]),
            )
        return cls(**metadata, amounts=amounts, status="started")


class SolutionHistory:
    """A single solver writer, atomic monthly shards, immutable txid-keyed facts.

    Callers own the solver lock. Readers never initialize absent state. A durable
    started record prevents an interrupted solve from silently restarting.
    """

    def __init__(self, root: Path):
        self.root = root / "fee-solutions"
        self.records: dict[str, Analysis] = {}
        self.shards: dict[str, dict[str, Analysis]] = {}
        state = self.root / "state.json"
        paths = sorted(self.root.glob("*.jsonl"))
        if state.exists():
            if json.loads(state.read_text()) != {"schema_version": 1, "method": METHOD}:
                raise ValueError("Unknown maker fee-solution format")
        elif paths:
            raise ValueError("Maker fee-solution metadata is missing")
        for path in paths:
            shard = self.shards.setdefault(path.stem, {})
            for line in path.read_text().splitlines():
                record = Analysis.model_validate_json(line)
                if month_of(record.timestamp) != path.stem or record.txid in self.records:
                    raise ValueError("Duplicate or misplaced maker fee solution")
                self.records[record.txid] = record
                shard[record.txid] = record

    def _write(self, record: Analysis) -> None:
        month = month_of(record.timestamp)
        self.records[record.txid] = record
        shard = self.shards.setdefault(month, {})
        shard[record.txid] = record
        state = self.root / "state.json"
        if not state.exists():
            write_json(state, {"schema_version": 1, "method": METHOD})
        atomic_write(
            self.root / f"{month}.jsonl",
            "".join(
                record.model_dump_json() + "\n"
                for record in sorted(shard.values(), key=lambda r: (r.height, r.txid))
            ),
        )

    def run(self, record: Analysis, *, retry: bool = False) -> Analysis:
        previous = self.records.get(record.txid)
        if previous:
            if previous.amounts is not None and record.amounts != previous.amounts:
                raise ValueError("Conflicting immutable transaction amounts")
            if not (retry and previous.status in ("started", "timeout", "error")):
                return previous
            # The same tx may be mined in a different block after a reorg. Its
            # amounts and result remain valid; retain the original provenance.
            record = previous.model_copy(update={"seconds": record.seconds})
        if record.status in ("invalid", "solved", "infeasible"):
            self._write(record)
            return record
        record = Analysis.model_validate(
            {**record.model_dump(), "status": "started", "outcome": None}
        )
        self._write(record)
        try:
            outcome = solve(record.amounts, record.seconds)
        except Exception as exc:
            outcome = Outcome(status="error", detail=f"{type(exc).__name__}: {exc}"[:300])
        finished = Analysis.model_validate(
            {**record.model_dump(), "status": outcome.status, "outcome": outcome.model_dump()}
        )
        self._write(finished)
        return finished
