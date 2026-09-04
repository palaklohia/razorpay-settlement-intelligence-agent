"""
Unit tests for engine/exceptions.py.

Builds the same minimal DataFrames as test_reconcile.py, runs them through
the full reconcile() -> classify_batch() pipeline, and asserts the correct
exception kind (and only that kind) is produced. Also specifically tests the
`unclassified_discrepancy` safety net with a genuinely unmodeled case: a
settlement that matches the contracted rate perfectly, but where the bank
credited a different amount than the settlement report says — a mismatch no
other rule checks for, which is exactly why the safety net exists.
"""

import pandas as pd
import pytest

from reconcile import reconcile, is_clean_match, CONTRACTED_MDR, GST_ON_FEE_RATE, DEFAULT_TDS_RATE
from exceptions import classify_batch

from test_reconcile import make_payment_row, make_settlement_row, make_bank_row


def kinds_for_order(exceptions, order_id):
    return {e.kind for e in exceptions if e.order_id == order_id}


class TestOnHoldException:
    def test_on_hold_produces_on_hold_exception(self):
        gross = 3000.0
        fee = round(gross * CONTRACTED_MDR, 2)
        gst = round(fee * GST_ON_FEE_RATE, 2)
        tds = round(gross * DEFAULT_TDS_RATE, 2)

        payments = pd.DataFrame([make_payment_row("o1", gross)])
        settlements = pd.DataFrame([make_settlement_row("s1", "o1", gross, fee, gst, tds, status="on_hold")])
        bank = pd.DataFrame([])

        results, orphans = reconcile(payments, settlements, bank)
        exceptions = classify_batch(results, orphans, bank)

        assert kinds_for_order(exceptions, "o1") == {"on_hold"}


class TestDuplicateVsSplit:
    def test_two_full_gross_rows_flagged_as_duplicate(self):
        gross = 6000.0
        fee = round(gross * CONTRACTED_MDR, 2)
        gst = round(fee * GST_ON_FEE_RATE, 2)
        tds = round(gross * DEFAULT_TDS_RATE, 2)
        net = round(gross - fee - gst - tds, 2)

        payments = pd.DataFrame([make_payment_row("o2", gross)])
        settlements = pd.DataFrame([
            make_settlement_row("s2a", "o2", gross, fee, gst, tds),
            make_settlement_row("s2b", "o2", gross, fee, gst, tds),
        ])
        bank = pd.DataFrame([make_bank_row("s2a", net), make_bank_row("s2b", net)])

        results, orphans = reconcile(payments, settlements, bank)
        exceptions = classify_batch(results, orphans, bank)

        assert "duplicate_settlement" in kinds_for_order(exceptions, "o2")

    def test_two_half_gross_rows_flagged_as_split_not_duplicate(self):
        gross = 6000.0
        half = 3000.0
        fee_h = round(half * CONTRACTED_MDR, 2)
        gst_h = round(fee_h * GST_ON_FEE_RATE, 2)
        tds_h = round(half * DEFAULT_TDS_RATE, 2)
        net_h = round(half - fee_h - gst_h - tds_h, 2)

        payments = pd.DataFrame([make_payment_row("o3", gross)])
        settlements = pd.DataFrame([
            make_settlement_row("s3a", "o3", half, fee_h, gst_h, tds_h),
            make_settlement_row("s3b", "o3", half, fee_h, gst_h, tds_h),
        ])
        bank = pd.DataFrame([make_bank_row("s3a", net_h), make_bank_row("s3b", net_h)])

        results, orphans = reconcile(payments, settlements, bank)
        exceptions = classify_batch(results, orphans, bank)

        kinds = kinds_for_order(exceptions, "o3")
        assert "split_settlement" in kinds
        assert "duplicate_settlement" not in kinds


