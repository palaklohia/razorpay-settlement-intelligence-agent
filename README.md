# Settlement Intelligence — AI Finance Controller for Razorpay

🔗 Live Demo: https://razorpay-settlement-intelligence.vercel.app/

Razorpay settlement reconciliation and financial intelligence platform
for transaction analysis, exception detection, tax matching, and 
settlement forecasting.

Reconstructs the full Razorpay settlement waterfall for a batch of orders —
gross payment → refund → fee → GST on fee → TDS → transfer → net settlement
→ bank credit — reconciles every leg across three independent sources, and
produces an honest exception list with root cause, confidence, and ₹ impact
for anything that doesn't tie out.

Built for the Razorpay Buildathon (RACK 04 — AI Finance Controller).

## The pitch in one line

Most reconciliation demos match a payment to a payout. This reconstructs
**every rupee of the settlement waterfall** across three sources, and when
something doesn't add up, it tells you *why*, *how confident it is*, and
*what it's worth* — instead of just saying "mismatch."

## Live demo: "Explain this ₹"

Click any order in the dashboard and watch the waterfall animate step by
step, ending in a side-by-side comparison of expected vs actual settlement
vs bank credit, plus any exception with a plain-English root cause.

## Testing

```bash
pip3 install pytest pandas
python3 -m pytest tests/ -v
```

20 unit tests covering `reconcile.py` and `exceptions.py` independently of the
synthetic data generator — clean matches, fee drift, on-hold, missing bank
credit, orphan credits, duplicate vs. split settlement, TDS mismatch, and the
`unclassified_discrepancy` safety net. One test documents a specific,
non-obvious design decision worth knowing about: a tiny MDR deviation (0.02%)
is correctly judged too small to call "fee_drift," but on a large enough
order that same tiny rate difference can still exceed the rounding-noise
ceiling in absolute rupees — the engine falls through to
`unclassified_discrepancy` rather than either mislabeling it or silently
absorbing it.

## Bonus features beyond the core reconciliation loop

- **Cash forecast (`engine/forecast.py`, `GET /api/forecast`)** — projects next-7-day
  expected inflow from money the engine has *already identified* as pending: held
  settlements (release modeled over days 3-6, confidence 0.55) and settlements
  reported by Razorpay but not yet seen in the bank statement (modeled to land
  within days 1-2, confidence 0.85). This is derived arithmetic on already-verified
  state, not a separate forecasting model — see the module docstring for the exact
  methodology.
- **AI-narrated exceptions (`engine/explain.py`, `GET /api/orders/{id}/explain`)** —
  Claude turns an already-classified, already-confidence-scored finding into a
  2-3 sentence plain-English explanation on request. It cannot change the
  classification or any number — verification stays 100% deterministic.
- **Settlement Q&A agent (`engine/qa.py`, `GET /api/ask`)** — free-form questions
  about the current batch ("which orders have the largest fee drift?"),
  answered strictly from the already-computed report data. Claude is told
  explicitly to say so if the data doesn't contain the answer, rather than
  extrapolate.
- **Tax-line matcher (`engine/tax_matcher.py`, `GET /api/tax-matches`)** —
  Razorpay's fee is itself a taxable supply, and it issues periodic GST
  invoices on the fees it charges. This module buckets settlements by week
  and checks the invoice for that week against what the settlement report's
  fee/GST line items actually imply — catching a missing invoice, an invoice
  computed at the wrong GST rate, or a GST total that doesn't match within
  tolerance. This is the same class of manual, error-prone check a finance
  team does before filing input tax credit claims. Fully deterministic, same
  as the core reconciliation engine — no model involved.
- **Command palette (⌘K in the frontend)** — jump to any order by ID or amount
  without scrolling the ledger.
- **Audit CSV export** — one-click download of the full exception list for
  handoff to a CA or finance team.

## Architecture

```text
                 ┌────────────────────┐
                 │  Payment Ledger     │   (internal system of record)
                 └──────────┬──────────┘
                 ┌──────────▼──────────┐
                 │ Settlement Report    │   (Razorpay's actual payout data)
                 └──────────┬──────────┘
                 ┌──────────▼──────────┐
                 │  Bank Statement      │   (ground truth of cash received)
                 └──────────┬──────────┘
                            │
                    ┌───────▼────────┐
                    │  RECONCILE      │   engine/reconcile.py
                    │  Recompute the  │   — deterministic waterfall math,
                    │  waterfall at   │     no LLM in this path
                    │  contracted     │
                    │  rates, diff    │
                    │  vs actual      │
                    └───────┬────────┘
                            │
                    ┌───────▼────────┐
                    │  CLASSIFY       │   engine/exceptions.py
                    │  8 rule-based   │   — every category maps to an
                    │  exception      │     explicit, inspectable rule;
                    │  categories +   │     confidence is a measured signal
                    │  confidence     │     (pattern recurrence), not a
                    │  + ₹ impact     │     model's guess
                    └───────┬────────┘
                            │
              ┌─────────────┼──────────────┐
              │             │               │
              ▼             ▼               ▼
         CLEAN MATCH   EXCEPTIONS      UNCLASSIFIED
         (80.3%)       (root cause,    (safety net —
                        confidence,     doesn't force
                        recommended     unfamiliar cases
                        action)         into a known bucket)
              │             │               │
              └─────────────┼───────────────┘
                            ▼
                  ┌───────────────────┐
                  │   FastAPI (api/)   │  serves report + per-order
                  │                    │  waterfall as JSON
                  └─────────┬──────────┘
                            ▼
                  ┌───────────────────┐
                  │  Frontend          │  "Explain this ₹" — ledger view
                  │  (frontend/)       │  + animated waterfall per order
                  └───────────────────┘
```

