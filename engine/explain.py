"""
Optional AI narrator.

IMPORTANT: this module NEVER decides what an exception is — that stays
100% deterministic in exceptions.py. This module only takes an ALREADY
VERIFIED, already-computed finding and turns it into plain English for a
non-technical reader. This separation is deliberate: verification (the hard,
trust-critical part) is rule-based and auditable; generation (turning a
correct finding into a sentence) is the one place an LLM is appropriate.

Requires ANTHROPIC_API_KEY to be set in the environment. Falls back
gracefully — the frontend always has the rule-based `detail` text to show
even if no key is configured, so a missing key never breaks the demo.
"""

import os


def explain_order(order_waterfall: dict) -> dict:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return {
            "narrative": None,
            "source": "fallback",
            "note": "Set ANTHROPIC_API_KEY to enable AI-generated explanations.",
        }

    try:
        import anthropic
    except ImportError:
        return {
            "narrative": None,
            "source": "fallback",
            "note": "Install the 'anthropic' package to enable AI-generated explanations.",
        }

    exceptions = order_waterfall.get("exceptions", [])
    findings_text = "\n".join(
        f"- {e['kind']} (confidence {e['confidence']}): {e['detail']} "
        f"[impact: rupee {e['financial_impact']}, recommended action: {e['recommended_action']}]"
        for e in exceptions
    ) or "None — this order reconciles cleanly across ledger, settlement, and bank."

    prompt = f"""You are a finance-ops assistant. Below is an ALREADY VERIFIED reconciliation
finding for one order, produced by a deterministic rules engine — the numbers and the
classification are ground truth. Do not recompute, second-guess, or add any number not given
below. Write a 2-3 sentence plain-English explanation a non-technical finance manager could
read at a glance: what happened, the rupee impact, and the recommended next step.

Order: {order_waterfall['order_id']}
Channel: {order_waterfall.get('channel', 'n/a')}
Expected net (recomputed at contracted rates): rupee {order_waterfall['expected_net']}
Actual settlement: rupee {order_waterfall['actual_settlement_net']}
Bank credit: rupee {order_waterfall['bank_credit_total']}

Verified findings:
{findings_text}
"""

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        narrative = "".join(block.text for block in response.content if block.type == "text")
        return {"narrative": narrative.strip(), "source": "claude", "note": None}
    except Exception as e:
        error_str = str(e)
        if "credit balance is too low" in error_str or "invalid_request_error" in error_str and "billing" in error_str.lower():
            note = "AI explanations are temporarily unavailable (Anthropic account needs billing set up). Showing the rule-based finding above instead — the reconciliation result itself is unaffected."
        elif "rate_limit" in error_str.lower() or "429" in error_str:
            note = "AI explanations are rate-limited right now. Showing the rule-based finding above instead."
        else:
            note = "AI explanation unavailable right now. Showing the rule-based finding above instead."
        return {"narrative": None, "source": "error", "note": note}