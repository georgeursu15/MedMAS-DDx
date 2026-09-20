

from dotenv import load_dotenv
load_dotenv() 

import asyncio
import csv
import json
import logging
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from MedMAS_DDx.main import run_diagnostic_pipeline
from MedMAS_DDx.pipeline.utils import PipelineLogger
from MedMAS_DDx.answer_collector import AsyncAnswerCollector

logger = logging.getLogger(__name__)

app = FastAPI(title="MedMAS_DDx Diagnostic API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_sessions: dict[str, dict[str, Any]] = {}


class DiagnoseRequest(BaseModel):
    user_message: str


class AnswerRequest(BaseModel):
    answer: str


def _session_response(session_id: str, status: str, message: str) -> dict[str, str]:
    return {
        "session_id": session_id,
        "status": status,
        "message": message,
    }


class SessionAnswerCollector(AsyncAnswerCollector):
    def __init__(self, session: dict[str, Any]) -> None:
        super().__init__()
        self._session = session

    async def on_extracted_symptoms(
        self, extracted_symptoms: list[dict[str, Any]]
    ) -> None:
        self._session["extracted_symptoms"] = extracted_symptoms

    async def on_initial_ranking(self, ranking_payload: dict[str, Any]) -> None:
        self._session["initial_ranking"] = ranking_payload



@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    html_path = Path(__file__).parent / "frontend.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))



@app.post("/api/diagnose")
async def diagnose(req: DiagnoseRequest):
    session_id = str(uuid.uuid4())

    _sessions[session_id] = {
        "collector": None,
        "task": None,
        "result": None,
        "error": None,
        "extracted_symptoms": None,
        "initial_ranking": None,
        "aborted": False,
    }
    collector = SessionAnswerCollector(_sessions[session_id])
    _sessions[session_id]["collector"] = collector

    async def _run():
        try:
            result = await run_diagnostic_pipeline(
                req.user_message,
                collector,
                log_file=PipelineLogger.build_runtime_log_file(),
            )
            _sessions[session_id]["result"] = result
        except asyncio.CancelledError:
            _sessions[session_id]["aborted"] = True
            _sessions[session_id]["error"] = "Consultation aborted by user."
            raise
        except Exception as e:
            _sessions[session_id]["error"] = str(e)

    _sessions[session_id]["task"] = asyncio.create_task(_run())
    return {"session_id": session_id}



@app.get("/api/questions/{session_id}")
async def stream_questions(session_id: str):
    session = _sessions.get(session_id)
    if not session:
        raise HTTPException(404, "Session not found")

    collector: AsyncAnswerCollector = session["collector"]

    async def event_generator():
        extracted_symptoms_sent = False
        initial_ranking_sent = False
        while True:
            if session.get("aborted"):
                yield f"data: {json.dumps({'aborted': True, 'message': 'Consultation aborted'})}\n\n"
                return

            if session["error"] is not None:
                yield f"data: {json.dumps({'error': session['error']})}\n\n"
                return

            if session.get("extracted_symptoms") is not None and not extracted_symptoms_sent:
                extracted_symptoms_sent = True
                yield f"data: {json.dumps({'extracted_symptoms': session['extracted_symptoms']})}\n\n"
                continue

            if session.get("initial_ranking") is not None and not initial_ranking_sent:
                initial_ranking_sent = True
                yield f"data: {json.dumps({'initial_ranking': session['initial_ranking']})}\n\n"
                continue

            if session["result"] is not None:
                yield f"data: {json.dumps({'done': True, 'result': session['result']})}\n\n"
                return

            try:
                question = await asyncio.wait_for(
                    collector.pending_questions.get(), timeout=2.0
                )
            except asyncio.TimeoutError:
                yield ": heartbeat\n\n"
                continue

            if question.get("done"):
                while session["result"] is None and session["error"] is None:
                    await asyncio.sleep(0.5)
                if session["error"]:
                    yield f"data: {json.dumps({'error': session['error']})}\n\n"
                else:
                    yield f"data: {json.dumps({'done': True, 'result': session['result']})}\n\n"
                return

            payload = dict(question)
            if question.get("index") == 1 and collector.investigations:
                payload["investigations"] = collector.investigations

            yield f"data: {json.dumps(payload)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")



@app.post("/api/answer/{session_id}")
async def submit_answer(session_id: str, req: AnswerRequest):
    session = _sessions.get(session_id)
    if not session:
        raise HTTPException(404, "Session not found")

    await session["collector"].submit_answer(req.answer)
    return {"status": "received"}



@app.post("/api/abort/{session_id}")
async def abort_session(session_id: str):
    session = _sessions.get(session_id)
    if not session:
        raise HTTPException(404, "Session not found")

    session["aborted"] = True
    task: asyncio.Task | None = session.get("task")
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    return _session_response(session_id, "aborted", "Consultation aborted")


# 
# Batch Evaluation Runner 

_eval_runs: dict[str, dict[str, Any]] = {}

_batch_locks: dict[str, asyncio.Lock] = {}

