# `Benchmark/` — case set and test runner

The evaluation data and the script that runs it inside Docker.

---

## Getting the data

The two CSV files are **not kept in this repository** — they belong to the
authors of the Q4Dx benchmark, whose repository states no licence. Download
them yourself from the source:

### <https://github.com/MaiWert/MedQDx>

Two files, and **both are renamed on the way in**. The names in that repository
are not the names this code expects:

| In MedQDx | Save here as |
|---|---|
| `EDA and Baseline/Patient Cases.csv` | `Benchmark/PatientCases.csv` |
| `Benchmark Creation/MedQDx_Benchmark.csv` | `Benchmark/MedQDx_Benchmark.csv` |

Note the space that disappears from `Patient Cases.csv`. Keeping it gives a
"file not found" from the evaluation runner rather than anything that points
at the real cause.

### Verifying what you downloaded

The published results were produced against commit
[`caec179`](https://github.com/MaiWert/MedQDx/tree/caec1794c4bdbb6fee46e506cc820a01263dc2d8).
If the upstream files change, yours will no longer match these:

```
48814e6498d1436fe30a078a9dd9c660559538e628844b747b9e0e5b49c2583a  PatientCases.csv
ba05009f00f20cbba2fa630b330a4fb8326e316f2157f478a9199a2624e4fd3d  MedQDx_Benchmark.csv
```

```bash
shasum -a 256 Benchmark/*.csv      # sha256sum on Linux
```

A mismatch is not necessarily an error — it means you have a different revision
of the benchmark, and your numbers will not be comparable to the paper's.

Do this before building the Docker image, which copies both files in and fails
if they are absent.

### Whose data this is

The Q4Dx benchmark is somebody else's work:

> M. Werthaim, M. Kimhi, A. Apartsin and Y. Aperstein, "A benchmark for
> evaluating diagnostic questioning efficiency of LLMs in patient
> conversations," *Scientific Reports*, vol. 16, no. 1, p. 6121, Jan. 2026.
> doi: [10.1038/s41598-026-37022-y](https://doi.org/10.1038/s41598-026-37022-y)
> · data: <https://github.com/MaiWert/MedQDx>

Their repository carries no licence file, so no rights to redistribute are
granted, and the article is published under CC BY-NC-ND 4.0. That is why there
is no copy here and no script that fetches one: whether to take the data is a
decision for whoever is reading this, not something to automate around its
authors. **If you use this benchmark, cite the paper above.**

The cases in turn derive from the SymbiPredict dataset (J. Tucker, Mendeley
Data, [10.17632/dv5z3v2xyd.1](https://doi.org/10.17632/dv5z3v2xyd.1)); the
narratives were generated from those symptom–disease pairs by the Q4Dx authors.

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

The same cases plus the baseline's interaction trace: for each of three turns,
the question asked, the answer given, the diagnosis proposed and its similarity
to the reference. Used for the comparison against the MedQDx baseline.

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
