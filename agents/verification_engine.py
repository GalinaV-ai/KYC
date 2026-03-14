"""
Verification Engine Agent — Smart Router for KYC checks.

This agent receives extracted facts from the Fact Extractor and decides
which verification methods to run for each fact. It uses Claude (Haiku)
to make routing decisions based on the fact type, business context,
and what checks have already been performed.

It manages 40+ verification methods across 7 categories:
  A. Government registers (FCA, ICO, Charity, Disqualified Directors, Gazette, etc.)
  B. Person verification (PEP, adverse media, director history, insolvency)
  C. Digital footprint (email MX, Wayback, social media, DNS, SSL, app stores, GitHub)
  D. Financial verification (company accounts, charges, VAT, benchmarks)
  E. Address & location (virtual office, postcode, company density, Land Registry)
  F. Industry-specific (professional bodies, food hygiene, CQC, gambling, MSB)
  G. Cross-referencing (timeline consistency, financial plausibility, network analysis, name matching)

The Engine does NOT assess findings — it only executes checks and returns raw results.
Assessment is the Assessor agent's job.
"""

import asyncio
import json
import traceback
from typing import Optional
from datetime import datetime
from anthropic import AsyncAnthropic

# ── Existing tools ──
try:
    from tools.web_search import _search_ddg, _search_ddg_news, search_business_online, search_person_online, search_company_online
    from tools.verification import check_domain_whois, check_address_type, search_reviews, verify_vat_number, get_industry_benchmarks, search_google_maps
    from tools.companies_house import search_company as ch_search, get_company_profile, get_company_officers, get_filing_history, get_persons_with_significant_control
    from tools.web_analysis import deep_analyze_website, deep_analyze_linkedin
    from tools.document_tools import analyze_document
except ImportError:
    from web_search import _search_ddg, _search_ddg_news, search_business_online, search_person_online, search_company_online
    from verification import check_domain_whois, check_address_type, search_reviews, verify_vat_number, get_industry_benchmarks, search_google_maps
    from companies_house import search_company as ch_search, get_company_profile, get_company_officers, get_filing_history, get_persons_with_significant_control
    from web_analysis import deep_analyze_website, deep_analyze_linkedin
    from document_tools import analyze_document

# ── New tools ──
try:
    from tools.gov_registers import (
        check_fca_register, check_ico_register, check_charity_commission,
        check_disqualified_directors, check_london_gazette, check_insolvency_register,
        check_food_hygiene_rating, check_cqc_register, check_company_charges,
        check_filing_compliance, check_gambling_commission, check_hmrc_msb_register,
        check_professional_registration, check_adverse_media,
        check_address_company_density, validate_postcode,
    )
    from tools.digital_footprint import (
        check_email_domain, check_wayback_machine, check_social_media_presence,
        check_app_store_presence, check_github_presence, check_dns_geolocation,
        check_ssl_certificate, check_director_history, check_land_registry,
    )
    from tools.cross_reference import (
        check_timeline_consistency, check_financial_plausibility,
        fuzzy_name_match, analyze_company_network, analyze_company_accounts,
    )
except ImportError:
    from gov_registers import (
        check_fca_register, check_ico_register, check_charity_commission,
        check_disqualified_directors, check_london_gazette, check_insolvency_register,
        check_food_hygiene_rating, check_cqc_register, check_company_charges,
        check_filing_compliance, check_gambling_commission, check_hmrc_msb_register,
        check_professional_registration, check_adverse_media,
        check_address_company_density, validate_postcode,
    )
    from digital_footprint import (
        check_email_domain, check_wayback_machine, check_social_media_presence,
        check_app_store_presence, check_github_presence, check_dns_geolocation,
        check_ssl_certificate, check_director_history, check_land_registry,
    )
    from cross_reference import (
        check_timeline_consistency, check_financial_plausibility,
        fuzzy_name_match, analyze_company_network, analyze_company_accounts,
    )


# ─────────────────────────────────────────────
# Verification Registry — all available checks
# ─────────────────────────────────────────────

