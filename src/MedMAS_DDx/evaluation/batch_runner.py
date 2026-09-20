#!/usr/bin/env python


from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from MedMAS_DDx.tools.refine_scoring_tool import resolve_weights


DEFAULT_CSV = Path(__file__).resolve().parents[3] / "Benchmark" / "PatientCases.csv"
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[3] / "eval_results"
_ALL_VARIANTS: list[str] = ["100", "80", "50"]
BATCH_FILENAME = "batch.json"



def _make_test_id(prognosis: str | None, variant: str | None, idx: int) -> str:
   
    parts: list[str] = []
    if prognosis:
        slug = re.sub(r"[^a-z0-9]+", "_", prognosis.strip().lower()).strip("_")
        parts.append(slug)
    if variant:
        parts.append(f"{variant}pct")
    if not parts:
        parts.append(f"test_{idx + 1}")
    return "_".join(parts)


def _assign_test_ids(configs: list[dict[str, Any]]) -> list[str]:
    seen: dict[str, int] = {}
    out: list[str] = []
    for idx, cfg in enumerate(configs):
        base = _make_test_id(cfg.get("prognosis"), cfg.get("variant"), idx)
        count = seen.get(base, 0)
        out.append(f"{base}_{count + 1}" if count > 0 else base)
        seen[base] = count + 1
    return out


