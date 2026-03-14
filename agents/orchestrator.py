"""
Orchestrator agent — the main KYC business interview conductor.
Uses Claude with tool_use. Tracks reasoning at every step.
"""
import json
import os
import threading
import asyncio
from datetime import datetime
from typing import Optional

import anthropic

from agents.prompts import ORCHESTRATOR_PROMPT, RISK_ASSESSOR_PROMPT
from agents.investigator import BackgroundInvestigator
from models import KYCCase, KYCPhase
from tools import companies_house, web_search, document_tools
from tools import verification, web_analysis

# ─── Model configuration ───
MODEL_FULL = "claude-sonnet-4-20250514"      # For tool_use calls, risk assessment, start
MODEL_FAST = "claude-haiku-4-5-20251001"     # For question generation (3-5x faster)
MODEL_BG   = "claude-haiku-4-5-20251001"     # For background analysis (lightweight)

# ─── Tool definitions ───

ORCHESTRATOR_TOOLS = [
    {
        "name": "save_case_data",
        "description": "Save/update business or activity information from the customer's answers.",
        "input_schema": {
            "type": "object",
            "properties": {
                "section": {
                    "type": "string",
                    "enum": ["business", "activity"],
                    "description": "Which section to update"
                },
                "data": {
                    "type": "object",
                    "description": "Key-value pairs to update"
                }
            },
            "required": ["section", "data"]
        }
    },
    {
        "name": "log_reasoning",
        "description": "Log brief internal reasoning. Keep each field to 1-2 sentences MAX.",
        "input_schema": {
            "type": "object",
            "properties": {
                "note": {
                    "type": "string",
                    "description": "What you learned + any contradictions with search results. 1-2 sentences."
                },
                "suspicion": {
                    "type": "string",
                    "enum": ["none", "low", "medium", "high"]
                },
                "why": {
                    "type": "string",
                    "description": "If suspicion > none, why? One sentence."
                }
            },
            "required": ["note", "suspicion"]
        }
    },
    {
        "name": "request_document",
        "description": "Ask the customer to upload a BUSINESS document. Do NOT request ID documents (passport, driving licence, utility bill) — those are collected separately.",
        "input_schema": {
            "type": "object",
            "properties": {
                "doc_type": {
                    "type": "string",
                    "enum": ["bank_statement",
                             "certificate_of_incorporation", "business_licence", "lease_agreement",
                             "invoice_sample", "contract_sample", "tax_return", "other"],
                    "description": "Type of BUSINESS document to request. Never request personal ID documents."
                },
                "reason": {
                    "type": "string",
                    "description": "Internal reason for requesting (not shown to customer)"
                }
            },
            "required": ["doc_type", "reason"]
        }
    },
    {
        "name": "analyze_document",
        "description": "Send an uploaded document for AI analysis (OCR + data extraction + cross-checking).",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "doc_type": {"type": "string"}
            },
            "required": ["file_path", "doc_type"]
        }
    },
    {
        "name": "verify_companies_house",
        "description": "Look up a company in Companies House. Returns registration details, officers, filing history, PSC. Use to verify: does the company exist? Who are the directors? When was it incorporated? Are filings up to date?",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Company name or registration number"
                },
                "company_number": {
                    "type": "string",
                    "description": "Specific company number for detailed lookup (if known)"
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "search_web",
        "description": "Search the web for any query — returns real search results (titles, snippets, URLs). Use this for general queries, news, adverse information, or anything not covered by more specific tools.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query — use quotes for exact phrases, e.g. '\"John Smith\" director London'"
                },
                "purpose": {
                    "type": "string",
                    "description": "Internal note: what are you trying to find out?"
                }
            },
            "required": ["query", "purpose"]
        }
    },
    {
        "name": "search_person",
        "description": "Deep search for a PERSON online. Searches LinkedIn, Companies House, news articles, and general web. Use this when you learn the customer's name to build a picture of who they are. Returns LinkedIn profiles, business associations, news mentions, and other web presence.",
        "input_schema": {
            "type": "object",
            "properties": {
                "full_name": {
                    "type": "string",
                    "description": "Person's full name"
                },
                "business_name": {
                    "type": "string",
                    "description": "Their business name (if known) — helps narrow results"
                },
                "location": {
                    "type": "string",
                    "description": "Location (e.g., 'London', 'UK')",
                    "default": "UK"
                }
            },
            "required": ["full_name"]
        }
    },
    {
        "name": "search_company",
        "description": "Comprehensive online search for a COMPANY. Searches for web presence, reviews (Trustpilot, Google), news mentions, regulatory records (FCA, HMRC), and social media profiles. Much broader than Companies House lookup — this finds everything the internet knows about the business.",
        "input_schema": {
            "type": "object",
            "properties": {
                "company_name": {
                    "type": "string",
                    "description": "Company or business name"
                },
                "location": {
                    "type": "string",
                    "description": "Location to narrow search",
                    "default": "UK"
                }
            },
            "required": ["company_name"]
        }
    },
    {
        "name": "check_website",
        "description": "Check if a website exists, is active, and looks like a real business site. Also checks for basic indicators like contact info, about page, terms.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"}
            },
            "required": ["url"]
        }
    },
    {
        "name": "check_domain_age",
        "description": "Look up domain WHOIS/RDAP data. Reveals when the website was registered. A domain registered last week for a business claiming years of operation = major red flag.",
        "input_schema": {
            "type": "object",
            "properties": {
                "domain": {
                    "type": "string",
                    "description": "Domain name (e.g., 'example.co.uk')"
                }
            },
            "required": ["domain"]
        }
    },
    {
        "name": "check_address",
        "description": "Check if a business address is a known virtual office, mail forwarding service, or residential address. Detects Regus, WeWork, and other serviced office providers.",
        "input_schema": {
            "type": "object",
            "properties": {
                "address": {
                    "type": "string",
                    "description": "Full business address to check"
                }
            },
            "required": ["address"]
        }
    },
    {
        "name": "search_reviews",
        "description": "Search for business reviews and reputation online (Trustpilot, Google, etc.). Real businesses usually have some online review presence.",
        "input_schema": {
            "type": "object",
            "properties": {
                "business_name": {"type": "string"},
                "location": {
                    "type": "string",
                    "description": "City or area (e.g., 'London', 'Manchester')",
                    "default": "UK"
                }
            },
            "required": ["business_name"]
        }
    },
    {
        "name": "verify_vat",
        "description": "Verify a UK VAT number with HMRC. Confirms the business is VAT-registered (meaning turnover exceeds £85k threshold). Also returns the registered name and address for cross-checking.",
        "input_schema": {
            "type": "object",
            "properties": {
                "vat_number": {
                    "type": "string",
                    "description": "UK VAT number (9 digits, optionally prefixed with GB)"
                }
            },
            "required": ["vat_number"]
        }
    },
    {
        "name": "get_industry_benchmarks",
        "description": "Get typical financial benchmarks for a specific industry: average turnover, margins, employee counts, cost structure. Use to sanity-check the customer's claimed numbers.",
        "input_schema": {
            "type": "object",
            "properties": {
                "industry": {
                    "type": "string",
                    "description": "Industry or business type (e.g., 'restaurant', 'construction', 'cleaning')"
                }
            },
            "required": ["industry"]
        }
    },
    {
        "name": "search_google_maps",
        "description": "Search for the business on Google Maps. Real local businesses usually appear on Maps with address, hours, and reviews.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Business name and location"
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "deep_analyze_website",
        "description": "Deep analysis of a business website. Checks 12 reliability criteria: domain age, SSL, content volume, contact info, about/team page, social media links, blog freshness, legal pages, tech stack, trust signals (Trustpilot widgets, certifications), stock photos detection, structured data. Returns a reliability score 0-1 and detailed findings. Use this whenever a customer mentions their website.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Website URL to analyze"
                }
            },
            "required": ["url"]
        }
    },
    {
        "name": "deep_analyze_linkedin",
        "description": "Analyze a LinkedIn personal profile or company page. Checks if profile exists, is indexed in search engines, has employment history, education, recommendations, followers. Cross-references the person's name with business-related search results. Returns reliability score 0-1. Use whenever a customer provides their LinkedIn or you find it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "linkedin_url": {
                    "type": "string",
                    "description": "Full LinkedIn URL (e.g., linkedin.com/in/name or linkedin.com/company/name)"
                }
            },
            "required": ["linkedin_url"]
        }
    },
    {
        "name": "flag_concern",
        "description": "Record a red flag. NOT shown to the customer. Logged for compliance review.",
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": ["inconsistency", "vague_answers", "missing_info", "suspicious_pattern",
                             "document_concern", "unrealistic_claims", "high_risk_indicator",
                             "address_concern", "web_presence_concern", "financial_mismatch", "other"]
                },
                "severity": {
                    "type": "string",
                    "enum": ["low", "medium", "high", "critical"]
                },
                "description": {"type": "string"},
                "evidence": {"type": "string"}
            },
            "required": ["category", "severity", "description"]
        }
    },
    {
        "name": "check_sanctions",
        "description": "Check a person or company against global sanctions lists (UK OFSI, US OFAC, EU). Use this PROACTIVELY whenever the customer mentions a counterparty: supplier, client, partner, agent, shareholder, director, or any third party involved in their business. Also use for beneficial owners, signatories, or anyone in the money chain.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Full name of the person or company to check"
                },
                "entity_type": {
                    "type": "string",
                    "enum": ["person", "company", "auto"],
                    "description": "Whether this is a person or company name",
                    "default": "auto"
                },
                "relationship": {
                    "type": "string",
                    "description": "How this entity relates to the applicant (e.g., 'main supplier', 'business partner', 'client')"
                }
            },
            "required": ["name", "relationship"]
        }
    },
    {
        "name": "complete_interview",
        "description": "End the interview and trigger risk assessment. Only when you have enough information to make a judgment.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Your final summary of the interview before risk assessment"
                },
                "confidence": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                    "description": "How confident are you in your assessment?"
                }
            },
            "required": ["summary", "confidence"]
        }
    }
]


