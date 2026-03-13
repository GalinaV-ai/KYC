"""
Web search tool for verifying business presence.
Uses ddgs for real web search results (no API key needed).
All searches run in parallel and non-blocking.
"""
import asyncio
import httpx
import re
import json
from typing import Optional
from concurrent.futures import ThreadPoolExecutor
from ddgs import DDGS

# Shared thread pool for blocking DDG calls
_executor = ThreadPoolExecutor(max_workers=6)


def _search_ddg_sync(query: str, max_results: int = 5, region: str = "uk-en") -> list:
    """Synchronous DDG search (runs in thread pool)."""
    try:
        with DDGS() as ddgs:
            return list(ddgs.text(query, region=region, max_results=max_results))
    except Exception as e:
        return [{"error": str(e)}]


def _search_ddg_news_sync(query: str, max_results: int = 3, region: str = "uk-en") -> list:
    """Synchronous DDG news search (runs in thread pool)."""
    try:
        with DDGS() as ddgs:
            return list(ddgs.news(query, region=region, max_results=max_results))
    except Exception as e:
        return [{"error": str(e)}]


async def _search_ddg(query: str, max_results: int = 5, region: str = "uk-en") -> list:
    """Non-blocking DDG search."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, _search_ddg_sync, query, max_results, region)


async def _search_ddg_news(query: str, max_results: int = 3, region: str = "uk-en") -> list:
    """Non-blocking DDG news search."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, _search_ddg_news_sync, query, max_results, region)


def _parse_results(raw: list) -> list:
    """Parse DDG results into clean format."""
    out = []
    for r in raw:
        if "error" not in r:
            out.append({
                "title": r.get("title", ""),
                "snippet": r.get("body", ""),
                "url": r.get("href", "")
            })
    return out


def _parse_news(raw: list) -> list:
    out = []
    for r in raw:
        if "error" not in r:
            out.append({
                "title": r.get("title", ""),
                "snippet": r.get("body", ""),
                "url": r.get("url", ""),
                "date": r.get("date", ""),
                "source": r.get("source", "")
            })
    return out


async def search_business_online(query: str, max_results: int = 5) -> dict:
    """Search the web for business presence."""
    results = {"query": query, "results": [], "source": "duckduckgo_search"}
    raw = await _search_ddg(query, max_results=max_results)
    results["results"] = _parse_results(raw)
    return results


def _relevance_score(text: str, must_terms: list[str], boost_terms: list[str] = None) -> str:
    """Score a search result's relevance. Returns 'high', 'medium', or 'low'.

    must_terms: key identifiers (person name parts, company name parts).
                If ALL appear → high baseline. If SOME → medium. If NONE → low.
    boost_terms: extra context (location, industry, role).
                 Presence of these shifts score up.
    """
    if not text:
        return "low"
    text_lower = text.lower()
    must_hits = sum(1 for t in must_terms if t.lower() in text_lower)
    must_ratio = must_hits / max(len(must_terms), 1)

    boost_hits = 0
    if boost_terms:
        boost_hits = sum(1 for t in boost_terms if t.lower() in text_lower)

    if must_ratio >= 0.8:
        return "high"
    elif must_ratio >= 0.5 or (must_ratio >= 0.3 and boost_hits >= 1):
        return "medium"
    return "low"


def _name_parts(full_name: str) -> list[str]:
    """Split a name into meaningful parts for matching (skip 1-2 char words)."""
    return [p for p in full_name.strip().split() if len(p) > 2]


