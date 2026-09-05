"""
Synthetic data generator for the Razorpay Settlement Intelligence Agent.

Produces THREE sources that a real merchant finance team reconciles by hand:
  1. payment_ledger.csv     -> internal system of record (what we THINK we're owed)
  2. settlement_report.csv  -> Razorpay's settlement report (gross -> deductions -> net)
  3. bank_statement.csv     -> actual bank credits (ground truth of cash received)

Realistic noise is injected on purpose so the reconciliation engine has real work
to do. Every injected anomaly is logged to ground_truth.json so we can later
score the engine's precision/recall honestly instead of cherry-picking.

Usage:
    python generate_data.py --records 60 --seed 42 --out ./output
"""

import argparse
import csv
import json
import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

CHANNELS = ["online_domestic", "international_card", "pos"]
CONTRACTED_MDR = 0.0200  # 2.00% standard contracted rate for this merchant
GST_ON_FEE_RATE = 0.18   # 18% GST charged on Razorpay's fee
DEFAULT_TDS_RATE = 0.001  # 0.1% TDS under 194-O for e-commerce operators


def money(x: float) -> float:
    return round(x + 1e-9, 2)


@dataclass
class Anomaly:
    order_id: str
    kind: str
    detail: str


class DataGenerator:
    def __init__(self, n_records: int, seed: int, anomaly_rate: float):
        self.n = n_records
        self.rng = random.Random(seed)
        self.anomaly_rate = anomaly_rate
        self.anomalies: list[Anomaly] = []
        self.payment_rows = []
        self.settlement_rows = []
        self.bank_rows = []

    # ---------- helpers ----------

    def _new_id(self, prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex[:14]}"

    def _rand_date(self, start: datetime, days_span: int) -> datetime:
        return start + timedelta(days=self.rng.randint(0, days_span))

    def _maybe(self, p: float) -> bool:
        return self.rng.random() < p

    # ---------- core generation ----------

    def _build_anomaly_plan(self) -> dict:
        """
        Pre-assign anomaly types to specific order indices so every category
        in the taxonomy is GUARANTEED to appear at least MIN_PER_KIND times.
        This makes the checked-in sample dataset reliable for demos and for
        precision/recall scoring — coverage isn't left to chance.
        """
        kinds = [
            "fee_drift", "tds_mismatch", "split_settlement",
            "refund_clawback_timing", "on_hold", "orphan_bank_credit",
            "duplicate_settlement", "rounding_noise",
        ]
        min_per_kind = 2
        target_total = max(len(kinds) * min_per_kind, int(self.n * self.anomaly_rate))
        target_total = min(target_total, self.n)  # can't exceed number of orders

        plan_kinds = kinds * min_per_kind
        while len(plan_kinds) < target_total:
            plan_kinds.append(self.rng.choice(kinds))
        plan_kinds = plan_kinds[:target_total]
        self.rng.shuffle(plan_kinds)

        indices = list(range(self.n))
        self.rng.shuffle(indices)
        chosen_indices = indices[:target_total]

        return dict(zip(chosen_indices, plan_kinds))

    def generate(self):
        base_date = datetime(2026, 8, 1)
        anomaly_plan = self._build_anomaly_plan()

        for i in range(self.n):
            order_id = self._new_id("order")
            gross = money(self.rng.uniform(300, 25000))
            order_date = self._rand_date(base_date, 20)
            channel = self.rng.choice(CHANNELS)
            linked_account = (
                self._new_id("acc") if self._maybe(0.15) else None
            )
            planned_kind = anomaly_plan.get(i)
            # force a real refund whenever this order is planned for refund_clawback_timing
            if planned_kind == "refund_clawback_timing":
                refund_amount = money(gross * self.rng.uniform(0.05, 0.4))
            else:
                refund_amount = money(gross * self.rng.uniform(0.05, 0.4)) if self._maybe(0.12) else 0.0

            expected_settlement_date = order_date + timedelta(days=2 if channel != "international_card" else 4)

            # --- expected (ledger-side) numbers, using the CORRECT contracted rate ---
            expected_fee = money(gross * CONTRACTED_MDR)
            expected_gst_on_fee = money(expected_fee * GST_ON_FEE_RATE)
            expected_tds = money(gross * DEFAULT_TDS_RATE)
            expected_adjustment = 0.0
            expected_transfer = money(gross * self.rng.uniform(0.1, 0.3)) if linked_account else 0.0
            expected_net = money(
                gross - refund_amount - expected_fee - expected_gst_on_fee
                - expected_tds - expected_adjustment - expected_transfer
            )

            payment_row = {
                "order_id": order_id,
                "order_date": order_date.date().isoformat(),
                "gross_amount": gross,
                "channel": channel,
                "contracted_mdr": CONTRACTED_MDR,
                "refund_amount": refund_amount,
                "linked_account_id": linked_account or "",
                "expected_settlement_date": expected_settlement_date.date().isoformat(),
                "expected_net_amount": expected_net,
            }

            # --- actual settlement report numbers (Razorpay side) — start as a clean mirror ---
            settlement_id = self._new_id("stl")
            actual_fee = expected_fee
            actual_gst_on_fee = expected_gst_on_fee
            actual_tds = expected_tds
            actual_adjustment = expected_adjustment
            actual_transfer = expected_transfer
            actual_refund = refund_amount
            settlement_date = expected_settlement_date
            status = "settled"
            split = False

            # ---------------- inject anomalies (per pre-built plan) ----------------
            if planned_kind is not None:
                kind = planned_kind

                if kind == "fee_drift":
                    drifted_mdr = CONTRACTED_MDR + self.rng.choice([0.0035, 0.006, -0.002])
                    actual_fee = money(gross * drifted_mdr)
                    actual_gst_on_fee = money(actual_fee * GST_ON_FEE_RATE)
                    self.anomalies.append(Anomaly(order_id, "fee_drift",
                        f"contracted_mdr={CONTRACTED_MDR}, observed_mdr={round(drifted_mdr,4)}"))

                elif kind == "tds_mismatch":
                    actual_tds = money(gross * self.rng.choice([0.002, 0.0005]))
                    self.anomalies.append(Anomaly(order_id, "tds_mismatch",
                        f"expected_tds={expected_tds}, actual_tds={actual_tds}"))

                elif kind == "split_settlement":
                    split = True
                    self.anomalies.append(Anomaly(order_id, "split_settlement",
                        "settled across two settlement rows"))

                elif kind == "refund_clawback_timing":
                    settlement_date = settlement_date  # payment settles on time
                    actual_refund = 0.0  # refund NOT in this settlement...
                    self.anomalies.append(Anomaly(order_id, "refund_clawback_timing",
                        f"refund of {refund_amount} appears in a LATER settlement row"))

                elif kind == "on_hold":
                    status = "on_hold"
                    self.anomalies.append(Anomaly(order_id, "on_hold",
                        "settlement withheld for risk review, not in bank yet"))

                elif kind == "orphan_bank_credit":
                    # handled later: we add an extra bank row with no settlement match
                    self.anomalies.append(Anomaly(order_id, "orphan_bank_credit",
                        "bank credit exists with no matching settlement report line"))

                elif kind == "duplicate_settlement":
                    self.anomalies.append(Anomaly(order_id, "duplicate_settlement",
                        "settlement row duplicated (double count risk)"))

                elif kind == "rounding_noise":
                    actual_fee = money(actual_fee + self.rng.choice([0.01, -0.01, 0.02]))
                    self.anomalies.append(Anomaly(order_id, "rounding_noise",
                        "paisa-level rounding difference in fee"))

            actual_net = money(
                gross - actual_refund - actual_fee - actual_gst_on_fee
                - actual_tds - actual_adjustment - actual_transfer
            )

            def make_settlement_row(sid, amt, gross_amt, refund_amt, fee_amt, gst_amt, tds_amt, adj_amt, transfer_amt, status_val, sdate):
                return {
                    "settlement_id": sid,
                    "order_id": order_id,
                    "settlement_date": sdate.date().isoformat(),
                    "channel": channel,
                    "gross_amount": gross_amt,
                    "adjustment": adj_amt,
                    "tax_tds": tds_amt,
                    "fee": fee_amt,
                    "gst_on_fee": gst_amt,
                    "transfer": transfer_amt,
                    "refund": refund_amt,
                    "net_amount": amt,
                    "status": status_val,
                }

            if split:
                half_gross = money(gross / 2)
                half_net = money(actual_net / 2)
                self.settlement_rows.append(make_settlement_row(
                    self._new_id("stl"), half_net, half_gross, money(actual_refund/2),
                    money(actual_fee/2), money(actual_gst_on_fee/2), money(actual_tds/2),
                    money(actual_adjustment/2), money(actual_transfer/2), status, settlement_date))
                self.settlement_rows.append(make_settlement_row(
                    self._new_id("stl"), money(actual_net - half_net), money(gross - half_gross),
                    money(actual_refund - actual_refund/2), money(actual_fee - actual_fee/2),
                    money(actual_gst_on_fee - actual_gst_on_fee/2), money(actual_tds - actual_tds/2),
                    money(actual_adjustment - actual_adjustment/2), money(actual_transfer - actual_transfer/2),
                    status, settlement_date + timedelta(days=1)))
            else:
                row = make_settlement_row(settlement_id, actual_net, gross, actual_refund,
                    actual_fee, actual_gst_on_fee, actual_tds, actual_adjustment, actual_transfer,
                    status, settlement_date)
                self.settlement_rows.append(row)
                if any(a.order_id == order_id and a.kind == "duplicate_settlement" for a in self.anomalies):
                    dup = dict(row)
                    dup["settlement_id"] = self._new_id("stl")
                    self.settlement_rows.append(dup)

            self.payment_rows.append(payment_row)

            # ---------------- bank statement ----------------
            if status == "settled":
                bank_credit_date = settlement_date + timedelta(days=1)
                if split:
                    for srow in self.settlement_rows[-2:]:
                        self.bank_rows.append({
                            "utr": srow["settlement_id"],
                            "credit_amount": srow["net_amount"],
                            "credit_date": bank_credit_date.date().isoformat(),
                            "narration": f"RAZORPAY SETTLEMENT {srow['settlement_id']}",
                        })
                else:
                    self.bank_rows.append({
                        "utr": settlement_id,
                        "credit_amount": actual_net,
                        "credit_date": bank_credit_date.date().isoformat(),
                        "narration": f"RAZORPAY SETTLEMENT {settlement_id}",
                    })

            if any(a.order_id == order_id and a.kind == "orphan_bank_credit" for a in self.anomalies):
                self.bank_rows.append({
                    "utr": self._new_id("utr"),
                    "credit_amount": money(self.rng.uniform(500, 5000)),
                    "credit_date": (base_date + timedelta(days=self.rng.randint(0, 22))).date().isoformat(),
                    "narration": "NEFT CREDIT - UNKNOWN SOURCE",
                })

            if any(a.order_id == order_id and a.kind == "refund_clawback_timing" for a in self.anomalies):
                clawback_settlement_id = self._new_id("stl")
                clawback_date = settlement_date + timedelta(days=self.rng.randint(3, 7))
                self.settlement_rows.append({
                    "settlement_id": clawback_settlement_id,
                    "order_id": order_id,
                    "settlement_date": clawback_date.date().isoformat(),
                    "channel": channel,
                    "gross_amount": 0.0,
                    "adjustment": 0.0,
                    "tax_tds": 0.0,
                    "fee": 0.0,
                    "gst_on_fee": 0.0,
                    "transfer": 0.0,
                    "refund": refund_amount,
                    "net_amount": money(-refund_amount),
                    "status": "settled",
                })
                self.bank_rows.append({
                    "utr": clawback_settlement_id,
                    "credit_amount": money(-refund_amount),
                    "credit_date": (clawback_date + timedelta(days=1)).date().isoformat(),
                    "narration": f"RAZORPAY SETTLEMENT {clawback_settlement_id} (refund adj.)",
                })

    # ---------- tax invoice generation ----------

    def generate_tax_invoices(self):
        """
        Razorpay issues periodic GST tax invoices on the FEE it charges (its
        own supply of service attracts GST). Here we bucket by week and
        deliberately mismatch a few weeks: one invoice missing entirely, one
        with a GST amount that doesn't match what the settlements imply, and
        one computed at the wrong GST rate (12% instead of the correct 18%)
        — the kind of error that genuinely happens when a rate change isn't
        propagated correctly.
        """
        weekly = {}
        for row in self.settlement_rows:
            if row["gross_amount"] <= 0:
                continue  # skip pure refund-clawback rows — no fee/GST on those
            d = datetime.fromisoformat(row["settlement_date"]).date()
            week_start = d - timedelta(days=d.weekday())
            key = week_start.isoformat()
            weekly.setdefault(key, {"fee": 0.0, "gst": 0.0})
            weekly[key]["fee"] += row["fee"]
            weekly[key]["gst"] += row["gst_on_fee"]

        self.tax_invoice_rows = []
        self.tax_anomalies = []

        week_keys = sorted(weekly.keys())
        anomaly_kinds = ["missing_invoice", "gst_amount_mismatch", "wrong_gst_rate"]
        assigned = {}
        shuffled = week_keys[:]
        self.rng.shuffle(shuffled)
        for i, kind in enumerate(anomaly_kinds):
            if i < len(shuffled):
                assigned[shuffled[i]] = kind

        for week_start in week_keys:
            fee_sum = money(weekly[week_start]["fee"])
            gst_sum = money(weekly[week_start]["gst"])
            invoice_id = self._new_id("inv")
            kind = assigned.get(week_start)
            period_end = (datetime.fromisoformat(week_start) + timedelta(days=6)).date().isoformat()

            if kind == "missing_invoice":
                self.tax_anomalies.append(Anomaly(
                    week_start, "missing_invoice",
                    f"No tax invoice issued for week of {week_start} — expected GST ~₹{gst_sum}"))
                continue

            actual_gst = gst_sum
            rate_used = GST_ON_FEE_RATE
            if kind == "gst_amount_mismatch":
                actual_gst = money(gst_sum + self.rng.choice([48.5, -35.2, 61.0]))
                self.tax_anomalies.append(Anomaly(
                    week_start, "gst_amount_mismatch",
                    f"Invoice GST ₹{actual_gst} vs settlement-computed GST ₹{gst_sum}"))
            elif kind == "wrong_gst_rate":
                rate_used = 0.12
                actual_gst = money(fee_sum * rate_used)
                self.tax_anomalies.append(Anomaly(
                    week_start, "wrong_gst_rate",
                    f"Invoice computed at {rate_used*100:.0f}% instead of the correct {GST_ON_FEE_RATE*100:.0f}%"))

            self.tax_invoice_rows.append({
                "invoice_id": invoice_id,
                "period_start": week_start,
                "period_end": period_end,
                "total_fee_amount": fee_sum,
                "total_gst_amount": actual_gst,
                "gst_rate": rate_used,
                "invoice_date": (datetime.fromisoformat(week_start) + timedelta(days=10)).date().isoformat(),
            })

    # ---------- output ----------

    def write(self, out_dir: Path):
        out_dir.mkdir(parents=True, exist_ok=True)

        self._write_csv(out_dir / "payment_ledger.csv", self.payment_rows)
        self._write_csv(out_dir / "settlement_report.csv", self.settlement_rows)
        self._write_csv(out_dir / "bank_statement.csv", self.bank_rows)
        self._write_csv(out_dir / "tax_invoice.csv", self.tax_invoice_rows)

        ground_truth = {
            "total_orders": self.n,
            "total_anomalies_injected": len(self.anomalies),
            "anomalies_by_type": self._count_by_kind(),
            "anomalies": [a.__dict__ for a in self.anomalies],
            "tax_anomalies": [a.__dict__ for a in self.tax_anomalies],
        }
        with open(out_dir / "ground_truth.json", "w") as f:
            json.dump(ground_truth, f, indent=2)

        print(f"Generated {self.n} orders -> {len(self.settlement_rows)} settlement rows, "
              f"{len(self.bank_rows)} bank rows, {len(self.anomalies)} injected anomalies.")
        print("Anomaly breakdown:", self._count_by_kind())
        print(f"Tax invoices: {len(self.tax_invoice_rows)} issued, {len(self.tax_anomalies)} tax anomalies "
              f"({[a.kind for a in self.tax_anomalies]})")

    def _count_by_kind(self):
        counts = {}
        for a in self.anomalies:
            counts[a.kind] = counts.get(a.kind, 0) + 1
        return counts

    @staticmethod
    def _write_csv(path: Path, rows: list[dict]):
        if not rows:
            return
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=int, default=60, help="number of orders to generate (default 60, meets 50+ bar)")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--anomaly-rate", type=float, default=0.35, help="fraction of orders that get an injected anomaly")
    parser.add_argument("--out", type=str, default="./output")
    args = parser.parse_args()

    gen = DataGenerator(args.records, args.seed, args.anomaly_rate)
    gen.generate()
    gen.generate_tax_invoices()
    gen.write(Path(args.out))


if __name__ == "__main__":
    main()