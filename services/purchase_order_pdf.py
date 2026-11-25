"""A purchase order as a PDF to send to the supplier (ReportLab).

Header: the business name with its KRA PIN and PPB premises licence when
set, the supplier's details, PO number and date. Then one row per line
(item, quantity, unit cost, line total), the order total and who prepared
it. Totals are Decimal sums, never floats.
"""
from decimal import Decimal
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

CENT = Decimal("0.01")


def _esc(text):
    return (str(text) if text not in (None, "") else "-").replace("&", "&amp;").replace(
        "<", "&lt;").replace(">", "&gt;")


def money(value):
    return f"{Decimal(value).quantize(CENT):,.2f}"


def quantity(value):
    """12.000 -> '12', 2.500 -> '2.5'."""
    text = f"{Decimal(value):f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def lines(po):
    """(item name, quantity, unit cost, line total) per PO line, and the total."""
    out, total = [], Decimal("0")
    for line in sorted(po.items, key=lambda i: i.id):
        qty, cost = Decimal(line.quantity_ordered), Decimal(line.unit_cost)
        line_total = (qty * cost).quantize(CENT)
        total += line_total
        out.append((line.item_name or f"Item #{line.inventory_item_id or '?'} (removed)",
                    qty, cost, line_total))
    return out, total


def build(business, po, preparer, generated_at):
    """Bytes of the PDF for ``po`` of ``business`` (a Tenant)."""
    out = BytesIO()
    doc = SimpleDocTemplate(out, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm,
                            topMargin=15 * mm, bottomMargin=15 * mm,
                            title=f"Purchase order {po.po_number}", author=business.name)
    styles = getSampleStyleSheet()
    body, small = styles["BodyText"], styles["BodyText"].clone("small", fontSize=8, leading=10)

    ids = " | ".join(filter(None, [
        f"KRA PIN {business.kra_pin}" if business.kra_pin else None,
        f"PPB licence {business.ppb_premises_license}" if business.ppb_premises_license else None,
    ]))
    supplier = po.supplier
    supplier_lines = [supplier.name if supplier else "No supplier chosen"]
    if supplier:
        supplier_lines += [x for x in (supplier.contact_name, supplier.phone, supplier.email,
                                       supplier.address) if x]
    created = po.created_at or generated_at
    meta = [["PO number", po.po_number], ["Date", f"{created:%Y-%m-%d}"],
            ["Status", po.status]]
    if po.expected_date:
        meta.append(["Expected", f"{po.expected_date:%Y-%m-%d}"])

    story = [Paragraph(_esc(business.name), styles["Title"])]
    if ids:
        story.append(Paragraph(_esc(ids), small))
    story += [Spacer(1, 4 * mm), Paragraph("PURCHASE ORDER", styles["Heading2"])]
    head = Table([[Paragraph("<b>Supplier</b><br/>" + "<br/>".join(_esc(x) for x in supplier_lines),
                             body),
                   Table(meta, colWidths=[28 * mm, 45 * mm],
                         style=[("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                                ("FONTSIZE", (0, 0), (-1, -1), 9)])]],
                 colWidths=[105 * mm, 75 * mm])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story += [head, Spacer(1, 6 * mm)]

    rows, total = lines(po)
    data = [["#", "Item", "Qty", "Unit cost (KES)", "Line total (KES)"]]
    for n, (name, qty, cost, line_total) in enumerate(rows, start=1):
        data.append([str(n), Paragraph(_esc(name), body), quantity(qty), money(cost),
                     money(line_total)])
    data.append(["", "", "", "Total", money(total)])
    table = Table(data, colWidths=[10 * mm, 85 * mm, 20 * mm, 32 * mm, 33 * mm], repeatRows=1)
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -2), 0.25, colors.grey),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e9ecef")),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (3, -1), (-1, -1), "Helvetica-Bold"),
        ("LINEABOVE", (3, -1), (-1, -1), 1, colors.black),
        ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    story += [table, Spacer(1, 8 * mm)]
    if po.notes:
        story += [Paragraph(f"<b>Notes:</b> {_esc(po.notes)}", body), Spacer(1, 4 * mm)]
    who = (preparer.name or preparer.email) if preparer else "-"
    story += [Paragraph(f"Prepared by: {_esc(who)}", body),
              Paragraph(f"Printed {generated_at:%Y-%m-%d %H:%M} EAT", small)]
    doc.build(story)
    return out.getvalue()