async def search_person_online(full_name: str, business_name: str = "", location: str = "UK") -> dict:
    """Search for a person online — all queries run in PARALLEL.
    Results are scored for relevance to THIS specific person+company pair.
    """
    results = {
        "person": full_name,
        "business": business_name,
        "linkedin_results": [],
        "business_associations": [],
        "news_mentions": [],
        "other_results": [],
        "summary": ""
    }

    name_parts = _name_parts(full_name)
    company_parts = _name_parts(business_name) if business_name else []

    # ── Strategy: run targeted queries that combine person + company ──
    # Primary: person + company together (most specific)
    # Secondary: person alone on specific sites
    # Tertiary: person + location (least specific, most noise)

    tasks = []
    task_labels = []

    # 1. LinkedIn: always include company name if we have it (critical for disambiguation)
    li_q = f'"{full_name}" site:linkedin.com'
    if business_name:
        li_q += f' "{business_name}"'
    tasks.append(_search_ddg(li_q, max_results=5))
    task_labels.append("linkedin")

    # 2. If LinkedIn+company finds nothing, we also search LinkedIn with just the name
    if business_name:
        tasks.append(_search_ddg(f'"{full_name}" site:linkedin.com', max_results=3))
        task_labels.append("linkedin_name_only")

    # 3. Companies House — always specific
    ch_q = f'"{full_name}" site:find-and-update.company-information.service.gov.uk'
    if business_name:
        ch_q += f' "{business_name}"'
    tasks.append(_search_ddg(ch_q, max_results=5))
    task_labels.append("companies_house")

    # 4. Person + Company combined (web)
    if business_name:
        tasks.append(_search_ddg(
            f'"{full_name}" "{business_name}" director OR founder OR CEO OR owner',
            max_results=5
        ))
        task_labels.append("person_company")

    # 5. News — always combine with company if possible
    news_q = f'"{full_name}"'
    if business_name:
        news_q += f' "{business_name}"'
    tasks.append(_search_ddg_news(news_q, max_results=5))
    task_labels.append("news")

    # 6. General web (fallback, most noise)
    general_q = f'"{full_name}" {location}'
    if business_name:
        general_q += f' "{business_name}"'
    tasks.append(_search_ddg(general_q, max_results=5))
    task_labels.append("general")

    all_results = await asyncio.gather(*tasks, return_exceptions=True)
    raw_by_label = {}
    for label, raw in zip(task_labels, all_results):
        raw_by_label[label] = raw if not isinstance(raw, Exception) else []

    # ── Score and categorize results ──
    seen_urls = set()
    must_terms = name_parts + company_parts
    boost_terms = [location] if location else []

    def _add_if_new(r: dict, category: str):
        url = r.get("href", r.get("url", ""))
        if url in seen_urls:
            return
        seen_urls.add(url)
        text = f"{r.get('title', '')} {r.get('body', r.get('snippet', ''))}"
        relevance = _relevance_score(text, must_terms, boost_terms)
        entry = {
            "title": r.get("title", ""),
            "snippet": r.get("body", r.get("snippet", "")),
            "url": url,
            "relevance": relevance,
        }
        if category == "linkedin":
            results["linkedin_results"].append(entry)
        elif category == "business":
            results["business_associations"].append(entry)
        elif category == "news":
            results["news_mentions"].append(entry)
        else:
            results["other_results"].append(entry)

    # LinkedIn results
    for r in raw_by_label.get("linkedin", []):
        if "error" not in r and "linkedin.com" in r.get("href", ""):
            _add_if_new(r, "linkedin")
    # LinkedIn name-only fallback — only add if we got nothing from the primary query
    if not results["linkedin_results"]:
        for r in raw_by_label.get("linkedin_name_only", []):
            if "error" not in r and "linkedin.com" in r.get("href", ""):
                _add_if_new(r, "linkedin")

    # Companies House
    for r in raw_by_label.get("companies_house", []):
        if "error" not in r:
            entry = r.copy()
            entry["source"] = "companies_house_search"
            _add_if_new(entry, "business")

    # Person + company combined
    for r in raw_by_label.get("person_company", []):
        if "error" not in r:
            _add_if_new(r, "business")

    # News
    for r in raw_by_label.get("news", []):
        if "error" not in r:
            _add_if_new(r, "news")

    # General — only keep medium/high relevance to filter out name collisions
    for r in raw_by_label.get("general", []):
        if "error" not in r and "linkedin.com" not in r.get("href", ""):
            text = f"{r.get('title', '')} {r.get('body', '')}"
            relevance = _relevance_score(text, must_terms, boost_terms)
            if relevance in ("high", "medium"):
                _add_if_new(r, "other")

    # ── Summary ──
    high_confidence = sum(
        1 for bucket in [results["linkedin_results"], results["business_associations"],
                         results["news_mentions"], results["other_results"]]
        for item in bucket if item.get("relevance") == "high"
    )
    total = (len(results["linkedin_results"]) + len(results["business_associations"]) +
             len(results["news_mentions"]) + len(results["other_results"]))

    if total == 0:
        results["summary"] = f"No online presence found for '{full_name}'"
        if business_name:
            results["summary"] += f" in connection with '{business_name}'"
        results["summary"] += "."
    else:
        parts = [f"Found {total} results ({high_confidence} high-confidence matches)."]
        if results["linkedin_results"]:
            li_high = [r for r in results["linkedin_results"] if r.get("relevance") == "high"]
            if li_high:
                parts.append(f"LinkedIn profile found (likely match).")
            else:
                parts.append(f"LinkedIn results found but may not be the same person.")
        if results["news_mentions"]:
            parts.append(f"{len(results['news_mentions'])} news mentions.")
        results["summary"] = " ".join(parts)

    return results


