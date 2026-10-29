import json
from datetime import UTC, datetime

import pytest

from jmstats.archive import Archive, acquire_lock, write_json
from jmstats.cli import main
from jmstats.collect import initialize
from jmstats.render import monthly_stats, render
from jmstats.solver import Result


def test_utc_coverage_gaps_and_maker_fee_fields(tmp_path):
    initialize(tmp_path, 1)
    a = Archive(tmp_path)
    # One contiguous segment bracketing January, a gap before March.
    a.add([1, "01" * 32, 1577829000, 1, []])
    a.add(
        [
            2,
            "02" * 32,
            1579000000,
            2,
            [
                ["ab" * 32, 100_000, 3, 500, 1000, 100],
                ["cd" * 32, 200_000, 5, None, None, None],
                ["ef" * 32, 74_999, 3, 100, 500, 20],
            ],
        ]
    )
    a.add([3, "03" * 32, 1580529600, 1, []])
    a.add([5, "05" * 32, 1583100000, 1, []])
    a.flush()
    now = datetime(2020, 4, 1, tzinfo=UTC)
    analyses = {
        "ab" * 32: Result(status="identified", low=100, high=100, gains=[40, 60]),
        "ef" * 32: Result(status="identified", low=7, high=7),  # Below the floor.
        "99" * 32: Result(status="identified", low=9, high=9),  # Not archived.
    }
    months = monthly_stats(a, now, analyses)
    january = months[0]
    assert january["coverage"] == "complete"
    assert january["count"] == 2
    assert january["volume_sats"] == 1_300_000
    assert january["median_size_sats"] == 150_000
    assert january["maker_fee_estimate_sats"] == january["maker_fee_exact_sats"] == 100
    assert january["maker_fee_mean_sats"] == 50
    assert january["maker_fee_mean_percent"] == pytest.approx(0.05)
    assert january["maker_fee_median_sats"] == 40
    assert january["maker_fee_median_percent"] == pytest.approx(0.04)
    assert (january["maker_fee_identified"], january["maker_fee_unanalyzed"]) == (1, 1)
    assert january["maker_fee_partial"] is True
    assert not any("ppm" in key for key in january)
    assert months[1]["coverage"] == "partial"
    assert months[3]["count"] is None
    assert months[3]["coverage"] == "unscanned"
    # Legacy single-solution totals (candidate[5]) are not evidence.
    assert monthly_stats(a, now)[0]["maker_fee_estimate_sats"] is None


def test_render_reads_analyses_and_ignores_legacy_fee_estimates(tmp_path):
    import csv

    from jmstats.fee_analysis import FeeStore

    initialize(tmp_path, 1)
    archive = Archive(tmp_path)
    txids = [f"{index + 1:064x}" for index in range(3)]
    archive.add(
        [1, "01" * 32, 1579000000, 3, [[txid, 100_000, 3, None, None, 5] for txid in txids]]
    )
    archive.flush()
    store = FeeStore(tmp_path)
    for txid in txids[:2]:
        store.add_amounts(
            txid,
            1579000000,
            [101_100, 150_000, 1_100_000],
            [100_000] * 3 + [1000, 50_000, 1_000_000],
        )
    store.add_analysis(txids[0], Result(status="identified", low=0, high=0, gains=[0, 0]))
    store.add_analysis(txids[1], Result(status="inconsistent"))
    store.flush()
    write_json(tmp_path / "fee-estimates.json", {"months": {"2020-01": {"median_sats": 123}}})
    output = tmp_path / "site"
    render(tmp_path, output, datetime(2020, 2, 1, tzinfo=UTC))
    data = json.loads((output / "data.json").read_text())
    assert data["schema_version"] == 2
    january, february = data["months"]
    assert january["maker_fee_estimate_sats"] == 0  # A real zero, not unknown.
    assert january["maker_fee_median_sats"] == 0
    assert (january["inconsistent"], january["maker_fee_unanalyzed"]) == (1, 1)
    assert february["maker_fee_estimate_sats"] is None
    exported = list(csv.DictReader((output / "monthly.csv").read_text().splitlines()))
    assert exported[0]["maker_fee_estimate_sats"] == "0"
    assert exported[1]["maker_fee_estimate_sats"] == ""


def test_render_without_chain_and_cli_update_error_keeps_market(tmp_path, monkeypatch):
    from jmstats import cli

    root, output = tmp_path / "data", tmp_path / "site"
    write_json(root / "market.json", {"as_of": "2026-10-29T09:00:00+00:00", "makers": 7})
    monkeypatch.setattr(cli, "collect_market", lambda *args: {"status": "ok"})
    code = main(["--data", str(root), "update", "--output", str(output)])
    assert code == 1  # Missing state is surfaced, not silently bootstrapped.
    data = json.loads((output / "data.json").read_text())
    assert data["market"]["makers"] == 7
    assert not (root / "state.json").exists()
    assert (output / "index.html").exists()


def test_writer_lock_is_exclusive_and_releases(tmp_path):
    import pytest

    first = acquire_lock(tmp_path)
    with pytest.raises(ValueError, match="Another process"):
        acquire_lock(tmp_path)
    first.close()
    acquire_lock(tmp_path).close()


def test_static_site_assets_and_csv(tmp_path):
    root, output = tmp_path / "data", tmp_path / "site"
    initialize(root, 1)
    result = render(root, output, datetime(2020, 2, 2, tzinfo=UTC))
    assert result["months"] == 2
    assert (output / "monthly.csv").read_text().startswith("month,coverage,blocks")
    assert (output / "app.js").exists()
    assert (output / ".nojekyll").exists()


def test_new_archive_renders_unknown_months_from_2020(tmp_path):
    output = tmp_path / "site"
    render(tmp_path / "missing-data", output, datetime(2020, 2, 2, tzinfo=UTC))
    months = json.loads((output / "data.json").read_text())["months"]
    assert [m["month"] for m in months] == ["2020-01", "2020-02"]
    assert all(m["count"] is None and m["coverage"] == "unscanned" for m in months)


def test_maker_estimate_does_not_depend_on_current_orderbook(tmp_path):
    from conftest import model_history

    root, output = tmp_path / "data", tmp_path / "site"
    model_history(root)
    render(root, output, datetime(2026, 10, 29, tzinfo=UTC))
    original = json.loads((output / "data.json").read_text())["maker_model"]
    assert original["status"] == "ok"
    write_json(root / "market.json", {"offers": [{"fee": "0.1", "weight": 10**30}]})
    render(root, output, datetime(2026, 10, 29, tzinfo=UTC))
    assert json.loads((output / "data.json").read_text())["maker_model"] == original
