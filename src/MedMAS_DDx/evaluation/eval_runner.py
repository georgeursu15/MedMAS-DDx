#!/usr/bin/env python


from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import os
import re
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from MedMAS_DDx.crew import MedMAS_DDx
from MedMAS_DDx.tools.graphdb_ontology_query_tool import GraphDbOntologyQueryTool
from MedMAS_DDx.tools.diagnosis_ranking_tool import rank_diagnoses
from MedMAS_DDx.tools.hp_ontology_query_tool import HpOntologyQueryTool
from MedMAS_DDx.followup_sanitizer import (
    build_patient_symptom_set,
    canonicalize_baseline_symptoms,
    sanitize_followup_payload,
)
from MedMAS_DDx.main import (
    _run_disease_crew,
    _run_followup_crew,
    _run_refine_phase,
)
from MedMAS_DDx.pipeline.symptom_mapper import SymptomMapperFlow
from MedMAS_DDx.pipeline.utils import PipelineLogger
from MedMAS_DDx.answer_collector import (
    AutoAnswerCollector,
    PatientLLMAnswerCollector,
)
from MedMAS_DDx.tools.similarity_scorer import get_default_scorer, DEFAULT_THRESHOLD


VARIANT_COLUMN_MAP = {
    "100": "100% Case",
    "80": "80% Case",
    "50": "50% Case",
}

DEFAULT_CSV = Path(__file__).resolve().parents[3] / "Benchmark" / "PatientCases.csv"
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[3] / "eval_results"




class _SympResolver:


    def __init__(self) -> None:
        self._graphdb = GraphDbOntologyQueryTool()
        self._hp = HpOntologyQueryTool()
        self._cache: dict[str, str | None] = {}

    @staticmethod
    def _normalise(s: str) -> str:
        return re.sub(r"[_\s\-]+", " ", s.strip().lower()).strip()


    def is_synonym(self, term_a: str, term_b: str) -> bool:
        norm_a = self._normalise(term_a)
        norm_b = self._normalise(term_b)
        if not norm_a or not norm_b:
            return False
        if norm_a == norm_b:
            return True

        symp_a = self.resolve_to_symp(term_a)
        symp_b = self.resolve_to_symp(term_b)
        return bool(symp_a) and symp_a == symp_b

    def warm_cache(self, terms: list[str]) -> None:
        for t in terms:
            self.resolve_to_symp(t)

    def clear_cache(self) -> None:
        self._cache.clear()

    def resolve_to_symp(self, text: str) -> str | None:
        """Return the SYMP URI for `text`, or None if it can't be resolved."""
        norm = self._normalise(text)
        if not norm:
            return None
        if norm in self._cache:
            return self._cache[norm]

        
        self._cache[norm] = None

       
        try:
            entry = SymptomMapperFlow._symp_map_one(self._graphdb, self._hp, norm, [])
        except Exception:
            return None

        if entry.get("status") == "mapped":
            uri = (entry.get("matched_symp_uri") or "").strip()
            if uri:
                self._cache[norm] = uri
        return self._cache[norm]


_hp_resolver = _SympResolver()


def _symptom_matches(
    question_symptom: str,
    known_symptom: str,
    *,
    use_hp_fallback: bool = True,
) -> bool:
   
    def _norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]", "", s.strip().lower())

    if _norm(question_symptom) == _norm(known_symptom):
        return True

    if use_hp_fallback:
        try:
            if _hp_resolver.is_synonym(question_symptom, known_symptom):
                return True
        except Exception:
           
            pass

    return False



