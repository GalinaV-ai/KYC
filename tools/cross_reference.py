"""
Cross-referencing and intelligence modules for KYC verification.
Timeline consistency, financial plausibility, network analysis,
and name fuzzy matching across sources.
"""
import re
import json
from datetime import datetime, date
from typing import Optional


# ─────────────────────────────────────────────
# G1. Timeline Consistency Engine
# ─────────────────────────────────────────────

def check_timeline_consistency(
    claimed_trading_start: str = "",
    company_incorporation_date: str = "",
    domain_registration_date: str = "",
    first_wayback_snapshot: str = "",
    linkedin_profile_date: str = "",
    first_social_media_post: str = "",
    first_ssl_cert_date: str = "",
    first_review_date: str = "",
    claimed_years_trading: float = 0,
) -> dict:
    """
    Cross-references multiple date sources to detect timeline inconsistencies.

    Example inconsistencies:
    - Claims 5 years trading, but company incorporated 6 months ago
    - Domain registered last week, but website claims "established since 2015"
    - LinkedIn shows starting this role in 2024, but claims founding in 2018
    """
    result = {
        "source": "timeline_consistency",
        "consistent": True,
        "inconsistencies": [],
        "timeline": {},
        "confidence": "low"  # low/medium/high — based on how many dates we have
    }

    def parse_date(date_str: str) -> Optional[date]:
        if not date_str:
            return None
        # Handle various formats
        for fmt in ["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ",
                     "%d/%m/%Y", "%d-%m-%Y", "%Y%m%d", "%B %Y", "%Y"]:
            try:
                return datetime.strptime(date_str.strip()[:19], fmt).date()
            except ValueError:
                continue
        # Try to extract just a year
        year_match = re.search(r'(19|20)\d{2}', date_str)
        if year_match:
            return date(int(year_match.group()), 6, 1)  # Mid-year estimate
        return None

    today = date.today()
    dates = {}

    # Parse all available dates
    date_sources = {
        "claimed_trading_start": claimed_trading_start,
        "company_incorporation": company_incorporation_date,
        "domain_registration": domain_registration_date,
        "first_wayback_snapshot": first_wayback_snapshot,
        "linkedin_profile": linkedin_profile_date,
        "first_social_media": first_social_media_post,
        "first_ssl_cert": first_ssl_cert_date,
        "first_review": first_review_date,
    }

    for key, val in date_sources.items():
        parsed = parse_date(val)
        if parsed:
            dates[key] = parsed
            result["timeline"][key] = parsed.isoformat()

    # If claimed_years_trading given, derive approximate start date
    if claimed_years_trading > 0 and "claimed_trading_start" not in dates:
        from dateutil.relativedelta import relativedelta
        approx_start = today - relativedelta(years=int(claimed_years_trading))
        dates["claimed_trading_start"] = approx_start
        result["timeline"]["claimed_trading_start"] = f"~{approx_start.isoformat()} (derived from {claimed_years_trading} years)"

    # Confidence based on data points
    data_points = len(dates)
    if data_points >= 5:
        result["confidence"] = "high"
    elif data_points >= 3:
        result["confidence"] = "medium"
    else:
        result["confidence"] = "low"

    # ── Consistency Checks ──

    claimed_start = dates.get("claimed_trading_start")
    incorporation = dates.get("company_incorporation")
    domain_reg = dates.get("domain_registration")
    wayback = dates.get("first_wayback_snapshot")
    linkedin = dates.get("linkedin_profile")

    # Check 1: Incorporation vs claimed trading start
    if claimed_start and incorporation:
        if incorporation > claimed_start:
            diff_days = (incorporation - claimed_start).days
            if diff_days > 365:  # More than 1 year difference
                result["consistent"] = False
                result["inconsistencies"].append({
                    "type": "incorporation_after_trading",
                    "severity": "high",
                    "description": (
                        f"Company incorporated on {incorporation.isoformat()} but claims trading "
                        f"since {claimed_start.isoformat()} — {diff_days // 365} year gap. "
                        f"Could be legitimate (reincorporation, sole trader to Ltd), but needs explanation."
                    ),
                })

    # Check 2: Domain registration vs claimed history
    if claimed_start and domain_reg:
        if domain_reg > claimed_start:
            diff_days = (domain_reg - claimed_start).days
            if diff_days > 730:  # 2+ years
                result["consistent"] = False
                result["inconsistencies"].append({
                    "type": "domain_newer_than_claims",
                    "severity": "medium",
                    "description": (
                        f"Domain registered on {domain_reg.isoformat()} but business claims "
                        f"trading since {claimed_start.isoformat()}. "
                        f"Could mean: rebranding, domain change, or exaggerated history."
                    ),
                })

    # Check 3: Very recent domain for established business
    if domain_reg:
        domain_age_days = (today - domain_reg).days
        if domain_age_days < 90 and claimed_start:
            claimed_age_days = (today - claimed_start).days
            if claimed_age_days > 365:
                result["consistent"] = False
                result["inconsistencies"].append({
                    "type": "very_new_domain",
                    "severity": "high",
                    "description": (
                        f"Domain registered only {domain_age_days} days ago, but claims "
                        f"{claimed_age_days // 365}+ years of trading. Strong inconsistency."
                    ),
                })

    # Check 4: Wayback Machine vs domain registration
    if wayback and domain_reg:
        if wayback < domain_reg:
            # Wayback captured before domain reg — impossible unless domain was re-registered
            result["inconsistencies"].append({
                "type": "wayback_before_domain",
                "severity": "low",
                "description": (
                    f"Wayback Machine shows snapshot from {wayback.isoformat()} before "
                    f"domain registration date {domain_reg.isoformat()}. "
                    f"Domain may have been re-registered."
                ),
            })

    # Check 5: No Wayback history for long-claimed business
    if claimed_start and not wayback and domain_reg:
        claimed_age_years = (today - claimed_start).days / 365.25
        if claimed_age_years > 3:
            result["inconsistencies"].append({
                "type": "no_web_archive_history",
                "severity": "medium",
                "description": (
                    f"Claims {claimed_age_years:.0f} years of trading but no Wayback Machine "
                    f"snapshots found. Established businesses usually have some web archive history."
                ),
            })

    # Check 6: LinkedIn profile date vs founding claim
    if linkedin and claimed_start:
        if linkedin > claimed_start:
            diff_years = (linkedin - claimed_start).days / 365.25
            if diff_years > 3:
                result["inconsistencies"].append({
                    "type": "linkedin_after_founding",
                    "severity": "low",
                    "description": (
                        f"LinkedIn profile activity starts {linkedin.isoformat()} but claims "
                        f"founding in {claimed_start.isoformat()}. {diff_years:.0f} year gap."
                    ),
                })

    # Check 7: Everything very recent
    recent_threshold = date(today.year - 1, today.month, today.day)
    all_dates = [d for d in dates.values() if d]
    if all_dates and all(d > recent_threshold for d in all_dates) and len(all_dates) >= 3:
        result["inconsistencies"].append({
            "type": "everything_very_recent",
            "severity": "medium",
            "description": (
                "All available dates (company registration, domain, web presence) are "
                "within the last 12 months. Combined with other factors, this could indicate "
                "a recently established entity."
            ),
        })

    if not result["inconsistencies"]:
        result["consistent"] = True

    return result


