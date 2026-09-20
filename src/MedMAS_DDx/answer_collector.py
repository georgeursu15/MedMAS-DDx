
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from abc import ABC, abstractmethod
from typing import Any, Callable


_YES_TOKENS = {
    "yes", "y", "yeah", "yep", "yup", "sure", "affirmative", "correct", "true",
}
_NO_TOKENS = {
    "no", "n", "nope", "nah", "negative", "false", "never",
}
_UNSURE_TOKENS = {
    "unsure", "maybe", "idk", "unknown", "dontknow", "notsure", "neutral",
}


def normalize_answer(raw: str) -> str:
    token = ""
    if raw.strip():
        token = raw.strip().lower().split()[0]
        token = re.sub(r"[^a-z]", "", token)
    if token in _YES_TOKENS:
        return "yes"
    if token in _NO_TOKENS:
        return "no"
    return "neutral"


def build_patient_question(symptom: str, suggested_question: str) -> str:
    clean_symptom = re.sub(r"\s+", " ", symptom.strip()) or "this symptom"
    clean_q = re.sub(r"\s+", " ", (suggested_question or "").strip())

    if not clean_q:
        return f"Do you currently have {clean_symptom}?"

    return clean_q



def _prepare_followup(followup_payload: dict[str, Any]) -> tuple[
    list[dict[str, Any]],  
    list[dict[str, Any]],  
]:
    
    investigations = followup_payload.get("investigations_required") or []

    questions: list[dict] = followup_payload.get("questions_asked") or []
    seen: set[str] = set()
    deduped: list[dict] = []
    for q in questions:
        if not isinstance(q, dict):
            continue
        key = str(q.get("symptom", "")).strip().casefold()
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        deduped.append(q)

    unanswered = [q for q in deduped if not (q.get("patient_answer") or "").strip()]
    return investigations, unanswered



class AnswerCollector(ABC):
 
    @abstractmethod
    async def present_investigations(
        self, investigations: list[dict[str, Any]]
    ) -> None:
        ...

    @abstractmethod
    async def ask_question(
        self,
        index: int,
        total: int,
        symptom: str,
        question_text: str,
    ) -> str:
     
        ...

    @abstractmethod
    async def on_no_followup_needed(self) -> None:
        ...

    async def on_initial_ranking(self, ranking_payload: dict[str, Any]) -> None:
        return

    async def on_extracted_symptoms(
        self, extracted_symptoms: list[dict[str, Any]]
    ) -> None:
        return

    async def collect(self, followup_payload: dict[str, Any]) -> dict[str, Any]:
     
        if not followup_payload.get("followup_needed", False):
            await self.on_no_followup_needed()
            return followup_payload

        investigations, unanswered = _prepare_followup(followup_payload)

        if investigations:
            await self.present_investigations(investigations)

        if not unanswered:
            return followup_payload

        for idx, q in enumerate(unanswered, start=1):
            symptom = str(q.get("symptom") or "this symptom").strip()
            question_text = build_patient_question(
                symptom, q.get("question", "")
            )
            raw_answer = await self.ask_question(idx, len(unanswered), symptom, question_text)
            q["patient_answer"] = normalize_answer(raw_answer)

        return followup_payload



class TerminalAnswerCollector(AnswerCollector):

    async def present_investigations(
        self, investigations: list[dict[str, Any]]
    ) -> None:
        print("\n[Clinical measurements/tests required before diagnosis refinement]")
        for item in investigations:
            if not isinstance(item, dict):
                continue
            finding = str(item.get("finding", "")).strip() or "unspecified finding"
            reason = str(item.get("reason", "")).strip() or "requires clinical measurement"
            print(f"  - {finding}: {reason}")
        print()

    async def ask_question(
        self, index: int, total: int, symptom: str, question_text: str
    ) -> str:
        while True:
            raw = input(f"[{index}/{total}] {question_text} ").strip()
            if raw:
                normalised = normalize_answer(raw)
                print(f"        → recorded: {normalised}\n")
                return raw
            print("        Please enter an answer (yes / no / unsure)")

    async def on_no_followup_needed(self) -> None:
        print("\n[No follow-up needed — only one disease candidate found.]\n")



