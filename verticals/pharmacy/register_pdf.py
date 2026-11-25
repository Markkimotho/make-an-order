"""The controlled-drug register as a PDF for a PPB inspection.

One table per drug, its header repeated on every page, rows in register
order with opening and closing balances. The column set is provisional
until checked against a real register with a pilot pharmacist.
"""
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle)

HEADER = ["#", "Date", "Type", "Batch", "In", "Out", "Open", "Close",
          "Patient / supplier", "ID / doc no.", "Prescriber (reg)", "Pharmacist (reg)",
          "Witness"]


def _cell(text, style):
    return Paragraph((str(text) if text not in (None, "") else "-")
                     .replace("&", "&amp;").replace("<", "&lt;"), style)


def build(business, rows, generated_at, period=None, breaks=()):
    """Bytes of the PDF. ``rows`` are ControlledDrugEntry in register order."""
    out = BytesIO()
    doc = SimpleDocTemplate(out, pagesize=landscape(A4), leftMargin=10 * mm,
                            rightMargin=10 * mm, topMargin=12 * mm, bottomMargin=12 * mm,
                            title=f"Controlled drug register - {business.name}")
    styles = getSampleStyleSheet()
    small = styles["BodyText"].clone("small", fontSize=7, leading=8.5)
    story = [Paragraph(f"Controlled drug register: {business.name}", styles["Title"]),
             Paragraph(" · ".join(filter(None, [
                 f"PPB premises licence {business.ppb_premises_license}"
                 if business.ppb_premises_license else None,
                 f"Superintendent reg. {business.superintendent_reg_no}"
                 if business.superintendent_reg_no else None,
                 period, f"Generated {generated_at:%Y-%m-%d %H:%M} EAT"])), small),
             Spacer(1, 4 * mm)]
    if not rows:
        story.append(Paragraph("No register entries in this period.", styles["BodyText"]))
    drugs = {}
    for row in rows:
        drugs.setdefault((row.inventory_item_id, row.drug_name, row.drug_schedule), []).append(row)
    for (_, name, schedule), entries in drugs.items():
        story.append(Paragraph(f"{name}" + (f" (schedule {schedule})" if schedule else ""),
                               styles["Heading3"]))
        data = [[_cell(h, small) for h in HEADER]]
        for e in entries:
            party = e.patient_name or e.supplier_name
            ident = e.patient_national_id or e.reference_doc_no
            if e.patient_address:
                party = f"{party}, {e.patient_address}"
            data.append([_cell(v, small) for v in (
                e.seq, f"{e.entry_date:%Y-%m-%d %H:%M}", e.entry_type, e.batch_number,
                e.quantity_in or "", e.quantity_out or "", e.opening_balance, e.closing_balance,
                party, ident,
                f"{e.prescriber_name} ({e.prescriber_reg_no})" if e.prescriber_name else None,
                f"{e.pharmacist_name} ({e.pharmacist_reg_no})" if e.pharmacist_name else None,
                f"{e.witness_name} ({e.witness_reg_no})" if e.witness_name and e.witness_reg_no
                else e.witness_name)])
        widths = [8, 22, 17, 20, 10, 10, 11, 11, 48, 26, 36, 36, 30]
        table = Table(data, colWidths=[w * mm for w in widths], repeatRows=1)
        style = [("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                 ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e9ecef")),
                 ("VALIGN", (0, 0), (-1, -1), "TOP")]
        for i, e in enumerate(entries, start=1):
            if e.id in breaks:
                style.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#f8d7da")))
        table.setStyle(TableStyle(style))
        story += [table, Spacer(1, 5 * mm)]
    doc.build(story)
    return out.getvalue()
