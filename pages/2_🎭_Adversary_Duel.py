#!/usr/bin/env python3
"""
Adversary Duel — Agent vs Agent testing page.

A fraudster agent (OpenAI GPT-5.4) tries to pass the KYC interview.
The user watches the dialogue in real-time — each message appears immediately.

Completely isolated: the adversary module has NO access to KYC prompts or logic.
"""
import asyncio
import json
import os
import sys
import time

import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adversary.duel import DuelOrchestrator, DuelEvent
from adversary.fraudster import FraudsterAgent

import subprocess

def _get_version() -> str:
    try:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        result = subprocess.run(
            ["git", "log", "-1", "--format=%h (%ad)", "--date=short"],
            capture_output=True, text=True, cwd=root, timeout=3,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return "unknown"

APP_VERSION = _get_version()

# Note: page_config is set by the main web_app.py — not here

# ─── CSS ───
st.markdown("""
<style>
    .block-container { padding-top: 2rem; }
    .kyc-msg {
        background: #e3f2fd; border-left: 3px solid #1976d2;
        padding: 10px 14px; margin: 6px 0; border-radius: 4px;
        font-size: 0.92em;
    }
    .fraud-msg {
        background: #fce4ec; border-left: 3px solid #c62828;
        padding: 10px 14px; margin: 6px 0; border-radius: 4px;
        font-size: 0.92em;
    }
    .system-msg {
        background: #f5f5f5; border-left: 3px solid #9e9e9e;
        padding: 8px 12px; margin: 4px 0; border-radius: 4px;
        font-size: 0.82em; color: #666;
    }
    .reasoning-card {
        background: #fffde7; border-left: 3px solid #f9a825;
        padding: 8px 12px; margin: 4px 0; border-radius: 3px;
        font-size: 0.80em; line-height: 1.4;
    }
    .verdict-approve {
        background: #e8f5e9; border: 2px solid #4caf50;
        padding: 16px; border-radius: 8px; text-align: center;
        font-size: 1.1em;
    }
    .verdict-decline {
        background: #ffebee; border: 2px solid #f44336;
        padding: 16px; border-radius: 8px; text-align: center;
        font-size: 1.1em;
    }
    .verdict-escalate {
        background: #fff8e1; border: 2px solid #ff9800;
        padding: 16px; border-radius: 8px; text-align: center;
        font-size: 1.1em;
    }
    .doc-badge {
        background: #e8eaf6; border: 1px solid #7986cb;
        padding: 6px 10px; border-radius: 4px; margin: 4px 0;
        font-size: 0.82em; display: inline-block;
    }
</style>
""", unsafe_allow_html=True)


# ─── Session state ───

def init_duel_state():
    defaults = {
        "duel": None,
        "duel_events_data": [],  # list of dicts (serialized events)
        "duel_complete": False,
        "duel_legend": None,
        "duel_case_id": None,
    }
    for key, val in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = val


def reset_duel():
    for key in list(st.session_state.keys()):
        if key.startswith("duel"):
            del st.session_state[key]
    init_duel_state()


# ─── Async helpers ───

def _run_async(coro):
    """Run an async coroutine synchronously."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ─── Rendering helpers ───

def render_message(container, source: str, content: str):
    """Render a chat message into a Streamlit container."""
    if source == "kyc":
        container.markdown(
            f'<div class="kyc-msg"><b>🏦 KYC Agent:</b><br>{content}</div>',
            unsafe_allow_html=True,
        )
    elif source == "fraudster":
        container.markdown(
            f'<div class="fraud-msg"><b>🎭 Fraudster:</b><br>{content}</div>',
            unsafe_allow_html=True,
        )


def render_reasoning(container, reasoning_data: dict):
    """Render a reasoning entry."""
    note = reasoning_data.get("note", "")
    suspicion = reasoning_data.get("suspicion", "none")
    if note:
        container.markdown(
            f'<div class="reasoning-card">🧠 {note} <i>(suspicion: {suspicion})</i></div>',
            unsafe_allow_html=True,
        )


def render_system(container, content: str):
    container.markdown(
        f'<div class="system-msg">⚙️ {content}</div>',
        unsafe_allow_html=True,
    )


def render_doc(container, doc_type: str, filename: str):
    container.markdown(
        f'<div class="doc-badge">📄 <b>Fake doc:</b> {filename} ({doc_type})</div>',
        unsafe_allow_html=True,
    )


def render_verdict(container, assessment: dict):
    """Render final verdict."""
    decision = assessment.get("decision", assessment.get("recommendation", "unknown")).lower()

    container.markdown("---")
    container.markdown("## Final Verdict")

    if "approve" in decision:
        container.markdown(
            '<div class="verdict-approve">✅ <b>APPROVED</b> — The fraudster passed!</div>',
            unsafe_allow_html=True,
        )
    elif "decline" in decision or "reject" in decision:
        container.markdown(
            '<div class="verdict-decline">❌ <b>DECLINED</b> — The fraudster was caught!</div>',
            unsafe_allow_html=True,
        )
    elif "escalat" in decision:
        container.markdown(
            '<div class="verdict-escalate">⚠️ <b>ESCALATED</b> — Sent for manual review</div>',
            unsafe_allow_html=True,
        )
    else:
        container.info(f"Decision: {decision}")

    with container.expander("Full Assessment", expanded=False):
        concerns = assessment.get("concerns", assessment.get("red_flags", []))
        if concerns:
            st.markdown("**Concerns:**")
            for c in concerns:
                if isinstance(c, dict):
                    st.markdown(f"- {c.get('concern', c.get('description', str(c)))}")
                else:
                    st.markdown(f"- {c}")

        positives = assessment.get("positive_signals", [])
        if positives:
            st.markdown("**Positive Signals:**")
            for p in positives:
                st.markdown(f"- {p}")

        st.json(assessment)


def render_legend_sidebar(legend: dict):
    with st.sidebar:
        st.caption(f"v {APP_VERSION}")
        st.markdown("### 🎭 Fraudster's Legend")
        st.markdown(f"**Name:** {legend.get('full_name', '?')}")
        st.markdown(f"**Company:** {legend.get('company_name', '?')}")
        st.markdown(f"**Business:** {legend.get('business_type', '?')}")
        st.markdown(f"**Revenue:** {legend.get('annual_revenue', '?')}")
        with st.expander("Full Legend", expanded=False):
            st.json(legend)
        st.markdown("---")
        st.caption("The fraudster (GPT-5.4) sees ONLY the interviewer's messages — no reasoning, no verifications, no prompts.")


def render_debug_downloads(duel):
    """Render JSON download buttons for debugging."""
    st.markdown("---")
    st.markdown("### 📥 Debug Downloads")
    dl_cols = st.columns(5)

    events_data = [e.to_dict() for e in duel.events]

    with dl_cols[0]:
        st.download_button(
            "Transcript",
            data=json.dumps(events_data, indent=2, ensure_ascii=False),
            file_name=f"duel_{duel.case_id}_transcript.json",
            mime="application/json", use_container_width=True,
        )

    with dl_cols[1]:
        if duel.fraudster and duel.fraudster.legend:
            st.download_button(
                "Legend",
                data=json.dumps(duel.fraudster.legend, indent=2, ensure_ascii=False),
                file_name=f"duel_{duel.case_id}_legend.json",
                mime="application/json", use_container_width=True,
            )

    with dl_cols[2]:
        if duel.case:
            st.download_button(
                "KYC Case",
                data=json.dumps(duel.case.to_dict(), indent=2, ensure_ascii=False),
                file_name=f"duel_{duel.case_id}_case.json",
                mime="application/json", use_container_width=True,
            )

    with dl_cols[3]:
        if duel.kyc_orchestrator:
            reasoning_log = duel.kyc_orchestrator.get_reasoning_log()
            if reasoning_log:
                st.download_button(
                    "Reasoning",
                    data=json.dumps(reasoning_log, indent=2, ensure_ascii=False),
                    file_name=f"duel_{duel.case_id}_reasoning.json",
                    mime="application/json", use_container_width=True,
                )

    with dl_cols[4]:
        assessment_events = [e for e in duel.events if e.event_type == "assessment"]
        if assessment_events:
            st.download_button(
                "Assessment",
                data=assessment_events[-1].content,
                file_name=f"duel_{duel.case_id}_assessment.json",
                mime="application/json", use_container_width=True,
            )


# ═══════════════════════════════════════════════
# LIVE DUEL — runs the entire duel in a single Streamlit execution,
# appending messages to containers in real-time.
# ═══════════════════════════════════════════════

def run_live_duel(duel: DuelOrchestrator):
    """
    Execute the full duel, rendering each message as it arrives.
    Uses st.container() so messages appear incrementally without page reloads.
    """
    # Two-column layout
    chat_col, reasoning_col = st.columns([3, 2])

    with chat_col:
        st.markdown("### 💬 Conversation")
        chat_area = st.container()

    with reasoning_col:
        st.markdown("### 🧠 KYC Reasoning & Verifications")
        reasoning_area = st.container()

    status = st.empty()

    # ── Step 1: KYC greeting ──
    status.info("⚔️ KYC agent is starting the interview...")
    greeting = _run_async(duel.kyc_orchestrator.start_interview(business_stage="existing"))
    event = DuelEvent("message", "kyc", greeting)
    duel.events.append(event)
    render_message(chat_area, "kyc", greeting)

    # ── Step 2: Duel loop ──
    max_turns = 40
    turn = 0
    prev_reasoning_count = 0
    prev_verification_count = 0

    while turn < max_turns and not duel.kyc_orchestrator.interview_complete:
        turn += 1
        status.info(f"⚔️ Turn {turn} — Fraudster is responding...")

        # ── Fraudster responds ──
        last_kyc = None
        for e in reversed(duel.events):
            if e.source == "kyc" and e.event_type == "message":
                last_kyc = e.content
                break

        if not last_kyc:
            break

        fraudster_reply = duel.fraudster.respond(last_kyc)
        event = DuelEvent("message", "fraudster", fraudster_reply)
        duel.events.append(event)
        render_message(chat_area, "fraudster", fraudster_reply)

        # ── Document generation (if requested) ──
        if duel.fraudster.should_offer_document(last_kyc):
            try:
                from adversary.doc_generator import generate_fake_document
                doc_type = duel._infer_doc_type(last_kyc)
                status.info(f"⚔️ Turn {turn} — Generating fake {doc_type}...")
                doc_path = generate_fake_document(
                    doc_type=doc_type,
                    legend=duel.fraudster.legend,
                    output_dir=duel.doc_output_dir,
                    context=last_kyc,
                )
                doc_event = DuelEvent("document", "fraudster",
                                      f"Generated fake {doc_type}",
                                      metadata={"doc_type": doc_type, "path": doc_path})
                duel.events.append(doc_event)
                render_doc(chat_area, doc_type, os.path.basename(doc_path))

                doc_response = _run_async(
                    duel.kyc_orchestrator.process_document_upload(doc_path, doc_type)
                )
                doc_ack = DuelEvent("message", "kyc", doc_response,
                                    metadata={"type": "doc_acknowledgement"})
                duel.events.append(doc_ack)
                render_message(chat_area, "kyc", doc_response)
            except Exception as e:
                err_event = DuelEvent("system", "system", f"Doc generation failed: {e}")
                duel.events.append(err_event)
                render_system(chat_area, str(e))

        # ── KYC processes the answer ──
        status.info(f"⚔️ Turn {turn} — KYC agent is thinking & verifying...")
        kyc_response = _run_async(
            duel.kyc_orchestrator.process_customer_input(fraudster_reply)
        )

        is_done = duel.kyc_orchestrator.interview_complete
        event = DuelEvent("message", "kyc", kyc_response,
                          metadata={"interview_complete": is_done} if is_done else {})
        duel.events.append(event)
        render_message(chat_area, "kyc", kyc_response)

        # ── Update reasoning panel ──
        reasoning_log = duel.kyc_orchestrator.get_reasoning_log()
        new_reasoning = reasoning_log[prev_reasoning_count:]
        for r in new_reasoning:
            render_reasoning(reasoning_area, r)
        prev_reasoning_count = len(reasoning_log)

        # ── Update verifications panel ──
        if duel.case and duel.case.verifications:
            new_verifications = duel.case.verifications[prev_verification_count:]
            for v in new_verifications:
                source = v.get("source", "?")
                query = v.get("query", "")
                with reasoning_area.expander(f"🔍 {source}: {query[:50]}", expanded=False):
                    st.json(v)
            prev_verification_count = len(duel.case.verifications)

    # ── Step 3: Assessment ──
    status.info("⚔️ Running final assessment...")
    duel.kyc_orchestrator.interview_complete = True
    assessment = _run_async(duel.kyc_orchestrator.run_assessment())

    assessment_event = DuelEvent(
        "assessment", "kyc",
        json.dumps(assessment, indent=2, ensure_ascii=False),
        metadata={"decision": assessment.get("decision", "unknown")},
    )
    duel.events.append(assessment_event)
    duel.is_complete = True

    # Render verdict in chat column
    render_verdict(chat_area, assessment)

    # Final reasoning
    reasoning_log = duel.kyc_orchestrator.get_reasoning_log()
    new_reasoning = reasoning_log[prev_reasoning_count:]
    for r in new_reasoning:
        render_reasoning(reasoning_area, r)

    status.empty()

    return assessment


# ─── Main page ───

def main():
    init_duel_state()

    with st.sidebar:
        st.caption(f"v {APP_VERSION}")

    st.markdown("# 🎭 Adversary Duel")
    st.caption("Fraudster (OpenAI GPT-5.4) vs KYC Agent (Claude) — automated stress test")

    # Check API keys
    def _get_key(name):
        val = os.getenv(name)
        if not val:
            try:
                val = st.secrets.get(name)
            except Exception:
                pass
        return val

    missing_keys = []
    if not _get_key("ANTHROPIC_API_KEY"):
        missing_keys.append("ANTHROPIC_API_KEY")
    if not _get_key("OPENAI_API_KEY"):
        missing_keys.append("OPENAI_API_KEY")

    if missing_keys:
        st.error(f"Missing API keys: {', '.join(missing_keys)}")
        st.caption("Add them in Streamlit Cloud → Settings → Secrets")
        st.stop()

    # ── If duel already completed, show stored results ──
    if st.session_state.duel_complete and st.session_state.duel:
        duel = st.session_state.duel

        if st.session_state.duel_legend:
            render_legend_sidebar(st.session_state.duel_legend)

        # Re-render all stored events
        chat_col, reasoning_col = st.columns([3, 2])

        with chat_col:
            st.markdown("### 💬 Conversation")
            for e in duel.events:
                if e.event_type == "message":
                    render_message(st, e.source, e.content)
                elif e.event_type == "document":
                    render_doc(st, e.metadata.get("doc_type", ""), os.path.basename(e.metadata.get("path", "")))
                elif e.event_type == "system":
                    render_system(st, e.content)
                elif e.event_type == "assessment":
                    try:
                        render_verdict(st, json.loads(e.content))
                    except Exception:
                        pass

        with reasoning_col:
            st.markdown("### 🧠 KYC Reasoning & Verifications")
            reasoning_log = duel.kyc_orchestrator.get_reasoning_log() if duel.kyc_orchestrator else []
            for r in reasoning_log:
                render_reasoning(st, r)

            if duel.case and duel.case.verifications:
                for v in duel.case.verifications:
                    source = v.get("source", "?")
                    query = v.get("query", "")
                    with st.expander(f"🔍 {source}: {query[:50]}", expanded=False):
                        st.json(v)

        # Stats
        st.markdown("---")
        msgs = [e for e in duel.events if e.event_type == "message"]
        stat_cols = st.columns(4)
        stat_cols[0].metric("Total Messages", len(msgs))
        stat_cols[1].metric("KYC Messages", len([e for e in msgs if e.source == "kyc"]))
        stat_cols[2].metric("Fraudster Answers", len([e for e in msgs if e.source == "fraudster"]))
        stat_cols[3].metric("Fake Docs", len([e for e in duel.events if e.event_type == "document"]))

        render_debug_downloads(duel)

        if st.button("🔄 New Duel", type="secondary"):
            reset_duel()
            st.rerun()

        return

    # ── Setup panel ──
    st.markdown("### Setup")
    mode = st.radio(
        "Legend generation",
        ["🎲 Auto-generate", "✏️ I'll provide hints"],
        horizontal=True,
        key="duel_mode_radio",
    )

    hints = None
    if "hints" in mode.lower():
        hints = st.text_area(
            "Describe the fraudster's business (optional details)",
            placeholder="e.g. Nigerian man running a cleaning company in Manchester, revenue about £200k...",
            height=100,
            key="duel_hints",
        )

    if st.button("⚔️ Start Duel", type="primary"):
        # Setup phase
        with st.spinner("Setting up agents & generating legend..."):
            duel = DuelOrchestrator()
            duel.setup(hints=hints if hints else None)
            st.session_state.duel = duel
            st.session_state.duel_legend = duel.fraudster.legend
            st.session_state.duel_case_id = duel.case_id

        # Show legend
        render_legend_sidebar(duel.fraudster.legend)

        # Run the entire duel live — messages appear in real-time
        run_live_duel(duel)

        # Mark complete for future page reruns
        st.session_state.duel_complete = True

        # Stats
        st.markdown("---")
        msgs = [e for e in duel.events if e.event_type == "message"]
        stat_cols = st.columns(4)
        stat_cols[0].metric("Total Messages", len(msgs))
        stat_cols[1].metric("KYC Messages", len([e for e in msgs if e.source == "kyc"]))
        stat_cols[2].metric("Fraudster Answers", len([e for e in msgs if e.source == "fraudster"]))
        stat_cols[3].metric("Fake Docs", len([e for e in duel.events if e.event_type == "document"]))

        render_debug_downloads(duel)

        if st.button("🔄 New Duel", type="secondary", key="new_duel_after"):
            reset_duel()
            st.rerun()


if __name__ == "__main__":
    main()
