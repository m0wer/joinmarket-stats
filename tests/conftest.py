import struct
from datetime import UTC, datetime
from pathlib import Path

import pytest

from jmstats.archive import Archive
from jmstats.blocks import hash256
from jmstats.bond_history import merge_bond_history
from jmstats.collect import MAINNET_GENESIS, initialize
from jmstats.inference import FeeHistory, FeeRecord


def transaction(*, segwit=False, amount=100_000, participants=3):
    body = bytes([participants])
    for i in range(participants):
        body += bytes([i + 1]) * 32 + bytes(4) + b"\0" + b"\xff" * 4
    body += bytes([participants * 2])
    for value in [amount] * participants + list(range(1000, 1000 + participants)):
        body += struct.pack("<Q", value) + b"\x01\x51"
    stripped = b"\x02\0\0\0" + body + bytes(4)
    raw = (
        (b"\x02\0\0\0\0\x01" + body + b"\x01\x01\x01" * participants + bytes(4))
        if segwit
        else stripped
    )
    return raw, hash256(stripped)[::-1].hex()


def raw_block(*, previous=MAINNET_GENESIS, timestamp=1738368000, **kwargs):
    tx, txid = transaction(**kwargs)
    header = (
        struct.pack("<I", 1)
        + bytes.fromhex(previous)[::-1]
        + bytes(32)
        + struct.pack("<III", timestamp, 0, 0)
    )
    return header + b"\x01" + tx, hash256(header)[::-1].hex(), txid


def model_history(root: Path, count: int = 200, high_gain: int = 1001):
    """A complete synthetic archive with separately persisted price/bond facts."""
    initialize(root, 1)
    archive, fees = Archive(root), FeeHistory(root)
    end = int(datetime(2026, 10, 29, 9, tzinfo=UTC).timestamp())
    archive.add([1, "01" * 32, 1735680000, 1, []])
    archive.add([2, "02" * 32, 1736000000, 1, [["ab" * 32, 100_000, 3, 100, 1000, 400]]])
    archive.add([3, "03" * 32, end - (count + 1) * 3600, 1, []])
    points = []
    for index in range(count):
        timestamp = end - (count - index) * 3600
        height, txid = index + 4, f"{index + 100:064x}"
        gain = 101 if index < count * 0.8 else high_gain
        archive.add(
            [height, f"{height:064x}", timestamp, 1, [[txid, 1_000_000, 4, 100, 1000, 999_999_999]]]
        )
        fees.add(
            FeeRecord(
                txid=txid,
                height=height,
                timestamp=timestamp,
                denomination=1_000_000,
                makers=3,
                status="inferred",
                ceiling_sats=gain,
                maker_gains=[0, 0, gain],
                network_fee=100,
            )
        )
        points.append(
            {
                "as_of": datetime.fromtimestamp(timestamp - 60, UTC).isoformat(),
                "weights": [100_000_000] * 10,
                "mapping_complete": True,
                "verified_count": 10,
            }
        )
    archive.add([count + 4, f"{count + 4:064x}", end + 7200, 1, []])
    archive.flush()
    fees.flush()
    merge_bond_history(root, points)
    return archive, fees.records, points


@pytest.fixture
def watcher_payload():
    point = {"txid": "ab" * 32, "vout": 0}
    return {
        "timestamp": "2026-10-29T09:00:00+00:00",
        "offers": [
            {
                "counterparty": "maker-a",
                "oid": 0,
                "ordertype": "sw0reloffer",
                "minsize": 100_000,
                "maxsize": 100_000_000,
                "cjfee": "0.001",
                "txfee": 0,
                "fidelity_bond_value": 10_000,
                "fidelity_bond_verified": True,
                "fidelity_bond_verification_stale": False,
                "fidelity_bond_data": {"utxo_txid": point["txid"], "utxo_vout": 0},
            }
        ],
        "fidelitybonds": [
            {
                "counterparty": "maker-a",
                "utxo": point,
                "amount": 100_000_000,
                "locktime": 1900000000,
                "bond_value": 10_000,
                "verification_valid": True,
                "verification_stale": False,
            }
        ],
    }
