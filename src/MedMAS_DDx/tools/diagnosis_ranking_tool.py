

from __future__ import annotations

import json
import math
from typing import Any

from MedMAS_DDx.utils import parse_json_safe


def rank_diagnoses(disease_results_raw: str | dict[str, Any]) -> dict[str, Any]:
   
    if isinstance(disease_results_raw, str):
        payload = parse_json_safe(disease_results_raw)
    else:
        payload = disease_results_raw

    disease_results: list[dict[str, Any]] = payload.get("disease_results", [])

    if not disease_results:
        return {
            "differential_diagnosis": [],
            "scoring_metadata": {
                "total_mapped_symptoms": 0,
                "symptom_disease_counts": {},
                "specificity_weights": {},
            },
            "total_symptoms_processed": 0,
            "total_diseases_found": 0,
            "unmapped_symptoms": [],
            "notes": "No disease_results provided — nothing to rank.",
        }

    symptom_disease_counts: dict[str, int] = {}
    unmapped_symptoms: list[str] = []
    total_symptoms_processed = len(disease_results)

    for entry in disease_results:
        symptom = entry.get("symptom", "")
        status = entry.get("status", "")
        candidates = entry.get("disease_candidates", [])

        if status in ("unmapped", "skipped") or not candidates:
            if symptom:
                unmapped_symptoms.append(symptom)
            continue

        symptom_disease_counts[symptom] = len(set(candidates))

    specificity_weights: dict[str, float] = {}
    for symptom, dc in symptom_disease_counts.items():
        if dc == 0:
            continue
        specificity_weights[symptom] = round(1.0 / math.log2(1 + dc), 3)

    total_mapped_symptoms = len(specificity_weights)

    disease_matching_symptoms: dict[str, list[str]] = {}
    all_disease_labels: set[str] = set()

    for entry in disease_results:
        symptom = entry.get("symptom", "")
        status = entry.get("status", "")
        candidates = entry.get("disease_candidates", [])

        if status in ("unmapped", "skipped") or not candidates:
            continue

        for disease_label in candidates:
            if not disease_label:
                continue
            all_disease_labels.add(disease_label)
            if disease_label not in disease_matching_symptoms:
                disease_matching_symptoms[disease_label] = []
            if symptom not in disease_matching_symptoms[disease_label]:
                disease_matching_symptoms[disease_label].append(symptom)

    if not disease_matching_symptoms:
        return {
            "differential_diagnosis": [],
            "scoring_metadata": {
                "total_mapped_symptoms": total_mapped_symptoms,
                "symptom_disease_counts": symptom_disease_counts,
                "specificity_weights": specificity_weights,
            },
            "total_symptoms_processed": total_symptoms_processed,
            "total_diseases_found": 0,
            "unmapped_symptoms": unmapped_symptoms,
            "notes": "All disease_candidates lists were empty — no diseases to rank.",
        }

    scored_diseases: list[dict[str, Any]] = []

    for disease, matching_syms in disease_matching_symptoms.items():
        match_count = len(matching_syms)
        raw_score = sum(specificity_weights.get(s, 0.0) for s in matching_syms)
        coverage = match_count / total_mapped_symptoms if total_mapped_symptoms > 0 else 0.0
        composite_score = raw_score * (0.5 + 0.5 * coverage)

        supporting_symptoms = [
            {
                "symptom": s,
                "specificity_weight": specificity_weights.get(s, 0.0),
            }
            for s in matching_syms
        ]

        scored_diseases.append({
            "disease": disease,
            "match_count": match_count,
            "raw_score": round(raw_score, 3),
            "coverage": round(coverage, 3),
            "composite_score": round(composite_score, 3),
            "supporting_symptoms": supporting_symptoms,
        })

    scored_diseases.sort(
        key=lambda x: (-x["composite_score"], -x["match_count"], x["disease"])
    )


    max_ranked = 50
    min_ranked = 20
    cutoff_ratio = 0.1

    if scored_diseases:
        top_raw = scored_diseases[0]["composite_score"]
        threshold = cutoff_ratio * top_raw
        kept: list[dict[str, Any]] = []
        for ds in scored_diseases:
            if len(kept) >= max_ranked:
                break
            if ds["composite_score"] >= threshold or ds["match_count"] >= 2:
                kept.append(ds)
            elif len(kept) < min_ranked:
               
                kept.append(ds)
        scored_diseases = kept

    top_score = scored_diseases[0]["composite_score"] if scored_diseases else 0.0
    weak_evidence = top_score < 0.5

    differential_diagnosis: list[dict[str, Any]] = []
    for rank_idx, ds in enumerate(scored_diseases, start=1):
        cs = ds["composite_score"]
        mc = ds["match_count"]

        if weak_evidence:
            if cs >= 0.4 * top_score and mc >= 2:
                confidence = "MEDIUM"
            else:
                confidence = "LOW"
        else:
            if cs >= 0.7 * top_score and mc >= 3:
                confidence = "HIGH"
            elif cs >= 0.4 * top_score and mc >= 2:
                confidence = "MEDIUM"
            else:
                confidence = "LOW"

        differential_diagnosis.append({
            "rank": rank_idx,
            "disease": ds["disease"],
            "match_count": ds["match_count"],
            "raw_score": ds["raw_score"],
            "coverage": ds["coverage"],
            "composite_score": ds["composite_score"],
            "confidence": confidence,
            "supporting_symptoms": ds["supporting_symptoms"],
        })

    notes_parts = [
        f"Ranked {len(differential_diagnosis)} diseases from "
        f"{len(all_disease_labels)} total candidates.",
        f"{total_mapped_symptoms} mapped symptoms used for scoring.",
    ]
    if unmapped_symptoms:
        notes_parts.append(
            f"{len(unmapped_symptoms)} unmapped/skipped symptoms: "
            f"{', '.join(unmapped_symptoms[:5])}"
            + ("..." if len(unmapped_symptoms) > 5 else "")
        )
    if weak_evidence:
        notes_parts.append(
            "Weak evidence overall (top_score < 0.5) — all confidences capped at MEDIUM."
        )

    return {
        "differential_diagnosis": differential_diagnosis,
        "scoring_metadata": {
            "total_mapped_symptoms": total_mapped_symptoms,
            "symptom_disease_counts": symptom_disease_counts,
            "specificity_weights": specificity_weights,
        },
        "total_symptoms_processed": total_symptoms_processed,
        "total_diseases_found": len(all_disease_labels),
        "unmapped_symptoms": unmapped_symptoms,
        "notes": " ".join(notes_parts),
    }