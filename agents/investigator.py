"""
Background Investigation Agent.

Runs in a separate thread while the customer types.
Extracts verifiable facts from each answer and searches them online.
Results are stored in investigation_log and fed back to the main orchestrator.
"""
import asyncio
import json
import threading
from datetime import datetime
from typing import Optional

import anthropic

import httpx

from tools import web_search, verification, companies_house
from tools import web_analysis


# ─── Sanctions checking ───

async def check_sanctions(name: str, entity_type: str = "auto") -> dict:
    """Check a person or company against global sanctions lists.

    Uses OpenSanctions API (free, no key) which aggregates:
    - UK OFSI (HM Treasury)
    - US OFAC SDN
    - EU consolidated list
    - UN Security Council
    - plus ~40 other sources

    Falls back to web search if API is unavailable.

    entity_type: "person", "company", or "auto" (guess from name).
    """
    result = {
        "name": name,
        "entity_type": entity_type,
        "sanctioned": False,
        "matches": [],
        "sources_checked": [],
        "error": None,
    }

    if not name or len(name.strip()) < 2:
        result["error"] = "Name too short to check"
        return result

    # ── 1. OpenSanctions API ──
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            # /search endpoint — free, no key, returns top matches
            resp = await client.get(
                "https://api.opensanctions.org/search/default",
                params={
                    "q": name,
                    "limit": 5,
                },
                headers={"Accept": "application/json"},
            )
            if resp.status_code == 200:
                data = resp.json()
                result["sources_checked"].append("opensanctions_api")

                for entry in data.get("results", []):
                    score = entry.get("score", 0)
                    # Only consider matches with reasonable confidence
                    if score < 0.5:
                        continue

                    match = {
                        "matched_name": entry.get("caption", ""),
                        "score": round(score, 2),
                        "schema": entry.get("schema", ""),
                        "datasets": [],
                        "properties": {},
                    }

                    # Extract which sanctions lists this entity is on
                    for ds in entry.get("datasets", []):
                        match["datasets"].append(ds)

                    # Key properties
                    props = entry.get("properties", {})
                    for key in ["country", "nationality", "birthDate",
                                "incorporationDate", "idNumber", "notes",
                                "topics", "program"]:
                        if key in props:
                            match["properties"][key] = props[key]

                    result["matches"].append(match)

                if result["matches"]:
                    # Check if any match is high-confidence
                    top_score = max(m["score"] for m in result["matches"])
                    if top_score >= 0.85:
                        result["sanctioned"] = True
                    elif top_score >= 0.65:
                        result["sanctioned"] = "possible_match"
            else:
                result["sources_checked"].append(
                    f"opensanctions_api (HTTP {resp.status_code})"
                )
    except Exception as e:
        result["sources_checked"].append(f"opensanctions_api (error: {str(e)[:60]})")

    # ── 2. Web search fallback / supplement ──
    try:
        queries = [
            f'"{name}" sanctions OR sanctioned OR "OFSI" OR "OFAC"',
            f'"{name}" "HM Treasury" OR "SDN list" OR "designated persons"',
        ]
        for q in queries:
            search_result = await web_search.search_business_online(q, max_results=3)
            hits = search_result.get("results", [])
            if hits:
                result["sources_checked"].append("web_search")
                for hit in hits:
                    snippet = hit.get("snippet", "").lower()
                    # Look for actual sanctions mentions in context of this name
                    name_lower = name.lower()
                    name_parts = name_lower.split()
                    if any(part in snippet for part in name_parts if len(part) > 2):
                        if any(kw in snippet for kw in [
                            "sanction", "designated", "frozen", "prohibited",
                            "ofsi", "ofac", "sdn", "asset freeze",
                        ]):
                            result["matches"].append({
                                "matched_name": name,
                                "score": 0.6,
                                "source": "web_search",
                                "snippet": hit.get("snippet", "")[:200],
                                "url": hit.get("url", ""),
                            })
                            if not result["sanctioned"]:
                                result["sanctioned"] = "possible_match"
                break  # One query is enough if we got results
    except Exception:
        pass

    if not result["sources_checked"]:
        result["error"] = "All sanctions check sources failed"

    return result


async def check_sanctions_batch(person_name: str, company_name: str) -> dict:
    """Check both person and company in parallel. Returns combined results."""
    tasks = {}
    if person_name:
        tasks["person"] = check_sanctions(person_name, entity_type="person")
    if company_name:
        tasks["company"] = check_sanctions(company_name, entity_type="company")

    if not tasks:
        return {"person": None, "company": None}

    keys = list(tasks.keys())
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)

    combined = {"person": None, "company": None}
    for key, res in zip(keys, results):
        if isinstance(res, Exception):
            combined[key] = {"name": person_name if key == "person" else company_name,
                             "error": str(res), "sanctioned": False, "matches": []}
        else:
            combined[key] = res

    return combined


# ─── Fact extraction prompt (lightweight, no tool_use) ───

