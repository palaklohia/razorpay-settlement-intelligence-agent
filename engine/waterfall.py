"""
Builds the step-by-step ₹ waterfall for a single order — this is what powers
the "Explain this ₹" view in the frontend. Every number here is copied
straight from reconcile.py's arithmetic; nothing is invented for display.
"""


def build_waterfall(rec, matching_exceptions: list[dict]) -> dict:
    steps = [
        {"label": "Customer paid (gross)", "amount": rec.gross_amount, "running_total": rec.gross_amount},
    ]
    running = rec.gross_amount

    if rec.refund_ledger > 0:
        running -= rec.refund_ledger
        steps.append({"label": "Refund issued", "amount": -rec.refund_ledger, "running_total": round(running, 2)})

    running -= rec.recomputed_fee
    steps.append({
        "label": "Razorpay fee (contracted 2.00% MDR)",
        "amount": -rec.recomputed_fee,
        "running_total": round(running, 2),
    })

    running -= rec.recomputed_gst_on_fee
    steps.append({
        "label": "GST on fee (18%)",
        "amount": -rec.recomputed_gst_on_fee,
        "running_total": round(running, 2),
    })

    running -= rec.recomputed_tds
    steps.append({
        "label": "TDS (194-O, 0.1%)",
        "amount": -rec.recomputed_tds,
        "running_total": round(running, 2),
    })

    if rec.actual_transfer > 0:
        running -= rec.actual_transfer
        steps.append({"label": "Transfer to linked account", "amount": -rec.actual_transfer, "running_total": round(running, 2)})

    expected_net = round(running, 2)

    result = {
        "order_id": rec.order_id,
        "channel": rec.channel,
        "steps": steps,
        "expected_net": expected_net,
        "actual_settlement_net": rec.actual_net,
        "bank_credit_total": rec.bank_credit_total,
        "discrepancy_vs_settlement": rec.delta_net_settlement_vs_recomputed,
        "discrepancy_vs_bank": rec.delta_bank_vs_settlement,
        "has_discrepancy": abs(rec.delta_net_settlement_vs_recomputed) > 0.05 or abs(rec.delta_bank_vs_settlement) > 0.05,
        "exceptions": matching_exceptions,
    }
    return result