async def search_company_online(company_name: str, location: str = "UK",
                                 owner_name: str = "") -> dict:
    """Comprehensive company search — all queries run in PARALLEL.
    owner_name is used to disambiguate results for common company names.
    """
    results = {
        "company": company_name,
        "web_results": [],
        "review_results": [],
        "news_results": [],
        "regulatory_results": [],
        "social_media": [],
        "summary": ""
    }

    company_parts = _name_parts(company_name)
    owner_parts = _name_parts(owner_name) if owner_name else []
    must_terms = company_parts
    boost_terms = owner_parts + ([location] if location else [])

    # Run ALL searches in parallel — include owner name in key queries for disambiguation
    owner_hint = f' "{owner_name}"' if owner_name else ""
    all_results = await asyncio.gather(
        _search_ddg(f'"{company_name}" {location}{owner_hint}', max_results=5),
        _search_ddg(f'"{company_name}" reviews trustpilot', max_results=3),
        _search_ddg_news(f'"{company_name}"{owner_hint}', max_results=5),
        _search_ddg(f'"{company_name}" FCA OR HMRC OR "Companies House" OR court', max_results=3),
        _search_ddg(f'"{company_name}" site:linkedin.com', max_results=3),
        _search_ddg(f'"{company_name}" site:facebook.com OR site:instagram.com', max_results=2),
        return_exceptions=True,
    )

    web_raw = all_results[0] if not isinstance(all_results[0], Exception) else []
    review_raw = all_results[1] if not isinstance(all_results[1], Exception) else []
    news_raw = all_results[2] if not isinstance(all_results[2], Exception) else []
    reg_raw = all_results[3] if not isinstance(all_results[3], Exception) else []
    li_raw = all_results[4] if not isinstance(all_results[4], Exception) else []
    social_raw = all_results[5] if not isinstance(all_results[5], Exception) else []

    seen_urls = set()

    def _score_and_add(raw: list, target: list, is_news: bool = False):
        for r in raw:
            if "error" in r:
                continue
            url = r.get("href", r.get("url", ""))
            if url in seen_urls:
                continue
            seen_urls.add(url)
            text = f"{r.get('title', '')} {r.get('body', r.get('snippet', ''))}"
            relevance = _relevance_score(text, must_terms, boost_terms)
            entry = {
                "title": r.get("title", ""),
                "snippet": r.get("body", r.get("snippet", "")),
                "url": url,
                "relevance": relevance,
            }
            if is_news:
                entry["date"] = r.get("date", "")
                entry["source"] = r.get("source", "")
            target.append(entry)

    _score_and_add(web_raw, results["web_results"])
    _score_and_add(review_raw, results["review_results"])
    _score_and_add(news_raw, results["news_results"], is_news=True)
    _score_and_add(reg_raw, results["regulatory_results"])

    for r in li_raw + social_raw:
        if "error" not in r:
            url = r.get("href", "")
            if url in seen_urls:
                continue
            seen_urls.add(url)
            platform = "linkedin" if "linkedin" in url else "facebook" if "facebook" in url else "instagram" if "instagram" in url else "other"
            text = f"{r.get('title', '')} {r.get('body', '')}"
            relevance = _relevance_score(text, must_terms, boost_terms)
            results["social_media"].append({
                "platform": platform, "title": r.get("title", ""),
                "url": url, "snippet": r.get("body", ""),
                "relevance": relevance,
            })

    # Summary — focus on high-confidence results
    high_conf = sum(
        1 for bucket in [results["web_results"], results["review_results"],
                         results["news_results"], results["social_media"]]
        for item in bucket if item.get("relevance") == "high"
    )
    total = sum(len(v) for v in [results["web_results"], results["review_results"],
                                  results["news_results"], results["social_media"]])

    parts = []
    if not results["web_results"]:
        parts.append(f"No web results for '{company_name}'")
    else:
        parts.append(f"{len(results['web_results'])} web results ({high_conf} high-confidence)")
    if results["review_results"]:
        parts.append(f"{len(results['review_results'])} review results")
    else:
        parts.append("no reviews found")
    if results["news_results"]:
        parts.append(f"{len(results['news_results'])} news mentions")
    if results["social_media"]:
        parts.append(f"social media: {', '.join(set(r['platform'] for r in results['social_media']))}")
    else:
        parts.append("no social media found")

    results["summary"] = ". ".join(parts) + "."
    return results


