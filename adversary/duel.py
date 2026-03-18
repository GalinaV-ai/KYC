"""
Duel Orchestrator — runs the adversary agent against the KYC pipeline.

This module bridges the FraudsterAgent (OpenAI) with the KYC Orchestrator (Anthropic).
The fraudster sees ONLY interviewer messages. It has NO access to:
  - KYC prompts or system messages
  - Verification results
  - Reasoning log
  - Risk assessment logic

The duel flow:
  1. Generate/set a legend for the fraudster
  2. KYC starts interview → greeting message
  3. Fraudster responds to greeting
  4. Loop: KYC processes answer → asks next question → Fraudster responds
  5. Until interview ends → KYC runs assessment
  6. Return full transcript + assessment for display
"""

import os
import json
import uuid
import asyncio
from datetime import datetime
from typing import Optional, AsyncGenerator

# KYC pipeline imports (used as a black box — we only call public interfaces)
from models import KYCCase
from agents.orchestrator import KYCOrchestrator

# Adversary imports (isolated)
from adversary.fraudster import FraudsterAgent
from adversary.doc_generator import generate_fake_document


CASES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cases")


class DuelEvent:
    """A single event in the duel timeline."""
    def __init__(self, event_type: str, source: str, content: str,
                 metadata: Optional[dict] = None):
        self.event_type = event_type  # "message", "legend", "document", "reasoning", "assessment"
        self.source = source          # "kyc", "fraudster", "system"
        self.content = content
        self.metadata = metadata or {}
        self.timestamp = datetime.now().isoformat()

    def to_dict(self) -> dict:
        return {
            "event_type": self.event_type,
            "source": self.source,
            "content": self.content,
            "metadata": self.metadata,
            "timestamp": self.timestamp,
        }


