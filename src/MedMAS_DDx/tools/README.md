# `tools/` — knowledge access and scoring

Two kinds of thing live here: the SPARQL query tools that reach the knowledge
base, and the scoring functions that decide the ranking. Both are deterministic.
No agent calls any of them — `main.py` does, and passes the results into the
task descriptions.

---

## Query tools

| File | Purpose |
|---|---|
| `graphdb_ontology_query_tool.py` | SPARQL against `DoidOntologyRepository` (DOID + SYMP). Endpoint from `GRAPHDB_REPOSITORY_URL`. |
| `hp_ontology_query_tool.py` | SPARQL against `HpOntologyRepository` (HPO), used as the synonym fallback. Endpoint from `HP_GRAPHDB_REPOSITORY_URL`. |
| `batch_disease_query_tool.py` | Fetches symptom profiles for several diseases in one round trip, instead of one query per candidate. |

The DOID tool carries three documented query patterns:

1. **symptom label → SYMP URI**, filtered to the `SYMP_` namespace;
2. **disease URI → its symptom profile**, over the `RO_0002452` restriction;
3. **symptom URI → every disease that lists it**, which is how candidates are
   retrieved.

---

## `diagnosis_ranking_tool.py` — Phase 1

`rank_diagnoses` produces the initial differential. Pure function, no I/O.

```
specificity_weight(s) = 1 / log2(1 + disease_count(s))
raw_score(d)          = Σ specificity_weight(s) for s in matched(d)
coverage(d)           = match_count(d) / total_mapped_symptoms
composite_score(d)    = raw_score(d) * (0.5 + 0.5 * coverage(d))
```

A rare symptom is worth more than a common one; the logarithm stops a very
large disease count from collapsing the weight entirely. Coverage is applied as
a **bounded** multiplier in `[0.5, 1]` so that it adjusts the evidence rather
than dominating it — multiplying the raw score by coverage directly would nearly
erase a disease indicated by a single highly specific symptom.

Other behaviour in this function:

- **Ordering** is `(-composite_score, -match_count, disease_name)`, so ties
  break deterministically and runs are reproducible.
- **Retention** keeps a candidate when `composite_score >= 0.1 * top_score` or
  `match_count >= 2`.
- **Weak evidence** is flagged when the top composite score is below `0.5`;
  confidence is capped under that condition.

---

## `refine_scoring_tool.py` — Phase 2

`RefineScoring` / `compute_refinement` rescore the differential once the
follow-up answers arrive.

```
discriminative_weight(s) = 1 - diseases_with_symptom(s) / n_profiled
final_score(d)           = composite_score(d) * (1 + α·support(d) - β·conflict(d))
```

with `α = 0.6` (`REFINE_SUPPORT_WEIGHT`) and `β = 0.2`
(`REFINE_CONFLICT_WEIGHT`), both overridable from the environment through
`resolve_weights`.

A symptom shared by every remaining candidate gets weight zero: asking about it
separates nothing. Support and conflict are summed over the questions relevant
to that disease and normalised by their count, so both land in `[0, 1)`.

**Why linear here and logarithmic in Phase 1.** The Phase 1 weight ranks rarity
across the whole ontology, where disease counts run into the hundreds, so a
logarithm is needed. The Phase 2 weight measures separation inside a small,
shrinking candidate pool and feeds a multiplicative update, which needs a
bounded input. Logarithmic IDF is unbounded above; the linear complement is
bounded by construction.

**Why α > β.** A confirmed specific symptom is stronger evidence than a denied
one, because a denial may mean absent, not yet manifest, or simply unnoticed.
Raising β lowers the floor of the update factor and starts eliminating
plausible candidates whose symptoms were merely unobserved.

Three properties follow from the formulation, for any `α > β > 0`:

- **bounded** — `1 - β < μ(d) < 1 + α`, so the adjustment cannot run away;
- **sign-preserving** — refinement reorders candidates that were already close,
  it cannot rescue one that was far behind;
- **neutral** — no relevant question, or an `unsure` answer, leaves `μ(d) = 1`.

`_find_most_impactful` identifies the answer that moved the ranking most,
weighting each confirmed or denied symptom by how many diseases it actually
changed.

---

## `similarity_scorer.py`

`DiagnosisSimilarityScorer` wraps
`cambridgeltl/SapBERT-from-PubMedBERT-fulltext` with a default threshold of
`0.6`. Model and tokenizer load lazily on first use.

Used by the **evaluation harness only**, to decide whether a predicted label
matches the reference diagnosis. Exact string matching would be far stricter
than clinical correctness — a prediction of *Viral Hepatitis* against a
reference of *Hepatitis B* is right in substance and wrong as a string.
