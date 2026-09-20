from __future__ import annotations

import json
import os
import re
from typing import Type
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from crewai.tools import BaseTool
from pydantic import BaseModel, Field


class GraphDbOntologyQueryToolInput(BaseModel):
    


    sparql_query: str = Field(
        ...,
        description=(
            "Complete read-only SPARQL query to execute against GraphDB. "
            "Include PREFIX declarations when needed and keep query type read-only "
            "(SELECT/ASK/CONSTRUCT/DESCRIBE)."
        ),
    )


class GraphDbOntologyQueryTool(BaseTool):
 

    name: str = "graphdb_ontology_query"
    description: str = (
        "Execute an agent-authored read-only SPARQL query against the medical "
        "ontology in GraphDB. Returns normalized rows plus pre-extracted "
        "candidate lists for symptoms and diseases."
        "\n\n"

        "=== HOW TO CALL ===\n"
        "Send exactly: {\"sparql_query\": \"<your full SPARQL query>\"}\n"
        "Allowed query types: SELECT, ASK, CONSTRUCT, DESCRIBE (read-only).\n"
        "\n"

        "=== ONTOLOGY NAMESPACES ===\n"
        "SYMP = symptom ontology.  Base URI: http://purl.obolibrary.org/obo/SYMP_\n"
        "DOID = disease ontology.  Base URI: http://purl.obolibrary.org/obo/DOID_\n"
        "RO   = relation ontology. Base URI: http://purl.obolibrary.org/obo/RO_\n"
        "Key relation: ro:0002452 means \"has symptom\".\n"
        "\n"

        "=== REQUIRED PREFIXES (include at the top of every query) ===\n"
        "PREFIX owl:  <http://www.w3.org/2002/07/owl#>\n"
        "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>\n"
        "PREFIX ro:   <http://purl.obolibrary.org/obo/RO_>\n"
        "\n"

        "=== QUERY PATTERNS ===\n"
        "Below are three ready-to-use query templates. Copy the template "
        "exactly, then replace only the placeholder (marked with {{ }}) "
        "with your actual value. Do NOT add, remove, or modify any other "
        "part of the WHERE clause.\n"
        "\n"

        # Pattern 1
        "--- PATTERN 1: Find symptom terms by label text (case-insensitive) ---\n"
        "Use case: You have a symptom name string and need its SYMP URI (SYMP-only scope).\n"
        "Placeholder: {{LOWERCASED_SEARCH_TERM}} = your symptom text, all lowercase.\n"
        "\n"
        "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>\n"
        "SELECT ?symptom ?symptomLabel\n"
        "WHERE {\n"
        "  ?symptom rdfs:label ?symptomLabel .\n"
        "  FILTER(STRSTARTS(STR(?symptom), \"http://purl.obolibrary.org/obo/SYMP_\"))\n"
        "  FILTER(CONTAINS(LCASE(STR(?symptomLabel)), \"{{LOWERCASED_SEARCH_TERM}}\"))\n"
        "}\n"
        "\n"

        # Pattern 2
        "--- PATTERN 2: Get all symptoms linked to a known disease ---\n"
        "Use case: You have a disease URI and need its symptom profile.\n"
        "Placeholder: {{DISEASE_URI}} = full disease URI in angle brackets.\n"
        "\n"
        "PREFIX owl:  <http://www.w3.org/2002/07/owl#>\n"
        "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>\n"
        "PREFIX ro:   <http://purl.obolibrary.org/obo/RO_>\n"
        "SELECT ?symptom ?symptomLabel\n"
        "WHERE {\n"
        "  {{DISEASE_URI}} rdfs:subClassOf ?restriction .\n"
        "  ?restriction owl:onProperty ro:0002452 .\n"
        "  ?restriction owl:someValuesFrom ?symptom .\n"
        "  ?symptom rdfs:label ?symptomLabel .\n"
        "}\n"
        "\n"

        # Pattern 3
        "--- PATTERN 3: Find all diseases linked to a known symptom ---\n"
        "Use case: You have a symptom URI (SYMP) and need all diseases (DOID) that list it. replace ONLY SYMPTOM_URI\n"
        "Placeholder: {{SYMPTOM_URI}} = full symptom URI in angle brackets.\n"
        "\n"
        "PREFIX owl:  <http://www.w3.org/2002/07/owl#>\n"
        "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>\n"
        "PREFIX ro:   <http://purl.obolibrary.org/obo/RO_>\n"
        "SELECT ?disease ?diseaseLabel\n"
        "WHERE {\n"
        "  ?disease rdfs:subClassOf ?restriction .\n"
        "  ?restriction owl:onProperty ro:0002452 .\n"
        "  ?restriction owl:someValuesFrom {{SYMPTOM_URI}} .\n"
        "  ?disease rdfs:label ?diseaseLabel .\n"
        "  FILTER(LANG(?diseaseLabel) = \"en\")\n"
        "}\n"
        "\n"

     
        "The tool response is a JSON object containing:\n"
        "- rows: all result rows with raw key-value pairs.\n"
        "- symp_candidates: extracted symptom URIs, labels, and parsed_symp_number.\n"
        "- disease_candidates: list of disease label strings.\n"
        "- disease_rows: list of {disease_uri, disease_label} objects.\n"
        "- symptom_candidates: list of symptom label strings.\n"
        "- symptom_rows: list of {symptom_uri, symptom_label} objects.\n"
        "Use these pre-extracted fields instead of manually parsing rows.\n"
        "\n"

        "=== RULES ===\n"
        "1. Copy patterns exactly. Do NOT add FILTER(LANG(...)) (except for pattern 3) or any filter "
        "not shown in the pattern.\n"
        "2. Do NOT modify the WHERE clause structure (no extra triples, no "
        "reordering, no optional blocks).\n"
        "3. Patterns 2 has NO language filters; results are returned as-is.\n"
        "4. Pattern 3 must remain symptom-to-disease only. Do NOT mix Pattern 2 triples into Pattern 3.\n"
        "5. Replace only the {{ }} placeholder with your actual value. "
        "URIs go inside angle brackets: <http://...>.\n"
        "6. Use data from tool responses ONLY. Never invent URIs, labels, or "
        "relation IDs from memory or examples.\n"
        "7. All URIs you use must come from prior tool results or from "
        "disease_entries provided by earlier pipeline stages.\n"
    )
    args_schema: Type[BaseModel] = GraphDbOntologyQueryToolInput

    def __init__(self, timeout_seconds: int = 30, **kwargs) -> None:
        super().__init__(**kwargs)
        self._timeout_seconds = timeout_seconds
        self._repository_url = os.environ.get(
            "GRAPHDB_REPOSITORY_URL",
            "http://localhost:7200/repositories/DoidOntologyRepository",
        )

    def _run(self, sparql_query: str) -> str:
        clean_sparql_query = sparql_query.strip()
        if not clean_sparql_query:
            return "sparql_query is empty"

        upper_query = clean_sparql_query.lstrip().upper()
        if not (
            upper_query.startswith("SELECT")
            or upper_query.startswith("PREFIX")
            or upper_query.startswith("ASK")
            or upper_query.startswith("CONSTRUCT")
            or upper_query.startswith("DESCRIBE")
        ):
            return (
                "Only read-only SPARQL queries are allowed. "
                "Expected SELECT/ASK/CONSTRUCT/DESCRIBE (optionally with PREFIX lines)."
            )

        payload = urlencode({"query": clean_sparql_query}).encode("utf-8")
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
                status_code = getattr(response, "status", 200)
                raw = response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            return (
                f"GraphDB request failed with HTTP {exc.code}. "
                f"Endpoint Response: {body}"
            )
        except URLError as exc:
            return (
                "Could not reach GraphDB endpoint. "
                f"Endpoint Error: {exc.reason}"
            )
        except TimeoutError:
            return f"GraphDB request timed out after {self._timeout_seconds}s."
        except Exception as exc:  
            return f"Unexpected error while querying GraphDB: {exc}"

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return (
                f"Query executed (HTTP {status_code}) but response is not JSON. "
                f"Raw response: {raw}"
            )

        bindings = parsed.get("results", {}).get("bindings", [])

        normalized_rows: list[dict[str, str]] = []
        for row in bindings:
            normalized_row: dict[str, str] = {}
            for key, val in row.items():
                if isinstance(val, dict) and "value" in val:
                    normalized_row[key] = str(val.get("value", ""))
            if normalized_row:
                normalized_rows.append(normalized_row)

        symp_candidates: list[dict[str, str | None]] = []
        seen_symp: set[tuple[str, str]] = set()

        disease_rows: list[dict[str, str]] = []
        seen_disease: set[tuple[str, str]] = set()

        symptom_rows: list[dict[str, str]] = []
        seen_symptom: set[tuple[str, str]] = set()

        for row in normalized_rows:
            symp_uri = row.get("symptom") or row.get("symp") or row.get("term") or ""
            symp_label = row.get("symptomLabel") or row.get("sympLabel") or row.get("termLabel") or row.get("label") or ""
            if symp_uri or symp_label:
                key = (symp_uri, symp_label)
                if key not in seen_symp:
                    seen_symp.add(key)
                    parsed_symp_number = None
                    match = re.search(r"SYMP_(\d+)$", symp_uri)
                    if match:
                        parsed_symp_number = match.group(1)
                    symp_candidates.append(
                        {
                            "symp_uri": symp_uri or None,
                            "symp_label": symp_label or None,
                            "parsed_symp_number": parsed_symp_number,
                        }
                    )

            disease_uri = row.get("disease_uri") or row.get("disease_iri") or row.get("disease") or ""
            disease_label = row.get("disease_label") or row.get("diseaseLabel") or ""
            if disease_uri or disease_label:
                key = (disease_uri, disease_label)
                if key not in seen_disease:
                    seen_disease.add(key)
                    disease_rows.append(
                        {
                            "disease_uri": disease_uri,
                            "disease_label": disease_label,
                        }
                    )

            symptom_uri = row.get("symptom_uri") or row.get("symptom") or ""
            symptom_label = row.get("symptom_label") or row.get("symptomLabel") or ""
            if symptom_uri or symptom_label:
                key = (symptom_uri, symptom_label)
                if key not in seen_symptom:
                    seen_symptom.add(key)
                    symptom_rows.append(
                        {
                            "symptom_uri": symptom_uri,
                            "symptom_label": symptom_label,
                        }
                    )

        disease_candidates = [
            r["disease_label"] for r in disease_rows if r.get("disease_label")
        ]
        symptom_candidates = [
            r["symptom_label"] for r in symptom_rows if r.get("symptom_label")
        ]

        result = {
            "status_code": status_code,
            "executed_query": clean_sparql_query,
            "result_row_count": len(bindings),
            "rows": normalized_rows,
            "symp_candidates": symp_candidates,
            "disease_count": len(disease_candidates),
            "disease_candidates": disease_candidates,
            "disease_rows": disease_rows,
            "symptom_count": len(symptom_candidates),
            "symptom_candidates": symptom_candidates,
            "symptom_rows": symptom_rows,
        }
        return json.dumps(result, ensure_ascii=True)