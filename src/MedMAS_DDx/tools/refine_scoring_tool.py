

from __future__ import annotations

import json
import math
import os
from typing import Any, Type

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from MedMAS_DDx.utils import parse_json_safe


_DEFAULT_SUPPORT_WEIGHT: float = 0.6   
_DEFAULT_CONFLICT_WEIGHT: float = 0.2  


def _env_float(name: str, default: float) -> float:
    """Read a float from the environment, falling back to *default*."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def resolve_weights(
    support_weight: float | None = None,
    conflict_weight: float | None = None,
) -> tuple[float, float]:
  
    alpha = (
        support_weight if support_weight is not None
        else _env_float("REFINE_SUPPORT_WEIGHT", _DEFAULT_SUPPORT_WEIGHT)
    )
    beta = (
        conflict_weight if conflict_weight is not None
        else _env_float("REFINE_CONFLICT_WEIGHT", _DEFAULT_CONFLICT_WEIGHT)
    )
    return alpha, beta


SUPPORT_WEIGHT, CONFLICT_WEIGHT = resolve_weights()


class RefineScoringToolInput(BaseModel):

    initial_ranking_json: str = Field(
        ...,
        description=(
            "JSON string of the Phase 1 ranking payload. Must contain "
            "'differential_diagnosis' (list of ranked diseases with composite_score "
            "and match_count) and optionally 'scoring_metadata'."
        ),
    )
    followup_payload_json: str = Field(
        ...,
        description=(
            "JSON string of the follow-up payload. Must contain "
            "'disease_symptom_profiles' (list of profiled diseases with "
            "candidate_symptoms) and 'questions_asked' (list of questions "
            "with symptom and patient_answer fields)."
        ),
    )


class RefineScoring(BaseTool):
   

    name: str = "refine_scoring"
    description: str = (
        "Deterministic Phase 2 refinement scorer. Computes discriminative "
        "weights for each follow-up symptom, then calculates support_delta "
        "and conflict_delta for every disease based on patient answers. "
        "Baseline-supporting symptoms (those already counted in Phase 1's "
        "composite_score) are excluded from follow-up deltas to prevent "
        "double-counting. Returns the re-ranked differential diagnosis "
        "with correct final scores and confidence tiers.\n\n"
        "Call with:\n"
        '  {"initial_ranking_json": "<Phase 1 ranking JSON string>",\n'
        '   "followup_payload_json": "<answered follow-up JSON string>"}\n\n'
        "Returns a JSON object with:\n"
        "  refined_differential_diagnosis — re-ranked list with final_score, "
        "baseline_score, followup_support, followup_conflict, updated_confidence\n"
        "  discriminative_weights — symptom → weight mapping used\n"
        "  per_disease_details — full breakdown of which symptoms were confirmed/"
        "denied and their weights per disease (baseline-already-counted symptoms "
        "are listed separately as 'baseline_already_counted')\n"
    )
    args_schema: Type[BaseModel] = RefineScoringToolInput

    def _run(
        self,
        initial_ranking_json: str,
        followup_payload_json: str,
        support_weight: float | None = None,
        conflict_weight: float | None = None,
    ) -> str:
        try:
            ranking = parse_json_safe(initial_ranking_json)
            followup = parse_json_safe(followup_payload_json)

           
            result = compute_refinement(
                ranking, followup,
                support_weight=support_weight,
                conflict_weight=conflict_weight,
            )
            return json.dumps(result, indent=2, ensure_ascii=False)

        except Exception as exc:
            return json.dumps({
                "error": f"{type(exc).__name__}: {exc}",
                "tool_failure": True,
                "refined_differential_diagnosis": [],
            })



def compute_refinement(
    ranking_payload: dict[str, Any],
    followup_payload: dict[str, Any],
    support_weight: float | None = None,
    conflict_weight: float | None = None,
) -> dict[str, Any]:
 
    alpha, beta = resolve_weights(support_weight, conflict_weight)

    diff_diag = ranking_payload.get("differential_diagnosis", [])[:25]
    profiles = followup_payload.get("disease_symptom_profiles", [])
    questions = followup_payload.get("questions_asked", [])

    profile_map: dict[str, set[str]] = {}
    for p in profiles:
        if p.get("status") != "profiled":
            continue
        dname = p.get("disease", "").strip().lower()
        symptoms = {s.strip().lower() for s in p.get("candidate_symptoms", [])}
        profile_map[dname] = symptoms

    n_profiled = len(profile_map)

    baseline_supporting_map: dict[str, set[str]] = {}
    for d in diff_diag:
        dname_lower = d.get("disease", "").strip().lower()
        supporting = d.get("supporting_symptoms", []) or []
        baseline_syms = set()
        for s in supporting:
            if isinstance(s, dict):
                sname = s.get("symptom", "")
            else:
                sname = str(s)
            sname = sname.strip().lower()
            if sname:
                baseline_syms.add(sname)
        baseline_supporting_map[dname_lower] = baseline_syms

    disc_weights: dict[str, float] = {}
    for q in questions:
        symptom = q.get("symptom", "").strip().lower()
        if not symptom:
            continue
        diseases_with = sum(
            1 for syms in profile_map.values() if symptom in syms
        )
        if diseases_with == 0 or n_profiled == 0:
            disc_weights[symptom] = 0.0
        else:
            disc_weights[symptom] = round(1.0 - (diseases_with / n_profiled), 3)

    per_disease_details: dict[str, dict[str, Any]] = {}
    disease_scores: list[dict[str, Any]] = []

    for d in diff_diag:
        dname = d.get("disease", "").strip()
        dname_lower = dname.lower()
        baseline = float(d.get("composite_score", 0.0))
        match_count = int(d.get("match_count", 0))

        candidate_syms = profile_map.get(dname_lower, set())
        already_counted = baseline_supporting_map.get(dname_lower, set())

        support_raw = 0.0  
        conflict_raw = 0.0 
        n_relevant = 0      
        confirmed: list[dict[str, Any]] = []
        denied: list[dict[str, Any]] = []
        neutral: list[str] = []
        baseline_already_counted: list[dict[str, Any]] = []

        for q in questions:
            symptom = q.get("symptom", "").strip().lower()
            answer = (q.get("patient_answer") or "").strip().lower()
            w = disc_weights.get(symptom, 0.0)

            if symptom in already_counted:
                baseline_already_counted.append({
                    "symptom": symptom,
                    "answer": answer or "neutral",
                    "weight": w,
                    "note": "Already in Phase 1 baseline; excluded from follow-up delta.",
                })
                continue

            if symptom not in candidate_syms:
                if not (answer.startswith("yes") or answer.startswith("no")):
                    neutral.append(symptom)
                continue

            n_relevant += 1
            if answer.startswith("yes"):
                support_raw += w
                confirmed.append({"symptom": symptom, "weight": w})
            elif answer.startswith("no"):
                conflict_raw += w
                denied.append({"symptom": symptom, "weight": w})
            else:
                neutral.append(symptom)

        if n_relevant > 0:
            support_norm = support_raw / n_relevant
            conflict_norm = conflict_raw / n_relevant
        else:
            support_norm = conflict_norm = 0.0

        multiplier = 1.0 + alpha * support_norm - beta * conflict_norm
        final_score = round(max(0.0, baseline * multiplier), 3)

        support_pts = round(baseline * alpha * support_norm, 3)
        conflict_pts = round(baseline * beta * conflict_norm, 3)
        support_raw = round(support_raw, 3)
        conflict_raw = round(conflict_raw, 3)

        per_disease_details[dname] = {
            "baseline_score": round(baseline, 3),
            "confirmed_symptoms": confirmed,
            "denied_symptoms": denied,
            "neutral_symptoms": neutral,
            "baseline_already_counted": baseline_already_counted,
            "questions_relevant": n_relevant,
            "support_raw": support_raw,
            "conflict_raw": conflict_raw,
            "support_norm": round(support_norm, 3),
            "conflict_norm": round(conflict_norm, 3),
            "support_delta": support_pts,
            "conflict_delta": conflict_pts,
            "multiplier": round(multiplier, 3),
            "final_score": final_score,
        }

        disease_scores.append({
            "disease": dname,
            "match_count": match_count,
            "baseline_score": round(baseline, 3),
            "followup_support": support_pts,
            "followup_conflict": conflict_pts,
            "followup_support_count": len(confirmed),
            "followup_conflict_count": len(denied),
            "questions_relevant": n_relevant,
            "final_score": final_score,
        })

    disease_scores.sort(
        key=lambda x: (-x["final_score"], -x["match_count"], x["disease"])
    )

    if disease_scores:
        top_score = disease_scores[0]["final_score"]
        second_score = disease_scores[1]["final_score"] if len(disease_scores) > 1 else 0.0
        gap = top_score - second_score
    else:
        top_score = 0.0
        second_score = 0.0
        gap = 0.0

    refined: list[dict[str, Any]] = []
    for rank_idx, ds in enumerate(disease_scores, start=1):
        if rank_idx == 1:
            if top_score > 0 and gap >= 0.5 * top_score:
                confidence = "HIGH"
            elif gap > 0:
                confidence = "MEDIUM"
            else:
                confidence = "LOW"
            if len(disease_scores) == 1:
                confidence = "HIGH"
        else:
            if top_score > 0 and ds["final_score"] > 0.5 * top_score:
                confidence = "MEDIUM"
            else:
                confidence = "LOW"

        refined.append({
            "rank": rank_idx,
            "disease": ds["disease"],
            "baseline_score": ds["baseline_score"],
            "followup_support": ds["followup_support"],
            "followup_conflict": ds["followup_conflict"],
            "followup_support_count": ds["followup_support_count"],
            "followup_conflict_count": ds["followup_conflict_count"],
            "questions_relevant": ds["questions_relevant"],
            "final_score": ds["final_score"],
            "updated_confidence": confidence,
        })

    disc_weights_readable = {
        q.get("symptom", "").strip(): disc_weights.get(
            q.get("symptom", "").strip().lower(), 0.0
        )
        for q in questions
        if q.get("symptom", "").strip()
    }

    most_impactful = _find_most_impactful(
        questions, disc_weights, profile_map, baseline_supporting_map
    )

    return {
        "refined_differential_diagnosis": refined,
        "discriminative_weights": disc_weights_readable,
        "per_disease_details": per_disease_details,
        "most_impactful_answer": most_impactful,
        "scoring_metadata": {
            "n_profiled_diseases": n_profiled,
            "n_questions": len(questions),
            "top_score": top_score,
            "second_score": second_score,
            "gap": round(gap, 3),
            "baseline_exclusion_applied": True,
            "support_weight": alpha,
            "conflict_weight": beta,
        },
    }


def _find_most_impactful(
    questions: list[dict],
    disc_weights: dict[str, float],
    profile_map: dict[str, set[str]],
    baseline_supporting_map: dict[str, set[str]],
) -> dict[str, Any]:
  
    best: dict[str, Any] = {
        "symptom": "none",
        "answer": "neutral",
        "weight": 0.0,
        "impact": "none",
    }
    best_impact = 0.0

    for q in questions:
        symptom = q.get("symptom", "").strip().lower()
        answer = (q.get("patient_answer") or "").strip().lower()
        w = disc_weights.get(symptom, 0.0)

        if w == 0.0 or answer not in ("yes", "no"):
            continue

        affected = 0
        for dname_lower, syms in profile_map.items():
            if symptom not in syms:
                continue
            baseline_syms = baseline_supporting_map.get(dname_lower, set())
            if symptom in baseline_syms:
                continue  
            affected += 1

        impact = w * affected
        if impact > best_impact:
            direction = "supported" if answer == "yes" else "conflicted"
            best = {
                "symptom": q.get("symptom", "").strip(),
                "answer": answer,
                "weight": w,
                "diseases_affected": affected,
                "direction": direction,
            }
            best_impact = impact

    return best