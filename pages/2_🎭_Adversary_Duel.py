#!/usr/bin/env python3
"""
Adversary Duel — Agent vs Agent testing page.

A fraudster agent (OpenAI GPT-5.4) tries to pass the KYC interview.
The user watches the dialogue in real-time and sees full KYC reasoning.

Completely isolated: the adversary module has NO access to KYC prompts or logic.
"""
import asyncio
import json
import os
import sys

import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adversary.duel import DuelOrchestrator, DuelEvent
from adversary.fraudster import FraudsterAgent

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
    .legend-box {
        background: #fff3e0; border: 1px solid #ffb74d;
        padding: 12px; border-radius: 6px; margin: 8px 0;
        font-size: 0.88em;
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
        "duel_events": [],
        "duel_running": False,
        "duel_complete": False,
        "duel_legend": None,
        "duel_setup_mode": "auto",  # "auto" or "manual"
    }
    for key, val in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = val


def reset_duel():
    for key in ["duel", "duel_events", "duel_running", "duel_complete", "duel_legend"]:
        if key in st.session_state:
            del st.session_state[key]
    init_duel_state()


# ─── Async runner ───

async def run_duel_async(duel: DuelOrchestrator):
    """Run duel and collect all events."""
    events = []
    async for event in duel.run_duel():
        events.append(event)
    return events


def run_duel_sync(duel: DuelOrchestrator) -> list[DuelEvent]:
    """Synchronous wrapper for the async duel runner."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(run_duel_async(duel))
    finally:
        loop.close()


# ─── Rendering ───

def render_event(event: DuelEvent):
    """Render a single duel event."""
    if event.event_type == "message":
        if event.source == "kyc":
            st.markdown(f'<div class="kyc-msg"><b>🏦 KYC Agent:</b><br>{event.content}</div>',
                        unsafe_allow_html=True)
        elif event.source == "fraudster":
            st.markdown(f'<div class="fraud-msg"><b>🎭 Fraudster:</b><br>{event.content}</div>',
                        unsafe_allow_html=True)

    elif event.event_type == "document":
        doc_name = os.path.basename(event.metadata.get("path", "document.pdf"))
        st.markdown(f'<div class="doc-badge">📄 <b>Fake document generated:</b> {doc_name} ({event.metadata.get("doc_type", "")})</div>',
                    unsafe_allow_html=True)

    elif event.event_type == "reasoning":
        try:
            reasoning = json.loads(event.content)
            note = reasoning.get("note", "")
            suspicion = reasoning.get("suspicion", "none")
            if note:
                st.markdown(f'<div class="reasoning-card">🧠 <b>KYC thinking:</b> {note} (suspicion: {suspicion})</div>',
                            unsafe_allow_html=True)
        except json.JSONDecodeError:
            pass

    elif event.event_type == "legend":
        st.markdown(f'<div class="system-msg">🎭 {event.content}</div>',
                    unsafe_allow_html=True)

    elif event.event_type == "system":
        st.markdown(f'<div class="system-msg">⚙️ {event.content}</div>',
                    unsafe_allow_html=True)


def render_assessment(event: DuelEvent):
    """Render the final assessment with verdict."""
    try:
        assessment = json.loads(event.content)
    except json.JSONDecodeError:
        st.error("Failed to parse assessment")
        return

    decision = assessment.get("decision", assessment.get("recommendation", "unknown")).lower()

    st.markdown("---")
    st.markdown("## Final Verdict")

    if "approve" in decision:
        st.markdown('<div class="verdict-approve">✅ <b>APPROVED</b> — The fraudster passed!</div>',
                    unsafe_allow_html=True)
        st.balloons()
    elif "decline" in decision or "reject" in decision:
        st.markdown('<div class="verdict-decline">❌ <b>DECLINED</b> — The fraudster was caught!</div>',
                    unsafe_allow_html=True)
    elif "escalat" in decision:
        st.markdown('<div class="verdict-escalate">⚠️ <b>ESCALATED</b> — Sent for manual review</div>',
                    unsafe_allow_html=True)
    else:
        st.info(f"Decision: {decision}")

    # Show assessment details
    with st.expander("Full Assessment", expanded=False):
        # Risk scores
        risk_scores = assessment.get("risk_scores", {})
        if risk_scores:
            cols = st.columns(len(risk_scores))
            for col, (key, val) in zip(cols, risk_scores.items()):
                label = key.replace("_", " ").title()
                score = val if isinstance(val, (int, float)) else 0
                col.metric(label, f"{int(score * 100)}%" if isinstance(score, float) else str(score))

        # Concerns
        concerns = assessment.get("concerns", assessment.get("red_flags", []))
        if concerns:
            st.markdown("**Concerns:**")
            for c in concerns:
                if isinstance(c, dict):
                    st.markdown(f"- {c.get('concern', c.get('description', str(c)))}")
                else:
                    st.markdown(f"- {c}")

        # Positive signals
        positives = assessment.get("positive_signals", [])
        if positives:
            st.markdown("**Positive Signals:**")
            for p in positives:
                st.markdown(f"- {p}")

        st.json(assessment)


def render_legend_sidebar(legend: dict):
    """Show the fraudster's legend in the sidebar."""
    with st.sidebar:
        st.markdown("### 🎭 Fraudster's Legend")
        st.markdown(f"**Name:** {legend.get('full_name', '?')}")
        st.markdown(f"**Company:** {legend.get('company_name', '?')}")
        st.markdown(f"**Business:** {legend.get('business_type', '?')}")
        st.markdown(f"**Revenue:** {legend.get('annual_revenue', '?')}")

        with st.expander("Full Legend", expanded=False):
            st.json(legend)

        st.markdown("---")
        st.markdown("### ℹ️ About")
        st.caption("The fraudster agent (OpenAI GPT-5.4) generates a fake identity and tries to pass the KYC interview. It sees ONLY the interviewer's messages — no reasoning, no verification results, no internal prompts.")


