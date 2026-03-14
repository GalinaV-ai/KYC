#!/usr/bin/env python3
"""
KYC Onboarding Agent — Web Interface (Streamlit)

Run:
    streamlit run web_app.py
"""
import asyncio
import json
import os
import sys
import uuid
import tempfile
import time
from datetime import datetime

import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from models import KYCCase, PersonInfo, BusinessInfo, BusinessActivity
from agents.orchestrator import KYCOrchestrator
from agents.risk_analyst import run_risk_assessment
import anthropic

# ─── Page config ───
st.set_page_config(
    page_title="KYC Agent",
    page_icon="🏦",
    layout="wide",
    initial_sidebar_state="expanded"
)

CASES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cases")
os.makedirs(CASES_DIR, exist_ok=True)

# ─── CSS (desktop + mobile) ───
st.markdown("""
<style>
    /* ── Desktop defaults ── */
    .block-container { padding-top: 2.5rem; }
    .reasoning-card {
        background: #fafafa; border-left: 3px solid #90a4ae;
        padding: 8px 12px; margin: 4px 0; border-radius: 3px; font-size: 0.82em;
        line-height: 1.4;
    }
    .suspicion-none { border-left-color: #a5d6a7; }
    .suspicion-low { border-left-color: #ffcc80; }
    .suspicion-medium { border-left-color: #ef9a9a; }
    .suspicion-high { border-left-color: #c62828; background: #fff8e1; }
    .conf-bar { height: 6px; border-radius: 3px; background: #e0e0e0; overflow: hidden; margin-top: 3px; }
    .conf-fill { height: 100%; border-radius: 3px; transition: width 0.3s; }
    .summary-box {
        background: #f5f5f5; border-radius: 4px; padding: 8px 12px;
        margin: 4px 0 12px 0; font-size: 0.83em; color: #555; line-height: 1.4;
    }
    .sidebar-field { font-size: 0.85em; color: #666; margin: 2px 0; }
    .sidebar-field b { color: #333; }
    [data-testid="stSidebar"] [data-testid="stExpander"] { font-size: 0.88em; }
    [data-testid="stSidebar"] [data-testid="stExpander"] p { font-size: 0.88em; margin: 2px 0; }
    [data-testid="stSidebar"] [data-testid="stCaptionContainer"] { font-size: 0.78em; }
    div[data-testid="stChatMessage"] { padding: 0.5rem 0.75rem; }

    /* ── Mobile adaptations (< 768px) ── */
    @media (max-width: 768px) {
        .block-container { padding: 1rem 0.5rem 0.5rem 0.5rem !important; max-width: 100% !important; }

        /* Stack columns vertically */
        [data-testid="stHorizontalBlock"] {
            flex-direction: column !important;
        }
        [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {
            width: 100% !important;
            flex: 1 1 100% !important;
            min-width: 100% !important;
        }

        /* Compact chat messages */
        div[data-testid="stChatMessage"] {
            padding: 0.3rem 0.5rem !important;
            font-size: 0.92em;
        }

        /* Smaller headings */
        h1 { font-size: 1.4em !important; }
        h2 { font-size: 1.2em !important; }
        h3 { font-size: 1.05em !important; }

        /* Metrics — smaller */
        [data-testid="stMetric"] { padding: 0.3rem 0.2rem !important; }
        [data-testid="stMetricLabel"] { font-size: 0.75em !important; }
        [data-testid="stMetricValue"] { font-size: 1.1em !important; }

        /* Reasoning cards — tighter */
        .reasoning-card { padding: 6px 8px; font-size: 0.78em; }

        /* Sidebar — compact */
        [data-testid="stSidebar"] { min-width: 260px !important; max-width: 300px !important; }
        [data-testid="stSidebar"] [data-testid="stExpander"] { font-size: 0.82em; }

        /* Buttons — full width and touch-friendly */
        button { min-height: 44px !important; }

        /* Download buttons — compact */
        [data-testid="stDownloadButton"] button { font-size: 0.85em !important; }

        /* Expanders — touch-friendly */
        [data-testid="stExpander"] summary { min-height: 44px; display: flex; align-items: center; }
    }

    /* ── Small phones (< 480px) ── */
    @media (max-width: 480px) {
        .block-container { padding: 0.5rem 0.3rem !important; }
        div[data-testid="stChatMessage"] { font-size: 0.88em; }
        .reasoning-card { font-size: 0.75em; padding: 4px 6px; }
        [data-testid="stSidebar"] { min-width: 240px !important; }
    }
</style>
""", unsafe_allow_html=True)

# ─── Paste detection ───
# Uses timing heuristic: if the user produces many characters in very little
# time, the text was almost certainly pasted rather than typed.  Average human
# typing speed is ~5-7 chars/sec for fast typists; anything above 15 chars/sec
# for 50+ characters is a reliable paste indicator.


# ─── Session state ───

