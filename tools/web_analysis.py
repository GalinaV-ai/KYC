"""
Deep analysis of websites and LinkedIn profiles.
Evaluates reliability signals that indicate a real vs. fabricated business.
"""
import httpx
import re
import json
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse
try:
    from tools.web_search import _search_ddg, _search_ddg_news
except ImportError:
    from web_search import _search_ddg, _search_ddg_news


async def deep_analyze_website(url: str) -> dict:
    """
    Deep analysis of a business website.

    Checks 12 reliability criteria:
    1.  Domain age (via WHOIS/RDAP) — older = more credible
    2.  SSL certificate — valid HTTPS?
    3.  Content volume — how many pages? Is it a one-pager shell?
    4.  Contact information — real address, phone, email?
    5.  About/Team page — do they show real people?
    6.  Social media links — do they link to real profiles?
    7.  Blog/News — has content been updated recently?
    8.  Legal pages — terms, privacy policy, cookie notice?
    9.  Technology stack — cheap template vs. real investment?
    10. Third-party trust signals — reviews widget, payment badges, certifications?
    11. Consistency — does the content match what the customer told us?
    12. Image analysis — stock photos vs. real photos?
    """
    if not url.startswith("http"):
        url = "https://" + url

    parsed = urlparse(url)
    domain = parsed.netloc.replace("www.", "")

    result = {
        "url": url,
        "domain": domain,
        "reachable": False,
        "reliability_score": 0.0,
        "signals": {},
        "red_flags": [],
        "positive_signals": [],
        "summary": ""
    }

    score_points = 0
    max_points = 0

    async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:

        # ─── 1. Basic reachability & SSL ───
        max_points += 10
        try:
            resp = await client.get(url)
            result["reachable"] = resp.status_code < 400
            result["status_code"] = resp.status_code
            result["final_url"] = str(resp.url)
            html = resp.text
            html_lower = html.lower()

            if result["reachable"]:
                score_points += 5

            # SSL check
            if str(resp.url).startswith("https"):
                score_points += 5
                result["signals"]["ssl"] = "valid"
                result["positive_signals"].append("Valid SSL certificate (HTTPS)")
            else:
                result["signals"]["ssl"] = "missing"
                result["red_flags"].append("No SSL certificate — site not on HTTPS")

        except Exception as e:
            result["error"] = str(e)
            result["signals"]["reachability"] = "failed"
            result["red_flags"].append(f"Website unreachable: {str(e)[:100]}")
            result["reliability_score"] = 0.0
            result["summary"] = "Website could not be reached. This is a significant concern if the customer claims to have a website."
            return result

        # ─── 2. Content volume ───
        max_points += 10
        content_length = len(html)
        result["signals"]["content_size_bytes"] = content_length

        # Count internal links (proxy for number of pages)
        internal_links = re.findall(rf'href=["\'](?:https?://(?:www\.)?{re.escape(domain)})?/[^"\']*["\']', html_lower)
        result["signals"]["internal_links_count"] = len(internal_links)

        if content_length > 50000 and len(internal_links) > 10:
            score_points += 10
            result["positive_signals"].append(f"Substantial website ({len(internal_links)} internal links)")
        elif content_length > 10000:
            score_points += 5
            result["signals"]["content_assessment"] = "moderate"
        else:
            result["red_flags"].append(f"Very thin website ({content_length} bytes, {len(internal_links)} links) — possible shell site")

        # ─── 3. Title & meta description ───
        max_points += 5
        title_match = re.search(r'<title[^>]*>(.*?)</title>', html, re.IGNORECASE | re.DOTALL)
        if title_match:
            title = title_match.group(1).strip()
            result["signals"]["title"] = title[:200]
            if len(title) > 10 and title.lower() not in ("home", "website", "welcome", "untitled"):
                score_points += 5
        else:
            result["red_flags"].append("No page title — looks like an unfinished site")

        meta_desc = re.search(r'<meta[^>]*name=["\']description["\'][^>]*content=["\']([^"\']*)', html, re.IGNORECASE)
        if meta_desc:
            result["signals"]["meta_description"] = meta_desc.group(1)[:200]

        # ─── 4. Contact information ───
        max_points += 15
        contacts = {}

        # Phone numbers (UK format)
        phones = re.findall(r'(?:tel:|phone|call)[^0-9]*(\+?44[\s\-]?\d{4}[\s\-]?\d{6}|\(?0\d{4}\)?[\s\-]?\d{6}|\d{11})', html_lower)
        if not phones:
            phones = re.findall(r'(\+44[\s\-]?\d{4}[\s\-]?\d{6}|0\d{2,4}[\s\-]?\d{6,7})', html)
        if phones:
            contacts["phone"] = phones[:3]
            score_points += 5
            result["positive_signals"].append("Phone number found on website")

        # Email
        emails = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', html)
        # Filter out common non-business emails
        biz_emails = [e for e in emails if not any(x in e.lower() for x in
                       ["example.com", "wordpress", "wp-", "schema.org", "sentry", "w3.org"])]
        if biz_emails:
            contacts["email"] = list(set(biz_emails))[:3]
            # Check if email domain matches website domain
            for email in biz_emails:
                email_domain = email.split("@")[1].lower()
                if domain.lower() in email_domain or email_domain in domain.lower():
                    score_points += 5
                    result["positive_signals"].append(f"Email matches website domain ({email_domain})")
                    break
            else:
                score_points += 2
                result["signals"]["email_domain_mismatch"] = True

        # Physical address
        # Look for UK postcodes as address indicator
        postcodes = re.findall(r'[A-Z]{1,2}\d{1,2}\s?\d[A-Z]{2}', html.upper())
        if postcodes:
            contacts["postcodes_found"] = list(set(postcodes))[:3]
            score_points += 5
            result["positive_signals"].append("Physical address with UK postcode found")

        result["signals"]["contact_info"] = contacts
        if not contacts:
            result["red_flags"].append("No contact information found on website")

        # ─── 5. About / Team page ───
        max_points += 10
        has_about = bool(re.search(r'(?:href|id|class)[^>]*(?:about|team|our-team|who-we-are|our-story)', html_lower))
        result["signals"]["has_about_page"] = has_about
        if has_about:
            score_points += 5
            result["positive_signals"].append("About/Team page exists")

        # Check for real people names (look for common patterns)
        people_patterns = re.findall(r'(?:CEO|Director|Founder|Manager|Owner|Partner)[^<]{0,50}', html)
        if people_patterns:
            result["signals"]["team_mentions"] = [p.strip()[:100] for p in people_patterns[:5]]
            score_points += 5
            result["positive_signals"].append("Team members/roles mentioned")

        # ─── 6. Social media links ───
        max_points += 10
        social_platforms = {
            "facebook": r'facebook\.com/[^"\'\s]+',
            "instagram": r'instagram\.com/[^"\'\s]+',
            "twitter": r'(?:twitter|x)\.com/[^"\'\s]+',
            "linkedin": r'linkedin\.com/(?:company|in)/[^"\'\s]+',
            "youtube": r'youtube\.com/[^"\'\s]+',
            "tiktok": r'tiktok\.com/@[^"\'\s]+',
        }
        found_social = {}
        for platform, pattern in social_platforms.items():
            matches = re.findall(pattern, html_lower)
            if matches:
                found_social[platform] = matches[0]

        result["signals"]["social_media"] = found_social
        if len(found_social) >= 2:
            score_points += 10
            result["positive_signals"].append(f"Multiple social media profiles linked ({', '.join(found_social.keys())})")
        elif len(found_social) == 1:
            score_points += 5
        else:
            result["red_flags"].append("No social media links on website")

        # ─── 7. Blog / recent content ───
        max_points += 5
        has_blog = bool(re.search(r'(?:href|id|class)[^>]*(?:blog|news|articles|posts|updates)', html_lower))
        result["signals"]["has_blog"] = has_blog
        if has_blog:
            score_points += 5
            result["positive_signals"].append("Blog/news section exists")

        # Check for recent dates
        current_year = datetime.now().year
        recent_dates = re.findall(rf'(?:{current_year}|{current_year-1})', html)
        result["signals"]["recent_year_mentions"] = len(recent_dates)

        # ─── 8. Legal pages ───
        max_points += 10
        legal_signals = {
            "privacy_policy": bool(re.search(r'privacy\s*policy', html_lower)),
            "terms": bool(re.search(r'terms\s*(?:and|&)?\s*conditions|terms\s*of\s*(?:service|use)', html_lower)),
            "cookie_notice": bool(re.search(r'cookie\s*(?:policy|notice|consent)', html_lower)),
        }
        result["signals"]["legal_pages"] = legal_signals
        legal_count = sum(legal_signals.values())
        if legal_count >= 2:
            score_points += 10
            result["positive_signals"].append("Legal pages present (privacy, terms, cookies)")
        elif legal_count == 1:
            score_points += 5

        # ─── 9. Technology / template detection ───
        max_points += 5
        tech = {}
        if "wordpress" in html_lower or "wp-content" in html_lower:
            tech["cms"] = "WordPress"
        elif "shopify" in html_lower:
            tech["cms"] = "Shopify"
            result["positive_signals"].append("Shopify store — likely real e-commerce")
        elif "wix" in html_lower:
            tech["cms"] = "Wix"
        elif "squarespace" in html_lower:
            tech["cms"] = "Squarespace"

        # Google Analytics / Tag Manager — investment in tracking
        if "google-analytics" in html_lower or "gtag" in html_lower or "googletagmanager" in html_lower:
            tech["analytics"] = "Google Analytics/GTM"
            score_points += 3
            result["positive_signals"].append("Google Analytics installed — indicates active management")

        # Payment processing
        if any(kw in html_lower for kw in ["stripe", "paypal", "square", "worldpay", "braintree"]):
            tech["payments"] = True
            score_points += 2
            result["positive_signals"].append("Payment processing integration found")

        result["signals"]["technology"] = tech

        # ─── 10. Trust signals ───
        max_points += 10
        trust = {}
        trust_keywords = {
            "trustpilot": "Trustpilot",
            "google-review": "Google Reviews",
            "feefo": "Feefo",
            "reviews.io": "Reviews.io",
            "checkatrade": "Checkatrade",
            "trading standards": "Trading Standards",
            "fca": "FCA",
            "ico.org": "ICO registered",
            "companies house": "Companies House reference",
        }
        for kw, label in trust_keywords.items():
            if kw in html_lower:
                trust[label] = True

        result["signals"]["trust_signals"] = trust
        if trust:
            score_points += min(10, len(trust) * 3)
            result["positive_signals"].append(f"Trust signals: {', '.join(trust.keys())}")

        # ─── 11. Image analysis (basic) ───
        max_points += 5
        images = re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', html)
        stock_indicators = sum(1 for img in images if any(
            kw in img.lower() for kw in ["stock", "shutterstock", "istock", "getty",
                                          "unsplash", "pexels", "pixabay", "placeholder"]
        ))
        result["signals"]["total_images"] = len(images)
        result["signals"]["likely_stock_images"] = stock_indicators

        if len(images) > 5 and stock_indicators == 0:
            score_points += 5
            result["positive_signals"].append("Multiple images, none obviously stock")
        elif stock_indicators > 3:
            result["red_flags"].append(f"Multiple stock images detected ({stock_indicators})")

        # ─── 12. Structured data (schema.org) ───
        max_points += 5
        has_schema = "schema.org" in html_lower or "application/ld+json" in html_lower
        result["signals"]["has_structured_data"] = has_schema
        if has_schema:
            score_points += 5
            # Try to extract business info from JSON-LD
            ld_matches = re.findall(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.DOTALL)
            for ld in ld_matches[:3]:
                try:
                    ld_data = json.loads(ld)
                    if isinstance(ld_data, dict):
                        biz_type = ld_data.get("@type", "")
                        if biz_type in ("LocalBusiness", "Organization", "Store", "Restaurant"):
                            result["signals"]["schema_business_type"] = biz_type
                            if ld_data.get("name"):
                                result["signals"]["schema_business_name"] = ld_data["name"]
                            if ld_data.get("address"):
                                result["signals"]["schema_address"] = str(ld_data["address"])[:200]
                except:
                    pass

    # ─── Calculate final score ───
    result["reliability_score"] = round(score_points / max_points, 2) if max_points > 0 else 0
    result["score_details"] = {"points": score_points, "max": max_points}

    # Generate summary
    score = result["reliability_score"]
    if score >= 0.7:
        result["summary"] = f"Website appears legitimate and well-maintained (score: {score:.0%}). " \
                           f"{len(result['positive_signals'])} positive signals found."
    elif score >= 0.4:
        result["summary"] = f"Website exists but has moderate reliability signals (score: {score:.0%}). " \
                           f"Some positive indicators but also some gaps."
    else:
        result["summary"] = f"Website has low reliability signals (score: {score:.0%}). " \
                           f"{len(result['red_flags'])} concerns identified. Manual review recommended."

    return result


