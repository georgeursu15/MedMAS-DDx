# `graphdb/` — knowledge base

The ontologies, the repository definitions, and the script that loads one into
the other. This is the only source of medical facts in the system: if a
disease–symptom link is not in here, the pipeline cannot propose it.

---

## Contents

| File | Role |
|---|---|
| `repo-config-DoidOntologyRepository.ttl` | Repository definition for DOID + SYMP. |
| `repo-config-HpOntologyRepository.ttl` | Repository definition for HPO. |
| `init-graphdb.sh` | Waits for GraphDB, creates both repositories, downloads the ontologies, loads them. |
| `data/` | Where the downloaded `.owl` files are cached. Empty in a fresh clone. |
| `license/graphdb.license` | Your GraphDB license. **Not supplied** — see below. |

Both repositories use the `rdfsplus-optimized` ruleset.

---

## The ontologies

They are **not vendored here**. `init-graphdb.sh` downloads them on first run
into `data/`, from URLs that name an exact, immutable OBO release:

| Ontology | Release | Size | Licence |
|---|---|---|---|
| [Human Disease Ontology](http://purl.obolibrary.org/obo/doid/releases/2026-04-30/translations/doid-international.owl), international edition | 2026-04-30 | 33 MB | [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/) |
| [Human Phenotype Ontology](http://purl.obolibrary.org/obo/hp/releases/2026-02-16/hp.owl) | 2026-02-16 | 73 MB | [HPO's own terms](https://hpo.jax.org/) |

DOID supplies the disease vocabulary **and bundles 894 SYMP symptom classes**,
which is why there is no separate SYMP file. HPO is used only as a synonym
bridge when a symptom fails to map against SYMP.

Fetching rather than vendoring keeps 106 MB of OWL out of git, and pins the
version in a way a checked-in copy never did: the release date is part of the
URL, so what the knowledge base contains is declared in
[`init-graphdb.sh`](init-graphdb.sh) rather than implied by whatever happened
to be committed.

Cite them as:

> L. M. Schriml et al., "The Disease Ontology: a backbone for disease semantic
> integration," *Nucleic Acids Research*. DOID is released under CC0 1.0.
>
> S. Köhler et al., "The Human Phenotype Ontology," *Nucleic Acids Research*.
> HPO is distributed under its own licence; see <https://hpo.jax.org/>.

---

## Repositories

| Repository | Holds | Reached through |
|---|---|---|
| `DoidOntologyRepository` | DOID + SYMP | `GRAPHDB_REPOSITORY_URL` |
| `HpOntologyRepository` | HPO | `HP_GRAPHDB_REPOSITORY_URL` |

Disease–symptom links are expressed as OWL restrictions on `RO_0002452`
("has symptom"), with symptom URIs in the `http://purl.obolibrary.org/obo/SYMP_`
namespace and disease URIs under `DOID_`.

---

## License

GraphDB 10 and later require a license file even for the Free edition. Without
one, `init-graphdb.sh` stops with an explicit error rather than failing
obscurely later.

Request a GraphDB Free license at
<https://www.ontotext.com/products/graphdb/#try-graphdb> and place it at:

```
graphdb/license/graphdb.license
```

---

## Initialisation

```bash
docker compose -f docker-compose.external-llm.yml up -d graphdb
docker compose -f docker-compose.external-llm.yml run --rm graphdb-init
```

The script waits for GraphDB to answer on `${GRAPHDB_URL}/rest/repositories`,
then creates and populates both repositories. It is **idempotent**: if the
repositories already exist it exits cleanly, so it is safe to leave in a
startup sequence.

The first run takes several minutes — 106 MB of OWL has to be parsed and
indexed. Subsequent starts reuse the Docker volume (`graphdb-data`, or
`graphdb-data-extern` in the external-LLM compose file) and are fast.

---

## Checking it worked

```bash
curl http://localhost:7200/rest/repositories
```

Both `DoidOntologyRepository` and `HpOntologyRepository` should be listed. The
GraphDB Workbench at <http://localhost:7200> is useful for trying the query
patterns documented in
[`src/MedMAS_DDx/tools/README.md`](../src/MedMAS_DDx/tools/README.md) by hand.

---

## Replacing an ontology

Change the release URL in [`init-graphdb.sh`](init-graphdb.sh), clear `data/`,
and re-run the init against an empty volume. To use a file you already have
rather than a published release, drop it into `data/` under the expected name —
the script leaves a file that is already there alone.

One caveat worth knowing: the ranking depends on **disease counts per symptom**,
so swapping an ontology for a differently scoped one changes every specificity
weight and therefore every Phase 1 score. Results are not comparable across
ontology versions.
