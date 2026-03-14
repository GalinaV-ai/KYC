"""
Digital footprint verification tools.
Email MX validation, Wayback Machine history, social media presence,
DNS/IP geolocation, SSL certificate details, app store presence,
GitHub presence, phone number analysis.
"""
import httpx
import re
import json
import asyncio
import socket
from typing import Optional
from datetime import datetime, timezone
from urllib.parse import urlparse

try:
    from tools.web_search import _search_ddg
except ImportError:
    from web_search import _search_ddg


# ─────────────────────────────────────────────
# C1. Email Domain MX Record Check
# ─────────────────────────────────────────────

async def check_email_domain(email: str) -> dict:
    """
    Validate an email domain's MX records and categorize the email type.
    - Corporate domain (company.com) with valid MX = positive signal
    - Free provider (gmail, yahoo, outlook) = neutral for small business, flag for large claims
    - Domain with no MX = suspicious
    - Domain matches claimed business website = positive signal
    """
    result = {
        "source": "email_domain_check",
        "email": email,
        "valid_mx": False,
        "email_type": "unknown",
        "details": {}
    }

    # Extract domain
    if "@" in email:
        domain = email.split("@")[1].lower().strip()
    else:
        domain = email.lower().strip()

    result["details"]["domain"] = domain

    # Known free email providers
    free_providers = {
        "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk",
        "hotmail.com", "hotmail.co.uk", "outlook.com", "live.com",
        "aol.com", "icloud.com", "me.com", "protonmail.com",
        "proton.me", "mail.com", "zoho.com", "yandex.com",
        "gmx.com", "gmx.co.uk", "tutanota.com", "fastmail.com",
    }

    # Known disposable email domains (red flag)
    disposable_providers = {
        "guerrillamail.com", "mailinator.com", "tempmail.com",
        "throwaway.email", "10minutemail.com", "temp-mail.org",
        "yopmail.com", "sharklasers.com", "guerrillamailblock.com",
        "grr.la", "dispostable.com", "maildrop.cc",
    }

    if domain in free_providers:
        result["email_type"] = "free_provider"
        result["valid_mx"] = True  # We know these work
        result["details"]["provider"] = domain
        result["details"]["note"] = "Free email provider. Normal for sole traders, unusual for established companies."
        return result

    if domain in disposable_providers:
        result["email_type"] = "disposable"
        result["details"]["note"] = "Disposable/temporary email address. Strong red flag."
        return result

    # Check MX records via DNS lookup
    loop = asyncio.get_event_loop()
    try:
        mx_records = await loop.run_in_executor(
            None,
            lambda: socket.getaddrinfo(domain, 25, socket.AF_INET, socket.SOCK_STREAM)
        )
        if mx_records:
            result["valid_mx"] = True
            result["email_type"] = "corporate"
    except (socket.gaierror, socket.herror, OSError):
        result["valid_mx"] = False
        result["email_type"] = "invalid_domain"
        result["details"]["note"] = "Domain has no mail server (MX record). Email address may be fake."

    # Try DNS over HTTPS for better MX check
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(
                "https://dns.google/resolve",
                params={"name": domain, "type": "MX"}
            )
            if resp.status_code == 200:
                dns_data = resp.json()
                answers = dns_data.get("Answer", [])
                mx_hosts = []
                for ans in answers:
                    if ans.get("type") == 15:  # MX record type
                        mx_hosts.append(ans.get("data", ""))
                if mx_hosts:
                    result["valid_mx"] = True
                    result["email_type"] = "corporate"
                    result["details"]["mx_records"] = mx_hosts[:5]

                    # Check if using Google Workspace, Microsoft 365, etc.
                    mx_str = " ".join(mx_hosts).lower()
                    if "google" in mx_str or "gmail" in mx_str:
                        result["details"]["email_provider"] = "Google Workspace"
                    elif "outlook" in mx_str or "microsoft" in mx_str:
                        result["details"]["email_provider"] = "Microsoft 365"
                    elif "zoho" in mx_str:
                        result["details"]["email_provider"] = "Zoho"
                    elif "protonmail" in mx_str:
                        result["details"]["email_provider"] = "ProtonMail Business"
                elif not answers:
                    result["valid_mx"] = False
                    result["details"]["note"] = "No MX records found for this domain."
        except Exception:
            pass

    return result


