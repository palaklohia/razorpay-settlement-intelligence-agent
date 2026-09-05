"""
Unit tests for engine/tax_matcher.py.

Builds minimal settlement + tax invoice DataFrames directly (bypassing the
generator) for precise control over each scenario.
"""

import pandas as pd
import pytest

from tax_matcher import build_tax_reconciliation, GST_ON_FEE_RATE


def settlement_row(order_id, settlement_date, gross, fee, gst):
    return {
        "settlement_id": f"stl_{order_id}", "order_id": order_id, "settlement_date": settlement_date,
        "channel": "online_domestic", "gross_amount": gross, "adjustment": 0.0, "tax_tds": 0.0,
        "fee": fee, "gst_on_fee": gst, "transfer": 0.0, "refund": 0.0,
        "net_amount": gross - fee - gst, "status": "settled",
    }


def invoice_row(period_start, period_end, total_fee, total_gst, gst_rate):
    return {
        "invoice_id": f"inv_{period_start}", "period_start": period_start, "period_end": period_end,
        "total_fee_amount": total_fee, "total_gst_amount": total_gst, "gst_rate": gst_rate,
        "invoice_date": period_end,
    }


class TestMatchedWeek:
    def test_correct_invoice_produces_no_exception(self):
        fee = 1000.0
        gst = round(fee * GST_ON_FEE_RATE, 2)
        settlements = pd.DataFrame([settlement_row("o1", "2026-09-01", 50000.0, fee, gst)])
        invoices = pd.DataFrame([invoice_row("2026-08-31", "2026-09-06", fee, gst, GST_ON_FEE_RATE)])

        result = build_tax_reconciliation(settlements, invoices)

        assert result["summary"]["total_exceptions"] == 0
        assert result["weeks"][0]["status"] == "matched"


class TestMissingInvoice:
    def test_no_invoice_for_week_with_settlements(self):
        fee = 800.0
        gst = round(fee * GST_ON_FEE_RATE, 2)
        settlements = pd.DataFrame([settlement_row("o2", "2026-09-01", 40000.0, fee, gst)])
        invoices = pd.DataFrame(columns=["invoice_id", "period_start", "period_end",
                                          "total_fee_amount", "total_gst_amount", "gst_rate", "invoice_date"])

        result = build_tax_reconciliation(settlements, invoices)

        assert result["summary"]["total_exceptions"] == 1
        assert result["exceptions"][0]["kind"] == "missing_invoice"
        assert result["exceptions"][0]["financial_impact"] == pytest.approx(gst)


class TestWrongGstRate:
    def test_invoice_at_wrong_rate_is_flagged(self):
        fee = 1000.0
        correct_gst = round(fee * GST_ON_FEE_RATE, 2)
        wrong_rate = 0.12
        wrong_gst = round(fee * wrong_rate, 2)

        settlements = pd.DataFrame([settlement_row("o3", "2026-09-01", 50000.0, fee, correct_gst)])
        invoices = pd.DataFrame([invoice_row("2026-08-31", "2026-09-06", fee, wrong_gst, wrong_rate)])

        result = build_tax_reconciliation(settlements, invoices)

        assert result["summary"]["total_exceptions"] == 1
        exc = result["exceptions"][0]
        assert exc["kind"] == "wrong_gst_rate"
        assert exc["financial_impact"] == pytest.approx(abs(wrong_gst - correct_gst))


class TestGstAmountMismatch:
    def test_correct_rate_but_wrong_total_is_flagged(self):
        fee = 1000.0
        correct_gst = round(fee * GST_ON_FEE_RATE, 2)
        invoiced_gst = round(correct_gst + 40.0, 2)  # correct rate, but total is off beyond tolerance

        settlements = pd.DataFrame([settlement_row("o4", "2026-09-01", 50000.0, fee, correct_gst)])
        invoices = pd.DataFrame([invoice_row("2026-08-31", "2026-09-06", fee, invoiced_gst, GST_ON_FEE_RATE)])

        result = build_tax_reconciliation(settlements, invoices)

        assert result["summary"]["total_exceptions"] == 1
        assert result["exceptions"][0]["kind"] == "gst_amount_mismatch"

    def test_small_rounding_within_tolerance_is_not_flagged(self):
        fee = 1000.0
        correct_gst = round(fee * GST_ON_FEE_RATE, 2)
        invoiced_gst = round(correct_gst + 2.0, 2)  # within GST_AMOUNT_TOLERANCE (₹5)

        settlements = pd.DataFrame([settlement_row("o5", "2026-09-01", 50000.0, fee, correct_gst)])
        invoices = pd.DataFrame([invoice_row("2026-08-31", "2026-09-06", fee, invoiced_gst, GST_ON_FEE_RATE)])

        result = build_tax_reconciliation(settlements, invoices)

        assert result["summary"]["total_exceptions"] == 0
