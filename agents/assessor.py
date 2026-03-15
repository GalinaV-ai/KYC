"""
Assessor Agent — evaluates verification results against customer claims.

Produces TWO outputs:
1. Structured assessments per fact (confirmed / contradicted / not_found / suspicious)
2. Probing directives for the Interviewer (areas to explore, without revealing what was found)

This is the bridge between the Verification Engine and the Interviewer.
"""
import json
from datetime import datetime
from typing import Optional

from anthropic import AsyncAnthropic

from agents.prompts import ASSESSOR_PROMPT

MODEL_ASSESSOR = "claude-sonnet-4-20250514"


class Assessor:
    """Evaluates verification results and generates probing directives."""

    def __init__(self):
        self.client = AsyncAnthropic()
        self.assessments: list[dict] = []  # All assessments across turns
        self.directives: list[dict] = []  # All directives generated
        self.assessment_log: list[dict] = []

    async def assess(
        self,
        facts: list[dict],
        verification_results: list[dict],
        conversation_context: str = "",
    ) -> dict:
        """Assess verification results against customer claims.

        Args:
            facts: extracted facts from customer answers
            verification_results: results from the Verification Engine
            conversation_context: summary of the interview so far

        Returns:
            dict with 'assessments' and 'directives' keys
        """
        if not verification_results:
            return {"assessments": [], "directives": [], "summary": "No verification results to assess."}

        # Build the assessment request
        prompt = self._build_prompt(facts, verification_results, conversation_context)

        try:
            response = await self.client.messages.create(
                model=MODEL_ASSESSOR,
                max_tokens=4096,
                system=ASSESSOR_PROMPT,
                messages=[{"role": "user", "content": prompt}]
            )

            text = response.content[0].text.strip()
            result = self._parse_response(text)

            # Store assessments and directives
            for a in result.get("assessments", []):
                a["timestamp"] = datetime.now().isoformat()
                self.assessments.append(a)

            # Cap directives at 3 per round — quality over quantity
            directives = result.get("directives", [])
            urgency_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
            directives.sort(key=lambda x: urgency_order.get(x.get("urgency", "low"), 3))
            directives = directives[:3]
            result["directives"] = directives

            for d in directives:
                d["timestamp"] = datetime.now().isoformat()
                self.directives.append(d)

            self.assessment_log.append({
                "timestamp": datetime.now().isoformat(),
                "facts_count": len(facts),
                "results_count": len(verification_results),
                "assessments_count": len(result.get("assessments", [])),
                "directives_count": len(result.get("directives", [])),
                "summary": result.get("summary", ""),
            })

            return result

        except Exception as e:
            self.assessment_log.append({
                "timestamp": datetime.now().isoformat(),
                "error": str(e),
            })
            return {"assessments": [], "directives": [], "summary": f"Assessment error: {e}"}

    def _build_prompt(
        self,
        facts: list[dict],
        verification_results: list[dict],
        conversation_context: str,
    ) -> str:
        """Build the prompt for the Assessor."""
        # Match facts to verification results
        fact_result_pairs = []

        for fact in facts:
            # Find verification results relevant to this fact
            relevant_results = self._match_results_to_fact(fact, verification_results)
            if relevant_results:
                fact_result_pairs.append({
                    "fact": fact,
                    "verification_results": relevant_results,
                })

        # Also include verification results that don't match specific facts
        # (e.g., general company checks, filing compliance)
        matched_check_ids = set()
        for pair in fact_result_pairs:
            for r in pair["verification_results"]:
                matched_check_ids.add(r.get("check_id", ""))

        unmatched_results = [
            r for r in verification_results
            if r.get("check_id", "") not in matched_check_ids
            and r.get("status") == "completed"
        ]

        prompt = f"""Assess these verification results against customer claims.

CONVERSATION CONTEXT:
{conversation_context or 'No context yet.'}

FACT-VERIFICATION PAIRS:
{json.dumps(fact_result_pairs, indent=2, default=str)}

ADDITIONAL VERIFICATION RESULTS (general checks, not tied to specific claims):
{json.dumps(unmatched_results[:20], indent=2, default=str)}

PREVIOUS ASSESSMENTS (for continuity — don't reassess already-assessed facts):
{json.dumps(self.assessments[-10:], indent=2, default=str) if self.assessments else 'None yet.'}

Produce your assessments and probing directives as JSON."""

        return prompt

    def _match_results_to_fact(
        self,
        fact: dict,
        verification_results: list[dict],
    ) -> list[dict]:
        """Find verification results relevant to a specific fact."""
        matched = []
        fact_type = fact.get("type", "")
        fact_value = fact.get("value", "").lower()

        for result in verification_results:
            if result.get("status") != "completed":
                continue

            check_id = result.get("check_id", "")
            params = result.get("params", {})

            # Match by type
            type_to_checks = {
                "company_name": ["companies_house_search", "london_gazette", "adverse_media",
                                 "ico_register", "social_media", "reviews_search", "company_search"],
                "person_name": ["disqualified_directors", "insolvency_register", "adverse_media",
                               "director_history", "person_search"],
                "website": ["domain_whois", "wayback_machine", "website_deep_analysis",
                           "ssl_certificate", "dns_geolocation"],
                "url": ["domain_whois", "wayback_machine", "website_deep_analysis",
                        "ssl_certificate", "dns_geolocation"],
                "linkedin_profile": ["linkedin_deep_analysis"],
                "address": ["address_type", "address_density", "postcode_validation", "google_maps"],
                "email": ["email_domain"],
                "vat_number": ["vat_check"],
                "financial": ["financial_plausibility", "industry_benchmarks"],
                "supplier": ["adverse_media"],
                "client_name": ["adverse_media"],
                "partner": ["adverse_media"],
                "counterparty": ["adverse_media"],
            }

            relevant_checks = type_to_checks.get(fact_type, [])
            if check_id in relevant_checks:
                matched.append(result)
                continue

            # Also match by param value similarity
            for param_val in params.values():
                if isinstance(param_val, str) and fact_value and fact_value in param_val.lower():
                    matched.append(result)
                    break

        return matched

    def _parse_response(self, text: str) -> dict:
        """Parse the Assessor's JSON response."""
        try:
            json_start = text.find('{')
            json_end = text.rfind('}')
            if json_start >= 0 and json_end > json_start:
                return json.loads(text[json_start:json_end + 1])
        except json.JSONDecodeError:
            pass

        # Try to find assessments and directives separately
        result = {"assessments": [], "directives": [], "summary": ""}
        try:
            if "```" in text:
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            return json.loads(text.strip())
        except (json.JSONDecodeError, IndexError):
            pass

        return result

    def get_new_directives(self) -> list[dict]:
        """Get directives that haven't been sent to the Interviewer yet."""
        unsent = [d for d in self.directives if not d.get("_sent")]
        for d in unsent:
            d["_sent"] = True
        return unsent

    def get_assessment_summary(self) -> dict:
        """Get a summary of all assessments."""
        confirmed = [a for a in self.assessments if a.get("status") == "confirmed"]
        contradicted = [a for a in self.assessments if a.get("status") == "contradicted"]
        not_found = [a for a in self.assessments if a.get("status") == "not_found"]
        suspicious = [a for a in self.assessments if a.get("status") == "suspicious"]

        return {
            "total": len(self.assessments),
            "confirmed": len(confirmed),
            "contradicted": len(contradicted),
            "not_found": len(not_found),
            "suspicious": len(suspicious),
            "confirmed_claims": [a.get("claim", "") for a in confirmed],
            "contradicted_claims": [{"claim": a.get("claim"), "reasoning": a.get("reasoning")} for a in contradicted],
            "suspicious_claims": [{"claim": a.get("claim"), "reasoning": a.get("reasoning")} for a in suspicious],
        }

    def get_findings_for_risk_analyst(self) -> dict:
        """Format all assessments for the Risk Analyst."""
        return {
            "assessments": self.assessments,
            "directives_issued": len(self.directives),
            "summary": self.get_assessment_summary(),
        }
