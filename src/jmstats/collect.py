"""Mainnet-only incremental collection and explicit local corpus import."""

from __future__ import annotations

import json
import sqlite3
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from statistics import median

from pydantic import BaseModel, Field, field_validator

from .archive import DETECTOR, Archive, State, write_json
from .blocks import scan_block
from .bond_history import merge_bond_history
from .http import Deferred, JsonClient
from .inference import FeeHistory, infer_fee_record
from .market import normalize_market
from .snapshots import reduce_bond_snapshot, reduce_snapshot

MAINNET_GENESIS = "000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f"


class HistoryPoint(BaseModel):
    as_of: datetime
    makers: int = Field(ge=0)
    offer_count: int = Field(ge=0)
    advertised_bonded_makers: int | None = Field(default=None, ge=0)
    bonded_makers: int | None = Field(default=None, ge=0)
    reported_locked_sats: int | None = Field(default=None, ge=0)
    locked_sats: int | None = Field(default=None, ge=0)
    bond_count: int = Field(default=0, ge=0)
    verification_available: bool = False

    @field_validator("as_of")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Snapshot observation time requires a timezone")
        return value.astimezone(UTC)


def merge_history(root: Path, incoming: list[dict]) -> int:
    path = root / "market-history.json"
    existing = json.loads(path.read_text()) if path.exists() else []
    by_time = {}
    for item in existing + incoming:
        point = HistoryPoint.model_validate(item).model_dump(mode="json")
        by_time[point["as_of"]] = point
    # Keep hourly observations, including brief offer-spam peaks and empty books.
    ordered = sorted(by_time, key=lambda t: datetime.fromisoformat(t.replace("Z", "+00:00")))
    write_json(path, [by_time[t] for t in ordered])
    return len(by_time)


def import_orderbook_history(root: Path, source: Path) -> dict:
    incoming = [json.loads(line) for line in source.read_text().splitlines() if line.strip()]
    return {"status": "ok", "snapshots": merge_history(root, incoming)}


def import_bond_history(root: Path, source: Path) -> dict:
    incoming = [json.loads(line) for line in source.read_text().splitlines() if line.strip()]
    return {"status": "ok", "snapshots": merge_bond_history(root, incoming)}


def initialize(root: Path, start: int) -> None:
    if (root / "state.json").exists() or any((root / "chain").glob("*.jsonl")):
        raise ValueError("Archive already exists. Never reinitialize existing data.")
    write_json(
        root / "state.json",
        State(schema_version=1, detector=DETECTOR, scan_start=start).model_dump(),
    )


def collect_chain(
    archive: Archive, client: JsonClient, *, backfill=False, max_blocks=200, from_height=None
) -> dict:
    completed = 0
    if archive.state.cooldown_until > time.time():
        return {"status": "cooldown", "until": archive.state.cooldown_until, "blocks": 0}
    height = archive.next_height(backfill, from_height)
    fee_history = FeeHistory(archive.root)
    try:
        if client.text("/block-height/0") != MAINNET_GENESIS:
            raise ValueError("The configured API is not Bitcoin mainnet")
        tip = int(client.text("/blocks/tip/height")) - 6
        # Check every stored segment's endpoint, including imported data above a backfill gap.
        endpoints = [h for h in sorted(archive.blocks) if h + 1 not in archive.blocks]
        for endpoint in endpoints:
            if client.text(f"/block-height/{endpoint}") != archive.blocks[endpoint][1]:
                raise ValueError(f"Reorganization at {endpoint}. Explicit rollback required.")
        while height <= tip and completed < max_blocks:
            if height in archive.blocks:
                height += 1
                continue
            block_hash = client.text(f"/block-height/{height}")
            block = scan_block(client.fetch(f"/block/{block_hash}/raw"))
            if block["hash"] != block_hash:
                raise ValueError("API block hash does not match raw header")
            if height - 1 in archive.blocks and block["previous"] != archive.blocks[height - 1][1]:
                raise ValueError("Block ancestry mismatch. Explicit rollback required.")
            if height + 1 in archive.blocks:
                successor = client.get(f"/block/{archive.blocks[height + 1][1]}")
                if successor["previousblockhash"] != block_hash:
                    raise ValueError("Backfill join ancestry mismatch")
            for candidate in block["candidates"]:
                # Below-floor candidates are retained cheaply; detail lookup is unnecessary.
                if candidate[1] < 75_000:
                    continue
                tx = client.get(f"/tx/{candidate[0]}")
                status = tx.get("status", {})
                if tx.get("txid") != candidate[0] or status.get("block_hash") != block_hash:
                    raise ValueError("Candidate transaction is not in the expected block")
                fee = tx["fee"]
                if type(fee) is not int or fee < 0:
                    raise ValueError("Invalid transaction fee")
                candidate[3] = fee
                if candidate[0] not in fee_history.records:
                    fee_history.add(
                        infer_fee_record(
                            tx,
                            height=height,
                            timestamp=block["timestamp"],
                            denomination=candidate[1],
                            participants=candidate[2],
                        )
                    )
            archive.add(
                [height, block_hash, block["timestamp"], block["tx_count"], block["candidates"]]
            )
            height += 1
            completed += 1
            if completed % 100 == 0:
                print(f"Scanned {completed} blocks, next height {height}", flush=True)
    except Deferred as exc:
        if exc.cooldown:
            archive.state.cooldown_until = time.time() + exc.cooldown
        return {"status": "deferred", "reason": str(exc), "blocks": completed}
    finally:
        fee_history.flush()
        archive.flush()
        if backfill and from_height is not None and from_height < archive.state.scan_start:
            if all(h in archive.blocks for h in range(from_height, archive.state.scan_start)):
                archive.state.scan_start = from_height
        archive.save_state()
    return {"status": "ok", "blocks": completed, "next_height": height, "confirmed_tip": tip}


