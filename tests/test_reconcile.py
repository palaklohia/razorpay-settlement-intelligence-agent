"""
Unit tests for engine/reconcile.py.

These build minimal, hand-crafted payment/settlement/bank DataFrames for a
handful of scenarios and assert the engine produces the correct waterfall
numbers and clean-match verdict. This is the deterministic core of the whole
project, so it's the part that most needs test coverage independent of the
synthetic data generator.
"""

import pandas as pd
import pytest

from reconcile import reconcile, is_clean_match, CONTRACTED_MDR, GST_ON_FEE_RATE, DEFAULT_TDS_RATE


def make_payment_row(order_id, gross_amount, channel="online_domestic", refund_amount=0.0, linked_account_id=""):
    return {
        "order_id": order_id,
        "order_date": "2026-09-01",
        "gross_amount": gross_amount,
        "channel": channel,
        "contracted_mdr": CONTRACTED_MDR,
        "refund_amount": refund_amount,
        "linked_account_id": linked_account_id,
        "expected_settlement_date": "2026-09-03",
        "expected_net_amount": 0.0,  # not used by reconcile(); ledger-side estimate only
    }


def make_settlement_row(settlement_id, order_id, gross_amount, fee, gst_on_fee, tax_tds,
                         adjustment=0.0, transfer=0.0, refund=0.0, status="settled",
                         settlement_date="2026-09-03", channel="online_domestic"):
    net = round(gross_amount - refund - fee - gst_on_fee - tax_tds - adjustment - transfer, 2)
    return {
        "settlement_id": settlement_id,
        "order_id": order_id,
        "settlement_date": settlement_date,
        "channel": channel,
        "gross_amount": gross_amount,
        "adjustment": adjustment,
        "tax_tds": tax_tds,
        "fee": fee,
        "gst_on_fee": gst_on_fee,
        "transfer": transfer,
        "refund": refund,
        "net_amount": net,
        "status": status,
    }


def make_bank_row(utr, credit_amount, credit_date="2026-09-04"):
    return {
        "utr": utr,
        "credit_amount": credit_amount,
        "credit_date": credit_date,
        "narration": f"RAZORPAY SETTLEMENT {utr}",
    }


def run_reconcile(payment_rows, settlement_rows, bank_rows):
    payments = pd.DataFrame(payment_rows)
    settlements = pd.DataFrame(settlement_rows)
    bank = pd.DataFrame(bank_rows)
    return reconcile(payments, settlements, bank)


class TestCleanMatch:
    def test_exact_match_ties_out(self):
        gross = 10000.0
        fee = round(gross * CONTRACTED_MDR, 2)          # 200.0
        gst = round(fee * GST_ON_FEE_RATE, 2)             # 36.0
        tds = round(gross * DEFAULT_TDS_RATE, 2)          # 10.0
        net = round(gross - fee - gst - tds, 2)           # 9754.0

        payments = [make_payment_row("order_1", gross)]
        settlements = [make_settlement_row("stl_1", "order_1", gross, fee, gst, tds)]
        bank = [make_bank_row("stl_1", net)]

        results, orphans = run_reconcile(payments, settlements, bank)

        assert len(results) == 1
        rec = results[0]
        assert rec.actual_net == pytest.approx(net)
        assert rec.recomputed_net == pytest.approx(net)
        assert rec.delta_net_settlement_vs_recomputed == pytest.approx(0.0, abs=0.01)
        assert rec.delta_bank_vs_settlement == pytest.approx(0.0, abs=0.01)
        assert is_clean_match(rec) is True
        assert orphans == []

    def test_refund_reduces_expected_net(self):
        gross = 5000.0
        refund = 500.0
        fee = round(gross * CONTRACTED_MDR, 2)
        gst = round(fee * GST_ON_FEE_RATE, 2)
        tds = round(gross * DEFAULT_TDS_RATE, 2)
        net = round(gross - refund - fee - gst - tds, 2)

        payments = [make_payment_row("order_2", gross, refund_amount=refund)]
        settlements = [make_settlement_row("stl_2", "order_2", gross, fee, gst, tds, refund=refund)]
        bank = [make_bank_row("stl_2", net)]

        results, _ = run_reconcile(payments, settlements, bank)
        rec = results[0]
        assert is_clean_match(rec) is True
        assert rec.actual_refund == pytest.approx(refund)