class TestFeeDriftException:
    def test_drifted_fee_produces_fee_drift(self):
        gross = 10000.0
        drifted_fee = round(gross * 0.025, 2)
        gst = round(drifted_fee * GST_ON_FEE_RATE, 2)
        tds = round(gross * DEFAULT_TDS_RATE, 2)
        net = round(gross - drifted_fee - gst - tds, 2)

        payments = pd.DataFrame([make_payment_row("o4", gross)])
        settlements = pd.DataFrame([make_settlement_row("s4", "o4", gross, drifted_fee, gst, tds)])
        bank = pd.DataFrame([make_bank_row("s4", net)])

        results, orphans = reconcile(payments, settlements, bank)
        exceptions = classify_batch(results, orphans, bank)

        assert "fee_drift" in kinds_for_order(exceptions, "o4")

    def test_fee_within_threshold_does_not_trigger_drift(self):
        # fee computed at EXACTLY the contracted rate should be a clean
        # match with no exception at all.
        gross = 10000.0
        fee = round(gross * CONTRACTED_MDR, 2)
        gst = round(fee * GST_ON_FEE_RATE, 2)
        tds = round(gross * DEFAULT_TDS_RATE, 2)
        net = round(gross - fee - gst - tds, 2)

        payments = pd.DataFrame([make_payment_row("o5", gross)])
        settlements = pd.DataFrame([make_settlement_row("s5", "o5", gross, fee, gst, tds)])
        bank = pd.DataFrame([make_bank_row("s5", net)])

        results, orphans = reconcile(payments, settlements, bank)
        exceptions = classify_batch(results, orphans, bank)

        assert kinds_for_order(exceptions, "o5") == set()

    def test_tiny_mdr_deviation_below_drift_threshold_can_still_be_a_real_discrepancy(self):
        """
        Documents an intentional, non-obvious behavior: a 0.02% MDR deviation
        is correctly judged too small to call "fee_drift" on its own — but on
        a large enough order, that tiny rate difference still produces a real
        rupee discrepancy that EXCEEDS the rounding-noise ceiling. The system
        must not mislabel this as drift, and must not silently absorb it as
        rounding either — it should fall through to unclassified_discrepancy
        so a real, if small, shortfall never disappears silently.
        """
        gross = 10000.0
        fee = round(gross * (CONTRACTED_MDR + 0.0002), 2)  # well under FEE_DRIFT_THRESHOLD (0.0015)
        gst = round(fee * GST_ON_FEE_RATE, 2)
        tds = round(gross * DEFAULT_TDS_RATE, 2)
        net = round(gross - fee - gst - tds, 2)

        payments = pd.DataFrame([make_payment_row("o5b", gross)])
        settlements = pd.DataFrame([make_settlement_row("s5b", "o5b", gross, fee, gst, tds)])
        bank = pd.DataFrame([make_bank_row("s5b", net)])

        results, orphans = reconcile(payments, settlements, bank)
        exceptions = classify_batch(results, orphans, bank)

        assert "fee_drift" not in kinds_for_order(exceptions, "o5b")  # too small to call drift
        assert kinds_for_order(exceptions, "o5b") == {"unclassified_discrepancy"}  # but not silently absorbed either


class TestTdsMismatchException:
    def test_wrong_tds_produces_tds_mismatch(self):
        gross = 10000.0
        fee = round(gross * CONTRACTED_MDR, 2)
        gst = round(fee * GST_ON_FEE_RATE, 2)
        wrong_tds = round(gross * 0.005, 2)  # way more than the contracted 0.1%
        net = round(gross - fee - gst - wrong_tds, 2)

        payments = pd.DataFrame([make_payment_row("o6", gross)])
        settlements = pd.DataFrame([make_settlement_row("s6", "o6", gross, fee, gst, wrong_tds)])
        bank = pd.DataFrame([make_bank_row("s6", net)])

        results, orphans = reconcile(payments, settlements, bank)
        exceptions = classify_batch(results, orphans, bank)

        assert "tds_mismatch" in kinds_for_order(exceptions, "o6")


class TestMissingBankCreditException:
    def test_settled_with_no_bank_row_produces_missing_bank_credit(self):
        gross = 7000.0
        fee = round(gross * CONTRACTED_MDR, 2)
        gst = round(fee * GST_ON_FEE_RATE, 2)
        tds = round(gross * DEFAULT_TDS_RATE, 2)

        payments = pd.DataFrame([make_payment_row("o7", gross)])
        settlements = pd.DataFrame([make_settlement_row("s7", "o7", gross, fee, gst, tds, status="settled")])
        bank = pd.DataFrame([])

        results, orphans = reconcile(payments, settlements, bank)
        exceptions = classify_batch(results, orphans, bank)

        assert "missing_bank_credit" in kinds_for_order(exceptions, "o7")


