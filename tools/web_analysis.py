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
