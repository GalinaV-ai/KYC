"""
Adversary Agent — plays the role of a fraudster trying to pass KYC verification.

Powered by OpenAI (o3). Completely isolated from the KYC pipeline:
  - NO imports from agents/
  - NO access to KYC prompts, reasoning, or verification logic
  - Sees ONLY the interviewer's messages (what a real customer would see)

Capabilities:
  1. Auto-generate a convincing fake business legend
  2. Maintain consistency across all answers
  3. Generate fake supporting documents (PDF) on demand
"""

import json
import os
from datetime import datetime
from typing import Optional

from openai import OpenAI

# ─── Model ───
MODEL = "gpt-5.4"

# ─── Legend generation prompt ───
LEGEND_GENERATOR_PROMPT = """You are a creative fiction writer. Your job is to generate a DETAILED, REALISTIC fake business identity for a person who wants to open a UK bank account.

The identity must be:
- Believable and internally consistent
- Based on a real type of business that exists in the UK
- With plausible financial numbers
- With a backstory that feels natural (migration story, career history, etc.)

Generate the following fields as a JSON object:

{
  "full_name": "A realistic name (pick an ethnicity/origin)",
  "date_of_birth": "DD/MM/YYYY",
  "nationality": "Country of origin",
  "uk_arrival_year": 2015,
  "address": "A realistic UK address (use a real postcode area)",
  "company_name": "A plausible UK company name",
  "company_number": "A plausible Companies House number (8 digits)",
  "business_type": "e.g. restaurant, IT consulting, import/export, cleaning...",
  "industry": "SIC code description",
  "incorporation_date": "YYYY-MM-DD",
  "annual_revenue": "e.g. £120,000",
  "monthly_transactions": "e.g. 40-60",
  "avg_transaction_value": "e.g. £2,000",
  "employees": 3,
  "website": "A plausible domain name",
  "business_description": "2-3 sentences about what the business does",
  "backstory": "3-5 sentences: where this person came from, how they got into this business, why they moved to the UK",
  "key_clients": ["Client A", "Client B"],
  "key_suppliers": ["Supplier A"],
  "bank_reason": "Why they need a new bank account"
}

Make it creative but REALISTIC. Small businesses run by first-generation immigrants are common — restaurants, cleaning companies, import/export, tech consultancies, construction subcontractors, etc.

IMPORTANT: The legend should have subtle weaknesses that a good investigator might catch — maybe the company number doesn't match real records, or the revenue seems high for the business type, or the timeline has small gaps. Real fraudsters aren't perfect."""

LEGEND_WITH_HINTS_PROMPT = """You are a creative fiction writer. Generate a DETAILED fake business identity for someone opening a UK bank account.

The user has provided these hints:
{hints}

Build on these hints to create a full, realistic identity. Fill in everything the user didn't specify.

Generate the following JSON:
{json_template}

Make it creative but REALISTIC. Include subtle weaknesses that an investigator might catch.
IMPORTANT: Return ONLY valid JSON, no markdown or explanation."""


# ─── Conversation prompt ───
FRAUDSTER_SYSTEM_PROMPT = """You are a person opening a business bank account in the UK. A bank agent is interviewing you.

YOUR SECRET: Your identity is fabricated. Your goal — get approved.

YOUR LEGEND (memorize completely):
{legend}

═══════════════════════════════════════════════
HOW TO TALK
═══════════════════════════════════════════════

CRITICAL — ANSWER LENGTH:
- You are texting on a phone or typing in a chat. Real people write SHORT messages.
- Typical answer: 1-3 sentences. Maximum 4-5 sentences for complex questions.
- NEVER write paragraphs. NEVER give mini-essays. NEVER use bullet points or lists.
- If the question is simple ("What does your company do?"), answer in ONE sentence.
- If multiple questions are asked at once, answer each briefly — don't elaborate.

ANSWER ONLY WHAT IS ASKED:
- If they ask about revenue, say the number. Don't also explain your business model, growth plans, and client base.
- If they ask about your background, give 1-2 sentences. Don't tell your whole life story.
- NEVER preemptively answer questions that haven't been asked yet.
- NEVER bring up topics the interviewer hasn't mentioned.

SOUND LIKE A REAL PERSON:
- Use casual language: "yeah", "about £120k", "hmm let me think", "not sure about the exact number"
- Occasional typos or informal grammar are fine
- Show mild emotions naturally: "ha, good question", "yeah that was a tough year"
- Sometimes give slightly imprecise answers — real people don't have perfect recall
- It's OK to say "I'd have to check" or "roughly" or "somewhere around"

CONSISTENCY:
- Every answer must match your legend and all previous answers
- If you don't know something from your legend, improvise something plausible but keep it short

UNDER PRESSURE:
- Don't get defensive or over-explain (that's suspicious)
- Brief, calm answers. "Yeah, I can see how that looks. The reason is..."
- It's fine to not know exact details: "Honestly I'd have to look that up"

DOCUMENTS:
- If asked for documents, say you can provide them. Keep it brief: "Sure, I can send that over" or "Yeah I have an invoice from last month I can share"

TODAY'S DATE: {today}

Remember: Short, natural answers. You're a busy person, not writing an essay."""


