"""Bounds-checked raw Bitcoin block reader and a deliberately heuristic filter.

Adapted from joinmarket_analyzer/tmp/rawblocks.py (MIT, see THIRD_PARTY_LICENSES).
No prevouts, addresses, or ownership assignments are needed for discovery.
"""

from __future__ import annotations

from collections import Counter
from hashlib import sha256


def hash256(raw: bytes) -> bytes:
    return sha256(sha256(raw).digest()).digest()


class Reader:
    def __init__(self, raw: bytes):
        self.raw = raw
        self.pos = 0

    def take(self, length: int) -> bytes:
        start = self.pos
        self.pos += length
        if length < 0 or self.pos > len(self.raw):
            raise ValueError("Truncated Bitcoin block")
        return self.raw[start : self.pos]

    def uint(self, length: int) -> int:
        return int.from_bytes(self.take(length), "little")

    def compact(self) -> int:
        prefix = self.uint(1)
        if prefix < 253:
            return prefix
        length, minimum = {253: (2, 253), 254: (4, 65536), 255: (8, 2**32)}[prefix]
        value = self.uint(length)
        if value < minimum or value > len(self.raw):
            raise ValueError("Invalid CompactSize")
        return value


def scan_block(raw: bytes) -> dict:
    reader = Reader(raw)
    header = reader.take(80)
    count = reader.compact()
    if count < 1:
        raise ValueError("Empty block")
    candidates = []
    for _ in range(count):
        start = reader.pos
        version = reader.take(4)
        segwit = reader.raw[reader.pos : reader.pos + 1] == b"\0"
        if segwit and reader.take(2) != b"\0\1":
            raise ValueError("Unknown witness flag")
        body_start = reader.pos
        inputs = reader.compact()
        if inputs < 1:
            raise ValueError("Transaction has no inputs")
        coinbase = False
        for _ in range(inputs):
            previous, index = reader.take(32), reader.uint(4)
            coinbase |= previous == bytes(32) and index == 0xFFFFFFFF
            reader.take(reader.compact())
            reader.take(4)
        outputs = reader.compact()
        if outputs < 1:
            raise ValueError("Transaction has no outputs")
        values = []
        for _ in range(outputs):
            amount = reader.uint(8)
            if amount > 21_000_000 * 100_000_000:
                raise ValueError("Invalid Bitcoin output value")
            values.append(amount)
            reader.take(reader.compact())
        body_end = reader.pos
        if segwit:
            for _ in range(inputs):
                for _ in range(reader.compact()):
                    reader.take(reader.compact())
        locktime = reader.take(4)
        amount, equal = Counter(values).most_common(1)[0]
        participants = (outputs + 1) // 2
        if coinbase or participants < 3 or inputs < participants:
            continue
        if equal != participants or amount < 1:
            continue
        stripped = version + raw[body_start:body_end] + locktime
        weight = len(stripped) * 3 + reader.pos - start
        candidates.append([hash256(stripped)[::-1].hex(), amount, participants, None, weight, None])
    if reader.pos != len(raw):
        raise ValueError("Trailing bytes in block")
    return {
        "hash": hash256(header)[::-1].hex(),
        "previous": header[4:36][::-1].hex(),
        "timestamp": int.from_bytes(header[68:72], "little"),
        "tx_count": count,
        "candidates": candidates,
    }
