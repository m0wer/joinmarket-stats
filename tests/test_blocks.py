import pytest
from conftest import raw_block

from jmstats.blocks import scan_block


@pytest.mark.parametrize("segwit", [False, True])
def test_txid_excludes_witness_and_filter_counts(segwit):
    raw, block_hash, txid = raw_block(segwit=segwit)
    result = scan_block(raw)
    assert result["hash"] == block_hash
    assert result["candidates"][0][:4] == [txid, 100_000, 3, None]
    assert result["candidates"][0][4] > 0


def test_persist_small_denominations_but_require_three_participants():
    assert scan_block(raw_block(amount=74_999)[0])["candidates"]
    assert scan_block(raw_block(participants=2)[0])["candidates"] == []


def test_truncated_and_trailing_blocks_fail():
    raw = raw_block()[0]
    for malformed in (raw[:-1], raw + b"extra", bytes(80), raw[:82] + b"\0\0"):
        with pytest.raises(ValueError):
            scan_block(malformed)