VERIFICATION_REGISTRY = {
    # ── A. Government Registers ──
    "fca_register": {
        "function": "check_fca_register",
        "category": "government",
        "description": "Check FCA register and warning list",
        "relevant_for": ["regulated_business", "financial_services", "payments", "insurance", "lending", "crypto"],
        "required_params": ["firm_name"],
        "cost": "free",
    },
    "ico_register": {
        "function": "check_ico_register",
        "category": "government",
        "description": "Check ICO data protection registration",
        "relevant_for": ["any_company"],
        "required_params": ["organisation_name"],
        "cost": "free",
    },
    "charity_commission": {
        "function": "check_charity_commission",
        "category": "government",
        "description": "Check Charity Commission register",
        "relevant_for": ["charity", "social_enterprise", "non_profit", "cic"],
        "required_params": ["name"],
        "cost": "free",
    },
    "disqualified_directors": {
        "function": "check_disqualified_directors",
        "category": "government",
        "description": "Check disqualified directors register",
        "relevant_for": ["any_person_director"],
        "required_params": ["person_name"],
        "cost": "free",
    },
    "london_gazette": {
        "function": "check_london_gazette",
        "category": "government",
        "description": "Search London Gazette for insolvency/winding-up notices",
        "relevant_for": ["any_company", "any_person"],
        "required_params": ["name"],
        "cost": "free",
    },
    "insolvency_register": {
        "function": "check_insolvency_register",
        "category": "government",
        "description": "Check individual insolvency register (bankruptcy, IVA)",
        "relevant_for": ["any_person_director"],
        "required_params": ["person_name"],
        "cost": "free",
    },
    "food_hygiene": {
        "function": "check_food_hygiene_rating",
        "category": "government",
        "description": "Check FSA food hygiene ratings",
        "relevant_for": ["restaurant", "takeaway", "food", "catering", "cafe", "bakery"],
        "required_params": ["business_name"],
        "cost": "free",
    },
    "cqc_register": {
        "function": "check_cqc_register",
        "category": "government",
        "description": "Check CQC registration for healthcare providers",
        "relevant_for": ["healthcare", "care_home", "dental", "clinic", "nursing", "pharmacy"],
        "required_params": ["provider_name"],
        "cost": "free",
    },
    "company_charges": {
        "function": "check_company_charges",
        "category": "government",
        "description": "Check Companies House charges register (secured debts)",
        "relevant_for": ["any_company_with_number"],
        "required_params": ["company_number"],
        "cost": "free",
    },
    "filing_compliance": {
        "function": "check_filing_compliance",
        "category": "government",
        "description": "Check if company has overdue filings",
        "relevant_for": ["any_company_with_number"],
        "required_params": ["company_number"],
        "cost": "free",
    },
    "gambling_commission": {
        "function": "check_gambling_commission",
        "category": "government",
        "description": "Check Gambling Commission licence",
        "relevant_for": ["gambling", "betting", "casino", "lottery", "gaming"],
        "required_params": ["operator_name"],
        "cost": "free",
    },
    "hmrc_msb": {
        "function": "check_hmrc_msb_register",
        "category": "government",
        "description": "Check HMRC Money Service Business register",
        "relevant_for": ["money_transfer", "currency_exchange", "forex", "remittance"],
        "required_params": ["business_name"],
        "cost": "free",
    },
    "professional_body": {
        "function": "check_professional_registration",
        "category": "government",
        "description": "Check professional body registration (SRA, ICAEW, GMC, etc.)",
        "relevant_for": ["solicitor", "accountant", "doctor", "dentist", "architect",
                         "estate_agent", "security", "construction", "financial_adviser",
                         "nurse", "teacher", "transport"],
        "required_params": ["name", "profession"],
        "cost": "free",
    },

    # ── B. Person Verification ──
    "adverse_media": {
        "function": "check_adverse_media",
        "category": "person",
        "description": "Targeted adverse media screening (fraud, crime, sanctions)",
        "relevant_for": ["any_person", "any_company"],
        "required_params": ["name"],
        "cost": "free",
    },
    "director_history": {
        "function": "check_director_history",
        "category": "person",
        "description": "Full directorship history and pattern analysis",
        "relevant_for": ["any_person_director"],
        "required_params": ["person_name"],
        "cost": "free",
    },

    # ── C. Digital Footprint ──
    "email_domain": {
        "function": "check_email_domain",
        "category": "digital",
        "description": "Validate email domain MX records and classify provider",
        "relevant_for": ["email"],
        "required_params": ["email"],
        "cost": "free",
    },
    "wayback_machine": {
        "function": "check_wayback_machine",
        "category": "digital",
        "description": "Check Internet Archive for website history",
        "relevant_for": ["website", "url", "domain"],
        "required_params": ["url"],
        "cost": "free",
    },
    "social_media": {
        "function": "check_social_media_presence",
        "category": "digital",
        "description": "Check social media presence across platforms",
        "relevant_for": ["any_company", "any_person"],
        "required_params": ["business_name"],
        "cost": "free",
    },
    "app_store": {
        "function": "check_app_store_presence",
        "category": "digital",
        "description": "Check Apple App Store and Google Play",
        "relevant_for": ["tech", "fintech", "app", "mobile"],
        "required_params": ["app_name"],
        "cost": "free",
    },
    "github": {
        "function": "check_github_presence",
        "category": "digital",
        "description": "Check GitHub/code repository presence",
        "relevant_for": ["tech", "software", "developer", "it_services"],
        "required_params": ["company_name"],
        "cost": "free",
    },
    "dns_geolocation": {
        "function": "check_dns_geolocation",
        "category": "digital",
        "description": "Check DNS records and hosting location",
        "relevant_for": ["website", "domain"],
        "required_params": ["domain"],
        "cost": "free",
    },
    "ssl_certificate": {
        "function": "check_ssl_certificate",
        "category": "digital",
        "description": "Analyze SSL certificate details (type, issuer, dates)",
        "relevant_for": ["website", "domain"],
        "required_params": ["domain"],
        "cost": "free",
    },

    # ── D. Financial ──
    "vat_check": {
        "function": "verify_vat_number",
        "category": "financial",
        "description": "Verify VAT registration with HMRC",
        "relevant_for": ["vat_number"],
        "required_params": ["vat_number"],
        "cost": "free",
    },
    "industry_benchmarks": {
        "function": "get_industry_benchmarks",
        "category": "financial",
        "description": "Get industry benchmarks for financial plausibility",
        "relevant_for": ["financial_claim", "turnover", "revenue"],
        "required_params": ["industry"],
        "cost": "free",
    },
    "financial_plausibility": {
        "function": "check_financial_plausibility",
        "category": "financial",
        "description": "Cross-check financial claims against benchmarks",
        "relevant_for": ["financial_claim", "turnover", "employees"],
        "required_params": [],
        "cost": "free",
    },
    "company_accounts": {
        "function": "analyze_company_accounts",
        "category": "financial",
        "description": "Compare filed accounts vs customer claims",
        "relevant_for": ["financial_claim", "any_company_with_accounts"],
        "required_params": [],
        "cost": "free",
    },

    # ── E. Address & Location ──
    "address_type": {
        "function": "check_address_type",
        "category": "address",
        "description": "Detect virtual offices, residential addresses",
        "relevant_for": ["address"],
        "required_params": ["address"],
        "cost": "free",
    },
    "address_density": {
        "function": "check_address_company_density",
        "category": "address",
        "description": "Count companies registered at same address",
        "relevant_for": ["address", "postcode"],
        "required_params": ["address"],
        "cost": "free",
    },
    "postcode_validation": {
        "function": "validate_postcode",
        "category": "address",
        "description": "Validate UK postcode and get geographic data",
        "relevant_for": ["postcode", "address"],
        "required_params": ["postcode"],
        "cost": "free",
    },
    "google_maps": {
        "function": "search_google_maps",
        "category": "address",
        "description": "Search Google Maps for business presence",
        "relevant_for": ["physical_business", "restaurant", "retail", "salon"],
        "required_params": ["query"],
        "cost": "free",
    },
    "land_registry": {
        "function": "check_land_registry",
        "category": "address",
        "description": "Check Land Registry for property ownership",
        "relevant_for": ["property", "premises_ownership"],
        "required_params": ["address"],
        "cost": "free",
    },

    # ── F. Industry-specific ──
    # (These are routed via the professional_body, food_hygiene, cqc, gambling, hmrc_msb checks above)

    # ── G. Cross-referencing ──
    "timeline_consistency": {
        "function": "check_timeline_consistency",
        "category": "cross_reference",
        "description": "Cross-reference dates from multiple sources",
        "relevant_for": ["timeline", "company_age", "trading_history"],
        "required_params": [],
        "cost": "free",
    },
    "network_analysis": {
        "function": "analyze_company_network",
        "category": "cross_reference",
        "description": "Analyze company network for shell company patterns",
        "relevant_for": ["director_with_multiple_companies"],
        "required_params": ["director_appointments"],
        "cost": "free",
    },
    "name_matching": {
        "function": "fuzzy_name_match",
        "category": "cross_reference",
        "description": "Fuzzy match names across sources (handles transliteration, variants)",
        "relevant_for": ["name_verification"],
        "required_params": ["name1", "name2"],
        "cost": "free",
    },

    # ── Existing deep checks ──
    "web_search": {
        "function": "search_business_online",
        "category": "web",
        "description": "General web search for business",
        "relevant_for": ["any_company", "any_claim"],
        "required_params": ["query"],
        "cost": "free",
    },
    "person_search": {
        "function": "search_person_online",
        "category": "web",
        "description": "Deep person search (LinkedIn, news, Companies House)",
        "relevant_for": ["any_person"],
        "required_params": ["person_name"],
        "cost": "free",
    },
    "company_search": {
        "function": "search_company_online",
        "category": "web",
        "description": "Comprehensive company online search",
        "relevant_for": ["any_company"],
        "required_params": ["company_name"],
        "cost": "free",
    },
    "domain_whois": {
        "function": "check_domain_whois",
        "category": "digital",
        "description": "WHOIS/RDAP domain registration data",
        "relevant_for": ["website", "domain"],
        "required_params": ["domain"],
        "cost": "free",
    },
    "website_deep_analysis": {
        "function": "deep_analyze_website",
        "category": "digital",
        "description": "12-criteria website reliability scoring",
        "relevant_for": ["website", "url"],
        "required_params": ["url"],
        "cost": "free",
    },
    "linkedin_deep_analysis": {
        "function": "deep_analyze_linkedin",
        "category": "digital",
        "description": "LinkedIn profile verification",
        "relevant_for": ["linkedin_profile"],
        "required_params": ["url"],
        "cost": "free",
    },
    "companies_house_search": {
        "function": "ch_search",
        "category": "government",
        "description": "Companies House company search",
        "relevant_for": ["any_company"],
        "required_params": ["query"],
        "cost": "free",
    },
    "companies_house_officers": {
        "function": "get_company_officers",
        "category": "government",
        "description": "Get company directors and officers",
        "relevant_for": ["any_company_with_number"],
        "required_params": ["company_number"],
        "cost": "free",
    },
    "companies_house_psc": {
        "function": "get_persons_with_significant_control",
        "category": "government",
        "description": "Get persons with significant control",
        "relevant_for": ["any_company_with_number"],
        "required_params": ["company_number"],
        "cost": "free",
    },
    "reviews_search": {
        "function": "search_reviews",
        "category": "web",
        "description": "Search for business reviews (Trustpilot, Google)",
        "relevant_for": ["any_company", "physical_business"],
        "required_params": ["business_name"],
        "cost": "free",
    },
    "sanctions_check": {
        "function": "_sanctions_placeholder",
        "category": "compliance",
        "description": "OpenSanctions screening (handled by investigator)",
        "relevant_for": ["any_person", "any_company", "counterparty"],
        "required_params": ["name"],
        "cost": "free",
    },
}