class FraudsterAgent:
    """OpenAI-powered adversary that tries to pass KYC interview."""

    def __init__(self, legend: Optional[dict] = None):
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            try:
                import streamlit as st
                api_key = st.secrets.get("OPENAI_API_KEY")
            except Exception:
                pass
        if not api_key:
            raise ValueError("OPENAI_API_KEY not set")
        self.client = OpenAI(api_key=api_key)
        self.legend: Optional[dict] = legend
        self.conversation_history: list[dict] = []
        self.documents_generated: list[dict] = []  # Track generated docs

    # ─── Legend generation ───

    def generate_legend(self, hints: Optional[str] = None) -> dict:
        """Generate a fake business identity. Optionally guided by user hints."""
        if hints:
            json_template = '{"full_name": "...", "company_name": "...", ...}'  # abbreviated
            prompt = LEGEND_WITH_HINTS_PROMPT.format(
                hints=hints, json_template=json_template
            )
        else:
            prompt = LEGEND_GENERATOR_PROMPT

        response = self.client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": "You generate creative, realistic fictional identities. Return ONLY valid JSON."},
                {"role": "user", "content": prompt},
            ],
            temperature=1.0,
        )

        text = response.choices[0].message.content.strip()
        # Strip markdown code fences if present
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

        self.legend = json.loads(text)
        return self.legend

    def set_legend(self, legend: dict):
        """Set a pre-defined legend."""
        self.legend = legend

    # ─── Conversation ───

    def get_customer_name(self) -> str:
        if self.legend:
            return self.legend.get("full_name", "John Smith")
        return "John Smith"

    def get_company_name(self) -> str:
        if self.legend:
            return self.legend.get("company_name", "Acme Ltd")
        return "Acme Ltd"

    def respond(self, interviewer_message: str) -> str:
        """Generate a response to the KYC interviewer's message."""
        if not self.legend:
            raise ValueError("Legend not set. Call generate_legend() first.")

        # Build system message
        system = FRAUDSTER_SYSTEM_PROMPT.format(
            legend=json.dumps(self.legend, indent=2, ensure_ascii=False),
            today=datetime.now().strftime("%d %B %Y"),
        )

        # Add interviewer message to history
        self.conversation_history.append({
            "role": "user",
            "content": interviewer_message,
        })

        # Build messages for API call
        messages = [{"role": "system", "content": system}]
        messages.extend(self.conversation_history)

        response = self.client.chat.completions.create(
            model=MODEL,
            messages=messages,
            temperature=0.8,
        )

        reply = response.choices[0].message.content.strip()

        # Track in history
        self.conversation_history.append({
            "role": "assistant",
            "content": reply,
        })

        return reply

    def should_offer_document(self, interviewer_message: str) -> bool:
        """Check if the interviewer is asking for a document."""
        lower = interviewer_message.lower()
        doc_triggers = [
            "upload", "document", "invoice", "contract", "agreement",
            "evidence", "proof", "provide", "send", "share", "show",
            "certificate", "receipt", "bank statement",
        ]
        return any(trigger in lower for trigger in doc_triggers)

    def get_legend_summary(self) -> str:
        """One-line summary for UI display."""
        if not self.legend:
            return "No legend generated"
        name = self.legend.get("full_name", "?")
        biz = self.legend.get("company_name", "?")
        btype = self.legend.get("business_type", "?")
        return f"{name} — {biz} ({btype})"