BATCH_FILENAME = "batch.json"


class EvalRunRequest(BaseModel):
    prognosis: str | None = None
    variant: str | None = None  
    limit: int | None = None
    csv_path: str | None = None
    batch_id: str | None = None  


class MultiEvalRunRequest(BaseModel):
    configs: list[EvalRunRequest]
    batch_id: str | None = None  


class RetryRunRequest(BaseModel):
    batch_id: str
    filename: str  
    retry_statuses: str = "api_unavailable,error"


class RetryTestRequest(BaseModel):
    batch_id: str
    test_id: str


class RetryCasesRequest(BaseModel):
    batch_id: str
    test_id: str
    case_ids: list[int]


def _resolve_csv_path(csv_path: str | None) -> Path:
    project_root = Path(__file__).resolve().parents[2]
    if not csv_path:
        return project_root / "Benchmark" / "PatientCases.csv"
    path = Path(csv_path)
    if not path.is_absolute():
        path = project_root / csv_path
    return path


def _make_batch_id() -> str:
    return f"batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}"



def _get_batch_lock(batch_id: str) -> asyncio.Lock:
    if batch_id not in _batch_locks:
        _batch_locks[batch_id] = asyncio.Lock()
    return _batch_locks[batch_id]


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


def _write_batch_file(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _read_batch_file(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Failed to read batch file: %s", path)
        return None


def _create_batch_file(
    batch_path: Path,
    batch_id: str,
    configs: list[dict[str, Any]],
    csv_path: str,
) -> dict[str, Any]:
  
    seen_ids: dict[str, int] = {}
    tests: list[dict[str, Any]] = []
    for idx, cfg in enumerate(configs):
        base_id = _make_test_id(cfg.get("prognosis"), cfg.get("variant"), idx)
        count = seen_ids.get(base_id, 0)
        test_id = f"{base_id}_{count + 1}" if count > 0 else base_id
        seen_ids[base_id] = count + 1

        tests.append({
            "test_id": test_id,
            "prognosis_filter": cfg.get("prognosis"),
            "variant_filter": cfg.get("variant"),
            "limit": cfg.get("limit"),
            "status": "pending",
            "error": None,
            "started_at": None,
            "completed_at": None,
            "total_cases": 0,
            "summary": {},
            "cases": [],
        })

    batch_data: dict[str, Any] = {
        "format": "batch_v2",
        "meta": {
            "batch_id": batch_id,
            "timestamp": datetime.now().isoformat(),
            "timestamp_end": None,
            "csv_path": csv_path,
            "status": "in_progress",
            "total_tests": len(tests),
            "completed_tests": 0,
            "failed_tests": 0,
            "pending_tests": len(tests),
        },
        "summary": {},
        "tests": tests,
    }
    _write_batch_file(batch_path, batch_data)
    return batch_data


def _recompute_global_summary(tests: list[dict[str, Any]]) -> dict[str, Any]:

    from MedMAS_DDx.evaluation.eval_runner import _build_summary 
    ok_cases: list[dict[str, Any]] = []
    for t in tests:
        if t.get("status") == "ok":
            ok_cases.extend(t.get("cases", []))
    return _build_summary(ok_cases) if ok_cases else {}


async def _update_test_entry(
    batch_path: Path,
    batch_id: str,
    test_id: str,
    *,
    status: str,
    error: str | None = None,
    started_at: str | None = None,
    completed_at: str | None = None,
    test_result: dict[str, Any] | None = None,
) -> None:
  
    lock = _get_batch_lock(batch_id)
    async with lock:
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
                test["cases"] = test_result.get("cases", [])
                test["summary"] = test_result.get("summary", {})
                test["total_cases"] = len(test["cases"])
                if status == "ok":
                    test["error"] = None
            break

        tests = batch.get("tests", [])
        ok_count     = sum(1 for t in tests if t["status"] == "ok")
        failed_count = sum(1 for t in tests if t["status"] == "failed")
        pending_count = sum(1 for t in tests if t["status"] in ("pending", "running"))

        batch["meta"]["completed_tests"] = ok_count
        batch["meta"]["failed_tests"]    = failed_count
        batch["meta"]["pending_tests"]   = pending_count

        total = batch["meta"]["total_tests"]
        done  = ok_count + failed_count
        if done >= total:
            batch["meta"]["status"] = "partial" if failed_count > 0 else "complete"
            batch["meta"]["timestamp_end"] = datetime.now().isoformat()
        else:
            batch["meta"]["status"] = "in_progress"

        try:
            batch["summary"] = _recompute_global_summary(tests)
        except Exception:
            logger.exception(
                "Failed to recompute global summary for batch %s", batch_id
            )

        _write_batch_file(batch_path, batch)



_TEST_MAX_ATTEMPTS  = 3  
_TEST_RETRY_PAUSE_S = 30  


async def _run_single_test_subprocess(
    run_id: str,
    batch_path: Path,
    batch_id: str,
    test_id: str,
    cmd: list[str],
    tmp_path: Path,
    project_root: Path,
    log_path: Path | None = None,
    line_prefix: str = "",
) -> int:

    started = datetime.now().isoformat()
    await _update_test_entry(
        batch_path, batch_id, test_id,
        status="running", started_at=started,
    )

    log_file = None
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = log_path.open("w", encoding="utf-8")

    try:
        for attempt in range(1, _TEST_MAX_ATTEMPTS + 1):
            is_last = attempt == _TEST_MAX_ATTEMPTS

            if attempt > 1:
                msg = (
                    f"{line_prefix}↺ Attempt {attempt}/{_TEST_MAX_ATTEMPTS} — retrying after "
                    f"{_TEST_RETRY_PAUSE_S}s pause …\n"
                )
                if log_file:
                    log_file.write(msg)
                    log_file.flush()
                await asyncio.sleep(_TEST_RETRY_PAUSE_S)

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(project_root),
            )
            async for raw_line in proc.stdout:
                line = raw_line.decode("utf-8", errors="replace")
                if log_file:
                    log_file.write(line_prefix + line)
                    log_file.flush()
            await proc.wait()

            completed = datetime.now().isoformat()
            rc = proc.returncode or 0

            if rc == 0 and tmp_path.exists():
                try:
                    test_result = json.loads(tmp_path.read_text(encoding="utf-8"))
                    summary      = test_result.get("summary", {})
                    total_evaluated = summary.get("total_evaluated", 0)
                    total_errors    = summary.get("total_errors", 0)
                    total_unavail   = summary.get("total_api_unavailable", 0)

                    if total_evaluated == 0:
                        parts = []
                        if total_errors:
                            parts.append(f"{total_errors} case error(s)")
                        if total_unavail:
                            parts.append(f"{total_unavail} API unavailable")
                        fail_reason = "; ".join(parts) or "all cases failed"

                        if not is_last:
                            if log_file:
                                log_file.write(f"{line_prefix}✗ Attempt {attempt} failed: {fail_reason}\n")
                                log_file.flush()
                            continue  # retry

                        await _update_test_entry(
                            batch_path, batch_id, test_id,
                            status="failed", completed_at=completed,
                            test_result=test_result, error=fail_reason,
                        )
                        return 1

       
                    if total_errors > 0 or total_unavail > 0:
                        partial_parts = []
                        if total_errors:
                            partial_parts.append(f"{total_errors} case error(s)")
                        if total_unavail:
                            partial_parts.append(f"{total_unavail} API unavailable")
                        partial_reason = "; ".join(partial_parts)
                        await _update_test_entry(
                            batch_path, batch_id, test_id,
                            status="partial", completed_at=completed,
                            test_result=test_result, error=partial_reason,
                        )
                    else:
                        await _update_test_entry(
                            batch_path, batch_id, test_id,
                            status="ok", completed_at=completed, test_result=test_result,
                        )
                    return 0

                except Exception as exc:
                    fail_reason = str(exc)
                    if not is_last:
                        if log_file:
                            log_file.write(f"{line_prefix}✗ Attempt {attempt} failed (parse error): {fail_reason}\n")
                            log_file.flush()
                        continue  
                    await _update_test_entry(
                        batch_path, batch_id, test_id,
                        status="failed", error=fail_reason, completed_at=completed,
                    )
                    return 1
                finally:
                    try:
                        tmp_path.unlink(missing_ok=True)
                    except Exception:
                        logger.debug(
                            "Cleanup of tmp file failed: %s", tmp_path,
                            exc_info=True,
                        )

            else:
         
                if rc != 0:
                    fail_reason = f"process exited with code {rc}"
                else:
                    fail_reason = "output file not produced (process exited 0)"

                if not is_last:
                    if log_file:
                        log_file.write(f"{line_prefix}✗ Attempt {attempt} failed: {fail_reason}\n")
                        log_file.flush()
                    continue  

            
                existing = _read_batch_file(batch_path)
                existing_test = next(
                    (t for t in (existing or {}).get("tests", []) if t["test_id"] == test_id),
                    None,
                )
                if existing_test:
                    ex_summary   = existing_test.get("summary", {})
                    ex_evaluated = ex_summary.get("total_evaluated", 0)
                    ex_errors    = ex_summary.get("total_errors", 0)
                    ex_unavail   = ex_summary.get("total_api_unavailable", 0)
                    ex_status    = existing_test.get("status", "failed")

                    if ex_evaluated > 0 and ex_errors == 0 and ex_unavail == 0 and ex_status != "failed":
                        if log_file:
                            log_file.write(
                                f"{line_prefix}⚠ {fail_reason}, but existing results are valid "
                                f"({ex_evaluated} evaluated, 0 errors) — keeping current status '{ex_status}'.\n"
                            )
                            log_file.flush()
                        return 0

                    if ex_evaluated > 0 and (ex_errors > 0 or ex_unavail > 0):
                        partial_parts = []
                        if ex_errors:
                            partial_parts.append(f"{ex_errors} case error(s)")
                        if ex_unavail:
                            partial_parts.append(f"{ex_unavail} API unavailable")
                        partial_reason = f"{'; '.join(partial_parts)} (retry: {fail_reason})"
                        await _update_test_entry(
                            batch_path, batch_id, test_id,
                            status="partial", error=partial_reason, completed_at=completed,
                        )
                        return 1

                await _update_test_entry(
                    batch_path, batch_id, test_id,
                    status="failed", error=fail_reason, completed_at=completed,
                )
                return rc if rc != 0 else 1

        return 1

    finally:
        if log_file:
            log_file.close()



@app.get("/test", response_class=HTMLResponse)
async def serve_eval_frontend():
    html_path = Path(__file__).parent / "evaluation" / "eval_frontend.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))



@app.get("/api/eval/csv-diseases")
async def list_csv_diseases(csv_path: str | None = None):
    path = _resolve_csv_path(csv_path)
    if not path.exists():
        raise HTTPException(404, f"CSV not found: {path}")

    try:
        with path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                raise HTTPException(400, "CSV has no header row")

            field_map = {name.lower(): name for name in reader.fieldnames}
            if "prognosis" not in field_map:
                raise HTTPException(400, "CSV is missing a 'prognosis' column")

            key = field_map["prognosis"]
            diseases = sorted({
                (row.get(key) or "").strip() for row in reader if row.get(key)
            })

        return {"csv_path": str(path), "diseases": diseases}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, str(exc))