def _evaluate_single_case(
    case_id: int,
    user_message: str,
    expected_prognosis: str,
    known_symptoms: list[str],
    variant: str,
    log_dir: Path | None = None,
    *,
    full_case_text: str | None = None,
    use_patient_llm: bool = False,
    patient_llm_model: str | None = None,
) -> dict[str, Any]:
   
    _hp_resolver.clear_cache()

    result: dict[str, Any] = {
        "case_id": case_id,
        "variant": variant,
        "expected_prognosis": expected_prognosis,
        "known_symptoms": known_symptoms,
        "phase1_top1": None,
        "phase1_top3": [],
        "phase1_ranking": [],
        "phase2_top1": None,
        "phase2_top3": [],
        "phase2_ranking": [],
        "followup_questions_count": 0,
        "followup_auto_answers": {},
        "top1_correct_phase1": False,
        "top3_correct_phase1": False,
        "top1_correct_phase2": False,
        "top3_correct_phase2": False,
        "top1_tied_correct_phase1": False,
        "top1_tied_correct_phase2": False,
     
        "sim_score_phase1_top1": 0.0,
        "sim_score_phase2_top1": 0.0,
        "sim_score_phase1_top3": 0.0,
        "sim_score_phase2_top3": 0.0,
        "sim_correct_phase1": False,
        "sim_correct_phase2": False,
        "sim_top3_correct_phase1": False,
        "sim_top3_correct_phase2": False,
        "status": "ok",
        "error": None,
        "duration_seconds": 0.0,
    }

    start = time.time()

    try:
        if log_dir:
            log_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            MedMAS_DDx.runtime_log_file = str(
                log_dir / f"eval_case{case_id}_{variant}pct_{ts}.txt"
            )
        else:
            MedMAS_DDx.runtime_log_file = PipelineLogger.build_runtime_log_file()

        _, disease_payload, symp_mappings, extracted_symptoms = _run_disease_crew(user_message)

        if not extracted_symptoms:
            result["error"] = "No extracted symptoms in Phase 1a output"
            result["duration_seconds"] = time.time() - start
            return result

        if not disease_payload or not disease_payload.get("disease_results"):
            result["error"] = "No disease_results in Phase 1a output"
            result["duration_seconds"] = time.time() - start
            return result

        ranking_payload = rank_diagnoses(disease_payload)
        if symp_mappings:
            ranking_payload = canonicalize_baseline_symptoms(ranking_payload, symp_mappings)

        diff_diag = ranking_payload.get("differential_diagnosis", [])
        sorted_diag = sorted(diff_diag, key=lambda d: d.get("rank", 999))
        phase1_diseases = [d.get("disease", "") for d in sorted_diag]

       
        p1_top1_diseases = phase1_diseases[:1]
        p1_top3_diseases = phase1_diseases[:3]

        result["phase1_ranking"] = sorted_diag
        result["phase1_top1"] = phase1_diseases[0] if phase1_diseases else None
        result["phase1_top3"] = p1_top3_diseases
        result["top1_correct_phase1"] = any(
            _disease_match(d, expected_prognosis) for d in p1_top1_diseases
        )
        result["top3_correct_phase1"] = any(
            _disease_match(d, expected_prognosis) for d in p1_top3_diseases
        )
        result["top1_tied_correct_phase1"] = result["top1_correct_phase1"]

        try:
            _scorer = get_default_scorer()
            result["sim_score_phase1_top1"] = max(
                _scorer.score_ranking(p1_top1_diseases, expected_prognosis),
                default=0.0,
            ) if p1_top1_diseases else 0.0
            result["sim_score_phase1_top3"] = max(
                _scorer.score_ranking(p1_top3_diseases, expected_prognosis),
                default=0.0,
            ) if p1_top3_diseases else 0.0
            result["sim_correct_phase1"] = (
                result["sim_score_phase1_top1"] >= DEFAULT_THRESHOLD
            )
            result["sim_top3_correct_phase1"] = (
                result["sim_score_phase1_top3"] >= DEFAULT_THRESHOLD
            )
        except Exception as _sim_exc:
            logging.warning("Similarity scoring failed for Phase 1: %s", _sim_exc)

        total_diseases = ranking_payload.get("total_diseases_found", 0)
        if total_diseases <= 1:
            result["phase2_top1"] = result["phase1_top1"]
            result["phase2_top3"] = result["phase1_top3"]
            result["phase2_ranking"] = result["phase1_ranking"]
            result["top1_correct_phase2"] = result["top1_correct_phase1"]
            result["top3_correct_phase2"] = result["top3_correct_phase1"]
            result["top1_tied_correct_phase2"] = result["top1_tied_correct_phase1"]
            result["sim_score_phase2_top1"] = result["sim_score_phase1_top1"]
            result["sim_score_phase2_top3"] = result["sim_score_phase1_top3"]
            result["sim_correct_phase2"] = result["sim_correct_phase1"]
            result["sim_top3_correct_phase2"] = result["sim_top3_correct_phase1"]
            result["duration_seconds"] = time.time() - start
            return result

        followup_payload = _run_followup_crew(disease_payload, ranking_payload, symp_mappings)

        if not followup_payload:
            result["phase2_top1"] = result["phase1_top1"]
            result["phase2_top3"] = result["phase1_top3"]
            result["phase2_ranking"] = result["phase1_ranking"]
            result["top1_correct_phase2"] = result["top1_correct_phase1"]
            result["top3_correct_phase2"] = result["top3_correct_phase1"]
            result["top1_tied_correct_phase2"] = result["top1_tied_correct_phase1"]
            result["sim_score_phase2_top1"] = result["sim_score_phase1_top1"]
            result["sim_score_phase2_top3"] = result["sim_score_phase1_top3"]
            result["sim_correct_phase2"] = result["sim_correct_phase1"]
            result["sim_top3_correct_phase2"] = result["sim_top3_correct_phase1"]
            result["duration_seconds"] = time.time() - start
            return result

        patient_symptom_set = build_patient_symptom_set(
            disease_results=disease_payload.get("disease_results"),
            symp_mappings=symp_mappings,
            extracted_symptoms=extracted_symptoms,
        )
        followup_payload = sanitize_followup_payload(
            followup_payload, patient_symptom_set, max_questions=11
        )

     
        if use_patient_llm:
     
            _hm = MedMAS_DDx()
            if patient_llm_model:
                os.environ["PATIENT_LLM"] = patient_llm_model
            patient_llm_obj = _hm.patient_llm()
            patient_persona = _hm.patient_persona()
            
            collector = PatientLLMAnswerCollector(
                case_text=full_case_text or user_message,
                known_symptoms=known_symptoms,
                llm=patient_llm_obj,
                persona=patient_persona,
            )
            result["patient_llm_used"] = True
            result["patient_llm_model"] = collector._model_name 
        else:
            try:
                _hp_resolver.warm_cache(known_symptoms)
            except Exception:
                pass 

            def _answer_fn(symptom: str) -> str:
                matched = any(_symptom_matches(symptom, ks) for ks in known_symptoms)
                return "yes" if matched else "no"

            collector = AutoAnswerCollector(answer_fn=_answer_fn)
            result["patient_llm_used"] = False

        followup_with_answers = asyncio.run(collector.collect(followup_payload))

        questions = followup_with_answers.get("questions_asked", [])
        result["followup_questions_count"] = len(questions)
        result["followup_auto_answers"] = {
            q.get("symptom", f"q{i}"): q.get("patient_answer", "")
            for i, q in enumerate(questions)
            if isinstance(q, dict)
        }
        if use_patient_llm and isinstance(collector, PatientLLMAnswerCollector):
            result["patient_llm_calls"]        = collector.llm_calls
            result["patient_llm_batched_ok"]   = collector.batched_ok
            result["patient_llm_batched_error"] = collector.batched_error

        if not questions:
            result["phase2_top1"] = result["phase1_top1"]
            result["phase2_top3"] = result["phase1_top3"]
            result["phase2_ranking"] = result["phase1_ranking"]
            result["top1_correct_phase2"] = result["top1_correct_phase1"]
            result["top3_correct_phase2"] = result["top3_correct_phase1"]
            result["top1_tied_correct_phase2"] = result["top1_tied_correct_phase1"]
            result["sim_score_phase2_top1"] = result["sim_score_phase1_top1"]
            result["sim_score_phase2_top3"] = result["sim_score_phase1_top3"]
            result["sim_correct_phase2"] = result["sim_correct_phase1"]
            result["sim_top3_correct_phase2"] = result["sim_top3_correct_phase1"]
            result["duration_seconds"] = time.time() - start
            return result

        refined = _run_refine_phase(ranking_payload, followup_with_answers)

        refined_diag = refined.get("refined_differential_diagnosis", [])
        sorted_refined = sorted(refined_diag, key=lambda d: d.get("rank", 999))
        phase2_diseases = [d.get("disease", "") for d in sorted_refined]

        p2_top1_diseases = phase2_diseases[:1]
        p2_top3_diseases = phase2_diseases[:3]

        result["phase2_ranking"] = sorted_refined
        result["phase2_top1"] = phase2_diseases[0] if phase2_diseases else None
        result["phase2_top3"] = p2_top3_diseases
        result["top1_correct_phase2"] = any(
            _disease_match(d, expected_prognosis) for d in p2_top1_diseases
        )
        result["top3_correct_phase2"] = any(
            _disease_match(d, expected_prognosis) for d in p2_top3_diseases
        )
        result["top1_tied_correct_phase2"] = result["top1_correct_phase2"]

        try:
            _scorer = get_default_scorer()
            result["sim_score_phase2_top1"] = max(
                _scorer.score_ranking(p2_top1_diseases, expected_prognosis),
                default=0.0,
            ) if p2_top1_diseases else 0.0
            result["sim_score_phase2_top3"] = max(
                _scorer.score_ranking(p2_top3_diseases, expected_prognosis),
                default=0.0,
            ) if p2_top3_diseases else 0.0
            result["sim_correct_phase2"] = (
                result["sim_score_phase2_top1"] >= DEFAULT_THRESHOLD
            )
            result["sim_top3_correct_phase2"] = (
                result["sim_score_phase2_top3"] >= DEFAULT_THRESHOLD
            )
        except Exception as _sim_exc:
            logging.warning("Similarity scoring failed for Phase 2: %s", _sim_exc)

    except Exception as exc:
        result["status"] = "error"
        result["error"] = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"

    result["duration_seconds"] = time.time() - start
    return result


