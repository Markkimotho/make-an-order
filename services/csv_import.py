"""Bulk import from CSV for onboarding a business: items, customers (dukas),
members (students, houses) and tier prices.

Every row is checked before anything is written; rejected rows are
reported with their line number and reason. ``strict`` imports nothing if
any row is rejected. Re-running the same file updates instead of
duplicating: items by SKU (else name), customers by phone, members by
member number, tier prices by tier and SKU. Phones are normalised. Stock
on an existing item is never changed by an import (stock moves only
through receipts and adjustments); for a new item, ``stock`` becomes its
first batch.

Runs in the bound business. Commits only when it writes.
"""
import csv
import io
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation

from models import Customer, InventoryItem, Member, PriceTier, TierPrice, db
from services import stock
from services.phone import InvalidPhone, normalize_ke_phone

KINDS = ("items", "customers", "members", "tier_prices")
REQUIRED = {"items": ("name", "price"), "customers": ("name", "phone"),
            "members": ("member_ref", "name"), "tier_prices": ("tier", "sku", "price")}


class ImportError_(ValueError):
    pass


@dataclass
class Report:
    created: int = 0
    updated: int = 0
    rejected: list = field(default_factory=list)      # [(line, reason)]
    written: bool = False

    def as_text(self):
        lines = [f"created {self.created}, updated {self.updated}, rejected {len(self.rejected)}"
                 + ("" if self.written else " (nothing written)")]
        lines += [f"  line {n}: {why}" for n, why in self.rejected]
        return "\n".join(lines)


def _money(value, name):
    try:
        amount = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, AttributeError):
        raise ImportError_(f"{name} is not a number: {value!r}")
    if amount < 0:
        raise ImportError_(f"{name} cannot be negative")
    return amount


def _int(value, name, default=None):
    if value in (None, ""):
        return default
    try:
        return int(str(value).strip())
    except ValueError:
        raise ImportError_(f"{name} is not a whole number: {value!r}")


def _date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        raise ImportError_(f"expiry_date must be YYYY-MM-DD: {value!r}")


def _yes(value):
    return str(value or "").strip().lower() in ("1", "yes", "y", "true")


def _phone(value, name):
    try:
        return normalize_ke_phone(value)
    except InvalidPhone:
        raise ImportError_(f"{name} is not a Kenyan mobile number: {value!r}")


# --- per kind: check a row (no writes), then apply it ---------------------------------

def _check_items(row):
    data = {"name": row["name"].strip(), "price": _money(row["price"], "price"),
            "sku": (row.get("sku") or "").strip() or None,
            "category": (row.get("category") or "").strip() or "General",
            "cost_price": _money(row["cost_price"], "cost_price") if row.get("cost_price") else None,
            "unit": (row.get("unit") or "").strip() or "pcs",
            "reorder_level": _int(row.get("reorder_level"), "reorder_level", 5),
            "is_returnable_container": _yes(row.get("is_returnable_container"))}
    initial = _int(row.get("stock"), "stock", 0)
    if initial < 0:
        raise ImportError_("stock cannot be negative")
    batch = {"stock": initial, "batch_number": (row.get("batch_number") or "").strip() or None,
             "expiry_date": _date(row.get("expiry_date"))}
    key = ("sku", data["sku"]) if data["sku"] else ("name", data["name"])
    return key, (data, batch)


def _apply_items(payload):
    data, batch = payload
    item = (InventoryItem.query.filter_by(sku=data["sku"]).first() if data["sku"]
            else InventoryItem.query.filter_by(name=data["name"]).first())
    if item is not None:
        for k, v in data.items():
            setattr(item, k, v)
        return False
    item = InventoryItem(**data)
    db.session.add(item)
    db.session.flush()
    if batch["stock"] > 0:
        stock.receive(item, batch["stock"], batch_number=batch["batch_number"],
                      expiry_date=batch["expiry_date"], unit_cost=data["cost_price"],
                      reason="initial_stock", note="CSV import")
    return True


def _check_customers(row):
    phone = _phone(row["phone"], "phone")
    tier_name = (row.get("price_tier") or "").strip() or None
    data = {"name": row["name"].strip(), "phone_number": phone,
            "code": (row.get("code") or "").strip() or phone[-6:],
            "credit_limit": _money(row["credit_limit"], "credit_limit") if row.get("credit_limit")
            else Decimal("0"),
            "credit_term_days": _int(row.get("credit_term_days"), "credit_term_days", 30)}
    if data["credit_term_days"] not in (7, 14, 30):
        raise ImportError_("credit_term_days must be 7, 14 or 30")
    for name in ("account_ref", "route_zone", "national_id", "physical_address", "kra_pin"):
        if row.get(name):
            data[name] = row[name].strip()
    return ("phone", phone), (data, tier_name)


