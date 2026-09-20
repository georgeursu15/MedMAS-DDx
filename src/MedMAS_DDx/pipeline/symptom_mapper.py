import json
from typing import Any
from MedMAS_DDx.tools.graphdb_ontology_query_tool import GraphDbOntologyQueryTool
from MedMAS_DDx.tools.hp_ontology_query_tool import HpOntologyQueryTool


class SymptomMapperFlow:
    @staticmethod
    def _sparql_symp_search(
        graphdb: GraphDbOntologyQueryTool, term: str
    ) -> tuple[str, str, str] | None:
        
        safe = term.lower().replace('"', "").replace("\\", "")
        query = (
            "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>\n"
            "SELECT ?symptom ?symptomLabel\n"
            "WHERE {\n"
            "  ?symptom rdfs:label ?symptomLabel .\n"
            '  FILTER(STRSTARTS(STR(?symptom), "http://purl.obolibrary.org/obo/SYMP_"))\n'
            f'  FILTER(CONTAINS(LCASE(STR(?symptomLabel)), "{safe}"))\n'
            "}"
        )
        try:
            result = json.loads(graphdb._run(sparql_query=query))
        except Exception:
            return None

        candidates = result.get("symp_candidates", [])
        if not candidates:
            return None

        c = candidates[0]
        return c.get("symp_uri"), c.get("symp_label"), c.get("parsed_symp_number")

    @staticmethod
    def _symp_map_one(
        graphdb: GraphDbOntologyQueryTool,
        hp: HpOntologyQueryTool,
        symptom_name: str,
        variants: list[str] | None = None,
    ) -> dict[str, Any]:
        

        seen_lc: set[str] = set()
        candidates: list[str] = []
        for term in [symptom_name, *(variants or [])]:
            if not term:
                continue
            clean = term.strip()
            if not clean:
                continue
            lc = clean.lower()
            if lc in seen_lc:
                continue
            seen_lc.add(lc)
            candidates.append(clean)

        def _build_note(used_term: str, prefix: str) -> str:
            if used_term == symptom_name:
                return prefix
            suffix = f"variant '{used_term}'"
            return f"{prefix} via {suffix}" if prefix else f"matched via {suffix}"

        for term in candidates:
            hit = SymptomMapperFlow._sparql_symp_search(graphdb, term)
            if hit:
                uri, label, number = hit
                return {
                    "symptom": symptom_name,
                    "matched_symp_uri": uri,
                    "matched_symp_label": label,
                    "parsed_symp_number": number,
                    "status": "mapped",
                    "notes": _build_note(term, ""),
                }

        for term in candidates:
            try:
                hp_result = json.loads(hp._run(search_term=term))
            except Exception:
                continue

            hp_matches = hp_result.get("hp_matches") or []
            if not hp_matches:
                continue

            first_hp_label = (hp_matches[0].get("hp_label") or "").strip()
            if not first_hp_label:
                continue

            hit = SymptomMapperFlow._sparql_symp_search(graphdb, first_hp_label)
            if hit:
                uri, label, number = hit
                note = _build_note(term, f"HP fallback → '{first_hp_label}'")
                return {
                    "symptom": symptom_name,
                    "matched_symp_uri": uri,
                    "matched_symp_label": label,
                    "parsed_symp_number": number,
                    "status": "mapped",
                    "notes": note,
                }

        return {
            "symptom": symptom_name,
            "matched_symp_uri": None,
            "matched_symp_label": None,
            "parsed_symp_number": None,
            "status": "unmapped",
            "notes": (
                f"no SYMP match across {len(candidates)} candidate(s); "
                "HP fallback also failed for every candidate"
            ),
        }

    @staticmethod
    def python_symp_mapping(extracted_symptoms: list[dict[str, Any]]) -> list[dict[str, Any]]:
       
        graphdb = GraphDbOntologyQueryTool()
        hp = HpOntologyQueryTool()
        mappings = []
        for sym in extracted_symptoms:
            name = (sym.get("name") or "").strip()
            if not name:
                continue
            raw_variants = sym.get("variants") or []
            variants = [
                str(v).strip() for v in raw_variants
                if isinstance(v, str) and str(v).strip()
            ]
            print(f"  [symp-map] '{name}' ...", end=" ", flush=True)
            entry = SymptomMapperFlow._symp_map_one(graphdb, hp, name, variants)
            label = entry.get("matched_symp_label") or "-"
            print(f"{entry['status']} → {label}")
            mappings.append(entry)
        return mappings
