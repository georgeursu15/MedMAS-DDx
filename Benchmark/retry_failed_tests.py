#!/usr/bin/env python3
"""
Retry partial/failed tests from a MedMAS_DDx batch run.

What it does
────────────
1. Reads an existing batch.json (defaults to the most recent batch in
   ./eval_results/).
2. Collects every test whose status is "partial" or "failed".
3. Writes a temporary retry-config (a tests.json-style file) listing
   those configs.
4. Invokes the batch_runner inside the `app` service via
       docker compose -f <compose-file> run --rm \
           -v "<host>/retry_tests.json:/app/tests.json:ro" \
           app python -m MedMAS_DDx.evaluation.batch_runner \
           --config /app/tests.json --batch-id <new-batch-id>
   (the --batch-id is generated ahead of time so we know where the
   results will land).
5. When the retry batch finishes, merges every retried test result back
   into the original batch.json (status/error/started_at/completed_at/
   summary/cases all replaced) and recomputes the global counters.
6. Copies each per-test log from the retry batch directory into the
   original batch directory, renamed to `{test_id}_retry{N}.txt`
   (`N` auto-increments so multiple retry passes don't collide).

Usage
─────
    # ── Foreground (must keep ssh alive) ────────────────────────────────────
    python retry_failed_tests.py
    python retry_failed_tests.py --batch-json eval_results/batch_xxx/batch.json
    python retry_failed_tests.py --dry-run
    python retry_failed_tests.py --statuses failed

    # ── Background / ssh-disconnect safe ────────────────────────────────────
    # 1) Kick off in detached docker (script exits immediately; container
    #    keeps running independently — no SIGHUP on ssh disconnect).
    python retry_failed_tests.py --detach
    # → prints container name + retry batch id, writes .retry_state.json

    # 2a) Later, peek at status without blocking:
    python retry_failed_tests.py --resume

    # 2b) …or block until the container exits, then merge results:
    python retry_failed_tests.py --resume --wait

    # Other options
    python retry_failed_tests.py --compose-file docker-compose.local.yml
    python retry_failed_tests.py --detach --force   # overwrite stale state file
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# ── Defaults ──────────────────────────────────────────────────────────────────

PROJECT_ROOT       = Path(__file__).resolve().parent.parent
DEFAULT_EVAL_DIR   = PROJECT_ROOT / "eval_results"
DEFAULT_COMPOSE    = "docker-compose.dgx.yml"
DEFAULT_SERVICE    = "app"
DEFAULT_STATE_FILE = PROJECT_ROOT / ".retry_state.json"
RETRY_STATUSES     = {"partial", "failed"}

# ── Small helpers ─────────────────────────────────────────────────────────────

def _log(msg: str) -> None:
    print(f"[retry] {msg}", flush=True)


def _read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except PermissionError as exc:
        # Common cause: docker container wrote files as root, host user can't
        # overwrite them. Give a concrete hint instead of a raw traceback.
        _log(f"ERROR: permission denied writing {path}")
        _log(f"  ({exc})")
        _log("")
        _log("Likely cause: files in eval_results/ are owned by root because the")
        _log("docker container writes as root. Fix with:")
        _log("    sudo chown -R \"$USER:$USER\" eval_results/")
        _log("Then re-run the same command (the state file is preserved).")
        raise SystemExit(3) from exc


def _find_latest_batch_json(eval_dir: Path) -> Path | None:
    """Return the newest eval_results/batch_*/batch.json (or None)."""
    if not eval_dir.exists():
        return None
    candidates = sorted(
        (p for p in eval_dir.glob("batch_*/batch.json") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _next_retry_suffix(batch_dir: Path, test_id: str) -> str:
    """`_retry1`, `_retry2`, … — first one that doesn't already exist."""
    n = 1
    while (batch_dir / f"{test_id}_retry{n}.txt").exists():
        n += 1
    return f"_retry{n}"


def _make_retry_batch_id() -> str:
    return f"batch_retry_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


# ── Retry config construction ────────────────────────────────────────────────