def _write_batch_file(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _read_batch_file(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _create_batch_file(
    batch_path: Path,
    batch_id: str,
    configs: list[dict[str, Any]],
    test_ids: list[str],
    csv_path: str,
) -> dict[str, Any]:
    tests: list[dict[str, Any]] = []
    for cfg, tid in zip(configs, test_ids):
        tests.append({
            "test_id":          tid,
            "prognosis_filter": cfg.get("prognosis"),
            "variant_filter":   cfg.get("variant"),
            "limit":            cfg.get("limit"),
            "status":           "pending",
            "error":            None,
            "started_at":       None,
            "completed_at":     None,
            "total_cases":      0,
            "summary":          {},
            "cases":            [],
        })

    _alpha, _beta = resolve_weights()
    batch_data: dict[str, Any] = {
        "format": "batch_v2",
        "meta": {
            "batch_id":        batch_id,
            "timestamp":       datetime.now().isoformat(),
            "timestamp_end":   None,
            "csv_path":        csv_path,
            "status":          "in_progress",
            "total_tests":     len(tests),
            "completed_tests": 0,
            "failed_tests":    0,
            "pending_tests":   len(tests),
            "refine_support_weight": _alpha,
            "refine_conflict_weight": _beta,
        },
        "summary": {},
        "tests":   tests,
    }
    _write_batch_file(batch_path, batch_data)
    return batch_data


def _recompute_global_summary(tests: list[dict[str, Any]]) -> dict[str, Any]:
   
    from MedMAS_DDx.evaluation.eval_runner import _build_summary  # noqa: WPS433

    ok_cases: list[dict[str, Any]] = []
    for t in tests:
        if t.get("status") == "ok":
            ok_cases.extend(t.get("cases", []))
    return _build_summary(ok_cases) if ok_cases else {}


def _update_test_entry(
    batch_path: Path,
    test_id: str,
    *,
    status: str,
    error: str | None = None,
    started_at: str | None = None,
    completed_at: str | None = None,
    test_result: dict[str, Any] | None = None,
) -> None:
    
    batch = _read_batch_file(batch_path)
    if not batch:
        return

    for test in batch.get("tests", []):
        if test["test_id"] != test_id:
            continue
        test["status"] = status
        if error is not None:
            test["error"] = error
        if started_at is not None:
            test["started_at"] = started_at
        if completed_at is not None:
            test["completed_at"] = completed_at
        if test_result is not None:
            test["cases"]       = test_result.get("cases", [])
            test["summary"]     = test_result.get("summary", {})
            test["total_cases"] = len(test["cases"])
            if status == "ok":
                test["error"] = None
        break

    tests        = batch.get("tests", [])
    ok_count     = sum(1 for t in tests if t["status"] == "ok")
    failed_count = sum(1 for t in tests if t["status"] == "failed")
    pending_count = sum(1 for t in tests if t["status"] in ("pending", "running"))

    batch["meta"]["completed_tests"] = ok_count
    batch["meta"]["failed_tests"]    = failed_count
    batch["meta"]["pending_tests"]   = pending_count

    total = batch["meta"]["total_tests"]
    done  = ok_count + failed_count
    if done >= total:
        batch["meta"]["status"]        = "partial" if failed_count > 0 else "complete"
        batch["meta"]["timestamp_end"] = datetime.now().isoformat()
    else:
        batch["meta"]["status"] = "in_progress"

    try:
        batch["summary"] = _recompute_global_summary(tests)
    except Exception:
        pass  

    _write_batch_file(batch_path, batch)



def _expand_configs(configs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    
    expanded: list[dict[str, Any]] = []
    for c in configs:
        if not c.get("variant"):
            for v in _ALL_VARIANTS:
                expanded.append({**c, "variant": v})
        else:
            expanded.append(c)
    return expanded


def _make_batch_id() -> str:
    return f"batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


def _test_label(cfg: dict[str, Any]) -> str:
    prog = cfg.get("prognosis") or "all"
    var  = cfg.get("variant") or "all"
    lim  = cfg.get("limit")
    s = f"{prog}  variant={var}"
    if lim:
        s += f"  limit={lim}"
    if cfg.get("patient_llm"):
        model = cfg.get("patient_llm_model") or "auto"
        s += f"  patient_llm={model}"
    return s


def _resolve_patient_llm(
    cfg: dict[str, Any],
    *,
    global_enabled: bool,
    global_model: str | None,
) -> tuple[bool, str | None]:
  
    if "patient_llm" in cfg and cfg["patient_llm"] is not None:
        enabled = bool(cfg["patient_llm"])
    else:
        enabled = global_enabled
    model = cfg.get("patient_llm_model") or global_model
    return enabled, model



def run_batch(
    configs: list[dict[str, Any]],
    batch_id: str | None = None,
    csv_path: str | Path | None = None,
    dry_run: bool = False,
    *,
    use_patient_llm: bool = False,
    patient_llm_model: str | None = None,
) -> int:
  
    csv_path = str(csv_path or DEFAULT_CSV)
    batch_id = batch_id or _make_batch_id()
    batch_dir = DEFAULT_OUTPUT_DIR / batch_id
    batch_path = batch_dir / BATCH_FILENAME
    log_path  = batch_dir / "run.txt"

    configs = _expand_configs(configs)
    total   = len(configs)

    if total == 0:
        print("[batch] No configs to run.")
        return 0

    test_ids = _assign_test_ids(configs)

    print(f"\n{'='*70}")
    print(f"  MedMAS_DDx Batch Runner")
    print(f"  Batch ID  : {batch_id}")
    print(f"  Output    : {batch_dir}")
    print(f"  Batch file: {batch_path.name}")
    print(f"  Tests     : {total}")
    if use_patient_llm:
        print(f"  Patient LLM (default ON): {patient_llm_model or 'auto'}")
    else:
        print(f"  Patient LLM (default OFF) — enable per-test via "
              f"\"patient_llm\": true, or pass --patient-llm")
    print(f"  Started   : {datetime.now().isoformat()}")
    print(f"{'='*70}")

    for i, (cfg, tid) in enumerate(zip(configs, test_ids), start=1):
        print(f"\n  [{i}/{total}] {_test_label(cfg)}  →  {tid}")

    if dry_run:
        print("\n[batch] Dry-run mode — nothing executed.")
        return 0

    batch_dir.mkdir(parents=True, exist_ok=True)
    _create_batch_file(batch_path, batch_id, configs, test_ids, csv_path)

    failures: list[int] = []

    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"▶ Running {total} test(s) → batch {batch_id}\n\n")
        log.flush()

        for i, (cfg, test_id) in enumerate(zip(configs, test_ids), start=1):
            prognosis = cfg.get("prognosis")
            variant   = cfg.get("variant")
            limit     = cfg.get("limit")

       
            out_log_path = batch_dir / f"{test_id}.txt"
            tmp_path     = batch_dir / f"_tmp_{test_id}.json"

            cmd = [sys.executable, "-m", "MedMAS_DDx.evaluation.eval_runner"]
            if prognosis:
                cmd += ["--prognosis", prognosis]
            if variant:
                cmd += ["--variant", variant]
            if limit is not None:
                cmd += ["--limit", str(limit)]

            test_patient_llm, test_patient_model = _resolve_patient_llm(
                cfg,
                global_enabled=use_patient_llm,
                global_model=patient_llm_model,
            )
            if test_patient_llm:
                cmd += ["--patient-llm"]
                if test_patient_model:
                    cmd += ["--patient-llm-model", test_patient_model]

            cmd += [
                "--csv",      csv_path,
                "--batch-id", batch_id,
                "--output",   str(tmp_path),
            ]

            label = _test_label(cfg)
            print(f"\n{'─'*70}")
            print(f"  [{i}/{total}] {label}")
            print(f"  Test ID → {test_id}")
            print(f"  Log     → {out_log_path.name}")
            print(f"{'─'*70}")
            log.write(f"── [{i}/{total}] Starting: {test_id}\n")
            log.flush()

            started_at = datetime.now().isoformat()
            _update_test_entry(
                batch_path, test_id,
                status="running", started_at=started_at,
            )

            with out_log_path.open("w", encoding="utf-8") as test_log:
                result = subprocess.run(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                test_log.write(result.stdout or "")

            if result.stdout:
                print(result.stdout, end="")

            rc           = result.returncode
            completed_at = datetime.now().isoformat()

            test_failed = False
            if rc == 0 and tmp_path.exists():
                try:
                    test_result   = json.loads(tmp_path.read_text(encoding="utf-8"))
                    test_summary  = test_result.get("summary", {})
                    total_eval    = test_summary.get("total_evaluated", 0)
                    total_err     = test_summary.get("total_errors", 0)
                    total_unavail = test_summary.get("total_api_unavailable", 0)

                    if total_eval == 0:
                        parts: list[str] = []
                        if total_err:
                            parts.append(f"{total_err} case error(s)")
                        if total_unavail:
                            parts.append(f"{total_unavail} API unavailable")
                        fail_reason = "; ".join(parts) or "all cases failed"
                        _update_test_entry(
                            batch_path, test_id,
                            status="failed", completed_at=completed_at,
                            test_result=test_result, error=fail_reason,
                        )
                        test_failed = True
                    elif total_err > 0 or total_unavail > 0:
                        parts = []
                        if total_err:
                            parts.append(f"{total_err} case error(s)")
                        if total_unavail:
                            parts.append(f"{total_unavail} API unavailable")
                        partial_reason = "; ".join(parts)
                        _update_test_entry(
                            batch_path, test_id,
                            status="partial", completed_at=completed_at,
                            test_result=test_result, error=partial_reason,
                        )
                    else:
                        _update_test_entry(
                            batch_path, test_id,
                            status="ok", completed_at=completed_at,
                            test_result=test_result,
                        )
                except Exception as exc:
                    _update_test_entry(
                        batch_path, test_id,
                        status="failed", completed_at=completed_at,
                        error=f"merge error: {exc}",
                    )
                    test_failed = True
                finally:
                    try:
                        tmp_path.unlink(missing_ok=True)
                    except Exception:
                        pass
            else:
                fail_reason = (
                    f"process exited with code {rc}"
                    if rc != 0
                    else "output file not produced (process exited 0)"
                )
                _update_test_entry(
                    batch_path, test_id,
                    status="failed", completed_at=completed_at,
                    error=fail_reason,
                )
                test_failed = True

            status_str = "✓ done" if rc == 0 and not test_failed else f"✗ FAILED (rc={rc})"
            print(f"\n  {status_str}")
            log.write(f"── [{i}/{total}] {status_str}: {test_id}\n")
            log.flush()

            if rc != 0 or test_failed:
                failures.append(i)

        n_ok = total - len(failures)
        log.write(f"\n✓ Batch {batch_id} complete — {n_ok}/{total} test(s) succeeded.\n")

    print(f"\n{'='*70}")
    print(f"  Batch complete")
    print(f"  Batch ID : {batch_id}")
    print(f"  Results  : {batch_dir}/")
    print(f"  Batch    : {batch_path}")
    print(f"  Passed   : {total - len(failures)}/{total}")
    if failures:
        print(f"  Failed   : tests {failures}")
    print(f"{'='*70}\n")

    return len(failures)



def main() -> None:
    parser = argparse.ArgumentParser(
        description="MedMAS_DDx batch runner — replicates the frontend multi-run flow",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""

  python -m MedMAS_DDx.evaluation.batch_runner --config tests.json
  python -m MedMAS_DDx.evaluation.batch_runner --config tests.json --dry-run
  python -m MedMAS_DDx.evaluation.batch_runner --config tests.json --batch-id batch_20260517_120000
        """,
    )
    parser.add_argument(
        "--config",
        required=True,
        metavar="FILE",
        help="Path to a JSON file containing the list of test configs.",
    )
    parser.add_argument(
        "--batch-id",
        default=None,
        metavar="BATCH_ID",
        help="Batch folder name (auto-generated if omitted).",
    )
    parser.add_argument(
        "--csv",
        default=str(DEFAULT_CSV),
        help=f"Path to PatientCases.csv (default: {DEFAULT_CSV})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the expanded test list without running anything.",
    )
    parser.add_argument(
        "--patient-llm",
        action="store_true",
        help=(
            "Global default: use a simulated-patient LLM to answer follow-up "
            "questions during every test (instead of SYMP/HP synonym matching). "
            "Individual tests can opt out via {\"patient_llm\": false}."
        ),
    )
    parser.add_argument(
        "--patient-llm-model",
        type=str,
        default=None,
        metavar="MODEL",
        help=(
            "Global default model for the patient LLM (e.g. "
            "google/gemini-3.1-flash-lite-preview).  Individual tests can "
            "override via the \"patient_llm_model\" field."
        ),
    )

    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"[batch] ERROR: config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    with config_path.open("r", encoding="utf-8") as f:
        try:
            configs = json.load(f)
        except json.JSONDecodeError as exc:
            print(f"[batch] ERROR: invalid JSON in {config_path}: {exc}", file=sys.stderr)
            sys.exit(1)

    if not isinstance(configs, list):
        print("[batch] ERROR: config file must contain a JSON array.", file=sys.stderr)
        sys.exit(1)

    failures = run_batch(
        configs=configs,
        batch_id=args.batch_id,
        csv_path=args.csv,
        dry_run=args.dry_run,
        use_patient_llm=args.patient_llm,
        patient_llm_model=args.patient_llm_model,
    )
    sys.exit(0 if failures == 0 else 1)


if __name__ == "__main__":
    main()