@app.post("/api/eval/run")
async def start_eval_run(req: EvalRunRequest):
    run_id = str(uuid.uuid4())
    project_root = Path(__file__).resolve().parents[2]
    batch_id = req.batch_id or _make_batch_id()
    batch_dir = project_root / "eval_results" / batch_id
    batch_path = batch_dir / BATCH_FILENAME
    csv_path = req.csv_path or str(project_root / "Benchmark" / "PatientCases.csv")

    configs_plain = [{"prognosis": req.prognosis, "variant": req.variant, "limit": req.limit}]
    batch_data = _create_batch_file(batch_path, batch_id, configs_plain, csv_path)
    test_id = batch_data["tests"][0]["test_id"]
    tmp_path = batch_dir / f"_tmp_{test_id}.json"

    cmd = [sys.executable, "-m", "MedMAS_DDx.evaluation.eval_runner"]
    if req.prognosis:
        cmd += ["--prognosis", req.prognosis]
    if req.variant:
        cmd += ["--variant", req.variant]
    if req.limit is not None:
        cmd += ["--limit", str(req.limit)]
    cmd += ["--csv", csv_path, "--batch-id", batch_id, "--output", str(tmp_path)]

    log_path = batch_dir / f"{test_id}.txt"

    _eval_runs[run_id] = {
        "done": False,
        "error": None,
        "output_path": str(batch_path),
        "task": None,
        "return_code": None,
        "batch_id": batch_id,
        "log_dir": str(batch_dir),
    }

    async def _run():
        try:
            rc = await _run_single_test_subprocess(
                run_id, batch_path, batch_id, test_id, cmd, tmp_path, project_root,
                log_path=log_path,
            )
            _eval_runs[run_id]["return_code"] = rc
            if rc != 0:
                _eval_runs[run_id]["error"] = f"Process exited with code {rc}"
        except Exception as exc:
            _eval_runs[run_id]["error"] = str(exc)
        finally:
            _eval_runs[run_id]["done"] = True

    _eval_runs[run_id]["task"] = asyncio.create_task(_run())
    return {"run_id": run_id, "batch_id": batch_id}



