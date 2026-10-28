# `graphdb/` — knowledge base

The ontologies, the repository definitions, and the script that loads one into
the other. This is the only source of medical facts in the system: if a
disease–symptom link is not in here, the pipeline cannot propose it.

---

## Contents

| File | Role |
|---|---|
| `doid-international.owl` | Human Disease Ontology, international edition (33 MB). Supplies the disease vocabulary **and bundles the SYMP symptom terms**, which is why there is no separate SYMP file. |
| `hp.owl` | Human Phenotype Ontology (73 MB). Used only as a synonym bridge. |
| `repo-config-DoidOntologyRepository.ttl` | Repository definition for DOID + SYMP. |
| `repo-config-HpOntologyRepository.ttl` | Repository definition for HPO. |
| `init-graphdb.sh` | Waits for GraphDB, creates both repositories, loads the ontologies. |
| `license/graphdb.license` | Your GraphDB license. **Not supplied** — see below. |

Both repositories use the `rdfsplus-optimized` ruleset.

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

Drop the new `.owl` in this folder, point the matching `repo-config-*.ttl` at
it, and re-run the init against an empty volume.

One caveat worth knowing: the ranking depends on **disease counts per symptom**,
so swapping an ontology for a differently scoped one changes every specificity
weight and therefore every Phase 1 score. Results are not comparable across
ontology versions.