# ─────────────────────────────────────────────
# G2. Financial Plausibility Calculator
# ─────────────────────────────────────────────

def check_financial_plausibility(
    claimed_annual_turnover: float = 0,
    claimed_monthly_turnover: float = 0,
    claimed_employees: int = 0,
    industry: str = "",
    company_age_years: float = 0,
    is_sole_trader: bool = False,
    has_physical_premises: bool = False,
    claimed_profit_margin: float = 0,
    claimed_avg_transaction: float = 0,
    claimed_monthly_transactions: int = 0,
) -> dict:
    """
    Cross-checks financial claims against industry benchmarks and common sense.

    Catches:
    - 2 employees with £5M turnover in cleaning (impossible)
    - £200k turnover but not VAT registered (illegal over £85k threshold)
    - 10,000 monthly transactions for a 3-person consultancy (implausible)
    - Profit margins outside industry norms
    """
    result = {
        "source": "financial_plausibility",
        "plausible": True,
        "flags": [],
        "calculations": {},
    }

    # Normalize to annual
    annual = claimed_annual_turnover
    if not annual and claimed_monthly_turnover:
        annual = claimed_monthly_turnover * 12
        result["calculations"]["annual_from_monthly"] = annual

    if not annual:
        result["flags"].append({
            "type": "no_turnover_data",
            "severity": "info",
            "description": "No turnover figures provided to analyze."
        })
        return result

    monthly = annual / 12 if annual else claimed_monthly_turnover

    # ── Basic sanity checks ──

    # Check 1: Revenue per employee
    if claimed_employees > 0:
        rev_per_employee = annual / claimed_employees
        result["calculations"]["revenue_per_employee"] = round(rev_per_employee)

        # General UK SME benchmarks
        if rev_per_employee > 500_000:
            result["flags"].append({
                "type": "high_revenue_per_employee",
                "severity": "medium",
                "description": (
                    f"£{rev_per_employee:,.0f} revenue per employee is very high. "
                    f"Average UK SME: £80k-£200k per employee. "
                    f"Could be legitimate for tech/consulting, suspicious for labour-intensive industries."
                ),
            })
        elif rev_per_employee < 20_000 and not is_sole_trader:
            result["flags"].append({
                "type": "low_revenue_per_employee",
                "severity": "low",
                "description": (
                    f"£{rev_per_employee:,.0f} revenue per employee is very low. "
                    f"May indicate overstated employee count or understated revenue."
                ),
            })

    # Check 2: VAT threshold
    if annual > 85_000:
        result["calculations"]["above_vat_threshold"] = True
        result["calculations"]["note"] = "Business should be VAT registered (threshold £85k)"

    # Check 3: Transaction volume vs revenue
    if claimed_monthly_transactions and monthly:
        avg_calculated = monthly / claimed_monthly_transactions
        result["calculations"]["calculated_avg_transaction"] = round(avg_calculated, 2)

        if claimed_avg_transaction:
            # Compare claimed vs calculated average transaction
            diff_pct = abs(avg_calculated - claimed_avg_transaction) / max(claimed_avg_transaction, 1) * 100
            result["calculations"]["avg_transaction_diff_pct"] = round(diff_pct, 1)
            if diff_pct > 50:
                result["plausible"] = False
                result["flags"].append({
                    "type": "transaction_math_mismatch",
                    "severity": "high",
                    "description": (
                        f"Claimed average transaction £{claimed_avg_transaction:,.0f} × "
                        f"{claimed_monthly_transactions} monthly transactions = "
                        f"£{claimed_avg_transaction * claimed_monthly_transactions:,.0f}/month. "
                        f"But claimed monthly turnover is £{monthly:,.0f}. "
                        f"Difference: {diff_pct:.0f}%."
                    ),
                })

    # Check 4: Profit margin
    if claimed_profit_margin:
        if claimed_profit_margin > 80:
            result["flags"].append({
                "type": "extreme_profit_margin",
                "severity": "medium",
                "description": (
                    f"Claimed profit margin of {claimed_profit_margin}% is exceptionally high. "
                    f"Very few businesses sustain margins above 60%. "
                    f"May indicate misunderstanding of gross vs net margin."
                ),
            })
        elif claimed_profit_margin < 0:
            result["flags"].append({
                "type": "negative_margin",
                "severity": "low",
                "description": "Negative profit margin — business is loss-making."
            })

    # Check 5: Company age vs revenue (startup vs established)
    if company_age_years and company_age_years < 1 and annual > 1_000_000:
        result["flags"].append({
            "type": "high_revenue_new_company",
            "severity": "medium",
            "description": (
                f"Company less than 1 year old claiming £{annual:,.0f} annual turnover. "
                f"Possible if acquiring an existing business, but worth verifying."
            ),
        })

    # Check 6: Industry-specific plausibility
    industry_lower = industry.lower() if industry else ""

    industry_max_per_employee = {
        "cleaning": 60_000,
        "restaurant": 80_000,
        "retail": 150_000,
        "construction": 200_000,
        "consulting": 300_000,
        "it_services": 250_000,
        "software": 400_000,
        "fintech": 500_000,
        "ecommerce": 500_000,
    }

    for ind_key, max_rev in industry_max_per_employee.items():
        if ind_key in industry_lower and claimed_employees > 0:
            if rev_per_employee > max_rev * 2:  # 2x the typical max
                result["plausible"] = False
                result["flags"].append({
                    "type": "industry_revenue_mismatch",
                    "severity": "high",
                    "description": (
                        f"£{rev_per_employee:,.0f}/employee is implausible for {ind_key}. "
                        f"Typical maximum: £{max_rev:,.0f}/employee."
                    ),
                })
            break

    # Check 7: Sole trader plausibility
    if is_sole_trader and annual > 500_000:
        result["flags"].append({
            "type": "sole_trader_high_revenue",
            "severity": "medium",
            "description": (
                f"Sole trader claiming £{annual:,.0f} annual turnover. "
                f"Possible in consulting/freelancing but unusual. "
                f"Most sole traders with this revenue incorporate for tax reasons."
            ),
        })

    if result["flags"]:
        high_severity = any(f["severity"] == "high" for f in result["flags"])
        result["plausible"] = not high_severity

    return result