class TestRoundingNoiseException:
    def test_small_residual_flagged_as_rounding_not_hidden(self):
        gross = 10000.0
        fee = round(gross * CONTRACTED_MDR, 2)
        gst = round(fee * GST_ON_FEE_RATE, 2)
        tds = round(gross * DEFAULT_TDS_RATE, 2)
        correct_net = round(gross - fee - gst - tds, 2)
        settled_net = round(correct_net - 0.20, 2)  # 20 paisa off — inside the rounding band

        payments = pd.DataFrame([make_payment_row("o8", gross)])
        settlement_row = make_settlement_row("s8", "o8", gross, fee, gst, tds)
        settlement_row["net_amount"] = settled_net  # deliberately override to inject the residual
        settlements = pd.DataFrame([settlement_row])
        bank = pd.DataFrame([make_bank_row("s8", settled_net)])

        results, orphans = reconcile(payments, settlements, bank)
        exceptions = classify_batch(results, orphans, bank)

        assert "rounding_noise" in kinds_for_order(exceptions, "o8")

    def test_true_paisa_rounding_within_tolerance_is_clean_not_flagged(self):
        gross = 10000.0
        fee = round(gross * CONTRACTED_MDR, 2)
        gst = round(fee * GST_ON_FEE_RATE, 2)
        tds = round(gross * DEFAULT_TDS_RATE, 2)
        correct_net = round(gross - fee - gst - tds, 2)
        settled_net = round(correct_net - 0.02, 2)  # 2 paisa — inside CLEAN_TOLERANCE

        payments = pd.DataFrame([make_payment_row("o9", gross)])
        settlement_row = make_settlement_row("s9", "o9", gross, fee, gst, tds)
        settlement_row["net_amount"] = settled_net
        settlements = pd.DataFrame([settlement_row])
        bank = pd.DataFrame([make_bank_row("s9", settled_net)])

        results, orphans = reconcile(payments, settlements, bank)
        exceptions = classify_batch(results, orphans, bank)

        assert kinds_for_order(exceptions, "o9") == set()
        assert is_clean_match(results[0]) is True


class TestOrphanBankCreditException:
    def test_unmatched_bank_row_produces_orphan_exception(self):
        gross = 4000.0
        fee = round(gross * CONTRACTED_MDR, 2)
        gst = round(fee * GST_ON_FEE_RATE, 2)
        tds = round(gross * DEFAULT_TDS_RATE, 2)
        net = round(gross - fee - gst - tds, 2)

        payments = pd.DataFrame([make_payment_row("o10", gross)])
        settlements = pd.DataFrame([make_settlement_row("s10", "o10", gross, fee, gst, tds)])
        bank = pd.DataFrame([
            make_bank_row("s10", net),
            make_bank_row("utr_ghost", 1234.56),
        ])

        results, orphans = reconcile(payments, settlements, bank)
        exceptions = classify_batch(results, orphans, bank)

        orphan_exceptions = [e for e in exceptions if e.kind == "orphan_bank_credit"]
        assert len(orphan_exceptions) == 1
        assert orphan_exceptions[0].financial_impact == pytest.approx(1234.56)


class TestUnclassifiedSafetyNet:
    def test_settlement_correct_but_bank_amount_wrong_falls_to_unclassified(self):
        """
        A genuinely unmodeled case: the settlement report matches the
        contracted rate perfectly (so fee_drift/tds_mismatch don't fire), the
        UTR is present in the bank statement (so missing_bank_credit doesn't
        fire), but the bank credited a DIFFERENT amount than the settlement
        says. No existing rule checks for this specific mismatch — it must
        fall through to the unclassified_discrepancy safety net rather than
        disappearing silently.
        """
        gross = 10000.0
        fee = round(gross * CONTRACTED_MDR, 2)
        gst = round(fee * GST_ON_FEE_RATE, 2)
        tds = round(gross * DEFAULT_TDS_RATE, 2)
        correct_net = round(gross - fee - gst - tds, 2)
        wrong_bank_credit = round(correct_net - 150.00, 2)  # bank shorted the merchant by ₹150

        payments = pd.DataFrame([make_payment_row("o11", gross)])
        settlements = pd.DataFrame([make_settlement_row("s11", "o11", gross, fee, gst, tds)])
        bank = pd.DataFrame([make_bank_row("s11", wrong_bank_credit)])

        results, orphans = reconcile(payments, settlements, bank)
        exceptions = classify_batch(results, orphans, bank)

        rec = results[0]
        assert is_clean_match(rec) is False  # bank delta exceeds tolerance
        assert kinds_for_order(exceptions, "o11") == {"unclassified_discrepancy"}

    def test_every_non_clean_order_produces_at_least_one_exception(self):
        """
        Structural guarantee: the safety net means no order that fails
        clean-match can vanish between 'matched' and 'explained'.
        """
        gross = 5000.0
        fee = round(gross * CONTRACTED_MDR, 2)
        gst = round(fee * GST_ON_FEE_RATE, 2)
        tds = round(gross * DEFAULT_TDS_RATE, 2)
        correct_net = round(gross - fee - gst - tds, 2)
        wrong_bank_credit = round(correct_net - 75.00, 2)

        payments = pd.DataFrame([make_payment_row("o12", gross)])
        settlements = pd.DataFrame([make_settlement_row("s12", "o12", gross, fee, gst, tds)])
        bank = pd.DataFrame([make_bank_row("s12", wrong_bank_credit)])

        results, orphans = reconcile(payments, settlements, bank)
        exceptions = classify_batch(results, orphans, bank)

        flagged_order_ids = {e.order_id for e in exceptions}
        for r in results:
            if not is_clean_match(r):
                assert r.order_id in flagged_order_ids, f"{r.order_id} failed clean match but has no exception recorded"