_ALL_VARIANTS: list[str] = ["100", "80", "50"]


def _expand_configs(configs: list[EvalRunRequest]) -> list[EvalRunRequest]:
    """
    Expand any config with variant=None (all variants) into three separate
    configs — one per variant (100 → 80 → 50).  Configs that already specify
    a single variant are left as-is.
    """
    expanded: list[EvalRunRequest] = []
    for c in configs:
        if c.variant is None:
            for v in _ALL_VARIANTS:
                expanded.append(EvalRunRequest(
                    prognosis=c.prognosis,
                    variant=v,
                    limit=c.limit,
                    csv_path=c.csv_path,
                ))
        else:
            expanded.append(c)
    return expanded


@app.post("/api/eval/multi-run")
async def start_multi_eval_run(req: MultiEvalRunRequest):
    if not req.configs:
        raise HTTPException(400, "At least one config required")

    run_id = str(uuid.uuid4())
    project_root = Path(__file__).resolve().parents[2]
    batch_id = req.batch_id or _make_batch_id()
    batch_dir = project_root / "eval_results" / batch_id
    batch_path = batch_dir / BATCH_FILENAME
    csv_path = str(project_root / "Benchmark" / "PatientCases.csv")

   
    configs = _expand_configs(req.configs)

    configs_plain = [
        {"prognosis": c.prognosis, "variant": c.variant, "limit": c.limit}
        for c in configs
    ]
    batch_data = _create_batch_file(batch_path, batch_id, configs_plain, csv_path)
    test_ids = [t["test_id"] for t in batch_data["tests"]]

    batch_log_path = batch_dir / "run.txt"

    _eval_runs[run_id] = {
        "done": False,
        "error": None,
        "output_path": str(batch_path),
        "task": None,
        "return_code": None,
        "is_multi": True,
        "batch_id": batch_id,
        "log_dir": str(batch_dir),
    }

    async def _run():
        batch_dir.mkdir(parents=True, exist_ok=True)
        batch_log = batch_log_path.open("w", encoding="utf-8")
        try:
            batch_log.write(f"▶ Running {len(configs)} test(s) sequentially → batch {batch_id}\n\n")
            batch_log.flush()
            return_codes: list[int] = []
            for i, config in enumerate(configs):
                test_id = test_ids[i]
                tmp_path = batch_dir / f"_tmp_{test_id}.json"
                log_path = batch_dir / f"{test_id}.txt"
                cmd = [sys.executable, "-m", "MedMAS_DDx.evaluation.eval_runner"]
                if config.prognosis:
                    cmd += ["--prognosis", config.prognosis]
                if config.variant:
                    cmd += ["--variant", config.variant]
                if config.limit is not None:
                    cmd += ["--limit", str(config.limit)]
                cfg_csv = config.csv_path or csv_path
                cmd += ["--csv", cfg_csv, "--batch-id", batch_id, "--output", str(tmp_path)]

                batch_log.write(f"── [{i + 1}/{len(configs)}] Starting: {test_id}\n")
                batch_log.flush()
                rc = await _run_single_test_subprocess(
                    run_id, batch_path, batch_id, test_id, cmd, tmp_path,
                    project_root, log_path=log_path,
                )
                return_codes.append(rc)
                batch_log.write(f"── [{i + 1}/{len(configs)}] Done: {test_id} (rc={rc})\n")
                batch_log.flush()

            failed = [i + 1 for i, rc in enumerate(return_codes) if rc != 0]
            if failed:
                _eval_runs[run_id]["error"] = f"Test(s) {failed} exited with non-zero code"

            batch_log.write(
                f"\n✓ Batch {batch_id} complete — {len(configs) - len(failed)}"
                f"/{len(configs)} test(s) succeeded.\n"
            )
            batch_log.flush()

        except Exception as exc:
            import traceback as _tb
            _eval_runs[run_id]["error"] = f"{type(exc).__name__}: {exc}"
            batch_log.write(f"\n[ERROR] {type(exc).__name__}: {exc}\n{_tb.format_exc()}")
            batch_log.flush()
        finally:
            batch_log.close()
            _eval_runs[run_id]["done"] = True

    _eval_runs[run_id]["task"] = asyncio.create_task(_run())
    return {"run_id": run_id, "batch_id": batch_id}