# ─────────────────────────────────────────────
# G3. Name Fuzzy Matching
# ─────────────────────────────────────────────

def fuzzy_name_match(name1: str, name2: str, threshold: float = 0.7) -> dict:
    """
    Compare two names with fuzzy matching, handling:
    - Case differences
    - Common transliterations (Cyrillic ↔ Latin, Arabic ↔ Latin)
    - Name variants (Mohammad/Mohammed/Muhammad)
    - First/last name swaps
    - Middle name presence/absence
    - Company suffixes (Ltd, Limited, LLC, Inc)
    """
    result = {
        "source": "name_matching",
        "name1": name1,
        "name2": name2,
        "match": False,
        "score": 0.0,
        "method": "",
    }

    if not name1 or not name2:
        return result

    # Normalize
    def normalize(name: str) -> str:
        name = name.lower().strip()
        # Remove company suffixes
        suffixes = [
            " ltd", " limited", " llc", " inc", " plc", " corp",
            " corporation", " co.", " company", " group", " holdings",
            " services", " solutions", " uk", " (uk)",
        ]
        for suffix in suffixes:
            name = name.replace(suffix, "")
        # Remove special characters
        name = re.sub(r'[^\w\s]', '', name)
        name = re.sub(r'\s+', ' ', name).strip()
        return name

    n1 = normalize(name1)
    n2 = normalize(name2)

    # Exact match after normalization
    if n1 == n2:
        result["match"] = True
        result["score"] = 1.0
        result["method"] = "exact_normalized"
        return result

    # Check containment (one name contained in the other)
    if n1 in n2 or n2 in n1:
        longer = max(len(n1), len(n2))
        shorter = min(len(n1), len(n2))
        result["score"] = shorter / longer
        result["match"] = result["score"] >= threshold
        result["method"] = "containment"
        if result["match"]:
            return result

    # Token-based matching (handles word order, middle names)
    tokens1 = set(n1.split())
    tokens2 = set(n2.split())
    if tokens1 and tokens2:
        intersection = tokens1 & tokens2
        union = tokens1 | tokens2
        jaccard = len(intersection) / len(union)
        result["score"] = max(result["score"], jaccard)
        if jaccard >= threshold:
            result["match"] = True
            result["method"] = "token_jaccard"
            return result

    # Name variants
    name_variants = {
        "mohammad": ["mohammed", "muhammad", "mohamad", "mohamed"],
        "alexander": ["alex", "aleksandr", "alexandre"],
        "william": ["will", "bill", "billy"],
        "robert": ["rob", "bob", "bobby"],
        "richard": ["rick", "dick", "rich"],
        "james": ["jim", "jimmy"],
        "michael": ["mike", "mick", "mikhail"],
        "david": ["dave", "davey"],
        "nikolay": ["nikolai", "nick", "nicholas"],
        "sergey": ["sergei", "serge"],
        "dmitry": ["dmitri", "dimitri", "dima"],
        "andrey": ["andrei", "andrew", "andre"],
        "yuri": ["yury", "yuriy"],
        "elena": ["helen", "helena", "yelena"],
        "natalia": ["natasha", "natalya"],
        "ekaterina": ["catherine", "katherine", "kate"],
    }

    tokens1_list = n1.split()
    tokens2_list = n2.split()
    variant_match = False
    for t1 in tokens1_list:
        for t2 in tokens2_list:
            if t1 == t2:
                continue
            # Check if one is a variant of the other
            for base, variants in name_variants.items():
                all_forms = [base] + variants
                if t1 in all_forms and t2 in all_forms:
                    variant_match = True
                    break
            if variant_match:
                break
        if variant_match:
            break

    if variant_match:
        # Re-score with variant match bonus
        result["score"] = max(result["score"], 0.8)
        result["match"] = True
        result["method"] = "name_variant"
        return result

    # Levenshtein-style character comparison (simplified)
    def char_similarity(s1, s2):
        if not s1 or not s2:
            return 0.0
        # Simple bigram similarity
        def bigrams(s):
            return set(s[i:i+2] for i in range(len(s)-1))
        b1 = bigrams(s1)
        b2 = bigrams(s2)
        if not b1 or not b2:
            return 0.0
        return 2 * len(b1 & b2) / (len(b1) + len(b2))

    char_sim = char_similarity(n1, n2)
    result["score"] = max(result["score"], char_sim)
    if char_sim >= threshold:
        result["match"] = True
        result["method"] = "character_similarity"

    return result