EXTRACT_FACTS_PROMPT = """You extract verifiable facts from a customer's answer during a KYC interview.

CONTEXT (what we know so far):
{context}

QUESTION ASKED: {question}
CUSTOMER ANSWER: {answer}

Extract any facts that can be independently verified online. Return JSON only:
{{
    "facts": [
        {{
            "claim": "what the customer claimed",
            "entity_name": "JUST the name of the entity — e.g. 'Tochka Bank', 'John Smith', 'Lloyds'. Leave empty string if no specific entity.",
            "type": "person_name|company_name|address|website|url|supplier|client_name|partner|agent|contractor|shareholder|bank|role|date|financial|competitor|linkedin_profile|industry_detail|other",
            "amount": "for financial claims ONLY: the number mentioned, e.g. '300000', '150000-200000'. Empty string if not financial.",
            "currency": "for financial claims ONLY: 'GBP', 'USD', 'EUR', etc. Empty string if not financial.",
            "financial_context": "for financial claims ONLY: what the number refers to — e.g. 'annual_salary', 'annual_turnover', 'monthly_rent', 'property_price', 'operating_costs', 'savings'. Empty string if not financial.",
            "search_query": "the best Google search query to verify this fact",
            "verification_strategy": "what to look for in search results"
        }}
    ]
}}

RULES:
- Only extract CONCRETE, SEARCHABLE facts. Skip vague statements.
- "search_query" should be a real web search query that would find evidence for or against the claim.
- Good facts: specific names, company names, addresses, websites, claimed roles, dates, specific suppliers, specific financial figures.
- Bad facts: opinions, feelings, vague descriptions ("we do a good job").
- If there are no verifiable facts in the answer, return {{"facts": []}}
- Maximum 5 facts per answer — pick the most important ones.
- CRITICAL: "entity_name" must be ONLY the proper name — never a sentence or description.
  CORRECT: "entity_name": "Tochka Bank"
  WRONG: "entity_name": "Customer previously worked at Tochka Bank"
  CORRECT: "entity_name": "Lloyds"
  WRONG: "entity_name": "Customer moved £400k to Lloyds bank in the UK"

FINANCIAL CLAIMS — extract ALL specific numbers:
- Salaries / earnings: "300-400k per year" → type: "financial", amount: "300000-400000", financial_context: "annual_salary"
- Turnover: "150-200k revenue" → type: "financial", amount: "150000-200000", financial_context: "annual_turnover"
- Property / asset prices: "I bought it for 500k" → type: "financial", amount: "500000", financial_context: "property_price"
- Operating costs: "costs are 100-120k" → type: "financial", amount: "100000-120000", financial_context: "operating_costs"
- Savings / funds: "I have 400k" → type: "financial", amount: "400000", financial_context: "savings"
- For financial facts, the search_query should check if the claimed amount is realistic (e.g. "CDO salary UK fintech" or "horse farm B&B annual turnover UK")

COUNTERPARTY NAMES:
Pay special attention to ANY third-party names mentioned: suppliers, clients, partners, agents, contractors, banks, investors, shareholders, directors, accountants, lawyers.
Each named entity must be extracted as a separate fact with the correct type and a clean entity_name.
These names need sanctions screening.

URLS AND LINKS:
If the customer mentions ANY URL, website, or social media handle, extract it with type "url" or "linkedin_profile".
- Website: "our site is example.com" → type: "url", entity_name: "https://example.com", search_query: "site:example.com"
- LinkedIn: "my linkedin is johnsmith" → type: "linkedin_profile", entity_name: "https://linkedin.com/in/johnsmith"
- Property listing: "rightmove.co.uk/property/12345" → type: "url", entity_name: the full URL
URLs are HIGH-PRIORITY facts — they can be directly verified.

COMPETITORS:
If the customer names competitors, extract each with type "competitor" and a clean entity_name.
- "Our main competitor is FarmStay UK" → type: "competitor", entity_name: "FarmStay UK"
Competitors can be verified to exist and help confirm the customer knows their market."""


