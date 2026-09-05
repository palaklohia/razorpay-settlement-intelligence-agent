"""
Tax-line matcher.

Razorpay's fee is itself a taxable supply of service — it issues periodic GST
invoices on the fees it charges. This module reconciles those invoices
against what the settlement report's fee/GST line items actually imply for
the same period, which is exactly the kind of manual, error-prone check a
merchant's finance/compliance team does before filing input tax credit
claims.

Deterministic, same as reconcile.py/exceptions.py — no model involved in
deciding whether a tax line matches.
"""

from dataclasses import dataclass, asdict
from datetime import datetime, timedelta

GST_ON_FEE_RATE = 0.18
GST_AMOUNT_TOLERANCE = 5.0  # rupees — small invoicing rounding is expected; more than this is a real mismatch
RATE_TOLERANCE = 0.005      # 0.5 percentage points


@dataclass
class TaxException:
    period_start: str
    period_end: str
    kind: str
    confidence: float
    financial_impact: float
    detail: str
    recommended_action: str


def _week_bucket(date_str: str) -> str:
    d = datetime.fromisoformat(date_str).date()
    week_start = d - timedelta(days=d.weekday())
    return week_start.isoformat()


def build_tax_reconciliation(settlements_df, tax_invoice_df) -> dict:
    """
    Groups settlement fee/GST by week, compares against the tax invoice for
    that week, and returns a per-week reconciliation plus a flat exception
    list in the same shape as exceptions.py's output (kind/confidence/impact/
    detail/recommended_action) so it can be surfaced the same way.
    """
    weekly_expected = {}
    for _, row in settlements_df.iterrows():
        if row["gross_amount"] <= 0:
            continue  # pure refund-clawback rows carry no fee/GST
        week_start = _week_bucket(row["settlement_date"])
        bucket = weekly_expected.setdefault(week_start, {"fee": 0.0, "gst": 0.0})
        bucket["fee"] += float(row["fee"])
        bucket["gst"] += float(row["gst_on_fee"])

    invoices_by_week = {}
    for _, row in tax_invoice_df.iterrows():
        invoices_by_week[row["period_start"]] = row

    weeks = []
    exceptions = []

    for week_start in sorted(weekly_expected.keys()):
        expected_gst = round(weekly_expected[week_start]["gst"], 2)
        expected_fee = round(weekly_expected[week_start]["fee"], 2)
        period_end = (datetime.fromisoformat(week_start) + timedelta(days=6)).date().isoformat()

        if week_start not in invoices_by_week:
            weeks.append({
                "period_start": week_start, "period_end": period_end,
                "expected_fee": expected_fee, "expected_gst": expected_gst,
                "invoiced_gst": None, "invoiced_rate": None, "status": "missing_invoice",
            })
            exceptions.append(TaxException(
                period_start=week_start, period_end=period_end, kind="missing_invoice",
                confidence=1.0, financial_impact=expected_gst,
                detail=f"No tax invoice found for the week of {week_start}, but settlements "
                       f"in that period imply ₹{expected_gst} of GST on Razorpay fees.",
                recommended_action="Request the missing invoice from Razorpay before filing input tax credit for this period.",
            ))
            continue

        inv = invoices_by_week[week_start]
        invoiced_gst = round(float(inv["total_gst_amount"]), 2)
        invoiced_rate = round(float(inv["gst_rate"]), 4)
        gst_delta = round(invoiced_gst - expected_gst, 2)
        rate_delta = round(invoiced_rate - GST_ON_FEE_RATE, 4)

        status = "matched"
        if abs(rate_delta) > RATE_TOLERANCE:
            status = "wrong_gst_rate"
            exceptions.append(TaxException(
                period_start=week_start, period_end=period_end, kind="wrong_gst_rate",
                confidence=0.95, financial_impact=abs(gst_delta),
                detail=f"Invoice for week of {week_start} was computed at {invoiced_rate*100:.0f}% GST "
                       f"instead of the correct {GST_ON_FEE_RATE*100:.0f}%. Invoiced ₹{invoiced_gst} vs "
                       f"expected ₹{expected_gst} on ₹{expected_fee} of fees.",
                recommended_action="Request a corrected invoice from Razorpay at the correct GST rate before filing ITC.",
            ))
        elif abs(gst_delta) > GST_AMOUNT_TOLERANCE:
            status = "gst_amount_mismatch"
            exceptions.append(TaxException(
                period_start=week_start, period_end=period_end, kind="gst_amount_mismatch",
                confidence=0.85, financial_impact=abs(gst_delta),
                detail=f"Invoice GST for week of {week_start} is ₹{invoiced_gst}, but settlements imply "
                       f"₹{expected_gst} — a ₹{abs(gst_delta)} difference beyond normal rounding.",
                recommended_action="Reconcile against Razorpay's fee breakdown for this period before claiming ITC on this invoice.",
            ))

        weeks.append({
            "period_start": week_start, "period_end": period_end,
            "expected_fee": expected_fee, "expected_gst": expected_gst,
            "invoiced_gst": invoiced_gst, "invoiced_rate": invoiced_rate, "status": status,
        })

    matched_weeks = sum(1 for w in weeks if w["status"] == "matched")
    return {
        "summary": {
            "total_weeks": len(weeks),
            "matched_weeks": matched_weeks,
            "match_rate": round(matched_weeks / len(weeks), 4) if weeks else 0.0,
            "total_exceptions": len(exceptions),
        },
        "weeks": weeks,
        "exceptions": [asdict(e) for e in exceptions],
    }
