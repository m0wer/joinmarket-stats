"""Small static artifacts, UTC aggregates, and explicitly incomplete coverage."""

from __future__ import annotations

import csv
import io
import json
import shutil
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from statistics import mean, median

from .archive import Archive, atomic_write, month_of, write_json
from .bond_history import load_bond_history
from .fee_analysis import load_analyses
from .fee_stats import monthly_maker_fees
from .inference import FeeHistory
from .maker import optimize_maker
from .solver import Result


def daily_history(history: list[dict]) -> list[dict]:
    days = {}
    for point in sorted(
        history, key=lambda p: datetime.fromisoformat(p["as_of"].replace("Z", "+00:00"))
    ):
        day = point["as_of"][:10]
        previous = days.get(day)
        current = dict(point)
        current["observations"] = 1
        if previous:
            current["makers"] = max(previous["makers"], point["makers"])
            current["offer_count"] = max(previous["offer_count"], point["offer_count"])
            bonded = [p.get("advertised_bonded_makers") for p in (previous, point)]
            current["advertised_bonded_makers"] = max(
                (n for n in bonded if n is not None), default=None
            )
            current["observations"] += previous["observations"]
        days[day] = current
    return [days[day] for day in sorted(days)]


def monthly_stats(
    archive: Archive | None,
    now: datetime,
    analyses: dict[str, Result] | None = None,
) -> list[dict]:
    blocks = sorted(archive.blocks.values(), key=lambda b: b[0]) if archive else []
    segments = []
    for block in blocks:
        if not segments or block[0] != segments[-1][-1][0] + 1:
            segments.append([])
        segments[-1].append(block)
    spans = [(s[0][2], s[-1][2]) for s in segments]
    groups = {}
    for block in blocks:
        groups.setdefault(month_of(block[2]), []).append(block)
    keys = []
    year, month = 2020, 1
    while (year, month) <= (now.year, now.month):
        keys.append((year, month))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    candidates_by_month = {
        key: [c for b in groups.get(key, []) for c in b[4] if c[1] >= 75_000]
        for key in (f"{y:04}-{m:02}" for y, m in keys)
    }
    maker_fees = monthly_maker_fees(candidates_by_month, analyses or {})
    result = []
    for year, month in keys:
        key = f"{year:04}-{month:02}"
        first = datetime(year, month, 1, tzinfo=UTC)
        following = datetime(year + (month == 12), 1 if month == 12 else month + 1, 1, tzinfo=UTC)
        records = groups.get(key, [])
        candidates = candidates_by_month[key]
        full = any(
            a <= first.timestamp() - 7200 and z >= following.timestamp() + 7200 for a, z in spans
        )
        fees = [c[3] for c in candidates if c[3] is not None]
        rates = [c[3] / ((c[4] + 3) // 4) for c in candidates if c[3] is not None and c[4]]
        result.append(
            {
                "month": key,
                "coverage": "complete" if full else "partial" if records else "unscanned",
                "blocks": len(records),
                "first_height": min((b[0] for b in records), default=None),
                "last_height": max((b[0] for b in records), default=None),
                "count": len(candidates) if records else None,
                "volume_sats": sum(c[1] * c[2] for c in candidates) if records else None,
                "median_size_sats": median(c[1] for c in candidates) if candidates else None,
                "median_participants": median(c[2] for c in candidates) if candidates else None,
                "network_fee_mean": mean(fees) if fees else None,
                "network_fee_samples": len(fees),
                "miner_sat_vb_median": median(rates) if rates else None,
                **maker_fees[key],
            }
        )
    return result


def render(root: Path, output: Path, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    archive = Archive(root) if (root / "state.json").exists() else None
    months = monthly_stats(archive, now, load_analyses(root))
    market_path, history_path = root / "market.json", root / "market-history.json"
    market = json.loads(market_path.read_text()) if market_path.exists() else None
    data = {
        "schema_version": 2,
        "generated_at": now.isoformat(),
        "months": months,
        "market": market,
        "maker_model": optimize_maker(archive, FeeHistory(root).records, load_bond_history(root)),
        "market_history": daily_history(json.loads(history_path.read_text()))
        if history_path.exists()
        else [],
        "provenance": archive.state.provenance if archive else [],
        "scan_start": archive.state.scan_start if archive else None,
        "last_scanned_at": datetime.fromtimestamp(
            max(b[2] for b in archive.blocks.values()), UTC
        ).isoformat()
        if archive and archive.blocks
        else None,
    }
    output.mkdir(parents=True, exist_ok=True)
    for asset in files("jmstats").joinpath("web").iterdir():
        if asset.is_file():
            with asset.open("rb") as source, (output / asset.name).open("wb") as target:
                shutil.copyfileobj(source, target)
    write_json(output / "data.json", data)
    stream = io.StringIO(newline="")
    if months:
        writer = csv.DictWriter(stream, fieldnames=list(months[0]))
        writer.writeheader()
        writer.writerows(months)
    atomic_write(output / "monthly.csv", stream.getvalue())
    atomic_write(output / ".nojekyll", "")
    return {"status": "ok", "output": str(output), "months": len(months)}