async def deep_analyze_linkedin(linkedin_url: str) -> dict:
    """
    Analyze a LinkedIn profile/company page for reliability signals.

    LinkedIn profiles are hard to scrape directly (login wall), so we:
    1. Check if the URL is valid and the page exists (public profile check)
    2. Search the web for cached/indexed information about this profile
    3. Cross-reference with other data we have

    Reliability criteria for PERSONAL profiles:
    - Profile completeness: headline, summary, photo
    - Employment history: does it match what they told us?
    - Duration at current role: consistent with business timeline?
    - Connections count (if visible): very low = possibly fake
    - Activity: do they post, engage?
    - Endorsements/recommendations: external validation
    - Education: does it support their claimed expertise?

    Reliability criteria for COMPANY pages:
    - Page exists and is claimed
    - Employee count listed
    - Posts/updates: active or dormant?
    - Followers: proportional to claimed business size?
    - Description matches what customer told us?
    """
    result = {
        "url": linkedin_url,
        "type": "unknown",  # person or company
        "reachable": False,
        "reliability_score": 0.0,
        "signals": {},
        "red_flags": [],
        "positive_signals": [],
        "web_search_findings": [],
        "summary": ""
    }

    # Determine if it's a personal or company profile
    url_lower = linkedin_url.lower()
    if "/company/" in url_lower:
        result["type"] = "company"
    elif "/in/" in url_lower:
        result["type"] = "person"

    score_points = 0
    max_points = 0

    async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:

        # ─── 1. Check if profile is reachable ───
        max_points += 10
        try:
            resp = await client.get(linkedin_url, headers={
                "User-Agent": "Mozilla/5.0 (compatible; research bot)"
            })
            result["reachable"] = resp.status_code < 400
            result["status_code"] = resp.status_code

            if result["reachable"]:
                score_points += 5
                html = resp.text
                html_lower = html.lower()

                # LinkedIn public profiles have some visible data even without login
                # Extract what we can from the HTML

                # Title contains name/company
                title_match = re.search(r'<title[^>]*>(.*?)</title>', html, re.IGNORECASE | re.DOTALL)
                if title_match:
                    title = title_match.group(1).strip()
                    result["signals"]["page_title"] = title[:200]
                    if "linkedin" in title.lower() and len(title) > 20:
                        score_points += 5
                        result["positive_signals"].append(f"LinkedIn profile exists: {title[:100]}")

                # Check for profile indicators
                if result["type"] == "person":
                    # Look for employment/experience keywords
                    if any(kw in html_lower for kw in ["experience", "employment", "position"]):
                        result["signals"]["has_experience_section"] = True
                        result["positive_signals"].append("Profile has experience/employment history")

                    if any(kw in html_lower for kw in ["education", "university", "college", "degree"]):
                        result["signals"]["has_education"] = True
                        result["positive_signals"].append("Profile has education section")

                    if "recommendation" in html_lower or "endorsed" in html_lower:
                        result["signals"]["has_recommendations"] = True
                        result["positive_signals"].append("Profile has recommendations/endorsements")

                elif result["type"] == "company":
                    # Company page signals
                    if any(kw in html_lower for kw in ["employee", "staff", "team member"]):
                        result["signals"]["mentions_employees"] = True

                    if any(kw in html_lower for kw in ["follower"]):
                        result["signals"]["has_followers"] = True
                        result["positive_signals"].append("Company page has followers")

            else:
                result["red_flags"].append(f"LinkedIn profile returned status {resp.status_code}")

        except Exception as e:
            result["signals"]["access_error"] = str(e)[:200]

        # ─── 2. Real web search for LinkedIn data (ALL IN PARALLEL) ───
        max_points += 20

        path_parts = urlparse(linkedin_url).path.strip("/").split("/")
        search_name = path_parts[-1].replace("-", " ") if path_parts else ""

        # ─── 3. Cross-reference search ───
        max_points += 10

        # Run all searches in parallel
        search_tasks = [
            _search_ddg(f'site:linkedin.com "{search_name}"', max_results=3),
            _search_ddg(f'"{search_name}" linkedin profile', max_results=3),
        ]
        if search_name:
            search_tasks.extend([
                _search_ddg(f'"{search_name}" director OR founder OR owner business UK', max_results=3),
                _search_ddg(f'"{search_name}" site:find-and-update.company-information.service.gov.uk', max_results=3),
                _search_ddg_news(f'"{search_name}"', max_results=3),
            ])

        import asyncio
        all_search = await asyncio.gather(*search_tasks, return_exceptions=True)

        # Parse LinkedIn results
        for i in range(2):
            raw = all_search[i] if i < len(all_search) and not isinstance(all_search[i], Exception) else []
            for r in raw:
                if "error" not in r:
                    result["web_search_findings"].append({
                        "title": r.get("title", ""), "snippet": r.get("body", "")[:300],
                        "url": r.get("href", "")
                    })
                    score_points += 2

        # Parse business/CH/news results
        if search_name and len(all_search) > 2:
            biz_raw = all_search[2] if not isinstance(all_search[2], Exception) else []
            for r in biz_raw:
                if "error" not in r:
                    score_points += 2
                    result["positive_signals"].append("Person appears in business-related web search results")
                    result["web_search_findings"].append({
                        "context": "business_association", "title": r.get("title", ""),
                        "snippet": r.get("body", "")[:200], "url": r.get("href", "")
                    })

            ch_raw = all_search[3] if len(all_search) > 3 and not isinstance(all_search[3], Exception) else []
            for r in ch_raw:
                if "error" not in r:
                    score_points += 3
                    result["positive_signals"].append("Person found in Companies House records")
                    result["web_search_findings"].append({
                        "context": "companies_house", "title": r.get("title", ""),
                        "snippet": r.get("body", "")[:200], "url": r.get("href", "")
                    })

            news_raw = all_search[4] if len(all_search) > 4 and not isinstance(all_search[4], Exception) else []
            for r in news_raw:
                if "error" not in r:
                    score_points += 2
                    result["web_search_findings"].append({
                        "context": "news", "title": r.get("title", ""),
                        "snippet": r.get("body", "")[:200],
                        "url": r.get("url", ""), "date": r.get("date", "")
                    })

    # ─── Profile age heuristics ───
    max_points += 10
    if result.get("web_search_findings"):
        # If there are search results, the profile has been around long enough to be indexed
        score_points += 10
        result["positive_signals"].append("Profile is indexed in search engines (not brand new)")
    else:
        result["signals"]["profile_indexing"] = "not_found_in_search"
        result["red_flags"].append("LinkedIn profile not found in search engines — may be very new or private")

    # ─── Scoring ───
    result["reliability_score"] = round(score_points / max_points, 2) if max_points > 0 else 0
    result["score_details"] = {"points": score_points, "max": max_points}

    score = result["reliability_score"]
    if result["type"] == "person":
        if score >= 0.6:
            result["summary"] = f"LinkedIn personal profile shows reasonable authenticity signals (score: {score:.0%}). " \
                               f"Profile exists and has web presence."
        elif score >= 0.3:
            result["summary"] = f"LinkedIn profile exists but limited public information available (score: {score:.0%}). " \
                               f"Consider asking the customer for more details about their professional background."
        else:
            result["summary"] = f"LinkedIn profile has weak signals (score: {score:.0%}). " \
                               f"Profile may be new, private, or fabricated."
    else:
        if score >= 0.6:
            result["summary"] = f"LinkedIn company page appears established (score: {score:.0%})."
        else:
            result["summary"] = f"LinkedIn company page has limited presence (score: {score:.0%}). " \
                               f"This is common for very small businesses but worth noting."

    return result