# ─────────────────────────────────────────────
# C2. Wayback Machine (Internet Archive)
# ─────────────────────────────────────────────

async def check_wayback_machine(url: str) -> dict:
    """
    Check the Wayback Machine for historical snapshots of a website.
    If someone claims 5 years of trading but site appeared 2 months ago = suspicious.
    Free API: https://web.archive.org/cdx/search/cdx
    """
    result = {
        "source": "wayback_machine",
        "url": url,
        "found": False,
        "details": {}
    }

    # Clean URL to domain
    if not url.startswith("http"):
        url = "https://" + url
    parsed = urlparse(url)
    domain = parsed.netloc.replace("www.", "")

    async with httpx.AsyncClient(timeout=20) as client:
        try:
            # CDX API — returns all snapshots
            resp = await client.get(
                "https://web.archive.org/cdx/search/cdx",
                params={
                    "url": domain,
                    "output": "json",
                    "limit": 100,
                    "fl": "timestamp,statuscode,mimetype",
                    "filter": "statuscode:200",
                    "collapse": "timestamp:6",  # One per month
                },
                follow_redirects=True
            )
            if resp.status_code == 200:
                data = resp.json()
                if len(data) > 1:  # First row is headers
                    snapshots = data[1:]  # Skip header row
                    result["found"] = True
                    result["details"]["total_snapshots"] = len(snapshots)

                    # First and last snapshot dates
                    timestamps = [s[0] for s in snapshots if len(s) > 0]
                    if timestamps:
                        first_ts = timestamps[0]
                        last_ts = timestamps[-1]

                        # Parse timestamp (format: 20200315120000)
                        try:
                            first_date = datetime.strptime(first_ts[:8], "%Y%m%d")
                            last_date = datetime.strptime(last_ts[:8], "%Y%m%d")
                            result["details"]["first_snapshot"] = first_date.strftime("%Y-%m-%d")
                            result["details"]["last_snapshot"] = last_date.strftime("%Y-%m-%d")

                            # Calculate age
                            age_days = (datetime.now() - first_date).days
                            result["details"]["web_presence_days"] = age_days
                            result["details"]["web_presence_years"] = round(age_days / 365.25, 1)

                            # Activity pattern
                            snapshots_per_year = len(snapshots) / max(1, age_days / 365.25)
                            result["details"]["snapshots_per_year"] = round(snapshots_per_year, 1)

                            if snapshots_per_year > 12:
                                result["details"]["activity_level"] = "very_active"
                            elif snapshots_per_year > 4:
                                result["details"]["activity_level"] = "active"
                            elif snapshots_per_year > 1:
                                result["details"]["activity_level"] = "moderate"
                            else:
                                result["details"]["activity_level"] = "sparse"
                        except ValueError:
                            pass
                else:
                    result["details"]["note"] = "No snapshots found in Wayback Machine"
        except Exception as e:
            result["error"] = str(e)

    return result


# ─────────────────────────────────────────────
# C3. Social Media Presence Verification
# ─────────────────────────────────────────────