async def check_website_exists(url: str) -> dict:
    """Check if a website URL is reachable and get basic info."""
    result = {"url": url, "exists": False, "details": {}}
    if not url.startswith("http"):
        url = "https://" + url

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.head(url, timeout=8, follow_redirects=True)
            result["exists"] = resp.status_code < 400
            result["status_code"] = resp.status_code
            result["final_url"] = str(resp.url)
            if result["exists"]:
                resp = await client.get(url, timeout=8, follow_redirects=True)
                title_match = re.search(r'<title[^>]*>(.*?)</title>', resp.text, re.IGNORECASE | re.DOTALL)
                if title_match:
                    result["details"]["title"] = title_match.group(1).strip()[:200]
                text_lower = resp.text.lower()
                result["details"]["has_contact_info"] = any(kw in text_lower for kw in ["contact", "email", "phone", "tel:"])
                result["details"]["has_about_page"] = "about" in text_lower
                result["details"]["has_terms"] = any(kw in text_lower for kw in ["terms", "privacy policy"])
        except Exception as e:
            result["error"] = str(e)
    return result


async def search_social_media(business_name: str) -> dict:
    """Search for social media presence — all platforms in parallel."""
    results = {"business_name": business_name, "platforms_found": []}

    queries = {
        "LinkedIn": f'"{business_name}" site:linkedin.com',
        "Facebook": f'"{business_name}" site:facebook.com',
        "Instagram": f'"{business_name}" site:instagram.com',
        "Twitter/X": f'"{business_name}" site:x.com OR site:twitter.com',
    }

    tasks = {name: _search_ddg(q, max_results=2) for name, q in queries.items()}
    raw_results = await asyncio.gather(*tasks.values(), return_exceptions=True)

    for (name, _), raw in zip(tasks.items(), raw_results):
        if isinstance(raw, Exception):
            raw = []
        platform_results = _parse_results(raw)
        results["platforms_found"].append({
            "platform": name, "found": len(platform_results) > 0, "results": platform_results
        })

    return results