# ─── Website Liveness Analysis ───


async def analyze_website_liveness(url: str) -> dict:
    """
    Deep "liveness" analysis of a website — goes beyond basic reachability.

    Checks:
    1. Domain age via Wayback Machine (Internet Archive)
    2. Number of indexed pages (via search engine)
    3. Content freshness (dates on the page)
    4. External reviews (Trustpilot, Google)
    5. App store presence (if applicable)

    Returns a liveness_score (0-1) and structured findings.
    """
    if not url.startswith("http"):
        url = "https://" + url

    parsed = urlparse(url)
    domain = parsed.netloc.replace("www.", "")

    result = {
        "url": url,
        "domain": domain,
        "liveness_score": 0.0,
        "domain_age": None,
        "indexed_pages": None,
        "content_freshness": None,
        "reviews": {},
        "app_store": {},
        "signals": [],
        "red_flags": [],
        "summary": ""
    }

    score_points = 0
    max_points = 0

    import asyncio

    # Run all checks in parallel
    tasks = {
        "wayback": _check_wayback_machine(domain),
        "indexation": _check_search_indexation(domain),
        "reviews": _check_external_reviews(domain),
        "app_store": _check_app_store_presence(domain),
    }
    all_results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    checks = {}
    for key, res in zip(tasks.keys(), all_results):
        checks[key] = res if not isinstance(res, Exception) else {}

    # ── 1. Domain age (Wayback Machine) ──
    max_points += 20
    wb = checks.get("wayback", {})
    if wb.get("first_snapshot"):
        result["domain_age"] = wb
        first = wb["first_snapshot"]
        total_snapshots = wb.get("total_snapshots", 0)

        try:
            first_year = int(first[:4])
            current_year = datetime.now().year
            age = current_year - first_year

            if age >= 5:
                score_points += 20
                result["signals"].append(f"Domain first seen {age} years ago ({first[:10]}) — well-established")
            elif age >= 2:
                score_points += 15
                result["signals"].append(f"Domain first seen {age} years ago ({first[:10]})")
            elif age >= 1:
                score_points += 8
                result["signals"].append(f"Domain first seen ~{age} year ago ({first[:10]})")
            else:
                score_points += 3
                result["red_flags"].append(f"Domain very new — first seen {first[:10]}")
        except (ValueError, IndexError):
            pass

        if total_snapshots > 100:
            score_points += 5
            result["signals"].append(f"{total_snapshots} Wayback Machine snapshots — actively maintained")
        elif total_snapshots > 10:
            score_points += 2
            result["signals"].append(f"{total_snapshots} Wayback Machine snapshots")
        elif total_snapshots <= 3:
            result["red_flags"].append(f"Only {total_snapshots} Wayback snapshots — minimal web history")
    else:
        result["red_flags"].append("Not found in Wayback Machine — domain may be very new or obscure")

    # ── 2. Search indexation ──
    max_points += 20
    idx = checks.get("indexation", {})
    indexed_count = idx.get("indexed_pages", 0)
    result["indexed_pages"] = idx

    if indexed_count >= 50:
        score_points += 20
        result["signals"].append(f"{indexed_count}+ pages indexed by search engines — substantial site")
    elif indexed_count >= 10:
        score_points += 12
        result["signals"].append(f"{indexed_count} pages indexed")
    elif indexed_count >= 3:
        score_points += 5
        result["signals"].append(f"Only {indexed_count} pages indexed — small or new site")
    elif indexed_count == 0:
        result["red_flags"].append("Zero pages indexed — site may be brand new, blocked, or fake")

    if idx.get("brand_mentions", 0) > 0:
        result["signals"].append(f"{idx['brand_mentions']} third-party mentions of the brand found")
        score_points += min(5, idx["brand_mentions"])

    # ── 3. External reviews ──
    max_points += 20
    rev = checks.get("reviews", {})
    result["reviews"] = rev

    trustpilot = rev.get("trustpilot", {})
    google_reviews = rev.get("google", {})

    if trustpilot.get("found"):
        score_points += 10
        result["signals"].append(f"Trustpilot listing found: {trustpilot.get('snippet', '')[:80]}")
    if google_reviews.get("found"):
        score_points += 10
        result["signals"].append(f"Google reviews found: {google_reviews.get('snippet', '')[:80]}")

    if not trustpilot.get("found") and not google_reviews.get("found"):
        result["signals"].append("No external reviews found (common for small/new businesses)")

    # ── 4. App store ──
    max_points += 10
    app = checks.get("app_store", {})
    result["app_store"] = app

    if app.get("ios_found") or app.get("android_found"):
        score_points += 10
        platforms = []
        if app.get("ios_found"):
            platforms.append("iOS")
        if app.get("android_found"):
            platforms.append("Android")
        result["signals"].append(f"App found on {', '.join(platforms)} — real product investment")

    # ── 5. Content freshness ──
    max_points += 10
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=12) as client:
            resp = await client.get(url)
            if resp.status_code < 400:
                html = resp.text
                current_year = datetime.now().year

                recent_year_count = len(re.findall(str(current_year), html))
                last_year_count = len(re.findall(str(current_year - 1), html))

                result["content_freshness"] = {
                    "current_year_mentions": recent_year_count,
                    "last_year_mentions": last_year_count,
                }

                if recent_year_count >= 3:
                    score_points += 10
                    result["signals"].append(f"Content references {current_year} ({recent_year_count} times) — actively updated")
                elif last_year_count >= 3:
                    score_points += 5
                    result["signals"].append("Content references last year — reasonably fresh")
                elif recent_year_count == 0 and last_year_count == 0:
                    old_years = sum(len(re.findall(str(y), html)) for y in range(current_year - 5, current_year - 1))
                    if old_years > 5:
                        result["red_flags"].append("No recent dates found — content may be stale")
    except Exception:
        pass

    # ── Final score ──
    result["liveness_score"] = round(score_points / max_points, 2) if max_points > 0 else 0

    score = result["liveness_score"]
    if score >= 0.7:
        result["summary"] = f"Website shows strong liveness signals (score: {score:.0%}). " \
                           f"Domain has history, content is indexed, and external presence detected."
    elif score >= 0.4:
        result["summary"] = f"Website has moderate liveness signals (score: {score:.0%}). " \
                           f"Some indicators present but gaps exist."
    else:
        result["summary"] = f"Website has weak liveness signals (score: {score:.0%}). " \
                           f"{len(result['red_flags'])} concerns. May be new, unused, or fabricated."

    return result