def _collect_retry_targets(
    batch: dict,
    statuses: set[str],
) -> list[dict[str, Any]]:
    """Return [{test_id, prognosis, variant, limit}, …] for each retry candidate."""
    targets: list[dict[str, Any]] = []
    for t in batch.get("tests", []):
        if t.get("status") in statuses:
            targets.append({
                "test_id":   t["test_id"],
                "prognosis": t.get("prognosis_filter"),
                "variant":   t.get("variant_filter"),
                "limit":     t.get("limit"),
                "old_status": t.get("status"),
                "old_error":  t.get("error"),
            })
    return targets


def _write_retry_config(path: Path, targets: list[dict[str, Any]]) -> None:
    """Write the tests.json-style file the batch_runner consumes.

    Keeps prognosis/variant even when null (batch_runner handles `null`),
    and only adds `limit` when explicitly set.
    """
    cfg = [
        {
            "prognosis": t["prognosis"],
            "variant":   t["variant"],
            **({"limit": t["limit"]} if t["limit"] is not None else {}),
        }
        for t in targets
    ]
    _write_json(path, cfg)


# ── Docker compose invocation ────────────────────────────────────────────────

def _run_docker_compose_batch(
    *,
    compose_file: str,
    service: str,
    retry_config_host: Path,
    retry_batch_id: str,
    extra_compose_args: list[str] | None = None,
) -> int:
    """Run the batch_runner via `docker compose run --rm`.

    Streams stdout/stderr live, returns the container's exit code.
    """
    cmd = [
        "docker", "compose", "-f", compose_file, "run", "--rm",
        "-v", f"{retry_config_host}:/app/tests.json:ro",
    ]
    if extra_compose_args:
        cmd.extend(extra_compose_args)
    cmd += [
        service,
        "test",
        "--config",   "/app/tests.json",
        "--batch-id", retry_batch_id,
    ]

    _log("Running: " + " ".join(cmd))
    return subprocess.call(cmd)


def _run_docker_compose_batch_detached(
    *,
    compose_file: str,
    service: str,
    retry_config_host: Path,
    retry_batch_id: str,
) -> str:
    """Run the batch_runner detached via `docker compose run -d`.

    Returns the container name printed by docker compose on stdout.

    NOTE: We intentionally don't use `--rm` here. Without `--rm`, the container
    sticks around in `exited` state after it finishes, which lets `--resume`
    inspect the exit code and read logs even if we connect back hours later.
    The resume step removes it explicitly once results are merged.
    """
    cmd = [
        "docker", "compose", "-f", compose_file, "run", "-d",
        "-v", f"{retry_config_host}:/app/tests.json:ro",
        service,
        "test",
        "--config",   "/app/tests.json",
        "--batch-id", retry_batch_id,
    ]
    _log("Running (detached): " + " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        raise RuntimeError(f"docker compose run -d failed with code {result.returncode}")
    # docker compose prints progress on stderr; the container name lands on
    # stdout (sometimes preceded by `Creating …` lines, hence the last-line pick).
    out = (result.stdout or "").strip().splitlines()
    if not out:
        raise RuntimeError("docker compose run -d produced no output")
    return out[-1].strip()


def _container_state(name: str) -> tuple[str | None, int | None]:
    """Return (status, exit_code) via `docker inspect`.

    status is one of: "created", "running", "paused", "restarting", "removing",
    "exited", "dead". Returns (None, None) if the container no longer exists.
    """
    result = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Status}}|{{.State.ExitCode}}", name],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None, None
    out = result.stdout.strip()
    if "|" not in out:
        return None, None
    status, code = out.split("|", 1)
    try:
        return status or None, int(code)
    except ValueError:
        return status or None, None