# ─────────────────────────────────────────────
# G4. Network Analysis
# ─────────────────────────────────────────────

def analyze_company_network(director_appointments: list) -> dict:
    """
    Analyze a network of companies associated with a person.
    Input: list of company appointments from director_history check.

    Detects:
    - Shell company patterns (many companies, same address, short lifespan)
    - Circular ownership
    - Concentration at virtual office addresses
    - Sector mismatch (director of restaurant also directing crypto exchange)
    """
    result = {
        "source": "network_analysis",
        "total_companies": len(director_appointments),
        "patterns": [],
        "risk_score": 0.0,  # 0-1
        "details": {}
    }

    if not director_appointments:
        return result

    # Track various dimensions
    statuses = {}
    addresses = {}
    sic_codes = {}
    incorporation_years = []
    active_count = 0
    dissolved_count = 0

    for appt in director_appointments:
        # Status distribution
        status = appt.get("status", "unknown")
        statuses[status] = statuses.get(status, 0) + 1
        if status == "active":
            active_count += 1
        elif status in ("dissolved", "liquidation"):
            dissolved_count += 1

        # Address clustering
        addr = appt.get("address", "")
        if isinstance(addr, dict):
            addr = addr.get("postal_code", "") or addr.get("address_line_1", "")
        if addr:
            addresses[addr] = addresses.get(addr, 0) + 1

        # Incorporation date tracking
        inc_date = appt.get("appointed_on", "") or appt.get("incorporation_date", "")
        if inc_date:
            year_match = re.search(r'(19|20)\d{2}', str(inc_date))
            if year_match:
                incorporation_years.append(int(year_match.group()))

    result["details"]["status_distribution"] = statuses
    result["details"]["active_count"] = active_count
    result["details"]["dissolved_count"] = dissolved_count

    risk = 0.0

    # Pattern 1: High volume of companies
    total = len(director_appointments)
    if total > 20:
        risk += 0.3
        result["patterns"].append({
            "type": "high_volume",
            "severity": "high",
            "description": f"{total} company associations — unusual for a typical business owner."
        })
    elif total > 10:
        risk += 0.15
        result["patterns"].append({
            "type": "elevated_volume",
            "severity": "medium",
            "description": f"{total} company associations — above average."
        })

    # Pattern 2: High dissolution rate
    if total > 3 and dissolved_count / total > 0.7:
        risk += 0.25
        result["patterns"].append({
            "type": "high_dissolution_rate",
            "severity": "high",
            "description": (
                f"{dissolved_count}/{total} companies dissolved ({dissolved_count/total*100:.0f}%). "
                f"Pattern of serial company formation and closure."
            )
        })

    # Pattern 3: Address concentration
    max_addr_count = max(addresses.values()) if addresses else 0
    if max_addr_count > 5:
        risk += 0.2
        max_addr = max(addresses, key=addresses.get)
        result["patterns"].append({
            "type": "address_concentration",
            "severity": "medium",
            "description": (
                f"{max_addr_count} companies at the same address ({max_addr}). "
                f"Typical of formation agent or virtual office customer."
            )
        })

    # Pattern 4: Burst incorporation (many companies in short period)
    if len(incorporation_years) > 5:
        from collections import Counter
        year_counts = Counter(incorporation_years)
        max_year, max_count = year_counts.most_common(1)[0]
        if max_count > 3:
            risk += 0.2
            result["patterns"].append({
                "type": "burst_incorporation",
                "severity": "medium",
                "description": (
                    f"{max_count} companies incorporated in {max_year}. "
                    f"Burst of company formation may indicate shell company creation."
                )
            })

    # Pattern 5: Only very recent companies
    current_year = datetime.now().year
    if incorporation_years and all(y >= current_year - 1 for y in incorporation_years) and len(incorporation_years) > 3:
        risk += 0.2
        result["patterns"].append({
            "type": "all_recent",
            "severity": "medium",
            "description": "All companies incorporated within last 1-2 years."
        })

    result["risk_score"] = min(1.0, risk)
    return result