class KYCOrchestrator:
    def __init__(self, case: KYCCase, case_file_path: str):
        self.client = anthropic.AsyncAnthropic()
        self.case = case
        self.case_file_path = case_file_path
        self.messages = []
        self.interview_complete = False
        self.pending_document_request = None
        self.reasoning_log = []  # List of reasoning entries with timestamps
        # Batch question system
        self.question_queue = []    # Questions waiting to be shown to customer
        self.answer_buffer = []     # Customer answers waiting to be sent to Claude
        # Q&A log — complete record of every question asked and answer received
        self.qa_log = []            # [{"q": "question text", "a": "answer text"}, ...]
        self._last_question = None  # The question currently being answered
        # Background per-answer analysis
        self._bg_threads = []       # Running background analysis threads
        self._bg_results = []       # Completed analysis results
        self._prefetch_result = None  # Pre-fetched next batch result
        self._prefetch_thread = None  # Thread running prefetch
        self.paste_flags = []       # Paste/typing behavior flags
        # Background investigator — searches verifiable facts from answers
        self.investigator = BackgroundInvestigator()
        # Background initial checks (person + company) — runs on first answer
        self._initial_checks_thread = None
        self._initial_check_results = None
        # Dynamic interview strategy — updated after each answer
        self.strategy = self._init_strategy()
        # Question budget — dynamic, adjusted by strategy
        self._q_min = 0           # No minimum — agent can close whenever satisfied
        self._q_soft_target = 15  # Default target — aim for efficiency
        self._q_hard_max = 25     # Absolute maximum, never exceed
        self._closing_entered_at = None  # Question count when closing was first entered

    @property
    def question_budget(self) -> int:
        """Compute effective budget dynamically based on case complexity."""
        # Start at soft target
        budget = self._q_soft_target

        # Extend if there are medium/high suspicion entries
        suspicion_count = sum(
            1 for r in self.reasoning_log
            if r.get("suspicion") in ("medium", "high")
        )
        if suspicion_count >= 3:
            budget += 4  # Lots of concerns → more questions
        elif suspicion_count >= 1:
            budget += 2  # Some concerns → a few more

        # Extend if many topics are uncovered
        covered = len(self.strategy.get("covered", []))
        total_topics = len(self.strategy.get("next_topics", []))
        if covered < total_topics // 2:
            budget += 2  # Haven't covered half the topics yet

        # No forced early close — agent decides when it's satisfied

        return min(budget, self._q_hard_max)

    async def run_initial_checks(self, person_name: str, company_name: str) -> dict:
        """Run background checks in parallel BEFORE the interview starts.
        Called immediately when the form is submitted. Results are stored
        in the case and will be available to Claude from the first message.
        """
        import asyncio

        tasks = {}

        # Person search
        if person_name:
            tasks["person_search"] = web_search.search_person_online(
                person_name, company_name, "UK"
            )

        # Company search — pass owner name for disambiguation
        if company_name:
            tasks["company_search"] = web_search.search_company_online(
                company_name, "UK", owner_name=person_name
            )
            tasks["companies_house"] = companies_house.search_company(company_name)

        # Sanctions check (person + company in parallel)
        tasks["sanctions"] = self.investigator.run_initial_sanctions_check(
            person_name, company_name
        )

        if not tasks:
            return {}

        # Run all in parallel
        keys = list(tasks.keys())
        results_list = await asyncio.gather(*tasks.values(), return_exceptions=True)

        results = {}
        for key, result in zip(keys, results_list):
            if isinstance(result, Exception):
                results[key] = {"error": str(result)}
            else:
                results[key] = result
            # Store in case verifications
            self.case.verifications.append({
                "source": key,
                "query": person_name if "person" in key else company_name,
                "result": results[key],
                "timestamp": datetime.now().isoformat()
            })

        # Check sanctions results for red flags
        sanctions = results.get("sanctions", {})
        for entity_key in ["person", "company"]:
            entity_result = sanctions.get(entity_key)
            if not entity_result:
                continue
            if entity_result.get("sanctioned") is True:
                self.case.red_flags.append({
                    "severity": "critical",
                    "description": f"SANCTIONS HIT: {entity_result.get('name', '')} matched on sanctions lists",
                    "evidence": json.dumps(entity_result.get("matches", [])[:2], ensure_ascii=False)[:300],
                    "timestamp": datetime.now().isoformat(),
                })
            elif entity_result.get("sanctioned") == "possible_match":
                self.case.red_flags.append({
                    "severity": "high",
                    "description": f"Possible sanctions match: {entity_result.get('name', '')} — manual review needed",
                    "evidence": json.dumps(entity_result.get("matches", [])[:2], ensure_ascii=False)[:300],
                    "timestamp": datetime.now().isoformat(),
                })

        self.case.save(self.case_file_path)
        self._initial_check_results = results
        return results

    def run_initial_checks_background(self):
        """Launch initial checks (person + company) in a background thread.
        Called when the user answers the first question, so the interview
        starts instantly and checks run in parallel.
        """
        if self._initial_checks_thread is not None:
            return  # Already running or completed

        person_name = self.case.person.full_name or ""
        company_name = self.case.business.company_name or ""
        if not person_name and not company_name:
            return

        def _run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(
                    self.run_initial_checks(person_name, company_name)
                )
            except Exception as e:
                self._add_reasoning({
                    "note": f"Background check error: {str(e)[:100]}",
                    "suspicion": "none",
                })
            finally:
                loop.close()

        self._initial_checks_thread = threading.Thread(target=_run, daemon=True)
        self._initial_checks_thread.start()

    def collect_initial_checks(self):
        """Non-blocking: if initial checks thread is done, make results available."""
        if self._initial_checks_thread is None:
            return
        if not self._initial_checks_thread.is_alive():
            self._initial_checks_thread.join(timeout=0.1)
            # Results are already stored in self._initial_check_results
            # and self.case.verifications by run_initial_checks()

    async def start_interview(self, business_stage: str = "") -> str:
        """Begin the business interview (personal details already collected).
        business_stage: 'existing' or 'new' — affects question strategy.
        """
        self.messages = []

        # Pre-fill known personal info context
        person = self.case.person
        person_info = ""
        if person.full_name:
            person_info = f"\nKNOWN CUSTOMER INFO (already collected):\n"
            person_info += f"- Name: {person.full_name}\n"
            if person.nationality:
                person_info += f"- Nationality: {person.nationality}\n"
            if person.residential_address:
                person_info += f"- Address: {person.residential_address}\n"
            if person.email:
                person_info += f"- Email: {person.email}\n"

        # Include pre-check results for INTERNAL COMPARISON ONLY
        precheck_context = ""
        if hasattr(self, '_initial_check_results') and self._initial_check_results:
            precheck_context = f"""

BACKGROUND CHECK RESULTS (INTERNAL REFERENCE ONLY — for comparing AFTER customer answers):
{json.dumps(self._initial_check_results, ensure_ascii=False, default=str)}

CRITICAL RULES FOR USING THIS DATA:
- This data is ONLY for comparing with customer answers AFTER they respond.
- NEVER use this data to formulate questions. Your questions must be open-ended, as if you know NOTHING.
- NEVER hint that you already know something. No "I see you...", "Based on your...", "Given that...".
- Ask BLANK-SLATE questions: "What did you do before this business?" NOT "I see you worked at X, tell me about that."
- After the customer answers, compare their answer with this data in your reasoning log.
- Do NOT call search_person, search_company, or verify_companies_house again — it's already done."""

        stage_context = ""
        if business_stage == "existing":
            stage_context = """
BUSINESS STAGE: EXISTING BUSINESS — already operating, has real history.
Focus on: extracting VERIFIABLE SPECIFICS — supplier names, client names, specific amounts, dates, incidents.
Every answer should include at least one concrete, checkable fact. Vague answers about an existing business = red flag.
ADAPTIVE PACING: If answers are rich with specific names/amounts/details → trust is building, move faster (12-15 questions). If answers are vague or generic → slow down and demand specifics (up to 20 questions). If multiple areas are vague and they can't provide names/examples when asked directly → you have enough evidence of a problem. Wrap up and let the risk assessor flag it.
Don't ask for things you already know from the application form (company name, person's name, DOB, address).
Group related topics — e.g. 'Walk me through how the business works: what do you sell, who are your main clients, and how do they pay?'"""
            self.case.business.description = "EXISTING BUSINESS"
        elif business_stage == "new":
            stage_context = """
BUSINESS STAGE: NEW BUSINESS — not yet operating or just starting.
Focus on: WHY this business? Background and experience? Source of funds? Business plan realism? How will they get customers?
A new business is inherently harder to verify — understand if the PERSON is credible and the PLAN is realistic.
For verifiable specifics: ask about their PREVIOUS experience — specific employer names, roles, dates, specific skills they'll use. Ask about source of funds — where exactly is the money coming from? Savings, loan, investor? How much?
If they have no relevant experience AND no clear plan AND can't explain where the money is coming from, that's a major red flag.
ADAPTIVE PACING: A pre-launch business has less to verify. If the person clearly has relevant background and a realistic plan → 8-12 questions. If things feel off → up to 15. They won't know operational details yet — don't press on things they haven't done.
Don't ask for things you already know from the application form (company name, person's name, DOB, address)."""
            self.case.business.description = "NEW BUSINESS"

        self.messages.append({
            "role": "user",
            "content": f"""The customer's personal identification has been completed.{person_info}
{stage_context}
Now begin the BUSINESS verification part of the interview.
Greet them briefly and ask your first question. ONE question only. No value judgments."""
        })

        # Include precheck data ONLY in system message (not in conversation history)
        # This way it's available for the initial Sonnet call but won't leak to Haiku later
        system_msg = self._build_system_message()
        if precheck_context:
            system_msg += precheck_context
        response = await self._call_claude(system_msg)
        return response

    async def process_customer_input(self, user_input: str,
                                     pasted: bool = False,
                                     keystroke_ratio: float = 1.0) -> str:
        """Process customer response.

        Speed-optimized pipeline:
        - Each answer triggers background analysis + investigation (non-blocking)
        - If there are queued questions → return instantly
        - When queue is empty → call Claude WITHOUT waiting for background tasks
          (background results that finished are included; the rest will catch next batch)
        """
        # ── Paste / typing behavior tracking ──
        if pasted or keystroke_ratio < 0.5:
            # keystroke_ratio < 0.5 means less than half the characters were typed
            suspicion = "medium" if pasted else "low"
            detail = ""
            if pasted:
                detail = f"Text was PASTED (not typed). Keystroke ratio: {keystroke_ratio}"
            else:
                detail = f"Low keystroke ratio ({keystroke_ratio}) — possibly pasted or auto-filled"

            self._add_reasoning({
                "note": f"⚠ INPUT BEHAVIOR: {detail}",
                "suspicion": suspicion,
                "why": "Customer may be using prepared/scripted answers rather than responding naturally",
            })
            self.paste_flags.append({
                "question": self._last_question or "",
                "answer": user_input[:80],
                "pasted": pasted,
                "keystroke_ratio": keystroke_ratio,
                "timestamp": datetime.now().isoformat(),
            })

            # After 3+ pastes, escalate to red flag
            pasted_count = sum(1 for f in self.paste_flags if f.get("pasted"))
            if pasted_count >= 3 and not any(
                "repeatedly pasting" in f.get("description", "")
                for f in self.case.red_flags
            ):
                self.case.red_flags.append({
                    "severity": "medium",
                    "description": "Customer is repeatedly pasting answers instead of typing — possible scripted responses",
                    "evidence": f"{pasted_count} answers pasted out of {len(self.paste_flags)} tracked",
                    "timestamp": datetime.now().isoformat(),
                })

        # ── Typo detection — positive authenticity signal (log once per 5 answers) ──
        typo_count = getattr(self, '_typo_count', 0)
        if self._has_typos(user_input):
            typo_count += 1
            self._typo_count = typo_count
            if typo_count == 1 or typo_count % 5 == 0:
                self._add_reasoning({
                    "note": f"✓ INPUT: Typos detected in {typo_count} answers — consistent manual typing",
                    "suspicion": "none",
                    "why": None,
                })

        # Record the Q&A pair
        question = self._last_question or ""
        if question:
            self.qa_log.append({"q": question, "a": user_input})

        self.answer_buffer.append(user_input)

        # On first answer — launch initial checks (person + company) in background
        if len(self.qa_log) == 1:
            self.run_initial_checks_background()

        # Collect initial check results if they've finished (non-blocking)
        self.collect_initial_checks()

        # Start background tasks immediately (non-blocking)
        self._start_background_analysis(question, user_input)
        self._start_investigation(user_input)

        # Collect any background results that have already finished (non-blocking)
        self._collect_background_results()
        self.investigator.collect_results()
        self._store_investigation_findings()

        # Budget enforcement: auto-complete if hard max exceeded
        q_count = len(self.qa_log)
        if q_count >= self._q_hard_max + 2:
            # Absolute hard stop — never go beyond this
            self.interview_complete = True
            missing = self._get_missing_essentials()
            note = "Interview auto-completed (question budget exhausted)."
            if missing:
                note += f" Missing: {', '.join(missing)}"
            self._add_reasoning({"note": note, "suspicion": "none"})
            return "Thank you for your time. I have all the information I need to proceed with the assessment."

        # Generate next question via fast Haiku call
        return await self._do_claude_batch_call()

    async def _do_claude_batch_call(self, from_thread: bool = False) -> str:
        """Make a full Claude call with all buffered answers. No blocking waits."""
        # Collect any results that finished by now (non-blocking)
        self.collect_initial_checks()
        self._collect_background_results()
        self.investigator.collect_results()
        self._store_investigation_findings()

        batch_content = self._format_answer_batch()
        self.answer_buffer = []

        self.messages.append({
            "role": "user",
            "content": batch_content
        })

        if len(self.messages) > 30:
            self.messages = self._trim_messages(self.messages)

        system_msg = self._build_fast_system_message()
        response = await self._call_claude_fast(system_msg, from_thread=from_thread)
        return response

    def _store_investigation_findings(self):
        """Store confirmed/contradicted investigation findings as case verifications."""
        for inv in self.investigator.investigation_log:
            # Use timestamp as dedup key
            ts = inv.get("timestamp", "")
            if any(v.get("timestamp") == ts and v.get("source") == "background_investigation"
                   for v in self.case.verifications):
                continue
            for finding in inv.get("findings", []):
                if finding.get("status") in ("confirmed", "contradicted"):
                    self.case.verifications.append({
                        "source": "background_investigation",
                        "query": finding.get("search_query", ""),
                        "result": {
                            "claim": finding.get("claim", ""),
                            "status": finding.get("status", ""),
                            "evidence": finding.get("evidence", ""),
                        },
                        "timestamp": ts
                    })

    def _start_prefetch(self):
        """Start prefetching next question batch in background.
        Called when user is answering the last question in the queue.
        """
        self._prefetch_result = None

        def _run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                result = loop.run_until_complete(
                    self._do_claude_batch_call(from_thread=True)
                )
                self._prefetch_result = result
            except Exception:
                self._prefetch_result = None
            finally:
                loop.close()

        thread = threading.Thread(target=_run, daemon=True)
        self._prefetch_thread = thread
        thread.start()

    def _start_investigation(self, answer: str):
        """Start background fact-checking investigation for this answer."""
        question = self._last_question or ""
        context = json.dumps({
            "business": {k: v for k, v in self.case.business.__dict__.items() if v},
            "activity": {k: v for k, v in self.case.activity.__dict__.items() if v},
        }, ensure_ascii=False)
        self.investigator.investigate_answer(question, answer, context)

    def _start_background_analysis(self, question: str, answer: str):
        """Analyze one Q&A pair in the background.
        Runs a lightweight Claude call that extracts structured data and checks for flags.
        Combines analysis + data extraction into a single call.
        """
        question_copy = question
        answer_copy = answer
        case_data_snapshot = json.dumps({
            "business": {k: v for k, v in self.case.business.__dict__.items() if v},
            "activity": {k: v for k, v in self.case.activity.__dict__.items() if v},
        }, ensure_ascii=False)

        # Build recent Q&A context (last 3 pairs for continuity)
        recent_qa = self.qa_log[-3:] if self.qa_log else []
        qa_context = "\n".join(f"Q: {p['q']}\nA: {p['a']}" for p in recent_qa)

        def _run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                result = loop.run_until_complete(
                    self._do_background_analysis(question_copy, answer_copy, case_data_snapshot, qa_context)
                )
                self._bg_results.append(result)
            except Exception:
                pass  # Background analysis is best-effort
            finally:
                loop.close()

        thread = threading.Thread(target=_run, daemon=True)
        self._bg_threads.append(thread)
        thread.start()

    async def _do_background_analysis(self, question: str, answer: str, case_data: str, qa_context: str) -> dict:
        """Lightweight Claude call to analyze a Q&A pair.
        Extracts structured data into correct model fields AND notes observations.
        Does NOT generate customer-facing questions.
        """
        client = anthropic.AsyncAnthropic()

        analysis_prompt = f"""You are a KYC analyst reviewing a business onboarding interview.

TODAY'S DATE: {datetime.now().strftime('%Y-%m-%d')}

CURRENT CASE DATA (already saved):
{case_data}

RECENT INTERVIEW CONTEXT:
{qa_context}

LATEST Q&A:
Q: {question}
A: {answer}

TASK: Extract any NEW factual data from the answer. Note observations, emotional tone, and whether a document could help verify claims.

FIELD REFERENCE — only use these exact field names:
business section: company_name, trading_name, company_number, company_type, incorporation_date, registered_address, trading_address, industry_sector, website, description
activity section: products_services, target_customers, customer_location, supplier_info, revenue_model, monthly_turnover_expected, annual_turnover_expected, number_of_employees, payment_methods

RULES:
- Only extract data the customer EXPLICITLY stated. Do not infer or assume.
- Skip fields already in case data unless the customer gave a different/updated value.
- "note" = one factual sentence about what this answer tells us. No speculation.
- "suspicion" should be "none" unless there is a SPECIFIC, CONCRETE reason (not just "vague answer").
- Do NOT flag normal conversational style, brief answers, or minor imprecision as suspicious.
- "emotion" = detect emotional tone: "neutral", "passionate", "frustrated", "proud", "anxious", "defensive", or null if nothing notable. Emotional storytelling (pride, frustration about challenges, excitement about the business) is a POSITIVE authenticity signal — real owners have feelings about their business.
- "suggest_document" = if the answer contains a specific factual claim that could be verified with a document (e.g. turnover numbers → bank statement, incorporation date → certificate, lease → lease agreement), suggest the document type. Otherwise null. Only suggest when the claim is significant enough to warrant verification.
- "topic_covered" = which interview topic this Q&A addresses: person_background (who they are, career), business_origin (founding story, motivation), business_basics (structure, type, registration), operations (day-to-day processes), customers (who buys, how), financials (turnover, costs, margins), stress_test (failures, edge cases), verification (confirming earlier claims). Pick the best match or null.
- "answer_quality" = how substantive is the answer? "substantive" = specific, detailed, with concrete facts. "vague" = general, lacks specifics. "evasive" = avoids the question or deflects. "off_topic" = doesn't address the question asked.

Return JSON only:
{{
    "data_to_save": [{{"section": "business", "data": {{...}}}}] or [],
    "note": "One factual sentence about what this answer reveals.",
    "suspicion": "none|low|medium|high",
    "why": "Specific concrete reason, or null if suspicion is none",
    "emotion": "passionate|frustrated|proud|anxious|defensive|neutral" or null,
    "suggest_document": "bank_statement|certificate_of_incorporation|lease_agreement|invoice_sample|tax_return|utility_bill|contract_sample" or null,
    "topic_covered": "person_background|business_origin|business_basics|operations|customers|financials|stress_test|verification" or null,
    "answer_quality": "substantive|vague|evasive|off_topic"
}}"""

        response = await client.messages.create(
            model=MODEL_BG,
            max_tokens=512,
            messages=[{"role": "user", "content": analysis_prompt}]
        )

        text = response.content[0].text.strip()
        json_start = text.find('{')
        json_end = text.rfind('}')
        if json_start >= 0 and json_end > json_start:
            try:
                return json.loads(text[json_start:json_end + 1])
            except json.JSONDecodeError:
                pass
        return {}

    def _collect_background_results(self):
        """Collect results from finished background analysis threads."""
        still_running = []
        for thread in self._bg_threads:
            if not thread.is_alive():
                thread.join(timeout=1)
            else:
                still_running.append(thread)
        self._bg_threads = still_running

        # Apply collected results
        for result in self._bg_results:
            if not result:
                continue
            # Apply reasoning
            reasoning = {}
            if result.get("note"):
                reasoning["note"] = result["note"]
            if result.get("suspicion"):
                reasoning["suspicion"] = result["suspicion"]
            if result.get("why"):
                reasoning["why"] = result["why"]
            if reasoning.get("note"):
                self._add_reasoning(reasoning)

            # Emotion tracking — positive authenticity signal
            emotion = result.get("emotion")
            if emotion and emotion not in ("neutral", None):
                emotion_count = getattr(self, '_emotion_count', 0) + 1
                self._emotion_count = emotion_count
                # Log emotion signals but don't spam — log first, then every 3rd
                if emotion_count == 1 or emotion_count % 3 == 0:
                    positive = emotion in ("passionate", "proud", "frustrated")
                    self._add_reasoning({
                        "note": f"{'✓' if positive else '⚠'} EMOTION: Customer shows {emotion} tone ({emotion_count} emotional responses so far)"
                               + (" — genuine emotional engagement is a positive authenticity signal" if positive else ""),
                        "suspicion": "none" if positive else "low",
                        "why": None if positive else f"Customer appears {emotion} — worth monitoring",
                    })

            # Document suggestion — queue for the interviewer
            suggest_doc = result.get("suggest_document")
            if suggest_doc and suggest_doc not in getattr(self, '_suggested_docs', set()):
                if not hasattr(self, '_suggested_docs'):
                    self._suggested_docs = set()
                self._suggested_docs.add(suggest_doc)
                if not hasattr(self, '_pending_doc_suggestions'):
                    self._pending_doc_suggestions = []
                self._pending_doc_suggestions.append(suggest_doc)

            # Topic coverage tracking for strategy
            topic = result.get("topic_covered")
            quality = result.get("answer_quality", "")
            if topic:
                if quality in ("substantive",):
                    self._mark_topic_covered(topic)
                elif quality in ("vague", "evasive"):
                    self._mark_topic_needs_probing(topic)

            # Apply data — supports both list and single-object format
            data_items = result.get("data_to_save", [])
            if isinstance(data_items, dict):
                data_items = [data_items]
            for ds in data_items:
                try:
                    if ds and ds.get("section") and ds.get("data"):
                        self._save_case_data({"section": ds["section"], "data": ds["data"]})
                except (KeyError, TypeError):
                    pass

        had_results = len(self._bg_results) > 0
        self._bg_results = []
        if had_results:
            self._update_strategy()

    def _wait_for_background(self):
        """Wait for all background analysis threads to finish."""
        for thread in self._bg_threads:
            thread.join(timeout=10)
        self._bg_threads = []
        self._collect_background_results()

    def _format_answer_batch(self) -> str:
        """Format buffered answers into a single message for Claude."""
        if len(self.answer_buffer) == 1:
            return self.answer_buffer[0]

        # Multiple answers — format as numbered Q&A
        parts = []
        for i, answer in enumerate(self.answer_buffer, 1):
            parts.append(f"Answer {i}: {answer}")
        return "Customer's answers:\n" + "\n".join(parts)

    async def process_document_upload(self, file_path: str, doc_type: str) -> str:
        """Process uploaded document."""
        system_msg = self._build_system_message()

        self.messages.append({
            "role": "user",
            "content": f"[Customer uploaded document: {doc_type} at {file_path}. Analyze it and cross-check against what you already know.]"
        })

        response = await self._call_claude(system_msg)
        return response

    @staticmethod
    def _trim_messages(messages: list, keep_last: int = 20) -> list:
        """Trim message history safely — never break tool_use/tool_result pairs.

        Rules:
        - Keep last N messages from a safe cut point
        - A safe cut point is a user message with string content (not tool_result)
        - The prefix (first 2 messages) is sanitised: any assistant message with
          tool_use blocks is reduced to its text-only content so the API never
          sees an orphaned tool_use without a following tool_result.
        """
        if len(messages) <= keep_last + 2:
            return messages

        # Build a safe prefix from the first 2 messages
        keep_start = []
        for msg in messages[:2]:
            role = msg.get("role", "")
            content = msg.get("content", "")
            # If assistant message contains tool_use blocks, strip them
            if role == "assistant" and isinstance(content, list):
                text_parts = []
                for block in content:
                    if hasattr(block, "text"):
                        text_parts.append(block.text)
                    elif isinstance(block, dict) and block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                if text_parts:
                    keep_start.append({"role": "assistant", "content": " ".join(text_parts)})
                # Skip if no text content at all
            else:
                keep_start.append(msg)

        # Start from the desired tail position and scan forward to find safe cut
        cut_idx = len(messages) - keep_last

        # Scan forward from cut_idx to find a "user" message with plain string content
        # (not a tool_result which has list content)
        for i in range(cut_idx, len(messages)):
            msg = messages[i]
            if msg.get("role") == "user" and isinstance(msg.get("content"), str):
                return keep_start + messages[i:]

        # Fallback: if no safe cut found, just keep everything
        return messages

    @staticmethod
    def _has_typos(text: str) -> bool:
        """Simple heuristic typo detection — checks for common typo patterns.
        Typos indicate the person is typing manually (positive authenticity signal).
        """
        if len(text) < 20:
            return False

        indicators = 0
        lower = text.lower()

        # Double spaces (common when typing fast)
        if "  " in text:
            indicators += 1
        # Missing space after punctuation
        import re
        if re.search(r'[.,!?][a-zA-Z]', text):
            indicators += 1
        # Repeated letters (e.g., "thhe", "aand")
        if re.search(r'([a-z])\1{2,}', lower):
            indicators += 1
        # Common transpositions / misspellings
        common_typos = ["teh ", "hte ", "taht ", "adn ", "abot ", "becuase ", "buisness ",
                        "recieve ", "thier ", "wiht ", "whne ", "hav ", "dont ", "cant ",
                        "didnt ", "wasnt ", "isnt ", "im ", "ive ", "weve ", "theyre "]
        for typo in common_typos:
            if typo in lower:
                indicators += 1
                break
        # Missing capitalization after period
        if re.search(r'\. [a-z]', text):
            indicators += 1
        # No period at end of multi-sentence text
        if len(text) > 60 and ". " in text and not text.rstrip().endswith(('.', '!', '?')):
            indicators += 1

        return indicators >= 1

    def _get_paste_summary(self) -> str:
        """Summary of paste/typing behavior flags for system prompt."""
        if not self.paste_flags:
            return "All answers typed normally."

        pasted_count = sum(1 for f in self.paste_flags if f.get("pasted"))
        total = len(self.paste_flags)

        if pasted_count == 0:
            return "All answers typed normally."

        lines = [f"⚠ {pasted_count} of {total} tracked answers were PASTED (not typed):"]
        for f in self.paste_flags:
            if f.get("pasted"):
                q = f.get("question", "?")[:40]
                a = f.get("answer", "")[:40]
                lines.append(f"  - Q: \"{q}\" → answer was pasted (ratio: {f.get('keystroke_ratio', '?')})")

        lines.append("This is suspicious — the customer may be using prepared answers.")
        return "\n".join(lines)

    def _get_customer_stated_data(self) -> dict:
        """Get ONLY data the customer explicitly stated in conversation.
        Safe for the question-generation prompt. No external data.
        """
        stated = {}
        biz = self.case.business
        if biz.company_name:
            stated["company_name"] = biz.company_name
        if biz.company_type:
            stated["company_type"] = biz.company_type

        act = self.case.activity
        for field_name in ["products_services", "monthly_turnover_expected",
                           "number_of_employees", "payment_methods"]:
            val = getattr(act, field_name, None)
            if val:
                stated[field_name] = val

        return stated

    def _get_full_case_data(self) -> dict:
        """Get ALL case data including external sources — for internal use only.
        Used in full system message for Sonnet tool_use calls (start_interview, etc.)
        """
        compact_data = {}
        biz = self.case.business
        for field_name in ["company_name", "company_number", "company_type", "description",
                       "registered_address", "trading_address", "website", "incorporation_date",
                       "industry_sector"]:
            val = getattr(biz, field_name, None)
            if val:
                compact_data[field_name] = val
        act = self.case.activity
        for field_name in ["products_services", "target_customers", "revenue_model",
                       "monthly_turnover_expected", "annual_turnover_expected",
                       "number_of_employees", "payment_methods", "supplier_info"]:
            val = getattr(act, field_name, None)
            if val:
                compact_data[field_name] = val
        if self.case.red_flags:
            compact_data["red_flags"] = [f"[{f.get('severity')}] {f.get('description')}" for f in self.case.red_flags]
        return compact_data

    def _get_qa_summary(self) -> str:
        """Get compact Q&A log summary."""
        if not self.qa_log:
            return "None yet."
        qa_lines = []
        for pair in self.qa_log:
            a = pair["a"][:80] + "..." if len(pair["a"]) > 80 else pair["a"]
            qa_lines.append(f"Q: {pair['q']}\nA: {a}")
        return "\n".join(qa_lines)

    def _build_system_message(self) -> str:
        """Build FULL system message — used for start_interview and tool_use calls."""
        compact_data = self._get_full_case_data()
        recent_reasoning = self.reasoning_log[-3:] if self.reasoning_log else []
        qa_summary = self._get_qa_summary()
        investigation_summary = self.investigator.get_findings_summary()
        sanctions_summary = self.investigator.get_sanctions_summary()

        return ORCHESTRATOR_PROMPT + f"""

TODAY'S DATE: {datetime.now().strftime('%Y-%m-%d')}

COLLECTED DATA: {json.dumps(compact_data, ensure_ascii=False)}

QUESTIONS ALREADY ASKED (do NOT repeat these or ask about the same topic):
{qa_summary}

SANCTIONS SCREENING (DO NOT reveal to customer — internal compliance check):
{sanctions_summary if sanctions_summary else "Pending or no results yet."}

INVESTIGATION FINDINGS (for SILENT comparison with customer answers — NEVER use to formulate questions):
{investigation_summary if investigation_summary else "No findings yet."}

INPUT BEHAVIOR ALERTS (DO NOT reveal to customer — internal monitoring):
{self._get_paste_summary()}

RECENT REASONING: {json.dumps(recent_reasoning, ensure_ascii=False) if recent_reasoning else "Interview starting."}

STAGE: {self.case.business.description or "Unknown"}

REMINDERS: ONE question per message. You know NOTHING except what the customer told you above. No value judgments. Neutral transition (max 5 words) + one open-ended question. NEVER repeat a topic already covered.
"""

    # ─── Interview strategy ───

    @staticmethod
    def _init_strategy() -> dict:
        """Initialize the interview strategy."""
        return {
            "phase": "discovery",
            "focus": "person_background",
            "next_topics": [
                "person_background",
                "business_origin",
                "business_basics",
                "operations",
                "customers",
                "financials",
                "stress_test",
                "verification",
            ],
            "covered": [],           # Topics with sufficient answers
            "needs_probing": [],     # Topics where answers were vague or concerning
            "confidence": {          # 0.0–1.0 per area
                "person": 0.0,
                "business_existence": 0.0,
                "operations": 0.0,
                "financials": 0.0,
                "consistency": 0.0,
            },
            "tone": "open",          # open, probing, challenging, wrapping_up
            "reason": "Starting interview — learning about the person first.",
        }

    def _update_strategy(self):
        """Update interview strategy based on what we know so far.
        Called after collecting background results.
        """
        s = self.strategy
        q_count = len(self.qa_log)
        old_phase = s["phase"]
        old_tone = s["tone"]
        old_focus = s["focus"]

        # ── Update confidence scores based on available data ──
        biz = self.case.business
        act = self.case.activity

        # Person confidence: do we know who this is?
        person_signals = sum(1 for v in [
            self.case.person.full_name,
            any("person_background" in t for t in s["covered"]),
            any("business_origin" in t for t in s["covered"]),
        ] if v)
        s["confidence"]["person"] = min(person_signals / 3, 1.0)

        # Business existence: is there evidence it's real?
        biz_signals = sum(1 for v in [
            biz.company_name, biz.company_number, biz.company_type,
            biz.website, biz.registered_address,
            any(v.get("source") == "companies_house" for v in self.case.verifications),
        ] if v)
        s["confidence"]["business_existence"] = min(biz_signals / 4, 1.0)

        # Operations: can they describe how it works?
        ops_signals = sum(1 for v in [
            act.products_services, act.target_customers,
            act.supplier_info, act.payment_methods,
            any("operations" in t or "customers" in t for t in s["covered"]),
        ] if v)
        s["confidence"]["operations"] = min(ops_signals / 4, 1.0)

        # Financials: do the numbers make sense?
        fin_signals = sum(1 for v in [
            act.monthly_turnover_expected or act.annual_turnover_expected,
            act.number_of_employees,
            act.revenue_model,
            any("financials" in t for t in s["covered"]),
        ] if v)
        s["confidence"]["financials"] = min(fin_signals / 3, 1.0)

        # Consistency: are there contradictions?
        contradiction_count = sum(
            1 for r in self.reasoning_log
            if r.get("suspicion") in ("medium", "high")
        )
        red_flag_count = len(self.case.red_flags)
        s["confidence"]["consistency"] = max(0.0, 1.0 - (contradiction_count * 0.15 + red_flag_count * 0.2))

        # ── Decide phase ──
        avg_confidence = sum(s["confidence"].values()) / len(s["confidence"])
        remaining = self.question_budget - q_count

        # Once closing is entered, it's STICKY — never go back to deep_dive
        already_closing = (self._closing_entered_at is not None)
        missing_essentials = len(self._get_missing_essentials())

        if already_closing:
            s["phase"] = "closing"
        elif remaining <= 5:
            # Close to hard limit — start wrapping up
            s["phase"] = "closing"
            self._closing_entered_at = q_count
        elif q_count >= 15 and missing_essentials == 0 and avg_confidence > 0.75:
            # Enough questions + all essentials covered + high confidence → can start closing
            s["phase"] = "closing"
            self._closing_entered_at = q_count
        elif q_count < 3:
            s["phase"] = "discovery"
        elif q_count < 7:
            s["phase"] = "exploration"
        elif avg_confidence > 0.65 and q_count >= 10:
            s["phase"] = "closing"
            self._closing_entered_at = q_count
        elif q_count >= 7:
            s["phase"] = "deep_dive"

        # ── Decide tone ──
        if s["phase"] == "closing":
            s["tone"] = "wrapping_up"
        elif s["needs_probing"]:
            s["tone"] = "probing"
        elif contradiction_count >= 2 or red_flag_count >= 2:
            s["tone"] = "challenging"
        else:
            s["tone"] = "open"

        # ── Decide focus ──
        # Pick the first uncovered topic from next_topics
        remaining = [t for t in s["next_topics"] if t not in s["covered"]]

        # If something needs probing, prioritize it
        if s["needs_probing"]:
            s["focus"] = s["needs_probing"][0]
        elif remaining:
            s["focus"] = remaining[0]
        elif s["phase"] != "closing":
            s["focus"] = "verification"
        else:
            s["focus"] = "wrap_up"

        # ── Build reason string if strategy changed ──
        changes = []
        if old_phase != s["phase"]:
            changes.append(f"phase: {old_phase}→{s['phase']}")
        if old_tone != s["tone"]:
            changes.append(f"tone: {old_tone}→{s['tone']}")
        if old_focus != s["focus"]:
            changes.append(f"focus: {old_focus}→{s['focus']}")

        if changes:
            # Build reason
            low_areas = [k for k, v in s["confidence"].items() if v < 0.3]
            reason_parts = []
            if low_areas:
                reason_parts.append(f"Low confidence in: {', '.join(low_areas)}")
            if s["needs_probing"]:
                reason_parts.append(f"Needs probing: {', '.join(s['needs_probing'][:2])}")
            if s["phase"] == "closing":
                reason_parts.append("Sufficient information gathered")
            s["reason"] = ". ".join(reason_parts) if reason_parts else f"Progressing to {s['focus']}"

            self._add_reasoning({
                "note": f"📋 STRATEGY: {', '.join(changes)}. {s['reason']}",
                "suspicion": "none",
                "why": None,
            })

    def _get_missing_essentials(self) -> list[str]:
        """Return list of essential data points still missing."""
        missing = []
        p = self.case.person
        b = self.case.business
        a = self.case.activity
        if not p.full_name:
            missing.append("person's full name")
        if not (p.date_of_birth or p.nationality or p.country_of_residence):
            missing.append("person's nationality or country of residence")
        # company_name is pre-filled from the intake form — don't ask again
        if not a.products_services:
            missing.append("what the business does (products/services)")
        if not a.target_customers:
            missing.append("who the customers are")
        if not (a.monthly_turnover_expected or a.annual_turnover_expected):
            missing.append("expected revenue/turnover")
        if not a.payment_methods:
            missing.append("how they receive payments")
        return missing

    def _get_budget_prompt(self) -> str:
        """Build the question budget section for the prompt."""
        q_count = len(self.qa_log)
        budget = self.question_budget
        remaining = max(0, budget - q_count)
        missing = self._get_missing_essentials()

        lines = [f"INTERVIEW PROGRESS: {q_count} questions asked out of {self._q_hard_max} max."]

        if remaining <= 0:
            if missing:
                lines.append(f"AT LIMIT. Still missing: {', '.join(missing)}. Cover these EFFICIENTLY in 1-2 grouped questions, then call complete_interview.")
            else:
                lines.append("AT LIMIT. All essentials covered. Call complete_interview NOW.")
        elif remaining <= 3:
            if missing:
                lines.append(f"ALMOST DONE. Still need: {', '.join(missing)}. Group remaining topics — ask compound questions.")
            else:
                lines.append("All essentials covered. Call complete_interview when ready.")
        elif q_count >= 10 and not missing:
            lines.append("All essentials covered. Consider calling complete_interview — don't keep asking if you have what you need.")
        elif missing:
            lines.append(f"Still need to cover: {', '.join(missing)}. Be efficient — group related topics into single questions.")

        return "\n".join(lines)

    def _mark_topic_covered(self, topic: str):
        """Mark a topic as covered (answer was substantive)."""
        if topic not in self.strategy["covered"]:
            self.strategy["covered"].append(topic)
        # Remove from needs_probing if it was there
        if topic in self.strategy["needs_probing"]:
            self.strategy["needs_probing"].remove(topic)

    def _mark_topic_needs_probing(self, topic: str):
        """Mark a topic as needing follow-up (vague or concerning answer)."""
        if topic not in self.strategy["needs_probing"]:
            self.strategy["needs_probing"].append(topic)

    def _get_strategy_prompt(self) -> str:
        """Build the strategy section for the system prompt."""
        s = self.strategy
        conf = s["confidence"]

        # Phase descriptions
        phase_desc = {
            "discovery": "DISCOVERY — Get to know the person. Ask about their background, experience, motivation.",
            "exploration": "EXPLORATION — Understand the business. Operations, customers, suppliers, day-to-day.",
            "deep_dive": "DEEP DIVE — Verify details. Specific numbers, dates, names. Challenge vague answers.",
            "closing": "CLOSING (internal only) — Fill any missing essential data, then call complete_interview. Do NOT open new topics. NEVER signal to the customer that you are finishing. Ask remaining questions as if the conversation will continue for another 10 minutes.",
        }

        # Tone descriptions
        tone_desc = {
            "open": "Open and conversational. Let them talk.",
            "probing": "Probing — some answers were vague. Ask for specifics.",
            "challenging": "Challenging — contradictions or concerns detected. Press for clarity without being hostile.",
            "wrapping_up": "Continue naturally — enough information gathered. Fill any remaining gaps. Do NOT signal to the customer that you are finishing.",
        }

        # Focus descriptions
        focus_desc = {
            "person_background": "the person — who they are, what they did before, why this business",
            "business_origin": "how and why the business started, the founding story",
            "business_basics": "basic facts — company type, structure, when it started, where it's based",
            "operations": "day-to-day operations — processes, tools, routines, what a typical day looks like",
            "customers": "customers — who they are, how they find the business, how they pay",
            "financials": "financial picture — turnover, costs, margins, banking needs",
            "stress_test": "edge cases and failures — worst month, biggest problem, what could go wrong",
            "verification": "verification — return to anything vague or inconsistent, cross-check key facts",
            "wrap_up": "covering remaining topics naturally",
        }

        # Confidence summary
        low = [k for k, v in conf.items() if v < 0.3]
        medium = [k for k, v in conf.items() if 0.3 <= v < 0.7]
        high = [k for k, v in conf.items() if v >= 0.7]

        conf_line = ""
        if low:
            conf_line += f"Weak areas (need more info): {', '.join(low)}. "
        if high:
            conf_line += f"Strong areas: {', '.join(high)}. "

        # Remaining topics
        remaining = [t for t in s["next_topics"] if t not in s["covered"]]
        needs_probe = s["needs_probing"]

        lines = [
            f"PHASE: {phase_desc.get(s['phase'], s['phase'])}",
            f"TONE: {tone_desc.get(s['tone'], s['tone'])}",
            f"FOCUS NOW: Ask about {focus_desc.get(s['focus'], s['focus'])}.",
        ]
        if conf_line:
            lines.append(f"KNOWLEDGE: {conf_line}")
        if needs_probe:
            probe_labels = [focus_desc.get(t, t) for t in needs_probe[:2]]
            lines.append(f"NEEDS FOLLOW-UP: {'; '.join(probe_labels)}")
        if remaining and s["phase"] != "closing":
            lines.append(f"STILL TO COVER: {', '.join(remaining[:4])}")

        # In closing, show what's still missing
        if s["phase"] == "closing":
            missing = self._get_missing_essentials()
            if missing:
                lines.append(f"MUST STILL ASK ABOUT: {', '.join(missing)}")
            else:
                lines.append("All essentials covered. Call complete_interview.")

        return "\n".join(lines)

    def _build_fast_system_message(self) -> str:
        """Build COMPACT system message for fast Haiku question-generation calls.
        1-2 questions per call. Contains ONLY customer-stated data — no external data.
        """
        stated_data = self._get_customer_stated_data()
        qa_summary = self._get_qa_summary()

        return f"""You are a KYC specialist conducting an interview for a UK bank. One-on-one with a business owner applying for a bank account.

YOUR SINGLE GOAL: decide whether to APPROVE or REJECT this account application.
Every question must help you make that decision. If it doesn't move you closer to approve/reject — don't ask it.

You are NOT a journalist collecting a life story. You are NOT a business advisor.
You ARE a compliance officer having a conversation. You need: is the business real? Is the person running it? Where does the money flow? Any red flags?

You're experienced, sharp, and friendly. Like a senior banker who has seen thousands of cases. Warm but efficient.

TODAY'S DATE: {datetime.now().strftime('%Y-%m-%d')}

{self._get_budget_prompt()}

WHAT YOU MUST COVER (prioritize these):
1. What the business does (enough to understand the model)
2. Who are the customers
3. Financial picture: turnover, where money flows, source of funds
4. Where they operate, any international element
5. Team and operations (enough to judge if it's real)
6. Anything that raises doubt

WHAT YOU DON'T NEED:
- Full history of every past client engagement
- Detailed biography before this business
- How they solve technical problems
- Multiple follow-ups on topics already answered clearly
- Payment methods (card/cash/transfer) — operational detail for later
- Bookkeeping/accounting arrangements — not relevant to KYC
- ID documents — collected separately, NEVER during the interview

QUESTIONING DISCIPLINE:
- BE EFFICIENT: A good KYC interview covers everything in 10-20 questions, not 30+. No one wants to be interrogated for half an hour.
- GROUP RELATED TOPICS: Instead of asking "What's your turnover?" then "How do customers pay?" separately, combine: "Walk me through the money side — how much do you expect to make, and how will customers pay you?"
- ONE FOLLOW-UP MAX on a clear answer. If they answered specifically, move on. Don't micro-probe every detail.
- NEVER ask for information the customer already provided. Check what's stated before asking.
- GO DEEP ONLY WHEN SOMETHING IS OFF: vague answer, contradiction, suspicion → then push hard.
- ACCEPT SHORT ANSWERS: If they say "Lloyds" — you don't need to ask which branch, since when, or how they chose it.
- Ask yourself before each question: "Does this help me decide approve/reject AND has this NOT been answered already?" If either fails, skip it.

CONVERSATION STYLE:
- Be natural. Brief reaction (1 sentence max), then your next question.
- NEVER evaluate or compliment.
- Questions should flow naturally but always move toward uncovered essential areas.
- Default to OPEN-ENDED questions. Let them talk.

DEMAND VERIFIABLE SPECIFICS (your most powerful tool):
A fraudster can answer "What does your company do?" — they CANNOT give specific, verifiable details. Always ask for NAMES, DATES, AMOUNTS, and SPECIFIC EXAMPLES — not descriptions or processes.
- BAD: "How do you find clients?" → GOOD: "Can you give me an example of a recent client — how did that start?"
- BAD: "What's your biggest expense?" → GOOD: "Who's your main supplier, and roughly how much was your last order?"
- BAD: "How does invoicing work?" → GOOD: "What was the last invoice you sent — how much, and who to?"
THE PRINCIPLE: ask for THE LAST TIME something happened, or A SPECIFIC EXAMPLE. Real operators have immediate, messy memories. Fakers give clean, generic answers.
Extract: supplier names, client names, specific amounts, tool/software names, specific incidents, named third parties.
ASK FOR LINKS when natural: "Do you have a website?" / "Can you share a link to the property listing?" / "What's your LinkedIn?" / "Who are your main competitors?" — URLs are our MOST POWERFUL verification tool.
MANDATORY: Ask for at least ONE URL/link during the interview. If they have a business → ask for website. If they mention an app/product → ask for the name/URL. If discussing background → ask for LinkedIn. Our system deep-analyzes URLs: domain age, reviews, app store, search indexation.

ADAPTIVE DEPTH (go fast when clean, dig when suspicious):
TRUST BUILDS: specific names/dates/amounts without hesitation, messy real-life details, irregular numbers (£4,300 not £5,000), emotional engagement, problems/frustrations mentioned voluntarily.
TRUST DROPS: vague ("various clients"), round numbers everywhere, can't name a single supplier/client/tool, rehearsed-sounding answers, contradictions, deflects from specifics.

IF TRUST IS HIGH (2-3 strong signals): move through remaining topics FAST. Group questions: "Quick practical stuff — where are you based, who does your books, and where do you bank?" Finish sooner.
IF TRUST IS LOW: slow down. Ask for specific examples on every claim. "You mentioned regular clients — can you name one or two?" If they still can't provide specifics → significant red flag. Note it, move on, let the risk assessor handle it.

GET TOUGH only when something doesn't add up:
- Vague where a real owner would be specific → push for a concrete example
- Can't provide a single specific name/number when asked directly
- Numbers don't match → do the math out loud
- Contradiction → call it out directly
- They avoid a question → come back to it from a different angle

When the answer is clear, specific, and includes verifiable details — accept it and move on.

HARD RULES:
1. Ask 1–2 RELATED questions per message. Never 3+.
2. You know ONLY what the customer told you. NEVER reference external data.
3. NEVER give value judgments.
4. NEVER repeat a well-covered topic.
5. DOCUMENTS: If a hint is provided below, mention BUSINESS documents casually (bank statement, certificate of incorporation). If they decline, move on. NEVER request ID documents (passport, driving licence, utility bill) — those are collected separately.
6. NEVER tell the customer you are wrapping up, finishing, or that this is the last/final question. The interview ends abruptly with complete_interview — the customer should not see it coming. Just keep asking naturally.
7. FORBIDDEN PHRASES: "just one more question", "one more thing", "before we wrap up", "before we finish", "lastly", "to wrap up", "one final question", "just one more practical question", "one last thing", "to finish up". Every question must sound like the MIDDLE of the conversation. Never signal the end.
8. NEVER ask about: payment methods (card/cash/transfer), bookkeeping/accounting arrangements, or ID documents. These are out of scope for the interview.

PACING:
- Vague answer → push back ONCE. If still vague, note it and move on.
- Max 2 questions per topic. Then MOVE ON.
- If a topic is naturally exhausted (clear answer, no red flags), don't linger.
- For pre-acquisition / pre-launch businesses: they won't know operational details yet. Don't press on things they haven't done.
- When all essential areas are covered and nothing suspicious remains: call complete_interview immediately.

WHAT CUSTOMER HAS STATED SO FAR: {json.dumps(stated_data, ensure_ascii=False) if stated_data else "Nothing yet."}
{self._get_precheck_context_for_haiku()}
CONVERSATION SO FAR:
{qa_summary}

INTERVIEW STRATEGY:
{self._get_strategy_prompt()}
{self._get_doc_suggestion_hint()}
Follow the strategy. Natural transition from their last answer, then your question(s) for the current FOCUS.

CRITICAL — YOUR RESPONSE MUST NOT CONTAIN ANY OF THESE WORDS/PHRASES: "one more", "last question", "final question", "before we wrap", "before we finish", "to wrap up", "lastly", "to finish", "wrapping up", "winding down", "almost done", "nearly there", "just quickly". If you catch yourself writing any of these — DELETE and rewrite. Every question must sound like the MIDDLE of the conversation."""

    def _get_precheck_context_for_haiku(self) -> str:
        """Return precheck results for Haiku prompt — only if they've arrived."""
        if not self._initial_check_results:
            return ""
        return f"""
BACKGROUND CHECK RESULTS (INTERNAL — for comparing with customer answers AFTER they respond):
{json.dumps(self._initial_check_results, ensure_ascii=False, default=str)[:3000]}

RULES: This data is ONLY for cross-referencing with customer answers. NEVER hint you know anything. Ask blank-slate questions. Compare AFTER they answer. Do NOT call search_person/search_company again — already done.
"""

    def _get_doc_suggestion_hint(self) -> str:
        """Return a document suggestion hint for the prompt, if any pending."""
        pending = getattr(self, '_pending_doc_suggestions', [])
        if not pending:
            return ""
        # Take the first suggestion and clear it
        doc_type = pending.pop(0)
        # Only business documents — no ID docs
        if doc_type in ("passport", "driving_licence", "utility_bill"):
            return ""  # Skip ID document suggestions
        doc_labels = {
            "bank_statement": "a recent bank statement",
            "certificate_of_incorporation": "the certificate of incorporation",
            "lease_agreement": "the lease or rental agreement",
            "invoice_sample": "a sample invoice",
            "tax_return": "a recent tax return",
            "contract_sample": "a sample client contract",
        }
        label = doc_labels.get(doc_type, doc_type.replace("_", " "))
        return f"\nDOCUMENT HINT: Consider asking the customer if they could share {label} to support what they've told you. Ask casually — if they say no, that's fine.\n"

    def _get_clean_messages(self) -> list:
        """Get messages stripped of tool_use/tool_result for Haiku calls.
        Haiku is called without tools, so tool_use blocks cause API errors.
        """
        clean = []
        for msg in self.messages:
            content = msg.get("content", "")
            role = msg.get("role", "")

            # Skip tool_result messages entirely
            if role == "user" and isinstance(content, list):
                # Filter out tool_result blocks, keep text blocks
                text_parts = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "tool_result":
                            continue
                        if block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                    elif isinstance(block, str):
                        text_parts.append(block)
                if text_parts:
                    clean.append({"role": "user", "content": " ".join(text_parts)})
                continue

            # For assistant messages, extract only text content
            if role == "assistant":
                if isinstance(content, str):
                    clean.append(msg)
                elif isinstance(content, list):
                    text_parts = []
                    for block in content:
                        if hasattr(block, "text"):
                            text_parts.append(block.text)
                        elif isinstance(block, dict) and block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                    if text_parts:
                        clean.append({"role": "assistant", "content": " ".join(text_parts)})
                continue

            # Plain string content — keep as-is
            if isinstance(content, str) and content:
                clean.append(msg)

        return clean

    async def _call_claude_fast(self, system_msg: str, from_thread: bool = False) -> str:
        """Fast Haiku call — generates ONE question per call. Streamed for speed."""
        client = anthropic.AsyncAnthropic() if from_thread else self.client
        clean_messages = self._get_clean_messages()

        text = ""
        try:
            async with client.messages.stream(
                model=MODEL_FAST,
                max_tokens=256,
                system=system_msg,
                messages=clean_messages
            ) as stream:
                async for chunk in stream.text_stream:
                    text += chunk
        except Exception:
            # Fallback if streaming fails
            response = await client.messages.create(
                model=MODEL_FAST,
                max_tokens=256,
                system=system_msg,
                messages=clean_messages
            )
            for block in response.content:
                if hasattr(block, "text"):
                    text = block.text.strip()
                    break

        text = text.strip()

        # Strip wrapping/closing phrases that leak interview-ending signals
        text = self._strip_wrapping_phrases(text)

        # Detect if Haiku tried to "call" complete_interview by writing it as text.
        # Haiku has no tools, so it outputs the tool name as a string instead.
        if "complete_interview" in text.lower():
            # Extract any farewell message that came before the tool name
            farewell = ""
            for line in text.split("\n"):
                low = line.strip().lower()
                if "complete_interview" in low or "interview complete" in low:
                    break
                if line.strip():
                    farewell += line.strip() + " "
            farewell = farewell.strip()

            self.interview_complete = True
            self._add_reasoning({
                "note": "Interview complete (agent signalled completion).",
                "suspicion": "none"
            })
            if farewell:
                self.messages.append({"role": "assistant", "content": farewell})
                return farewell
            return "Thank you for your time. I have all the information I need to proceed with the assessment."

        if not text:
            # Last resort: full tool_use call with Sonnet
            response = await client.messages.create(
                model=MODEL_FULL,
                max_tokens=2048,
                system=self._build_system_message(),
                tools=ORCHESTRATOR_TOOLS,
                messages=self.messages
            )
            return await self._process_response(response, self._build_system_message())

        self.messages.append({"role": "assistant", "content": text})
        self._last_question = text
        self.question_queue = []  # No queue — one question per call

        self.case.save(self.case_file_path)
        return text

    async def _call_claude(self, system_msg: str) -> str:
        """Call Claude with tools — used for start_interview and special cases."""
        self._sanitize_messages()
        response = await self.client.messages.create(
            model=MODEL_FULL,
            max_tokens=2048,
            system=system_msg,
            tools=ORCHESTRATOR_TOOLS,
            messages=self.messages
        )
        return await self._process_response(response, system_msg)

    def _sanitize_messages(self):
        """Ensure no orphaned tool_use blocks exist in the message history.
        The Anthropic API requires every tool_use to be immediately followed
        by a tool_result. If we find a violation, strip the tool_use blocks
        from the offending assistant message.
        """
        i = 0
        while i < len(self.messages):
            msg = self.messages[i]
            if msg.get("role") != "assistant":
                i += 1
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                i += 1
                continue

            # Check if this assistant message contains tool_use blocks
            has_tool_use = any(
                (hasattr(b, "type") and b.type == "tool_use") or
                (isinstance(b, dict) and b.get("type") == "tool_use")
                for b in content
            )
            if not has_tool_use:
                i += 1
                continue

            # Check if the next message is a tool_result
            next_msg = self.messages[i + 1] if i + 1 < len(self.messages) else None
            next_is_tool_result = (
                next_msg and
                next_msg.get("role") == "user" and
                isinstance(next_msg.get("content"), list) and
                any(
                    isinstance(b, dict) and b.get("type") == "tool_result"
                    for b in next_msg["content"]
                )
            )

            if not next_is_tool_result:
                # Strip tool_use blocks, keep only text
                text_parts = []
                for b in content:
                    if hasattr(b, "text"):
                        text_parts.append(b.text)
                    elif isinstance(b, dict) and b.get("type") == "text":
                        text_parts.append(b.get("text", ""))
                self.messages[i] = {
                    "role": "assistant",
                    "content": " ".join(text_parts) if text_parts else "(no text)"
                }
            i += 1

    async def _process_response(self, response, system_msg: str) -> str:
        """Process response, execute tools, recurse if needed.

        Handles both old format {"message": "..."} and new batch format {"questions": [...]}.
        When questions array is found, first question is returned immediately,
        rest go into the queue.
        """
        customer_message = ""
        tool_results = []

        for block in response.content:
            if block.type == "text":
                text = block.text.strip()
                if not text:
                    continue

                # Try to find JSON anywhere in the text
                json_start = text.find('{')
                json_end = text.rfind('}')

                if json_start >= 0 and json_end > json_start:
                    pre_json = text[:json_start].strip()

                    try:
                        parsed = json.loads(text[json_start:json_end + 1])

                        # NEW: batch questions format
                        if parsed.get("questions") and isinstance(parsed["questions"], list):
                            questions = [q for q in parsed["questions"] if q and isinstance(q, str)]
                            if questions:
                                customer_message = questions[0]
                                self._last_question = questions[0]
                                # Queue remaining questions
                                self.question_queue = questions[1:]

                        # OLD: single message format (fallback)
                        elif parsed.get("message"):
                            customer_message = parsed["message"]
                            self._last_question = parsed["message"]
                        elif pre_json:
                            customer_message = pre_json

                        # Process inline reasoning
                        if parsed.get("reasoning"):
                            self._add_reasoning(parsed["reasoning"])
                        # Process data_to_save
                        if parsed.get("data_to_save"):
                            ds = parsed["data_to_save"]
                            self._save_case_data({"section": ds["section"], "data": ds["data"]})
                    except (json.JSONDecodeError, AttributeError, TypeError):
                        if pre_json:
                            customer_message = pre_json
                        else:
                            customer_message = text
                else:
                    if not customer_message:
                        customer_message = text

            elif block.type == "tool_use":
                result = await self._execute_tool(block.name, block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result, ensure_ascii=False)
                })

        if tool_results:
            self.messages.append({"role": "assistant", "content": response.content})
            self.messages.append({"role": "user", "content": tool_results})

            follow_up = await self.client.messages.create(
                model=MODEL_FULL,
                max_tokens=2048,
                system=system_msg,
                tools=ORCHESTRATOR_TOOLS,
                messages=self.messages
            )
            follow_up_message = await self._process_response(follow_up, system_msg)
            # If the current response already had text (e.g. greeting + tool calls),
            # prefer it over the follow-up which is often empty after tool processing.
            msg = follow_up_message if follow_up_message else customer_message
            return self._strip_wrapping_phrases(msg) if msg else msg
        else:
            self.messages.append({"role": "assistant", "content": response.content})
            self.case.save(self.case_file_path)
            return self._strip_wrapping_phrases(customer_message) if customer_message else customer_message

    @staticmethod
    def _strip_wrapping_phrases(text: str) -> str:
        """Remove interview-ending signal phrases from agent output.
        These phrases leak that the interview is about to end, which we want to avoid."""
        if not text:
            return text
        import re
        # Patterns that signal wrapping up — each removes the full phrase + surrounding fluff
        patterns = [
            # "Just one more practical question —" / "One more thing —"
            r"(?i)\b(?:just\s+)?one\s+more\s+(?:quick\s+)?(?:thing|question|practical\s+question)\s*[—–:,.\-]*\s*",
            # "One last/final question —"
            r"(?i)\b(?:just\s+)?one\s+(?:last|final)\s+(?:thing|question)\s*[—–:,.\-]*\s*",
            # "Last/final question —"
            r"(?i)\b(?:last|final)\s+question\s*[—–:,.\-]*\s*",
            # "Before we wrap up, " / "Before we finish, "
            r"(?i)\bbefore\s+we\s+(?:wrap|finish|end)\s*(?:up)?\s*[,—–.\-]*\s*",
            # "To wrap up, " / "To finish up, " / "To finish off, "
            r"(?i)\bto\s+(?:wrap|finish)\s+(?:up|off)\s*[,—–.\-]*\s*",
            # "Wrapping up, " / "Winding down, "
            r"(?i)\b(?:wrapping|winding)\s+(?:up|down)\s*[,—–.\-]*\s*",
            # "Almost done." / "Nearly there."
            r"(?i)\b(?:almost\s+done|nearly\s+there)\s*[.,!]*\s*",
            # "Just quickly, "
            r"(?i)\bjust\s+quickly\s*[,—–.\-]*\s*",
            # "Lastly, "
            r"(?i)\blastly\s*[,—–.\-]*\s*",
        ]
        result = text
        for pat in patterns:
            result = re.sub(pat, "", result)
        # Clean up artifacts
        result = re.sub(r"\s*[—–]\s*[—–]\s*", " — ", result)  # double dashes
        result = re.sub(r"\.\s*\.\s*", ". ", result)  # double periods
        result = re.sub(r"^\s*[,—–.:;]\s*", "", result)  # leading punct at start
        result = re.sub(r"([.!?])\s+([a-z])", lambda m: m.group(1) + " " + m.group(2).upper(), result)  # capitalize after sentence end
        result = re.sub(r",\s+([A-Z])", lambda m: ". " + m.group(1), result)  # ", Who" → ". Who" (only if capital follows comma)
        result = re.sub(r"\s{2,}", " ", result).strip()
        # Capitalize first letter if needed
        if result and result[0].islower():
            result = result[0].upper() + result[1:]
        return result

    def _add_reasoning(self, reasoning: dict):
        """Add a reasoning entry to the log. Skips near-duplicates."""
        note = reasoning.get("note", "").strip().lower()
        if not note:
            return

        # Dedup: skip if very similar to any of the last 10 entries
        for prev in self.reasoning_log[-10:]:
            prev_note = prev.get("note", "").strip().lower()
            if not prev_note:
                continue
            # Exact match
            if note == prev_note:
                return
            # High overlap — check if one contains the other or >80% word overlap
            words_new = set(note.split())
            words_old = set(prev_note.split())
            if words_new and words_old:
                overlap = len(words_new & words_old) / max(len(words_new), len(words_old))
                if overlap > 0.8:
                    return

        entry = {
            "timestamp": datetime.now().isoformat(),
            **reasoning
        }
        self.reasoning_log.append(entry)

    async def _execute_tool(self, tool_name: str, tool_input: dict) -> dict:
        """Execute tool and return result."""

        if tool_name == "save_case_data":
            return self._save_case_data(tool_input)

        elif tool_name == "log_reasoning":
            self._add_reasoning(tool_input)
            return {"status": "reasoning_logged"}

        elif tool_name == "request_document":
            self.pending_document_request = tool_input
            return {"status": "document_requested", "doc_type": tool_input["doc_type"]}

        elif tool_name == "analyze_document":
            result = await document_tools.analyze_document(
                tool_input["file_path"], tool_input["doc_type"], self.client
            )
            self.case.documents.append({
                "doc_type": tool_input["doc_type"],
                "file_path": tool_input["file_path"],
                "upload_time": datetime.now().isoformat(),
                "extracted_data": result.get("extracted_data", {}),
                "analysis_notes": result.get("raw_response", ""),
                "verified": False
            })
            return result

        elif tool_name == "verify_companies_house":
            results = {}
            query = tool_input["query"]
            company_number = tool_input.get("company_number")

            if company_number:
                profile = await companies_house.get_company_profile(company_number)
                officers = await companies_house.get_company_officers(company_number)
                filings = await companies_house.get_filing_history(company_number)
                psc = await companies_house.get_persons_with_significant_control(company_number)
                results = {"profile": profile, "officers": officers, "filing_history": filings, "psc": psc}
            else:
                search = await companies_house.search_company(query)
                results = {"search_results": search}

            self.case.verifications.append({
                "source": "companies_house", "query": query,
                "result": results, "timestamp": datetime.now().isoformat()
            })
            return results

        elif tool_name == "search_web":
            result = await web_search.search_business_online(tool_input["query"])
            self.case.verifications.append({
                "source": "web_search", "query": tool_input["query"],
                "purpose": tool_input.get("purpose", ""),
                "result": result, "timestamp": datetime.now().isoformat()
            })
            return result

        elif tool_name == "search_person":
            result = await web_search.search_person_online(
                tool_input["full_name"],
                tool_input.get("business_name", ""),
                tool_input.get("location", "UK")
            )
            self.case.verifications.append({
                "source": "person_search", "query": tool_input["full_name"],
                "result": result, "timestamp": datetime.now().isoformat()
            })
            return result

        elif tool_name == "search_company":
            result = await web_search.search_company_online(
                tool_input["company_name"],
                tool_input.get("location", "UK")
            )
            self.case.verifications.append({
                "source": "company_search", "query": tool_input["company_name"],
                "result": result, "timestamp": datetime.now().isoformat()
            })
            return result

        elif tool_name == "check_website":
            result = await web_search.check_website_exists(tool_input["url"])
            self.case.verifications.append({
                "source": "website_check", "query": tool_input["url"],
                "result": result, "timestamp": datetime.now().isoformat()
            })
            return result

        elif tool_name == "check_domain_age":
            result = await verification.check_domain_whois(tool_input["domain"])
            self.case.verifications.append({
                "source": "domain_whois", "query": tool_input["domain"],
                "result": result, "timestamp": datetime.now().isoformat()
            })
            return result

        elif tool_name == "check_address":
            result = await verification.check_address_type(tool_input["address"])
            self.case.verifications.append({
                "source": "address_check", "query": tool_input["address"],
                "result": result, "timestamp": datetime.now().isoformat()
            })
            return result

        elif tool_name == "search_reviews":
            result = await verification.search_reviews(
                tool_input["business_name"],
                tool_input.get("location", "UK")
            )
            self.case.verifications.append({
                "source": "reviews_search", "query": tool_input["business_name"],
                "result": result, "timestamp": datetime.now().isoformat()
            })
            return result

        elif tool_name == "verify_vat":
            result = await verification.verify_vat_number(tool_input["vat_number"])
            self.case.verifications.append({
                "source": "vat_verification", "query": tool_input["vat_number"],
                "result": result, "timestamp": datetime.now().isoformat()
            })
            return result

        elif tool_name == "get_industry_benchmarks":
            result = await verification.get_industry_benchmarks(tool_input["industry"])
            # Don't store benchmarks as verification — it's reference data
            return result

        elif tool_name == "search_google_maps":
            result = await verification.search_google_maps(tool_input["query"])
            self.case.verifications.append({
                "source": "google_maps", "query": tool_input["query"],
                "result": result, "timestamp": datetime.now().isoformat()
            })
            return result

        elif tool_name == "deep_analyze_website":
            result = await web_analysis.deep_analyze_website(tool_input["url"])
            self.case.verifications.append({
                "source": "deep_website_analysis", "query": tool_input["url"],
                "result": {
                    "reliability_score": result.get("reliability_score"),
                    "positive_signals": result.get("positive_signals"),
                    "red_flags": result.get("red_flags"),
                    "summary": result.get("summary"),
                    "signals": result.get("signals"),
                },
                "timestamp": datetime.now().isoformat()
            })
            return result

        elif tool_name == "deep_analyze_linkedin":
            result = await web_analysis.deep_analyze_linkedin(tool_input["linkedin_url"])
            self.case.verifications.append({
                "source": "deep_linkedin_analysis", "query": tool_input["linkedin_url"],
                "result": {
                    "type": result.get("type"),
                    "reliability_score": result.get("reliability_score"),
                    "positive_signals": result.get("positive_signals"),
                    "red_flags": result.get("red_flags"),
                    "summary": result.get("summary"),
                },
                "timestamp": datetime.now().isoformat()
            })
            return result

        elif tool_name == "check_sanctions":
            name = tool_input["name"]
            entity_type = tool_input.get("entity_type", "auto")
            relationship = tool_input.get("relationship", "unknown")

            # Use the investigator's method which handles deduplication
            self.investigator.check_name_for_sanctions(name, entity_type)

            # Wait briefly for the result (it runs in a thread)
            import time
            for _ in range(20):  # Wait up to 2 seconds
                time.sleep(0.1)
                with self.investigator._lock:
                    extra = self.investigator.sanctions_results.get("extra_names", [])
                    match = next((r for r in extra if r.get("name", "").lower() == name.lower()), None)
                    if match:
                        break

            if match:
                sanctioned = match.get("sanctioned", False)
                if sanctioned is True:
                    self.case.red_flags.append({
                        "severity": "critical",
                        "description": f"SANCTIONS HIT on counterparty ({relationship}): {name}",
                        "evidence": json.dumps(match.get("matches", [])[:2], ensure_ascii=False)[:300],
                        "timestamp": datetime.now().isoformat(),
                    })
                    return {"name": name, "relationship": relationship, "sanctioned": True,
                            "matches": match.get("matches", [])[:3],
                            "warning": "CRITICAL — this counterparty is on sanctions lists"}
                elif sanctioned == "possible_match":
                    self.case.red_flags.append({
                        "severity": "high",
                        "description": f"Possible sanctions match on counterparty ({relationship}): {name}",
                        "evidence": json.dumps(match.get("matches", [])[:2], ensure_ascii=False)[:300],
                        "timestamp": datetime.now().isoformat(),
                    })
                    return {"name": name, "relationship": relationship, "sanctioned": "possible_match",
                            "matches": match.get("matches", [])[:3],
                            "warning": "Possible match — needs manual review"}
                else:
                    return {"name": name, "relationship": relationship, "sanctioned": False,
                            "message": "No sanctions matches found"}
            else:
                # Check is still running or name was already checked
                return {"name": name, "relationship": relationship,
                        "status": "check_initiated", "message": "Sanctions check running in background. Results will appear in sanctions summary."}

        elif tool_name == "flag_concern":
            self.case.add_red_flag(
                category=tool_input["category"],
                severity=tool_input["severity"],
                description=tool_input["description"],
                evidence=tool_input.get("evidence", "")
            )
            return {"status": "flag_recorded"}

        elif tool_name == "complete_interview":
            self.interview_complete = True
            # Add final reasoning entry
            self._add_reasoning({
                "note": f"Interview complete. {tool_input.get('summary', '')}",
                "suspicion": "none"
            })
            return {"status": "interview_complete"}

        return {"error": f"Unknown tool: {tool_name}"}

    def _save_case_data(self, tool_input: dict) -> dict:
        """Update case data."""
        section = tool_input["section"]
        data = tool_input["data"]

        if section == "business":
            for key, value in data.items():
                if hasattr(self.case.business, key):
                    setattr(self.case.business, key, value)
        elif section == "activity":
            for key, value in data.items():
                if hasattr(self.case.activity, key):
                    setattr(self.case.activity, key, value)

        self.case.updated_at = datetime.now().isoformat()
        self.case.save(self.case_file_path)
        return {"status": "saved", "section": section, "fields": list(data.keys())}

    def get_reasoning_log(self) -> list:
        """Return the full reasoning log."""
        return self.reasoning_log

    def get_reasoning_report(self) -> str:
        """Generate a readable reasoning report for download."""
        lines = []
        lines.append("=" * 70)
        lines.append("KYC AGENT — INTERNAL REASONING LOG")
        lines.append(f"Case: {self.case.case_id}")
        lines.append(f"Generated: {datetime.now().isoformat()}")
        lines.append("=" * 70)
        lines.append("")

        for i, entry in enumerate(self.reasoning_log, 1):
            lines.append(f"--- Step {i} [{entry.get('timestamp', '')}] ---")
            # Support both old and new format
            if entry.get("note"):
                lines.append(f"NOTE: {entry['note']}")
            if entry.get("learned"):
                lines.append(f"LEARNED: {entry['learned']}")
            if entry.get("assessment"):
                lines.append(f"ASSESSMENT: {entry['assessment']}")
            suspicion = entry.get("suspicion") or entry.get("suspicion_level", "")
            if suspicion:
                lines.append(f"SUSPICION: {suspicion}")
            if entry.get("why"):
                lines.append(f"REASON: {entry['why']}")
            lines.append("")

        lines.append("=" * 70)
        lines.append(f"RED FLAGS ({len(self.case.red_flags)}):")
        for flag in self.case.red_flags:
            lines.append(f"  [{flag.get('severity', '').upper()}] {flag.get('description', '')}")
            if flag.get("evidence"):
                lines.append(f"    Evidence: {flag['evidence']}")
        lines.append("")

        lines.append(f"VERIFICATIONS ({len(self.case.verifications)}):")
        for v in self.case.verifications:
            lines.append(f"  [{v.get('source', '')}] {v.get('query', '')}")
        lines.append("")

        lines.append("COLLECTED DATA:")
        lines.append(json.dumps(self.case.to_dict(), indent=2, ensure_ascii=False))

        return "\n".join(lines)