class TestFeeDrift:
    def test_fee_above_contracted_rate_fails_clean_match(self):
        gross = 10000.0
        drifted_fee = round(gross * 0.025, 2)  # 2.5% instead of contracted 2.0%
        gst = round(drifted_fee * GST_ON_FEE_RATE, 2)
        tds = round(gross * DEFAULT_TDS_RATE, 2)
        net = round(gross - drifted_fee - gst - tds, 2)

        payments = [make_payment_row("order_3", gross)]
        settlements = [make_settlement_row("stl_3", "order_3", gross, drifted_fee, gst, tds)]
        bank = [make_bank_row("stl_3", net)]

        results, _ = run_reconcile(payments, settlements, bank)
        rec = results[0]

        assert rec.observed_mdr == pytest.approx(0.025, abs=0.0001)
        assert is_clean_match(rec) is False
        # the recomputed (contracted-rate) net should be HIGHER than what was
        # actually settled, since the drifted fee overcharged the merchant
        assert rec.recomputed_net > rec.actual_net


class TestOnHold:
    def test_on_hold_settlement_is_never_clean(self):
        gross = 3000.0
        fee = round(gross * CONTRACTED_MDR, 2)
        gst = round(fee * GST_ON_FEE_RATE, 2)
        tds = round(gross * DEFAULT_TDS_RATE, 2)

        payments = [make_payment_row("order_4", gross)]
        settlements = [make_settlement_row("stl_4", "order_4", gross, fee, gst, tds, status="on_hold")]
        bank = []  # nothing has hit the bank yet — money is held

        results, _ = run_reconcile(payments, settlements, bank)
        rec = results[0]

        assert rec.any_on_hold is True
        assert is_clean_match(rec) is False


class TestMissingBankCredit:
    def test_settled_but_no_bank_row_is_not_clean(self):
        gross = 8000.0
        fee = round(gross * CONTRACTED_MDR, 2)
        gst = round(fee * GST_ON_FEE_RATE, 2)
        tds = round(gross * DEFAULT_TDS_RATE, 2)

        payments = [make_payment_row("order_5", gross)]
        settlements = [make_settlement_row("stl_5", "order_5", gross, fee, gst, tds, status="settled")]
        bank = []  # Razorpay says settled, but nothing shows up in the bank statement

        results, _ = run_reconcile(payments, settlements, bank)
        rec = results[0]

        assert rec.bank_utrs_missing == ["stl_5"]
        assert is_clean_match(rec) is False


class TestOrphanBankCredit:
    def test_unmatched_bank_row_is_reported_as_orphan(self):
        gross = 4000.0
        fee = round(gross * CONTRACTED_MDR, 2)
        gst = round(fee * GST_ON_FEE_RATE, 2)
        tds = round(gross * DEFAULT_TDS_RATE, 2)
        net = round(gross - fee - gst - tds, 2)

        payments = [make_payment_row("order_6", gross)]
        settlements = [make_settlement_row("stl_6", "order_6", gross, fee, gst, tds)]
        bank = [
            make_bank_row("stl_6", net),           # matches order_6 correctly
            make_bank_row("utr_mystery", 999.99),  # no settlement row claims this UTR
        ]

        results, orphans = run_reconcile(payments, settlements, bank)

        assert is_clean_match(results[0]) is True  # order_6 itself still reconciles cleanly
        assert orphans == ["utr_mystery"]


class TestSplitSettlement:
    def test_two_settlement_rows_for_one_order_are_aggregated(self):
        gross = 10000.0
        half_gross = 5000.0
        fee_half = round(half_gross * CONTRACTED_MDR, 2)
        gst_half = round(fee_half * GST_ON_FEE_RATE, 2)
        tds_half = round(half_gross * DEFAULT_TDS_RATE, 2)
        net_half = round(half_gross - fee_half - gst_half - tds_half, 2)

        payments = [make_payment_row("order_7", gross)]
        settlements = [
            make_settlement_row("stl_7a", "order_7", half_gross, fee_half, gst_half, tds_half),
            make_settlement_row("stl_7b", "order_7", half_gross, fee_half, gst_half, tds_half),
        ]
        bank = [make_bank_row("stl_7a", net_half), make_bank_row("stl_7b", net_half)]

        results, _ = run_reconcile(payments, settlements, bank)
        rec = results[0]

        assert rec.n_settlement_rows == 2
        assert rec.actual_gross == pytest.approx(gross)
        assert is_clean_match(rec) is True  # both legs sum correctly, should still be clean