def init_session_state():
    defaults = {
        "case": None, "orchestrator": None, "messages": [],
        "reasoning_entries": [], "interview_started": False,
        "interview_complete": False, "risk_assessment": None,
        "pending_doc_request": None, "case_id": None,
        "business_stage": "existing", "_last_msg_time": 0,
    }
    for key, val in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = val


def create_new_case(customer_name: str, company_name: str):
    case_id = f"KYC-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
    case = KYCCase(
        case_id=case_id,
        created_at=datetime.now().isoformat(),
        updated_at=datetime.now().isoformat(),
        current_phase="business_basics",
    )
    case.person.full_name = customer_name
    case.business.company_name = company_name

    case_path = os.path.join(CASES_DIR, f"{case_id}.json")
    case.save(case_path)

    st.session_state.case = case
    st.session_state.case_id = case_id
    st.session_state.orchestrator = KYCOrchestrator(case, case_path)
    st.session_state.messages = []
    st.session_state.reasoning_entries = []
    st.session_state.interview_started = False
    st.session_state.interview_complete = False
    st.session_state.risk_assessment = None
    st.session_state.pending_doc_request = None


# ─── Async handlers ───

async def start_interview_fast():
    """Start interview immediately — checks will run in background on first answer."""
    orch = st.session_state.orchestrator
    case = st.session_state.case
    stage = st.session_state.get("business_stage", "existing")
    greeting = await orch.start_interview(business_stage=stage)
    st.session_state.messages.append({"role": "assistant", "content": greeting})
    case.add_conversation_entry("agent", greeting)
    st.session_state.interview_started = True
    _sync_reasoning()


async def send_message(user_input: str, pasted: bool = False, keystroke_ratio: float = 1.0):
    orch = st.session_state.orchestrator
    case = st.session_state.case
    st.session_state.messages.append({"role": "user", "content": user_input})
    case.add_conversation_entry("customer", user_input)
    response = await orch.process_customer_input(user_input, pasted=pasted, keystroke_ratio=keystroke_ratio)
    if orch.interview_complete:
        st.session_state.interview_complete = True
    st.session_state.messages.append({"role": "assistant", "content": response})
    case.add_conversation_entry("agent", response)
    _sync_reasoning()
    return response


async def process_document(file_path: str, doc_type: str):
    orch = st.session_state.orchestrator
    case = st.session_state.case
    response = await orch.process_document_upload(file_path, doc_type)
    case.add_conversation_entry("system", f"Document uploaded: {doc_type}")
    st.session_state.messages.append({"role": "assistant", "content": response})
    case.add_conversation_entry("agent", response)
    st.session_state.pending_doc_request = None
    _sync_reasoning()
    return response


async def run_assessment():
    case = st.session_state.case
    orch = st.session_state.orchestrator

    # Use the new orchestrator's run_assessment which delegates to Risk Analyst
    assessment = await orch.run_assessment()
    case.risk_assessment = assessment
    case.current_phase = "completed"
    case.save(os.path.join(CASES_DIR, f"{st.session_state.case_id}.json"))
    st.session_state.risk_assessment = assessment
    return assessment


def _sync_reasoning():
    orch = st.session_state.orchestrator
    if orch:
        st.session_state.reasoning_entries = orch.get_reasoning_log()


# ─── UI helpers ───

def _get_confidence_pct() -> int:
    orch = st.session_state.orchestrator
    if not orch or not orch.qa_log:
        return 0
    # Estimate confidence from assessor summary
    summary = orch.assessor.get_assessment_summary()
    total = summary.get("total", 0)
    if total == 0:
        # Fallback: estimate from question count
        return min(100, len(orch.qa_log) * 8)
    confirmed = summary.get("confirmed", 0)
    return min(100, int(confirmed / total * 100) if total else 0)


def _get_live_summary() -> str:
    orch = st.session_state.orchestrator
    if not orch or not orch.qa_log:
        return ""

    case = st.session_state.case
    biz_name = case.business.company_name or "the business"

    parts = []
    q_count = len(orch.qa_log)
    parts = [f"{q_count} question{'s' if q_count != 1 else ''} asked"]

    # Assessment status from assessor
    a_summary = orch.assessor.get_assessment_summary()
    total_assessed = a_summary.get("total", 0)
    if total_assessed > 0:
        confirmed = a_summary.get("confirmed", 0)
        contradicted = a_summary.get("contradicted", 0)
        parts.append(f"{total_assessed} facts checked ({confirmed} confirmed)")
        if contradicted > 0:
            parts.append(f"{contradicted} contradiction{'s' if contradicted > 1 else ''}")

    # Verification engine status
    ve_summary = orch.verification_engine.get_summary()
    checks_run = ve_summary.get("total_checks_run", 0)
    if checks_run > 0:
        parts.append(f"{checks_run} verification checks run")

    red_flags = len(case.red_flags)
    if red_flags > 0:
        parts.append(f"{red_flags} red flag{'s' if red_flags > 1 else ''}")

    return ". ".join(parts) + "."