@app.post("/api/eval/retry-test")
async def retry_test(req: RetryTestRequest):
    """
    Re-run a single failed (or api_unavailable) test within an existing
    batch.json.  Only that test's entry is overwritten; other tests and the
    global summary are recomputed from results already in the file.
    """
    run_id = str(uuid.uuid4())
    project_root = Path(__file__).resolve().parents[2]
    batch_dir = project_root / "eval_results" / req.batch_id
    batch_path = batch_dir / BATCH_FILENAME

    if not batch_path.exists():
        raise HTTPException(404, f"Batch not found: {req.batch_id}")

    batch = _read_batch_file(batch_path)
    if not batch:
        raise HTTPException(500, "Failed to read batch file")

    test = next(
        (t for t in batch.get("tests", []) if t["test_id"] == req.test_id), None
    )
    if test is None:
        raise HTTPException(404, f"Test '{req.test_id}' not found in batch '{req.batch_id}'")

    csv_path = batch.get("meta", {}).get("csv_path") or str(project_root / "Benchmark" / "PatientCases.csv")
    test_id = test["test_id"]
    tmp_path = batch_dir / f"_tmp_{test_id}_retry.json"

    retry_src_path = batch_dir / f"_tmp_{test_id}_retry_src.json"
    retry_src_path.write_text(
        json.dumps({"cases": test.get("cases", [])}, ensure_ascii=False),
        encoding="utf-8",
    )

    cmd = [sys.executable, "-m", "MedMAS_DDx.evaluation.eval_runner"]
    cmd += ["--retry-failed", str(retry_src_path)]
    cmd += ["--csv", csv_path, "--batch-id", req.batch_id, "--output", str(tmp_path)]

    retry_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = batch_dir / f"{test_id}_retry_{retry_ts}.txt"

    _eval_runs[run_id] = {
        "done": False,
        "error": None,
        "output_path": str(batch_path),
        "task": None,
        "return_code": None,
        "batch_id": req.batch_id,
        "log_dir": str(batch_dir),
    }

    async def _run():
        try:
            rc = await _run_single_test_subprocess(
                run_id, batch_path, req.batch_id, test_id, cmd, tmp_path, project_root,
                log_path=log_path,
            )
            _eval_runs[run_id]["return_code"] = rc
            if rc != 0:
                _eval_runs[run_id]["error"] = f"Process exited with code {rc}"
        except Exception as exc:
            _eval_runs[run_id]["error"] = str(exc)
        finally:
            _eval_runs[run_id]["done"] = True
            try:
                retry_src_path.unlink(missing_ok=True)
            except Exception:
                logger.debug(
                    "Cleanup of retry source file failed: %s",
                    retry_src_path, exc_info=True,
                )

    _eval_runs[run_id]["task"] = asyncio.create_task(_run())
    return {"run_id": run_id, "batch_id": req.batch_id, "test_id": test_id}