class BackgroundInvestigator:
    """Runs fact-checking searches in the background, one thread per answer."""

    def __init__(self):
        self.investigation_log = []  # [{timestamp, question, answer, facts_found, search_results}]
        self.sanctions_results = {}  # {"person": {...}, "company": {...}, "extra_names": [...]}
        self._threads = []
        self._results_queue = []  # Completed results waiting to be collected
        self._lock = threading.Lock()
        self._checked_names = set()  # Names already checked for sanctions

    # ── Known foreign companies → language/country for native-language search ──
    # Maps lowercase company name → (language_code, native_name_or_empty)
    FOREIGN_COMPANIES = {
        # Russian / CIS
        "tochka": ("ru", "Точка"), "tochka bank": ("ru", "Точка банк"),
        "sberbank": ("ru", "Сбербанк"), "sber": ("ru", "Сбер"),
        "alfa bank": ("ru", "Альфа банк"), "alfa-bank": ("ru", "Альфа-банк"),
        "vtb": ("ru", "ВТБ"), "gazprombank": ("ru", "Газпромбанк"),
        "tinkoff": ("ru", "Тинькофф"), "yandex": ("ru", "Яндекс"),
        "ozon": ("ru", "Озон"), "wildberries": ("ru", "Вайлдберриз"),
        "mail.ru": ("ru", ""), "vk": ("ru", ""), "kaspersky": ("ru", "Касперский"),
        "avito": ("ru", "Авито"), "raiffeisen russia": ("ru", ""),
        # Chinese
        "alibaba": ("zh", "阿里巴巴"), "tencent": ("zh", "腾讯"),
        "baidu": ("zh", "百度"), "huawei": ("zh", "华为"),
        "xiaomi": ("zh", "小米"), "bytedance": ("zh", "字节跳动"),
        "jd.com": ("zh", "京东"), "icbc": ("zh", "工商银行"),
        "bank of china": ("zh", "中国银行"), "ping an": ("zh", "平安"),
        "nio": ("zh", "蔚来"), "byd": ("zh", "比亚迪"),
        # Japanese
        "softbank": ("ja", "ソフトバンク"), "rakuten": ("ja", "楽天"),
        "toyota": ("ja", "トヨタ"), "sony": ("ja", "ソニー"),
        "mitsubishi": ("ja", "三菱"), "mizuho": ("ja", "みずほ"),
        "nomura": ("ja", "野村"), "sumitomo": ("ja", "住友"),
        # Korean
        "samsung": ("ko", "삼성"), "hyundai": ("ko", "현대"),
        "lg": ("ko", "LG"), "kakao": ("ko", "카카오"),
        "naver": ("ko", "네이버"), "sk group": ("ko", "SK그룹"),
        "shinhan bank": ("ko", "신한은행"),
        # Arabic / Middle East
        "emirates nbd": ("ar", "الإمارات دبي الوطني"),
        "al rajhi bank": ("ar", "مصرف الراجحي"),
        "saudi aramco": ("ar", "أرامكو السعودية"),
        "qatar airways": ("ar", "الخطوط الجوية القطرية"),
        "etisalat": ("ar", "اتصالات"),
        # Turkish
        "garanti bank": ("tr", "Garanti Bankası"),
        "is bank": ("tr", "İş Bankası"), "isbank": ("tr", "İş Bankası"),
        "turkcell": ("tr", "Turkcell"), "thy": ("tr", "Türk Hava Yolları"),
        "halkbank": ("tr", "Halkbank"),
        # Indian
        "tata": ("hi", "टाटा"), "reliance": ("hi", "रिलायंस"),
        "infosys": ("hi", ""), "wipro": ("hi", ""),
        "hdfc bank": ("hi", ""), "icici bank": ("hi", ""),
        "state bank of india": ("hi", "भारतीय स्टेट बैंक"),
        # German
        "deutsche bank": ("de", ""), "commerzbank": ("de", ""),
        "siemens": ("de", ""), "allianz": ("de", ""),
        # French
        "bnp paribas": ("fr", ""), "societe generale": ("fr", "Société Générale"),
        "credit agricole": ("fr", "Crédit Agricole"), "total": ("fr", ""),
        # Spanish
        "santander": ("es", ""), "bbva": ("es", ""),
        "telefonica": ("es", "Telefónica"), "inditex": ("es", ""),
        # Portuguese / Brazilian
        "itau": ("pt", "Itaú"), "bradesco": ("pt", ""),
        "petrobras": ("pt", ""), "nubank": ("pt", ""),
        # Thai
        "kasikornbank": ("th", "ธนาคารกสิกรไทย"),
        "bangkok bank": ("th", "ธนาคารกรุงเทพ"),
        "scb": ("th", "ธนาคารไทยพาณิชย์"),
    }

    # ── Nationality → language code mapping ──
    NATIONALITY_TO_LANG = {
        "russian": "ru", "russia": "ru", "russian federation": "ru",
        "ukrainian": "uk", "ukraine": "uk",
        "belarusian": "ru", "belarus": "ru",  # Most search in Russian
        "kazakh": "ru", "kazakhstan": "ru",  # Russian widely used
        "chinese": "zh", "china": "zh", "prc": "zh",
        "japanese": "ja", "japan": "ja",
        "korean": "ko", "south korean": "ko", "korea": "ko",
        "arabic": "ar", "saudi": "ar", "saudi arabian": "ar",
        "emirati": "ar", "uae": "ar", "qatari": "ar", "kuwaiti": "ar",
        "egyptian": "ar", "lebanese": "ar", "iraqi": "ar", "jordanian": "ar",
        "turkish": "tr", "turkey": "tr", "türkiye": "tr",
        "indian": "hi", "india": "hi",
        "pakistani": "ur", "pakistan": "ur",
        "bangladeshi": "bn", "bangladesh": "bn",
        "german": "de", "germany": "de",
        "french": "fr", "france": "fr",
        "spanish": "es", "spain": "es",
        "portuguese": "pt", "portugal": "pt", "brazilian": "pt", "brazil": "pt",
        "italian": "it", "italy": "it",
        "polish": "pl", "poland": "pl",
        "romanian": "ro", "romania": "ro",
        "bulgarian": "bg", "bulgaria": "bg",
        "thai": "th", "thailand": "th",
        "vietnamese": "vi", "vietnam": "vi",
        "persian": "fa", "iranian": "fa", "iran": "fa",
        "greek": "el", "greece": "el",
        "hebrew": "he", "israeli": "he", "israel": "he",
        "georgian": "ka", "georgia": "ka",
        "armenian": "hy", "armenia": "hy",
        "uzbek": "uz", "uzbekistan": "uz",
    }

    def run_deep_person_search(self, person_name: str, context: str,
                                nationality: str = "", employer: str = ""):
        """Run deep background search: LinkedIn analysis + native language search.
        Called when we learn new info about the person (nationality, employer, LinkedIn).
        Non-blocking — launches a thread.
        """
        def _run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                result = loop.run_until_complete(
                    self._deep_person_search(person_name, context, nationality, employer)
                )
                with self._lock:
                    self._results_queue.append(result)
            except Exception as e:
                with self._lock:
                    self._results_queue.append({
                        "timestamp": datetime.now().isoformat(),
                        "question": "deep_person_search",
                        "answer": person_name,
                        "error": str(e),
                        "findings": []
                    })
            finally:
                loop.close()

        thread = threading.Thread(target=_run, daemon=True)
        self._threads.append(thread)
        thread.start()

    async def _deep_person_search(self, person_name: str, context: str,
                                   nationality: str, employer: str) -> dict:
        """Deep search: native language + LinkedIn + employer-specific searches.
        Works for ANY language — uses Haiku for name translation."""
        client = anthropic.AsyncAnthropic()
        result = {
            "timestamp": datetime.now().isoformat(),
            "question": "deep_person_search",
            "answer": f"{person_name} ({nationality}, {employer})",
            "findings": []
        }
        search_tasks = []

        # Determine the target language
        lang_code = self._detect_language(nationality, employer)

        if lang_code:
            # Use Haiku to translate name + employer to native script
            translations = await self._translate_for_search(
                client, person_name, employer, lang_code
            )
            native_name = translations.get("person_name_native", "")
            native_employer = translations.get("employer_native", "")
            search_queries = translations.get("search_queries", [])

            base_fact = {
                "claim": f"Person identity: {person_name}",
                "type": "person_name",
                "entity_name": person_name,
                "verification_strategy": f"Find information about this person in {lang_code} sources"
            }

            # Search with native name
            if native_name:
                search_tasks.append(self._search_and_assess(
                    client, base_fact,
                    f'"{native_name}"',
                    source_label=f"native_web_{lang_code}"
                ))
                # Native name + native employer
                if native_employer:
                    search_tasks.append(self._search_and_assess(
                        client, base_fact,
                        f'"{native_name}" "{native_employer}"',
                        source_label=f"native_employer_{lang_code}"
                    ))

            # Use Haiku-suggested search queries
            for sq in search_queries[:3]:
                if sq and sq != native_name:
                    search_tasks.append(self._search_and_assess(
                        client, base_fact, sq,
                        source_label=f"native_search_{lang_code}"
                    ))

            # Latin name + native employer (catches bilingual content)
            if native_employer:
                emp_fact = {
                    "claim": f"{person_name} worked at {employer}",
                    "type": "role",
                    "entity_name": employer,
                    "verification_strategy": f"Verify {person_name} was employed at {employer}"
                }
                search_tasks.append(self._search_and_assess(
                    client, emp_fact,
                    f'{person_name} "{native_employer}"',
                    source_label=f"mixed_employer_{lang_code}"
                ))

        # 2. Search for the person on LinkedIn directly if we have their URL
        if "linkedin" in context.lower():
            import re
            li_match = re.search(r'linkedin[:\s]*(?:\.com/in/)?(\w[\w-]+)', context.lower())
            if li_match:
                handle = li_match.group(1)
                li_url = f"https://linkedin.com/in/{handle}"
                li_fact = {
                    "claim": f"LinkedIn profile: {handle}",
                    "type": "linkedin_profile",
                    "entity_name": li_url,
                }
                search_tasks.append(self._verify_linkedin(client, li_fact, li_url, context))

        # 3. Always search for person + employer in English too
        if employer:
            emp_fact = {
                "claim": f"{person_name} worked at {employer}",
                "type": "role",
                "entity_name": employer,
                "verification_strategy": f"Verify {person_name} was employed at {employer}"
            }
            search_tasks.append(self._search_and_assess(
                client, emp_fact,
                f'"{person_name}" "{employer}"',
                source_label="employer_en_check"
            ))

        if search_tasks:
            findings = await asyncio.gather(*search_tasks, return_exceptions=True)
            for f in findings:
                if isinstance(f, dict):
                    result["findings"].append(f)

        return result

    def _detect_language(self, nationality: str, employer: str) -> str:
        """Detect the appropriate language code for native-language search.
        Returns empty string if English (no native search needed)."""
        # Check nationality first
        if nationality:
            lang = self.NATIONALITY_TO_LANG.get(nationality.lower().strip(), "")
            if lang:
                return lang

        # Check employer
        if employer:
            emp_lower = employer.lower().strip()
            entry = self.FOREIGN_COMPANIES.get(emp_lower)
            if entry:
                return entry[0]

        return ""

    def _get_native_company_name(self, employer: str) -> str:
        """Get pre-stored native name for a known company, or empty string."""
        if not employer:
            return ""
        entry = self.FOREIGN_COMPANIES.get(employer.lower().strip())
        if entry and entry[1]:
            return entry[1]
        return ""

    async def _translate_for_search(self, client, person_name: str,
                                     employer: str, lang_code: str) -> dict:
        """Use Haiku to translate a person's name and employer into native script.
        Returns dict with: person_name_native, employer_native, search_queries."""

        # Check if we have a pre-stored native employer name
        known_native_emp = self._get_native_company_name(employer)

        lang_names = {
            "ru": "Russian", "zh": "Chinese (Simplified)", "ja": "Japanese",
            "ko": "Korean", "ar": "Arabic", "tr": "Turkish", "hi": "Hindi",
            "ur": "Urdu", "bn": "Bengali", "de": "German", "fr": "French",
            "es": "Spanish", "pt": "Portuguese", "it": "Italian", "pl": "Polish",
            "ro": "Romanian", "bg": "Bulgarian", "th": "Thai", "vi": "Vietnamese",
            "fa": "Persian/Farsi", "el": "Greek", "he": "Hebrew",
            "ka": "Georgian", "hy": "Armenian", "uz": "Uzbek", "uk": "Ukrainian",
        }
        lang_name = lang_names.get(lang_code, lang_code)

        prompt = f"""Translate these for web search in {lang_name}. Return JSON only.

Person name (Latin): {person_name}
Employer (Latin): {employer or "unknown"}
{"Known employer in native: " + known_native_emp if known_native_emp else ""}

Return JSON:
{{
    "person_name_native": "the person's full name written in {lang_name} script as it would appear in that country's records/media. If you're unsure of the correct form, return empty string.",
    "employer_native": "the employer name in {lang_name}. Use the well-known local name if this is a famous company. Empty string if unknown.",
    "search_queries": ["2-3 search queries in {lang_name} that would find this person online — e.g. their name + employer, their name + job title, their name + city"]
}}

IMPORTANT: Only transliterate if you're confident. For names with clear etymology (e.g. Slavic names for Russian, Chinese names for Chinese), transliterate. For names that don't belong to that language group, just use the Latin form in search queries."""

        try:
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=300,
                messages=[{"role": "user", "content": prompt}]
            )
            text = response.content[0].text.strip()
            json_start = text.find('{')
            json_end = text.rfind('}')
            if json_start >= 0 and json_end > json_start:
                parsed = json.loads(text[json_start:json_end + 1])
                # Use known native employer if Haiku didn't provide one
                if known_native_emp and not parsed.get("employer_native"):
                    parsed["employer_native"] = known_native_emp
                return parsed
        except Exception:
            pass

        # Fallback: use known native names if available
        return {
            "person_name_native": "",
            "employer_native": known_native_emp,
            "search_queries": []
        }

    def investigate_answer(self, question: str, answer: str, case_context: str):
        """Start background investigation of one customer answer.
        Non-blocking — launches a thread and returns immediately.
        """
        def _run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                result = loop.run_until_complete(
                    self._do_investigation(question, answer, case_context)
                )
                with self._lock:
                    self._results_queue.append(result)
            except Exception as e:
                with self._lock:
                    self._results_queue.append({
                        "timestamp": datetime.now().isoformat(),
                        "question": question,
                        "answer": answer[:100],
                        "error": str(e),
                        "findings": []
                    })
            finally:
                loop.close()

        thread = threading.Thread(target=_run, daemon=True)
        self._threads.append(thread)
        thread.start()

    def collect_results(self) -> list:
        """Collect completed investigation results. Non-blocking."""
        # Clean up finished threads
        self._threads = [t for t in self._threads if t.is_alive()]

        with self._lock:
            new_results = list(self._results_queue)
            self._results_queue = []

        # Add to permanent log
        for r in new_results:
            self.investigation_log.append(r)

        return new_results

    def wait_and_collect(self, timeout: float = 15) -> list:
        """Wait for all running investigations, then collect."""
        for t in self._threads:
            t.join(timeout=timeout)
        self._threads = [t for t in self._threads if t.is_alive()]
        return self.collect_results()

    async def run_initial_sanctions_check(self, person_name: str, company_name: str) -> dict:
        """Run sanctions check at interview start. Blocking — call with await."""
        result = await check_sanctions_batch(person_name, company_name)
        self.sanctions_results = result

        # Track names so we don't re-check
        if person_name:
            self._checked_names.add(person_name.lower().strip())
        if company_name:
            self._checked_names.add(company_name.lower().strip())

        return result

    def check_name_for_sanctions(self, name: str, entity_type: str = "auto"):
        """Check a newly discovered name (supplier, partner, etc.) in the background.
        Non-blocking — launches a thread."""
        name_key = name.lower().strip()
        if name_key in self._checked_names or len(name_key) < 3:
            return  # Already checked or too short

        self._checked_names.add(name_key)

        def _run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                result = loop.run_until_complete(check_sanctions(name, entity_type))
                with self._lock:
                    if "extra_names" not in self.sanctions_results:
                        self.sanctions_results["extra_names"] = []
                    self.sanctions_results["extra_names"].append(result)
            finally:
                loop.close()

        thread = threading.Thread(target=_run, daemon=True)
        self._threads.append(thread)
        thread.start()

    def get_sanctions_summary(self) -> str:
        """Compact summary of sanctions check results for the system prompt."""
        if not self.sanctions_results:
            return ""

        lines = []
        for key in ["person", "company"]:
            res = self.sanctions_results.get(key)
            if not res:
                continue
            name = res.get("name", "")
            sanctioned = res.get("sanctioned", False)
            if sanctioned is True:
                lines.append(f"🚨 SANCTIONS HIT: {name} — HIGH CONFIDENCE MATCH on {', '.join(m.get('datasets', ['unknown']) for m in res.get('matches', [])[:2])}")
            elif sanctioned == "possible_match":
                top = res["matches"][0] if res.get("matches") else {}
                lines.append(f"⚠ POSSIBLE SANCTIONS MATCH: {name} (score: {top.get('score', '?')}) — needs manual review")
            else:
                lines.append(f"✓ {name} — no sanctions matches found")

        # Extra names found during interview (counterparties)
        extra = self.sanctions_results.get("extra_names", [])
        if extra:
            lines.append("--- Counterparties / mentioned entities ---")
            for res in extra:
                name = res.get("name", "")
                sanctioned = res.get("sanctioned", False)
                if sanctioned is True:
                    lines.append(f"🚨 SANCTIONS HIT (counterparty): {name} — HIGH CONFIDENCE MATCH")
                elif sanctioned == "possible_match":
                    top = res["matches"][0] if res.get("matches") else {}
                    lines.append(f"⚠ POSSIBLE SANCTIONS MATCH (counterparty): {name} (score: {top.get('score', '?')})")
                else:
                    lines.append(f"✓ {name} — clear")

        return "\n".join(lines) if lines else ""

    def get_findings_summary(self) -> str:
        """Compact summary of all investigation findings for the system prompt."""
        if not self.investigation_log:
            return ""

        lines = []
        for entry in self.investigation_log:
            if entry.get("error"):
                continue
            findings = entry.get("findings", [])
            if not findings:
                continue
            for f in findings:
                status = f.get("status", "unknown")
                icon = "✓" if status == "confirmed" else "✗" if status == "contradicted" else "?"
                confidence = f.get("confidence", "low")
                claim = f.get("claim", "")[:80]
                evidence = f.get("evidence", "")[:150]
                key_detail = f.get("key_detail", "")
                line = f"[{icon}] ({confidence}) {claim} — {evidence}"
                if key_detail:
                    line += f" | KEY: {key_detail[:80]}"
                lines.append(line)

        return "\n".join(lines) if lines else ""

    def get_detailed_findings(self) -> list:
        """Return full structured findings for the risk assessment report.
        Unlike get_findings_summary(), this preserves all detail."""
        all_findings = []
        for entry in self.investigation_log:
            if entry.get("error"):
                continue
            findings = entry.get("findings", [])
            for f in findings:
                all_findings.append({
                    "claim": f.get("claim", ""),
                    "type": f.get("type", "other"),
                    "status": f.get("status", "not_found"),
                    "confidence": f.get("confidence", "low"),
                    "evidence": f.get("evidence", ""),
                    "key_detail": f.get("key_detail", ""),
                    "source": f.get("source", "web"),
                    "urls": f.get("urls", []),
                    "question_context": entry.get("question", ""),
                    "answer_excerpt": entry.get("answer", "")[:200],
                })
        return all_findings

    async def _do_investigation(self, question: str, answer: str, context: str) -> dict:
        """Full investigation pipeline:
        1. Extract verifiable facts from the answer (Claude call)
        2. Search each fact online (web search)
        3. Assess whether findings confirm or contradict the claim
        """
        client = anthropic.AsyncAnthropic()
        result = {
            "timestamp": datetime.now().isoformat(),
            "question": question,
            "answer": answer[:100],
            "findings": []
        }

        # Step 1: Extract facts
        prompt = EXTRACT_FACTS_PROMPT.format(
            context=context[:2000],
            question=question,
            answer=answer
        )

        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}]
        )

        text = response.content[0].text.strip()
        facts = []
        try:
            json_start = text.find('{')
            json_end = text.rfind('}')
            if json_start >= 0 and json_end > json_start:
                parsed = json.loads(text[json_start:json_end + 1])
                facts = parsed.get("facts", [])
        except (json.JSONDecodeError, AttributeError):
            pass

        if not facts:
            return result

        # Step 2: Search each fact in parallel — use targeted strategies
        search_tasks = []
        for fact in facts[:5]:  # All extracted facts
            query = fact.get("search_query", "")
            fact_type = fact.get("type", "other")
            if query:
                # Add targeted search tasks based on fact type
                search_tasks.append(self._search_and_assess(
                    client, fact, query
                ))
                # Targeted: Companies House search for company claims
                if fact_type in ("company_name", "role", "date") and "company" in query.lower():
                    ch_query = f'{fact.get("claim", "")} site:find-and-update.company-information.service.gov.uk'
                    search_tasks.append(self._search_and_assess(
                        client, fact, ch_query, source_label="companies_house"
                    ))
                # Targeted: address verification
                elif fact_type == "address":
                    addr_query = f'"{fact.get("claim", "")}" business OR office OR registered'
                    search_tasks.append(self._search_and_assess(
                        client, fact, addr_query, source_label="address_check"
                    ))
                # Targeted: financial plausibility check
                elif fact_type == "financial" and fact.get("amount"):
                    search_tasks.append(self._verify_financial_claim(
                        client, fact, context
                    ))
                # Targeted: URL / website analysis
                elif fact_type in ("url", "website"):
                    url = fact.get("entity_name", "").strip()
                    if url and ("." in url or "http" in url):
                        search_tasks.append(self._verify_url(client, fact, url))
                # Targeted: LinkedIn profile verification
                elif fact_type == "linkedin_profile":
                    li_url = fact.get("entity_name", "").strip()
                    if li_url:
                        search_tasks.append(self._verify_linkedin(client, fact, li_url, context))
                # Targeted: Competitor verification
                elif fact_type == "competitor":
                    comp_name = fact.get("entity_name", "").strip()
                    if comp_name:
                        comp_query = f'"{comp_name}" business UK'
                        search_tasks.append(self._search_and_assess(
                            client, fact, comp_query, source_label="competitor_check"
                        ))

        if search_tasks:
            findings = await asyncio.gather(*search_tasks, return_exceptions=True)
            for f in findings:
                if isinstance(f, dict):
                    result["findings"].append(f)

        # Check any person/company names mentioned in the answer for sanctions
        person_types = {"person_name", "shareholder", "agent", "contractor"}
        company_types = {"company_name", "supplier", "client_name", "partner", "bank"}
        all_checkable = person_types | company_types
        detected_foreign_company = None  # (name, lang_code)
        for fact in facts[:5]:
            fact_type = fact.get("type", "")
            if fact_type in all_checkable:
                # Prefer entity_name (clean name) over claim (full sentence)
                name = fact.get("entity_name", "").strip()
                if not name or len(name) < 3:
                    name = fact.get("claim", "")
                # Skip if it's clearly a sentence, not a name (more than 6 words = likely a sentence)
                if name and len(name) > 2 and len(name.split()) <= 6:
                    entity_type = "person" if fact_type in person_types else "company"
                    self.check_name_for_sanctions(name, entity_type)
                    # Detect foreign companies for deep search trigger
                    if fact_type in company_types:
                        fc_entry = self.FOREIGN_COMPANIES.get(name.lower().strip())
                        if fc_entry:
                            detected_foreign_company = (name, fc_entry[0])

        # Detect nationality from the answer text itself
        import re
        answer_lower = answer.lower()
        detected_nationality = ""
        # Build patterns from NATIONALITY_TO_LANG keys
        nationality_patterns = [
            ("i'm ", ""), ("i am ", ""), ("", " national"),
            ("from ", ""), ("", " citizen"),
        ]
        for prefix, suffix in nationality_patterns:
            for nat_key in self.NATIONALITY_TO_LANG:
                pattern = f"{prefix}{nat_key}{suffix}"
                if pattern in answer_lower:
                    detected_nationality = nat_key
                    break
            if detected_nationality:
                break

        # If a foreign company was mentioned OR non-English nationality revealed, trigger deep search
        has_foreign_signal = detected_foreign_company or (
            detected_nationality and self.NATIONALITY_TO_LANG.get(detected_nationality, "") not in ("", "en")
        )
        should_deep_search = has_foreign_signal and not getattr(self, '_deep_search_done', False)
        if should_deep_search:
            self._deep_search_done = True
            # Try to extract person name from context
            name_match = re.search(r'"full_name":\s*"([^"]+)"', context)
            if name_match:
                person_name = name_match.group(1)
                employer = detected_foreign_company[0] if detected_foreign_company else ""
                # If no employer yet, try to find it in context
                if not employer:
                    ctx_lower = context.lower()
                    for fc_name in self.FOREIGN_COMPANIES:
                        if fc_name in ctx_lower:
                            employer = fc_name.title()
                            break
                self.run_deep_person_search(
                    person_name, context,
                    nationality=detected_nationality or "",
                    employer=employer
                )

        return result

    async def _search_and_assess(self, client, fact: dict, query: str,
                                  source_label: str = "web") -> dict:
        """Search for a fact and assess whether results confirm or contradict it."""
        finding = {
            "claim": fact.get("claim", ""),
            "type": fact.get("type", "other"),
            "search_query": query,
            "source": source_label,
            "status": "not_found",  # confirmed | contradicted | not_found | inconclusive
            "evidence": "",
            "confidence": "low",  # low | medium | high
            "urls": []
        }

        # Run the search
        search_result = await web_search.search_business_online(query, max_results=5)
        results_list = search_result.get("results", [])

        if not results_list:
            finding["evidence"] = "No search results found."
            return finding

        # Collect snippets and URLs — use more results for better context
        snippets = []
        for r in results_list[:5]:
            snippet = r.get("snippet", r.get("body", ""))
            url = r.get("url", r.get("href", ""))
            title = r.get("title", "")
            if snippet:
                snippets.append(f"[{title}] {snippet}" if title else snippet)
            if url:
                finding["urls"].append(url)

        if not snippets:
            return finding

        # Step 3: Assess with a thorough Claude call
        assess_prompt = f"""You are a KYC fact-checker. Assess whether search results CONFIRM or CONTRADICT the customer's claim.

CUSTOMER'S CLAIM: {fact.get('claim', '')}
CLAIM TYPE: {fact.get('type', 'other')}
WHAT TO LOOK FOR: {fact.get('verification_strategy', '')}

SEARCH RESULTS:
{chr(10).join(f"Result {i+1}: {s}" for i, s in enumerate(snippets[:5]))}

Analyze carefully:
1. Do the search results contain direct evidence about THIS specific claim?
2. Is the evidence about the SAME entity (not a different person/company with similar name)?
3. Does the evidence confirm, contradict, or is it unclear?
4. How strong is the evidence? (high = direct official source, medium = credible indirect source, low = weak/unclear)

Return JSON only:
{{
    "status": "confirmed|contradicted|inconclusive",
    "evidence": "2-3 sentence summary of what you found and WHY it confirms/contradicts/is inconclusive",
    "confidence": "high|medium|low",
    "key_detail": "The single most important fact found (e.g. 'Company incorporated 2019-03-15' or 'No record found for this company name')"
}}"""

        try:
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=400,
                messages=[{"role": "user", "content": assess_prompt}]
            )
            text = response.content[0].text.strip()
            json_start = text.find('{')
            json_end = text.rfind('}')
            if json_start >= 0 and json_end > json_start:
                parsed = json.loads(text[json_start:json_end + 1])
                finding["status"] = parsed.get("status", "inconclusive")
                finding["evidence"] = parsed.get("evidence", "")
                finding["confidence"] = parsed.get("confidence", "low")
                finding["key_detail"] = parsed.get("key_detail", "")
        except Exception:
            finding["evidence"] = f"Search found {len(results_list)} results but assessment failed."
            finding["status"] = "inconclusive"

        return finding

    async def _verify_financial_claim(self, client, fact: dict, context: str) -> dict:
        """Verify a financial claim by combining web search with industry benchmarks."""
        amount_str = fact.get("amount", "")
        fin_context = fact.get("financial_context", "")
        claim = fact.get("claim", "")

        finding = {
            "claim": claim,
            "type": "financial",
            "search_query": f"financial_check: {fin_context}",
            "source": "financial_verification",
            "status": "inconclusive",
            "confidence": "low",
            "evidence": "",
            "key_detail": "",
            "urls": []
        }

        # Parse amount range
        amounts = []
        for part in amount_str.replace(",", "").split("-"):
            part = part.strip()
            try:
                amounts.append(float(part))
            except ValueError:
                pass
        if not amounts:
            finding["evidence"] = "Could not parse financial amount."
            return finding

        claimed_low = min(amounts)
        claimed_high = max(amounts)

        # 1. Web search for typical figures
        search_queries = []
        if fin_context == "annual_salary":
            role_hint = fact.get("entity_name", "") or claim
            search_queries.append(f"{role_hint} salary UK average")
        elif fin_context == "annual_turnover":
            search_queries.append(f"{claim} typical annual turnover UK")
        elif fin_context in ("property_price", "savings"):
            search_queries.append(fact.get("search_query", claim))
        elif fin_context == "operating_costs":
            search_queries.append(f"{claim} typical operating costs UK small business")
        else:
            search_queries.append(fact.get("search_query", claim))

        # 2. Get industry benchmarks if we can guess the industry from context
        benchmarks_data = None
        try:
            # Extract industry from context
            industry_hint = ""
            ctx_lower = context.lower()
            for kw in ["restaurant", "construction", "cleaning", "ecommerce", "e-commerce",
                        "consulting", "consultancy", "retail", "beauty", "salon", "transport",
                        "logistics", "takeaway", "food", "it ", "software", "farm", "horse",
                        "hospitality", "b&b", "bed and breakfast", "hotel"]:
                if kw in ctx_lower:
                    industry_hint = kw
                    break
            if industry_hint:
                benchmarks_data = await verification.get_industry_benchmarks(industry_hint)
        except Exception:
            pass

        # 3. Run web search
        snippets = []
        for q in search_queries[:2]:
            try:
                sr = await web_search.search_business_online(q, max_results=3)
                for r in sr.get("results", []):
                    snippet = r.get("snippet", "")
                    if snippet:
                        snippets.append(snippet)
                    url = r.get("url", "")
                    if url:
                        finding["urls"].append(url)
            except Exception:
                pass

        # 4. Assess with Claude — provide both web results and benchmarks
        benchmark_text = ""
        if benchmarks_data and benchmarks_data.get("industry_matched") != "default":
            bm = benchmarks_data.get("benchmarks", {})
            benchmark_text = f"\nINDUSTRY BENCHMARKS ({benchmarks_data['industry_matched']}):\n{json.dumps(bm, indent=2)}"

        assess_prompt = f"""You are a financial plausibility checker for KYC. Assess whether a customer's financial claim is realistic.

CUSTOMER'S CLAIM: {claim}
CLAIMED AMOUNT: {amount_str} {fact.get('currency', 'GBP')}
FINANCIAL CONTEXT: {fin_context} (what the number represents)
{benchmark_text}

WEB SEARCH RESULTS (typical market rates/prices):
{chr(10).join(f"- {s}" for s in snippets[:5]) if snippets else "No search results found."}

Assess:
1. Is the claimed amount within a plausible range for this type of claim?
2. If benchmarks are available, does the amount align with industry norms?
3. Is it suspiciously high, suspiciously low, or reasonable?

Return JSON only:
{{
    "status": "confirmed|contradicted|inconclusive",
    "evidence": "2-3 sentences explaining why this amount is plausible or not. Reference specific benchmark figures or search results.",
    "confidence": "high|medium|low",
    "key_detail": "e.g. 'Typical CDO salary in UK fintech is £80-150k; claimed £300-400k is 2-3x above market rate' or 'Horse farm B&B turnover of £150-200k aligns with hospitality benchmarks'"
}}"""

        try:
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=400,
                messages=[{"role": "user", "content": assess_prompt}]
            )
            text = response.content[0].text.strip()
            json_start = text.find('{')
            json_end = text.rfind('}')
            if json_start >= 0 and json_end > json_start:
                parsed = json.loads(text[json_start:json_end + 1])
                finding["status"] = parsed.get("status", "inconclusive")
                finding["evidence"] = parsed.get("evidence", "")
                finding["confidence"] = parsed.get("confidence", "low")
                finding["key_detail"] = parsed.get("key_detail", "")
        except Exception:
            finding["evidence"] = "Financial verification assessment failed."

        return finding

    async def _verify_url(self, client, fact: dict, url: str) -> dict:
        """Verify a URL: deep website analysis + liveness check in parallel."""
        finding = {
            "claim": fact.get("claim", ""),
            "type": "url",
            "search_query": f"url_check: {url}",
            "source": "url_verification",
            "status": "inconclusive",
            "confidence": "low",
            "evidence": "",
            "key_detail": "",
            "urls": [url],
            "liveness": {},
        }

        try:
            if not url.startswith("http"):
                url = "https://" + url

            # Run both analyses in parallel
            site_task = web_analysis.deep_analyze_website(url)
            liveness_task = web_analysis.analyze_website_liveness(url)
            site_result, liveness_result = await asyncio.gather(
                site_task, liveness_task, return_exceptions=True
            )

            parts = []

            # Website structure analysis
            if isinstance(site_result, dict) and not site_result.get("error"):
                site_score = site_result.get("reliability_score", 0)
                parts.append(f"Structure: {site_result.get('summary', '')}")
            else:
                site_score = 0
                error = site_result.get("error", str(site_result)) if isinstance(site_result, dict) else str(site_result)
                parts.append(f"Website unreachable: {str(error)[:100]}")

            # Liveness analysis
            if isinstance(liveness_result, dict):
                finding["liveness"] = liveness_result
                liveness_score = liveness_result.get("liveness_score", 0)
                parts.append(f"Liveness: {liveness_result.get('summary', '')}")

                # Include key liveness signals
                for sig in liveness_result.get("signals", [])[:3]:
                    parts.append(f"  + {sig}")
                for flag in liveness_result.get("red_flags", [])[:2]:
                    parts.append(f"  ! {flag}")

                # Domain age detail
                if liveness_result.get("domain_age", {}).get("first_snapshot"):
                    finding["key_detail"] = f"Domain first seen: {liveness_result['domain_age']['first_snapshot'][:10]}, " \
                                           f"{liveness_result['domain_age'].get('total_snapshots', 0)} archive snapshots"

                # Reviews detail
                reviews = liveness_result.get("reviews", {})
                if reviews.get("trustpilot", {}).get("found"):
                    parts.append(f"  + Trustpilot: {reviews['trustpilot'].get('snippet', '')[:80]}")
                if reviews.get("google", {}).get("found"):
                    parts.append(f"  + Reviews: {reviews['google'].get('snippet', '')[:80]}")

                # App store
                app = liveness_result.get("app_store", {})
                if app.get("ios_found") or app.get("android_found"):
                    app_names = []
                    if app.get("ios_found"):
                        app_names.append(f"iOS: {app.get('ios_title', '')[:40]}")
                    if app.get("android_found"):
                        app_names.append(f"Android: {app.get('android_title', '')[:40]}")
                    parts.append(f"  + App store: {', '.join(app_names)}")
            else:
                liveness_score = 0

            # Combined assessment
            avg_score = (site_score + liveness_score) / 2 if liveness_score > 0 else site_score
            if avg_score >= 0.5:
                finding["status"] = "confirmed"
                finding["confidence"] = "high" if avg_score >= 0.7 else "medium"
            elif avg_score >= 0.2:
                finding["status"] = "inconclusive"
                finding["confidence"] = "low"
            else:
                finding["status"] = "not_found"
                finding["confidence"] = "medium"

            finding["evidence"] = " | ".join(parts)

            if not finding["key_detail"]:
                finding["key_detail"] = f"Combined score: structure={site_score:.0%}, liveness={liveness_score:.0%}"

        except Exception as e:
            finding["evidence"] = f"URL verification failed: {str(e)[:100]}"

        return finding

    async def _verify_linkedin(self, client, fact: dict, linkedin_url: str, context: str) -> dict:
        """Verify a LinkedIn profile: deep analysis + cross-reference with claims."""
        finding = {
            "claim": fact.get("claim", ""),
            "type": "linkedin_profile",
            "search_query": f"linkedin_check: {linkedin_url}",
            "source": "linkedin_verification",
            "status": "inconclusive",
            "confidence": "low",
            "evidence": "",
            "key_detail": "",
            "urls": [linkedin_url],
            "linkedin_depth": {},
        }

        try:
            # Normalize LinkedIn URL
            if not linkedin_url.startswith("http"):
                if "linkedin.com" not in linkedin_url:
                    linkedin_url = f"https://linkedin.com/in/{linkedin_url}"
                else:
                    linkedin_url = f"https://{linkedin_url}"

            # Extract person name and business from context for deep analysis
            import re as _re
            name_match = _re.search(r'"full_name":\s*"([^"]+)"', context)
            biz_match = _re.search(r'"company_name":\s*"([^"]+)"', context)
            person_name = name_match.group(1) if name_match else ""
            business_name = biz_match.group(1) if biz_match else ""

            # Run both old-style and new deep analysis in parallel
            old_task = web_analysis.deep_analyze_linkedin(linkedin_url)
            depth_task = web_analysis.analyze_linkedin_depth(
                person_name or linkedin_url.split("/")[-1].replace("-", " "),
                business_name
            )
            old_analysis, depth_analysis = await asyncio.gather(
                old_task, depth_task, return_exceptions=True
            )

            # Merge results for the Haiku assessment
            combined_data = {}
            if isinstance(old_analysis, dict):
                combined_data.update(old_analysis)
            if isinstance(depth_analysis, dict):
                finding["linkedin_depth"] = depth_analysis
                combined_data["depth_analysis"] = {
                    "connections": depth_analysis.get("connections"),
                    "headline": depth_analysis.get("headline"),
                    "current_role": depth_analysis.get("current_role"),
                    "location": depth_analysis.get("location"),
                    "activity_signals": depth_analysis.get("activity_signals", []),
                    "profile_completeness": depth_analysis.get("profile_completeness"),
                    "company_page": depth_analysis.get("company_page", {}),
                    "signals": depth_analysis.get("signals", []),
                    "red_flags": depth_analysis.get("red_flags", []),
                    "reliability_score": depth_analysis.get("reliability_score", 0),
                }

            if combined_data:
                li_data = json.dumps(combined_data, ensure_ascii=False)[:2500]

                assess_prompt = f"""You are a KYC fact-checker. Cross-reference LinkedIn profile data with customer claims.

KNOWN CUSTOMER CLAIMS (from interview):
{context[:1500]}

LINKEDIN PROFILE DATA (including deep analysis):
{li_data}

Check:
1. Does the profile match the person's claimed name?
2. Does it show the claimed employer and role?
3. Is the timeline consistent?
4. How many connections? (Under 50 = red flag for established professional)
5. Is the profile active (posts, shares)?
6. Does the company page exist and match?
7. Any red flags (new profile, very few connections, mismatched info)?

Return JSON only:
{{
    "status": "confirmed|contradicted|inconclusive",
    "evidence": "3-4 sentences. Include connection count, headline, activity level, company page status.",
    "confidence": "high|medium|low",
    "key_detail": "The single most important finding (e.g. '500+ connections, headline matches claimed role, company page has 200 followers')"
}}"""

                response = await client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=500,
                    messages=[{"role": "user", "content": assess_prompt}]
                )
                text = response.content[0].text.strip()
                json_start = text.find('{')
                json_end = text.rfind('}')
                if json_start >= 0 and json_end > json_start:
                    parsed = json.loads(text[json_start:json_end + 1])
                    finding["status"] = parsed.get("status", "inconclusive")
                    finding["evidence"] = parsed.get("evidence", "")
                    finding["confidence"] = parsed.get("confidence", "low")
                    finding["key_detail"] = parsed.get("key_detail", "")
            else:
                finding["evidence"] = "LinkedIn profile could not be analyzed"
        except Exception as e:
            finding["evidence"] = f"LinkedIn verification failed: {str(e)[:100]}"

        return finding