def render_score_bar(label: str, score: float, color: str = "#1976d2"):
    pct = int(score * 100)
    st.markdown(f"""
    <div style="margin: 4px 0;">
        <div style="display:flex; justify-content:space-between; font-size:0.83em;">
            <span>{label}</span><span><b>{pct}%</b></span>
        </div>
        <div class="conf-bar"><div class="conf-fill" style="width:{pct}%;background:{color};"></div></div>
    </div>
    """, unsafe_allow_html=True)


# ─── Verification rendering ───

def _render_sidebar_verification(v: dict):
    """Render a single verification as a readable sidebar card."""
    source = v.get("source", "unknown")
    query = v.get("query", "")
    result = v.get("result", {})

    # Icons and labels per source type
    source_map = {
        "person_search": ("👤", "Person Search"),
        "company_search": ("🏢", "Company Search"),
        "companies_house": ("🏛", "Companies House"),
        "sanctions": ("🔍", "Sanctions Check"),
        "background_investigation": ("🔎", "Fact Check"),
        "url_verification": ("🌐", "Website Check"),
        "linkedin_verification": ("💼", "LinkedIn Check"),
    }
    icon, label = source_map.get(source, ("📋", source.replace("_", " ").title()))

    # Short display for query
    short_query = query[:50] + "…" if len(query) > 50 else query

    # Status-based coloring for investigation results
    status = result.get("status", "") if isinstance(result, dict) else ""
    status_indicator = {
        "confirmed": "🟢", "contradicted": "🔴", "inconclusive": "🟡",
        "partially_confirmed": "🟡", "not_found": "⚪",
    }.get(status, "")

    expander_title = f"{icon} {label}"
    if short_query:
        expander_title += f": {short_query}"
    if status_indicator:
        expander_title = f"{status_indicator} {expander_title}"

    with st.expander(expander_title, expanded=False):
        if not isinstance(result, dict):
            st.caption("No data")
            return

        if source == "sanctions":
            _render_sanctions(result)
        elif source == "companies_house":
            _render_companies_house(result)
        elif source == "person_search":
            _render_person_search(result)
        elif source == "company_search":
            _render_company_search(result)
        elif source == "background_investigation":
            _render_fact_check(result)
        elif source == "url_verification":
            _render_url_check(result)
        elif source == "linkedin_verification":
            _render_linkedin_check(result)
        else:
            summary = result.get("summary", result.get("evidence", ""))
            if summary:
                st.markdown(f"*{summary}*")
            else:
                st.caption("Raw data available in case JSON")


def _render_fact_check(result: dict):
    """Render a background fact-check finding."""
    claim = result.get("claim", "")
    status = result.get("status", "unknown")
    evidence = result.get("evidence", "")
    confidence = result.get("confidence", "")

    status_label = {
        "confirmed": "🟢 Confirmed",
        "contradicted": "🔴 Contradicted",
        "inconclusive": "🟡 Inconclusive",
        "partially_confirmed": "🟡 Partially confirmed",
        "not_found": "⚪ Not found",
    }.get(status, f"⚪ {status}")

    if claim:
        st.markdown(f"**Claim:** {claim}")
    st.markdown(f"**Result:** {status_label}")
    if confidence:
        st.caption(f"Confidence: {confidence}")
    if evidence:
        st.markdown(f"{evidence[:300]}")


def _render_url_check(result: dict):
    """Render a website/URL verification finding."""
    claim = result.get("claim", "")
    status = result.get("status", "unknown")
    evidence = result.get("evidence", "")
    key_detail = result.get("key_detail", "")
    urls = result.get("urls", [])
    liveness = result.get("liveness", {})

    status_label = {
        "confirmed": "🟢 Confirmed",
        "contradicted": "🔴 Contradicted",
        "inconclusive": "🟡 Inconclusive",
    }.get(status, f"⚪ {status}")

    if urls:
        for u in urls[:2]:
            st.markdown(f"[{u[:60]}]({u})")
    if claim:
        st.caption(f"Claim: {claim}")
    st.markdown(f"**{status_label}**")

    if liveness:
        score = liveness.get("liveness_score")
        if score is not None:
            pct = int(score * 100)
            bar = "🟢" if pct >= 60 else "🟡" if pct >= 30 else "🔴"
            st.markdown(f"{bar} Liveness: **{pct}%**")
        domain_age = liveness.get("domain_age_years")
        if domain_age:
            st.caption(f"Domain age: {domain_age} years")
        indexation = liveness.get("search_indexed_pages")
        if indexation:
            st.caption(f"Indexed pages: {indexation}")
        reviews = liveness.get("reviews", {})
        if reviews:
            parts = []
            if reviews.get("trustpilot"):
                parts.append(f"Trustpilot: {reviews['trustpilot']}")
            if reviews.get("google"):
                parts.append(f"Google: {reviews['google']}")
            if parts:
                st.caption(" | ".join(parts))
        app_store = liveness.get("app_store")
        if app_store and app_store.get("found"):
            st.caption(f"App store: found ({app_store.get('platform', '')})")

    if key_detail:
        st.markdown(f"*{key_detail[:200]}*")
    if evidence and evidence != key_detail:
        st.caption(f"{evidence[:200]}")