class AsyncAnswerCollector(AnswerCollector):
    

    def __init__(self) -> None:
        self._answer_queue: asyncio.Queue[str] = asyncio.Queue()
        self._pending_questions: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._investigations: list[dict[str, Any]] = []

    @property
    def pending_questions(self) -> asyncio.Queue[dict[str, Any]]:
        return self._pending_questions

    @property
    def investigations(self) -> list[dict[str, Any]]:
        return self._investigations

    async def submit_answer(self, raw_answer: str) -> None:
        await self._answer_queue.put(raw_answer)

    async def present_investigations(
        self, investigations: list[dict[str, Any]]
    ) -> None:
        self._investigations = investigations

    async def ask_question(
        self, index: int, total: int, symptom: str, question_text: str
    ) -> str:
        await self._pending_questions.put({
            "index": index,
            "total": total,
            "symptom": symptom,
            "question": question_text,
        })
        return await self._answer_queue.get()

    async def on_no_followup_needed(self) -> None:
        await self._pending_questions.put({"done": True, "reason": "no_followup_needed"})



class AutoAnswerCollector(AnswerCollector):
 

    def __init__(self, answer_fn: Callable[[str], str]) -> None:
        self._answer_fn = answer_fn

    async def present_investigations(
        self, investigations: list[dict[str, Any]]
    ) -> None:
        pass 

    async def ask_question(
        self, index: int, total: int, symptom: str, question_text: str
    ) -> str:
        return self._answer_fn(symptom)

    async def on_no_followup_needed(self) -> None:
        pass


_PATIENT_LLM_CACHE: dict[tuple[str, int], Any] = {}


def _resolve_patient_model() -> str:
   
    for var in ("PATIENT_LLM", "MEDMAS_DDX_LLM", "MODEL"):
        v = os.getenv(var, "").strip()
        if v:
            return v
    return "google/gemini-3.1-flash-lite-preview"


def _get_patient_llm(model: str | None = None, timeout: int | None = None) -> Any:
   
    from crewai import LLM  

    model = (model or _resolve_patient_model()).strip()
    timeout = int(timeout or os.getenv("MEDMAS_DDX_LLM_TIMEOUT", "300"))
    key = (model, timeout)
    if key not in _PATIENT_LLM_CACHE:
        _PATIENT_LLM_CACHE[key] = LLM(model=model, timeout=timeout)
    return _PATIENT_LLM_CACHE[key]


_LLM_YES = re.compile(
    r"\b(yes|yeah|yep|affirmative|correct|true|i\s+do|i\s+have)\b",
    re.IGNORECASE,
)
_LLM_NO = re.compile(
    r"\b(no|nope|not|negative|false|never|don'?t|i\s+do\s+not|i\s+don'?t)\b",
    re.IGNORECASE,
)
_LLM_UNSURE = re.compile(
    r"\b(unsure|maybe|don'?t know|do not know|idk|unknown|not sure|neutral)\b",
    re.IGNORECASE,
)


def _normalise_llm_answer(raw: str) -> str:

    if not raw or not raw.strip():
        return "neutral"

    m = re.search(r"answer\s*[:\-]\s*([a-z]+)", raw, re.IGNORECASE)
    if m:
        token = m.group(1).lower()
        if _LLM_YES.search(token):
            return "yes"
        if _LLM_NO.search(token):
            return "no"
        if _LLM_UNSURE.search(token):
            return "neutral"

    if _LLM_YES.search(raw):
        return "yes"
    if _LLM_NO.search(raw):
        return "no"
    if _LLM_UNSURE.search(raw):
        return "neutral"

    return "neutral"



_DEFAULT_PATIENT_PERSONA: dict[str, str] = {
    "role": "Simulated Patient for Batch Evaluation",
    "goal": (
        "Role-play a patient with a specific medical record and answer "
        "every yes/no follow-up question the doctor asks in a SINGLE "
        "response, using ONLY the provided ground-truth context."
    ),
    "backstory": (
        "You are role-playing a patient. The doctor asks a BATCH of yes/no "
        "follow-up questions and you must answer ALL of them in ONE reply.\n\n"
        "GROUND-TRUTH context (the doctor does NOT see this):\n"
        "  - You have a full clinical description of your presentation.\n"
        "  - You have a canonical list of symptoms you actually have. If a "
        "symptom (or a clear medical/lay synonym) is on that list OR is "
        "directly described in the case text, you HAVE it. Otherwise you "
        "do NOT.\n\n"
        "Rules:\n"
        "  - Each answer must be EXACTLY one of: yes / no / neutral.\n"
        "  - Answer EVERY question in the same order the doctor asked.\n\n"
        "Output format (strict): a single JSON object, no prose, no fences:\n"
        "  {\n"
        '    "answers": [\n'
        '      {"index": 1, "symptom": "<from Q1>", "answer": "yes"},\n'
        '      {"index": 2, "symptom": "<from Q2>", "answer": "no"}\n'
        "    ]\n"
        "  }\n"
        "Copy each symptom verbatim from the question header."
    ),
}