async def _check_wayback_machine(domain: str) -> dict:
    """Check Internet Archive for domain history — free, no key."""
    result = {"first_snapshot": None, "last_snapshot": None, "total_snapshots": 0}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # CDX API — returns snapshot timestamps
            resp = await client.get(
                "https://web.archive.org/cdx/search/cdx",
                params={
                    "url": domain,
                    "output": "json",
                    "fl": "timestamp",
                    "limit": 5,
                    "collapse": "timestamp:6",
                }
            )
            if resp.status_code == 200:
                data = resp.json()
                if len(data) > 1:
                    timestamps = [row[0] for row in data[1:]]
                    result["first_snapshot"] = timestamps[0]
                    result["last_snapshot"] = timestamps[-1]

            # Get total count
            resp2 = await client.get(
                "https://web.archive.org/cdx/search/cdx",
                params={
                    "url": domain,
                    "output": "json",
                    "fl": "timestamp",
                    "limit": 1,
                    "showNumPages": "true",
                }
            )
            if resp2.status_code == 200:
                try:
                    result["total_snapshots"] = int(resp2.text.strip())
                except ValueError:
                    pass
    except Exception:
        pass
    return result


async def _check_search_indexation(domain: str) -> dict:
    """Check how many pages are indexed via search."""
    result = {"indexed_pages": 0, "brand_mentions": 0}
    try:
        site_results = await _search_ddg(f"site:{domain}", max_results=10)
        valid = [r for r in site_results if "error" not in r]
        result["indexed_pages"] = len(valid)

        brand = domain.split(".")[0]
        if len(brand) > 3:
            mention_results = await _search_ddg(f'"{brand}" -{domain}', max_results=5)
            valid_mentions = [r for r in mention_results if "error" not in r
                            and domain not in r.get("href", "")]
            result["brand_mentions"] = len(valid_mentions)
    except Exception:
        pass
    return result


