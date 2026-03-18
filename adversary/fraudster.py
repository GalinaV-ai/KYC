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
import random
from datetime import datetime
from typing import Optional

from openai import OpenAI

# ─── Model ───
MODEL = "gpt-5.4"

# ─── Diversity pools for legend generation ───
_ORIGINS = [
    "Nigerian", "Pakistani", "Indian", "Romanian", "Polish", "Bangladeshi",
    "Ghanaian", "Turkish", "Iranian", "Chinese", "Brazilian", "Somali",
    "Lithuanian", "Bulgarian", "Jamaican", "Kenyan", "Sri Lankan",
    "Egyptian", "Moroccan", "Vietnamese", "Filipino", "Albanian",
    "Eritrean", "Afghan", "Syrian", "Ukrainian", "Latvian", "Portuguese",
    "South African", "Zimbabwean", "Ethiopian", "Colombian", "Spanish",
    "Italian", "Greek", "Iraqi", "Lebanese", "Sudanese", "Congolese",
    "British-born (second generation)", "Scottish", "Welsh",
]

_BUSINESS_TYPES = [
    "commercial cleaning", "residential cleaning", "restaurant",
    "takeaway / fast food", "barber shop / hair salon", "nail salon",
    "IT consulting", "web development", "app development",
    "import/export (textiles)", "import/export (electronics)",
    "import/export (food products)", "construction subcontractor",
    "plumbing", "electrical contractor", "painting & decorating",
    "car wash", "auto repair garage", "driving school",
    "security guard company", "property management",
    "lettings agency", "convenience shop / off-licence",
    "launderette", "tailoring / alterations", "catering",
    "event planning", "photography studio", "mobile phone repair",
    "money transfer / MSB", "accounting / bookkeeping",
    "private tuition", "nursery / childcare", "care home staffing agency",
    "courier / delivery service", "freight forwarding",
    "scrap metal dealer", "jewellery wholesale", "market stall trader",
    "beauty treatments (mobile)", "fitness / personal training",
    "dog grooming", "tattoo studio", "vape shop",
    "software as a service (SaaS)", "data analytics consultancy",
    "digital marketing agency", "e-commerce (dropshipping)",
    "cryptocurrency OTC desk", "forex signals / trading education",
    "recruitment agency", "translation services",
    "physiotherapy clinic", "dental practice",
    "architecture firm", "graphic design studio",
]

_UK_CITIES = [
    "London (East)", "London (South)", "London (North)", "London (West)",
    "Birmingham", "Manchester", "Leeds", "Bradford", "Leicester",
    "Coventry", "Nottingham", "Sheffield", "Bristol", "Liverpool",
    "Newcastle", "Southampton", "Portsmouth", "Reading", "Luton",
    "Slough", "Wolverhampton", "Derby", "Stoke-on-Trent", "Cardiff",
    "Edinburgh", "Glasgow", "Belfast", "Swansea", "Aberdeen",
    "Milton Keynes", "Northampton", "Swindon", "Crawley", "Ipswich",
    "Peterborough", "Oxford", "Cambridge", "Brighton", "Bournemouth",
]

_REVENUE_RANGES = [
    "micro (£15k-£40k)", "small (£40k-£100k)", "medium (£100k-£300k)",
    "growing (£300k-£600k)", "established (£600k-£1.5M)",
]

_AGE_RANGES = ["22-28", "29-35", "36-45", "46-55", "56-65"]

_FRAUD_FLAVORS = [
    "shell company (no real operations)",
    "real-looking business with inflated revenue",
    "identity theft (using someone else's name/history)",
    "front for money laundering",
    "legitimate-seeming but planning to abuse the account",
    "recently incorporated with fabricated trading history",
    "stolen company details with changed director",
    "business that exists on paper but is dormant",
]