async def check_social_media_presence(
    business_name: str,
    person_name: str = "",
    known_website: str = ""
) -> dict:
    """
    Check social media presence across major platforms.
    A real business usually has at least 1-2 social media profiles.
    Zero social presence for a business claiming significant revenue = flag.
    """
    result = {
        "source": "social_media_presence",
        "business_name": business_name,
        "platforms_found": [],
        "platforms_checked": [],
        "details": {}
    }

    platforms = {
        "facebook": f'site:facebook.com "{business_name}"',
        "instagram": f'site:instagram.com "{business_name}"',
        "twitter_x": f'site:twitter.com OR site:x.com "{business_name}"',
        "youtube": f'site:youtube.com "{business_name}"',
        "tiktok": f'site:tiktok.com "{business_name}"',
    }

    # Add person-specific searches if provided
    if person_name:
        platforms["linkedin_person"] = f'site:linkedin.com/in/ "{person_name}"'

    for platform, query in platforms.items():
        result["platforms_checked"].append(platform)
        try:
            results = await _search_ddg(query, max_results=3)
            for r in results:
                if not isinstance(r, dict) or "error" in r:
                    continue
                href = r.get("href", "").lower()
                # Verify it's actually from the target platform
                platform_domains = {
                    "facebook": "facebook.com",
                    "instagram": "instagram.com",
                    "twitter_x": ("twitter.com", "x.com"),
                    "youtube": "youtube.com",
                    "tiktok": "tiktok.com",
                    "linkedin_person": "linkedin.com/in/",
                }
                domain_check = platform_domains.get(platform, "")
                if isinstance(domain_check, tuple):
                    found = any(d in href for d in domain_check)
                else:
                    found = domain_check in href

                if found:
                    # Filter out platform homepages (not actual company profiles)
                    homepage_patterns = [
                        "instagram.com/$", "instagram.com/accounts",
                        "facebook.com/$", "facebook.com/login",
                        "twitter.com/$", "x.com/$", "x.com/X",
                        "youtube.com/$", "youtube.com/feed",
                        "tiktok.com/$", "tiktok.com/explore",
                        "linkedin.com/$", "linkedin.com/feed",
                    ]
                    import re as _re
                    is_homepage = any(_re.search(pat, href) for pat in homepage_patterns)
                    # Also check if the business name appears in the title/snippet
                    text_lower = f"{r.get('title', '')} {r.get('body', '')}".lower()
                    name_lower = business_name.lower()
                    name_in_result = name_lower in text_lower
                    if is_homepage or not name_in_result:
                        continue  # Skip generic platform pages

                    result["platforms_found"].append(platform)
                    result["details"][platform] = {
                        "url": r.get("href", ""),
                        "title": r.get("title", ""),
                        "snippet": r.get("body", "")[:200],
                    }
                    break
        except Exception:
            continue

    # Summary signal
    found_count = len(result["platforms_found"])
    if found_count >= 3:
        result["details"]["signal"] = "strong_social_presence"
    elif found_count >= 1:
        result["details"]["signal"] = "some_social_presence"
    else:
        result["details"]["signal"] = "no_social_presence"

    return result


# ─────────────────────────────────────────────
# C4. App Store Check
# ─────────────────────────────────────────────

async def check_app_store_presence(app_name: str, company_name: str = "") -> dict:
    """
    Check Apple App Store and Google Play for app presence.
    For tech/fintech companies claiming to have mobile apps.
    """
    result = {
        "source": "app_store_check",
        "app_name": app_name,
        "found_apple": False,
        "found_google": False,
        "details": {}
    }

    search_name = app_name or company_name

    # Apple App Store
    try:
        results = await _search_ddg(
            f'site:apps.apple.com "{search_name}"',
            max_results=3
        )
        for r in results:
            if isinstance(r, dict) and "apps.apple.com" in r.get("href", ""):
                result["found_apple"] = True
                result["details"]["apple"] = {
                    "url": r["href"],
                    "title": r.get("title", ""),
                }
                break
    except Exception:
        pass

    # Google Play Store
    try:
        results = await _search_ddg(
            f'site:play.google.com "{search_name}"',
            max_results=3
        )
        for r in results:
            if isinstance(r, dict) and "play.google.com" in r.get("href", ""):
                result["found_google"] = True
                result["details"]["google"] = {
                    "url": r["href"],
                    "title": r.get("title", ""),
                }
                break
    except Exception:
        pass

    return result


# ─────────────────────────────────────────────
# C5. GitHub / Code Repository Check
# ─────────────────────────────────────────────

