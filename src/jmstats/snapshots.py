"""Stdlib-only reducer, runnable over SSH without installing this application.

Only aggregates leave the source host. Raw totals intentionally include spam.
Absent historical verification remains unknown, never verified zero capital.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path


def reduce_snapshot(payload: dict) -> dict:
    observed = datetime.fromisoformat(payload["timestamp"].replace("Z", "+00:00"))
    if observed.tzinfo is None:
        raise ValueError("Snapshot must have an explicit timezone")
    offers, bonds = payload["offers"], payload["fidelitybonds"]
    if not isinstance(offers, list) or not isinstance(bonds, list):
        raise ValueError("Invalid snapshot arrays")
    makers = {o["counterparty"] for o in offers}
    advertised_bonded = {
        o["counterparty"] for o in offers if (o.get("fidelity_bond_value") or 0) > 0
    }
    verified_bonded = {
        o["counterparty"]
        for o in offers
        if (o.get("fidelity_bond_value") or 0) > 0
        and o.get("fidelity_bond_verified") is True
        and o.get("fidelity_bond_verification_stale") is False
    }
    available = any("verification_valid" in b for b in bonds) or any(
        "fidelity_bond_verified" in o for o in offers
    )
    by_point = {}
    for bond in bonds:
        point = bond["utxo"]
        key = (point["txid"], point["vout"])
        by_point.setdefault(key, []).append(bond)
    reported, verified, count = 0, 0, 0
    for entries in by_point.values():
        values = {(b.get("amount", 0), b["locktime"]) for b in entries}
        if len(values) != 1:
            continue
        amount, locktime = next(iter(values))
        if type(amount) is not int or not 0 < amount <= 21_000_000 * 100_000_000:
            continue
        if locktime <= observed.timestamp():
            continue
        reported += amount
        count += 1
        if any(
            b.get("verification_valid") is True and b.get("verification_stale") is False
            for b in entries
        ):
            verified += amount
    return {
        "as_of": observed.astimezone(UTC).isoformat(),
        "makers": len(makers),
        "offer_count": len(offers),
        "advertised_bonded_makers": len(advertised_bonded),
        "bonded_makers": len(verified_bonded) if available else None,
        "reported_locked_sats": reported,
        "locked_sats": verified if available else None,
        "bond_count": count,
        "verification_available": available,
    }


def reduce_bond_snapshot(payload: dict) -> dict:
    """Historical active bond competition, with no advertised price/size inputs."""
    observed = datetime.fromisoformat(payload["timestamp"].replace("Z", "+00:00"))
    if observed.tzinfo is None:
        raise ValueError("Snapshot must have an explicit timezone")
    by_point, by_maker = {}, {}
    for bond in payload["fidelitybonds"]:
        key = bond["utxo"]["txid"], bond["utxo"]["vout"]
        by_point.setdefault(key, []).append(bond)
        by_maker.setdefault(bond["counterparty"], set()).add(key)
    weights, verified = {}, set()
    missing, conflicts, invalid = 0, 0, 0
    for offer in payload["offers"]:
        weight = offer.get("fidelity_bond_value") or 0
        if weight <= 0:
            continue
        if type(weight) is not int:
            raise ValueError("Bond weight must be an integer")
        if (
            offer.get("fidelity_bond_verified") is False
            or offer.get("fidelity_bond_verification_stale") is True
        ):
            invalid += 1
            continue
        data = offer.get("fidelity_bond_data") or {}
        key = data.get("utxo_txid"), data.get("utxo_vout")
        if key not in by_point:
            candidates = by_maker.get(offer["counterparty"], set())
            if len(candidates) != 1:
                missing += 1
                continue
            key = next(iter(candidates))
        bonds = [
            b
            for b in by_point[key]
            if b.get("verification_valid") is not False and b.get("verification_stale") is not True
        ]
        if not bonds:
            invalid += 1
            continue
        values = {b.get("bond_value") for b in bonds}
        if values != {weight} or (key in weights and weights[key] != weight):
            conflicts += 1
            continue
        # Expired bonds can retain decaying positive selection weight.
        weights[key] = weight
        if (
            offer.get("fidelity_bond_verified") is True
            and offer.get("fidelity_bond_verification_stale") is False
            and any(
                b.get("verification_valid") is True and b.get("verification_stale") is False
                for b in bonds
            )
        ):
            verified.add(key)
    return {
        "as_of": observed.astimezone(UTC).isoformat(),
        "weights": sorted(weights.values()),
        "mapping_complete": missing == 0 and conflicts == 0,
        "verified_count": len(verified),
        "missing_links": missing,
        "conflicting_links": conflicts,
        "excluded_invalid": invalid,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Reduce snapshots without copying raw data")
    parser.add_argument("directory", type=Path)
    parser.add_argument("--bonds-only", action="store_true")
    args = parser.parse_args()
    paths = sorted(args.directory.glob("*.json"))
    if not paths:
        raise ValueError("No snapshot files")
    for path in paths:
        try:
            payload = json.loads(path.read_text())
            point = reduce_bond_snapshot(payload) if args.bonds_only else reduce_snapshot(payload)
        except Exception as exc:
            raise ValueError(f"Invalid snapshot {path.name}: {exc}") from exc
        print(json.dumps(point, separators=(",", ":")), flush=True)
    print(f"Reduced {len(paths)} snapshots, raw identities and proofs omitted.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