def _render_linkedin_check(result: dict):
    """Render a LinkedIn profile verification finding."""
    claim = result.get("claim", "")
    status = result.get("status", "unknown")
    evidence = result.get("evidence", "")
    urls = result.get("urls", [])
    depth = result.get("linkedin_depth", {})

    status_label = {
        "confirmed": "🟢 Confirmed",
        "contradicted": "🔴 Contradicted",
        "inconclusive": "🟡 Inconclusive",
    }.get(status, f"⚪ {status}")

    if urls:
        for u in urls[:2]:
            st.markdown(f"[{u[:60]}]({u})")
    st.markdown(f"**{status_label}**")
    if claim:
        st.caption(f"Claim: {claim}")

    if depth:
        # Person info
        headline = depth.get("headline", "")
        connections = depth.get("connections_count")
        location = depth.get("location", "")
        activity = depth.get("activity_level", "")

        if headline:
            st.markdown(f"**{headline}**")
        info_parts = []
        if connections:
            info_parts.append(f"{connections}+ connections")
        if location:
            info_parts.append(location)
        if activity:
            info_parts.append(f"Activity: {activity}")
        if info_parts:
            st.caption(" | ".join(info_parts))

        # Company page
        company_page = depth.get("company_page", {})
        if company_page and company_page.get("found"):
            cp_parts = []
            if company_page.get("followers"):
                cp_parts.append(f"{company_page['followers']} followers")
            if company_page.get("employees"):
                cp_parts.append(f"{company_page['employees']} employees")
            if cp_parts:
                st.caption(f"Company page: {' | '.join(cp_parts)}")

    if evidence:
        st.markdown(f"*{evidence[:250]}*")


def _render_sanctions(result: dict):
    """Render sanctions screening results."""
    for entity_type in ("person", "company"):
        entity = result.get(entity_type, {})
        if not entity:
            continue
        name = entity.get("name", entity_type)
        status = entity.get("status", "unknown")
        status_icon = {"clear": "🟢", "hit": "🔴", "possible_match": "🟡"}.get(status, "⚪")
        st.markdown(f"{status_icon} **{name}**: {status}")

        matches = entity.get("matches", [])
        if matches:
            for m in matches[:3]:
                m_name = m.get("name", "?")
                m_score = m.get("score", 0)
                m_lists = ", ".join(m.get("lists", [])[:2]) if m.get("lists") else ""
                detail = f"  {m_name} (score: {m_score})"
                if m_lists:
                    detail += f" — {m_lists}"
                st.caption(detail)


def _render_companies_house(result: dict):
    """Render Companies House lookup."""
    if result.get("error"):
        st.caption(result.get("note", result["error"]))
        return

    status = result.get("company_status", "")
    number = result.get("company_number", "")
    inc_date = result.get("date_of_creation", "")
    address = result.get("registered_office_address", "")
    sic = result.get("sic_codes", [])

    if number:
        st.markdown(f"**#{number}** — {status or 'unknown status'}")
    if inc_date:
        st.markdown(f"Incorporated: {inc_date}")
    if address and isinstance(address, dict):
        addr_str = ", ".join(v for v in address.values() if v)
        st.markdown(f"Address: {addr_str}")
    elif address:
        st.markdown(f"Address: {address}")
    if sic:
        st.markdown(f"SIC: {', '.join(sic)}")

    officers = result.get("officers", [])
    if officers:
        st.markdown("**Officers:**")
        for o in officers[:5]:
            role = o.get("officer_role", "")
            o_name = o.get("name", "?")
            st.caption(f"  {o_name} — {role}")


def _render_person_search(result: dict):
    """Render person search results."""
    summary = result.get("summary", "")
    if summary:
        st.markdown(f"*{summary}*")
        st.markdown("")

    # LinkedIn
    linkedin = result.get("linkedin_results", [])
    if linkedin:
        st.markdown(f"**LinkedIn** ({len(linkedin)} results)")
        for item in linkedin[:3]:
            title = item.get("title", "")
            snippet = item.get("snippet", "")[:120]
            url = item.get("url", "")
            relevance = item.get("relevance", "")
            rel_icon = {"high": "🟢", "medium": "🟡", "low": "⚪"}.get(relevance, "")
            st.markdown(f"{rel_icon} [{title[:60]}]({url})" if url else f"{rel_icon} {title[:60]}")
            if snippet:
                st.caption(f"  {snippet}...")

    # Business associations
    biz_assoc = result.get("business_associations", [])
    if biz_assoc:
        st.markdown(f"**Business links** ({len(biz_assoc)})")
        for item in biz_assoc[:4]:
            title = item.get("title", "")
            snippet = item.get("snippet", "")[:100]
            st.caption(f"  {title[:60]}: {snippet}")

    # News
    news = result.get("news_mentions", [])
    if news:
        st.markdown(f"**News** ({len(news)})")
        for item in news[:3]:
            title = item.get("title", "")
            url = item.get("url", "")
            st.markdown(f"  📰 [{title[:60]}]({url})" if url else f"  📰 {title[:60]}")


