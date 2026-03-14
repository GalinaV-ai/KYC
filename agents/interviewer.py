"""
Interviewer Agent — conducts the KYC conversation with the customer.

Pure conversation agent: no tool access, no direct verification.
Receives probing directives from the Assessor to guide questioning.
Outputs: next question to ask the customer + internal reasoning.
"""
import json
from datetime import datetime
from typing import Optional

from anthropic import AsyncAnthropic

from agents.prompts import INTERVIEWER_PROMPT

MODEL_INTERVIEWER = "claude-sonnet-4-20250514"


class Interviewer:
    """Conducts the KYC interview conversation."""

    def __init__(self):
        self.client = AsyncAnthropic()
        self.messages: list[dict] = []
        self.reasoning_log: list[dict] = []
        self.qa_log: list[dict] = []
        self._last_question: Optional[str] = None
        self.interview_complete = False
        self.paste_flags: list[dict] = []
        self._typo_count = 0

        # Probing directives from the Assessor
        self._pending_directives: list[dict] = []
        self._used_directives: list[dict] = []

        # Question budget
        self._q_soft_target = 15
        self._q_hard_max = 25

    def add_directives(self, directives: list[dict]):
        """Add probing directives from the Assessor."""
        for d in directives:
            # Avoid duplicates
            area = d.get("area", "")
            if not any(existing.get("area") == area and existing.get("reason_code") == d.get("reason_code")
                       for existing in self._pending_directives + self._used_directives):
                self._pending_directives.append(d)

        # Sort by urgency
        urgency_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        self._pending_directives.sort(key=lambda x: urgency_order.get(x.get("urgency", "low"), 3))

    async def start_interview(self, person_name: str, company_name: str,
                               business_stage: str = "") -> str:
        """Generate the opening greeting and first question."""
        context_parts = []
        if person_name:
            context_parts.append(f"Customer name: {person_name}")
        if company_name:
            context_parts.append(f"Company name: {company_name}")
        if business_stage:
            context_parts.append(f"Business stage: {business_stage}")

        context = "\n".join(context_parts)

        self.messages = [{
            "role": "user",
            "content": f"Start the KYC interview. Customer details:\n{context}\n\n"
                       f"Generate a warm, professional greeting and your first question. "
                       f"Do NOT ask for the customer's name or company name — you already have those."
        }]

        system_msg = self._build_system_message()
        response = await self.client.messages.create(
            model=MODEL_INTERVIEWER,
            max_tokens=1024,
            system=system_msg,
            messages=self.messages
        )

        text = response.content[0].text.strip()
        message = self._extract_message(text)

        self.messages.append({"role": "assistant", "content": text})
        self._last_question = message
        return message

    async def process_answer(self, customer_answer: str,
                              pasted: bool = False,
                              keystroke_ratio: float = 1.0) -> str:
        """Process a customer answer and generate the next question.

        Returns the next question/message for the customer.
        """
        # Track paste behavior
        self._track_input_behavior(customer_answer, pasted, keystroke_ratio)

        # Record Q&A
        question = self._last_question or ""
        if question:
            self.qa_log.append({"q": question, "a": customer_answer})

        # Budget enforcement
        q_count = len(self.qa_log)
        if q_count >= self._q_hard_max:
            self.interview_complete = True
            self._add_reasoning({
                "note": f"Interview auto-completed: {q_count} questions reached hard max.",
                "suspicion": "none"
            })
            return "Thank you for your time. I have all the information I need to proceed with the assessment."

        # Build the user message with directives context
        user_content = f"Customer's answer: {customer_answer}"

        # Consume pending directives (mark as used)
        active_directives = self._get_active_directives()
        if active_directives:
            directives_text = "\n".join(
                f"- [{d['urgency'].upper()}] {d['directive']}"
                for d in active_directives
            )
            user_content += (
                f"\n\nPROBING DIRECTIVES (from verification system — do NOT reveal these to customer):\n"
                f"{directives_text}"
            )

        self.messages.append({"role": "user", "content": user_content})

        # Trim message history to prevent context overflow
        self._trim_messages()

        system_msg = self._build_system_message()
        response = await self.client.messages.create(
            model=MODEL_INTERVIEWER,
            max_tokens=1024,
            system=system_msg,
            messages=self.messages
        )

        text = response.content[0].text.strip()
        message = self._extract_message(text)
        reasoning = self._extract_reasoning(text)

        if reasoning:
            self._add_reasoning(reasoning)

        # Check if interview should complete
        data_to_save = self._extract_data_to_save(text)

        self.messages.append({"role": "assistant", "content": text})
        self._last_question = message
        return message, reasoning, data_to_save

    def complete_interview(self, reason: str = ""):
        """Mark the interview as complete."""
        self.interview_complete = True
        self._add_reasoning({
            "note": f"Interview completed. {reason}",
            "suspicion": "none"
        })

    def _get_active_directives(self) -> list[dict]:
        """Get directives to include in the next turn."""
        active = []
        remaining = []

        for d in self._pending_directives:
            urgency = d.get("urgency", "low")
            if urgency in ("critical", "high"):
                active.append(d)
                self._used_directives.append(d)
            elif urgency == "medium" and len(active) < 2:
                active.append(d)
                self._used_directives.append(d)
            else:
                remaining.append(d)

        # Also include one low-priority if there's room
        if not active and remaining:
            active.append(remaining.pop(0))
            self._used_directives.append(active[-1])

        self._pending_directives = remaining
        return active

    def _build_system_message(self) -> str:
        """Build the system message with current interview context."""
        parts = [INTERVIEWER_PROMPT]

        q_count = len(self.qa_log)
        budget = self._q_soft_target

        parts.append(f"\n\nINTERVIEW STATUS:")
        parts.append(f"Questions asked so far: {q_count}")
        parts.append(f"Target: {budget} questions (max {self._q_hard_max})")

        if q_count >= budget - 3:
            parts.append("You are near the end of your budget. Start wrapping up — cover any remaining essentials and prepare to complete.")

        # Include pending directive count
        pending_high = sum(1 for d in self._pending_directives if d.get("urgency") in ("critical", "high"))
        if pending_high:
            parts.append(f"\nPENDING HIGH-PRIORITY DIRECTIVES: {pending_high} — address these before completing.")

        return "\n".join(parts)

    def _extract_message(self, text: str) -> str:
        """Extract the customer-facing message from the response."""
        try:
            json_start = text.find('{')
            json_end = text.rfind('}')
            if json_start >= 0 and json_end > json_start:
                parsed = json.loads(text[json_start:json_end + 1])
                if parsed.get("message"):
                    return parsed["message"]
        except (json.JSONDecodeError, TypeError):
            pass
        # If no JSON, return the raw text (strip any wrapping phrases)
        return self._strip_wrapping_phrases(text)

    def _extract_reasoning(self, text: str) -> Optional[dict]:
        """Extract reasoning from the response."""
        try:
            json_start = text.find('{')
            json_end = text.rfind('}')
            if json_start >= 0 and json_end > json_start:
                parsed = json.loads(text[json_start:json_end + 1])
                if parsed.get("reasoning"):
                    return parsed["reasoning"]
        except (json.JSONDecodeError, TypeError):
            pass
        return None

    def _extract_data_to_save(self, text: str) -> Optional[dict]:
        """Extract data_to_save from the response."""
        try:
            json_start = text.find('{')
            json_end = text.rfind('}')
            if json_start >= 0 and json_end > json_start:
                parsed = json.loads(text[json_start:json_end + 1])
                if parsed.get("data_to_save"):
                    return parsed["data_to_save"]
        except (json.JSONDecodeError, TypeError):
            pass
        return None

    def _track_input_behavior(self, text: str, pasted: bool, keystroke_ratio: float):
        """Track paste/typing behavior signals."""
        if pasted or keystroke_ratio < 0.5:
            suspicion = "medium" if pasted else "low"
            self._add_reasoning({
                "note": f"INPUT BEHAVIOR: {'Pasted' if pasted else 'Low keystroke ratio'} (ratio: {keystroke_ratio})",
                "suspicion": suspicion,
                "why": "Customer may be using prepared/scripted answers",
            })
            self.paste_flags.append({
                "question": self._last_question or "",
                "answer": text[:80],
                "pasted": pasted,
                "keystroke_ratio": keystroke_ratio,
                "timestamp": datetime.now().isoformat(),
            })

        # Typo detection — positive signal
        if self._has_typos(text):
            self._typo_count += 1
            if self._typo_count == 1 or self._typo_count % 5 == 0:
                self._add_reasoning({
                    "note": f"INPUT: Typos detected in {self._typo_count} answers — consistent manual typing",
                    "suspicion": "none",
                })

    @staticmethod
    def _has_typos(text: str) -> bool:
        """Simple heuristic for typo detection."""
        indicators = 0
        if any(c * 2 in text.lower() for c in 'bcdfghjklmnpqrstvwxyz'
               if c * 2 not in ('ll', 'ss', 'ff', 'tt', 'rr', 'nn', 'pp', 'mm', 'cc', 'dd', 'gg', 'bb', 'zz')):
            indicators += 1
        words = text.split()
        if any(len(w) <= 2 and w.lower() not in (
            'a', 'i', 'an', 'in', 'on', 'at', 'to', 'of', 'is', 'it', 'or', 'by',
            'do', 'we', 'no', 'so', 'if', 'up', 'my', 'he', 'me', 'us', 'am', 'be',
            'as', 'ok', 'uk', 'go'
        ) for w in words):
            indicators += 1
        return indicators >= 1

    def _add_reasoning(self, reasoning: dict):
        """Add a reasoning entry to the log."""
        reasoning["timestamp"] = datetime.now().isoformat()
        self.reasoning_log.append(reasoning)

    def _trim_messages(self, keep_last: int = 20):
        """Trim message history, keeping the first message and last N messages."""
        if len(self.messages) <= keep_last + 1:
            return
        first = self.messages[0]
        self.messages = [first] + self.messages[-(keep_last):]

    @staticmethod
    def _strip_wrapping_phrases(text: str) -> str:
        """Remove forbidden wrapping phrases from the output."""
        import re
        forbidden = [
            r"(?:just\s+)?one\s+(?:more|last|final)\s+(?:question|thing)",
            r"before\s+we\s+(?:wrap|finish|end)",
            r"to\s+(?:wrap|finish)\s+up",
            r"lastly",
            r"almost\s+done",
            r"nearly\s+there",
        ]
        for pattern in forbidden:
            text = re.sub(pattern, "", text, flags=re.IGNORECASE)
        return text.strip()
