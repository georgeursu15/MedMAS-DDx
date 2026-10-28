# `evaluation/` — batch harness

Runs the full pipeline over a benchmark case set without a human in the loop,
then scores and reports the results. This is what produced the numbers in the
paper.

The only component that exists solely for evaluation is the **simulated
patient**: an LLM holding the ground-truth symptom profile that answers a whole
batch of follow-up questions in one call. It never takes part in a live
diagnosis. Because it answers from the complete profile, the figures this
harness produces are an **optimistic estimate**, not real-patient performance.

---

## Modules

| File | Role |
|---|---|
| `eval_runner.py` | The core loop. Loads cases, runs the pipeline per case and variant, decides correctness, writes progress. |
| `batch_runner.py` | Drives several configurations in one batch from a JSON config, assigns test IDs, keeps a batch file updated as runs complete. |
| `eval_adapter.py` | Turns raw results into spreadsheet rows and writes the `.xlsx` export. |
| `eval_report.py` | Confusion matrix, failure analysis, terminal report, CSV and HTML export. |
| `eval_frontend.html` | Browser view for inspecting a completed batch. |

---

## Running

Through the Docker test runner (preferred — see [`Benchmark/`](../../../Benchmark/)):

```bash
./run_tests.sh -- --variant 100 --limit 5
./run_tests.sh --full
./run_tests.sh --config tests.json
```

Directly:

```bash
python -m MedMAS_DDx.evaluation.eval_runner --variant 80 --limit 10
python -m MedMAS_DDx.evaluation.batch_runner --config tests.json
python -m MedMAS_DDx.evaluation.eval_report eval_results/<batch>/<file>.json
```

### `eval_runner` arguments

| Flag | Meaning |
|---|---|
| `--variant 100\|80\|50` | Run one symptom-completeness variant. |
| `--prognosis NAME` | Filter cases by disease name, substring, case-insensitive. |
| `--limit N` | Cap the number of (case, variant) pairs. |
| `--batch-id ID` | Group several runs into one results folder. |
| `--retry-failed FILE` | Re-run only the failures recorded in a previous results file. |

---

## Completeness variants

Each benchmark case exists at three disclosure levels: `100%`, `80%` and `50%`
of its canonical symptom list. The 100% variant leaves nothing to ask about, so
the interactive phase has no work to do there; the paper reports the 80% and
50% variants for that reason.

---

## How correctness is decided

`_disease_match` and `_symptom_matches` compare the prediction against the
reference label using the SapBERT scorer from
[`tools/similarity_scorer.py`](../tools/similarity_scorer.py) at a threshold of
`0.6`, not by string equality.

Matching is also **tie-aware**: a disease tied on score with the first-ranked
candidate counts as Top-1. Without that, a correct answer sharing an identical
score with another candidate would be graded wrong on alphabetical ordering
alone.

---

## Output

Results land in `eval_results/`, logs in `logs/`. A batch run creates
`eval_results/<batch_id>/` holding one file per configuration plus a batch
summary that is recomputed as each test finishes, so an interrupted batch can be
inspected or resumed rather than restarted.

`_classify_run` in `eval_adapter.py` labels each run so that failures caused by
infrastructure can be separated from genuine diagnostic misses.