def _render_company_search(result: dict):
    """Render company search results."""
    summary = result.get("summary", "")
    if summary:
        st.markdown(f"*{summary}*")
        st.markdown("")

    # Web results
    web = result.get("web_results", [])
    if web:
        st.markdown(f"**Web** ({len(web)})")
        for item in web[:3]:
            title = item.get("title", "")
            url = item.get("url", "")
            snippet = item.get("snippet", "")[:100]
            st.markdown(f"  🌐 [{title[:50]}]({url})" if url else f"  🌐 {title[:50]}")
            if snippet:
                st.caption(f"  {snippet}")

    # Reviews
    reviews = result.get("review_results", [])
    if reviews:
        st.markdown(f"**Reviews** ({len(reviews)})")
        for item in reviews[:3]:
            title = item.get("title", "")
            snippet = item.get("snippet", "")[:100]
            st.caption(f"  ⭐ {title[:50]}: {snippet}")

    # News
    news = result.get("news_results", [])
    if news:
        st.markdown(f"**News** ({len(news)})")
        for item in news[:3]:
            title = item.get("title", "")
            url = item.get("url", "")
            st.markdown(f"  📰 [{title[:50]}]({url})" if url else f"  📰 {title[:50]}")

    # Regulatory
    reg = result.get("regulatory_results", [])
    if reg:
        st.markdown(f"**Regulatory** ({len(reg)})")
        for item in reg[:3]:
            title = item.get("title", "")
            url = item.get("url", "")
            st.markdown(f"  🏛 [{title[:50]}]({url})" if url else f"  🏛 {title[:50]}")


# ─── Sidebar ───

def render_sidebar():
    with st.sidebar:
        st.markdown("### 🏦 KYC Agent")

        with st.expander("New Interview", expanded=st.session_state.case is None):
            name = st.text_input("Customer name", key="new_name")
            company = st.text_input("Company name", key="new_company")
            biz_stage = st.radio(
                "Stage", ["Existing business", "New business"],
                key="new_biz_stage", horizontal=True,
            )
            if st.button("Start", type="primary", use_container_width=True):
                stage = "existing" if biz_stage == "Existing business" else "new"
                if not name:
                    st.warning("Customer name is required")
                elif stage == "existing" and not company:
                    st.warning("Company name is required for existing businesses")
                else:
                    create_new_case(name, company or "---")
                    st.session_state.business_stage = stage
                    st.rerun()

        if not st.session_state.case:
            return

        case = st.session_state.case
        st.markdown("---")

        if case.business.company_name:
            st.markdown(f"**{case.business.company_name}**")

        # Red flags — compact
        if case.red_flags:
            st.markdown(f"**Red flags: {len(case.red_flags)}**")
            for flag in case.red_flags:
                sev = flag.get("severity", "").upper()
                icon = "🔴" if sev in ("HIGH", "CRITICAL") else "🟡"
                st.markdown(f"{icon} {flag.get('description', '')[:60]}")

        # Verifications — readable cards in sidebar
        if case.verifications:
            st.markdown(f"**Verifications** ({len(case.verifications)})")
            for v in case.verifications:
                _render_sidebar_verification(v)

        # Downloads
        st.markdown("---")
        case_json = json.dumps(case.to_dict(), indent=2, ensure_ascii=False)
        st.download_button("Case JSON", data=case_json,
                           file_name=f"{st.session_state.case_id}.json",
                           mime="application/json", use_container_width=True)

        orch = st.session_state.orchestrator
        if orch and orch.get_reasoning_log():
            report = orch.get_reasoning_report()
            st.download_button("Reasoning log", data=report,
                               file_name=f"{st.session_state.case_id}_reasoning.txt",
                               mime="text/plain", use_container_width=True)


# ─── Main area ───

def render_main():
    case = st.session_state.case

    if not case:
        st.markdown("### 🏦 KYC Onboarding Agent")
        st.caption("Business verification for UK small business banking")
        st.info("👈 Fill in customer details to start.")
        return

    # Two-column layout: Chat + Reasoning
    chat_col, reasoning_col = st.columns([3, 2])
    with chat_col:
        render_chat(case)
    with reasoning_col:
        render_reasoning_panel()