def _docker_wait(name: str) -> int:
    """Block until `name` exits; return its exit code (0 if we can't read it)."""
    result = subprocess.run(
        ["docker", "wait", name],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return 0
    try:
        return int(result.stdout.strip())
    except ValueError:
        return 0


def _docker_rm(name: str) -> None:
    """Best-effort `docker rm` (ignores errors — container may already be gone)."""
    subprocess.run(
        ["docker", "rm", "-f", name],
        capture_output=True, text=True,
    )


def _docker_chown_eval_results(eval_dir: Path) -> int:
    """`chown -R $UID:$GID eval_results/` via a throwaway docker container.

    Useful when the host user isn't in sudoers but docker writes root-owned
    files. Tries `alpine` first; falls back to `busybox` and to the project's
    own app image so it works even without an internet connection.
    """
    uid = os.getuid() if hasattr(os, "getuid") else 0
    gid = os.getgid() if hasattr(os, "getgid") else 0
    target = f"{uid}:{gid}"
    mount = f"{eval_dir}:/work"
    candidates = [
        ["docker", "run", "--rm", "-v", mount, "alpine",
         "chown", "-R", target, "/work"],
        ["docker", "run", "--rm", "-v", mount, "busybox",
         "chown", "-R", target, "/work"],
        ["docker", "run", "--rm", "--entrypoint", "chown",
         "-v", mount, "MedMAS_DDx-app-v2:latest",
         "-R", target, "/work"],
    ]
    for cmd in candidates:
        _log("Running: " + " ".join(cmd))
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            _log(f"Fixed ownership of {eval_dir} → {target}")
            return 0
        _log(f"  (failed with rc={result.returncode}: {result.stderr.strip()[:200]})")
    _log("ERROR: none of the chown fallbacks worked. "
         "Make sure docker is running and you have at least one of "
         "`alpine`, `busybox`, or `MedMAS_DDx-app-v2:latest` available.")
    return 1


# ── Merge retry results back into the original batch ─────────────────────────

def _merge_results(
    *,
    original_batch_path: Path,
    retry_batch_path: Path,
    targets: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Replace each retried test entry in the original batch.json with the
    fresh entry from the retry batch.json.

    Returns a report dict: {test_id: {old_status, new_status, …}}.
    """
    original = _read_json(original_batch_path)
    retry    = _read_json(retry_batch_path)
    retry_by_id = {t["test_id"]: t for t in retry.get("tests", [])}

    report: dict[str, dict[str, Any]] = {}
    tests = original.get("tests", [])

    for target in targets:
        tid = target["test_id"]
        new_entry = retry_by_id.get(tid)
        if new_entry is None:
            report[tid] = {
                "old_status": target["old_status"],
                "new_status": "missing_in_retry",
                "note": "no matching test_id in retry batch.json",
            }
            continue

        # Replace the test entry in-place (preserve list order)
        for i, t in enumerate(tests):
            if t["test_id"] == tid:
                # Keep the original test_id and its identifying fields, but
                # adopt everything else from the retry run.
                merged = {**t, **{
                    "status":       new_entry.get("status"),
                    "error":        new_entry.get("error"),
                    "started_at":   new_entry.get("started_at"),
                    "completed_at": new_entry.get("completed_at"),
                    "total_cases":  new_entry.get("total_cases", 0),
                    "summary":      new_entry.get("summary", {}),
                    "cases":        new_entry.get("cases", []),
                }}
                tests[i] = merged
                report[tid] = {
                    "old_status": target["old_status"],
                    "new_status": new_entry.get("status"),
                    "old_error":  target["old_error"],
                    "new_error":  new_entry.get("error"),
                }
                break

    # ── Recompute meta counters (mirrors batch_runner._update_test_entry) ─────
    ok_count      = sum(1 for t in tests if t.get("status") == "ok")
    failed_count  = sum(1 for t in tests if t.get("status") == "failed")
    partial_count = sum(1 for t in tests if t.get("status") == "partial")
    pending_count = sum(1 for t in tests if t.get("status") in ("pending", "running"))
    total         = original["meta"].get("total_tests", len(tests))

    original["meta"]["completed_tests"] = ok_count
    original["meta"]["failed_tests"]    = failed_count
    original["meta"]["pending_tests"]   = pending_count

    done = ok_count + failed_count + partial_count
    if pending_count == 0 and done >= total:
        if failed_count > 0 or partial_count > 0:
            original["meta"]["status"] = "partial"
        else:
            original["meta"]["status"] = "complete"
        original["meta"]["timestamp_end"] = datetime.now().isoformat()
    else:
        original["meta"]["status"] = "in_progress"

    # ── Best-effort: recompute the global summary using eval_runner ───────────
    try:
        sys.path.insert(0, str(PROJECT_ROOT / "src"))
        from MedMAS_DDx.evaluation.eval_runner import _build_summary  # type: ignore
        ok_cases: list[dict[str, Any]] = []
        for t in tests:
            if t.get("status") == "ok":
                ok_cases.extend(t.get("cases", []))
        if ok_cases:
            original["summary"] = _build_summary(ok_cases)
    except Exception as exc:
        _log(f"(warn) couldn't recompute global summary locally: {exc}")

    _write_json(original_batch_path, original)
    return report


def _copy_renamed_logs(
    *,
    original_batch_dir: Path,
    retry_batch_dir: Path,
    targets: list[dict[str, Any]],
) -> dict[str, str]:
    """
    Copy each `{test_id}.txt` from the retry batch dir into the original
    batch dir as `{test_id}_retryN.txt`. Returns {test_id: new_filename}.
    """
    moved: dict[str, str] = {}
    for target in targets:
        tid = target["test_id"]
        src = retry_batch_dir / f"{tid}.txt"
        if not src.exists():
            _log(f"(warn) no log file found at {src}")
            continue
        suffix = _next_retry_suffix(original_batch_dir, tid)
        dst = original_batch_dir / f"{tid}{suffix}.txt"
        shutil.copy2(src, dst)
        moved[tid] = dst.name
    return moved


# ── Shared finalize step (used by both foreground and --resume) ───────────────

def _finalize(
    *,
    original_batch_path: Path,
    original_batch_dir: Path,
    retry_batch_id: str,
    retry_batch_dir: Path,
    targets: list[dict[str, Any]],
    retry_config_path: Path,
    keep_retry_config: bool,
) -> int:
    """Merge retry results into batch.json, copy logs, print summary."""
    retry_batch_json = retry_batch_dir / "batch.json"
    if not retry_batch_json.exists():
        _log(f"ERROR: retry batch.json not found at {retry_batch_json}")
        return 2

    report = _merge_results(
        original_batch_path=original_batch_path,
        retry_batch_path=retry_batch_json,
        targets=targets,
    )

    moved = _copy_renamed_logs(
        original_batch_dir=original_batch_dir,
        retry_batch_dir=retry_batch_dir,
        targets=targets,
    )

    _log("─" * 70)
    _log("Retry summary:")
    n_ok = n_partial = n_failed = n_missing = 0
    for tid, info in report.items():
        new = info["new_status"]
        if   new == "ok":      n_ok      += 1
        elif new == "partial": n_partial += 1
        elif new == "failed":  n_failed  += 1
        else:                  n_missing += 1
        log_name = moved.get(tid, "<no log copied>")
        _log(
            f"  • {tid:<35} {info['old_status']:<7} → {str(new):<8}  "
            f"log: {log_name}"
        )
        if new == "failed" and info.get("new_error"):
            _log(f"      new error: {info['new_error']}")

    _log("─" * 70)
    _log(f"Recovered     : {n_ok}/{len(targets)}")
    _log(f"Still partial : {n_partial}")
    _log(f"Still failed  : {n_failed}")
    if n_missing:
        _log(f"Missing in retry batch.json : {n_missing}")
    _log(f"Updated batch.json : {original_batch_path}")

    if not keep_retry_config:
        try:
            retry_config_path.unlink(missing_ok=True)
            _log(f"Removed temp retry config {retry_config_path}")
        except Exception as exc:
            _log(f"(warn) couldn't remove {retry_config_path}: {exc}")

    return 0 if (n_ok + n_partial) > 0 or n_failed == 0 else 1


# ── Main ──────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--batch-json",
        default=None,
        help="Path to the original batch.json. "
             "Defaults to the most recent batch in ./eval_results/.",
    )
    parser.add_argument(
        "--compose-file",
        default=DEFAULT_COMPOSE,
        help=f"Docker compose file (default: {DEFAULT_COMPOSE}).",
    )
    parser.add_argument(
        "--service",
        default=DEFAULT_SERVICE,
        help=f"Compose service to run (default: {DEFAULT_SERVICE}).",
    )
    parser.add_argument(
        "--statuses",
        nargs="+",
        default=sorted(RETRY_STATUSES),
        choices=["partial", "failed"],
        help="Which statuses to retry (default: partial failed).",
    )
    parser.add_argument(
        "--retry-config",
        default=str(PROJECT_ROOT / "retry_tests.json"),
        help="Where to write the temp tests.json the retry batch reads "
             "(default: ./retry_tests.json).",
    )
    parser.add_argument(
        "--retry-batch-id",
        default=None,
        help="Batch ID to use for the retry run "
             "(default: batch_retry_<YYYYMMDD_HHMMSS>).",
    )
    parser.add_argument(
        "--keep-retry-config",
        action="store_true",
        help="Don't delete the retry_tests.json file when done.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be retried, don't run docker.",
    )

    # ── Detach / resume ───────────────────────────────────────────────────────
    parser.add_argument(
        "--detach", action="store_true",
        help="Start the retry batch via `docker compose run -d` and exit "
             "immediately. ssh-disconnect safe. Use `--resume` later.",
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="Resume a previous --detach run: read state file, optionally wait "
             "for the container, merge results, copy renamed logs.",
    )
    parser.add_argument(
        "--wait", action="store_true",
        help="With --resume: block until the container exits (via `docker wait`) "
             "instead of giving up if it's still running.",
    )
    parser.add_argument(
        "--state-file",
        default=str(DEFAULT_STATE_FILE),
        help=f"Where to store the detach handoff state "
             f"(default: {DEFAULT_STATE_FILE.name}).",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="With --detach: overwrite an existing state file instead of refusing.",
    )
    parser.add_argument(
        "--fix-perms", action="store_true",
        help="Before merging, run `chown -R $UID:$GID eval_results/` via a "
             "throwaway docker container — useful when docker wrote files as "
             "root and you don't have sudo on the host.",
    )
    return parser


def _mode_resume(args) -> int:
    """`--resume`: pick up a previously detached run, merge results."""
    state_path = Path(args.state_file).resolve()
    if not state_path.exists():
        _log(f"ERROR: no state file at {state_path}. "
             "Run `--detach` first, or pass `--state-file PATH`.")
        return 1

    state = _read_json(state_path)
    container_name    = state["container_name"]
    retry_batch_id    = state["retry_batch_id"]
    retry_batch_dir   = Path(state["retry_batch_dir"])
    original_path     = Path(state["original_batch_path"])
    original_dir      = original_path.parent
    targets           = state["targets"]
    retry_config_path = Path(state["retry_config_path"])
    keep_retry_config = args.keep_retry_config or state.get("keep_retry_config", False)

    _log(f"Resuming retry batch {retry_batch_id}")
    _log(f"Container        : {container_name}")
    _log(f"Original batch   : {original_path}")
    _log(f"Retry batch dir  : {retry_batch_dir}")

    if args.fix_perms:
        _docker_chown_eval_results(DEFAULT_EVAL_DIR)

    status, exit_code = _container_state(container_name)
    if status is None:
        # Container is gone — must have already finished and been cleaned up.
        # Proceed to merge whatever's on disk.
        _log("Container no longer exists (already cleaned up). "
             "Merging results from disk.")
    elif status == "running":
        if not args.wait:
            _log(f"Container is still running. Re-run with `--resume --wait` "
                 f"to block, or peek with `docker logs {container_name}`.")
            return 0
        _log(f"Container still running — blocking on `docker wait {container_name}` …")
        exit_code = _docker_wait(container_name)
        status = "exited"

    if status == "exited":
        _log(f"Container exited with code {exit_code}.")
        if exit_code != 0:
            _log("(warn) non-zero exit — merging whatever landed on disk anyway.")
    elif status not in (None, "exited"):
        _log(f"(warn) unexpected container status: {status}")

    rc = _finalize(
        original_batch_path=original_path,
        original_batch_dir=original_dir,
        retry_batch_id=retry_batch_id,
        retry_batch_dir=retry_batch_dir,
        targets=targets,
        retry_config_path=retry_config_path,
        keep_retry_config=keep_retry_config,
    )

    # Clean up container (best-effort) and state file
    if status is not None:
        _docker_rm(container_name)
    try:
        state_path.unlink(missing_ok=True)
        _log(f"Removed state file {state_path}")
    except Exception as exc:
        _log(f"(warn) couldn't remove state file {state_path}: {exc}")

    return rc


def _mode_normal_or_detach(args) -> int:
    """Foreground (default) or `--detach` mode — they share most of the setup."""
    # ── Locate batch.json ─────────────────────────────────────────────────────
    if args.batch_json:
        batch_path = Path(args.batch_json).resolve()
    else:
        latest = _find_latest_batch_json(DEFAULT_EVAL_DIR)
        if not latest:
            _log(f"ERROR: no batch.json found under {DEFAULT_EVAL_DIR}")
            return 1
        batch_path = latest
        _log(f"Auto-detected latest batch.json: {batch_path}")

    if not batch_path.exists():
        _log(f"ERROR: batch.json not found: {batch_path}")
        return 1

    batch_dir = batch_path.parent
    batch     = _read_json(batch_path)

    # ── Collect retry targets ─────────────────────────────────────────────────
    statuses = set(args.statuses)
    targets  = _collect_retry_targets(batch, statuses)

    if not targets:
        _log(f"No tests with status in {sorted(statuses)} — nothing to retry.")
        return 0

    _log(f"Found {len(targets)} test(s) to retry:")
    for t in targets:
        _log(
            f"  • {t['test_id']:<35} "
            f"[{t['old_status']:<7}]  "
            f"prognosis={t['prognosis']!r}, variant={t['variant']!r}, limit={t['limit']!r}"
        )
        if t["old_error"]:
            _log(f"      old error: {t['old_error']}")

    if args.dry_run:
        _log("Dry-run — exiting before docker compose.")
        return 0

    # ── Refuse to clobber an in-flight detach unless --force ──────────────────
    state_path = Path(args.state_file).resolve()
    if args.detach and state_path.exists() and not args.force:
        _log(f"ERROR: state file already exists at {state_path}.")
        _log("Run `--resume` to handle the previous detach, or pass `--force` to overwrite.")
        return 1

    # ── Write retry config ────────────────────────────────────────────────────
    retry_config_path = Path(args.retry_config).resolve()
    _write_retry_config(retry_config_path, targets)
    _log(f"Wrote retry config → {retry_config_path}")

    retry_batch_id  = args.retry_batch_id or _make_retry_batch_id()
    retry_batch_dir = DEFAULT_EVAL_DIR / retry_batch_id
    _log(f"Retry batch ID  : {retry_batch_id}")
    _log(f"Retry batch dir : {retry_batch_dir}")

    # ── Detach branch ─────────────────────────────────────────────────────────
    if args.detach:
        try:
            container_name = _run_docker_compose_batch_detached(
                compose_file=args.compose_file,
                service=args.service,
                retry_config_host=retry_config_path,
                retry_batch_id=retry_batch_id,
            )
        except Exception as exc:
            _log(f"ERROR: failed to start detached container: {exc}")
            return 1

        state = {
            "container_name":      container_name,
            "retry_batch_id":      retry_batch_id,
            "retry_batch_dir":     str(retry_batch_dir),
            "original_batch_path": str(batch_path),
            "retry_config_path":   str(retry_config_path),
            "compose_file":        args.compose_file,
            "service":             args.service,
            "targets":             targets,
            "keep_retry_config":   args.keep_retry_config,
            "created_at":          datetime.now().isoformat(),
        }
        _write_json(state_path, state)

        _log("─" * 70)
        _log(f"Detached. Container: {container_name}")
        _log(f"State file        : {state_path}")
        _log("ssh disconnect is safe — the container keeps running.")
        _log("")
        _log("When done, finish with:")
        _log(f"  python3 {Path(__file__).name} --resume          # peek / merge if exited")
        _log(f"  python3 {Path(__file__).name} --resume --wait   # block then merge")
        _log("")
        _log("Live logs:")
        _log(f"  docker logs -f {container_name}")
        return 0

    # ── Foreground branch (original behaviour) ────────────────────────────────
    rc = _run_docker_compose_batch(
        compose_file=args.compose_file,
        service=args.service,
        retry_config_host=retry_config_path,
        retry_batch_id=retry_batch_id,
    )
    if rc != 0:
        _log(f"(warn) docker compose exited with code {rc} — "
             "merging whatever results landed on disk anyway.")

    if args.fix_perms:
        _docker_chown_eval_results(DEFAULT_EVAL_DIR)

    return _finalize(
        original_batch_path=batch_path,
        original_batch_dir=batch_dir,
        retry_batch_id=retry_batch_id,
        retry_batch_dir=retry_batch_dir,
        targets=targets,
        retry_config_path=retry_config_path,
        keep_retry_config=args.keep_retry_config,
    )


def main() -> int:
    args = _build_parser().parse_args()

    if args.detach and args.resume:
        _log("ERROR: --detach and --resume are mutually exclusive.")
        return 1

    if args.resume:
        return _mode_resume(args)
    return _mode_normal_or_detach(args)


if __name__ == "__main__":
    raise SystemExit(main())