def infer_historical_fees(
    archive: Archive,
    client: JsonClient,
    *,
    days: int = 365,
    cache: Path | None = None,
    cache_only: bool = False,
    max_transactions: int = 20_000,
) -> dict:
    """Explicit bounded analysis of known historical candidates, never a startup scan."""
    if days < 1 or max_transactions < 1 or (cache_only and cache is None):
        raise ValueError("Invalid historical inference scope")
    if not archive.blocks:
        raise ValueError("Historical inference requires scanned canonical blocks")
    history = FeeHistory(archive.root)
    end = max(b[2] for b in archive.blocks.values()) - 7200
    start = end - days * 86400
    database = sqlite3.connect(cache.resolve().as_uri() + "?mode=ro", uri=True) if cache else None
    attempted, missing, status = 0, 0, "ok"
    network_checked = False
    try:
        for block in sorted(archive.blocks.values(), key=lambda b: b[0]):
            if not start < block[2] <= end:
                continue
            for candidate in block[4]:
                txid, amount, participants, _, _, _ = candidate
                if amount < 75_000 or txid in history.records:
                    continue
                if attempted >= max_transactions:
                    status = "bounded"
                    break
                cached = (
                    database.execute(
                        "SELECT json_extract(payload,'$.raw_tx') FROM analyses WHERE txid=?",
                        (txid,),
                    ).fetchone()
                    if database
                    else None
                )
                if cached and cached[0]:
                    raw = json.loads(cached[0])
                elif cache_only:
                    missing += 1
                    continue
                else:
                    if not network_checked:
                        if archive.state.cooldown_until > time.time():
                            raise Deferred("API cooldown still active")
                        if client.text("/block-height/0") != MAINNET_GENESIS:
                            raise ValueError("The configured API is not Bitcoin mainnet")
                        network_checked = True
                    raw = client.get(f"/tx/{txid}")
                if raw.get("txid") != txid or raw.get("status", {}).get("block_hash") != block[1]:
                    raise ValueError("Historical transaction does not match the canonical archive")
                history.add(
                    infer_fee_record(
                        raw,
                        height=block[0],
                        timestamp=block[2],
                        denomination=amount,
                        participants=participants,
                    )
                )
                attempted += 1
                if attempted % 500 == 0:
                    print(f"Analyzed {attempted} historical CJ candidates", flush=True)
            if status == "bounded":
                break
    except Deferred as exc:
        status = "deferred"
        if exc.cooldown:
            archive.state.cooldown_until = max(
                archive.state.cooldown_until, time.time() + exc.cooldown
            )
            archive.save_state()
    finally:
        history.flush()
        if database:
            database.close()
    counts = Counter(r.status for r in history.records.values() if start < r.timestamp <= end)
    return {
        "status": status,
        "attempted": attempted,
        "cache_missing": missing,
        "outcomes": dict(counts),
    }