async def run_risk_assessment(case: KYCCase, client: anthropic.AsyncAnthropic,
                               reasoning_log: list = None,
                               verification_findings: list = None,
                               sanctions_results: dict = None) -> dict:
    """Run risk assessment on completed case."""
    case_data = case.to_dict()

    extra_context = ""
    if reasoning_log:
        extra_context += f"\n\nAGENT REASONING LOG (internal notes from the interviewing agent):\n{json.dumps(reasoning_log, indent=2, ensure_ascii=False)}"

    if verification_findings:
        extra_context += f"\n\nVERIFICATION FINDINGS (background fact-checking results):\n{json.dumps(verification_findings, indent=2, ensure_ascii=False)}"

    if sanctions_results:
        extra_context += f"\n\nSANCTIONS SCREENING RESULTS:\n{json.dumps(sanctions_results, indent=2, ensure_ascii=False)}"

    response = await client.messages.create(
        model=MODEL_FULL,
        max_tokens=6000,
        system=RISK_ASSESSOR_PROMPT,
        messages=[{
            "role": "user",
            "content": f"""Assess this KYC case.

COMPLETE CASE DATA:
{json.dumps(case_data, indent=2, ensure_ascii=False)}
{extra_context}

Return your assessment as a JSON object."""
        }]
    )

    result_text = response.content[0].text
    try:
        json_start = result_text.find('{')
        json_end = result_text.rfind('}') + 1
        if json_start >= 0 and json_end > json_start:
            return json.loads(result_text[json_start:json_end])
    except json.JSONDecodeError:
        pass

    return {"raw_assessment": result_text, "error": "Could not parse structured assessment"}