async def check_github_presence(company_name: str) -> dict:
    """
    Check GitHub for company/organisation presence.
    For tech companies claiming software development capabilities.
    """
    result = {
        "source": "github_check",
        "company_name": company_name,
        "found": False,
        "details": {}
    }

    try:
        # Search GitHub org/user pages
        results = await _search_ddg(
            f'site:github.com "{company_name}"',
            max_results=5
        )
        github_urls = []
        for r in results:
            if isinstance(r, dict) and "github.com" in r.get("href", ""):
                github_urls.append({
                    "url": r["href"],
                    "title": r.get("title", ""),
                    "snippet": r.get("body", "")[:200],
                })

        if github_urls:
            result["found"] = True
            result["details"]["repositories"] = github_urls[:5]

            # Check if it's an org page vs just mentions
            for url_info in github_urls:
                url = url_info["url"]
                # github.com/orgname pattern (not github.com/user/repo/... path)
                path_parts = urlparse(url).path.strip("/").split("/")
                if len(path_parts) == 1:
                    result["details"]["has_org_page"] = True
                    result["details"]["org_url"] = url
                    break
    except Exception as e:
        result["error"] = str(e)

    return result


# ─────────────────────────────────────────────
# C6. DNS / IP Geolocation
# ─────────────────────────────────────────────

async def check_dns_geolocation(domain: str) -> dict:
    """
    Check DNS records and IP geolocation for a domain.
    A 'London-based' company hosted in a high-risk jurisdiction could be a signal.
    """
    result = {
        "source": "dns_geolocation",
        "domain": domain,
        "resolved": False,
        "details": {}
    }

    # Clean domain
    if "://" in domain:
        domain = urlparse(domain).netloc
    domain = domain.replace("www.", "").split("/")[0]

    async with httpx.AsyncClient(timeout=10) as client:
        # Resolve A record
        try:
            dns_resp = await client.get(
                "https://dns.google/resolve",
                params={"name": domain, "type": "A"}
            )
            if dns_resp.status_code == 200:
                dns_data = dns_resp.json()
                answers = dns_data.get("Answer", [])
                ip_addresses = [a["data"] for a in answers if a.get("type") == 1]
                if ip_addresses:
                    result["resolved"] = True
                    result["details"]["ip_addresses"] = ip_addresses[:3]

                    # Get geolocation for the first IP
                    ip = ip_addresses[0]
                    try:
                        geo_resp = await client.get(
                            f"http://ip-api.com/json/{ip}",
                            params={"fields": "status,country,countryCode,region,regionName,city,isp,org,as,hosting"},
                            timeout=5
                        )
                        if geo_resp.status_code == 200:
                            geo = geo_resp.json()
                            if geo.get("status") == "success":
                                result["details"]["geolocation"] = {
                                    "country": geo.get("country", ""),
                                    "country_code": geo.get("countryCode", ""),
                                    "region": geo.get("regionName", ""),
                                    "city": geo.get("city", ""),
                                    "isp": geo.get("isp", ""),
                                    "org": geo.get("org", ""),
                                    "is_hosting": geo.get("hosting", False),
                                }

                                # Check for CDN (if hosting=True, it's likely a CDN)
                                org_lower = geo.get("org", "").lower()
                                isp_lower = geo.get("isp", "").lower()
                                cdn_indicators = ["cloudflare", "amazon", "aws", "google", "akamai",
                                                  "fastly", "cloudfront", "azure", "digitalocean"]
                                if any(cdn in org_lower or cdn in isp_lower for cdn in cdn_indicators):
                                    result["details"]["likely_cdn"] = True
                                    result["details"]["cdn_note"] = "Website uses CDN — geolocation reflects CDN server, not business location"
                    except Exception:
                        pass
        except Exception as e:
            result["error"] = str(e)

        # Also check NS records for hosting provider insight
        try:
            ns_resp = await client.get(
                "https://dns.google/resolve",
                params={"name": domain, "type": "NS"}
            )
            if ns_resp.status_code == 200:
                ns_data = ns_resp.json()
                ns_answers = ns_data.get("Answer", [])
                ns_records = [a["data"] for a in ns_answers if a.get("type") == 2]
                if ns_records:
                    result["details"]["nameservers"] = ns_records[:4]
        except Exception:
            pass

    return result