async def _check_external_reviews(domain: str) -> dict:
    """Check for reviews on Trustpilot and Google."""
    result = {"trustpilot": {"found": False}, "google": {"found": False}}
    try:
        import asyncio
        tp_task = _search_ddg(f"site:trustpilot.com {domain}", max_results=2)
        gr_task = _search_ddg(f'"{domain}" reviews OR review', max_results=3)
        tp_raw, gr_raw = await asyncio.gather(tp_task, gr_task, return_exceptions=True)

        if not isinstance(tp_raw, Exception):
            tp_valid = [r for r in tp_raw if "error" not in r and "trustpilot" in r.get("href", "")]
            if tp_valid:
                result["trustpilot"] = {
                    "found": True,
                    "url": tp_valid[0].get("href", ""),
                    "snippet": tp_valid[0].get("body", "")[:150],
                }

        if not isinstance(gr_raw, Exception):
            gr_valid = [r for r in gr_raw if "error" not in r and "review" in r.get("body", "").lower()]
            if gr_valid:
                result["google"] = {
                    "found": True,
                    "snippet": gr_valid[0].get("body", "")[:150],
                    "url": gr_valid[0].get("href", ""),
                }
    except Exception:
        pass
    return result


async def _check_app_store_presence(domain: str) -> dict:
    """Check if the brand has apps on iOS/Android stores."""
    result = {"ios_found": False, "android_found": False}
    brand = domain.split(".")[0]
    if len(brand) < 4:
        return result
    try:
        import asyncio
        ios_task = _search_ddg(f'site:apps.apple.com "{brand}"', max_results=2)
        and_task = _search_ddg(f'site:play.google.com "{brand}"', max_results=2)
        ios_raw, and_raw = await asyncio.gather(ios_task, and_task, return_exceptions=True)

        if not isinstance(ios_raw, Exception):
            ios_valid = [r for r in ios_raw if "error" not in r and "apps.apple.com" in r.get("href", "")]
            if ios_valid:
                result["ios_found"] = True
                result["ios_url"] = ios_valid[0].get("href", "")
                result["ios_title"] = ios_valid[0].get("title", "")

        if not isinstance(and_raw, Exception):
            and_valid = [r for r in and_raw if "error" not in r and "play.google.com" in r.get("href", "")]
            if and_valid:
                result["android_found"] = True
                result["android_url"] = and_valid[0].get("href", "")
                result["android_title"] = and_valid[0].get("title", "")
    except Exception:
        pass
    return result


