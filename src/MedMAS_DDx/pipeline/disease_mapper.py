import json
from typing import Any
from MedMAS_DDx.tools.batch_disease_query_tool import BatchDiseaseQueryTool


class DiseaseMapperFlow:
    @staticmethod
    def python_disease_mapping(symp_mappings: list[dict[str, Any]]) -> dict[str, Any]:
       
        tool = BatchDiseaseQueryTool()
        mapped_uris = [
            m["matched_symp_uri"]
            for m in symp_mappings
            if m.get("status") == "mapped" and m.get("matched_symp_uri")
        ]

        if not mapped_uris:
            return {"disease_results": [
                {
                    "symptom": m.get("symptom", ""),
                    "symp_uri": None,
                    "parsed_symp_number": None,
                    "disease_candidates": [],
                    "disease_entries": [],
                    "status": "skipped",
                    "notes": "unmapped",
                }
                for m in symp_mappings
            ]}

        print(f"  [disease-map] Querying {len(mapped_uris)} SYMP URI(s) ... ", end="", flush=True)
        raw = tool._run(symptom_uris=mapped_uris)
        try:
            batch_result = json.loads(raw)
        except Exception:
            batch_result = {"results": {}}
        print(f"{batch_result.get('total_rows_returned', '?')} rows returned.")

        results_by_uri: dict[str, dict] = batch_result.get("results", {})
        disease_results = []
        for m in symp_mappings:
            uri = m.get("matched_symp_uri")
            if m.get("status") != "mapped" or not uri:
                disease_results.append({
                    "symptom": m.get("symptom", ""),
                    "symp_uri": None,
                    "parsed_symp_number": None,
                    "disease_candidates": [],
                    "disease_entries": [],
                    "status": "skipped",
                    "notes": "unmapped",
                })
                continue

            data = results_by_uri.get(uri, {})
            candidates = data.get("disease_candidates", [])
            entries = data.get("disease_entries", [])
            disease_results.append({
                "symptom": m.get("symptom", ""),
                "symp_uri": uri,
                "parsed_symp_number": m.get("parsed_symp_number"),
                "disease_candidates": candidates,
                "disease_entries": entries,
                "status": "success" if candidates else "no_diseases",
                "notes": "",
            })

        return {"disease_results": disease_results}