## Repo structure

```
data/
  generator/          synthetic data generator (3 sources + injected anomalies)
  sample_output/       curated 61-order demo batch, committed for reproducibility
engine/
  reconcile.py         waterfall reconstruction + diffing (deterministic)
  exceptions.py         8-category exception classifier + confidence scoring
  waterfall.py          per-order step builder for the "Explain this ₹" view
  run.py                 orchestrator: produces report.json
  score.py               scores report.json against ground_truth.json
api/
  main.py                FastAPI layer serving report / waterfall / scorecard
frontend/
  index.html              single-file dashboard (no build step)
```

## Running it

**1. Generate data** (optional — `data/sample_output/` already has a
curated batch checked in):
```bash
cd data/generator
python3 generate_data.py --records 60 --seed 42 --out ../sample_output
```

**2. Run the reconciliation engine:**
```bash
cd engine
python3 run.py --data-dir ../data/sample_output --out report.json
python3 score.py --report report.json --ground-truth ../data/sample_output/ground_truth.json
```

**3. Start the API:**
```bash
cd api
pip3 install -r requirements.txt
python3 -m uvicorn main:app --reload --port 8000
```

**4. Serve the frontend:**
```bash
cd frontend
python3 -m http.server 5500
```
Open `http://localhost:5500`.

## Results (61-order batch, checked in at `data/sample_output/`)

| Metric | Value |
|---|---|
| Clean match rate | 80.3% (49 / 61 orders) |
| Value reconciled | ₹6,28,892 of ₹8,03,282 (78.3%) |
| Total exceptions | 20 |
| Throughput | ~440 records/sec (measured on a 500-order stress batch) |

**Exception breakdown:** on_hold (3), tds_mismatch (3), refund_clawback_timing (4),
duplicate_settlement (2), fee_drift (3), split_settlement (2), orphan_bank_credit (2),
unclassified_discrepancy (1).

**Precision / recall against ground truth:** 1.00 / 1.00 across all 8 known
categories (`engine/score.py` output, reproducible from the command above).

### Why we're not just leading with "100% accuracy"

That score is measured against a synthetic set built from the same 8 rules
the engine checks for — it proves rule-fidelity, not real-world
generalization, and we say so up front rather than let a judge assume
otherwise.

What we think is the more honest proof point: `data/sample_output/` also
contains one hand-crafted order (`order_edge_case_chargeback01`) with a
deduction pattern **outside the 8-category taxonomy** — a hidden chargeback
fee not represented anywhere in our waterfall schema. The engine doesn't
force it into a wrong category. It flags it as `unclassified_discrepancy`
at low confidence (0.3) with the exact ₹350 impact, for manual review. A
system that can say "I don't know what this is" is more trustworthy than
one that's always confident.

We also caught and fixed a real bug in the data generator during
development: an early version silently created meaningless "refund
clawback" rows on orders that had zero refund, which inflated the
orphan-bank-credit count. It was only caught by scoring against ground
truth instead of trusting the top-line match rate — which is the exact
discipline this whole project is meant to demonstrate.

## Design decisions worth calling out

- **Confidence scores are computed, not asserted.** Fee-drift confidence,
  for example, is derived from how many other settlements in the batch show
  the same MDR deviation — a measurable pattern, not an LLM's guess. This
  matters because it's defensible under questioning.
- **Reconciliation is 100% deterministic.** No LLM sits in the
  `reconcile.py` / `exceptions.py` path. An LLM would only be appropriate
  for turning a structured finding into a plain-English explanation — the
  classification itself should never be a guess.
- **Every order that fails clean-match has to produce an exception.**
  `exceptions.py` has an explicit catch-all so a mismatched order can never
  silently disappear between "matched" and "explained" — see
  `unclassified_discrepancy`.
- **Tolerance is deliberately wider than injected rounding noise.** Real
  paisa-level rounding (₹0.01–0.02) is absorbed into a clean match rather
  than manufacturing a fake exception; the threshold (₹0.05) is documented
  in `reconcile.py`.

## Known limitations / next steps

- Detection rules are tuned to the 8 anomaly types we modeled from
  Razorpay's public settlement documentation; a production version would
  need exposure to real settlement data to find categories we haven't
  thought of.
- Multi-currency / international-card FX legs are represented but not
  independently verified against a forex rate source.
