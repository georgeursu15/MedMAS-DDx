from __future__ import annotations

import json
import os
from typing import Type
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from crewai.tools import BaseTool
from pydantic import BaseModel, Field


class BatchDiseaseQueryToolInput(BaseModel):


    symptom_uris: list[str] = Field(
        ...,
        description=(
            "List of full SYMP URIs to query diseases for, e.g. "
            '["http://purl.obolibrary.org/obo/SYMP_0000064", '
            '"http://purl.obolibrary.org/obo/SYMP_0019177"]'
        ),
    )


class BatchDiseaseQueryTool(BaseTool):
  

    name: str = "batch_disease_query"
    description: str = (
        "Query diseases for multiple symptoms in ONE call. "
        "Send: {\"symptom_uris\": [\"http://purl.obolibrary.org/obo/SYMP_0000064\", ...]} "
        "Returns a JSON object mapping each symptom URI to its disease candidates. "
        "Use this INSTEAD of calling graphdb_ontology_query once per symptom. "
        "Only include URIs with status 'mapped' — skip unmapped symptoms."
    )
    args_schema: Type[BaseModel] = BatchDiseaseQueryToolInput

    def __init__(self, timeout_seconds: int = 60, **kwargs) -> None:
        super().__init__(**kwargs)
        self._timeout_seconds = timeout_seconds
        self._repository_url = os.environ.get(
            "GRAPHDB_REPOSITORY_URL",
            "http://localhost:7200/repositories/DoidOntologyRepository",
        )

    def _run(self, symptom_uris: list[str]) -> str:
        if not symptom_uris:
            return json.dumps({"error": "No symptom URIs provided", "results": {}})

        valid_uris = [
            uri.strip() for uri in symptom_uris
            if uri.strip().startswith("http://purl.obolibrary.org/obo/SYMP_")
        ]
        if not valid_uris:
            return json.dumps({"error": "No valid SYMP URIs found", "results": {}})

        values_entries = " ".join(f"<{uri}>" for uri in valid_uris)
        sparql_query = f"""
PREFIX owl:  <http://www.w3.org/2002/07/owl#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX ro:   <http://purl.obolibrary.org/obo/RO_>
SELECT ?symptomUri ?disease ?diseaseLabel
WHERE {{
  VALUES ?symptomUri {{ {values_entries} }}
  ?disease rdfs:subClassOf ?restriction .
  ?restriction owl:onProperty ro:0002452 .
  ?restriction owl:someValuesFrom ?symptomUri .
  ?disease rdfs:label ?diseaseLabel .
  FILTER(LANG(?diseaseLabel) = "en")
}}
"""

        payload = urlencode({"query": sparql_query.strip()}).encode("utf-8")
        headers = {
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Accept": "application/sparql-results+json, application/json;q=0.9, */*;q=0.8",
        }
        request = Request(
            self._repository_url,
            data=payload,
            headers=headers,
            method="POST",
        )

        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            return json.dumps({
                "error": f"GraphDB HTTP {exc.code}: {body}",
                "results": {},
            })
        except URLError as exc:
            return json.dumps({
                "error": f"Cannot reach GraphDB: {exc.reason}",
                "results": {},
            })
        except TimeoutError:
            return json.dumps({
                "error": f"Query timed out after {self._timeout_seconds}s",
                "results": {},
            })

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return json.dumps({
                "error": "Response is not valid JSON",
                "results": {},
            })

        bindings = parsed.get("results", {}).get("bindings", [])

        results: dict[str, dict] = {uri: {"disease_candidates": [], "disease_entries": []} for uri in valid_uris}
        seen: dict[str, set] = {uri: set() for uri in valid_uris}

        for row in bindings:
            symp_uri = row.get("symptomUri", {}).get("value", "")
            disease_uri = row.get("disease", {}).get("value", "")
            disease_label = row.get("diseaseLabel", {}).get("value", "")

            if symp_uri in results and disease_label and disease_label not in seen[symp_uri]:
                seen[symp_uri].add(disease_label)
                results[symp_uri]["disease_candidates"].append(disease_label)
                results[symp_uri]["disease_entries"].append({
                    "disease_uri": disease_uri,
                    "disease_label": disease_label,
                })

        return json.dumps({
            "total_symptom_uris_queried": len(valid_uris),
            "total_rows_returned": len(bindings),
            "results": results,
        }, ensure_ascii=True)