# ─── Main page ───

def main():
    init_duel_state()

    st.markdown("# 🎭 Adversary Duel")
    st.caption("Fraudster (OpenAI GPT-5.4) vs KYC Agent (Claude) — automated stress test")

    # Check API keys
    missing_keys = []
    if not os.getenv("ANTHROPIC_API_KEY"):
        missing_keys.append("ANTHROPIC_API_KEY")
    if not os.getenv("OPENAI_API_KEY"):
        missing_keys.append("OPENAI_API_KEY")

    if missing_keys:
        st.error(f"Missing API keys: {', '.join(missing_keys)}")
        st.code("\n".join(f"export {k}='...'" for k in missing_keys), language="bash")
        st.stop()

    # ── Setup panel ──
    if not st.session_state.duel_running and not st.session_state.duel_complete:

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
                placeholder="e.g. Nigerian man running a cleaning company in Manchester, revenue about £200k, wants to launder money through the account...",
                height=100,
                key="duel_hints",
            )

        col1, col2 = st.columns([1, 3])
        with col1:
            if st.button("⚔️ Start Duel", type="primary", use_container_width=True):
                with st.spinner("Setting up agents..."):
                    duel = DuelOrchestrator()
                    duel.setup(hints=hints if hints else None)
                    st.session_state.duel = duel
                    st.session_state.duel_legend = duel.fraudster.legend
                    st.session_state.duel_running = True
                    st.rerun()

    # ── Running / Complete ──
    if st.session_state.duel_running and not st.session_state.duel_complete:
        duel = st.session_state.duel

        # Show legend in sidebar
        if st.session_state.duel_legend:
            render_legend_sidebar(st.session_state.duel_legend)

        st.markdown("### ⚔️ Duel in Progress...")

        with st.spinner("Agents are talking... This takes a few minutes."):
            events = run_duel_sync(duel)
            st.session_state.duel_events = events
            st.session_state.duel_running = False
            st.session_state.duel_complete = True
            st.rerun()

    # ── Show results ──
    if st.session_state.duel_complete:
        duel = st.session_state.duel

        # Legend sidebar
        if st.session_state.duel_legend:
            render_legend_sidebar(st.session_state.duel_legend)

        # Render conversation + reasoning in two columns
        chat_col, reasoning_col = st.columns([3, 2])

        messages = [e for e in st.session_state.duel_events if e.event_type in ("message", "document", "legend")]
        reasoning_events = [e for e in st.session_state.duel_events if e.event_type == "reasoning"]
        assessment_events = [e for e in st.session_state.duel_events if e.event_type == "assessment"]

        with chat_col:
            st.markdown("### 💬 Conversation")
            for event in messages:
                render_event(event)

            # Assessment
            for event in assessment_events:
                render_assessment(event)

        with reasoning_col:
            st.markdown("### 🧠 KYC Reasoning")
            if reasoning_events:
                for event in reasoning_events:
                    render_event(event)
            else:
                st.caption("No reasoning entries captured.")

            # Verification details from orchestrator
            if duel and duel.kyc_orchestrator:
                case = duel.case
                if case and case.verifications:
                    st.markdown("### 🔍 Verifications")
                    for v in case.verifications:
                        source = v.get("source", "?")
                        query = v.get("query", "")
                        result = v.get("result", {})
                        status = result.get("status", "?") if isinstance(result, dict) else "?"
                        with st.expander(f"{source}: {query[:50]}"):
                            st.json(v)

        # ── Stats and download ──
        st.markdown("---")
        stat_cols = st.columns(4)

        msg_count = len([e for e in messages if e.event_type == "message"])
        kyc_msgs = len([e for e in messages if e.source == "kyc" and e.event_type == "message"])
        fraud_msgs = len([e for e in messages if e.source == "fraudster" and e.event_type == "message"])
        docs_generated = len([e for e in st.session_state.duel_events if e.event_type == "document"])

        stat_cols[0].metric("Total Messages", msg_count)
        stat_cols[1].metric("KYC Questions", kyc_msgs)
        stat_cols[2].metric("Fraudster Answers", fraud_msgs)
        stat_cols[3].metric("Fake Docs", docs_generated)

        # Downloads
        dl_col1, dl_col2, dl_col3 = st.columns(3)
        with dl_col1:
            transcript = json.dumps(
                [e.to_dict() for e in st.session_state.duel_events],
                indent=2, ensure_ascii=False,
            )
            st.download_button("📥 Full Transcript",
                               data=transcript,
                               file_name=f"duel_{duel.case_id}.json",
                               mime="application/json",
                               use_container_width=True)
        with dl_col2:
            if st.session_state.duel_legend:
                st.download_button("📥 Legend",
                                   data=json.dumps(st.session_state.duel_legend, indent=2, ensure_ascii=False),
                                   file_name=f"legend_{duel.case_id}.json",
                                   mime="application/json",
                                   use_container_width=True)
        with dl_col3:
            if st.button("🔄 New Duel", type="secondary", use_container_width=True):
                reset_duel()
                st.rerun()


if __name__ == "__main__":
    main()