def render_chat(case):
    """Left column: chat interface."""

    # Risk assessment results
    if st.session_state.risk_assessment:
        render_risk_assessment(st.session_state.risk_assessment)
        return

    # Auto-start interview
    if not st.session_state.interview_started:
        with st.spinner("Preparing interview..."):
            asyncio.run(start_interview_fast())
        st.rerun()
        return

    # Chat messages
    for msg in st.session_state.messages:
        avatar = "👤" if msg["role"] == "user" else "🏦"
        with st.chat_message(msg["role"], avatar=avatar):
            st.markdown(msg["content"])

    # Document upload (if requested)
    if st.session_state.pending_doc_request:
        doc_req = st.session_state.pending_doc_request
        st.info(f"📎 Document requested: {doc_req['doc_type']}")
        uploaded = st.file_uploader(
            f"Upload {doc_req['doc_type']}",
            type=["jpg", "jpeg", "png", "gif", "webp", "pdf"],
            key=f"doc_{len(st.session_state.messages)}"
        )
        c1, c2 = st.columns(2)
        with c1:
            if uploaded and st.button("Submit", type="primary"):
                td = tempfile.mkdtemp()
                fp = os.path.join(td, uploaded.name)
                with open(fp, "wb") as f:
                    f.write(uploaded.getbuffer())
                with st.spinner("Analyzing..."):
                    asyncio.run(process_document(fp, doc_req["doc_type"]))
                st.rerun()
        with c2:
            if st.button("Skip"):
                st.session_state.pending_doc_request = None
                with st.spinner("Continuing..."):
                    asyncio.run(send_message(f"[Customer chose not to provide {doc_req['doc_type']}]"))
                st.rerun()

    # Interview complete → auto-trigger risk assessment
    if st.session_state.interview_complete and not st.session_state.risk_assessment:
        with st.spinner("Analyzing collected information and generating assessment..."):
            asyncio.run(run_assessment())
        st.rerun()
        return

    # "Stop & assess" button
    if not st.session_state.interview_complete:
        if st.button("⏹ Stop interview & see result", type="secondary",
                      use_container_width=True):
            orch = st.session_state.orchestrator
            orch.interview_complete = True
            orch._add_reasoning({
                "note": "Interview stopped manually by the operator.",
                "suspicion": "none",
            })
            st.session_state.interview_complete = True
            st.rerun()

    # Chat input with paste detection (timing heuristic)
    if not st.session_state.interview_complete:
        user_input = st.chat_input("Type your response...")
        if user_input:
            now = time.time()
            last_t = st.session_state.get("_last_msg_time", 0)
            elapsed = now - last_t if last_t else 999
            text_len = len(user_input)

            # Timing heuristic: characters per second
            cps = text_len / max(elapsed, 0.1)
            pasted = False
            if text_len > 50 and cps > 15:
                # >15 chars/sec for 50+ chars — almost certainly paste
                pasted = True
            elif text_len > 150 and cps > 8:
                # >8 chars/sec for long text — very likely paste
                pasted = True

            ks_ratio = 1.0
            if pasted:
                ks_ratio = min(0.2, 5.0 / max(cps, 1))

            with st.spinner(""):
                asyncio.run(send_message(user_input, pasted=pasted, keystroke_ratio=ks_ratio))
            st.session_state["_last_msg_time"] = time.time()
            st.rerun()

    # Manual doc upload
    with st.expander("📎 Upload document"):
        doc_type = st.selectbox("Type:", [
            "certificate_of_incorporation", "bank_statement", "invoice_sample",
            "contract_sample", "business_licence", "lease_agreement",
            "utility_bill", "tax_return", "other"
        ])
        uploaded = st.file_uploader("File", type=["jpg", "jpeg", "png", "gif", "webp", "pdf"],
                                     key="manual_upload")
        if uploaded and st.button("Upload"):
            td = tempfile.mkdtemp()
            fp = os.path.join(td, uploaded.name)
            with open(fp, "wb") as f:
                f.write(uploaded.getbuffer())
            with st.spinner("Analyzing..."):
                asyncio.run(process_document(fp, doc_type))
            st.rerun()

    # Collected data
    if case:
        biz = {k: v for k, v in case.business.__dict__.items() if v}
        act = {k: v for k, v in case.activity.__dict__.items() if v}
        if biz or act:
            with st.expander("Collected Data"):
                if biz:
                    for k, v in biz.items():
                        st.markdown(f"**{k}:** {v}")
                if act:
                    for k, v in act.items():
                        st.markdown(f"**{k}:** {v}")


