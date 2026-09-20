

from __future__ import annotations

from typing import Any



def build_patient_symptom_set(
    disease_results: list[dict[str, Any]] | None = None,
    symp_mappings: list[dict[str, Any]] | None = None,
    extracted_symptoms: list[dict[str, Any]] | None = None,
) -> set[str]:
    known: set[str] = set()

    if disease_results:
        for entry in disease_results:
            if isinstance(entry, dict):
                sym = str(entry.get("symptom", "")).strip().lower()
                if sym:
                    known.add(sym)

    if symp_mappings:
        for m in symp_mappings:
            if not isinstance(m, dict):
                continue
            raw = str(m.get("symptom", "")).strip().lower()
            label = str(m.get("matched_symp_label", "") or "").strip().lower()
            if raw:
                known.add(raw)
            if label:
                known.add(label)

    if extracted_symptoms:
        for s in extracted_symptoms:
            if isinstance(s, dict):
                name = str(s.get("name", "")).strip().lower()
                if name:
                    known.add(name)

    return known



def sanitize_followup_payload(
    followup_payload: dict[str, Any],
    patient_symptom_set: set[str],
    max_questions: int = 11,
) -> dict[str, Any]:
   
    if not isinstance(followup_payload, dict):
        return followup_payload

    new_payload = dict(followup_payload)

    profiles = followup_payload.get("disease_symptom_profiles") or []
    profiled_syms_per_disease: list[set[str]] = []
    for p in profiles:
        if isinstance(p, dict) and p.get("status") == "profiled":
            syms = {
                str(s).strip().lower()
                for s in (p.get("candidate_symptoms") or [])
                if s
            }
            if syms:
                profiled_syms_per_disease.append(syms)

    n_profiled = len(profiled_syms_per_disease)

    def appears_in_all_profiles(sym_lower: str) -> bool:
        if n_profiled == 0:
            return False
        return all(sym_lower in syms for syms in profiled_syms_per_disease)

    patient_set_lower = {s.strip().lower() for s in patient_symptom_set if s}

    def is_already_known(sym_lower: str) -> bool:
        if sym_lower in patient_set_lower:
            return True
        return False

    questions = followup_payload.get("questions_asked") or []
    kept_questions: list[dict[str, Any]] = []
    seen: set[str] = set()
    dropped_already_known: list[str] = []
    dropped_useless: list[str] = []
    dropped_duplicate: list[str] = []

    for q in questions:
        if not isinstance(q, dict):
            continue
        sym = str(q.get("symptom", "")).strip()
        sym_lower = sym.lower()
        if not sym:
            continue

        if is_already_known(sym_lower):
            dropped_already_known.append(sym)
            continue

        if appears_in_all_profiles(sym_lower):
            dropped_useless.append(sym)
            continue

        if sym_lower in seen:
            dropped_duplicate.append(sym)
            continue

        seen.add(sym_lower)
        kept_questions.append(q)

    
    dropped_capped: list[str] = []
    if len(kept_questions) > max_questions:
        dropped_capped = [
            str(q.get("symptom", "")).strip()
            for q in kept_questions[max_questions:]
        ]
        kept_questions = kept_questions[:max_questions]

    new_payload["questions_asked"] = kept_questions

    investigations = followup_payload.get("investigations_required") or []
    cleaned_invs: list[dict[str, Any]] = []
    dropped_invs: list[dict[str, Any]] = []

    _patient_reportable_markers = (
        "treated here as patient_reportable",
        "treated as patient_reportable",
        "patient_reportable so no investigation_required",
        "patient reportable; no investigation",
    )

    for inv in investigations:
        if not isinstance(inv, dict):
            continue
        finding = str(inv.get("finding", "")).strip().lower()
        reason = str(inv.get("reason", "")).strip().lower()

        if any(marker in reason for marker in _patient_reportable_markers):
            dropped_invs.append(
                {"finding": finding, "reason": "patient-reportable; not a clinical investigation"}
            )
            continue

        if finding and is_already_known(finding):
            dropped_invs.append(
                {"finding": finding, "reason": "already in patient symptoms"}
            )
            continue

        if any(
            finding == str(q.get("symptom", "")).strip().lower()
            for q in kept_questions
        ):
            dropped_invs.append(
                {"finding": finding, "reason": "also in questions_asked"}
            )
            continue

        cleaned_invs.append(inv)

    new_payload["investigations_required"] = cleaned_invs

    new_payload["sanitization_report"] = {
        "original_question_count": len(questions),
        "kept_question_count": len(kept_questions),
        "dropped_already_known": dropped_already_known,
        "dropped_useless_zero_weight": dropped_useless,
        "dropped_duplicate": dropped_duplicate,
        "dropped_over_cap": dropped_capped,
        "original_investigation_count": len(investigations),
        "kept_investigation_count": len(cleaned_invs),
        "dropped_investigations": dropped_invs,
        "patient_symptom_set_size": len(patient_set_lower),
    }

    if not kept_questions:
        new_payload["followup_needed"] = False

    return new_payload



def canonicalize_baseline_symptoms(
    ranking_payload: dict[str, Any],
    symp_mappings: list[dict[str, Any]],
) -> dict[str, Any]:
   
    if not isinstance(ranking_payload, dict):
        return ranking_payload

    rename: dict[str, str] = {}
    for m in symp_mappings or []:
        if not isinstance(m, dict):
            continue
        if m.get("status") != "mapped":
            continue
        raw = str(m.get("symptom", "")).strip().lower()
        label = str(m.get("matched_symp_label", "") or "").strip()
        if raw and label:
            rename[raw] = label

    if not rename:
        return ranking_payload  

    new_payload = dict(ranking_payload)
    new_diff_diag = []
    for d in ranking_payload.get("differential_diagnosis", []) or []:
        d2 = dict(d)
        new_supporting = []
        for s in d.get("supporting_symptoms", []) or []:
            if isinstance(s, dict):
                raw = str(s.get("symptom", "")).strip().lower()
                s2 = dict(s)
                if raw in rename:
                    s2["symptom"] = rename[raw]
                new_supporting.append(s2)
            else:
                new_supporting.append(s)
        d2["supporting_symptoms"] = new_supporting
        new_diff_diag.append(d2)
    new_payload["differential_diagnosis"] = new_diff_diag
    return new_payload