@app.post("/api/eval/retry-cases")
async def retry_cases(req: RetryCasesRequest):
    """
    Re-run only the specified case IDs within an existing batch test.

    All other cases in that test are carried over unchanged.  Only the cases
    whose case_id appears in *req.case_ids* are forced to re-evaluate,
    regardless of their current status.
    """
    if not req.case_ids:
        raise HTTPException(400, "case_ids must be a non-empty list")

    run_id = str(uuid.uuid4())
    project_root = Path(__file__).resolve().parents[2]
    batch_dir = project_root / "eval_results" / req.batch_id
    batch_path = batch_dir / BATCH_FILENAME

    if not batch_path.exists():
        raise HTTPException(404, f"Batch not found: {req.batch_id}")

    batch = _read_batch_file(batch_path)
    if not batch:
        raise HTTPException(500, "Failed to read batch file")

    test = next(
        (t for t in batch.get("tests", []) if t["test_id"] == req.test_id), None
    )
    if test is None:
        raise HTTPException(404, f"Test '{req.test_id}' not found in batch '{req.batch_id}'")

    csv_path = batch.get("meta", {}).get("csv_path") or str(project_root / "Benchmark" / "PatientCases.csv")
    test_id = test["test_id"]
    tmp_path = batch_dir / f"_tmp_{test_id}_cases_retry.json"

    case_ids_set = set(req.case_ids)
    source_cases = []
    for c in test.get("cases", []):
        c_copy = dict(c)
        if c_copy.get("case_id") in case_ids_set:
            c_copy["status"] = "error" 
        source_cases.append(c_copy)

    retry_src_path = batch_dir / f"_tmp_{test_id}_cases_retry_src.json"
    retry_src_path.write_text(
        json.dumps({"cases": source_cases}, ensure_ascii=False),
        encoding="utf-8",
    )

    cmd = [sys.executable, "-m", "MedMAS_DDx.evaluation.eval_runner"]
    cmd += ["--retry-failed", str(retry_src_path)]
    cmd += ["--csv", csv_path, "--batch-id", req.batch_id, "--output", str(tmp_path)]

    retry_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = batch_dir / f"{test_id}_cases_retry_{retry_ts}.txt"

    _eval_runs[run_id] = {
        "done": False,
        "error": None,
        "output_path": str(batch_path),
        "task": None,
        "return_code": None,
        "batch_id": req.batch_id,
        "log_dir": str(batch_dir),
    }

    async def _run():
        try:
            rc = await _run_single_test_subprocess(
                run_id, batch_path, req.batch_id, test_id, cmd, tmp_path, project_root,
                log_path=log_path,
            )
            _eval_runs[run_id]["return_code"] = rc
            if rc != 0:
                _eval_runs[run_id]["error"] = f"Process exited with code {rc}"
        except Exception as exc:
            _eval_runs[run_id]["error"] = str(exc)
        finally:
            _eval_runs[run_id]["done"] = True
            try:
                retry_src_path.unlink(missing_ok=True)
            except Exception:
                logger.debug(
                    "Cleanup of retry source file failed: %s",
                    retry_src_path, exc_info=True,
                )

    _eval_runs[run_id]["task"] = asyncio.create_task(_run())
    return {"run_id": run_id, "batch_id": req.batch_id, "test_id": test_id}



class FixTestStatusRequest(BaseModel):
    batch_id: str
    test_id: str


