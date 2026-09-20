import json
from typing import Any
from MedMAS_DDx.tools.graphdb_ontology_query_tool import GraphDbOntologyQueryTool


class DiseaseProfilerFlow:
    @staticmethod
    def _build_disease_uri_lookup(disease_payload: dict[str, Any]) -> dict[str, str]:
        lookup: dict[str, str] = {}
        for entry in disease_payload.get("disease_results", []):
            for de in entry.get("disease_entries", []):
                label = (de.get("disease_label") or "").strip().lower()
                uri = (de.get("disease_uri") or "").strip()
                if label and uri and label not in lookup:
                    lookup[label] = uri
        return lookup

    @staticmethod
    def _select_candidate_diseases(
        ranking_payload: dict[str, Any], max_diseases: int = 25
    ) -> list[dict[str, Any]]:
        
        diff: list[dict] = ranking_payload.get("differential_diagnosis", [])
        selected: list[dict] = []

        for d in diff:
            if d.get("confidence", "").upper() in ("HIGH", "MEDIUM"):
                selected.append(d)

        if diff:
            top_co = max((d.get("co_occurrence_score", 0) for d in diff), default=0)
            for d in diff:
                if d.get("confidence", "").upper() == "LOW" and d.get("co_occurrence_score", 0) == top_co:
                    if d not in selected:
                        selected.append(d)

        return selected[:max_diseases]

    @staticmethod
    def python_fetch_disease_profiles(
        disease_payload: dict[str, Any],
        ranking_payload: dict[str, Any],
    ) -> list[dict[str, Any]]:
       
        graphdb = GraphDbOntologyQueryTool()
        uri_lookup = DiseaseProfilerFlow._build_disease_uri_lookup(disease_payload)
        candidates = DiseaseProfilerFlow._select_candidate_diseases(ranking_payload)

        profiles: list[dict[str, Any]] = []
        print(f"  [profile-fetch] {len(candidates)} candidate disease(s) to profile ...")

        for d in candidates:
            disease_label = (d.get("disease") or "").strip()
            disease_uri = uri_lookup.get(disease_label.lower())

            if not disease_uri:
                print(f"    '{disease_label}' — no URI, skipped.")
                profiles.append({
                    "disease": disease_label,
                    "disease_uri": None,
                    "candidate_symptoms": [],
                    "status": "skipped_for_followup",
                })
                continue

            query = (
                "PREFIX owl:  <http://www.w3.org/2002/07/owl#>\n"
                "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>\n"
                "PREFIX ro:   <http://purl.obolibrary.org/obo/RO_>\n"
                "SELECT ?symptom ?symptomLabel\n"
                "WHERE {\n"
                f"  <{disease_uri}> rdfs:subClassOf ?restriction .\n"
                "  ?restriction owl:onProperty ro:0002452 .\n"
                "  ?restriction owl:someValuesFrom ?symptom .\n"
                "  ?symptom rdfs:label ?symptomLabel .\n"
                "}"
            )

            try:
                result = json.loads(graphdb._run(sparql_query=query))
                symptom_labels: list[str] = result.get("symptom_candidates", [])
                if not symptom_labels:
                    symptom_labels = [
                        c.get("symp_label", "")
                        for c in result.get("symp_candidates", [])
                        if c.get("symp_label")
                    ]
                status = "profiled" if symptom_labels else "no_symptoms_found"
                print(f"    '{disease_label}' — {len(symptom_labels)} symptom(s).")
            except Exception as exc:
                symptom_labels = []
                status = "tool_failure"
                print(f"    '{disease_label}' — ERROR: {exc}")

            profiles.append({
                "disease": disease_label,
                "disease_uri": disease_uri,
                "candidate_symptoms": symptom_labels,
                "status": status,
            })

        return profiles