def collect_market(root: Path, url: str) -> dict:
    # Separate budget and host, a chain rate limit must not suppress the orderbook.
    payload = JsonClient(url, budget=5).get("")
    snapshot = reduce_snapshot(payload)
    # Raw market-health totals remain useful even if a malformed advertisement
    # prevents detailed offer analysis during an attack.
    merge_history(root, [snapshot])
    merge_bond_history(root, [reduce_bond_snapshot(payload)])
    market = normalize_market(payload)
    market["advertised_bonded_makers"] = snapshot["advertised_bonded_makers"]
    market["reported_locked_sats"] = snapshot["reported_locked_sats"]
    write_json(root / "market.json", market)
    return {"status": "ok", "as_of": market["as_of"], "makers": market["makers"]}


def import_analyzer(root: Path, source: Path, start: int | None = None) -> dict:
    """Explicit import of local full-block scans, not a seed-followed graph."""
    database = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        # The source discarded candidates before this timestamp, so earlier block rows
        # are not evidence of a completed scan under our detector.
        cutoff = int(datetime(2025, 1, 1, tzinfo=UTC).timestamp())
        first = database.execute(
            "SELECT min(height) FROM blocks WHERE timestamp>=?", (cutoff,)
        ).fetchone()[0]
        if first is None:
            raise ValueError("No supported analyzer corpus")
        if not (root / "state.json").exists():
            initialize(root, first if start is None else start)
        archive = Archive(root)
        candidates = {}
        query = """SELECT c.txid,c.height,c.equal_amount,c.participants,a.payload
                   FROM candidates c LEFT JOIN analyses a ON a.txid=c.txid
                   WHERE c.height>=? ORDER BY c.height,c.txid"""
        for txid, height, amount, participants, payload in database.execute(query, (first,)):
            analysis = json.loads(payload) if payload else {}
            raw = analysis.get("raw_tx", {})
            solutions = analysis.get("solutions", [])
            inferred = solutions[0]["total_maker_fees"] if solutions else None
            candidates.setdefault(height, []).append(
                [
                    txid,
                    amount,
                    participants,
                    raw.get("fee"),
                    raw.get("weight"),
                    inferred,
                ]
            )
        count = 0
        try:
            for height, block_hash, timestamp, tx_count in database.execute(
                "SELECT height,hash,timestamp,tx_count FROM blocks WHERE height>=? ORDER BY height",
                (first,),
            ):
                archive.add([height, block_hash, timestamp, tx_count, candidates.get(height, [])])
                count += 1
        finally:
            archive.flush()
        note = (
            f"Imported joinmarket_analyzer full-block corpus from height {first}; "
            "conditional solver gains: max(0.5% denomination, 10,000 sat), "
            "one feasible solution, uniqueness not proven. Detector: " + DETECTOR
        )
        if note not in archive.state.provenance:
            archive.state.provenance.append(note)
        archive.save_state()
        return {"status": "ok", "blocks": count, "first_height": first}
    finally:
        database.close()


def import_fee_estimates(root: Path, source: Path) -> dict:
    """Explicitly reduce conditional solver results to monthly per-maker medians."""
    database = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
    groups = {}
    query = """SELECT c.timestamp,c.equal_amount,a.payload FROM candidates c
               JOIN analyses a ON a.txid=c.txid WHERE c.equal_amount>=75000"""
    try:
        for timestamp, amount, payload in database.execute(query):
            solutions = json.loads(payload).get("solutions", [])
            if not solutions:
                continue
            month = datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m")
            absolute, relative, transactions = groups.setdefault(month, [[], [], []])
            for participant in solutions[0]["participants"]:
                if participant["role"] != "maker":
                    continue
                fee = -participant["fee"]
                if type(fee) is not int or fee < 0:
                    raise ValueError("Invalid conditional maker gain")
                absolute.append(fee)
                relative.append(fee / amount * 1e6)
            transactions.append(1)
    finally:
        database.close()
    months = {
        month: {
            "median_sats": median(a),
            "median_ppm": median(r),
            "maker_samples": len(a),
            "transactions": len(t),
        }
        for month, (a, r, t) in sorted(groups.items())
        if a
    }
    write_json(
        root / "fee-estimates.json", {"kind": "conditional_solver_net_gain", "months": months}
    )
    return {"status": "ok", "months": len(months)}
