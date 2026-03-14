"""
Fact Extractor Agent — parses customer answers into structured verifiable claims.

Uses Claude Haiku (fast, cheap) to extract facts from each answer.
Filters unverifiable claims before they reach the Verification Engine.
Handles the Casino.com problem (client names vs URLs).
"""
import json
import re
from datetime import datetime
from typing import Optional

from anthropic import AsyncAnthropic

from agents.prompts import FACT_EXTRACTOR_PROMPT

MODEL_EXTRACTOR = "claude-haiku-4-5-20251001"


class FactExtractor:
    """Extracts verifiable facts from customer answers."""

    def __init__(self):
        self.client = AsyncAnthropic()
        self.all_facts: list[dict] = []  # Cumulative facts from all answers
        self.extraction_log: list[dict] = []
        self._known_client_names: set[str] = set()  # Track client names to avoid URL false positives

    async def extract_facts(self, customer_answer: str,
                            question_context: str = "",
                            conversation_summary: str = "") -> list[dict]:
        """Extract verifiable facts from a customer answer.

        Returns a list of fact objects with type, value, context, search_query.
        """
        prompt = f"""Extract verifiable facts from this customer answer.

QUESTION ASKED: {question_context}

CUSTOMER'S ANSWER: {customer_answer}

CONVERSATION CONTEXT (what we know so far): {conversation_summary}

KNOWN CLIENT/COMPANY NAMES (do NOT treat these as URLs even if they end in .com):
{', '.join(self._known_client_names) if self._known_client_names else 'None yet'}

Return a JSON array of facts."""

        try:
            response = await self.client.messages.create(
                model=MODEL_EXTRACTOR,
                max_tokens=2048,
                system=FACT_EXTRACTOR_PROMPT,
                messages=[{"role": "user", "content": prompt}]
            )
            text = response.content[0].text.strip()

            # Extract JSON array
            facts = self._parse_facts_json(text)

            # Post-processing
            facts = self._filter_unverifiable(facts)
            facts = self._fix_url_vs_client_name(facts)
            facts = self._inject_urls_from_text(customer_answer, facts)
            facts = self._deduplicate(facts)

            # Track client names
            for f in facts:
                if f.get("type") in ("client_name", "supplier", "partner", "counterparty"):
                    self._known_client_names.add(f.get("value", "").lower())

            # Log
            self.extraction_log.append({
                "timestamp": datetime.now().isoformat(),
                "answer": customer_answer[:200],
                "facts_extracted": len(facts),
                "facts": facts,
            })

            self.all_facts.extend(facts)
            return facts

        except Exception as e:
            self.extraction_log.append({
                "timestamp": datetime.now().isoformat(),
                "answer": customer_answer[:200],
                "error": str(e),
            })
            return []

    def _parse_facts_json(self, text: str) -> list[dict]:
        """Parse JSON array of facts from Claude response."""
        # Try to find JSON array
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]

        # Try direct parse
        try:
            result = json.loads(text.strip())
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

        # Find array in text
        arr_start = text.find('[')
        arr_end = text.rfind(']')
        if arr_start >= 0 and arr_end > arr_start:
            try:
                return json.loads(text[arr_start:arr_end + 1])
            except json.JSONDecodeError:
                pass

        return []

    def _filter_unverifiable(self, facts: list[dict]) -> list[dict]:
        """Remove facts that can't be verified online."""
        unverifiable_patterns = [
            r"customer is \w+",  # "customer is Israeli"
            r"(?:i|he|she) (?:am|is|was) (?:a )?(?:hard worker|passionate|experienced)",
            r"(?:i|we) (?:love|enjoy|like) ",
            r"(?:business|company) is (?:good|great|doing well)",
        ]

        filtered = []
        for fact in facts:
            value = fact.get("value", "").lower()
            fact_type = fact.get("type", "")

            # Skip if explicitly marked unverifiable
            if fact.get("verifiable") is False:
                continue

            # Skip short/useless values
            if len(value.strip()) < 2:
                continue

            # Skip unverifiable patterns
            skip = False
            for pattern in unverifiable_patterns:
                if re.search(pattern, value, re.IGNORECASE):
                    skip = True
                    break

            if not skip:
                filtered.append(fact)

        return filtered

    def _fix_url_vs_client_name(self, facts: list[dict]) -> list[dict]:
        """Fix false positives where client names are mistaken for URLs.

        Example: "Casino.com" as a client name, not a URL to visit.
        """
        fixed = []
        for fact in facts:
            value = fact.get("value", "")
            fact_type = fact.get("type", "")

            # If it's marked as URL/website, check if it's actually a client name
            if fact_type in ("website", "url"):
                value_lower = value.lower().strip()
                # Check against known client names
                if value_lower in self._known_client_names:
                    fact["type"] = "client_name"
                    fact["note"] = "Reclassified: was marked as URL but is a known client name"

                # Check if the context suggests it's being used as a company name
                context = fact.get("context", "").lower()
                name_indicators = ["client", "customer", "works with", "contract with",
                                   "project for", "invoice", "supplier", "partner"]
                if any(ind in context for ind in name_indicators):
                    # It's probably a company name that happens to look like a domain
                    if not value.startswith("http") and "/" not in value:
                        fact["type"] = "client_name"
                        fact["note"] = "Reclassified: mentioned in client/company context"

            fixed.append(fact)

        return fixed

    @staticmethod
    def _inject_urls_from_text(text: str, facts: list[dict]) -> list[dict]:
        """Extract URLs/domains from text that Claude might have missed."""
        existing_urls = set()
        for f in facts:
            if f.get("type") in ("website", "url", "linkedin_profile"):
                existing_urls.add(f.get("value", "").lower().strip().rstrip("/"))

        new_facts = []

        # Full URLs
        url_pattern = re.compile(
            r'https?://[^\s<>"\'\),]+',
            re.IGNORECASE
        )
        for m in url_pattern.finditer(text):
            url = m.group().rstrip(".,;:!?)")
            url_lower = url.lower().rstrip("/")
            if url_lower not in existing_urls:
                existing_urls.add(url_lower)
                fact_type = "linkedin_profile" if "linkedin.com" in url_lower else "website"
                new_facts.append({
                    "type": fact_type,
                    "value": url,
                    "context": "Extracted from answer text",
                    "search_query": url,
                    "verifiable": True,
                })
                # Also mark bare domain as seen
                domain = re.sub(r'https?://(www\.)?', '', url_lower).split('/')[0]
                existing_urls.add(domain)

        # Bare domains (e.g., "theleverage.net")
        domain_pattern = re.compile(
            r'\b([a-zA-Z0-9][-a-zA-Z0-9]*\.(?:com|co\.uk|org|net|io|uk|eu|info|biz|me|dev|app|tech))\b',
            re.IGNORECASE
        )
        for m in domain_pattern.finditer(text):
            domain = m.group(1).lower()
            if domain not in existing_urls and len(domain) > 5:
                existing_urls.add(domain)
                fact_type = "linkedin_profile" if "linkedin.com" in domain else "website"
                new_facts.append({
                    "type": fact_type,
                    "value": domain,
                    "context": "Domain extracted from answer text",
                    "search_query": domain,
                    "verifiable": True,
                })

        # LinkedIn paths
        linkedin_pattern = re.compile(
            r'linkedin\.com/(?:in|company)/[\w-]+',
            re.IGNORECASE
        )
        for m in linkedin_pattern.finditer(text):
            li_url = "https://" + m.group()
            li_lower = li_url.lower().rstrip("/")
            if li_lower not in existing_urls:
                existing_urls.add(li_lower)
                new_facts.append({
                    "type": "linkedin_profile",
                    "value": li_url,
                    "context": "LinkedIn URL extracted from answer text",
                    "search_query": li_url,
                    "verifiable": True,
                })

        return facts + new_facts

    def _deduplicate(self, facts: list[dict]) -> list[dict]:
        """Remove duplicate facts."""
        seen = set()
        unique = []
        for f in facts:
            key = (f.get("type", ""), f.get("value", "").lower().strip())
            if key not in seen:
                seen.add(key)
                unique.append(f)
        return unique

    def get_all_facts_by_type(self) -> dict[str, list[dict]]:
        """Return all extracted facts grouped by type."""
        by_type: dict[str, list] = {}
        for f in self.all_facts:
            t = f.get("type", "unknown")
            by_type.setdefault(t, []).append(f)
        return by_type

    def get_business_context(self) -> dict:
        """Build a business context dict from all extracted facts.

        Used by the Verification Engine for routing decisions.
        """
        context = {}
        for f in self.all_facts:
            t = f.get("type", "")
            v = f.get("value", "")
            if t == "company_name" and not context.get("company_name"):
                context["company_name"] = v
            elif t == "person_name" and not context.get("person_name"):
                context["person_name"] = v
            elif t in ("website", "url") and not context.get("website"):
                context["website"] = v
            elif t == "address" and not context.get("address"):
                context["address"] = v
            elif t == "email" and not context.get("email"):
                context["email"] = v
            elif t == "industry_detail":
                context.setdefault("industry", v)
            elif t == "financial":
                fc = f.get("context", "").lower()
                if "turnover" in fc or "revenue" in fc:
                    context["claimed_annual_turnover"] = v
                elif "month" in fc:
                    context["claimed_monthly_turnover"] = v
            elif t == "company_number":
                context["company_number"] = v
            elif t == "vat_number":
                context["vat_number"] = v
        return context
