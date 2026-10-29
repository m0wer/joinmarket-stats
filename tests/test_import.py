import json
import sqlite3

from jmstats.archive import Archive
from jmstats.collect import import_analyzer, import_fee_estimates


def test_import_retains_low_values_and_conditional_gains_but_not_filtered_history(tmp_path):
    source = tmp_path / "source.sqlite3"
    database = sqlite3.connect(source)
    database.executescript("""
        CREATE TABLE blocks (height,hash,timestamp,tx_count);
        CREATE TABLE candidates (txid,height,equal_amount,participants);
        CREATE TABLE analyses (txid,payload);
    """)
    database.executemany(
        "INSERT INTO blocks VALUES (?,?,?,?)",
        [
            (1, "01" * 32, 1735689500, 1),  # Earlier than 2025, source filtered these.
            (2, "02" * 32, 1735691000, 1),
        ],
    )
    database.execute("INSERT INTO candidates VALUES (?,?,?,?)", ("ab" * 32, 2, 100, 3))
    database.execute(
        "INSERT INTO analyses VALUES (?,?)",
        (
            "ab" * 32,
            json.dumps(
                {
                    "raw_tx": {"fee": 500, "weight": 1000},
                    "solutions": [{"total_maker_fees": 10}],
                }
            ),
        ),
    )
    database.commit()
    database.close()
    root = tmp_path / "data"
    result = import_analyzer(root, source)
    archive = Archive(root)
    assert result["blocks"] == 1
    assert archive.state.scan_start == 2
    assert set(archive.blocks) == {2}
    assert archive.blocks[2][4] == [["ab" * 32, 100, 3, 500, 1000, 10]]


def test_fee_medians_use_individual_makers_not_transaction_means(tmp_path):
    source = tmp_path / "fees.sqlite3"
    database = sqlite3.connect(source)
    database.executescript("""
        CREATE TABLE candidates (txid,timestamp,equal_amount);
        CREATE TABLE analyses (txid,payload);
    """)
    for txid, denomination, gains in [
        ("first", 100_000, [0, 100]),
        ("second", 200_000, [100, 300, 500]),
    ]:
        database.execute("INSERT INTO candidates VALUES (?,?,?)", (txid, 1736000000, denomination))
        participants = [{"role": "maker", "fee": -gain} for gain in gains]
        participants.append({"role": "taker", "fee": 1000})
        database.execute(
            "INSERT INTO analyses VALUES (?,?)",
            (
                txid,
                json.dumps(
                    {
                        "solutions": [{"participants": participants}],
                    }
                ),
            ),
        )
    database.commit()
    database.close()
    root = tmp_path / "data"
    import_fee_estimates(root, source)
    month = json.loads((root / "fee-estimates.json").read_text())["months"]["2025-01"]
    assert month["median_sats"] == 100
    assert month["median_ppm"] == 1000
    assert month["maker_samples"] == 5
    assert month["transactions"] == 2
