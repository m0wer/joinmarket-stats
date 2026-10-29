from copy import deepcopy

from jmstats.collect import import_orderbook_history
from jmstats.render import daily_history
from jmstats.snapshots import reduce_snapshot


def test_legacy_snapshot_reports_principal_without_inventing_verification(watcher_payload):
    for offer in watcher_payload["offers"]:
        offer.pop("fidelity_bond_verified")
        offer.pop("fidelity_bond_verification_stale")
    for bond in watcher_payload["fidelitybonds"]:
        bond.pop("verification_valid")
        bond.pop("verification_stale")
    point = reduce_snapshot(watcher_payload)
    assert point["advertised_bonded_makers"] == 1
    assert point["reported_locked_sats"] == 100_000_000
    assert point["locked_sats"] is None
    assert point["bonded_makers"] is None
    assert not point["verification_available"]


def test_spam_peaks_and_empty_books_are_retained(watcher_payload):
    payload = deepcopy(watcher_payload)
    for i in range(3000):
        payload["offers"].append({"counterparty": f"spam-{i}", "fidelity_bond_value": 0})
    attack = reduce_snapshot(payload)
    assert attack["makers"] == 3001
    assert attack["advertised_bonded_makers"] == 1
    empty = {"timestamp": "2026-10-29T10:00:00+00:00", "offers": [], "fidelitybonds": []}
    empty = reduce_snapshot(empty)
    assert empty["makers"] == 0
    day = daily_history([attack, empty])[0]
    assert day["makers"] == 3001
    assert day["reported_locked_sats"] == 0
    assert day["observations"] == 2


def test_outpoint_deduplication_and_conflicts(watcher_payload):
    duplicate = deepcopy(watcher_payload["fidelitybonds"][0])
    watcher_payload["fidelitybonds"].append(duplicate)
    assert reduce_snapshot(watcher_payload)["reported_locked_sats"] == 100_000_000
    duplicate["amount"] += 1
    assert reduce_snapshot(watcher_payload)["reported_locked_sats"] == 0


def test_reduced_history_import_is_idempotent_and_keeps_hourly_points(tmp_path, watcher_payload):
    import json

    first = reduce_snapshot(watcher_payload)
    second = dict(first, as_of="2026-10-29T10:00:00+00:00", makers=5000)
    source = tmp_path / "history.jsonl"
    source.write_text("\n".join(json.dumps(x) for x in (first, second)))
    root = tmp_path / "data"
    assert import_orderbook_history(root, source)["snapshots"] == 2
    assert import_orderbook_history(root, source)["snapshots"] == 2
    saved = json.loads((root / "market-history.json").read_text())
    assert len(saved) == 2
    assert saved[0]["locked_sats"] == 100_000_000


def test_raw_totals_survive_invalid_offer_analysis(tmp_path, watcher_payload, monkeypatch):
    import json

    import pytest

    from jmstats import collect
    from jmstats.archive import write_json

    write_json(tmp_path / "market.json", {"retained": True})
    watcher_payload["offers"][0]["maxsize"] = 1

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def get(self, path):
            return watcher_payload

    monkeypatch.setattr(collect, "JsonClient", Client)
    with pytest.raises(ValueError):
        collect.collect_market(tmp_path, "https://example.invalid")
    assert json.loads((tmp_path / "market-history.json").read_text())[0]["makers"] == 1
    assert json.loads((tmp_path / "market.json").read_text()) == {"retained": True}


def test_daily_last_capital_uses_time_not_iso_lexical_order():
    base = {"makers": 1, "offer_count": 1, "advertised_bonded_makers": 1}
    earlier = dict(base, as_of="2026-10-29T09:00:00Z", reported_locked_sats=100)
    later = dict(base, as_of="2026-10-29T09:00:00.1Z", reported_locked_sats=200)
    assert daily_history([later, earlier])[0]["reported_locked_sats"] == 200
