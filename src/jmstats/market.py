"""Validate watcher data and publish aggregates, never identity histories."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from statistics import mean, median
from typing import Literal

from pydantic import BaseModel, Field, model_validator

SATS = 100_000_000
REL_GRID = {
    Decimal(x)
    for x in (
        ".00002",
        ".00005",
        ".0001",
        ".0002",
        ".0005",
        ".001",
        ".002",
        ".005",
        ".01",
        ".02",
        ".05",
        ".1",
    )
}
ABS_GRID = {0, 100, 200, 500, 1000, 2000, 5000, 10000}


class Offer(BaseModel):
    counterparty: str = Field(min_length=1)
    oid: int = Field(ge=0)
    ordertype: Literal["sw0reloffer", "sw0absoffer", "swreloffer", "swabsoffer"]
    minsize: int = Field(ge=0)
    maxsize: int = Field(ge=0)
    cjfee: Decimal = Field(ge=0, le=21_000_000 * SATS)
    txfee: int = Field(default=0, ge=0)
    fidelity_bond_value: int | None = Field(default=0, ge=0)
    fidelity_bond_verified: bool | None = False
    fidelity_bond_verification_stale: bool | None = False
    fidelity_bond_data: dict | None = None

    @model_validator(mode="after")
    def valid_range(self):
        if self.minsize > self.maxsize:
            raise ValueError("minsize exceeds maxsize")
        if "abs" in self.ordertype and self.cjfee != int(self.cjfee):
            raise ValueError("Absolute fee must be integral satoshis")
        return self


class Outpoint(BaseModel):
    txid: str = Field(pattern=r"^[0-9a-f]{64}$")
    vout: int = Field(ge=0)


class Bond(BaseModel):
    counterparty: str
    utxo: Outpoint
    amount: int = Field(default=0, ge=0, le=21_000_000 * SATS)
    locktime: int = Field(ge=0)
    bond_value: int | None = Field(default=0, ge=0)
    verification_valid: bool | None = False
    verification_stale: bool | None = False


class Watcher(BaseModel):
    timestamp: datetime
    offers: list[Offer] = Field(max_length=100_000)
    fidelitybonds: list[Bond] = Field(max_length=100_000)


def summarize(values: list) -> dict:
    return (
        {"mean": mean(values), "median": median(values)}
        if values
        else {"mean": None, "median": None}
    )


def eligible_offers(offers: list[dict], amount: int, absolute=500, relative=0.001) -> list[dict]:
    """NG-default fee policy, public grid, cheapest per nick then positive bond."""
    by_maker = {}
    for offer in offers:
        limit = absolute if offer["kind"] == "absolute" else relative
        if not (offer["min"] <= amount <= offer["max"] and offer["grid"]):
            continue
        if Decimal(offer["fee"]) > Decimal(str(limit)):
            continue
        if not offer["weight"] and Decimal(offer["fee"]) != 0:
            continue
        fee = realized_fee(offer, amount)
        old = by_maker.get(offer["maker"])
        if old is None or fee < realized_fee(old, amount):
            by_maker[offer["maker"]] = offer
    by_bond = {}
    for offer in by_maker.values():
        key = ("bond", offer["bond"]) if offer["weight"] else ("nick", offer["maker"])
        old = by_bond.get(key)
        if old is None or realized_fee(offer, amount) < realized_fee(old, amount):
            by_bond[key] = offer
    return list(by_bond.values())


def realized_fee(offer: dict, amount: int) -> int:
    value = Decimal(offer["fee"])
    if offer["kind"] == "relative":
        value *= amount
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def normalize_market(payload: dict, now: datetime | None = None) -> dict:
    watcher = Watcher.model_validate(payload)
    now = now or datetime.now(UTC)
    if watcher.timestamp.tzinfo is None or (watcher.timestamp - now).total_seconds() > 300:
        raise ValueError("Invalid watcher observation time")
    observed = int(watcher.timestamp.timestamp())
    bonds: dict[tuple, list[Bond]] = {}
    for bond in watcher.fidelitybonds:
        bonds.setdefault((bond.utxo.txid, bond.utxo.vout), []).append(bond)
    verified = {}
    unknown = 0
    for key, entries in bonds.items():
        good = [
            b
            for b in entries
            if b.verification_valid is True and b.verification_stale is False and b.amount > 0
        ]
        if good and len({(b.amount, b.locktime) for b in good}) == 1:
            verified[key] = good[0]
        else:
            unknown += 1
    nicks = {
        nick: index for index, nick in enumerate(sorted({o.counterparty for o in watcher.offers}))
    }
    bond_ids = {key: i for i, key in enumerate(sorted(verified))}
    offers = []
    seen = set()
    for offer in watcher.offers:
        key = (offer.counterparty, offer.oid)
        if key in seen:
            raise ValueError("Duplicate maker offer id")
        seen.add(key)
        data = offer.fidelity_bond_data or {}
        point = (data.get("utxo_txid"), data.get("utxo_vout"))
        valid = (
            offer.fidelity_bond_verified is True
            and offer.fidelity_bond_verification_stale is False
            and point in verified
        )
        kind = "absolute" if "abs" in offer.ordertype else "relative"
        offers.append(
            {
                "maker": nicks[offer.counterparty],
                "kind": kind,
                "min": offer.minsize,
                "max": offer.maxsize,
                "fee": str(offer.cjfee),
                "txfee": offer.txfee,
                "weight": (offer.fidelity_bond_value or 0) if valid else 0,
                "bond": bond_ids.get(point) if valid else None,
                "grid": offer.cjfee in (ABS_GRID if kind == "absolute" else REL_GRID)
                or offer.cjfee == 0,
            }
        )
    curves = []
    for amount in (100_000, 1_000_000, 5_000_000, 10_000_000, 50_000_000, SATS, 5 * SATS):
        selected = eligible_offers(offers, amount)
        fees = [realized_fee(o, amount) for o in selected]
        curves.append(
            {
                "amount": amount,
                "makers": len(selected),
                "fee": summarize(fees),
                "weight": sum(o["weight"] for o in selected),
            }
        )
    locked = [b for b in verified.values() if b.locktime > observed]
    return {
        "as_of": watcher.timestamp.isoformat(),
        "collected_at": now.isoformat(),
        "makers": len(nicks),
        "offer_count": len(offers),
        "bond_count": len(locked),
        "locked_sats": sum(b.amount for b in locked),
        "unknown_bonds": unknown,
        "bonded_makers": len({o["maker"] for o in offers if o["weight"] > 0}),
        "absolute_fees": summarize([int(o.cjfee) for o in watcher.offers if "abs" in o.ordertype]),
        "relative_fees": summarize(
            [float(o.cjfee) for o in watcher.offers if "rel" in o.ordertype]
        ),
        "capacity": curves,
        "offers": offers,
        "locked_distribution": sorted(b.amount for b in locked),
    }