@app.post("/api/eval/fix-test-status")
async def fix_test_status(req: FixTestStatusRequest):
   
    project_root = Path(__file__).resolve().parents[2]
    batch_dir    = project_root / "eval_results" / req.batch_id
    batch_path   = batch_dir / BATCH_FILENAME

    if not batch_path.exists():
        raise HTTPException(404, f"Batch not found: {req.batch_id}")

    lock = _get_batch_lock(req.batch_id)
    async with lock:
        batch = _read_batch_file(batch_path)
        if not batch:
            raise HTTPException(500, "Failed to read batch file")

        test = next(
            (t for t in batch.get("tests", []) if t["test_id"] == req.test_id), None
        )
        if test is None:
            raise HTTPException(404, f"Test '{req.test_id}' not found in batch '{req.batch_id}'")

        summary    = test.get("summary", {})
        evaluated  = summary.get("total_evaluated", 0)
        errors     = summary.get("total_errors", 0)
        unavail    = summary.get("total_api_unavailable", 0)
        old_status = test.get("status", "failed")

        if evaluated == 0:
            return {"old_status": old_status, "new_status": old_status, "changed": False,
                    "reason": "No evaluated cases — status unchanged."}

        if errors == 0 and unavail == 0:
            new_status = "ok"
            test["error"] = None
        else:
            new_status = "partial"
            parts = []
            if errors:
                parts.append(f"{errors} case error(s)")
            if unavail:
                parts.append(f"{unavail} API unavailable")
            test["error"] = "; ".join(parts)

        test["status"] = new_status

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
            batch["meta"]["status"] = "partial" if failed_count > 0 else "complete"
            if not batch["meta"].get("timestamp_end"):
                batch["meta"]["timestamp_end"] = datetime.now().isoformat()
        else:
            batch["meta"]["status"] = "in_progress"

        try:
            batch["summary"] = _recompute_global_summary(tests)
        except Exception:
            logger.exception("Failed to recompute global summary after fix-test-status")

        _write_batch_file(batch_path, batch)

    return {
        "old_status": old_status,
        "new_status": new_status,
        "changed": old_status != new_status,
        "reason": f"Re-derived from data: {evaluated} evaluated, {errors} errors, {unavail} unavailable.",
    }



@app.post("/api/eval/retry")
async def retry_failed_run(req: RetryRunRequest):
    """
    Backward-compatible endpoint: retries failed cases from an old v1
    individual eval JSON file (not a batch.json).
    """
    run_id = str(uuid.uuid4())
    project_root = Path(__file__).resolve().parents[2]
    source_path = project_root / "eval_results" / req.batch_id / req.filename

    if not source_path.exists():
        raise HTTPException(404, f"Source result not found: {source_path}")

    stem = source_path.stem
    retry_name = f"{stem}_retry.json"
    output_path = source_path.parent / retry_name

    cmd = [
        sys.executable, "-m", "MedMAS_DDx.evaluation.eval_runner",
        "--retry-failed", str(source_path),
        "--retry-statuses", req.retry_statuses,
        "--batch-id", req.batch_id,
        "--output", str(output_path),
    ]

    log_path = source_path.parent / f"{source_path.stem}_retry.txt"

    _eval_runs[run_id] = {
        "done": False,
        "error": None,
        "output_path": str(output_path),
        "task": None,
        "return_code": None,
        "log_dir": str(source_path.parent),
    }

    async def _run():
        log_file = log_path.open("w", encoding="utf-8")
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(project_root),
            )
            async for raw_line in proc.stdout:
                line = raw_line.decode("utf-8", errors="replace")
                log_file.write(line)
                log_file.flush()
            await proc.wait()
            _eval_runs[run_id]["return_code"] = proc.returncode
            if proc.returncode != 0:
                _eval_runs[run_id]["error"] = f"Process exited with code {proc.returncode}"
        except Exception as exc:
            _eval_runs[run_id]["error"] = str(exc)
        finally:
            log_file.close()
            _eval_runs[run_id]["done"] = True

    _eval_runs[run_id]["task"] = asyncio.create_task(_run())
    return {"run_id": run_id, "batch_id": req.batch_id, "output_file": retry_name}



@app.get("/api/eval/stream/{run_id}")
async def stream_eval_output(run_id: str):
    """
    Streams heartbeats until the run finishes, then emits a single 'done'
    event.  Subprocess output is written to per-test .txt files in the batch
    directory instead of being kept in memory.
    """
    run = _eval_runs.get(run_id)
    if not run:
        raise HTTPException(404, "Eval run not found")

    async def event_gen():
        while not run["done"]:
            yield ": heartbeat\n\n"
            await asyncio.sleep(0.5)

        yield f"data: {json.dumps({'done': True, 'error': run.get('error'), 'return_code': run.get('return_code'), 'log_dir': run.get('log_dir')})}\n\n"

    return StreamingResponse(event_gen(), media_type="text/event-stream")



@app.get("/api/eval/result/{run_id}")
async def get_eval_result(run_id: str):
    run = _eval_runs.get(run_id)
    if not run:
        raise HTTPException(404, "Eval run not found")

    if not run["done"]:
        return {"status": "running"}

    output_path = Path(run["output_path"])
    if output_path.exists():
        try:
            result = json.loads(output_path.read_text(encoding="utf-8"))
            return {"status": "complete", "result": result}
        except Exception as exc:
            return {"status": "complete", "result": None, "error": str(exc)}

    return {"status": "complete", "result": None, "error": run.get("error")}



