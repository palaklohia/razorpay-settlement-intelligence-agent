"""
Settlement Q&A agent.

Answers free-form questions about the current reconciled batch ("how many
orders had fee drift over ₹500?", "which orders are still on hold?").

Same architectural rule as explain.py: Claude answers FROM the already-
computed report data, it does not recompute or re-derive anything. The
report (summary + orders + exceptions) is passed as structured context, and
the model is instructed to answer only from what's given and to say so
explicitly if the batch doesn't contain the answer, rather than guess.

Requires ANTHROPIC_API_KEY. Falls back to an honest "not configured" message
if missing, same as explain.py.
"""

import os
import json


def answer_question(question: str, report: dict) -> dict:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return {
            "answer": None,
            "source": "fallback",
            "note": "Set ANTHROPIC_API_KEY to enable the Q&A agent.",
        }

    try:
        import anthropic
    except ImportError:
        return {
            "answer": None,
            "source": "fallback",
            "note": "Install the 'anthropic' package to enable the Q&A agent.",
        }

    # Keep the context compact: summary + a trimmed view of orders/exceptions.
    # For a larger production batch this would need retrieval instead of
    # dumping everything in-context, but at this batch size (tens to low
    # hundreds of orders) passing it directly is simpler and still accurate.
    context = {
        "summary": report.get("summary", {}),
        "orders": [
            {k: o[k] for k in ("order_id", "gross_amount", "channel", "clean_match",
                                "actual_net", "recomputed_net", "observed_mdr")}
            for o in report.get("orders", [])
        ],
        "exceptions": report.get("exceptions", []),
    }

    system_prompt = """You are a finance-ops assistant answering questions about a batch of \
reconciled Razorpay settlements. You will be given the ALREADY VERIFIED, already-computed \
reconciliation data as JSON — summary stats, every order, and every exception found. This data \
is ground truth; do not recompute, estimate, or second-guess any number in it.

Rules:
- Answer ONLY using the data provided. Cite specific order IDs and rupee amounts when relevant.
- If the data doesn't contain enough information to answer precisely, say so explicitly rather \
than guessing or extrapolating.
- Keep answers concise: 2-5 sentences, or a short list if the question asks for multiple orders.
- Do not invent any order_id, amount, or exception that isn't literally present in the data."""

    user_prompt = f"""Reconciliation batch data:
{json.dumps(context, indent=2)}

Question: {question}"""

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=500,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        answer = "".join(block.text for block in response.content if block.type == "text")
        return {"answer": answer.strip(), "source": "claude", "note": None}
    except Exception as e:
        error_str = str(e)
        if "credit balance is too low" in error_str:
            note = "Q&A agent temporarily unavailable (Anthropic account needs billing set up)."
        elif "rate_limit" in error_str.lower() or "429" in error_str:
            note = "Q&A agent is rate-limited right now — try again shortly."
        else:
            note = "Q&A agent unavailable right now."
        return {"answer": None, "source": "error", "note": note}
