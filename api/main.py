"""
FastAPI backend for the Razorpay Settlement Intelligence Agent.

Endpoints:
    GET  /api/health
    GET  /api/report                 -> full reconciliation report (summary + orders + exceptions)
    GET  /api/orders/{order_id}/waterfall  -> step-by-step ₹ breakdown for one order ("Explain this ₹")
    GET  /api/scorecard               -> precision/recall against ground truth (if available)
    POST /api/regenerate               -> regenerate a fresh synthetic batch and re-run reconciliation live

Run locally:
    cd api
    uvicorn main:app --reload --port 8000
"""

import json
import subprocess
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

ROOT = Path(__file__).resolve().parent.parent
ENGINE_DIR = ROOT / "engine"
DATA_DIR = ROOT / "data" / "sample_output"       # curated, checked-in demo data — never overwritten
LIVE_DIR = ROOT / "data" / "live_output"          # scratch space for the /regenerate endpoint
GENERATOR_DIR = ROOT / "data" / "generator"

sys.path.append(str(ENGINE_DIR))
from reconcile import load_sources, reconcile, is_clean_match  # noqa: E402
from exceptions import classify_batch, exceptions_to_dicts  # noqa: E402
from waterfall import build_waterfall  # noqa: E402

app = FastAPI(title="Razorpay Settlement Intelligence Agent")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # fine for a hackathon demo; tighten before any real deployment
    allow_methods=["*"],
    allow_headers=["*"],
)

_cache = {"results": None, "orphans": None, "exceptions": None, "bank_df": None, "payments": None}


def _run_engine(data_dir: Path):
    payments, settlements, bank = load_sources(
        data_dir / "payment_ledger.csv",
        data_dir / "settlement_report.csv",
        data_dir / "bank_statement.csv",
    )
    results, orphans = reconcile(payments, settlements, bank)
    exceptions = classify_batch(results, orphans, bank)
    _cache.update({"results": results, "orphans": orphans, "exceptions": exceptions, "bank_df": bank, "payments": payments})


@app.on_event("startup")
def startup():
    _run_engine(DATA_DIR)


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/report")
def get_report():
    results = _cache["results"]
    exceptions = _cache["exceptions"]
    if results is None:
        raise HTTPException(500, "Engine not initialized")

    clean = [r for r in results if is_clean_match(r)]
    match_rate = round(len(clean) / len(results), 4) if results else 0.0
    total_value = round(sum(r.gross_amount for r in results), 2)
    reconciled_value = round(sum(r.gross_amount for r in clean), 2)

    exception_summary = {}
    for e in exceptions:
        exception_summary[e.kind] = exception_summary.get(e.kind, 0) + 1

    return {
        "summary": {
            "total_orders": len(results),
            "clean_matches": len(clean),
            "match_rate": match_rate,
            "total_order_value": total_value,
            "reconciled_value": reconciled_value,
            "reconciled_value_pct": round(reconciled_value / total_value, 4) if total_value else 0.0,
            "total_exceptions": len(exceptions),
            "exception_breakdown": exception_summary,
        },
        "orders": [
            {
                "order_id": r.order_id,
                "gross_amount": r.gross_amount,
                "channel": r.channel,
                "clean_match": is_clean_match(r),
                "actual_net": r.actual_net,
                "recomputed_net": r.recomputed_net,
                "bank_credit_total": r.bank_credit_total,
                "delta_net_vs_recomputed": r.delta_net_settlement_vs_recomputed,
                "observed_mdr": r.observed_mdr,
            }
            for r in results
        ],
        "exceptions": exceptions_to_dicts(exceptions),
    }


@app.get("/api/orders/{order_id}/waterfall")
def get_waterfall(order_id: str):
    results = _cache["results"]
    exceptions = _cache["exceptions"]
    rec = next((r for r in results if r.order_id == order_id), None)
    if rec is None:
        raise HTTPException(404, f"Order {order_id} not found")

    matching = [
        {"kind": e.kind, "confidence": e.confidence, "financial_impact": e.financial_impact,
         "detail": e.detail, "recommended_action": e.recommended_action}
        for e in exceptions if e.order_id == order_id
    ]
    return build_waterfall(rec, matching)


@app.get("/api/scorecard")
def get_scorecard():
    scorecard_path = DATA_DIR / "scorecard.json"
    if not scorecard_path.exists():
        raise HTTPException(404, "No scorecard available — run engine/score.py first")
    return json.loads(scorecard_path.read_text())


@app.post("/api/regenerate")
def regenerate(records: int = 60, seed: int = None, anomaly_rate: float = 0.35):
    import random
    seed = seed if seed is not None else random.randint(1, 100000)

    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [sys.executable, "generate_data.py", "--records", str(records), "--seed", str(seed),
         "--anomaly-rate", str(anomaly_rate), "--out", str(LIVE_DIR)],
        cwd=str(GENERATOR_DIR), check=True,
    )
    _run_engine(LIVE_DIR)  # loads the fresh live batch into the in-memory cache;
                            # data/sample_output on disk is left untouched
    return {"status": "regenerated", "records": records, "seed": seed}


@app.post("/api/reset")
def reset_to_sample_data():
    """Reload the curated, checked-in demo dataset (undoes /regenerate for this session)."""
    _run_engine(DATA_DIR)
    return {"status": "reset_to_sample_data"}
