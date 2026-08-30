"""
Orchestrator: loads the three sources, reconciles, classifies exceptions,
and writes report.json — the single artifact the API/frontend consumes.

Usage:
    python run.py --data-dir ../data/sample_output --out report.json
"""
import argparse
import json
from pathlib import Path

from reconcile import load_sources, reconcile, is_clean_match
from exceptions import classify_batch, exceptions_to_dicts


def build_report(data_dir: Path) -> dict:
    payments, settlements, bank = load_sources(
        data_dir / "payment_ledger.csv",
        data_dir / "settlement_report.csv",
        data_dir / "bank_statement.csv",
    )

    results, orphan_bank_utrs = reconcile(payments, settlements, bank)
    exceptions = classify_batch(results, orphan_bank_utrs, bank)

    clean = [r for r in results if is_clean_match(r)]
    match_rate = round(len(clean) / len(results), 4) if results else 0.0

    total_value = round(sum(r.gross_amount for r in results), 2)
    reconciled_value = round(sum(r.gross_amount for r in clean), 2)

    exception_summary = {}
    for e in exceptions:
        exception_summary[e.kind] = exception_summary.get(e.kind, 0) + 1

    report = {
        "summary": {
            "total_orders": len(results),
            "clean_matches": len(clean),
            "match_rate": match_rate,
            "total_order_value": total_value,
            "reconciled_value": reconciled_value,
            "reconciled_value_pct": round(reconciled_value / total_value, 4) if total_value else 0.0,
            "total_exceptions": len(exceptions),
            "exception_breakdown": exception_summary,
            "orphan_bank_credits": len(orphan_bank_utrs),
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
                "delta_bank_vs_settlement": r.delta_bank_vs_settlement,
                "observed_mdr": r.observed_mdr,
                "settlement_ids": r.settlement_ids,
            }
            for r in results
        ],
        "exceptions": exceptions_to_dicts(exceptions),
    }
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=str, default="../data/sample_output")
    parser.add_argument("--out", type=str, default="report.json")
    args = parser.parse_args()

    report = build_report(Path(args.data_dir))

    with open(args.out, "w") as f:
        json.dump(report, f, indent=2)

    s = report["summary"]
    print(f"Total orders:        {s['total_orders']}")
    print(f"Clean matches:       {s['clean_matches']} ({s['match_rate']*100:.1f}%)")
    print(f"Value reconciled:    ₹{s['reconciled_value']:,} of ₹{s['total_order_value']:,} "
          f"({s['reconciled_value_pct']*100:.1f}%)")
    print(f"Total exceptions:    {s['total_exceptions']}")
    print(f"Exception breakdown: {s['exception_breakdown']}")
    print(f"Report written to:   {args.out}")


if __name__ == "__main__":
    main()
