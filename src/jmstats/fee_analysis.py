"""Immutable transaction amounts and method-versioned maker fee analyses.

``data/amounts/<month>.jsonl`` holds ``[txid, timestamp, inputs, outputs]`` per
CoinJoin candidate, so a new analysis method never needs a download. Amounts are
facts about a txid; denomination and participants come from the chain archive.
``data/fee-analyses/<month>.jsonl`` holds one solver result per txid for the
method recorded in its ``state.json``. Unknown formats or methods fail closed.

One fee writer owns both directories (lock in ``data/fee-analyses``),
independently of the chain writer. Readers never initialize absent state.
"""

from __future__ import annotations

import json
from pathlib import Path

from .archive import atomic_write, month_of, write_json
from .solver import METHOD, Amounts, Result

AMOUNTS_STATE = {"schema_version": 1}
ANALYSES_STATE = {"schema_version": 1, "method": METHOD}
TXID = frozenset("0123456789abcdef")


def capture(tx: dict, txid: str) -> tuple[list[int], list[int]]:
    """Input and output amounts from an Esplora transaction, checked against its fee."""
    if tx.get("txid") != txid:
        raise ValueError("Transaction ID does not match the archived candidate")
    inputs = [vin["prevout"]["value"] for vin in tx["vin"]]
    outputs = [out["value"] for out in tx["vout"]]
    if any(type(v) is not int or v < 0 for v in inputs + outputs):
        raise ValueError("Invalid transaction amounts")
    if sum(inputs) - sum(outputs) != tx["fee"]:
        raise ValueError("Transaction amounts do not match the reported fee")
    return inputs, outputs


def _metadata(directory: Path, expected: dict) -> list[Path]:
    paths = sorted(directory.glob("*.jsonl"))
    state = directory / "state.json"
    if state.exists():
        found = json.loads(state.read_text())
        if found != expected:
            raise ValueError(
                f"Unknown {directory.name} format or method: found {found}, expected"
                f" {expected} (regenerate with analyze-fees)"
            )
    elif paths:
        raise ValueError(f"{directory.name} metadata is missing")
    return paths


def load_analyses(root: Path) -> dict[str, Result]:
    """Read-only view for rendering; never creates state."""
    directory = root / "fee-analyses"
    records: dict[str, Result] = {}
    for path in _metadata(directory, ANALYSES_STATE):
        for line in path.read_text().splitlines():
            record = json.loads(line)
            txid = record.pop("txid")
            if txid in records:
                raise ValueError("Duplicate maker fee analysis")
            records[txid] = Result.model_validate(record)
    return records


class FeeStore:
    """The single fee writer's view: amounts and analyses, flushed in that order."""

    def __init__(self, root: Path):
        self.amounts_dir = root / "amounts"
        self.analyses_dir = root / "fee-analyses"
        self.amounts: dict[str, tuple[int, list[int], list[int]]] = {}
        self.months: dict[str, str] = {}
        for path in _metadata(self.amounts_dir, AMOUNTS_STATE):
            for line in path.read_text().splitlines():
                txid, timestamp, inputs, outputs = json.loads(line)
                if (
                    len(txid) != 64
                    or not set(txid) <= TXID
                    or type(timestamp) is not int
                    or month_of(timestamp) != path.stem
                    or txid in self.amounts
                    or any(type(v) is not int or v < 0 for v in inputs + outputs)
                    or sum(inputs) < sum(outputs)
                ):
                    raise ValueError("Invalid, duplicate, or misplaced transaction amounts")
                self.amounts[txid] = (timestamp, inputs, outputs)
                self.months[txid] = path.stem
        self.analyses = load_analyses(root)
        if not self.analyses.keys() <= self.amounts.keys():
            raise ValueError("Maker fee analyses without stored amounts")
        self.dirty: set[str] = set()
        self.pending = 0

    def get(self, txid: str, denomination: int, participants: int) -> Amounts | None:
        stored = self.amounts.get(txid)
        if stored is None:
            return None
        _, inputs, outputs = stored
        return Amounts(
            inputs=inputs, outputs=outputs, denomination=denomination, participants=participants
        )

    def get_safely(self, txid: str, denomination: int, participants: int) -> Amounts | None:
        """Stored amounts that do not match the archived shape are reported, not analyzed."""
        try:
            return self.get(txid, denomination, participants)
        except ValueError as exc:
            print(f"Stored amounts for {txid} do not match the candidate: {exc}", flush=True)
            return None

    def add_amounts(self, txid: str, timestamp: int, inputs: list[int], outputs: list[int]):
        previous = self.amounts.get(txid)
        if previous is not None:
            # A re-mined transaction keeps its first provenance; amounts never change.
            if previous[1:] != (inputs, outputs):
                raise ValueError("Conflicting immutable transaction amounts")
            return
        self.amounts[txid] = (timestamp, inputs, outputs)
        self.months[txid] = month_of(timestamp)
        self._touch(txid)

    def add_analysis(self, txid: str, result: Result) -> None:
        if txid not in self.amounts:
            raise ValueError("Store transaction amounts before their analysis")
        previous = self.analyses.get(txid)
        if previous is not None:
            if previous != result:
                raise ValueError("Conflicting deterministic analysis; explicit migration required")
            return
        self.analyses[txid] = result
        self._touch(txid)

    def _touch(self, txid: str) -> None:
        self.dirty.add(self.months[txid])
        self.pending += 1
        if self.pending >= 50:
            self.flush()

    def flush(self) -> None:
        if not self.dirty:
            return
        by_month: dict[str, list[str]] = {}
        for txid, month in self.months.items():
            if month in self.dirty:
                by_month.setdefault(month, []).append(txid)
        # Amounts first: an interruption can leave amounts without analyses (the
        # transaction is simply unanalyzed), never analyses without amounts.
        if not (self.amounts_dir / "state.json").exists():
            write_json(self.amounts_dir / "state.json", AMOUNTS_STATE)
        for month, txids in by_month.items():
            ordered = sorted(txids, key=lambda t: (self.amounts[t][0], t))
            atomic_write(
                self.amounts_dir / f"{month}.jsonl",
                "".join(
                    json.dumps([t, *self.amounts[t]], separators=(",", ":")) + "\n" for t in ordered
                ),
            )
        analyzed = {m: [t for t in ts if t in self.analyses] for m, ts in by_month.items()}
        if any(analyzed.values()) and not (self.analyses_dir / "state.json").exists():
            write_json(self.analyses_dir / "state.json", ANALYSES_STATE)
        for month, txids in analyzed.items():
            if not txids:
                continue
            ordered = sorted(txids, key=lambda t: (self.amounts[t][0], t))
            atomic_write(
                self.analyses_dir / f"{month}.jsonl",
                "".join(
                    json.dumps(
                        {"txid": t, **self.analyses[t].model_dump(exclude_defaults=True)},
                        separators=(",", ":"),
                    )
                    + "\n"
                    for t in ordered
                ),
            )
        self.dirty.clear()
        self.pending = 0