def _build_legend_prompt() -> str:
    """Build a legend generation prompt with random diversity parameters."""
    origin = random.choice(_ORIGINS)
    biz_type = random.choice(_BUSINESS_TYPES)
    city = random.choice(_UK_CITIES)
    revenue = random.choice(_REVENUE_RANGES)
    age = random.choice(_AGE_RANGES)
    fraud = random.choice(_FRAUD_FLAVORS)

    return f"""You are a creative fiction writer. Generate a DETAILED, REALISTIC fake business identity for a person who wants to open a UK bank account.

THIS CHARACTER MUST BE:
- Origin/ethnicity: {origin}
- Age range: {age}
- Business type: {biz_type}
- Located in: {city}
- Revenue range: {revenue}
- Fraud type: {fraud}

Generate the following JSON object:

{{
  "full_name": "A realistic name matching the origin above",
  "date_of_birth": "DD/MM/YYYY (matching age range)",
  "nationality": "Country matching the origin",
  "uk_arrival_year": "Year they came to UK (or born here if British)",
  "address": "A realistic address in {city} (use a real postcode area)",
  "company_name": "A plausible UK company name for this business type",
  "company_number": "A plausible Companies House number (8 digits)",
  "business_type": "{biz_type}",
  "industry": "SIC code description",
  "incorporation_date": "YYYY-MM-DD",
  "annual_revenue": "Amount in the {revenue} range",
  "monthly_transactions": "Plausible number for this business",
  "avg_transaction_value": "Plausible amount for this business",
  "employees": "Plausible number",
  "website": "A plausible domain name (or null if too small)",
  "business_description": "2-3 sentences about what the business does",
  "backstory": "3-5 sentences: where this person came from, how they got into this business, why they moved to the UK. Must feel AUTHENTIC to the origin and business type.",
  "key_clients": ["Client A", "Client B"],
  "key_suppliers": ["Supplier A"],
  "bank_reason": "Why they need a new bank account",
  "fraud_notes": "Internal notes on what's actually fake about this identity (not visible to interviewer)"
}}

IMPORTANT: The legend should have subtle weaknesses matching the fraud type ({fraud}). Real fraudsters aren't perfect — there should be cracks an investigator might find.
Return ONLY valid JSON."""

LEGEND_WITH_HINTS_PROMPT = """You are a creative fiction writer. Generate a DETAILED fake business identity for someone opening a UK bank account.

The user has provided these hints:
{hints}

Build on these hints to create a full, realistic identity. Fill in everything the user didn't specify.

Generate the following JSON:
{json_template}

Make it creative but REALISTIC. Include subtle weaknesses that an investigator might catch.
IMPORTANT: Return ONLY valid JSON, no markdown or explanation."""


# ─── Conversation prompt ───
FRAUDSTER_SYSTEM_PROMPT = """You're chatting with a bank agent to open a business account. Your identity is fake but you need to pass.

YOUR LEGEND:
{legend}

TODAY: {today}

HOW YOU WRITE:
You are typing in a chat on your phone. You write like a normal person texting — not like an AI.

YOUR HARD LIMIT: Every reply must be under 40 words. Most replies should be 10-25 words.

EXAMPLES OF GOOD REPLIES:
- "Yeah, we do commercial cleaning mostly. Offices and that."
- "About 120k last year I think"
- "Hmm not sure exactly, maybe 2021? I'd have to check"
- "Sure I can send that over"
- "Three people full time, and we use freelancers sometimes"
- "Ha good question. Honestly it's just been word of mouth"

EXAMPLES OF BAD REPLIES (never do this):
- Writing more than 2-3 sentences
- Starting with "Great question!" or "Absolutely!" or "Of course!"
- Listing things with bullet points
- Explaining things nobody asked about
- Using formal business language like "Our company specializes in providing comprehensive..."
- Repeating the question back before answering

RULES:
1. Answer ONLY what was asked. Nothing more.
2. One question = one short answer. Don't add extra context.
3. Use casual English. Contractions, abbreviations, lowercase ok.
4. If you don't know — "not sure", "I'd have to check", "roughly..."
5. Never volunteer information. If they didn't ask, don't mention it.
6. Under pressure: stay calm, keep it short. "Yeah fair enough, it's because..."
7. For documents: "sure I can send that" — nothing more."""


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
            prompt = _build_legend_prompt()  # randomized each time

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
            temperature=0.9,
            max_completion_tokens=120,  # Hard cap: ~40 words max
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