# ─────────────────────────────────────────────
# C7. SSL Certificate Details
# ─────────────────────────────────────────────

async def check_ssl_certificate(domain: str) -> dict:
    """
    Check SSL certificate details for a domain.
    EV certificates (with organization name) = strong positive signal.
    Self-signed or expired = red flag.
    No HTTPS at all = very suspicious for a business website.
    """
    result = {
        "source": "ssl_certificate",
        "domain": domain,
        "has_ssl": False,
        "details": {}
    }

    if "://" in domain:
        domain = urlparse(domain).netloc
    domain = domain.replace("www.", "").split("/")[0]

    # Use crt.sh for certificate transparency log lookup
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.get(
                f"https://crt.sh/?q={domain}&output=json",
                follow_redirects=True
            )
            if resp.status_code == 200:
                certs = resp.json()
                if certs:
                    result["has_ssl"] = True
                    result["details"]["total_certificates"] = len(certs)

                    # Get the most recent certificate
                    latest = certs[0]  # crt.sh returns newest first
                    result["details"]["latest_cert"] = {
                        "issuer": latest.get("issuer_name", ""),
                        "not_before": latest.get("not_before", ""),
                        "not_after": latest.get("not_after", ""),
                        "common_name": latest.get("common_name", ""),
                    }

                    # Check certificate type
                    issuer = latest.get("issuer_name", "").lower()
                    if "let's encrypt" in issuer:
                        result["details"]["cert_type"] = "DV (Let's Encrypt)"
                        result["details"]["cert_note"] = "Free DV certificate — common, not a signal either way"
                    elif "extended validation" in issuer or "ev " in issuer:
                        result["details"]["cert_type"] = "EV (Extended Validation)"
                        result["details"]["cert_note"] = "EV certificate — organization identity verified. Positive signal."
                    elif "organization" in issuer:
                        result["details"]["cert_type"] = "OV (Organization Validated)"
                        result["details"]["cert_note"] = "OV certificate — organization existence verified."
                    else:
                        result["details"]["cert_type"] = "DV (Domain Validated)"

                    # Check oldest cert for domain age estimation
                    if len(certs) > 1:
                        oldest = certs[-1]
                        result["details"]["first_cert_date"] = oldest.get("not_before", "")
                else:
                    result["details"]["note"] = "No certificates found in CT logs"
        except Exception as e:
            result["error"] = str(e)

    return result


# ─────────────────────────────────────────────
# B3. Director Cross-Referencing
# ─────────────────────────────────────────────

