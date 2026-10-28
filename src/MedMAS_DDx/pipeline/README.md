# `pipeline/` — deterministic flows

Everything between "the model produced some symptom strings" and "we have a
scored list of candidate diseases". No agent runs here. Each flow is a class of
static methods, called directly from `main.py`.

This is the layer that gives the system its grounding guarantee: a disease
reaches the differential only because a SPARQL query returned it, never because
a model suggested it.

---

## `symptom_mapper.py` — `SymptomMapperFlow`

Resolves extracted symptom strings to SYMP concepts.

| Method | Role |
|---|---|
| `_sparql_symp_search` | Label search restricted to the `SYMP_` namespace. |
| `_symp_map_one` | Resolution strategy for a single symptom: canonical term first, then alternative phrasings, then the HPO synonym bridge. |
| `python_symp_mapping` | Entry point. Maps the whole extracted list and returns both the resolved concepts and the ones that failed. |

Symptoms that resolve to nothing are **kept in the output for traceability but
excluded from retrieval**, so an unrecognised term cannot pull in diseases or
distort coverage.

---

## `disease_mapper.py` — `DiseaseMapperFlow`

`python_disease_mapping` takes the resolved SYMP URIs and queries the graph for
every disease that declares them, through the `RO_0002452` ("has symptom")
restriction. The result is the raw candidate set, before any scoring.

---

## `disease_profiler.py` — `DiseaseProfilerFlow`

Runs after the first ranking, only when follow-up questioning is needed.

| Method | Role |
|---|---|
| `_build_disease_uri_lookup` | Maps disease labels back to URIs so profiles can be fetched. |
| `_select_candidate_diseases` | Picks which ranked candidates are worth profiling. |
| `python_fetch_disease_profiles` | Fetches the full symptom profile of each selected candidate. |

The profiles this produces are what the `followup_interviewer` agent receives
inside its task description. The agent itself issues no queries.

---

## `utils.py`

| Class | Role |
|---|---|
| `PipelineLogger` | Builds the per-run log file and tees terminal output into it, so a run can be reconstructed from the log. |
| `PayloadExtractor` | Parses JSON out of model output that may be wrapped in prose or code fences. |

---

## Conventions

- Flows are **static methods on a class**, not instances. There is no flow state.
- Every method either returns a plain `dict`/`list` or raises. Nothing mutates
  a shared context object.
- SPARQL lives in the tools layer; flows call tools, they do not build HTTP
  requests themselves.