# ─────────────────────────────────────────────
# Smart Router — Decides which checks to run
# ─────────────────────────────────────────────

ROUTING_PROMPT = """You are a KYC Verification Router. Your job is to decide which verification checks
to run for a given set of extracted facts about a customer and their business.

You have access to {total_checks} verification methods. For each fact, select the most relevant checks.

RULES:
1. ALWAYS run these checks regardless of context:
   - adverse_media (for person AND company name)
   - london_gazette (for company name)
   - disqualified_directors (for person name)
   - ico_register (for company name)
   - social_media (for company name)

2. ALWAYS run these if you have the data:
   - domain_whois + wayback_machine + ssl_certificate + dns_geolocation (if website/URL provided)
   - website_deep_analysis (if URL provided)
   - linkedin_deep_analysis (if LinkedIn URL provided)
   - email_domain (if email provided)
   - companies_house_search + filing_compliance + company_charges (if company number known)
   - director_history (for every named director)
   - postcode_validation + address_type + address_density (if address/postcode provided)

3. Run industry-specific checks ONLY when relevant:
   - food_hygiene → ONLY for restaurants, takeaways, food businesses
   - cqc_register → ONLY for healthcare/care providers
   - gambling_commission → ONLY for gambling/betting businesses
   - hmrc_msb → ONLY for money transfer/currency exchange
   - fca_register → ONLY for financial services (payments, lending, insurance, crypto, investment)
   - professional_body → ONLY for regulated professions (solicitors, accountants, doctors, etc.)
   - app_store → ONLY for tech companies claiming to have apps
   - github → ONLY for tech/software companies

4. Run cross-reference checks when enough data is accumulated:
   - timeline_consistency → when 3+ dates are available
   - financial_plausibility → when turnover AND employee count are known
   - network_analysis → after director_history returns results

5. DO NOT run checks that cannot produce useful results:
   - No point checking food_hygiene for a consulting firm
   - No point checking github for a restaurant
   - No point checking land_registry without a specific address

Return a JSON array of check objects. Each object must have:
- "check_id": the check name from the registry
- "params": dict of parameters to pass
- "priority": "critical" | "high" | "medium" | "low"
- "reason": brief explanation of why this check is relevant

AVAILABLE CHECKS:
{check_list}

BUSINESS CONTEXT:
{business_context}

EXTRACTED FACTS:
{facts_json}

CHECKS ALREADY COMPLETED:
{completed_checks}

Return ONLY valid JSON array. No commentary."""


