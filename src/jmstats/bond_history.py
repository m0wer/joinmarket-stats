"""Versioned historical competition observations, separate from advertised fees."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

from .archive import write_json

KIND = "active_reported_bond_weights_v1"


class BondPoint(BaseModel):
    as_of: datetime
    weights: list[int] = Field(max_length=100_000)
    mapping_complete: bool
    verified_count: int = Field(default=0, ge=0)
    missing_links: int = Field(default=0, ge=0)
    conflicting_links: int = Field(default=0, ge=0)
    excluded_invalid: int = Field(default=0, ge=0)

    @field_validator("as_of")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Historical bond observation requires a timezone")
        return value.astimezone(UTC)

    @field_validator("weights", mode="before")
    @classmethod
    def positive_weights(cls, value: list[int]) -> list[int]:
        if not isinstance(value, list) or any(
            type(weight) is not int or weight <= 0 for weight in value
        ):
            raise ValueError("Only positive active bond weights belong in competition history")
        return sorted(value)


def load_bond_history(root: Path) -> list[dict]:
    path = root / "bond-history.json"
    if not path.exists():
        return []  # No evidence, not permission to download or reconstruct history.
    payload = json.loads(path.read_text())
    if payload.get("schema_version") != 1 or payload.get("kind") != KIND:
        raise ValueError("Unknown historical bond format, refusing to reinterpret it")
    return [BondPoint.model_validate(point).model_dump(mode="json") for point in payload["points"]]


def merge_bond_history(root: Path, incoming: list[dict]) -> int:
    points = {}
    for item in load_bond_history(root) + incoming:
        point = BondPoint.model_validate(item).model_dump(mode="json")
        points[point["as_of"]] = point
    ordered = sorted(points, key=lambda t: datetime.fromisoformat(t.replace("Z", "+00:00")))
    write_json(
        root / "bond-history.json",
        {
            "schema_version": 1,
            "kind": KIND,
            "points": [points[t] for t in ordered],
        },
    )
    return len(points)