def render_reasoning_panel():
    """Right column: agent's internal reasoning."""
    st.markdown("**Agent Reasoning**")

    entries = st.session_state.reasoning_entries
    if not entries:
        st.caption("Reasoning log will appear here as the interview progresses.")
        return

    # Show entries newest first
    for i, entry in enumerate(reversed(entries)):
        idx = len(entries) - i
        suspicion = entry.get("suspicion") or entry.get("suspicion_level", "none")
        css = f"reasoning-card suspicion-{suspicion}"

        html = f'<div class="{css}">'
        # Header with step number and suspicion badge
        if suspicion and suspicion != "none":
            sus_colors = {"low": "#ff9800", "medium": "#f44336", "high": "#b71c1c"}
            html += f'<span style="color:{sus_colors.get(suspicion, "#666")};font-weight:600;font-size:0.8em;">⚠ {suspicion.upper()}</span> '

        if entry.get("note"):
            html += f'{entry["note"]}'
        if entry.get("why"):
            html += f'<br><span style="color:#d32f2f;font-size:0.9em;">↳ {entry["why"]}</span>'
        # Old format fallback
        if entry.get("learned") and not entry.get("note"):
            html += f'{entry["learned"]}'
        html += '</div>'
        st.markdown(html, unsafe_allow_html=True)



