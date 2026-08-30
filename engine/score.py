"""
Scores the engine's exception list against ground_truth.json.

Design note on "rounding_noise": the generator injects sub-paisa-tolerance
noise (±0.01-0.02) on purpose, and the engine's CLEAN_TOLERANCE (₹0.05) is
deliberately set wider than that — real rounding noise should be absorbed
into a clean match, NOT surfaced as a false exception. So for this category,
"success" means the order was NOT flagged as an exception at all. This is
called out explicitly rather than silently treated as a miss, because an
un-flagged rounding order looks identical to a true negative and a missed
detection unless you say so.

Usage:
    python score.py --report report.json --ground-truth ../data/sample_output/ground_truth.json
"""
import argparse
import json


def score(report: dict, ground_truth: dict) -> dict:
    gt_by_order = {}
    for a in ground_truth["anomalies"]:
        gt_by_order.setdefault(a["order_id"], []).append(a["kind"])

    found_by_order = {}
    for e in report["exceptions"]:
        found_by_order.setdefault(e["order_id"], []).append(e["kind"])

    categories = sorted(set(a["kind"] for a in ground_truth["anomalies"]))
    rows = []
    total_tp = total_fp = total_fn = 0

    for kind in categories:
        gt_orders = {oid for oid, kinds in gt_by_order.items() if kind in kinds}

        if kind == "rounding_noise":
            # success = NOT flagged as any exception (absorbed by tolerance)
            tp = sum(1 for oid in gt_orders if oid not in found_by_order)
            fn = len(gt_orders) - tp
            fp = 0  # not applicable for this category's definition of success
        elif kind == "orphan_bank_credit":
            # bank-side exceptions aren't tied to an order_id in ground truth by
            # design (order_id there is just for bookkeeping) — count by volume instead
            found_count = sum(1 for e in report["exceptions"] if e["kind"] == kind)
            gt_count = len(gt_orders)
            tp = min(found_count, gt_count)
            fp = max(found_count - gt_count, 0)
            fn = max(gt_count - found_count, 0)
        else:
            found_orders = {oid for oid, kinds in found_by_order.items() if kind in kinds}
            tp = len(gt_orders & found_orders)
            fn = len(gt_orders - found_orders)
            fp = len(found_orders - gt_orders)

        precision = tp / (tp + fp) if (tp + fp) else None
        recall = tp / (tp + fn) if (tp + fn) else None

        total_tp += tp
        total_fp += fp
        total_fn += fn

        rows.append({
            "kind": kind,
            "injected": len(gt_orders) if kind != "orphan_bank_credit" else gt_count,
            "true_positives": tp,
            "false_positives": fp,
            "false_negatives": fn,
            "precision": round(precision, 3) if precision is not None else None,
            "recall": round(recall, 3) if recall is not None else None,
        })

    overall_precision = total_tp / (total_tp + total_fp) if (total_tp + total_fp) else None
    overall_recall = total_tp / (total_tp + total_fn) if (total_tp + total_fn) else None

    return {
        "per_category": rows,
        "overall": {
            "true_positives": total_tp,
            "false_positives": total_fp,
            "false_negatives": total_fn,
            "precision": round(overall_precision, 3) if overall_precision is not None else None,
            "recall": round(overall_recall, 3) if overall_recall is not None else None,
        },
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=str, default="report.json")
    parser.add_argument("--ground-truth", type=str, default="../data/sample_output/ground_truth.json")
    parser.add_argument("--out", type=str, default="scorecard.json")
    args = parser.parse_args()

    report = json.load(open(args.report))
    ground_truth = json.load(open(args.ground_truth))

    result = score(report, ground_truth)

    with open(args.out, "w") as f:
        json.dump(result, f, indent=2)

    print(f"{'CATEGORY':<24}{'INJECTED':<10}{'TP':<5}{'FP':<5}{'FN':<5}{'PRECISION':<11}{'RECALL':<8}")
    for r in result["per_category"]:
        p = f"{r['precision']:.2f}" if r["precision"] is not None else "n/a"
        rc = f"{r['recall']:.2f}" if r["recall"] is not None else "n/a"
        print(f"{r['kind']:<24}{r['injected']:<10}{r['true_positives']:<5}{r['false_positives']:<5}"
              f"{r['false_negatives']:<5}{p:<11}{rc:<8}")

    o = result["overall"]
    print()
    print(f"OVERALL  TP={o['true_positives']}  FP={o['false_positives']}  FN={o['false_negatives']}  "
          f"Precision={o['precision']}  Recall={o['recall']}")
    print(f"Scorecard written to: {args.out}")


if __name__ == "__main__":
    main()