def _build_system_prompt(persona: dict[str, str]) -> str:
   
    parts: list[str] = []
    for field, label in (
        ("role",      "ROLE"),
        ("goal",      "GOAL"),
        ("backstory", "BACKSTORY"),
    ):
        text = (persona.get(field) or _DEFAULT_PATIENT_PERSONA[field]).strip()
        if text:
            parts.append(f"── {label} ──\n{text}")
    return "\n\n".join(parts)


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)


def _extract_json_object(raw: str) -> dict[str, Any] | None:
   
    if not raw:
        return None
    text = _FENCE_RE.sub("", raw).strip()
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(text[start:end + 1])
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


class PatientLLMAnswerCollector(AnswerCollector):
   

    def __init__(
        self,
        case_text: str,
        known_symptoms: list[str] | None = None,
        *,
        llm: Any | None = None,
        persona: dict[str, str] | None = None,
        model: str | None = None,       
        timeout: int | None = None,      
        extra_context: str = "",
    ) -> None:
        self._case_text = (case_text or "").strip()
        self._known_symptoms = [s.strip() for s in (known_symptoms or []) if s and s.strip()]
        self._extra_context = (extra_context or "").strip()

   
        self._llm = llm
        self._fallback_model = (model or _resolve_patient_model()).strip()
        self._timeout = timeout

        if llm is not None and hasattr(llm, "model"):
            self._model_name = str(getattr(llm, "model", "")).strip() or self._fallback_model
        else:
            self._model_name = self._fallback_model

        self._persona = persona or _DEFAULT_PATIENT_PERSONA
        self._system_prompt = _build_system_prompt(self._persona)

        self._answer_cache: dict[str, str] = {}
        self.llm_calls: list[dict[str, Any]] = []
        self.batched_ok: bool | None = None
        self.batched_error: str | None = None
        self.batched_raw_response: str = ""


    def _ensure_llm(self) -> Any:
        if self._llm is None:
            self._llm = _get_patient_llm(model=self._fallback_model, timeout=self._timeout)
            if hasattr(self._llm, "model"):
                self._model_name = (
                    str(getattr(self._llm, "model", "")).strip() or self._model_name
                )
        return self._llm

    @staticmethod
    def _key(symptom: str) -> str:
        return re.sub(r"\s+", " ", (symptom or "").strip()).casefold()

    def _ground_truth_block(self) -> str:
        symptom_list = (
            ", ".join(self._known_symptoms)
            if self._known_symptoms
            else "(none provided)"
        )
        case_text = self._case_text or "(no case text provided)"
        extra = f"\n\n── ADDITIONAL CONTEXT ──\n{self._extra_context}" if self._extra_context else ""
        return (
            "── YOUR GROUND-TRUTH SYMPTOM LIST (canonical, from your medical record) ──\n"
            f"{symptom_list}\n\n"
            "── FULL CLINICAL DESCRIPTION OF YOUR PRESENTATION (100% case text) ──\n"
            f"{case_text}"
            f"{extra}"
        )

    def _build_batch_prompt(self, questions: list[dict[str, Any]]) -> str:
        lines: list[str] = ["── DOCTOR'S BATCH OF QUESTIONS ──"]
        for i, q in enumerate(questions, start=1):
            symptom = str(q.get("symptom") or "this symptom").strip()
            qtext = str(q.get("question") or "").strip() or f"Do you currently have {symptom}?"
            lines.append(f"{i}. [{symptom}] {qtext}")
        lines.extend([
            "",
            "Respond with a single JSON object of this exact shape (no prose, no fences):",
            "{",
            '  "answers": [',
            *(
                f'    {{"index": {i}, "symptom": "{str(q.get("symptom") or "").strip()}", "answer": "<yes|no|neutral>"}}'
                + ("," if i < len(questions) else "")
                for i, q in enumerate(questions, start=1)
            ),
            "  ]",
            "}",
        ])
        return (
            f"{self._ground_truth_block()}\n\n"
            + "\n".join(lines)
        )

    def _build_single_prompt(self, symptom: str, question_text: str) -> str:
        return (
            f"{self._ground_truth_block()}\n\n"
            "── DOCTOR'S QUESTION ──\n"
            f"Symptom under discussion: {symptom}\n"
            f"Question: {question_text}\n\n"
            'Respond with a single JSON object: {"answers": [{"index": 1, '
            f'"symptom": "{symptom}", "answer": "<yes|no|neutral>"}}]}}'
        )

    def _ingest_batch_response(
        self,
        raw: str,
        questions: list[dict[str, Any]],
    ) -> bool:
     
        obj = _extract_json_object(raw)
        if not obj:
            return False

        answers = obj.get("answers")
        if not isinstance(answers, list):
            return False

        ingested = 0
        by_index: dict[int, str] = {}
        by_symptom: dict[str, str] = {}
        for entry in answers:
            if not isinstance(entry, dict):
                continue
            decision = _normalise_llm_answer(str(entry.get("answer", "")))
            idx = entry.get("index")
            sym = str(entry.get("symptom") or "").strip()
            if isinstance(idx, int):
                by_index[idx] = decision
            if sym:
                by_symptom[self._key(sym)] = decision

        for i, q in enumerate(questions, start=1):
            sym = str(q.get("symptom") or "").strip()
            if not sym:
                continue
            key = self._key(sym)
            if i in by_index:
                self._answer_cache[key] = by_index[i]
                ingested += 1
            elif key in by_symptom:
                self._answer_cache[key] = by_symptom[key]
                ingested += 1

        return ingested > 0

    async def _run_batched_call(
        self, questions: list[dict[str, Any]]
    ) -> tuple[bool, str, str | None]:
      
        prompt = self._build_batch_prompt(questions)
        messages = [
            {"role": "system", "content": self._system_prompt},
            {"role": "user",   "content": prompt},
        ]
        try:
            llm = self._ensure_llm()
            raw = await asyncio.to_thread(llm.call, messages)
            raw = (raw or "").strip()
        except Exception as exc:
            return False, "", f"{type(exc).__name__}: {exc}"

        ok = self._ingest_batch_response(raw, questions)
        return ok, raw, None if ok else "batched JSON parse failed"


    async def present_investigations(
        self, investigations: list[dict[str, Any]]
    ) -> None:
        return None

    async def on_no_followup_needed(self) -> None:
        return None

    async def collect(self, followup_payload: dict[str, Any]) -> dict[str, Any]:
        
        self._answer_cache = {}
        self.llm_calls = []
        self.batched_ok = None
        self.batched_error = None
        self.batched_raw_response = ""

        if not followup_payload.get("followup_needed", False):
            await self.on_no_followup_needed()
            return followup_payload

        _, unanswered = _prepare_followup(followup_payload)

        if unanswered:
            ok, raw, error = await self._run_batched_call(unanswered)
            self.batched_ok = ok
            self.batched_raw_response = raw
            self.batched_error = error
            if not ok:
                logging.warning(
                    "PatientLLMAnswerCollector: batched call failed (%s) — "
                    "falling back to per-question calls for %d question(s).",
                    error, len(unanswered),
                )

        return await super().collect(followup_payload)

    async def ask_question(
        self, index: int, total: int, symptom: str, question_text: str
    ) -> str:
        key = self._key(symptom)
        cached = self._answer_cache.get(key)
        if cached is not None:
            self.llm_calls.append({
                "index": index,
                "total": total,
                "symptom": symptom,
                "question": question_text,
                "raw_response": "(from batched call)",
                "decision": cached,
                "error": None,
                "source": "batched",
            })
            return cached

        
        prompt = self._build_single_prompt(symptom, question_text)
        messages = [
            {"role": "system", "content": self._system_prompt},
            {"role": "user",   "content": prompt},
        ]
        raw = ""
        error: str | None = None
        try:
            llm = self._ensure_llm()
            raw = await asyncio.to_thread(llm.call, messages)
            raw = (raw or "").strip()
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            logging.warning(
                "PatientLLMAnswerCollector: per-question fallback failed for "
                "symptom %r — defaulting to neutral. error=%s", symptom, error,
            )

        decision = "neutral"
        if not error and raw:
            obj = _extract_json_object(raw)
            if obj and isinstance(obj.get("answers"), list) and obj["answers"]:
                first = obj["answers"][0]
                if isinstance(first, dict):
                    decision = _normalise_llm_answer(str(first.get("answer", "")))
            else:
                decision = _normalise_llm_answer(raw)

        self.llm_calls.append({
            "index": index,
            "total": total,
            "symptom": symptom,
            "question": question_text,
            "raw_response": raw,
            "decision": decision,
            "error": error,
            "source": "per_question",
        })
        return decision