# `Benchmark/` — case set and test runner

The evaluation data and the script that runs it inside Docker.

---

## Data

### `PatientCases.csv`

99 cases, one per row. Each case carries a reference diagnosis, its canonical
symptom list, and the same clinical narrative written at three levels of
disclosure.

| Column | Contents |
|---|---|
| `prognosis` | Reference diagnosis. |
| `symptoms` | Canonical symptom list, comma-separated. |
| `100% Case` | Narrative containing the full symptom list. |
| `80% Case` | Narrative with 20% of the symptoms withheld. |
| `50% Case` | Narrative with half the symptoms withheld. |

The withheld symptoms are what the follow-up questions have to recover. At
100% there is nothing left to ask, which is why the paper reports the 80% and
50% variants.

### `MedQDx_Benchmark.csv`

The same 99 cases plus the baseline's interaction trace: for each of three
turns, the question asked, the answer given, the diagnosis proposed and its
similarity to the reference. Used for the comparison against the MedQDx
baseline.

### `tests.json`

Batch configuration consumed by `batch_runner`. Mirrors the "Add to batch"
flow in the evaluation frontend: a list of run configurations executed in
sequence into one results folder.

---

## `run_tests.sh`

Builds the image if needed and runs the evaluation inside the app container.
Results appear in `eval_results/` and `logs/` on the host.

```bash
./run_tests.sh [OPTIONS] [-- EVAL_RUNNER_ARGS...]
```

| Option | Effect |
|---|---|
| `--up` | Start GraphDB and the LLM service, then run the idempotent GraphDB init. |
| `--pull-model` | Pull the model. Needed once, or after changing it. |
| `--build` | Force-rebuild the app image. |
| `--full` | Run all three variants into one shared batch folder. |
| `--config FILE` | Run a batch from a JSON config. |
| `--down` | Stop and remove the containers. |

Anything after `--` is forwarded to `eval_runner`.

### Typical sequence

```bash
./run_tests.sh --up --pull-model            # first time
./run_tests.sh -- --variant 100 --limit 5   # smoke test
./run_tests.sh --full                       # the real run
./run_tests.sh --down
```

Re-running only what failed:

```bash
./run_tests.sh -- --retry-failed eval_results/batch_YYYYMMDD_HHMMSS/<file>.txt \
                  --batch-id batch_YYYYMMDD_HHMMSS
```

Passing the same `--batch-id` keeps the retried results in the original folder
instead of starting a new one.

---

## `retry_failed_tests.py`

Standalone recovery script for batches that ended with infrastructure failures
rather than diagnostic ones. Useful when a long run was interrupted by a model
timeout or a container restart and you want the affected cases re-run without
repeating the whole batch.

---

## A note on the numbers

Answers come from a simulated patient with access to the full symptom profile,
so results from this harness are an upper bound on what the same pipeline would
achieve with a real patient who may misremember, misreport, or not know.