def _apply_customers(payload):
    data, tier_name = payload
    if tier_name:
        tier = PriceTier.query.filter_by(name=tier_name).first()
        if tier is None:
            tier = PriceTier(name=tier_name)
            db.session.add(tier)
            db.session.flush()
        data = {**data, "price_tier_id": tier.id}
    customer = Customer.query.filter_by(phone_number=data["phone_number"]).first()
    if customer is not None:
        for k, v in data.items():
            if k != "code":
                setattr(customer, k, v)
        return False
    db.session.add(Customer(**data))
    db.session.flush()
    return True


def _check_members(row):
    data = {"member_ref": row["member_ref"].strip(), "name": row["name"].strip(),
            "group_label": (row.get("group_label") or "").strip() or None,
            "guardian_phone": _phone(row["guardian_phone"], "guardian_phone")
            if row.get("guardian_phone") else None,
            "secondary_phone": _phone(row["secondary_phone"], "secondary_phone")
            if row.get("secondary_phone") else None}
    probe = Member(member_ref=data["member_ref"], name="x")   # normalises the number
    return ("member_ref", probe.member_ref), data


def _apply_members(data):
    probe = Member(member_ref=data["member_ref"], name="x")
    member = Member.query.filter_by(member_ref=probe.member_ref).first()
    if member is not None:
        for k, v in data.items():
            setattr(member, k, v)
        return False
    db.session.add(Member(**data))
    db.session.flush()
    return True


def _check_tier_prices(row):
    data = {"tier": row["tier"].strip(), "sku": row["sku"].strip(),
            "price": _money(row["price"], "price")}
    if InventoryItem.query.filter_by(sku=data["sku"]).first() is None:
        raise ImportError_(f"no item with SKU {data['sku']}")
    return ("tier_sku", (data["tier"], data["sku"])), data


def _apply_tier_prices(data):
    tier = PriceTier.query.filter_by(name=data["tier"]).first()
    if tier is None:
        tier = PriceTier(name=data["tier"])
        db.session.add(tier)
        db.session.flush()
    item = InventoryItem.query.filter_by(sku=data["sku"]).first()
    row = TierPrice.query.filter_by(price_tier_id=tier.id, inventory_item_id=item.id).first()
    if row is not None:
        row.price = data["price"]
        return False
    db.session.add(TierPrice(price_tier_id=tier.id, inventory_item_id=item.id, price=data["price"]))
    db.session.flush()
    return True


CHECK = {"items": _check_items, "customers": _check_customers, "members": _check_members,
         "tier_prices": _check_tier_prices}
APPLY = {"items": _apply_items, "customers": _apply_customers, "members": _apply_members,
         "tier_prices": _apply_tier_prices}


def import_csv(kind, text, strict=False):
    """Import ``text`` (CSV with a header row) as ``kind``. Returns a Report."""
    if kind not in KINDS:
        raise ImportError_(f"kind must be one of {KINDS}")
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    headers = {h.strip().lower() for h in (reader.fieldnames or [])}
    missing = [h for h in REQUIRED[kind] if h not in headers]
    if missing:
        raise ImportError_(f"missing columns: {', '.join(missing)}")
    report, checked, seen = Report(), [], {}
    for line, raw in enumerate(reader, start=2):
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        if not any(row.values()):
            continue
        absent = [h for h in REQUIRED[kind] if not row.get(h)]
        if absent:
            report.rejected.append((line, f"empty {', '.join(absent)}"))
            continue
        try:
            key, payload = CHECK[kind](row)
        except (ImportError_, ValueError) as e:
            report.rejected.append((line, str(e)))
            continue
        if key in seen:
            report.rejected.append((line, f"same {key[0]} as line {seen[key]}"))
            continue
        seen[key] = line
        checked.append(payload)
    if strict and report.rejected:
        return report
    for payload in checked:
        if APPLY[kind](payload):
            report.created += 1
        else:
            report.updated += 1
    if checked:
        db.session.commit()
        report.written = True
    return report