async def check_director_history(person_name: str) -> dict:
    """
    Find ALL companies where this person is/was a director.
    Pattern detection:
    - 15+ directorships with recent incorporation = shell network
    - All at same address = formation agent customer
    - Many dissolved companies = serial business failure
    """
    import os
    api_key = os.getenv("COMPANIES_HOUSE_API_KEY", "")
    result = {
        "source": "director_history",
        "person_name": person_name,
        "total_appointments": 0,
        "active_companies": 0,
        "dissolved_companies": 0,
        "details": {},
        "flags": []
    }

    if api_key:
        async with httpx.AsyncClient(timeout=20) as client:
            try:
                # Search for officers
                resp = await client.get(
                    "https://api.company-information.service.gov.uk/search/officers",
                    params={"q": person_name, "items_per_page": 20},
                    auth=(api_key, ""),
                )
                if resp.status_code == 200:
                    data = resp.json()
                    items = data.get("items", [])

                    if items:
                        # Get the first matching officer's appointment details
                        officer = items[0]
                        officer_link = officer.get("links", {}).get("self", "")

                        if officer_link:
                            # Get all appointments
                            appt_resp = await client.get(
                                f"https://api.company-information.service.gov.uk{officer_link}/appointments",
                                params={"items_per_page": 50},
                                auth=(api_key, ""),
                            )
                            if appt_resp.status_code == 200:
                                appt_data = appt_resp.json()
                                appointments = appt_data.get("items", [])
                                result["total_appointments"] = appt_data.get("total_results", len(appointments))

                                addresses = {}
                                companies = []
                                for appt in appointments:
                                    company_name = appt.get("appointed_to", {}).get("company_name", "")
                                    company_number = appt.get("appointed_to", {}).get("company_number", "")
                                    status = appt.get("appointed_to", {}).get("company_status", "")
                                    role = appt.get("officer_role", "")
                                    appointed_on = appt.get("appointed_on", "")

                                    companies.append({
                                        "name": company_name,
                                        "number": company_number,
                                        "status": status,
                                        "role": role,
                                        "appointed_on": appointed_on,
                                    })

                                    if status == "active":
                                        result["active_companies"] += 1
                                    elif status in ("dissolved", "liquidation"):
                                        result["dissolved_companies"] += 1

                                    # Track addresses
                                    addr = appt.get("address", {})
                                    addr_key = addr.get("postal_code", "") or addr.get("address_line_1", "")
                                    if addr_key:
                                        addresses[addr_key] = addresses.get(addr_key, 0) + 1

                                result["details"]["companies"] = companies[:20]

                                # Flag patterns
                                if result["total_appointments"] > 15:
                                    result["flags"].append(
                                        f"High number of directorships: {result['total_appointments']}. "
                                        f"May indicate shell company network or formation agent customer."
                                    )
                                if result["dissolved_companies"] > 5:
                                    result["flags"].append(
                                        f"{result['dissolved_companies']} dissolved companies. "
                                        f"Pattern of serial company formation and dissolution."
                                    )
                                # Check if many companies at same address
                                for addr, count in addresses.items():
                                    if count > 5 and addr:
                                        result["flags"].append(
                                            f"{count} companies at same address ({addr}). "
                                            f"Possible formation agent / virtual office customer."
                                        )

            except Exception as e:
                result["error"] = str(e)
    else:
        # Fallback: web search
        try:
            results = await _search_ddg(
                f'"{person_name}" director site:find-and-update.company-information.service.gov.uk',
                max_results=5
            )
            for r in results:
                if isinstance(r, dict) and "body" in r:
                    result["details"].setdefault("web_results", []).append({
                        "title": r.get("title", ""),
                        "snippet": r.get("body", "")[:300],
                        "url": r.get("href", ""),
                    })
        except Exception:
            pass

    return result


# ─────────────────────────────────────────────
# E2. Land Registry (basic)
# ─────────────────────────────────────────────

async def check_land_registry(address: str, postcode: str = "") -> dict:
    """
    Check Land Registry price paid data for an address.
    Free data available at landregistry.data.gov.uk for price paid.
    Can verify if claimed owned premises are actually owned.
    """
    result = {
        "source": "land_registry",
        "address": address,
        "found": False,
        "details": {}
    }

    search_term = postcode or address

    # Land Registry Price Paid Data (SPARQL endpoint)
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            # Use web search to find land registry records
            results = await _search_ddg(
                f'site:landregistry.data.gov.uk "{search_term}"',
                max_results=3
            )
            for r in results:
                if isinstance(r, dict) and "landregistry" in r.get("href", ""):
                    result["found"] = True
                    result["details"]["url"] = r["href"]
                    result["details"]["snippet"] = r.get("body", "")[:300]
                    break
        except Exception:
            pass

        # Fallback: general search
        if not result["found"]:
            try:
                results = await _search_ddg(
                    f'"{search_term}" "land registry" OR "title register" OR "price paid"',
                    max_results=3
                )
                for r in results:
                    if isinstance(r, dict) and "body" in r:
                        body = r.get("body", "").lower()
                        if "land registry" in body or "price paid" in body:
                            result["details"]["web_snippet"] = r.get("body", "")[:300]
                            break
            except Exception:
                pass

    return result