# ─── Enhanced LinkedIn Analysis ───


async def analyze_linkedin_depth(search_name: str, business_name: str = "") -> dict:
    """
    Deep LinkedIn analysis using search snippets.

    Extracts from DuckDuckGo snippets of LinkedIn pages:
    - Connection count (e.g. "500+ connections")
    - Headline / current title
    - Location
    - Activity level (posts, articles)
    - Profile completeness signals
    - Company page: followers, employee count
    """
    result = {
        "person": search_name,
        "business": business_name,
        "connections": None,
        "headline": None,
        "location": None,
        "current_role": None,
        "activity_signals": [],
        "profile_completeness": "unknown",
        "company_page": {},
        "reliability_score": 0.0,
        "signals": [],
        "red_flags": [],
        "raw_snippets": [],
        "summary": ""
    }

    import asyncio
    score_points = 0
    max_points = 0

    # Run targeted searches in parallel
    queries = {}
    queries["profile"] = _search_ddg(
        f'site:linkedin.com/in "{search_name}"', max_results=5
    )
    queries["activity"] = _search_ddg(
        f'site:linkedin.com "{search_name}" posted OR shared OR published', max_results=3
    )
    if business_name:
        queries["company_page"] = _search_ddg(
            f'site:linkedin.com/company "{business_name}"', max_results=3
        )
        queries["person_company"] = _search_ddg(
            f'site:linkedin.com "{search_name}" "{business_name}"', max_results=3
        )

    all_raw = await asyncio.gather(*queries.values(), return_exceptions=True)
    search_data = {}
    for key, raw in zip(queries.keys(), all_raw):
        search_data[key] = raw if not isinstance(raw, Exception) else []

    # ── Parse profile snippets ──
    max_points += 30
    profile_results = [r for r in search_data.get("profile", [])
                       if "error" not in r and "linkedin.com/in/" in r.get("href", "")]

    if profile_results:
        score_points += 10
        result["signals"].append("LinkedIn personal profile found")

        for r in profile_results[:3]:
            title = r.get("title", "")
            snippet = r.get("body", "")
            combined = f"{title} {snippet}"
            result["raw_snippets"].append({"title": title, "snippet": snippet[:200]})

            # Extract headline (usually: "Name - Headline | LinkedIn")
            title_parts = title.split(" - ", 1)
            if len(title_parts) > 1:
                headline = title_parts[1].replace(" | LinkedIn", "").strip()
                if headline and not result["headline"]:
                    result["headline"] = headline
                    score_points += 5
                    result["signals"].append(f"Headline: {headline[:80]}")

            # Extract connections count
            conn_match = re.search(r'(\d+)\+?\s*(?:connections?|контакт|связ)', combined, re.IGNORECASE)
            if conn_match and not result["connections"]:
                count = int(conn_match.group(1))
                result["connections"] = count
                if count >= 500:
                    score_points += 10
                    result["signals"].append(f"{count}+ connections — well-networked")
                elif count >= 100:
                    score_points += 5
                    result["signals"].append(f"{count}+ connections")
                elif count < 50:
                    result["red_flags"].append(f"Only {count} connections — possibly new or inactive")

            # Extract location
            loc_match = re.search(
                r'(?:location|based in|from)\s*[:\-]?\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*(?:,\s*[A-Z][a-z]+)*)',
                combined
            )
            if loc_match and not result["location"]:
                result["location"] = loc_match.group(1)

            # Extract current role
            role_match = re.search(
                r'(?:CEO|CTO|CFO|COO|Director|Founder|Co-founder|Owner|Manager|Partner|Head of)\s+(?:at|of|@)\s+([^\|,\-]+)',
                combined, re.IGNORECASE
            )
            if role_match and not result["current_role"]:
                result["current_role"] = role_match.group(0).strip()[:100]
                score_points += 5
    else:
        result["red_flags"].append("No LinkedIn personal profile found")

    # ── Parse activity ──
    max_points += 15
    activity_results = [r for r in search_data.get("activity", [])
                        if "error" not in r and "linkedin.com" in r.get("href", "")]

    if activity_results:
        score_points += 10
        result["activity_signals"].append(f"{len(activity_results)} posts/shares found")
        result["signals"].append("Active on LinkedIn (posts/shares found)")

        for r in activity_results[:3]:
            snippet = r.get("body", "")
            if any(kw in snippet.lower() for kw in ["2026", "2025", "week", "day", "month", "hour"]):
                score_points += 5
                result["signals"].append("Recent LinkedIn activity detected")
                break
    else:
        result["signals"].append("No public LinkedIn posts found")

    # ── Parse company page ──
    max_points += 15
    if business_name:
        company_results = [r for r in search_data.get("company_page", [])
                           if "error" not in r and "linkedin.com/company" in r.get("href", "")]
        if company_results:
            score_points += 5
            result["company_page"]["found"] = True
            for r in company_results[:2]:
                title = r.get("title", "")
                snippet = r.get("body", "")
                combined = f"{title} {snippet}"

                foll_match = re.search(r'(\d[\d,]*)\s*(?:followers?|подписчик)', combined, re.IGNORECASE)
                if foll_match:
                    try:
                        result["company_page"]["followers"] = int(foll_match.group(1).replace(",", ""))
                        score_points += 5
                    except ValueError:
                        pass

                emp_match = re.search(r'(\d[\d,]*)\s*(?:employees?|сотрудник|people|associated members)', combined, re.IGNORECASE)
                if emp_match:
                    try:
                        result["company_page"]["employees"] = int(emp_match.group(1).replace(",", ""))
                        score_points += 5
                    except ValueError:
                        pass

                result["company_page"]["snippet"] = snippet[:150]
        else:
            result["company_page"]["found"] = False
            result["signals"].append(f"No LinkedIn company page for '{business_name}'")

    # ── Person+Company cross-reference ──
    max_points += 10
    if business_name:
        cross_results = [r for r in search_data.get("person_company", []) if "error" not in r]
        if cross_results:
            score_points += 10
            result["signals"].append("Person and company appear together on LinkedIn — confirms association")
        else:
            result["red_flags"].append(f"'{search_name}' and '{business_name}' not found together on LinkedIn")

    # ── Profile completeness ──
    completeness_score = sum([
        bool(result["headline"]),
        bool(result["connections"] and result["connections"] >= 50),
        bool(result["current_role"]),
        bool(result["activity_signals"]),
    ])
    result["profile_completeness"] = (
        "strong" if completeness_score >= 3 else
        "moderate" if completeness_score >= 2 else
        "weak" if completeness_score >= 1 else "not_found"
    )

    # ── Final score ──
    result["reliability_score"] = round(score_points / max_points, 2) if max_points > 0 else 0
    score = result["reliability_score"]

    if score >= 0.6:
        result["summary"] = f"LinkedIn presence is solid (score: {score:.0%}). " \
                           f"Profile {result['profile_completeness']}, " \
                           f"{result.get('connections', '?')} connections."
    elif score >= 0.3:
        result["summary"] = f"LinkedIn presence is limited (score: {score:.0%}). " \
                           f"Profile exists but signals are weak."
    else:
        result["summary"] = f"LinkedIn presence is very weak or absent (score: {score:.0%}). " \
                           f"Warrants further verification."

    return result
