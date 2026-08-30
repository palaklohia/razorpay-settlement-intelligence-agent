"""
Exception classification.

IMPORTANT DESIGN DECISION: every rule here is deterministic (a threshold or a
field check), not an LLM guess. Confidence scores are derived from measurable
signals (how many other rows in this batch show the same pattern), not from
an LLM asserting a percentage. This is what lets us defend the numbers when
asked "how did you get 94%?" in a panel interview.

The LLM's job (added later, in the API/frontend layer) is ONLY to turn these
structured findings into a plain-English explanation for the "Explain this ₹"
view. It never decides what the exception IS.
"""

from dataclasses import dataclass, asdict
from collections import Counter

FEE_DRIFT_THRESHOLD = 0.0015     # >0.15% MDR deviation counts as drift, not noise
TDS_MISMATCH_THRESHOLD = 0.05    # rupees
ROUNDING_CEILING = 0.5           # rupees — below this, a mismatch is "rounding noise" not a real exception
CLEAN_TOLERANCE = 0.05           # must match reconcile.TOLERANCE


@dataclass
class Exception_:
    order_id: str
    kind: str
    confidence: float
    financial_impact: float
    detail: str
    recommended_action: str


def classify_batch(results, orphan_bank_utrs, bank_df) -> list[Exception_]:
    exceptions: list[Exception_] = []

    # Batch-level frequency table for fee drift, so confidence reflects a
    # measurable pattern ("this happened 17 times") rather than a vibe.
    mdr_buckets = Counter(round(r.observed_mdr, 4) for r in results if r.n_settlement_rows > 0)

    for r in results:
        # Priority order matters: check the most specific / highest-certainty
        # explanation first so we don't mislabel e.g. an on-hold split as "drift".

        if r.any_on_hold:
            exceptions.append(Exception_(
                order_id=r.order_id, kind="on_hold", confidence=1.0,
                financial_impact=round(r.gross_amount - r.actual_refund, 2),
                detail="Settlement marked on_hold by Razorpay risk review — funds not yet due to bank.",
                recommended_action="No action needed yet; escalate to Razorpay support only if held > 7 days.",
            ))
            continue

        if r.n_settlement_rows >= 2:
            total_actual_gross = r.actual_gross
            if abs(total_actual_gross - r.gross_amount * 2) < 1.0:
                exceptions.append(Exception_(
                    order_id=r.order_id, kind="duplicate_settlement", confidence=1.0,
                    financial_impact=round(r.gross_amount, 2),
                    detail=f"{r.n_settlement_rows} settlement rows found for one order; combined gross "
                           f"(₹{total_actual_gross}) is ~2x the order's actual gross (₹{r.gross_amount}).",
                    recommended_action="Verify with Razorpay this isn't a double-count; do not book both rows as revenue.",
                ))
                continue
            else:
                exceptions.append(Exception_(
                    order_id=r.order_id, kind="split_settlement", confidence=0.95,
                    financial_impact=0.0,
                    detail=f"Order settled across {r.n_settlement_rows} separate settlement rows "
                           f"(e.g. POS + online leg, or partial batch settlement).",
                    recommended_action="No fix needed — informational. Ensure downstream ledger sums all legs before comparing to bank.",
                ))
                # a split can still have other problems, so don't `continue` — fall through to other checks

        if r.future_refund_only_rows:
            exceptions.append(Exception_(
                order_id=r.order_id, kind="refund_clawback_timing", confidence=1.0,
                financial_impact=round(r.refund_ledger, 2),
                detail=f"Refund of ₹{r.refund_ledger} was issued but appears in a LATER settlement "
                       f"({', '.join(r.future_refund_only_rows)}), not the original payment's settlement.",
                recommended_action="Match refund manually to the original order in the ledger; do not flag original settlement as short.",
            ))

        if abs(r.observed_mdr - 0.0200) > FEE_DRIFT_THRESHOLD and r.n_settlement_rows > 0:
            recurrence = mdr_buckets[round(r.observed_mdr, 4)]
            confidence = 0.95 if recurrence >= 2 else 0.75
            fee_delta = round(r.actual_fee - (r.recomputed_fee), 2)
            exceptions.append(Exception_(
                order_id=r.order_id, kind="fee_drift", confidence=confidence,
                financial_impact=abs(fee_delta),
                detail=f"Contracted MDR: 2.00%. Observed MDR: {r.observed_mdr*100:.2f}%. "
                       f"Same rate seen on {recurrence} settlement(s) in this batch.",
                recommended_action="Raise with Razorpay account manager — check for an unannounced MDR slab change.",
            ))

        if abs(r.actual_tds - r.recomputed_tds) > TDS_MISMATCH_THRESHOLD:
            exceptions.append(Exception_(
                order_id=r.order_id, kind="tds_mismatch", confidence=0.9,
                financial_impact=round(abs(r.actual_tds - r.recomputed_tds), 2),
                detail=f"Expected TDS (194-O, 0.1%): ₹{r.recomputed_tds}. Actual TDS deducted: ₹{r.actual_tds}.",
                recommended_action="Reconcile against Form 26AS at filing time; flag for CA review.",
            ))

        if r.bank_utrs_missing and not r.any_on_hold:
            exceptions.append(Exception_(
                order_id=r.order_id, kind="missing_bank_credit", confidence=1.0,
                financial_impact=round(r.actual_net, 2),
                detail=f"Settlement report shows {r.order_id} as settled (UTR(s): "
                       f"{', '.join(r.bank_utrs_missing)}) but no matching credit found in bank statement.",
                recommended_action="Check bank statement for a delayed credit or contact bank; do not assume funds lost.",
            ))

        residual = abs(r.delta_net_settlement_vs_recomputed)
        if CLEAN_TOLERANCE < residual <= ROUNDING_CEILING and not any(
            e.order_id == r.order_id for e in exceptions
        ):
            exceptions.append(Exception_(
                order_id=r.order_id, kind="rounding_noise", confidence=0.6,
                financial_impact=round(residual, 2),
                detail=f"Net settlement differs from recomputed expectation by only ₹{residual} — "
                       f"likely paisa-level rounding, not a real discrepancy.",
                recommended_action="No action needed; monitor if it recurs at scale.",
            ))

    # Safety net: if an order fails clean-match criteria but didn't trip any
    # specific rule above, it must NOT disappear silently. Surface it as an
    # honest "we don't know why this doesn't reconcile" rather than hiding it.
    from reconcile import is_clean_match
    flagged_orders = {e.order_id for e in exceptions}
    for r in results:
        if not is_clean_match(r) and r.order_id not in flagged_orders:
            exceptions.append(Exception_(
                order_id=r.order_id, kind="unclassified_discrepancy", confidence=0.3,
                financial_impact=round(abs(r.delta_net_settlement_vs_recomputed) + abs(r.delta_bank_vs_settlement), 2),
                detail=f"Order does not reconcile cleanly (net delta ₹{r.delta_net_settlement_vs_recomputed}, "
                       f"bank delta ₹{r.delta_bank_vs_settlement}) but doesn't match any known pattern.",
                recommended_action="Needs manual review — does not fit an automated root-cause rule.",
            ))

    for utr in orphan_bank_utrs:
        row = bank_df[bank_df["utr"] == utr].iloc[0]
        exceptions.append(Exception_(
            order_id="(none — bank-side only)", kind="orphan_bank_credit", confidence=0.7,
            financial_impact=round(float(row["credit_amount"]), 2),
            detail=f"Bank credit of ₹{row['credit_amount']} on {row['credit_date']} (UTR {utr}) has no "
                   f"matching row in the Razorpay settlement report.",
            recommended_action="Investigate — could be a manual payout, interest credit, or unreported settlement.",
        ))

    return exceptions


def exceptions_to_dicts(exceptions: list[Exception_]) -> list[dict]:
    return [asdict(e) for e in exceptions]