def render_risk_assessment(assessment: dict):
    """Show risk assessment results — full narrative report."""

    # Handle parse failure
    if assessment.get("error"):
        st.error("Could not parse structured assessment")
        st.text(assessment.get("raw_assessment", ""))
        return

    decision = assessment.get("decision", "unknown").upper()
    risk = assessment.get("overall_risk_level", "unknown").upper()
    confidence = assessment.get("confidence_score", 0)

    # ── Decision banner ──
    decision_labels = {
        "APPROVE": ("Approved", "success"),
        "APPROVE_WITH_EDD": ("Approved — Enhanced Due Diligence", "warning"),
        "MANUAL_REVIEW": ("Sent to Manual Review", "warning"),
        "MANUAL_REVIEW_URGENT": ("Sent to Manual Review (Urgent)", "error"),
    }
    label, kind = decision_labels.get(decision, (decision, "info"))
    if kind == "success":
        st.success(f"### {label}")
    elif kind == "warning":
        st.warning(f"### {label}")
    else:
        st.error(f"### {label}")

    # Decision reasoning
    reasoning = assessment.get("decision_reasoning", "")
    if reasoning:
        st.markdown(f"*{reasoning}*")

    # ── Metrics (2x2 grid — works on mobile) ──
    row1_c1, row1_c2 = st.columns(2)
    with row1_c1:
        risk_icon = {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🔴", "CRITICAL": "🔴"}.get(risk, "⚪")
        st.metric("Risk Level", f"{risk_icon} {risk}")
    with row1_c2:
        st.metric("Overall Score", f"{assessment.get('overall_score', 0):.0%}")
    row2_c1, row2_c2 = st.columns(2)
    with row2_c1:
        st.metric("Confidence", f"{confidence:.0%}")
    with row2_c2:
        concerns = assessment.get("concerns", assessment.get("red_flags", []))
        st.metric("Concerns", len(concerns))

    # ── Score bars (2x2 grid) ──
    st.markdown("---")
    scores = [
        ("Business Legitimacy", "business_legitimacy_score", "#388e3c"),
        ("Operational Knowledge", "operational_knowledge_score", "#1976d2"),
        ("Financial Plausibility", "financial_plausibility_score", "#f57c00"),
        ("Consistency", "consistency_score", "#7b1fa2"),
    ]
    for i in range(0, len(scores), 2):
        cols = st.columns(2)
        for j, col in enumerate(cols):
            if i + j < len(scores):
                lbl, key, color = scores[i + j]
                score = assessment.get(key, 0)
                if isinstance(score, (int, float)):
                    with col:
                        render_score_bar(lbl, score, color)

    # ── Sanctions Screening ──
    sanctions = assessment.get("sanctions_screening")
    if sanctions:
        st.markdown("---")
        st.markdown("### Sanctions Screening")
        scol1, scol2 = st.columns(2)
        with scol1:
            app_status = sanctions.get("applicant", "not checked")
            comp_status = sanctions.get("company", "not checked")
            app_icon = "🔴" if app_status == "hit" else "🟡" if app_status == "possible_match" else "🟢"
            comp_icon = "🔴" if comp_status == "hit" else "🟡" if comp_status == "possible_match" else "🟢"
            st.markdown(f"{app_icon} **Applicant:** {app_status}")
            st.markdown(f"{comp_icon} **Company:** {comp_status}")
        with scol2:
            counterparties = sanctions.get("counterparties", [])
            if counterparties:
                for cp in counterparties:
                    cp_name = cp.get("name", "?")
                    cp_rel = cp.get("relationship", "")
                    cp_stat = cp.get("status", "not_checked")
                    cp_icon = "🔴" if cp_stat == "hit" else "🟡" if cp_stat == "possible_match" else "🟢" if cp_stat == "clear" else "⚪"
                    st.markdown(f"{cp_icon} **{cp_name}** ({cp_rel}): {cp_stat}")
            else:
                st.caption("No counterparties screened")

    # ── Verification Findings ──
    vf = assessment.get("verification_findings")
    if vf and isinstance(vf, dict):
        st.markdown("---")
        st.markdown("### Verification Findings")

        # Summary line
        vf_summary = vf.get("summary", "")
        if vf_summary:
            st.markdown(f"*{vf_summary}*")

        confirmed = vf.get("confirmed", [])
        contradicted = vf.get("contradicted", [])
        inconclusive = vf.get("inconclusive", [])
        not_checked = vf.get("not_checked", [])

        # Stats row (2x2)
        vr1_c1, vr1_c2 = st.columns(2)
        with vr1_c1:
            st.metric("Confirmed", len(confirmed))
        with vr1_c2:
            st.metric("Contradicted", len(contradicted))
        vr2_c1, vr2_c2 = st.columns(2)
        with vr2_c1:
            st.metric("Inconclusive", len(inconclusive))
        with vr2_c2:
            st.metric("Not Checked", len(not_checked))

        # Contradicted findings first (most important)
        if contradicted:
            st.markdown("#### Contradicted Claims")
            for item in contradicted:
                if isinstance(item, dict):
                    st.markdown(f"🔴 **{item.get('claim', '')}**")
                    st.caption(f"  Evidence: {item.get('evidence', '')} (confidence: {item.get('confidence', 'unknown')})")
                elif isinstance(item, str):
                    st.markdown(f"🔴 {item}")

        # Confirmed findings
        if confirmed:
            st.markdown("#### Confirmed Claims")
            for item in confirmed:
                if isinstance(item, dict):
                    st.markdown(f"🟢 **{item.get('claim', '')}**")
                    st.caption(f"  Evidence: {item.get('evidence', '')} (confidence: {item.get('confidence', 'unknown')})")
                elif isinstance(item, str):
                    st.markdown(f"🟢 {item}")

        # Inconclusive
        if inconclusive:
            with st.expander(f"Inconclusive ({len(inconclusive)})"):
                for item in inconclusive:
                    if isinstance(item, dict):
                        st.markdown(f"🟡 {item.get('claim', '')} — {item.get('evidence', '')} ({item.get('note', '')})")
                    elif isinstance(item, str):
                        st.markdown(f"🟡 {item}")

        # Not checked
        if not_checked:
            with st.expander(f"Not Checked ({len(not_checked)})"):
                for item in not_checked:
                    st.markdown(f"⚪ {item}")

    # ── Company Profile (what we know) ──
    profile = assessment.get("company_profile", "")
    if profile:
        st.markdown("---")
        st.markdown("### What We Know")
        st.markdown(profile)

    # ── Positive Signals + Concerns side by side ──
    st.markdown("---")
    pos_col, neg_col = st.columns(2)

    with pos_col:
        st.markdown("### Positive Signals")
        positives = assessment.get("positive_signals", assessment.get("positive_indicators", []))
        if positives:
            for p in positives:
                st.markdown(f"✅ {p}")
        else:
            st.caption("None identified")

    with neg_col:
        st.markdown("### Concerns")
        concerns = assessment.get("concerns", [])
        # Fallback to old red_flags format
        if not concerns:
            concerns = assessment.get("red_flags", [])
        if concerns:
            for c in concerns:
                if isinstance(c, dict):
                    sev = c.get("severity", "").upper()
                    desc = c.get("concern", c.get("flag", c.get("description", str(c))))
                    evidence = c.get("evidence", "")
                    icon = "🔴" if sev in ("HIGH", "CRITICAL") else "🟡" if sev == "MEDIUM" else "⚠️"
                    st.markdown(f"{icon} **[{sev}]** {desc}")
                    if evidence:
                        st.caption(f"  ↳ {evidence}")
                elif isinstance(c, str):
                    st.markdown(f"⚠️ {c}")
        else:
            st.caption("None identified")

    # ── Information Gaps ──
    gaps = assessment.get("information_gaps", [])
    if gaps:
        st.markdown("---")
        st.markdown("### Information Gaps")
        for g in gaps:
            st.markdown(f"❓ {g}")

    # ── Recommendation ──
    recommendation = assessment.get("recommendation", "")
    if recommendation:
        st.markdown("---")
        st.markdown("### Recommendation")
        st.info(recommendation)

    # ── Download ──
    st.markdown("---")
    st.download_button("Download Full Assessment (JSON)",
                       data=json.dumps(assessment, indent=2, ensure_ascii=False),
                       file_name=f"assessment_{st.session_state.case_id}.json",
                       mime="application/json", use_container_width=True)


# ─── Main ───

def main():
    if not os.getenv("ANTHROPIC_API_KEY"):
        st.error("ANTHROPIC_API_KEY not set.")
        st.code("export ANTHROPIC_API_KEY='sk-ant-...'", language="bash")
        st.stop()

    init_session_state()
    render_sidebar()
    render_main()


if __name__ == "__main__":
    main()
