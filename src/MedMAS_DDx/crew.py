import logging
import os

from crewai import Agent, Crew, LLM, Process, Task
from crewai.project import CrewBase, agent, crew, task
from crewai.agents.agent_builder.base_agent import BaseAgent

_LLM_TIMEOUT_S = int(os.getenv("MEDMAS_DDX_LLM_TIMEOUT", "300"))


@CrewBase
class MedMAS_DDx():


    agents: list[BaseAgent]
    tasks: list[Task]

    runtime_log_file: str | None = None

    def _agent_config(self, key: str, env_var: str) -> dict:
       
        base_cfg = dict(self.agents_config[key])
        override = os.getenv(env_var, "").strip()
        fallback = os.getenv("MEDMAS_DDX_LLM", "").strip()
        if override:
            model = override
        elif fallback:
            model = fallback
        else:
            model = base_cfg.get("llm", "")

        if model:
            base_cfg["llm"] = LLM(model=model, timeout=_LLM_TIMEOUT_S)
        return base_cfg


    @agent
    def symptom_extractor(self) -> Agent:
        return Agent(
            config=self._agent_config("symptom_extractor", "SYMPTOM_EXTRACTOR_LLM"),
            tools=[],
            verbose=True,
            allow_delegation=False,
            max_iter=2,
            max_retry_limit=2,
        )

    @agent
    def followup_interviewer(self) -> Agent:
   
        return Agent(
            config=self._agent_config("followup_interviewer", "FOLLOWUP_INTERVIEWER_LLM"),
            tools=[],
            verbose=True,
            allow_delegation=False,
            max_iter=4,
            max_retry_limit=2,
        )

    def patient_llm(self) -> LLM:
       
        cfg = self._agent_config("patient_simulator", "PATIENT_LLM")
        llm = cfg.get("llm")
        if not isinstance(llm, LLM): 
            llm = LLM(model="gpt-4o-mini", timeout=_LLM_TIMEOUT_S)
        return llm

    def patient_persona(self) -> dict[str, str]:
        base_cfg = dict(self.agents_config.get("patient_simulator", {}))
        return {
            "role":      str(base_cfg.get("role",      "") or "").strip(),
            "goal":      str(base_cfg.get("goal",      "") or "").strip(),
            "backstory": str(base_cfg.get("backstory", "") or "").strip(),
        }

    @agent
    def diagnosis_refiner(self) -> Agent:
        return Agent(
            config=self._agent_config("diagnosis_refiner", "DIAGNOSIS_REFINER_LLM"),
            tools=[],
            verbose=True,
            allow_delegation=False,
            max_iter=3,
            max_retry_limit=2,
        )


    @task
    def extract_symptoms_task(self) -> Task:
        return Task(
            config=self.tasks_config['extract_symptoms_task'],
            agent=self.symptom_extractor(),
        )

    def clarify_followup_symptoms_task_dynamic(
        self,
        ranking_json: str,
        mapped_symptom_labels_json: str,
        disease_profiles_json: str,
    ) -> Task:
        
        base_cfg = dict(self.tasks_config['clarify_followup_symptoms_task'])
        base_cfg['description'] = (
            base_cfg.get('description', '').rstrip()
            + "\n\n"
            + "── DIFFERENTIAL DIAGNOSIS / RANKING (JSON) ─────────────────────\n"
            + ranking_json
            + "\n\n"
            + "── MAPPED SYMP SYMPTOM LABELS (JSON) ──────────────────────────\n"
            + mapped_symptom_labels_json
            + "\n\n"
            + "── DISEASE SYMPTOM PROFILES (pre-fetched by Python) ────────────\n"
            + disease_profiles_json
        )
        return Task(
            description=base_cfg['description'],
            expected_output=base_cfg.get('expected_output', ''),
            agent=self.followup_interviewer(),
        )

    def refine_diagnosis_task_dynamic(
        self,
        ranking_json: str,
        followup_json: str,
        scores_json: str,
    ) -> Task:
        base_cfg = dict(self.tasks_config['refine_diagnosis_task'])
        base_cfg['description'] = (
            base_cfg.get('description', '').rstrip()
            + "\n\n"
            + "── INITIAL RANKING (JSON) ──────────────────────────────────────\n"
            + ranking_json
            + "\n\n"
            + "── FOLLOW-UP ANSWERS (JSON) ────────────────────────────────────\n"
            + followup_json
            + "\n\n"
            + "── PRE-COMPUTED SCORES (Python refine_scoring result) ───────────\n"
            + scores_json
        )
        return Task(
            description=base_cfg['description'],
            expected_output=base_cfg.get('expected_output', ''),
            agent=self.diagnosis_refiner(),
        )


    @crew
    def crew(self) -> Crew:
        return self.disease_crew()

    def disease_crew(self) -> Crew:
        
        return Crew(
            agents=[self.symptom_extractor()],
            tasks=[self.extract_symptoms_task()],
            process=Process.sequential,
            verbose=True,
            output_log_file=self.runtime_log_file,
        )

    def followup_crew(
        self,
        ranking_json: str,
        mapped_symptom_labels_json: str,
        disease_profiles_json: str,
    ) -> Crew:
    
        return Crew(
            agents=[self.followup_interviewer()],
            tasks=[
                self.clarify_followup_symptoms_task_dynamic(
                    ranking_json,
                    mapped_symptom_labels_json,
                    disease_profiles_json,
                ),
            ],
            process=Process.sequential,
            verbose=True,
            output_log_file=self.runtime_log_file,
        )

    def refine_crew(
        self,
        ranking_json: str,
        followup_json: str,
        scores_json: str,
    ) -> Crew:
       
        return Crew(
            agents=[self.diagnosis_refiner()],
            tasks=[
                self.refine_diagnosis_task_dynamic(ranking_json, followup_json, scores_json)
            ],
            process=Process.sequential,
            verbose=True,
            output_log_file=self.runtime_log_file,
        )