class DuelOrchestrator:
    """Runs a fraudster vs KYC agent duel."""

    def __init__(self):
        self.fraudster: Optional[FraudsterAgent] = None
        self.kyc_orchestrator: Optional[KYCOrchestrator] = None
        self.case: Optional[KYCCase] = None
        self.case_id: Optional[str] = None
        self.events: list[DuelEvent] = []
        self.is_running = False
        self.is_complete = False
        self.doc_output_dir: Optional[str] = None

    def setup(self, legend: Optional[dict] = None, hints: Optional[str] = None):
        """Initialize both agents. Generate legend if not provided."""
        # ── Fraudster setup ──
        self.fraudster = FraudsterAgent()

        if legend:
            self.fraudster.set_legend(legend)
        else:
            self.fraudster.generate_legend(hints=hints)

        self.events.append(DuelEvent(
            "legend", "fraudster",
            f"Legend created: {self.fraudster.get_legend_summary()}",
            metadata={"legend": self.fraudster.legend},
        ))

        # ── KYC setup ──
        # Use standard KYC case ID format — the KYC agent must NOT know it's a duel
        self.case_id = f"KYC-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
        self.case = KYCCase(
            case_id=self.case_id,
            created_at=datetime.now().isoformat(),
            updated_at=datetime.now().isoformat(),
            current_phase="business_basics",
        )
        self.case.person.full_name = self.fraudster.get_customer_name()
        self.case.business.company_name = self.fraudster.get_company_name()

        case_path = os.path.join(CASES_DIR, f"{self.case_id}.json")
        os.makedirs(CASES_DIR, exist_ok=True)
        self.case.save(case_path)

        self.kyc_orchestrator = KYCOrchestrator(self.case, case_path)

        # Doc output dir
        self.doc_output_dir = os.path.join(CASES_DIR, f"{self.case_id}_docs")
        os.makedirs(self.doc_output_dir, exist_ok=True)

    async def run_duel(self) -> AsyncGenerator[DuelEvent, None]:
        """Run the full duel as an async generator yielding events."""
        if not self.fraudster or not self.kyc_orchestrator:
            raise ValueError("Call setup() first")

        self.is_running = True

        try:
            # ── Step 1: KYC starts interview ──
            greeting = await self.kyc_orchestrator.start_interview(business_stage="existing")
            event = DuelEvent("message", "kyc", greeting)
            self.events.append(event)
            yield event

            # ── Step 2: Duel loop ──
            max_turns = 40  # Safety limit
            turn = 0

            while turn < max_turns and not self.kyc_orchestrator.interview_complete:
                turn += 1

                # Fraudster responds to the last KYC message
                kyc_message = self.events[-1].content
                fraudster_reply = self.fraudster.respond(kyc_message)

                event = DuelEvent("message", "fraudster", fraudster_reply)
                self.events.append(event)
                yield event

                # Check if fraudster offers a document
                if self.fraudster.should_offer_document(kyc_message):
                    try:
                        doc_type = self._infer_doc_type(kyc_message)
                        doc_path = generate_fake_document(
                            doc_type=doc_type,
                            legend=self.fraudster.legend,
                            output_dir=self.doc_output_dir,
                            context=kyc_message,
                        )
                        doc_event = DuelEvent(
                            "document", "fraudster",
                            f"Generated fake {doc_type}: {os.path.basename(doc_path)}",
                            metadata={"doc_type": doc_type, "path": doc_path},
                        )
                        self.events.append(doc_event)
                        yield doc_event

                        # Upload to KYC
                        doc_response = await self.kyc_orchestrator.process_document_upload(
                            doc_path, doc_type
                        )
                        doc_ack = DuelEvent("message", "kyc", doc_response,
                                            metadata={"type": "doc_acknowledgement"})
                        self.events.append(doc_ack)
                        yield doc_ack
                    except Exception as e:
                        err_event = DuelEvent("system", "system",
                                              f"Document generation failed: {e}")
                        self.events.append(err_event)
                        yield err_event

                # Yield reasoning snapshot
                reasoning = self.kyc_orchestrator.get_reasoning_log()
                if reasoning:
                    latest = reasoning[-1] if reasoning else {}
                    r_event = DuelEvent("reasoning", "kyc", json.dumps(latest, ensure_ascii=False),
                                        metadata={"full_log_length": len(reasoning)})
                    self.events.append(r_event)
                    yield r_event

                # KYC processes the fraudster's answer
                kyc_response = await self.kyc_orchestrator.process_customer_input(fraudster_reply)

                if self.kyc_orchestrator.interview_complete:
                    # The response may be a farewell — still yield it
                    event = DuelEvent("message", "kyc", kyc_response,
                                      metadata={"interview_complete": True})
                    self.events.append(event)
                    yield event
                    break

                event = DuelEvent("message", "kyc", kyc_response)
                self.events.append(event)
                yield event

            # ── Step 3: Run assessment ──
            self.kyc_orchestrator.interview_complete = True
            assessment = await self.kyc_orchestrator.run_assessment()

            assessment_event = DuelEvent(
                "assessment", "kyc",
                json.dumps(assessment, indent=2, ensure_ascii=False),
                metadata={"decision": assessment.get("decision", "unknown")},
            )
            self.events.append(assessment_event)
            yield assessment_event

        finally:
            self.is_running = False
            self.is_complete = True

    def _infer_doc_type(self, message: str) -> str:
        """Infer what type of document the interviewer is asking for."""
        lower = message.lower()
        if "invoice" in lower:
            return "invoice"
        elif "contract" in lower or "agreement" in lower:
            return "service_agreement"
        elif "bank statement" in lower:
            return "bank_statement"
        elif "receipt" in lower:
            return "receipt"
        elif "certificate" in lower:
            return "certificate"
        else:
            return "business_document"

    def get_transcript(self) -> list[dict]:
        """Return all events as a list of dicts."""
        return [e.to_dict() for e in self.events]

    def get_messages_only(self) -> list[dict]:
        """Return only chat messages (no reasoning/system events)."""
        return [
            e.to_dict() for e in self.events
            if e.event_type == "message"
        ]

    def get_decision(self) -> Optional[str]:
        """Return the final KYC decision, if available."""
        for e in reversed(self.events):
            if e.event_type == "assessment":
                return e.metadata.get("decision", "unknown")
        return None
