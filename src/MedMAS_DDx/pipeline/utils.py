import sys
import json
import io
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

class PipelineLogger:
    @staticmethod
    def build_runtime_log_file() -> str:
        logs_dir = Path(__file__).resolve().parents[3] / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        return str(logs_dir / f"flow_{ts}.txt")

    @staticmethod
    @contextmanager
    def tee_terminal_to_log(log_file_path: str):
        original_stdout = sys.stdout
        original_stderr = sys.stderr
        with open(log_file_path, "a", encoding="utf-8") as log_handle:
            sys.stdout = PipelineLogger._TeeStream(original_stdout, log_handle)
            sys.stderr = PipelineLogger._TeeStream(original_stderr, log_handle)
            try:
                yield
            finally:
                sys.stdout.flush()
                sys.stderr.flush()
                sys.stdout = original_stdout
                sys.stderr = original_stderr

    class _TeeStream(io.TextIOBase):

        def __init__(self, *streams: io.TextIOBase):
            self._streams = streams

        def write(self, s: str) -> int:
            for stream in self._streams:
                stream.write(s)
            return len(s)

        def flush(self) -> None:
            for stream in self._streams:
                stream.flush()

        def isatty(self) -> bool:
            return any(getattr(stream, "isatty", lambda: False)() for stream in self._streams)


class PayloadExtractor:
    @staticmethod
    def parse_json(raw: str) -> dict[str, Any] | None:
        if not raw:
            return None
        try:
            p = json.loads(raw)
            return p if isinstance(p, dict) else None
        except json.JSONDecodeError:
            pass
        s, e = raw.find("{"), raw.rfind("}")
        if s == -1 or e <= s:
            return None
        try:
            p = json.loads(raw[s:e + 1])
            return p if isinstance(p, dict) else None
        except json.JSONDecodeError:
            return None

    @staticmethod
    def extract_extracted_symptoms(crew_result: Any) -> list[dict[str, Any]]:
        for task_out in getattr(crew_result, "tasks_output", []) or []:
            parsed = PayloadExtractor.parse_json(getattr(task_out, "raw", ""))
            if not parsed:
                continue
            if isinstance(parsed.get("symptoms"), list):
                return parsed["symptoms"]
        return []

    @staticmethod
    def extract_followup_payload(crew_result: Any) -> dict[str, Any]:
        for task_out in getattr(crew_result, "tasks_output", []) or []:
            parsed = PayloadExtractor.parse_json(getattr(task_out, "raw", ""))
            if not parsed:
                continue
            if "followup_needed" in parsed and "questions_asked" in parsed:
                return parsed
        return {}
