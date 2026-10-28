# `MedMAS_DDx` — package overview

Orchestration layer. Everything here either wires up the CrewAI agents, drives
the diagnostic pipeline end to end, or exposes it over HTTP.

The rule the whole package follows: **the model reads and writes language,
Python decides the ranking.** When you add code here, anything that produces a
number belongs in [`tools/`](tools/) or [`pipeline/`](pipeline/), not in an
agent prompt.

---

## Modules

| File | Responsibility |
|---|---|
| `crew.py` | `@CrewBase` definition. Builds the live agents and their tasks from the YAML config, resolves each agent's LLM, applies the call timeout. |
| `main.py` | The pipeline itself. Runs the phases in order and holds the branch that decides whether follow-up questions are needed at all. |
| `server.py` | FastAPI app. Serves the UI, streams run progress, exposes the diagnosis endpoints. The largest file in the package. |
| `answer_collector.py` | Collects answers to follow-up questions. Abstract base with two implementations: interactive user input, and the batched simulated patient used in evaluation. Normalises free text into `yes` / `no` / `unsure`. |
| `followup_sanitizer.py` | Deterministic guard between the interviewer agent and the scoring stage. Builds the set of symptoms the patient already reported and drops any question that asks about them again. |
| `utils.py` | Small parsing helpers, notably `extract_first_json_object` for recovering a JSON payload from model output that may carry prose or code fences. |
| `compose_entrypoint.py` | Docker entry point. `normal` starts uvicorn, `test` runs the batch evaluation harness. |
| `frontend.html` | Single-page UI for an interactive consultation. |
| `config/agents.yaml` | Agent roles, goals and backstories. |
| `config/tasks.yaml` | Task descriptions: extraction rules, selection policy, output schemas. |

---

## Pipeline phases

`main.py` composes a run from these steps:

| Function | What it does |
|---|---|
| `_run_disease_crew` | Symptom extraction, then symptom resolution and disease retrieval. |
| `_run_deterministic_ranking` | Calls `rank_diagnoses`. No model involved. |
| `_slim_ranking_for_followup` | Trims the ranking to what the interviewer actually needs, so the prompt stays small. |
| `_run_followup_crew` | Profiles the remaining candidates, then asks the interviewer for questions. |
| `_run_refine_phase` | Runs `RefineScoring` over the answers, then hands the computed numbers to the refiner agent for narration. |
| `run_diagnostic_pipeline` | The async entry point used by both the server and the evaluation harness. |

---

## Where the prompts live

Agent behaviour is split deliberately across two files:

- `config/agents.yaml` holds the **stable identity** of an agent: its role,
  what it must never do, how it classifies things.
- `config/tasks.yaml` holds the **per-run instructions**: extraction rules,
  banned-word lists, mapping tables, output schemas, the self-check.

If you are changing what an agent is allowed to output, you probably want
`tasks.yaml`.

---

## Adding an agent

1. Add the entry to `config/agents.yaml` and a matching task to `config/tasks.yaml`.
2. Register both in `crew.py` with the `@agent` and `@task` decorators.
3. If the agent needs facts from the knowledge base, **do not give it a tool.**
   Fetch the data in `main.py` and pass it into the task description, the way
   `_run_followup_crew` passes disease profiles. That is what keeps knowledge
   access uniform and inspectable.