@app.get("/api/eval/batch/{batch_id}/{filename}")
async def get_batch_run_result(batch_id: str, filename: str):
    project_root = Path(__file__).resolve().parents[2]
    path = project_root / "eval_results" / batch_id / filename

    if not path.exists():
        if filename != BATCH_FILENAME:
            fallback = project_root / "eval_results" / batch_id / BATCH_FILENAME
            if fallback.exists():
                path = fallback
        if not path.exists():
            progress = path.with_suffix(".progress.json")
            if progress.exists():
                try:
                    result = json.loads(progress.read_text(encoding="utf-8"))
                    return {"status": "in_progress", "result": result}
                except Exception:
                    logger.exception(
                        "Failed to read in-progress file: %s", progress
                    )
            raise HTTPException(404, f"Result not found: {batch_id}/{filename}")

    try:
        result = json.loads(path.read_text(encoding="utf-8"))
        batch_status = result.get("meta", {}).get("status", "complete")
        return {"status": batch_status, "result": result}
    except Exception as exc:
        raise HTTPException(500, str(exc))



def _run_summary_v1(data: dict) -> dict:
    """Extract a compact summary row from a v1 individual batch result JSON dict."""
    meta = data.get("meta", {})
    summary = data.get("summary", {})
    return {
        "timestamp": meta.get("timestamp", ""),
        "variant_filter": meta.get("variant_filter"),
        "prognosis_filter": meta.get("prognosis_filter"),
        "total_cases": meta.get("total_cases", 0),
        "errors": meta.get("errors", 0),
        "api_unavailable": meta.get("api_unavailable", 0),
        "total_evaluated": summary.get("total_evaluated", 0),
        "sim_phase1_top1_accuracy": summary.get("sim_phase1_top1_accuracy"),
        "sim_phase1_top3_accuracy": summary.get("sim_phase1_top3_accuracy"),
        "sim_phase2_top1_accuracy": summary.get("sim_phase2_top1_accuracy"),
        "sim_phase2_top3_accuracy": summary.get("sim_phase2_top3_accuracy"),
        "mean_sim_score_phase2": summary.get("mean_sim_score_phase2"),
        "retry_mode": meta.get("retry_mode", False),
        "status": meta.get("status", "complete"),
    }


@app.get("/api/eval/history")
async def list_eval_history():
    project_root = Path(__file__).resolve().parents[2]
    results_dir = project_root / "eval_results"

    if not results_dir.exists():
        return {"batches": []}

    batches = []
    for batch_dir in sorted(
        (d for d in results_dir.iterdir() if d.is_dir() and d.name.startswith("batch_")),
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    ):
        batch_path = batch_dir / BATCH_FILENAME

        if batch_path.exists():
            try:
                data = json.loads(batch_path.read_text(encoding="utf-8"))
                if data.get("format") != "batch_v2":
                    raise ValueError("Not a v2 batch file")

                meta    = data.get("meta", {})
                summary = data.get("summary", {})
                tests   = data.get("tests", [])

                test_summaries = []
                for t in tests:
                    ts = t.get("summary", {})
                    test_summaries.append({
                        "test_id":             t["test_id"],
                        "prognosis_filter":    t.get("prognosis_filter"),
                        "variant_filter":      t.get("variant_filter"),
                        "status":              t.get("status", "unknown"),
                        "error":               t.get("error"),
                        "total_cases":         t.get("total_cases", 0),
                        "sim_phase1_top1_accuracy": ts.get("sim_phase1_top1_accuracy"),
                        "sim_phase1_top3_accuracy": ts.get("sim_phase1_top3_accuracy"),
                        "sim_phase2_top1_accuracy": ts.get("sim_phase2_top1_accuracy"),
                        "sim_phase2_top3_accuracy": ts.get("sim_phase2_top3_accuracy"),
                    })

                batches.append({
                    "batch_id":        batch_dir.name,
                    "format":          "batch_v2",
                    "timestamp":       meta.get("timestamp", ""),
                    "timestamp_end":   meta.get("timestamp_end"),
                    "status":          meta.get("status", "unknown"),
                    "total_tests":     meta.get("total_tests", 0),
                    "completed_tests": meta.get("completed_tests", 0),
                    "failed_tests":    meta.get("failed_tests", 0),
                    "total_evaluated": summary.get("total_evaluated", 0),
                    "sim_phase2_top1_accuracy": summary.get("sim_phase2_top1_accuracy"),
                    "filename":        BATCH_FILENAME,
                    "tests":           test_summaries,
                })
            except Exception:
                logger.exception(
                    "Skipping unreadable v2 batch file: %s", batch_path
                )
                continue

        else:
            runs = []
            for f in sorted(
                batch_dir.glob("eval_*.json"),
                key=lambda p: p.stat().st_mtime,
            ):
                if f.stem.endswith(".progress"):
                    continue
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    row = _run_summary_v1(data)
                    row["filename"] = f.name
                    runs.append(row)
                except Exception:
                    logger.exception(
                        "Skipping unreadable v1 eval file: %s", f
                    )
                    continue

            if not runs:
                continue

            batch_ts = runs[0].get("timestamp", "") if runs else ""
            batches.append({
                "batch_id":  batch_dir.name,
                "format":    "batch_v1",
                "timestamp": batch_ts,
                "runs":      runs,
            })

    return {"batches": batches}