def _disease_match(predicted: str | None, expected: str) -> bool:
    """
    Fuzzy case-insensitive match between predicted disease and expected prognosis.
    Handles minor whitespace/punctuation differences.
    """
    if not predicted:
        return False
    norm_pred = re.sub(r"[^a-z0-9]", "", predicted.strip().lower())
    norm_exp = re.sub(r"[^a-z0-9]", "", expected.strip().lower())
    if norm_pred == norm_exp:
        return True
    if norm_pred in norm_exp or norm_exp in norm_pred:
        return True
    return False



def load_test_cases(
    csv_path: str | Path,
    variant_filter: str | None = None,
    prognosis_filter: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    
    csv_path = Path(csv_path)
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV not found: {csv_path}")

    variants = (
        {variant_filter: VARIANT_COLUMN_MAP[variant_filter]}
        if variant_filter and variant_filter in VARIANT_COLUMN_MAP
        else VARIANT_COLUMN_MAP
    )
    full_col = VARIANT_COLUMN_MAP["100"]

    cases: list[dict[str, Any]] = []
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row_idx, row in enumerate(reader):
            prognosis = row.get("prognosis", "").strip()
            if not prognosis:
                continue

            if prognosis_filter:
                if prognosis_filter.lower() not in prognosis.lower():
                    continue

            raw_symptoms = row.get("symptoms", "")
            known_symptoms = [
                s.strip() for s in raw_symptoms.split(",") if s.strip()
            ]
          
            full_case_text = (row.get(full_col) or "").strip()

            for var_key, var_col in variants.items():
                user_message = (row.get(var_col) or "").strip()
                if not user_message:
                    continue
                cases.append({
                    "case_id": row_idx + 1,
                    "variant": var_key,
                    "expected_prognosis": prognosis,
                    "known_symptoms": known_symptoms,
                    "user_message": user_message,
                    "full_case_text": full_case_text or user_message,
                })

                if limit and len(cases) >= limit:
                    return cases

    return cases



def _load_retry_cases(
    retry_failed_path: str | Path,
    statuses: tuple[str, ...] = ("api_unavailable", "error"),
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
   
    path = Path(retry_failed_path)
    if not path.exists():
        raise FileNotFoundError(f"Retry source file not found: {path}")

    with open(path, "r", encoding="utf-8") as f:
        prev_report = json.load(f)

    retry_cases: list[dict[str, Any]] = []
    kept_results: list[dict[str, Any]] = []

    for res in prev_report.get("cases", []):
        if res.get("status", "ok") in statuses:
           
            retry_cases.append({
                "case_id": res["case_id"],
                "variant": res["variant"],
                "expected_prognosis": res["expected_prognosis"],
                "known_symptoms": res["known_symptoms"],
                "user_message": res.get("_user_message", ""),
            })
        else:
            kept_results.append(res)

    return retry_cases, kept_results


def _save_progress(
    output_path: Path,
    results: list[dict[str, Any]],
    meta: dict[str, Any],
) -> None:
    progress_path = output_path.with_suffix(".progress.json")
    summary = _build_summary(results)
    payload = {
        "meta": {**meta, "status": "in_progress"},
        "summary": summary,
        "cases": results,
    }
    try:
        with open(progress_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
    except Exception as exc:
        logging.warning("Could not write progress file: %s", exc)


def _make_batch_id() -> str:
    return f"batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


def run_evaluation(
    csv_path: str | Path = DEFAULT_CSV,
    variant_filter: str | None = None,
    prognosis_filter: str | None = None,
    limit: int | None = None,
    output_path: str | Path | None = None,
    log_dir: Path | None = None,
    retry_failed_path: str | Path | None = None,
    retry_statuses: tuple[str, ...] = ("api_unavailable", "error"),
    batch_id: str | None = None,
    use_patient_llm: bool = False,
    patient_llm_model: str | None = None,
) -> dict[str, Any]:
    
    if batch_id is None:
        batch_id = _make_batch_id()

    if output_path is None:
        batch_dir = DEFAULT_OUTPUT_DIR / batch_id
        batch_dir.mkdir(parents=True, exist_ok=True)
    
        if prognosis_filter:
            prog_slug = re.sub(r"[^a-z0-9]+", "_", prognosis_filter.strip().lower()).strip("_")
            prog_tag = f"_{prog_slug}" if prog_slug else ""
        else:
            prog_tag = ""
        var_tag = f"_{variant_filter}pct" if variant_filter else "_all"
        suffix = "_retry" if retry_failed_path else ""
        output_path = batch_dir / f"eval{prog_tag}{var_tag}{suffix}.json"
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    carried_over_results: list[dict[str, Any]] = []
    if retry_failed_path:
        retry_case_stubs, carried_over_results = _load_retry_cases(
            retry_failed_path, retry_statuses
        )
        if not retry_case_stubs:
            print("[Eval] No failed/unavailable cases found in the previous batch. Nothing to retry.")
            return {"cases": carried_over_results, "summary": _build_summary(carried_over_results), "error": "No cases to retry"}

        all_cases = load_test_cases(csv_path, variant_filter=None)
        case_msg_map: dict[tuple[int, str], dict[str, str]] = {
            (c["case_id"], c["variant"]): {
                "user_message":   c["user_message"],
                "full_case_text": c.get("full_case_text") or c["user_message"],
            }
            for c in all_cases
        }
        cases: list[dict[str, Any]] = []
        for stub in retry_case_stubs:
            entry = case_msg_map.get((stub["case_id"], stub["variant"]))
            if not entry or not entry["user_message"]:
                logging.warning(
                    "Could not find user_message for case %d variant %s — skipping.",
                    stub["case_id"], stub["variant"],
                )
                continue
            cases.append({
                **stub,
                "user_message":   entry["user_message"],
                "full_case_text": entry["full_case_text"],
            })

        retry_mode = True
    else:
        cases = load_test_cases(csv_path, variant_filter, prognosis_filter, limit)
        retry_mode = False

    total = len(cases)

    if total == 0:
        print("[Eval] No test cases matched the filters. Exiting.")
        return {"cases": [], "summary": {}, "error": "No matching cases"}

    print(f"\n{'='*70}")
    print(f"  MedMAS_DDx Batch Evaluation{'  [RETRY MODE]' if retry_mode else ''}")
    print(f"  Batch ID:    {batch_id}")
    print(f"  Output dir:  {output_path.parent}")
    print(f"  Cases to run: {total}")
    if carried_over_results:
        print(f"  Carried over (already passed): {len(carried_over_results)}")
    print(f"  Variant filter: {variant_filter or 'all (100%, 80%, 50%)'}")
    print(f"  Prognosis filter: {prognosis_filter or 'none'}")
    print(f"  Patient LLM: {'ON (' + (patient_llm_model or 'auto') + ')' if use_patient_llm else 'OFF (synonym match)'}")
    print(f"  Started: {datetime.now().isoformat()}")
    print(f"{'='*70}\n")

    results: list[dict[str, Any]] = list(carried_over_results)  
    errors = 0

    meta = {
        "batch_id": batch_id,
        "timestamp": datetime.now().isoformat(),
        "csv_path": str(csv_path),
        "variant_filter": variant_filter,
        "prognosis_filter": prognosis_filter,
        "total_cases": total + len(carried_over_results),
        "retry_mode": retry_mode,
        "retry_source": str(retry_failed_path) if retry_failed_path else None,
        "errors": 0,
        "patient_llm": use_patient_llm,
        "patient_llm_model": patient_llm_model if use_patient_llm else None,
    }

    def _mark(strict: bool, tied: bool) -> str:
        if strict:
            return "✓"
        if tied:
            return "≈"
        return "✗"

    for idx, case in enumerate(cases, start=1):
        case_label = (
            f"[{idx}/{total}] "
            f"Case #{case['case_id']} ({case['variant']}%) — "
            f"Expected: {case['expected_prognosis']}"
        )
        print(f"\n{case_label}")
        print(f"{'─'*60}")

        res = _evaluate_single_case(
            case_id=case["case_id"],
            user_message=case["user_message"],
            expected_prognosis=case["expected_prognosis"],
            known_symptoms=case["known_symptoms"],
            variant=case["variant"],
            log_dir=log_dir,
            full_case_text=case.get("full_case_text") or case["user_message"],
            use_patient_llm=use_patient_llm,
            patient_llm_model=patient_llm_model,
        )
        results.append(res)

        status = res.get("status", "ok")
        if status == "error":
            errors += 1
            print(f"  ✗ ERROR: {res['error'][:120]}")
        else:
            p1 = _mark(res["top1_correct_phase1"], res.get("top1_tied_correct_phase1", False))
            p2 = _mark(res["top1_correct_phase2"], res.get("top1_tied_correct_phase2", False))
            print(f"  Phase 1 top-1: {res['phase1_top1']} {p1}")
            print(f"  Phase 2 top-1: {res['phase2_top1']} {p2}")
            print(f"  Follow-up Qs:  {res['followup_questions_count']}")
            print(f"  Duration:      {res['duration_seconds']:.1f}s")

        meta["errors"] = errors
        _save_progress(output_path, results, meta)

    summary = _build_summary(results)

    report = {
        "meta": {
            **meta,
            "status": "complete",
            "timestamp_end": datetime.now().isoformat(),
        },
        "summary": summary,
        "cases": results,
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    progress_path = output_path.with_suffix(".progress.json")
    if progress_path.exists():
        try:
            progress_path.unlink()
        except Exception:
            pass

    print(f"\n{'='*70}")
    print(f"  Evaluation complete")
    print(f"  Batch ID:  {batch_id}")
    print(f"  Results:   {output_path}")
    _print_summary(summary)
    print(f"{'='*70}\n")

    return report



def _build_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute accuracy metrics from evaluation results."""
    valid = [r for r in results if r.get("status", "ok") == "ok"]
    total_valid = len(valid)

    if total_valid == 0:
        return {
            "total_evaluated": 0,
            "total_errors": len(results),
            "sim_phase1_top1_accuracy": 0.0,
            "sim_phase1_top3_accuracy": 0.0,
            "sim_phase2_top1_accuracy": 0.0,
            "sim_phase2_top3_accuracy": 0.0,
            "mean_sim_score_phase1": 0.0,
            "mean_sim_score_phase2": 0.0,
        }

    sim_p1_top1_correct = sum(1 for r in valid if r.get("sim_correct_phase1"))
    sim_p1_top3_correct = sum(1 for r in valid if r.get("sim_top3_correct_phase1"))
    sim_p2_top1_correct = sum(1 for r in valid if r.get("sim_correct_phase2"))
    sim_p2_top3_correct = sum(1 for r in valid if r.get("sim_top3_correct_phase2"))
    mean_sim_p1 = round(
        sum(r.get("sim_score_phase1_top1", 0.0) for r in valid) / total_valid, 4
    )
    mean_sim_p2 = round(
        sum(r.get("sim_score_phase2_top1", 0.0) for r in valid) / total_valid, 4
    )

    summary: dict[str, Any] = {
        "total_evaluated": total_valid,
        "total_errors": len(results) - total_valid,
        "sim_phase1_top1_accuracy": round(sim_p1_top1_correct / total_valid, 4),
        "sim_phase1_top3_accuracy": round(sim_p1_top3_correct / total_valid, 4),
        "sim_phase2_top1_accuracy": round(sim_p2_top1_correct / total_valid, 4),
        "sim_phase2_top3_accuracy": round(sim_p2_top3_correct / total_valid, 4),
        "sim_phase1_top1_correct": sim_p1_top1_correct,
        "sim_phase1_top3_correct": sim_p1_top3_correct,
        "sim_phase2_top1_correct": sim_p2_top1_correct,
        "sim_phase2_top3_correct": sim_p2_top3_correct,
        "mean_sim_score_phase1": mean_sim_p1,
        "mean_sim_score_phase2": mean_sim_p2,
        "avg_duration_seconds": round(
            sum(r["duration_seconds"] for r in valid) / total_valid, 2
        ),
       
        "total_followup_questions": sum(
            r["followup_questions_count"] for r in valid
        ),
        "avg_followup_questions": round(
            sum(r["followup_questions_count"] for r in valid) / total_valid, 2
        ),
    }

    for var_key in ["100", "80", "50"]:
        var_results = [r for r in valid if r["variant"] == var_key]
        if not var_results:
            continue
        n = len(var_results)
        summary[f"variant_{var_key}_count"] = n
        summary[f"variant_{var_key}_sim_phase1_top1_acc"] = round(
            sum(1 for r in var_results if r.get("sim_correct_phase1")) / n, 4
        )
        summary[f"variant_{var_key}_sim_phase1_top3_acc"] = round(
            sum(1 for r in var_results if r.get("sim_top3_correct_phase1")) / n, 4
        )
        summary[f"variant_{var_key}_sim_phase2_top1_acc"] = round(
            sum(1 for r in var_results if r.get("sim_correct_phase2")) / n, 4
        )
        summary[f"variant_{var_key}_sim_phase2_top3_acc"] = round(
            sum(1 for r in var_results if r.get("sim_top3_correct_phase2")) / n, 4
        )
        summary[f"variant_{var_key}_mean_sim_phase1"] = round(
            sum(r.get("sim_score_phase1_top1", 0.0) for r in var_results) / n, 4
        )
        summary[f"variant_{var_key}_mean_sim_phase2"] = round(
            sum(r.get("sim_score_phase2_top1", 0.0) for r in var_results) / n, 4
        )
        summary[f"variant_{var_key}_avg_duration_seconds"] = round(
            sum(r["duration_seconds"] for r in var_results) / n, 2
        )
        summary[f"variant_{var_key}_total_followup_questions"] = sum(
            r["followup_questions_count"] for r in var_results
        )
        summary[f"variant_{var_key}_avg_followup_questions"] = round(
            sum(r["followup_questions_count"] for r in var_results) / n, 2
        )

    prognoses = set(r["expected_prognosis"] for r in valid)
    per_prognosis: dict[str, dict] = {}
    for prog in sorted(prognoses):
        prog_results = [r for r in valid if r["expected_prognosis"] == prog]
        n = len(prog_results)
        per_prognosis[prog] = {
            "count": n,
            "sim_phase1_top1_acc": round(
                sum(1 for r in prog_results if r.get("sim_correct_phase1")) / n, 4
            ),
            "sim_phase2_top1_acc": round(
                sum(1 for r in prog_results if r.get("sim_correct_phase2")) / n, 4
            ),
            "sim_phase1_top3_acc": round(
                sum(1 for r in prog_results if r.get("sim_top3_correct_phase1")) / n, 4
            ),
            "sim_phase2_top3_acc": round(
                sum(1 for r in prog_results if r.get("sim_top3_correct_phase2")) / n, 4
            ),
            "mean_sim_score_phase1": round(
                sum(r.get("sim_score_phase1_top1", 0.0) for r in prog_results) / n, 4
            ),
            "mean_sim_score_phase2": round(
                sum(r.get("sim_score_phase2_top1", 0.0) for r in prog_results) / n, 4
            ),
            "avg_duration_seconds": round(
                sum(r["duration_seconds"] for r in prog_results) / n, 2
            ),
        }
    summary["per_prognosis"] = per_prognosis

    return summary


def _print_summary(summary: dict[str, Any]) -> None:
    """Print a human-readable summary to stdout."""
    print(f"\n  ── Overall Accuracy (embedding cosine ≥ {DEFAULT_THRESHOLD:.2f}) ──")
    print(f"  Cases evaluated:       {summary.get('total_evaluated', 0)}")
    print(f"  Errors:                {summary.get('total_errors', 0)}")
    print(f"  Phase 1 Sim Top-1 acc:   {summary.get('sim_phase1_top1_accuracy', 0):.1%}  (mean score: {summary.get('mean_sim_score_phase1', 0):.3f})")
    print(f"  Phase 1 Sim Top-3 acc:   {summary.get('sim_phase1_top3_accuracy', 0):.1%}")
    print(f"  Phase 2 Sim Top-1 acc:   {summary.get('sim_phase2_top1_accuracy', 0):.1%}  (mean score: {summary.get('mean_sim_score_phase2', 0):.3f})")
    print(f"  Phase 2 Sim Top-3 acc:   {summary.get('sim_phase2_top3_accuracy', 0):.1%}")
    print(f"  Avg duration:          {summary.get('avg_duration_seconds', 0):.1f}s")
    print(f"  Total follow-up Qs:    {summary.get('total_followup_questions', 0)}")
    print(f"  Avg follow-up Qs:      {summary.get('avg_followup_questions', 0):.2f}")

    for var_key in ["100", "80", "50"]:
        count_key = f"variant_{var_key}_count"
        if count_key in summary:
            print(f"\n  ── Variant {var_key}% (n={summary[count_key]}) ──")
            print(f"  Phase 1 Sim Top-1 acc:   {summary.get(f'variant_{var_key}_sim_phase1_top1_acc', 0):.1%}  (mean: {summary.get(f'variant_{var_key}_mean_sim_phase1', 0):.3f})")
            print(f"  Phase 1 Sim Top-3 acc:   {summary.get(f'variant_{var_key}_sim_phase1_top3_acc', 0):.1%}")
            print(f"  Phase 2 Sim Top-1 acc:   {summary.get(f'variant_{var_key}_sim_phase2_top1_acc', 0):.1%}  (mean: {summary.get(f'variant_{var_key}_mean_sim_phase2', 0):.3f})")
            print(f"  Phase 2 Sim Top-3 acc:   {summary.get(f'variant_{var_key}_sim_phase2_top3_acc', 0):.1%}")
            print(f"  Avg duration:            {summary.get(f'variant_{var_key}_avg_duration_seconds', 0):.2f}s")
            print(f"  Total follow-up Qs:      {summary.get(f'variant_{var_key}_total_followup_questions', 0)}")
            print(f"  Avg follow-up Qs:        {summary.get(f'variant_{var_key}_avg_followup_questions', 0):.2f}")



def main():
    parser = argparse.ArgumentParser(
        description="MedMAS_DDx Batch Evaluation Runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""

  python -m MedMAS_DDx.evaluation.eval_runner --variant 50 --limit 5
        """,
    )
    parser.add_argument(
        "--csv",
        type=str,
        default=str(DEFAULT_CSV),
        help=f"Path to Patient_Cases.csv (default: {DEFAULT_CSV})",
    )
    parser.add_argument(
        "--variant",
        type=str,
        choices=["100", "80", "50"],
        default=None,
        help="Run only a specific case variant (100, 80, or 50). Default: all.",
    )
    parser.add_argument(
        "--prognosis",
        type=str,
        default=None,
        help="Filter by expected prognosis (case-insensitive substring match).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of (case, variant) pairs to evaluate.",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help=(
            "Explicit output JSON file path.  If omitted, the file is placed "
            "automatically inside eval_results/<batch-id>/."
        ),
    )
    parser.add_argument(
        "--batch-id",
        type=str,
        default=None,
        metavar="BATCH_ID",
        help=(
            "Batch folder name under eval_results/ (e.g. 'batch_20260505_143022').  "
            "Auto-generated if omitted.  Pass the same value across multiple "
            "invocations (e.g. one per variant) to collect all their output files "
            "in one folder."
        ),
    )
    parser.add_argument(
        "--log-dir",
        type=str,
        default=None,
        help="Directory for per-case CrewAI log files. Default: logs/",
    )
    parser.add_argument(
        "--retry-failed",
        type=str,
        default=None,
        metavar="PREV_BATCH_JSON",
        help=(
            "Path to a previous batch result JSON.  Only cases whose status is "
            "'api_unavailable' or 'error' will be re-evaluated; all other cases are "
            "carried over unchanged.  Example: "
            "--retry-failed eval_results/eval_100pct_20260505_120000.json"
        ),
    )
    parser.add_argument(
        "--retry-statuses",
        type=str,
        default="api_unavailable,error",
        help=(
            "Comma-separated list of statuses to retry when using --retry-failed. "
            "Choices: api_unavailable, error.  Default: 'api_unavailable,error'."
        ),
    )
    parser.add_argument(
        "--patient-llm",
        action="store_true",
        help=(
            "Use a simulated-patient LLM (instead of SYMP/HP synonym matching) "
            "to answer follow-up questions during batch testing.  The patient "
            "agent receives the full 100% case text and the canonical "
            "symptom list as ground-truth context."
        ),
    )
    parser.add_argument(
        "--patient-llm-model",
        type=str,
        default=None,
        metavar="MODEL",
        help=(
            "Optional model override for the patient LLM.  Defaults to the "
            "PATIENT_LLM env var, then MEDMAS_DDX_LLM."
        ),
    )
    args = parser.parse_args()

    log_dir = Path(args.log_dir) if args.log_dir else Path(__file__).resolve().parents[3] / "logs"
    retry_statuses = tuple(s.strip() for s in args.retry_statuses.split(",") if s.strip())

    run_evaluation(
        csv_path=args.csv,
        variant_filter=args.variant,
        prognosis_filter=args.prognosis,
        limit=args.limit,
        output_path=args.output,
        log_dir=log_dir,
        retry_failed_path=args.retry_failed,
        retry_statuses=retry_statuses,
        batch_id=args.batch_id,
        use_patient_llm=args.patient_llm,
        patient_llm_model=args.patient_llm_model,
    )


if __name__ == "__main__":
    main()