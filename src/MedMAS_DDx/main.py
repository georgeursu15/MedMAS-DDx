#!/usr/bin/env python
import re
import sys
import warnings
import json
import asyncio
import io
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from MedMAS_DDx.crew import MedMAS_DDx
from MedMAS_DDx.tools.graphdb_ontology_query_tool import GraphDbOntologyQueryTool
from MedMAS_DDx.tools.hp_ontology_query_tool import HpOntologyQueryTool
from MedMAS_DDx.tools.batch_disease_query_tool import BatchDiseaseQueryTool
from MedMAS_DDx.tools.refine_scoring_tool import RefineScoring
from MedMAS_DDx.tools.diagnosis_ranking_tool import rank_diagnoses
from MedMAS_DDx.followup_sanitizer import (
    build_patient_symptom_set,
    canonicalize_baseline_symptoms,
    sanitize_followup_payload,
)
from MedMAS_DDx.pipeline.utils import PipelineLogger, PayloadExtractor
from MedMAS_DDx.pipeline.symptom_mapper import SymptomMapperFlow
from MedMAS_DDx.pipeline.disease_mapper import DiseaseMapperFlow
from MedMAS_DDx.pipeline.disease_profiler import DiseaseProfilerFlow
from MedMAS_DDx.answer_collector import (
    AnswerCollector,
    TerminalAnswerCollector,
    normalize_answer,
)

warnings.filterwarnings("ignore", category=SyntaxWarning, module="pysbd")