class VerificationEngine:
    """
    Smart verification router that decides which checks to run
    based on extracted facts and business context.
    """

    def __init__(self, model: str = "claude-haiku-4-5-20251001"):
        self.client = AsyncAnthropic()
        self.model = model
        self.completed_checks: list[dict] = []  # Track what's been run
        self.all_results: list[dict] = []  # All verification results
        self.check_log: list[dict] = []  # Audit log
        self._collected_dates: dict = {}  # For timeline consistency
        self._collected_financials: dict = {}  # For financial plausibility
        self._director_appointments: list = []  # For network analysis

    def _dedup_key(self, check_id: str, params: dict) -> str:
        """Generate a deduplication key from check_id + sorted params."""
        # Normalize params: lowercase string values, sort keys
        norm = {}
        for k, v in sorted(params.items()):
            if isinstance(v, str):
                norm[k] = v.strip().lower()
            else:
                norm[k] = v
        return f"{check_id}::{json.dumps(norm, sort_keys=True)}"

    def _dedup_planned(self, planned: list[dict]) -> list[dict]:
        """Remove checks that have already been completed with the same params."""
        # Build set of already-completed keys
        completed_keys = set()
        for cc in self.completed_checks:
            cid = cc.get("check_id", "")
            cparams = cc.get("params", {})
            completed_keys.add(self._dedup_key(cid, cparams))

        # Also dedup within the planned list itself
        seen = set()
        deduped = []
        for check in planned:
            key = self._dedup_key(check.get("check_id", ""), check.get("params", {}))
            if key not in completed_keys and key not in seen:
                seen.add(key)
                deduped.append(check)

        skipped = len(planned) - len(deduped)
        if skipped:
            print(f"[VerificationEngine] Dedup: skipped {skipped} duplicate checks")
        return deduped

    async def plan_checks(
        self,
        facts: list[dict],
        business_context: dict,
    ) -> list[dict]:
        """
        Use Claude to decide which checks to run for a set of facts.
        Returns a list of planned checks with priorities.
        """
        # Build check list description
        check_list = ""
        for check_id, info in VERIFICATION_REGISTRY.items():
            check_list += (
                f"- {check_id}: {info['description']} "
                f"(relevant for: {', '.join(info['relevant_for'])})\n"
            )

        # Build completed checks summary — include params for better dedup by LLM
        completed_summary = ""
        if self.completed_checks:
            for cc in self.completed_checks[-30:]:  # Last 30 checks
                params_str = ", ".join(f"{k}={v}" for k, v in cc.get("params", {}).items())
                completed_summary += f"- {cc.get('check_id', '')}({params_str}) → {cc.get('status', '')}\n"
        else:
            completed_summary = "None yet."

        prompt = ROUTING_PROMPT.format(
            total_checks=len(VERIFICATION_REGISTRY),
            check_list=check_list,
            business_context=json.dumps(business_context, indent=2, default=str),
            facts_json=json.dumps(facts, indent=2, default=str),
            completed_checks=completed_summary,
        )

        try:
            response = await self.client.messages.create(
                model=self.model,
                max_tokens=4096,
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.content[0].text.strip()
            # Extract JSON from response
            if "```" in text:
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            planned = json.loads(text)
            if isinstance(planned, list):
                return self._dedup_planned(planned)
        except Exception as e:
            print(f"[VerificationEngine] Routing error: {e}")
            # Fallback to rule-based routing
            return self._rule_based_routing(facts, business_context)

        return []

    def _rule_based_routing(
        self,
        facts: list[dict],
        business_context: dict,
    ) -> list[dict]:
        """
        Fallback rule-based routing when Claude is unavailable.
        Covers the most important checks deterministically.
        """
        planned = []
        company_name = business_context.get("company_name", "")
        person_name = business_context.get("person_name", "")
        company_number = business_context.get("company_number", "")
        industry = business_context.get("industry", "").lower()

        # Build dedup keys from completed checks (check_id + params)
        completed_keys = set()
        for cc in self.completed_checks:
            completed_keys.add(self._dedup_key(cc.get("check_id", ""), cc.get("params", {})))

        def add(check_id, params, priority="medium", reason=""):
            key = self._dedup_key(check_id, params)
            if key not in completed_keys:
                planned.append({
                    "check_id": check_id,
                    "params": params,
                    "priority": priority,
                    "reason": reason,
                })
                completed_keys.add(key)  # Prevent intra-batch duplicates

        # ── Mandatory checks ──
        if person_name:
            add("adverse_media", {"name": person_name, "company_name": company_name},
                "critical", "Mandatory adverse media screening for person")
            add("disqualified_directors", {"person_name": person_name},
                "critical", "Mandatory disqualified directors check")
            add("insolvency_register", {"person_name": person_name},
                "high", "Check individual insolvency")
            add("director_history", {"person_name": person_name},
                "high", "Full directorship history")

        if company_name:
            add("adverse_media", {"name": company_name},
                "critical", "Mandatory adverse media screening for company")
            add("london_gazette", {"name": company_name},
                "critical", "Check for insolvency/winding-up notices")
            add("ico_register", {"organisation_name": company_name},
                "medium", "Check ICO data protection registration")
            add("social_media", {"business_name": company_name, "person_name": person_name},
                "medium", "Check social media presence")
            add("reviews_search", {"business_name": company_name},
                "medium", "Search for business reviews")

        if company_number:
            add("filing_compliance", {"company_number": company_number},
                "high", "Check for overdue filings")
            add("company_charges", {"company_number": company_number},
                "medium", "Check charges register")
            add("companies_house_officers", {"company_number": company_number},
                "high", "Get company officers list")
            add("companies_house_psc", {"company_number": company_number},
                "high", "Get persons with significant control")

        # ── Fact-driven checks ──
        for fact in facts:
            fact_type = fact.get("type", "")
            value = fact.get("value", "")

            if fact_type in ("website", "url") and value:
                add("domain_whois", {"domain": value}, "high", "Check domain registration")
                add("wayback_machine", {"url": value}, "high", "Check web archive history")
                add("website_deep_analysis", {"url": value}, "high", "Deep website analysis")
                add("ssl_certificate", {"domain": value}, "medium", "Check SSL certificate")
                add("dns_geolocation", {"domain": value}, "medium", "Check hosting location")

            elif fact_type == "linkedin_profile" and value:
                add("linkedin_deep_analysis", {"url": value}, "high", "Verify LinkedIn profile")

            elif fact_type == "email" and value:
                add("email_domain", {"email": value}, "high", "Validate email domain")

            elif fact_type == "address" and value:
                add("address_type", {"address": value}, "high", "Check address type")
                add("address_density", {"address": value}, "medium", "Check company density at address")
                # Extract postcode if present
                import re
                pc_match = re.search(r'[A-Z]{1,2}\d{1,2}\s?\d[A-Z]{2}', value.upper())
                if pc_match:
                    add("postcode_validation", {"postcode": pc_match.group()},
                        "medium", "Validate postcode")

            elif fact_type == "vat_number" and value:
                add("vat_check", {"vat_number": value}, "high", "Verify VAT registration")

            elif fact_type in ("supplier", "client_name", "partner", "counterparty") and value:
                add("adverse_media", {"name": value}, "medium",
                    f"Adverse media check on counterparty: {value}")

        # ── Industry-specific checks ──
        food_keywords = ["restaurant", "takeaway", "food", "catering", "cafe", "bakery", "kitchen"]
        if any(kw in industry for kw in food_keywords) and company_name:
            add("food_hygiene", {"business_name": company_name},
                "high", "Food business — check FSA rating")

        healthcare_keywords = ["health", "care", "clinic", "dental", "nursing", "pharmacy", "medical"]
        if any(kw in industry for kw in healthcare_keywords) and company_name:
            add("cqc_register", {"provider_name": company_name},
                "high", "Healthcare — check CQC registration")

        gambling_keywords = ["gambling", "betting", "casino", "lottery", "gaming"]
        if any(kw in industry for kw in gambling_keywords) and company_name:
            add("gambling_commission", {"operator_name": company_name},
                "high", "Gambling business — check licence")

        money_keywords = ["money transfer", "remittance", "forex", "currency exchange", "money service"]
        if any(kw in industry for kw in money_keywords) and company_name:
            add("hmrc_msb", {"business_name": company_name},
                "critical", "Money service business — check HMRC registration")

        finance_keywords = ["financial", "payment", "lending", "insurance", "investment", "crypto", "fintech"]
        if any(kw in industry for kw in finance_keywords) and company_name:
            add("fca_register", {"firm_name": company_name},
                "critical", "Financial services — check FCA registration")

        tech_keywords = ["tech", "software", "app", "saas", "platform", "digital"]
        if any(kw in industry for kw in tech_keywords) and company_name:
            add("app_store", {"app_name": company_name},
                "medium", "Tech company — check app stores")
            add("github", {"company_name": company_name},
                "low", "Tech company — check code presence")

        # Professional bodies
        professional_industries = [
            "solicitor", "law", "legal", "accountant", "accounting",
            "doctor", "medical", "dentist", "dental", "architect",
            "estate agent", "property", "security", "construction",
            "financial advi", "nursing", "teaching", "transport", "logistics"
        ]
        for prof in professional_industries:
            if prof in industry:
                name_to_check = person_name or company_name
                if name_to_check:
                    add("professional_body",
                        {"name": name_to_check, "profession": industry},
                        "high", f"Check professional registration for {industry}")
                break

        return planned

    async def execute_checks(
        self,
        planned_checks: list[dict],
        max_concurrent: int = 8,
    ) -> list[dict]:
        """
        Execute planned verification checks in parallel with priority ordering.
        Returns list of results.
        """
        # Sort by priority
        priority_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        planned_checks.sort(key=lambda x: priority_order.get(x.get("priority", "low"), 3))

        results = []
        semaphore = asyncio.Semaphore(max_concurrent)

        async def run_check(check: dict) -> dict:
            async with semaphore:
                check_id = check["check_id"]
                params = check.get("params", {})
                start = datetime.now()

                try:
                    result = await self._dispatch_check(check_id, params)
                    elapsed = (datetime.now() - start).total_seconds()

                    check_result = {
                        "check_id": check_id,
                        "status": "completed",
                        "priority": check.get("priority", "medium"),
                        "reason": check.get("reason", ""),
                        "params": params,
                        "result": result,
                        "elapsed_seconds": round(elapsed, 2),
                        "timestamp": datetime.now().isoformat(),
                    }

                    # Track for cross-reference checks
                    self._collect_cross_reference_data(check_id, result)

                    return check_result

                except Exception as e:
                    elapsed = (datetime.now() - start).total_seconds()
                    return {
                        "check_id": check_id,
                        "status": "error",
                        "priority": check.get("priority", "medium"),
                        "params": params,
                        "error": str(e),
                        "elapsed_seconds": round(elapsed, 2),
                        "timestamp": datetime.now().isoformat(),
                    }

        # Run all checks concurrently
        tasks = [run_check(check) for check in planned_checks]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Process results
        valid_results = []
        for r in results:
            if isinstance(r, dict):
                valid_results.append(r)
                self.completed_checks.append(r)
                self.all_results.append(r)
                self.check_log.append({
                    "check_id": r["check_id"],
                    "status": r["status"],
                    "timestamp": r.get("timestamp", ""),
                    "elapsed": r.get("elapsed_seconds", 0),
                })
            elif isinstance(r, Exception):
                valid_results.append({
                    "check_id": "unknown",
                    "status": "exception",
                    "error": str(r),
                })

        return valid_results

    async def _dispatch_check(self, check_id: str, params: dict) -> dict:
        """Dispatch a check to the appropriate function."""

        # Map check_id to actual function call
        dispatch_map = {
            # Government
            "fca_register": lambda p: check_fca_register(p.get("firm_name", ""), p.get("firm_number", "")),
            "ico_register": lambda p: check_ico_register(p.get("organisation_name", "")),
            "charity_commission": lambda p: check_charity_commission(p.get("name", ""), p.get("charity_number", "")),
            "disqualified_directors": lambda p: check_disqualified_directors(p.get("person_name", "")),
            "london_gazette": lambda p: check_london_gazette(p.get("name", ""), p.get("is_company", True)),
            "insolvency_register": lambda p: check_insolvency_register(p.get("person_name", "")),
            "food_hygiene": lambda p: check_food_hygiene_rating(p.get("business_name", ""), p.get("location", "")),
            "cqc_register": lambda p: check_cqc_register(p.get("provider_name", "")),
            "company_charges": lambda p: check_company_charges(p.get("company_number", "")),
            "filing_compliance": lambda p: check_filing_compliance(p.get("company_number", "")),
            "gambling_commission": lambda p: check_gambling_commission(p.get("operator_name", "")),
            "hmrc_msb": lambda p: check_hmrc_msb_register(p.get("business_name", "")),
            "professional_body": lambda p: check_professional_registration(p.get("name", ""), p.get("profession", "")),
            "adverse_media": lambda p: check_adverse_media(p.get("name", ""), p.get("company_name", "")),

            # Digital
            "email_domain": lambda p: check_email_domain(p.get("email", "")),
            "wayback_machine": lambda p: check_wayback_machine(p.get("url", "")),
            "social_media": lambda p: check_social_media_presence(p.get("business_name", ""), p.get("person_name", "")),
            "app_store": lambda p: check_app_store_presence(p.get("app_name", ""), p.get("company_name", "")),
            "github": lambda p: check_github_presence(p.get("company_name", "")),
            "dns_geolocation": lambda p: check_dns_geolocation(p.get("domain", "")),
            "ssl_certificate": lambda p: check_ssl_certificate(p.get("domain", "")),
            "director_history": lambda p: check_director_history(p.get("person_name", "")),
            "land_registry": lambda p: check_land_registry(p.get("address", ""), p.get("postcode", "")),

            # Financial
            "vat_check": lambda p: verify_vat_number(p.get("vat_number", "")),
            "industry_benchmarks": lambda p: get_industry_benchmarks(p.get("industry", ""), p.get("business_type", "")),
            "reviews_search": lambda p: search_reviews(p.get("business_name", ""), p.get("location", "UK")),

            # Address
            "address_type": lambda p: check_address_type(p.get("address", "")),
            "address_density": lambda p: check_address_company_density(p.get("address", ""), p.get("postcode", "")),
            "postcode_validation": lambda p: validate_postcode(p.get("postcode", "")),
            "google_maps": lambda p: search_google_maps(p.get("query", "")),

            # Existing deep checks
            "domain_whois": lambda p: check_domain_whois(p.get("domain", "")),
            "website_deep_analysis": lambda p: deep_analyze_website(p.get("url", "")),
            "linkedin_deep_analysis": lambda p: deep_analyze_linkedin(
                p.get("url", ""), p.get("person_name", ""), p.get("company_name", "")
            ),
            "companies_house_search": lambda p: ch_search(p.get("query", "")),
            "companies_house_officers": lambda p: get_company_officers(p.get("company_number", "")),
            "companies_house_psc": lambda p: get_persons_with_significant_control(p.get("company_number", "")),

            # Web search
            "web_search": lambda p: search_business_online(p.get("query", "")),
            "person_search": lambda p: search_person_online(
                p.get("person_name", ""), p.get("company", ""), p.get("role", "")
            ),
            "company_search": lambda p: search_company_online(
                p.get("company_name", ""), p.get("location", ""), p.get("owner_name", "")
            ),
        }

        handler = dispatch_map.get(check_id)
        if handler is None:
            return {"error": f"Unknown check: {check_id}"}

        result = handler(params)
        # Handle both async and sync functions
        if asyncio.iscoroutine(result):
            return await result
        return result

    def _collect_cross_reference_data(self, check_id: str, result: dict):
        """Collect data from individual checks for cross-reference analyses."""
        if not isinstance(result, dict):
            return

        # Collect dates for timeline consistency
        if check_id == "domain_whois" and result.get("found"):
            self._collected_dates["domain_registration"] = result.get("registration_date", "")
        elif check_id == "wayback_machine" and result.get("found"):
            self._collected_dates["first_wayback_snapshot"] = result.get("details", {}).get("first_snapshot", "")
        elif check_id == "ssl_certificate" and result.get("has_ssl"):
            self._collected_dates["first_ssl_cert"] = result.get("details", {}).get("first_cert_date", "")
        elif check_id == "filing_compliance":
            status = result.get("details", {}).get("company_status", "")
            if status:
                self._collected_dates.setdefault("company_status", status)

        # Collect director appointments for network analysis
        if check_id == "director_history":
            companies = result.get("details", {}).get("companies", [])
            if companies:
                self._director_appointments = companies

    async def run_cross_reference_checks(
        self,
        business_context: dict,
    ) -> list[dict]:
        """
        Run cross-reference checks that need accumulated data from other checks.
        Call this AFTER individual checks have completed.
        """
        results = []

        # Timeline consistency
        if len(self._collected_dates) >= 2:
            timeline_params = {**self._collected_dates}
            timeline_params["claimed_trading_start"] = business_context.get("claimed_trading_start", "")
            timeline_params["claimed_years_trading"] = business_context.get("claimed_years_trading", 0)
            try:
                timeline_result = check_timeline_consistency(**timeline_params)
                results.append({
                    "check_id": "timeline_consistency",
                    "status": "completed",
                    "priority": "high",
                    "result": timeline_result,
                    "timestamp": datetime.now().isoformat(),
                })
                self.completed_checks.append(results[-1])
            except Exception as e:
                results.append({"check_id": "timeline_consistency", "status": "error", "error": str(e)})

        # Financial plausibility
        fin = self._collected_financials
        if business_context.get("claimed_annual_turnover") or business_context.get("claimed_monthly_turnover"):
            try:
                fin_result = check_financial_plausibility(
                    claimed_annual_turnover=business_context.get("claimed_annual_turnover", 0),
                    claimed_monthly_turnover=business_context.get("claimed_monthly_turnover", 0),
                    claimed_employees=business_context.get("claimed_employees", 0),
                    industry=business_context.get("industry", ""),
                    company_age_years=business_context.get("company_age_years", 0),
                    is_sole_trader=business_context.get("is_sole_trader", False),
                    claimed_profit_margin=business_context.get("claimed_profit_margin", 0),
                    claimed_avg_transaction=business_context.get("claimed_avg_transaction", 0),
                    claimed_monthly_transactions=business_context.get("claimed_monthly_transactions", 0),
                )
                results.append({
                    "check_id": "financial_plausibility",
                    "status": "completed",
                    "priority": "high",
                    "result": fin_result,
                    "timestamp": datetime.now().isoformat(),
                })
                self.completed_checks.append(results[-1])
            except Exception as e:
                results.append({"check_id": "financial_plausibility", "status": "error", "error": str(e)})

        # Network analysis
        if self._director_appointments and len(self._director_appointments) > 2:
            try:
                network_result = analyze_company_network(self._director_appointments)
                results.append({
                    "check_id": "network_analysis",
                    "status": "completed",
                    "priority": "high",
                    "result": network_result,
                    "timestamp": datetime.now().isoformat(),
                })
                self.completed_checks.append(results[-1])
            except Exception as e:
                results.append({"check_id": "network_analysis", "status": "error", "error": str(e)})

        self.all_results.extend(results)
        return results

    def get_summary(self) -> dict:
        """Get a summary of all verification activity."""
        total = len(self.completed_checks)
        completed = sum(1 for c in self.completed_checks if c["status"] == "completed")
        errors = sum(1 for c in self.completed_checks if c["status"] == "error")

        # Categorize results
        by_category = {}
        for check in self.completed_checks:
            check_id = check.get("check_id", "unknown")
            registry_entry = VERIFICATION_REGISTRY.get(check_id, {})
            category = registry_entry.get("category", "unknown")
            by_category.setdefault(category, []).append(check_id)

        # Find notable findings
        notable = []
        for check in self.completed_checks:
            if check["status"] != "completed":
                continue
            result = check.get("result", {})
            if isinstance(result, dict):
                # Check for red flags
                if result.get("adverse_found"):
                    notable.append(f"ADVERSE MEDIA found for {check.get('params', {})}")
                if result.get("disqualified"):
                    notable.append(f"DISQUALIFIED DIRECTOR: {check.get('params', {})}")
                if result.get("notices_found"):
                    notable.append(f"GAZETTE NOTICES found for {check.get('params', {})}")
                if result.get("warning_list"):
                    notable.append(f"FCA WARNING LIST: {check.get('params', {})}")
                if not result.get("consistent", True):
                    notable.append(f"TIMELINE INCONSISTENCY detected")
                if not result.get("plausible", True):
                    notable.append(f"FINANCIAL IMPLAUSIBILITY detected")
                if result.get("email_type") == "disposable":
                    notable.append(f"DISPOSABLE EMAIL detected")
                flags = result.get("flags", [])
                if isinstance(flags, list):
                    for f in flags:
                        if isinstance(f, dict) and f.get("severity") == "high":
                            notable.append(f.get("description", "High severity flag"))
                        elif isinstance(f, str) and "red flag" in f.lower():
                            notable.append(f)

        return {
            "total_checks_run": total,
            "completed": completed,
            "errors": errors,
            "categories_covered": list(by_category.keys()),
            "checks_by_category": {k: len(v) for k, v in by_category.items()},
            "notable_findings": notable[:20],
        }
