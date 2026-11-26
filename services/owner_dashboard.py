"""What the owner sees first: today's money and what needs attention.

One read-only summary per request, shaped by the business's features and
the viewer's role, so a cashier never gets profit figures and a school never
gets stock tiles it does not use. Every figure links to the screen that
explains it.
"""
from datetime import timedelta
from decimal import Decimal

from flask import current_app

from models import UNVERIFIED_METHODS, Batch, InventoryItem, Payment
from services.clock import today_nairobi
from verticals.features import features_for

FINANCE = ("accountant", "admin", "superuser")
STOCK = ("stock_clerk", "pharmacist", "admin", "superuser")


def _money(value):
    return float(Decimal(str(value or 0)).quantize(Decimal("0.01")))


def _money_today(today):
    from services.reports import profit, reconciliation
    rows = profit.daily(today - timedelta(days=6), today)
    day = rows[-1]
    recon = reconciliation.day_report(today)["totals"]
    return {
        "revenue": day["revenue"], "gross_profit": day["gross_profit"],
        "gross_margin_pct": day["gross_margin_pct"], "net_profit": day["net_profit"],
        "expenses": day["expenses"], "sales_count": day["sales_count"],
        "discounts": day["discounts"], "by_method": day["by_method"],
        "cost_missing_lines": day["cost_missing_lines"],
        "last_7_days": [{"date": r["date"], "revenue": r["revenue"],
                         "gross_profit": r["gross_profit"]} for r in rows],
        "cash_variance": recon.get("variance"),
        "variance_over_threshold": bool(recon.get("over_threshold")),
        "open_shifts": recon.get("open_shifts", 0),
    }


def _stock(today, features):
    low = (InventoryItem.query.filter(InventoryItem.stock <= InventoryItem.reorder_level)
           .count())
    out = {"low_stock": low}
    if "batches" in features:
        live = Batch.query.filter(Batch.quantity_on_hand > 0)
        out["expired"] = live.filter(Batch.expiry_date < today).count()
        out["expiring_30"] = live.filter(Batch.expiry_date >= today,
                                         Batch.expiry_date <= today + timedelta(days=30)).count()
    if "recalls" in features:
        out["recalled_on_hand"] = (Batch.query.filter(Batch.recalled_at.isnot(None),
                                                      Batch.quantity_on_hand > 0).count())
    return out


def _unverified():
    rows = Payment.query.filter(Payment.verified.is_(False), Payment.status == "confirmed",
                                Payment.method.in_(UNVERIFIED_METHODS)).all()
    return {"count": len(rows), "total": _money(sum((p.amount for p in rows), Decimal("0")))}


def _backup(now=None):
    from services.backup import backup_status
    return backup_status(current_app.config.get("BACKUP_DIR", "backups"), now=now)


def summary(vertical, role, now=None):
    features = features_for(vertical)
    today = today_nairobi(now)
    out = {"date": today.isoformat(), "vertical": vertical}
    if role in FINANCE and "finance" in features:
        out["money"] = _money_today(today)
        out["unverified_payments"] = _unverified()
    if role in STOCK and "inventory" in features:
        out["stock"] = _stock(today, features)
    if role == "superuser" and "backup" in features:
        out["backup"] = _backup(now)
    out["attention"] = attention(out)
    return out


def attention(s):
    """Plain-language list of things to act on, most urgent first."""
    items = []
    stock, money = s.get("stock", {}), s.get("money", {})
    if stock.get("recalled_on_hand"):
        items.append(("danger", f"{stock['recalled_on_hand']} recalled batch(es) still on the "
                                "shelf: write them off or return them.", "recalls_page"))
    if stock.get("expired"):
        items.append(("danger", f"{stock['expired']} expired batch(es) still in stock.",
                      "batches_page"))
    if money.get("variance_over_threshold"):
        items.append(("danger", "A shift closed today with a cash variance over your limit.",
                      "reconciliation_page"))
    if s.get("backup", {}).get("stale"):
        items.append(("warning", s["backup"]["message"], "backup_page"))
    if s.get("unverified_payments", {}).get("count"):
        u = s["unverified_payments"]
        items.append(("warning", f"{u['count']} typed payment(s) worth KES {u['total']:,.2f} "
                                 "to check against the statement.", "verify_payments_page"))
    if stock.get("low_stock"):
        items.append(("warning", f"{stock['low_stock']} item(s) at or below reorder level.",
                      "purchase_orders_page"))
    if stock.get("expiring_30"):
        items.append(("info", f"{stock['expiring_30']} batch(es) expire within 30 days.",
                      "batches_page"))
    if money.get("cost_missing_lines"):
        items.append(("info", f"{money['cost_missing_lines']} item(s) sold today have no cost "
                              "price, so profit for them is not counted.", "pricing_page"))
    return [{"level": level, "text": text, "endpoint": endpoint}
            for level, text, endpoint in items]