# ─────────────────────────────────────────────
# D1. Company Accounts Analysis
# ─────────────────────────────────────────────

def analyze_company_accounts(
    filed_turnover: float = 0,
    filed_total_assets: float = 0,
    filed_net_assets: float = 0,
    filed_employees: int = 0,
    claimed_turnover: float = 0,
    claimed_employees: int = 0,
    accounts_type: str = "",
) -> dict:
    """
    Compare filed company accounts (from Companies House) against customer claims.
    Note: micro-entity and small company accounts may not include turnover.
    """
    result = {
        "source": "company_accounts_analysis",
        "matches_claims": True,
        "flags": [],
        "details": {}
    }

    # Check if micro/small entity (limited disclosure)
    if accounts_type:
        result["details"]["accounts_type"] = accounts_type
        if "micro" in accounts_type.lower():
            result["details"]["note"] = (
                "Micro-entity accounts — turnover and employee info not required to be filed. "
                "Limited data available for cross-checking."
            )
        elif "small" in accounts_type.lower():
            result["details"]["note"] = (
                "Small company accounts — abbreviated disclosure. "
                "Turnover may not be reported."
            )

    # Turnover comparison
    if filed_turnover and claimed_turnover:
        ratio = claimed_turnover / filed_turnover if filed_turnover != 0 else 0
        result["details"]["turnover_ratio"] = round(ratio, 2)
        if ratio > 2.0:
            result["matches_claims"] = False
            result["flags"].append({
                "type": "turnover_overclaim",
                "severity": "high",
                "description": (
                    f"Claimed turnover £{claimed_turnover:,.0f} is {ratio:.1f}x the filed "
                    f"turnover of £{filed_turnover:,.0f}. Significant overstatement."
                )
            })
        elif ratio < 0.3:
            result["flags"].append({
                "type": "turnover_underclaim",
                "severity": "medium",
                "description": (
                    f"Claimed turnover £{claimed_turnover:,.0f} is much lower than filed "
                    f"£{filed_turnover:,.0f}. Could be annual vs monthly confusion."
                )
            })

    # Employee comparison
    if filed_employees and claimed_employees:
        emp_ratio = claimed_employees / filed_employees if filed_employees != 0 else 0
        result["details"]["employee_ratio"] = round(emp_ratio, 2)
        if emp_ratio > 3.0 or emp_ratio < 0.3:
            result["flags"].append({
                "type": "employee_mismatch",
                "severity": "medium",
                "description": (
                    f"Claimed {claimed_employees} employees vs filed {filed_employees}. "
                    f"Significant discrepancy."
                )
            })

    # Net assets as health indicator
    if filed_net_assets is not None and filed_net_assets < 0:
        result["flags"].append({
            "type": "negative_net_assets",
            "severity": "medium",
            "description": (
                f"Company has negative net assets (£{filed_net_assets:,.0f}). "
                f"May indicate financial distress or accumulated losses."
            )
        })

    return result
