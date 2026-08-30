"""
Core reconciliation engine.

Reconstructs the settlement waterfall for every order:

    gross payment
      - refund
      - fee (recomputed at CONTRACTED_MDR, the rate the merchant actually agreed to)
      - GST on fee
      - TDS
      - adjustment
      - transfer (Route / linked accounts)
    = expected net settlement

...then compares that RECOMPUTED number against what Razorpay's settlement
report actually paid out, and against what the bank actually credited.

This module does NOT decide "why" something is wrong — that's exceptions.py.
This module only produces the numbers: expected vs actual vs bank, and the
discrepancy at each leg of the waterfall. Keeping this separate means every
number here is pure arithmetic, defensible, and reproducible — nothing here
is a guess.
"""

from dataclasses import dataclass, field
import pandas as pd

CONTRACTED_MDR = 0.0200
GST_ON_FEE_RATE = 0.18
DEFAULT_TDS_RATE = 0.001
TOLERANCE = 0.05  # paisa-level tolerance (in rupees) before something counts as a real mismatch


@dataclass
class OrderReconciliation:
    order_id: str
    gross_amount: float
    channel: str
    refund_ledger: float
    linked_account_id: str

    # settlement-report side (actual, aggregated across all settlement rows for this order)
    settlement_ids: list = field(default_factory=list)
    n_settlement_rows: int = 0
    actual_gross: float = 0.0
    actual_fee: float = 0.0
    actual_gst_on_fee: float = 0.0
    actual_tds: float = 0.0
    actual_adjustment: float = 0.0
    actual_transfer: float = 0.0
    actual_refund: float = 0.0
    actual_net: float = 0.0
    any_on_hold: bool = False

    # recomputed side (what SHOULD have happened, per contract terms)
    recomputed_fee: float = 0.0
    recomputed_gst_on_fee: float = 0.0
    recomputed_tds: float = 0.0
    recomputed_net: float = 0.0

    # bank side
    bank_credit_total: float = 0.0
    bank_utrs_found: list = field(default_factory=list)
    bank_utrs_missing: list = field(default_factory=list)

    # deltas
    delta_net_settlement_vs_recomputed: float = 0.0
    delta_bank_vs_settlement: float = 0.0
    observed_mdr: float = 0.0

    # later-settlement lookups (for refund clawback detection)
    future_refund_only_rows: list = field(default_factory=list)


def load_sources(payment_path: str, settlement_path: str, bank_path: str):
    payments = pd.read_csv(payment_path)
    settlements = pd.read_csv(settlement_path)
    bank = pd.read_csv(bank_path)
    return payments, settlements, bank


def reconcile(payments: pd.DataFrame, settlements: pd.DataFrame, bank: pd.DataFrame) -> list[OrderReconciliation]:
    bank_by_utr = {row["utr"]: row for _, row in bank.iterrows()}
    matched_utrs = set()

    results = []

    for _, prow in payments.iterrows():
        order_id = prow["order_id"]
        srows = settlements[settlements["order_id"] == order_id]

        rec = OrderReconciliation(
            order_id=order_id,
            gross_amount=float(prow["gross_amount"]),
            channel=prow["channel"],
            refund_ledger=float(prow["refund_amount"]),
            linked_account_id=prow.get("linked_account_id", ""),
        )

        # Separate "primary" settlement rows (real gross > 0) from pure
        # refund-clawback rows (gross == 0, refund > 0, settled later)
        primary_rows = srows[srows["gross_amount"] > 0]
        clawback_rows = srows[(srows["gross_amount"] == 0) & (srows["refund"] > 0)]

        rec.n_settlement_rows = len(primary_rows)
        rec.settlement_ids = list(primary_rows["settlement_id"])
        rec.future_refund_only_rows = list(clawback_rows["settlement_id"])

        rec.actual_gross = float(primary_rows["gross_amount"].sum())
        rec.actual_fee = float(primary_rows["fee"].sum())
        rec.actual_gst_on_fee = float(primary_rows["gst_on_fee"].sum())
        rec.actual_tds = float(primary_rows["tax_tds"].sum())
        rec.actual_adjustment = float(primary_rows["adjustment"].sum())
        rec.actual_transfer = float(primary_rows["transfer"].sum())
        rec.actual_refund = float(primary_rows["refund"].sum()) + float(clawback_rows["refund"].sum())
        rec.actual_net = float(primary_rows["net_amount"].sum()) + float(clawback_rows["net_amount"].sum())
        rec.any_on_hold = bool((primary_rows["status"] == "on_hold").any())

        # --- recompute what SHOULD have happened, using the contracted rate ---
        basis_gross = rec.actual_gross if rec.actual_gross > 0 else rec.gross_amount
        rec.recomputed_fee = round(basis_gross * CONTRACTED_MDR, 2)
        rec.recomputed_gst_on_fee = round(rec.recomputed_fee * GST_ON_FEE_RATE, 2)
        rec.recomputed_tds = round(basis_gross * DEFAULT_TDS_RATE, 2)
        rec.recomputed_net = round(
            basis_gross - rec.refund_ledger - rec.recomputed_fee - rec.recomputed_gst_on_fee
            - rec.recomputed_tds - rec.actual_adjustment - rec.actual_transfer,
            2,
        )

        rec.observed_mdr = round(rec.actual_fee / basis_gross, 4) if basis_gross else 0.0
        rec.delta_net_settlement_vs_recomputed = round(rec.actual_net - rec.recomputed_net, 2)

        # --- bank matching ---
        all_utrs = rec.settlement_ids + rec.future_refund_only_rows
        bank_total = 0.0
        found, missing = [], []
        for utr in all_utrs:
            if utr in bank_by_utr:
                bank_total += float(bank_by_utr[utr]["credit_amount"])
                found.append(utr)
                matched_utrs.add(utr)
            else:
                missing.append(utr)
        rec.bank_credit_total = round(bank_total, 2)
        rec.bank_utrs_found = found
        rec.bank_utrs_missing = missing
        rec.delta_bank_vs_settlement = round(rec.bank_credit_total - rec.actual_net, 2)

        results.append(rec)

    # any bank row never claimed by any order = orphan credit
    orphan_bank_rows = [utr for utr in bank_by_utr if utr not in matched_utrs]

    return results, orphan_bank_rows


def is_clean_match(rec: OrderReconciliation) -> bool:
    """An order is a clean match if every leg of the waterfall ties out within tolerance."""
    if rec.any_on_hold:
        return False
    if rec.n_settlement_rows == 0:
        return False
    if abs(rec.delta_net_settlement_vs_recomputed) > TOLERANCE:
        return False
    if abs(rec.delta_bank_vs_settlement) > TOLERANCE:
        return False
    if rec.bank_utrs_missing:
        return False
    return True
