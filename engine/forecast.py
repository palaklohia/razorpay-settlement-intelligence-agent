"""
Forward cash forecast — derived directly from the reconciliation engine's
already-computed state, not a separate model. This is deliberate: we already
know exactly which orders are on_hold (withheld by Razorpay risk review) and
which are missing_bank_credit (settled per Razorpay but not yet seen in the
bank statement, almost always a timing lag rather than a real problem).
Projecting those forward is just arithmetic on facts we've already verified.

Methodology (stated plainly so it can be defended under questioning):
  - on_hold amounts: assumed to release over days 3-6 from "today", front-
    loaded (40/30/20/10%), reflecting typical risk-hold review timelines.
    Confidence: 0.55 (a hold can extend beyond this window).
  - missing_bank_credit amounts: assumed to land within days 1-2 (a bank
    processing lag, not a real shortfall). Confidence: 0.85.
  - Already clean-matched or already-banked amounts are NOT included — they
    are realized cash already, not a forecast item.

"Today" is the REAL current date (the server's system clock), not a date
derived from the synthetic dataset — so the forecast window always reflects
the actual next 7 calendar days, updating day by day regardless of when the
underlying settlement data happens to be dated.
"""

from datetime import timedelta, datetime
from collections import defaultdict

HOLD_RELEASE_WEIGHTS = {3: 0.40, 4: 0.30, 5: 0.20, 6: 0.10}
MISSING_CREDIT_WEIGHTS = {1: 0.6, 2: 0.4}


def build_forecast(results, settlements_df, horizon_days: int = 7) -> dict:
    import pandas as pd
    today = pd.Timestamp(datetime.now().date())

    daily = defaultdict(lambda: {"expected_amount": 0.0, "weighted_confidence_sum": 0.0, "sources": []})

    for r in results:
        if r.any_on_hold and r.actual_net > 0:
            for offset, weight in HOLD_RELEASE_WEIGHTS.items():
                amt = round(r.actual_net * weight, 2)
                daily[offset]["expected_amount"] += amt
                daily[offset]["weighted_confidence_sum"] += amt * 0.55
                daily[offset]["sources"].append({"order_id": r.order_id, "kind": "on_hold_release", "amount": amt})
            continue

        if r.bank_utrs_missing and not r.any_on_hold and r.actual_net > 0:
            for offset, weight in MISSING_CREDIT_WEIGHTS.items():
                amt = round(r.actual_net * weight, 2)
                daily[offset]["expected_amount"] += amt
                daily[offset]["weighted_confidence_sum"] += amt * 0.85
                daily[offset]["sources"].append({"order_id": r.order_id, "kind": "delayed_bank_credit", "amount": amt})

    days = []
    total = 0.0
    for offset in range(1, horizon_days + 1):
        bucket = daily.get(offset, {"expected_amount": 0.0, "weighted_confidence_sum": 0.0, "sources": []})
        amount = round(bucket["expected_amount"], 2)
        confidence = round(bucket["weighted_confidence_sum"] / amount, 2) if amount > 0 else None
        total += amount
        days.append({
            "date": (today + timedelta(days=offset - 1)).date().isoformat(),
            "day_offset": offset,
            "expected_amount": amount,
            "confidence": confidence,
            "n_sources": len(bucket["sources"]),
        })

    return {
        "as_of": today.date().isoformat(),
        "horizon_days": horizon_days,
        "total_expected_inflow": round(total, 2),
        "days": days,
        "methodology": (
            "Held settlements are projected to release over days 3-6 (front-loaded), "
            "confidence 0.55. Settlements reported by Razorpay but not yet seen in the "
            "bank statement are projected to land within days 1-2, confidence 0.85. "
            "Already-banked amounts are excluded — this is a forward-looking projection "
            "of pending money only, not a restatement of cash already received."
        ),
    }