def _run_disease_crew(
    user_message: str,
) -> tuple[Any, dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    result = MedMAS_DDx().disease_crew().kickoff(
        inputs={"user_message": user_message}
    )
    extracted_symptoms = PayloadExtractor.extract_extracted_symptoms(result)

    if not extracted_symptoms:
        return result, {}, [], []

    print(f"\n[Phase 1a-python] Mapping {len(extracted_symptoms)} symptom(s) to SYMP ...")
    symp_mappings = SymptomMapperFlow.python_symp_mapping(extracted_symptoms)
    mapped = sum(1 for m in symp_mappings if m.get("status") == "mapped")
    print(f"[Phase 1a-python] {mapped}/{len(symp_mappings)} mapped.")

    print("\n[Phase 1a-python] Querying diseases ...")
    disease_payload = DiseaseMapperFlow.python_disease_mapping(symp_mappings)

    return result, disease_payload, symp_mappings, extracted_symptoms



def _run_deterministic_ranking(disease_payload: dict[str, Any]) -> dict[str, Any]:
    """Deterministic TF-IDF ranking — no LLM."""
    return rank_diagnoses(disease_payload)



def _slim_ranking_for_followup(
    ranking_payload: dict[str, Any], max_diseases: int = 25
) -> dict[str, Any]:
    slim = {k: v for k, v in ranking_payload.items() if k != "scoring_metadata"}
    diff = slim.get("differential_diagnosis", [])
    slim["differential_diagnosis"] = diff[:max_diseases]
    return slim


def _run_followup_crew(
    disease_payload: dict[str, Any],
    ranking_payload: dict[str, Any],
    symp_mappings: list[dict[str, Any]],
) -> dict[str, Any]:
    print("\n[Phase 1c-python] Fetching disease symptom profiles ...")
    disease_profiles = DiseaseProfilerFlow.python_fetch_disease_profiles(disease_payload, ranking_payload)
    profiled = sum(1 for p in disease_profiles if p.get("status") == "profiled")
    print(f"[Phase 1c-python] {profiled}/{len(disease_profiles)} profiled.")

    slim_ranking = _slim_ranking_for_followup(ranking_payload)
    ranking_json = json.dumps(slim_ranking, indent=2, ensure_ascii=True)
    mapped_symptom_labels = [
        m["matched_symp_label"]
        for m in (symp_mappings or [])
        if isinstance(m, dict) and m.get("status") == "mapped" and m.get("matched_symp_label")
    ]
    mapped_symptom_labels_json = json.dumps(mapped_symptom_labels, indent=2, ensure_ascii=True)
    disease_profiles_json = json.dumps(disease_profiles, indent=2, ensure_ascii=True)

    medmas_ddx = MedMAS_DDx()
    result = medmas_ddx.followup_crew(
        ranking_json,
        mapped_symptom_labels_json,
        disease_profiles_json,
    ).kickoff()

    return PayloadExtractor.extract_followup_payload(result)



def _run_refine_phase(
    ranking_payload: dict[str, Any],
    followup_payload: dict[str, Any],
) -> dict[str, Any]:
    ranking_json = json.dumps(ranking_payload, indent=2, ensure_ascii=True)
    followup_json = json.dumps(followup_payload, indent=2, ensure_ascii=True)

    print("\n[Phase 2-python] Computing refinement scores ...")
    scorer = RefineScoring()
    scores_str = scorer._run(
        initial_ranking_json=ranking_json,
        followup_payload_json=followup_json,
    )

    try:
        scores = json.loads(scores_str)
    except Exception:
        scores = {}

    if scores.get("tool_failure") or not scores.get("refined_differential_diagnosis"):
        return {
            "refined_differential_diagnosis": [],
            "followup_summary": "Python scorer failed.",
            "notes": scores_str[:500],
        }

    top_n = len(scores.get("refined_differential_diagnosis", []))
    print(f"[Phase 2-python] Scores ready for {top_n} disease(s).")

    medmas_ddx = MedMAS_DDx()
    result = medmas_ddx.refine_crew(ranking_json, followup_json, scores_str).kickoff()

    raw = getattr(result, "raw", str(result))
    parsed = PayloadExtractor.parse_json(raw)
    if parsed and "refined_differential_diagnosis" in parsed:
        return parsed

    print("[Phase 2] LLM narrative failed; returning Python-computed scores.")
    return {
        "refined_differential_diagnosis": scores.get("refined_differential_diagnosis", []),
        "discriminative_weights": scores.get("discriminative_weights", {}),
        "followup_summary": (
            "Narrative generation failed; scores computed by Python. "
            f"LLM output (first 300 chars): {raw[:300]}"
        ),
        "notes": "Python scorer succeeded; LLM narrative was not valid JSON.",
    }



async def run_diagnostic_pipeline(
    user_message: str,
    collector: AnswerCollector,
    log_file: str | None = None,
) -> dict[str, Any]:

    MedMAS_DDx.runtime_log_file = log_file

    print("\n[Phase 1a] Extracting symptoms ...")
    (
        disease_crew_result,
        disease_payload,
        symp_mappings,
        extracted_symptoms,
    ) = _run_disease_crew(user_message)

    if not disease_payload or not disease_payload.get("disease_results"):
        raw = getattr(disease_crew_result, "raw", str(disease_crew_result))
        return {"error": "No disease_results found in Phase 1a output.", "raw_output": raw}

    await collector.on_extracted_symptoms(extracted_symptoms)

    print("\n[Phase 1b] Running deterministic diagnosis ranking ...")
    ranking_payload = _run_deterministic_ranking(disease_payload)
    print(
        f"[Phase 1b] Ranked {len(ranking_payload.get('differential_diagnosis', []))} diseases "
        f"from {ranking_payload.get('total_diseases_found', 0)} candidates."
    )

    if symp_mappings:
        ranking_payload = canonicalize_baseline_symptoms(ranking_payload, symp_mappings)

    await collector.on_initial_ranking(ranking_payload)

    total_diseases = ranking_payload.get("total_diseases_found", 0)
    if total_diseases <= 1:
        return ranking_payload

    print("\n[Phase 1c] Generating follow-up questions ...")
    followup_payload = _run_followup_crew(disease_payload, ranking_payload, symp_mappings)

    if not followup_payload:
        return ranking_payload

    patient_symptom_set = build_patient_symptom_set(
        disease_results=disease_payload.get("disease_results"),
        symp_mappings=symp_mappings,
        extracted_symptoms=extracted_symptoms,
    )
    followup_payload = sanitize_followup_payload(
        followup_payload, patient_symptom_set, max_questions=11
    )
    report = followup_payload.get("sanitization_report", {})
    if report:
        drops = (
            len(report.get("dropped_already_known", []))
            + len(report.get("dropped_useless_zero_weight", []))
            + len(report.get("dropped_duplicate", []))
            + len(report.get("dropped_over_cap", []))
        )
        inv_drops = len(report.get("dropped_investigations", []))
        if drops or inv_drops:
            print(
                f"[Phase 1c-post] Sanitizer dropped "
                f"{drops} question(s) and {inv_drops} invalid investigation(s)."
            )

    questions = followup_payload.get("questions_asked") or []
    if not questions:
        print("\n[Phase 2 skipped] No follow-up questions — returning Phase 1 ranking.\n")
        return ranking_payload

    followup_with_answers = await collector.collect(followup_payload)

    print("\n[Phase 2] Refining diagnosis ...")
    return _run_refine_phase(ranking_payload, followup_with_answers)



def run():

    user_message = input("Describe your symptoms: ").strip()
    if not user_message:
        user_message = "I have bone pain."

    log_file = PipelineLogger.build_runtime_log_file()
    print(f"[Logging] Full terminal output will be saved to: {log_file}")

    collector = TerminalAnswerCollector()

    with PipelineLogger.tee_terminal_to_log(log_file):
        result = asyncio.run(
            run_diagnostic_pipeline(user_message, collector, log_file)
        )
        print("\n── Diagnosis result ──────────────────────────────────────────")
        print(json.dumps(result, indent=2, ensure_ascii=True))


def train():
    inputs = {
        "user_message": (
            "I have persistent dry cough for two weeks, chest tightness, "
            "and shortness of breath on exertion."
        )
    }
    MedMAS_DDx.runtime_log_file = PipelineLogger.build_runtime_log_file()
    MedMAS_DDx().crew().train(
        n_iterations=int(sys.argv[1]),
        filename=sys.argv[2],
        inputs=inputs,
    )


def replay():
    MedMAS_DDx.runtime_log_file = PipelineLogger.build_runtime_log_file()
    MedMAS_DDx().crew().replay(task_id=sys.argv[1])


def test():
    inputs = {
        "user_message": (
            "I feel extreme fatigue, muscle weakness, and dizziness since yesterday."
        )
    }
    MedMAS_DDx.runtime_log_file = PipelineLogger.build_runtime_log_file()
    MedMAS_DDx().crew().test(
        n_iterations=int(sys.argv[1]),
        eval_llm=sys.argv[2],
        inputs=inputs,
    )


def run_with_trigger():
    if len(sys.argv) < 2:
        raise Exception("No trigger payload provided.")
    trigger_payload = json.loads(sys.argv[1])
    inputs = {
        "crewai_trigger_payload": trigger_payload,
        "user_message": trigger_payload.get("user_message", ""),
    }
    MedMAS_DDx.runtime_log_file = PipelineLogger.build_runtime_log_file()
    return MedMAS_DDx().disease_crew().kickoff(inputs=inputs)
