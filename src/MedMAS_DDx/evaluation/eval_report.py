#!/usr/bin/env python



from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any



def load_results(path: str | Path) -> dict[str, Any]:
    """Load evaluation results JSON."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Results file not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)



def build_confusion_matrix(
    results: list[dict[str, Any]],
    phase: str = "phase2",
) -> dict[str, dict[str, int]]:
   
    matrix: dict[str, dict[str, int]] = defaultdict(lambda: Counter())
    top_key = f"{phase}_top1"

    for r in results:
        if r.get("error"):
            continue
        actual = r["expected_prognosis"].strip()
        predicted = (r.get(top_key) or "NO_PREDICTION").strip()
        matrix[actual][predicted] += 1

    return {k: dict(v) for k, v in sorted(matrix.items())}



def failure_analysis(
    results: list[dict[str, Any]],
    phase: str = "phase2",
) -> list[dict[str, Any]]:
   
    top3_key = f"sim_top3_correct_{phase}"
    failures = []
    for r in results:
        if r.get("error"):
            failures.append({
                "case_id": r["case_id"],
                "variant": r["variant"],
                "expected": r["expected_prognosis"],
                "predicted_top1": None,
                "predicted_top3": [],
                "reason": f"Error: {r['error'][:200]}",
            })
        elif not r.get(top3_key, False):
            top1_key = f"{phase}_top1"
            top3_list_key = f"{phase}_top3"
            failures.append({
                "case_id": r["case_id"],
                "variant": r["variant"],
                "expected": r["expected_prognosis"],
                "predicted_top1": r.get(top1_key),
                "predicted_top3": r.get(top3_list_key, []),
                "reason": "Expected disease not in top-3",
            })
    return failures



def print_terminal_report(report: dict[str, Any]) -> None:
    """Print a comprehensive terminal report."""
    meta = report.get("meta", {})
    summary = report.get("summary", {})
    cases = report.get("cases", [])

    print(f"\n{'═'*72}")
    print(f"  MedMAS_DDx EVALUATION REPORT")
    print(f"{'═'*72}")
    print(f"  Timestamp:       {meta.get('timestamp', 'N/A')}")
    print(f"  CSV source:      {meta.get('csv_path', 'N/A')}")
    print(f"  Variant filter:  {meta.get('variant_filter') or 'all'}")
    print(f"  Prognosis filter: {meta.get('prognosis_filter') or 'none'}")
    print(f"  Total cases:     {meta.get('total_cases', 0)}")
    print(f"  Errors:          {meta.get('errors', 0)}")

    n = summary.get("total_evaluated", 0)

    print(f"\n{'─'*72}")
    print(f"  SIMILARITY ACCURACY  (embedding cosine, threshold ≥ 0.60)")
    print(f"{'─'*72}")
    print(f"  {'Metric':<30} {'Correct':>8} {'Total':>8} {'Accuracy':>10} {'Mean sim':>10}")
    print(f"  {'─'*68}")
    for label, correct_key, acc_key, mean_key in [
        ("Phase 1 Sim Top-1", "sim_phase1_top1_correct", "sim_phase1_top1_accuracy", "mean_sim_score_phase1"),
        ("Phase 1 Sim Top-3", "sim_phase1_top3_correct", "sim_phase1_top3_accuracy", None),
        ("Phase 2 Sim Top-1", "sim_phase2_top1_correct", "sim_phase2_top1_accuracy", "mean_sim_score_phase2"),
        ("Phase 2 Sim Top-3", "sim_phase2_top3_correct", "sim_phase2_top3_accuracy", None),
    ]:
        c = summary.get(correct_key, 0)
        a = summary.get(acc_key, 0)
        mean_val = f"{summary[mean_key]:.3f}" if mean_key and mean_key in summary else "    —  "
        print(f"  {label:<30} {c:>8} {n:>8} {a:>10.1%} {mean_val:>10}")

    print(f"\n  Avg duration per case:   {summary.get('avg_duration_seconds', 0):.1f}s")
    print(f"  Avg follow-up questions: {summary.get('avg_followup_questions', 0):.1f}")

    has_variant_data = any(f"variant_{v}_count" in summary for v in ["100", "80", "50"])
    if has_variant_data:
        print(f"\n{'─'*72}")
        print(f"  PER-VARIANT BREAKDOWN")
        print(f"{'─'*72}")
        print(f"  {'Variant':<10} {'Count':>6} {'Sim P1 T1':>10} {'Sim P1 T3':>10} {'Sim P2 T1':>10} {'Sim P2 T3':>10}")
        print(f"  {'─'*62}")
        for var_key in ["100", "80", "50"]:
            count_key = f"variant_{var_key}_count"
            if count_key not in summary:
                continue
            cnt = summary[count_key]
            s1   = summary.get(f"variant_{var_key}_sim_phase1_top1_acc", 0)
            s1t3 = summary.get(f"variant_{var_key}_sim_phase1_top3_acc", 0)
            s2   = summary.get(f"variant_{var_key}_sim_phase2_top1_acc", 0)
            s2t3 = summary.get(f"variant_{var_key}_sim_phase2_top3_acc", 0)
            print(f"  {var_key + '%':<10} {cnt:>6} {s1:>10.1%} {s1t3:>10.1%} {s2:>10.1%} {s2t3:>10.1%}")

    per_prog = summary.get("per_prognosis", {})
    if per_prog:
        print(f"\n{'─'*72}")
        print(f"  PER-PROGNOSIS BREAKDOWN")
        print(f"{'─'*72}")
        print(f"  {'Prognosis':<26} {'N':>4} {'Sim P1 T1':>10} {'Sim P1 T3':>10} {'Sim P2 T1':>10} {'Sim P2 T3':>10}")
        print(f"  {'─'*78}")
        for prog, stats in sorted(per_prog.items()):
            n_prog = stats["count"]
            s1   = stats.get("sim_phase1_top1_acc", 0)
            s1t3 = stats.get("sim_phase1_top3_acc", 0)
            s2   = stats.get("sim_phase2_top1_acc", 0)
            s2t3 = stats.get("sim_phase2_top3_acc", 0)
            print(f"  {prog:<26} {n_prog:>4} {s1:>10.1%} {s1t3:>10.1%} {s2:>10.1%} {s2t3:>10.1%}")

    valid_cases = [c for c in cases if not c.get("error")]
    if valid_cases:
        cm = build_confusion_matrix(valid_cases, phase="phase2")
        print(f"\n{'─'*72}")
        print(f"  CONFUSION MATRIX (Phase 2 Top-1)")
        print(f"{'─'*72}")
        print(f"  {'Actual':<28} → {'Predicted (top counts)'}")
        print(f"  {'─'*60}")
        for actual, preds in sorted(cm.items()):
            sorted_preds = sorted(preds.items(), key=lambda x: -x[1])
            pred_str = ", ".join(f"{p}({c})" for p, c in sorted_preds[:3])
            print(f"  {actual:<28} → {pred_str}")

    failures = failure_analysis(valid_cases, phase="phase2")
    if failures:
        print(f"\n{'─'*72}")
        print(f"  FAILURE ANALYSIS (expected NOT in Phase 2 top-3): {len(failures)} cases")
        print(f"{'─'*72}")
        for f in failures[:20]: 
            print(f"  Case #{f['case_id']} ({f['variant']}%)")
            print(f"    Expected:  {f['expected']}")
            print(f"    Got top-1: {f['predicted_top1']}")
            print(f"    Got top-3: {f['predicted_top3']}")
            print()
        if len(failures) > 20:
            print(f"  ... and {len(failures) - 20} more failures (see JSON for full list)")

    print(f"\n{'═'*72}\n")



def export_csv(report: dict[str, Any], output_path: str | Path) -> None:
    """Export per-case results to a CSV file for spreadsheet analysis."""
    cases = report.get("cases", [])
    if not cases:
        print("[Report] No cases to export.")
        return

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "case_id",
        "variant",
        "expected_prognosis",
        "phase1_top1",
        "top1_correct_phase1",
        "top1_tied_correct_phase1",
        "phase1_top3",
        "top3_correct_phase1",
        "phase2_top1",
        "top1_correct_phase2",
        "top1_tied_correct_phase2",
        "phase2_top3",
        "top3_correct_phase2",
        "sim_score_phase1_top1",
        "sim_score_phase1_top3",
        "sim_correct_phase1",
        "sim_top3_correct_phase1",
        "sim_score_phase2_top1",
        "sim_score_phase2_top3",
        "sim_correct_phase2",
        "sim_top3_correct_phase2",
        "followup_questions_count",
        "duration_seconds",
        "error",
    ]

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for c in cases:
            row = dict(c)
            row["phase1_top3"] = " | ".join(c.get("phase1_top3", []))
            row["phase2_top3"] = " | ".join(c.get("phase2_top3", []))
            writer.writerow(row)

    print(f"[Report] CSV exported to: {output_path}")



def export_html(report: dict[str, Any], output_path: str | Path) -> None:
    """Export a self-contained HTML report."""
    meta = report.get("meta", {})
    summary = report.get("summary", {})
    cases = report.get("cases", [])
    valid_cases = [c for c in cases if not c.get("error")]

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    cm = build_confusion_matrix(valid_cases, phase="phase2")
    failures = failure_analysis(valid_cases, phase="phase2")

    html_parts = [
        "<!DOCTYPE html>",
        "<html><head><meta charset='utf-8'>",
        "<title>MedMAS_DDx Evaluation Report</title>",
        "<style>",
        "  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;",
        "         max-width: 1100px; margin: 2rem auto; padding: 0 1rem; color: #1a1a1a; }",
        "  h1 { color: #2c3e50; border-bottom: 3px solid #3498db; padding-bottom: 0.5rem; }",
        "  h2 { color: #34495e; margin-top: 2rem; }",
        "  table { border-collapse: collapse; width: 100%; margin: 1rem 0; }",
        "  th, td { border: 1px solid #ddd; padding: 0.5rem 0.75rem; text-align: left; font-size: 0.9rem; }",
        "  th { background: #f8f9fa; font-weight: 600; }",
        "  tr:nth-child(even) { background: #fdfdfd; }",
        "  .pass { color: #27ae60; font-weight: 600; }",
        "  .fail { color: #e74c3c; font-weight: 600; }",
        "  .tied { color: #f39c12; font-weight: 600; }",
        "  .metric-card { display: inline-block; background: #f8f9fa; border-radius: 8px;",
        "                  padding: 1rem 1.5rem; margin: 0.5rem; text-align: center; min-width: 150px; }",
        "  .metric-value { font-size: 1.8rem; font-weight: 700; color: #2c3e50; }",
        "  .metric-label { font-size: 0.85rem; color: #7f8c8d; margin-top: 0.25rem; }",
        "</style></head><body>",
        "<h1>MedMAS_DDx Evaluation Report</h1>",
        f"<p><strong>Timestamp:</strong> {meta.get('timestamp', 'N/A')} · "
        f"<strong>Cases:</strong> {meta.get('total_cases', 0)} · "
        f"<strong>Errors:</strong> {meta.get('errors', 0)} · "
        f"<strong>Variant:</strong> {meta.get('variant_filter') or 'all'}</p>",
    ]

    html_parts.append("<h2>Similarity Accuracy (embedding cosine ≥ 0.60)</h2>")
    for label, acc_key, mean_key in [
        ("P1 Sim Top-1", "sim_phase1_top1_accuracy", "mean_sim_score_phase1"),
        ("P1 Sim Top-3", "sim_phase1_top3_accuracy", None),
        ("P2 Sim Top-1", "sim_phase2_top1_accuracy", "mean_sim_score_phase2"),
        ("P2 Sim Top-3", "sim_phase2_top3_accuracy", None),
    ]:
        val = summary.get(acc_key, 0)
        mean_str = (
            f"<div class='metric-label' style='font-size:0.75rem'>mean {summary[mean_key]:.3f}</div>"
            if mean_key and mean_key in summary else ""
        )
        html_parts.append(
            f"<div class='metric-card'>"
            f"<div class='metric-value'>{val:.1%}</div>"
            f"<div class='metric-label'>{label}</div>{mean_str}</div>"
        )

    has_variant = any(f"variant_{v}_count" in summary for v in ["100", "80", "50"])
    if has_variant:
        html_parts.append("<h2>Per-Variant Breakdown (similarity)</h2>")
        html_parts.append(
            "<table><tr><th>Variant</th><th>Count</th>"
            "<th>Sim P1 Top-1</th><th>Sim P1 Top-3</th>"
            "<th>Sim P2 Top-1</th><th>Sim P2 Top-3</th></tr>"
        )
        for v in ["100", "80", "50"]:
            ck = f"variant_{v}_count"
            if ck not in summary:
                continue
            html_parts.append(
                f"<tr><td>{v}%</td><td>{summary[ck]}</td>"
                f"<td>{summary.get(f'variant_{v}_sim_phase1_top1_acc', 0):.1%}</td>"
                f"<td>{summary.get(f'variant_{v}_sim_phase1_top3_acc', 0):.1%}</td>"
                f"<td>{summary.get(f'variant_{v}_sim_phase2_top1_acc', 0):.1%}</td>"
                f"<td>{summary.get(f'variant_{v}_sim_phase2_top3_acc', 0):.1%}</td></tr>"
            )
        html_parts.append("</table>")

    per_prog = summary.get("per_prognosis", {})
    if per_prog:
        html_parts.append("<h2>Per-Prognosis Breakdown (similarity)</h2>")
        html_parts.append(
            "<table><tr><th>Prognosis</th><th>N</th>"
            "<th>Sim P1 Top-1</th><th>Sim P1 Top-3</th>"
            "<th>Sim P2 Top-1</th><th>Sim P2 Top-3</th>"
            "<th>Mean Sim P1</th><th>Mean Sim P2</th></tr>"
        )
        for prog, stats in sorted(per_prog.items()):
            html_parts.append(
                f"<tr><td>{prog}</td><td>{stats['count']}</td>"
                f"<td>{stats.get('sim_phase1_top1_acc', 0):.1%}</td>"
                f"<td>{stats.get('sim_phase1_top3_acc', 0):.1%}</td>"
                f"<td>{stats.get('sim_phase2_top1_acc', 0):.1%}</td>"
                f"<td>{stats.get('sim_phase2_top3_acc', 0):.1%}</td>"
                f"<td>{stats.get('mean_sim_score_phase1', 0):.3f}</td>"
                f"<td>{stats.get('mean_sim_score_phase2', 0):.3f}</td></tr>"
            )
        html_parts.append("</table>")

    if cm:
        html_parts.append("<h2>Confusion Matrix (Phase 2 Top-1)</h2>")
        html_parts.append("<table><tr><th>Actual</th><th>Predicted (top counts)</th></tr>")
        for actual, preds in sorted(cm.items()):
            sorted_preds = sorted(preds.items(), key=lambda x: -x[1])
            pred_str = ", ".join(f"{p} ({c})" for p, c in sorted_preds[:4])
            html_parts.append(f"<tr><td>{actual}</td><td>{pred_str}</td></tr>")
        html_parts.append("</table>")

    html_parts.append("<h2>Per-Case Results</h2>")
    html_parts.append(
        "<table><tr><th>#</th><th>Var</th><th>Expected</th>"
        "<th>P1 Top-1</th><th>P1 ✓</th><th>P2 Top-1</th><th>P2 ✓</th>"
        "<th>Sim P1</th><th>Sim P2</th>"
        "<th>FU Qs</th><th>Time</th></tr>"
    )

    def _case_mark(strict: bool, tied: bool) -> tuple[str, str]:
        if strict:
            return ("pass", "✓")
        if tied:
            return ("tied", "≈")
        return ("fail", "✗")

    for c in cases:
        p1_cls, p1_sym = _case_mark(
            bool(c.get("top1_correct_phase1")),
            bool(c.get("top1_tied_correct_phase1")),
        )
        p2_cls, p2_sym = _case_mark(
            bool(c.get("top1_correct_phase2")),
            bool(c.get("top1_tied_correct_phase2")),
        )
        err = c.get("error")
        s1 = c.get("sim_score_phase1_top1", 0.0)
        s2 = c.get("sim_score_phase2_top1", 0.0)
        s1_cls = "pass" if c.get("sim_correct_phase1") else ("tied" if s1 >= 0.45 else "fail")
        s2_cls = "pass" if c.get("sim_correct_phase2") else ("tied" if s2 >= 0.45 else "fail")
        html_parts.append(
            f"<tr><td>{c['case_id']}</td><td>{c['variant']}%</td>"
            f"<td>{c['expected_prognosis']}</td>"
            f"<td>{c.get('phase1_top1') or ('ERR' if err else '—')}</td>"
            f"<td class='{p1_cls}'>{p1_sym}</td>"
            f"<td>{c.get('phase2_top1') or ('ERR' if err else '—')}</td>"
            f"<td class='{p2_cls}'>{p2_sym}</td>"
            f"<td class='{s1_cls}'>{s1:.2f}</td>"
            f"<td class='{s2_cls}'>{s2:.2f}</td>"
            f"<td>{c.get('followup_questions_count', 0)}</td>"
            f"<td>{c.get('duration_seconds', 0):.1f}s</td></tr>"
        )
    html_parts.append("</table>")
    html_parts.append(
        "<p style='font-size:0.85rem;color:#7f8c8d;margin-top:-0.5rem;'>"
        "<strong>✓</strong> = strict top-1 correct &nbsp;·&nbsp; "
        "<strong class='tied'>≈</strong> = tied for top-1 with the correct disease "
        "(missed by strict top-1 due to tiebreaker) &nbsp;·&nbsp; "
        "<strong class='fail'>✗</strong> = incorrect &nbsp;·&nbsp; "
        "<strong>Sim P1/P2</strong> = cosine similarity score "
        "(<span class='pass'>green</span> ≥ 0.60, "
        "<span class='tied'>amber</span> ≥ 0.45, "
        "<span class='fail'>red</span> &lt; 0.45)"
        "</p>"
    )

    if failures:
        html_parts.append(f"<h2>Failure Analysis ({len(failures)} cases)</h2>")
        html_parts.append("<table><tr><th>#</th><th>Var</th><th>Expected</th><th>Got Top-1</th><th>Got Top-3</th></tr>")
        for f in failures:
            t3 = ", ".join(f.get("predicted_top3", []))
            html_parts.append(
                f"<tr><td>{f['case_id']}</td><td>{f['variant']}%</td>"
                f"<td>{f['expected']}</td><td>{f.get('predicted_top1') or '—'}</td>"
                f"<td>{t3 or '—'}</td></tr>"
            )
        html_parts.append("</table>")

    html_parts.append("</body></html>")

    with open(output_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(html_parts))

    print(f"[Report] HTML report exported to: {output_path}")



def main():
    parser = argparse.ArgumentParser(
        description="MedMAS_DDx Evaluation Report Generator",
    )
    parser.add_argument(
        "results_json",
        type=str,
        help="Path to the eval results JSON (output of eval_runner.py).",
    )
    parser.add_argument(
        "--export-csv",
        type=str,
        default=None,
        help="Export per-case results to CSV.",
    )
    parser.add_argument(
        "--export-html",
        type=str,
        default=None,
        help="Export a self-contained HTML report.",
    )

    args = parser.parse_args()

    report = load_results(args.results_json)
    print_terminal_report(report)

    if args.export_csv:
        export_csv(report, args.export_csv)
    if args.export_html:
        export_html(report, args.export_html)


if __name__ == "__main__":
    main()