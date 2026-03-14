"""
Pipeline Orchestrator — lightweight coordinator for the 5-agent KYC system.

Manages the flow:
  Customer answer
      → Fact Extractor (extract claims)
      → Verification Engine (run checks in background)
      → Assessor (evaluate results, produce probing directives)
      → Interviewer (next question, guided by directives)

The Orchestrator owns no business logic — it only passes data between agents
and manages timing (what runs in parallel vs sequential).
"""
import json
import os
import threading
import asyncio
from datetime import datetime
from typing import Optional

import anthropic

from models import KYCCase, KYCPhase
from agents.interviewer import Interviewer
from agents.fact_extractor import FactExtractor
from agents.assessor import Assessor
from agents.verification_engine import VerificationEngine
from agents.risk_analyst import run_risk_assessment
from agents.investigator import BackgroundInvestigator, check_sanctions

# ─── Model configuration ───
MODEL_FULL = "claude-sonnet-4-20250514"
MODEL_FAST = "claude-haiku-4-5-20251001"
MODEL_BG   = "claude-haiku-4-5-20251001"


class KYCOrchestrator:
    """Lightweight pipeline coordinator for the 5-agent KYC system."""

    def __init__(self, case: KYCCase, case_file_path: str):
        self.case = case
        self.case_file_path = case_file_path

        # ── The 5 agents ──
        self.interviewer = Interviewer()
        self.fact_extractor = FactExtractor()
        self.verification_engine = VerificationEngine()
        self.assessor = Assessor()
        # Risk Analyst is a function, not a stateful agent

        # ── Legacy investigator (for sanctions — will be migrated) ──
        self.investigator = BackgroundInvestigator()

        # ── State ──
        self.interview_complete = False
        self.reasoning_log: list[dict] = []
        self.qa_log: list[dict] = []
        self._last_question: Optional[str] = None
        self.paste_flags: list[dict] = []

        # ── Background threads ──
        self._bg_verification_thread: Optional[threading.Thread] = None
        self._bg_verification_results: list[dict] = []
        self._bg_assessment_thread: Optional[threading.Thread] = None
        self._bg_assessment_result: Optional[dict] = None
        self._initial_checks_thread: Optional[threading.Thread] = None
        self._initial_check_results: Optional[dict] = None
        self._lock = threading.Lock()

    # ═══════════════════════════════════════════
    # PUBLIC API (called by web_app.py)
    # ═══════════════════════════════════════════

    async def start_interview(self, business_stage: str = "") -> str:
        """Start the interview — generate greeting and first question."""
        person_name = self.case.person.full_name
        company_name = self.case.business.company_name

        greeting = await self.interviewer.start_interview(
            person_name=person_name,
            company_name=company_name,
            business_stage=business_stage,
        )

        self._last_question = greeting
        return greeting

    async def process_customer_input(
        self,
        user_input: str,
        pasted: bool = False,
        keystroke_ratio: float = 1.0,
    ) -> str:
        """Process customer's answer through the full pipeline.

        Pipeline:
        1. Interviewer tracks input behavior
        2. Fact Extractor extracts claims (fast, Haiku)
        3. Verification Engine runs checks (background, async)
        4. Assessor evaluates (background, after verification)
        5. Feed directives to Interviewer
        6. Interviewer generates next question

        Steps 2-4 run in background — Interviewer responds with whatever
        directives are available from PREVIOUS turns.
        """
        # ── 0. Collect background results from previous turn ──
        self._collect_background_results()

        # ── 1. Send new directives from previous assessment to Interviewer ──
        new_directives = self.assessor.get_new_directives()
        if new_directives:
            self.interviewer.add_directives(new_directives)

        # ── 2. On first answer — launch initial checks in background ──
        if len(self.qa_log) == 0:
            self._launch_initial_checks()

        # ── 3. Start fact extraction + verification in background ──
        question_context = self._last_question or ""
        self._launch_background_pipeline(user_input, question_context)

        # ── 4. Generate next question via Interviewer ──
        result = await self.interviewer.process_answer(
            customer_answer=user_input,
            pasted=pasted,
            keystroke_ratio=keystroke_ratio,
        )

        # Handle different return formats
        if isinstance(result, tuple):
            message, reasoning, data_to_save = result
        else:
            message = result
            reasoning = None
            data_to_save = None

        # ── 5. Record Q&A and update state ──
        self.qa_log.append({"q": question_context, "a": user_input})

        if reasoning:
            self._add_reasoning(reasoning)

        if data_to_save:
            self._save_case_data(data_to_save)

        # Sync interview_complete from interviewer
        if self.interviewer.interview_complete:
            self.interview_complete = True

        self._last_question = message

        # Save case
        self._save_case()

        return message

    async def process_document_upload(self, file_path: str, doc_type: str) -> str:
        """Process a document upload."""
        # For now, delegate to a simple handler
        from tools import document_tools
        client = anthropic.AsyncAnthropic()
        result = await document_tools.analyze_document(file_path, doc_type, client)
        self.case.documents.append({
            "doc_type": doc_type,
            "file_path": file_path,
            "upload_time": datetime.now().isoformat(),
            "extracted_data": result.get("extracted_data", {}),
            "analysis_notes": result.get("raw_response", ""),
            "verified": False,
        })
        self._save_case()
        return result.get("raw_response", "Document analyzed.")

    async def run_assessment(self) -> dict:
        """Run the final risk assessment after interview completes."""
        # Collect any remaining background results
        self._wait_for_background()

        # Gather all data for the Risk Analyst
        return await run_risk_assessment(
            case=self.case,
            reasoning_log=self.interviewer.reasoning_log,
            assessor_findings=self.assessor.get_findings_for_risk_analyst(),
            verification_engine_summary=self.verification_engine.get_summary(),
            verification_results=self.verification_engine.all_results,
            sanctions_results=self.investigator.sanctions_results if hasattr(self.investigator, 'sanctions_results') else None,
        )

    # ═══════════════════════════════════════════
    # BACKGROUND PIPELINE
    # ═══════════════════════════════════════════

    def _launch_background_pipeline(self, answer: str, question_context: str):
        """Launch fact extraction → verification → assessment in background."""
        def _run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(
                    self._background_pipeline(answer, question_context)
                )
            except Exception as e:
                print(f"[Orchestrator] Background pipeline error: {e}")
            finally:
                loop.close()

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        self._bg_verification_thread = thread

    async def _background_pipeline(self, answer: str, question_context: str):
        """The actual background pipeline: extract → verify → assess."""

        # Step 1: Extract facts
        conversation_summary = self._get_conversation_summary()
        facts = await self.fact_extractor.extract_facts(
            customer_answer=answer,
            question_context=question_context,
            conversation_summary=conversation_summary,
        )

        if not facts:
            return

        # Step 2: Plan and run verification checks
        business_context = self.fact_extractor.get_business_context()
        # Add case data to context
        business_context.update({
            "company_name": business_context.get("company_name") or self.case.business.company_name,
            "person_name": business_context.get("person_name") or self.case.person.full_name,
            "company_number": business_context.get("company_number") or self.case.business.company_number,
            "industry": business_context.get("industry") or self.case.business.industry_sector,
        })

        planned_checks = await self.verification_engine.plan_checks(
            facts=facts,
            business_context=business_context,
        )

        if planned_checks:
            results = await self.verification_engine.execute_checks(planned_checks)

            # Run cross-reference checks after individual checks
            cross_ref_results = await self.verification_engine.run_cross_reference_checks(
                business_context=business_context,
            )
            results.extend(cross_ref_results)

            # Step 3: Assess results
            assessment = await self.assessor.assess(
                facts=facts,
                verification_results=results,
                conversation_context=conversation_summary,
            )

            with self._lock:
                self._bg_assessment_result = assessment
                self._bg_verification_results.extend(results)

            # Store verification results in case
            for r in results:
                if r.get("status") == "completed":
                    self.case.verifications.append({
                        "source": r.get("check_id", "unknown"),
                        "query": json.dumps(r.get("params", {}), default=str)[:200],
                        "result": r.get("result", {}),
                        "timestamp": r.get("timestamp", datetime.now().isoformat()),
                    })

        # Also run sanctions check on any new counterparties
        await self._check_new_counterparties(facts)

    async def _check_new_counterparties(self, facts: list[dict]):
        """Run sanctions checks on newly mentioned counterparties."""
        counterparty_types = {"supplier", "client_name", "partner", "counterparty", "person_name"}
        for fact in facts:
            if fact.get("type") in counterparty_types:
                name = fact.get("value", "")
                if name and len(name) > 2:
                    try:
                        result = await check_sanctions(name)
                        if result.get("sanctioned") is True:
                            self.case.red_flags.append({
                                "severity": "critical",
                                "description": f"SANCTIONS HIT: {name}",
                                "evidence": json.dumps(result.get("matches", [])[:2], default=str)[:300],
                                "timestamp": datetime.now().isoformat(),
                            })
                        elif result.get("sanctioned") == "possible_match":
                            self.case.red_flags.append({
                                "severity": "high",
                                "description": f"Possible sanctions match: {name}",
                                "evidence": json.dumps(result.get("matches", [])[:2], default=str)[:300],
                                "timestamp": datetime.now().isoformat(),
                            })
                    except Exception:
                        pass

    def _launch_initial_checks(self):
        """Launch initial background checks on first answer."""
        person_name = self.case.person.full_name
        company_name = self.case.business.company_name

        if not person_name and not company_name:
            return

        def _run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(self._run_initial_checks(person_name, company_name))
            except Exception as e:
                print(f"[Orchestrator] Initial checks error: {e}")
            finally:
                loop.close()

        self._initial_checks_thread = threading.Thread(target=_run, daemon=True)
        self._initial_checks_thread.start()

    async def _run_initial_checks(self, person_name: str, company_name: str):
        """Run initial checks: sanctions + basic company search."""
        # Sanctions
        if person_name:
            result = await check_sanctions(person_name)
            if result.get("sanctioned") is True:
                self.case.red_flags.append({
                    "severity": "critical",
                    "description": f"SANCTIONS HIT on applicant: {person_name}",
                    "evidence": json.dumps(result.get("matches", [])[:2], default=str)[:300],
                    "timestamp": datetime.now().isoformat(),
                })

        if company_name:
            result = await check_sanctions(company_name, entity_type="company")
            if result.get("sanctioned") is True:
                self.case.red_flags.append({
                    "severity": "critical",
                    "description": f"SANCTIONS HIT on company: {company_name}",
                    "evidence": json.dumps(result.get("matches", [])[:2], default=str)[:300],
                    "timestamp": datetime.now().isoformat(),
                })

        # Initial verification engine checks (company name + person name)
        initial_facts = []
        if person_name:
            initial_facts.append({"type": "person_name", "value": person_name})
        if company_name:
            initial_facts.append({"type": "company_name", "value": company_name})

        business_context = {
            "person_name": person_name,
            "company_name": company_name,
            "company_number": self.case.business.company_number,
            "industry": self.case.business.industry_sector,
        }

        planned = await self.verification_engine.plan_checks(
            facts=initial_facts,
            business_context=business_context,
        )
        if planned:
            results = await self.verification_engine.execute_checks(planned)
            for r in results:
                if r.get("status") == "completed":
                    self.case.verifications.append({
                        "source": r.get("check_id", "unknown"),
                        "query": json.dumps(r.get("params", {}), default=str)[:200],
                        "result": r.get("result", {}),
                        "timestamp": r.get("timestamp", datetime.now().isoformat()),
                    })

    # ═══════════════════════════════════════════
    # BACKGROUND RESULT COLLECTION
    # ═══════════════════════════════════════════

    def _collect_background_results(self):
        """Non-blocking collection of background results."""
        # Check if verification thread finished
        if self._bg_verification_thread and not self._bg_verification_thread.is_alive():
            with self._lock:
                if self._bg_assessment_result:
                    # New directives will be picked up on next process_customer_input
                    self._bg_assessment_result = None
            self._bg_verification_thread = None

        # Check initial checks
        if self._initial_checks_thread and not self._initial_checks_thread.is_alive():
            self._initial_checks_thread = None

    def _wait_for_background(self, timeout: float = 30.0):
        """Wait for all background threads to complete."""
        if self._bg_verification_thread and self._bg_verification_thread.is_alive():
            self._bg_verification_thread.join(timeout=timeout)
        if self._initial_checks_thread and self._initial_checks_thread.is_alive():
            self._initial_checks_thread.join(timeout=timeout)

    # ═══════════════════════════════════════════
    # STATE MANAGEMENT
    # ═══════════════════════════════════════════

    def _save_case_data(self, data_to_save: dict):
        """Update case data from interviewer output."""
        section = data_to_save.get("section", "")
        data = data_to_save.get("data", {})

        if section == "business":
            for key, value in data.items():
                if hasattr(self.case.business, key):
                    setattr(self.case.business, key, value)
        elif section == "activity":
            for key, value in data.items():
                if hasattr(self.case.activity, key):
                    setattr(self.case.activity, key, value)

        self.case.updated_at = datetime.now().isoformat()

    def _add_reasoning(self, reasoning: dict):
        """Add a reasoning entry to the log."""
        reasoning["timestamp"] = datetime.now().isoformat()
        self.reasoning_log.append(reasoning)

    def _get_conversation_summary(self) -> str:
        """Build a brief summary of the conversation so far."""
        if not self.qa_log:
            return "Interview just started."

        lines = []
        for qa in self.qa_log[-5:]:  # Last 5 Q&As
            lines.append(f"Q: {qa['q'][:100]}")
            lines.append(f"A: {qa['a'][:200]}")
        return "\n".join(lines)

    def _save_case(self):
        """Save case to disk."""
        try:
            self.case.save(self.case_file_path)
        except Exception:
            pass

    # ═══════════════════════════════════════════
    # COMPATIBILITY API (for web_app.py)
    # ═══════════════════════════════════════════

    def get_reasoning_log(self) -> list:
        """Return the combined reasoning log from Interviewer + Orchestrator."""
        return self.interviewer.reasoning_log + self.reasoning_log

    def get_reasoning_report(self) -> str:
        """Generate a readable reasoning report."""
        all_reasoning = self.get_reasoning_log()
        lines = []
        lines.append("=" * 70)
        lines.append("KYC AGENT — INTERNAL REASONING LOG")
        lines.append(f"Case: {self.case.case_id}")
        lines.append(f"Generated: {datetime.now().isoformat()}")
        lines.append(f"Architecture: 5-agent pipeline")
        lines.append("=" * 70)
        lines.append("")

        for i, entry in enumerate(all_reasoning, 1):
            lines.append(f"--- Step {i} [{entry.get('timestamp', '')}] ---")
            if entry.get("note"):
                lines.append(f"NOTE: {entry['note']}")
            suspicion = entry.get("suspicion", "")
            if suspicion:
                lines.append(f"SUSPICION: {suspicion}")
            if entry.get("why"):
                lines.append(f"REASON: {entry['why']}")
            lines.append("")

        lines.append("=" * 70)
        lines.append(f"RED FLAGS ({len(self.case.red_flags)}):")
        for flag in self.case.red_flags:
            lines.append(f"  [{flag.get('severity', '').upper()}] {flag.get('description', '')}")
        lines.append("")

        lines.append(f"VERIFICATIONS ({len(self.case.verifications)}):")
        for v in self.case.verifications:
            lines.append(f"  [{v.get('source', '')}] {v.get('query', '')[:80]}")
        lines.append("")

        # Verification Engine summary
        ve_summary = self.verification_engine.get_summary()
        lines.append(f"VERIFICATION ENGINE: {ve_summary.get('total_checks_run', 0)} checks run")
        for finding in ve_summary.get("notable_findings", []):
            lines.append(f"  ! {finding}")
        lines.append("")

        # Assessor summary
        as_summary = self.assessor.get_assessment_summary()
        lines.append(f"ASSESSOR: {as_summary.get('total', 0)} assessments "
                     f"({as_summary.get('confirmed', 0)} confirmed, "
                     f"{as_summary.get('contradicted', 0)} contradicted, "
                     f"{as_summary.get('suspicious', 0)} suspicious)")
        lines.append("")

        lines.append("COLLECTED DATA:")
        lines.append(json.dumps(self.case.to_dict(), indent=2, ensure_ascii=False))

        return "\n".join(lines)
