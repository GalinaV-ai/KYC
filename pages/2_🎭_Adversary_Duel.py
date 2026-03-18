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
    .turn-divider {
        border-top: 1px dashed #ccc; margin: 12px 0 8px 0;
        font-size: 0.72em; color: #aaa; text-align: center;
    }
</style>
""", unsafe_allow_html=True)


# ─── Session state ───

def init_duel_state():
    defaults = {
        "duel": None,
        "duel_events": [],       # list of DuelEvent
        "duel_running": False,
        "duel_complete": False,
        "duel_legend": None,
        "duel_step": 0,          # current step in incremental execution
    }
    for key, val in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = val


def reset_duel():
    for key in list(st.session_state.keys()):
        if key.startswith("duel"):
            del st.session_state[key]
    init_duel_state()


# ─── Incremental execution ───
# Instead of running the entire duel and showing nothing until done,
# we run ONE step per Streamlit rerun cycle. Each step = one exchange.
# Events accumulate in session_state and render immediately.

async def run_one_step(duel: DuelOrchestrator) -> list[DuelEvent]:
    """Execute one step of the duel: fraudster reply + KYC processing. Returns new events."""
    new_events = []

    # If no events yet, start the interview
    if not duel.events:
        greeting = await duel.kyc_orchestrator.start_interview(business_stage="existing")
        event = DuelEvent("message", "kyc", greeting)
        duel.events.append(event)
        new_events.append(event)
        return new_events

    # If interview is complete, run assessment
    if duel.kyc_orchestrator.interview_complete and not duel.is_complete:
        assessment = await duel.kyc_orchestrator.run_assessment()
        event = DuelEvent(
            "assessment", "kyc",
            json.dumps(assessment, indent=2, ensure_ascii=False),
            metadata={"decision": assessment.get("decision", "unknown")},
        )
        duel.events.append(event)
        new_events.append(event)
        duel.is_complete = True
        return new_events

    # Normal turn: fraudster responds, then KYC processes
    if duel.is_complete:
        return new_events

    # Get last KYC message
    last_kyc = None
    for e in reversed(duel.events):
        if e.source == "kyc" and e.event_type == "message":
            last_kyc = e.content
            break

    if not last_kyc:
        return new_events

    # Fraudster responds
    fraudster_reply = duel.fraudster.respond(last_kyc)
    event = DuelEvent("message", "fraudster", fraudster_reply)
    duel.events.append(event)
    new_events.append(event)

    # Check for document generation
    if duel.fraudster.should_offer_document(last_kyc):
        try:
            from adversary.doc_generator import generate_fake_document
            doc_type = duel._infer_doc_type(last_kyc)
            doc_path = generate_fake_document(
                doc_type=doc_type,
                legend=duel.fraudster.legend,
                output_dir=duel.doc_output_dir,
                context=last_kyc,
            )
            doc_event = DuelEvent(
                "document", "fraudster",
                f"Generated fake {doc_type}: {os.path.basename(doc_path)}",
                metadata={"doc_type": doc_type, "path": doc_path},
            )
            duel.events.append(doc_event)
            new_events.append(doc_event)

            doc_response = await duel.kyc_orchestrator.process_document_upload(doc_path, doc_type)
            doc_ack = DuelEvent("message", "kyc", doc_response,
                                metadata={"type": "doc_acknowledgement"})
            duel.events.append(doc_ack)
            new_events.append(doc_ack)
        except Exception as e:
            err = DuelEvent("system", "system", f"Doc generation failed: {e}")
            duel.events.append(err)
            new_events.append(err)

    # Yield reasoning snapshot
    reasoning = duel.kyc_orchestrator.get_reasoning_log()
    if reasoning:
        latest = reasoning[-1]
        r_event = DuelEvent("reasoning", "kyc", json.dumps(latest, ensure_ascii=False),
                            metadata={"full_log_length": len(reasoning)})
        duel.events.append(r_event)
        new_events.append(r_event)

    # KYC processes fraudster's answer
    kyc_response = await duel.kyc_orchestrator.process_customer_input(fraudster_reply)

    is_done = duel.kyc_orchestrator.interview_complete
    event = DuelEvent("message", "kyc", kyc_response,
                      metadata={"interview_complete": is_done} if is_done else {})
    duel.events.append(event)
    new_events.append(event)

    # Safety limit
    msg_count = sum(1 for e in duel.events if e.event_type == "message" and e.source == "fraudster")
    if msg_count >= 40:
        duel.kyc_orchestrator.interview_complete = True

    return new_events


def run_one_step_sync(duel: DuelOrchestrator) -> list[DuelEvent]:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(run_one_step(duel))
    finally:
        loop.close()


# ─── Rendering ───

def render_event(event):
    """Render a single duel event."""
    if isinstance(event, dict):
        etype = event.get("event_type", "")
        source = event.get("source", "")
        content = event.get("content", "")
        metadata = event.get("metadata", {})
    else:
        etype = event.event_type
        source = event.source
        content = event.content
        metadata = event.metadata

    if etype == "message":
        if source == "kyc":
            st.markdown(f'<div class="kyc-msg"><b>🏦 KYC Agent:</b><br>{content}</div>',
                        unsafe_allow_html=True)
        elif source == "fraudster":
            st.markdown(f'<div class="fraud-msg"><b>🎭 Fraudster:</b><br>{content}</div>',
                        unsafe_allow_html=True)

    elif etype == "document":
        doc_name = os.path.basename(metadata.get("path", "document.pdf"))
        st.markdown(f'<div class="doc-badge">📄 <b>Fake doc:</b> {doc_name} ({metadata.get("doc_type", "")})</div>',
                    unsafe_allow_html=True)

    elif etype == "legend":
        st.markdown(f'<div class="system-msg">🎭 {content}</div>',
                    unsafe_allow_html=True)

    elif etype == "system":
        st.markdown(f'<div class="system-msg">⚙️ {content}</div>',
                    unsafe_allow_html=True)


def render_reasoning_event(event):
    """Render a reasoning event in the right panel."""
    if isinstance(event, dict):
        content = event.get("content", "")
    else:
        content = event.content

    try:
        reasoning = json.loads(content)
        note = reasoning.get("note", "")
        suspicion = reasoning.get("suspicion", "none")
        if note:
            st.markdown(f'<div class="reasoning-card">🧠 {note} <i>(suspicion: {suspicion})</i></div>',
                        unsafe_allow_html=True)
    except (json.JSONDecodeError, TypeError):
        pass


def render_assessment(event):
    """Render the final assessment with verdict."""
    content = event.content if hasattr(event, "content") else event.get("content", "")
    metadata = event.metadata if hasattr(event, "metadata") else event.get("metadata", {})

    try:
        assessment = json.loads(content)
    except json.JSONDecodeError:
        st.error("Failed to parse assessment")
        return

    decision = assessment.get("decision", assessment.get("recommendation", "unknown")).lower()

    st.markdown("---")
    st.markdown("## Final Verdict")

    if "approve" in decision:
        st.markdown('<div class="verdict-approve">✅ <b>APPROVED</b> — The fraudster passed!</div>',
                    unsafe_allow_html=True)
    elif "decline" in decision or "reject" in decision:
        st.markdown('<div class="verdict-decline">❌ <b>DECLINED</b> — The fraudster was caught!</div>',
                    unsafe_allow_html=True)
    elif "escalat" in decision:
        st.markdown('<div class="verdict-escalate">⚠️ <b>ESCALATED</b> — Sent for manual review</div>',
                    unsafe_allow_html=True)
    else:
        st.info(f"Decision: {decision}")

    with st.expander("Full Assessment", expanded=False):
        risk_scores = assessment.get("risk_scores", {})
        if risk_scores:
            cols = st.columns(min(len(risk_scores), 4))
            for col, (key, val) in zip(cols, risk_scores.items()):
                label = key.replace("_", " ").title()
                score = val if isinstance(val, (int, float)) else 0
                col.metric(label, f"{int(score * 100)}%" if isinstance(score, float) else str(score))

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
        st.caption("The fraudster (GPT-5.4) sees ONLY the interviewer's messages — no reasoning, no verification results, no internal prompts.")


def render_debug_downloads(duel):
    """Render JSON download buttons for debugging."""
    st.markdown("---")
    st.markdown("### 📥 Debug Downloads")
    dl_cols = st.columns(4)

    events_data = [e.to_dict() for e in duel.events]

    # 1. Full transcript (all events)
    with dl_cols[0]:
        st.download_button(
            "Full Transcript",
            data=json.dumps(events_data, indent=2, ensure_ascii=False),
            file_name=f"duel_{duel.case_id}_transcript.json",
            mime="application/json",
            use_container_width=True,
        )

    # 2. Legend
    with dl_cols[1]:
        if duel.fraudster and duel.fraudster.legend:
            st.download_button(
                "Legend",
                data=json.dumps(duel.fraudster.legend, indent=2, ensure_ascii=False),
                file_name=f"duel_{duel.case_id}_legend.json",
                mime="application/json",
                use_container_width=True,
            )

    # 3. KYC Case JSON
    with dl_cols[2]:
        if duel.case:
            st.download_button(
                "KYC Case",
                data=json.dumps(duel.case.to_dict(), indent=2, ensure_ascii=False),
                file_name=f"duel_{duel.case_id}_case.json",
                mime="application/json",
                use_container_width=True,
            )

    # 4. Reasoning log
    with dl_cols[3]:
        if duel.kyc_orchestrator:
            reasoning_log = duel.kyc_orchestrator.get_reasoning_log()
            if reasoning_log:
                st.download_button(
                    "Reasoning Log",
                    data=json.dumps(reasoning_log, indent=2, ensure_ascii=False),
                    file_name=f"duel_{duel.case_id}_reasoning.json",
                    mime="application/json",
                    use_container_width=True,
                )

    # 5. Assessment (separate row)
    assessment_events = [e for e in duel.events if e.event_type == "assessment"]
    if assessment_events:
        dl_cols2 = st.columns(4)
        with dl_cols2[0]:
            st.download_button(
                "Assessment",
                data=assessment_events[-1].content,
                file_name=f"duel_{duel.case_id}_assessment.json",
                mime="application/json",
                use_container_width=True,
            )


# ─── Main page ───

def main():
    init_duel_state()

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

    # ── Setup panel (before duel starts) ──
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
                placeholder="e.g. Nigerian man running a cleaning company in Manchester, revenue about £200k...",
                height=100,
                key="duel_hints",
            )

        col1, col2 = st.columns([1, 3])
        with col1:
            if st.button("⚔️ Start Duel", type="primary", use_container_width=True):
                with st.spinner("Setting up agents & generating legend..."):
                    duel = DuelOrchestrator()
                    duel.setup(hints=hints if hints else None)
                    st.session_state.duel = duel
                    st.session_state.duel_legend = duel.fraudster.legend
                    st.session_state.duel_events = []
                    st.session_state.duel_running = True
                    st.session_state.duel_complete = False
                    st.session_state.duel_step = 0
                    st.rerun()
        return

    # ── Duel is running or complete — show everything ──
    duel = st.session_state.duel

    # Sidebar: legend + stop button
    if st.session_state.duel_legend:
        render_legend_sidebar(st.session_state.duel_legend)

    with st.sidebar:
        st.markdown("---")
        if st.session_state.duel_running and not st.session_state.duel_complete:
            if st.button("⏹ Stop Duel", type="secondary", use_container_width=True):
                duel.kyc_orchestrator.interview_complete = True
                duel.is_complete = True
                st.session_state.duel_running = False
                st.session_state.duel_complete = True
                st.rerun()

        if st.session_state.duel_complete:
            if st.button("🔄 New Duel", type="secondary", use_container_width=True):
                reset_duel()
                st.rerun()

    # ── If still running, execute ONE step then rerun ──
    if st.session_state.duel_running and not st.session_state.duel_complete:
        # Show status
        fraud_msgs = sum(1 for e in duel.events if e.event_type == "message" and e.source == "fraudster")
        st.info(f"⚔️ Duel in progress... Turn {fraud_msgs + 1}")

        # Render all events so far
        _render_all_events(duel)

        # Run next step
        with st.spinner("Agents are thinking..."):
            new_events = run_one_step_sync(duel)

        # Check if duel is done
        has_assessment = any(e.event_type == "assessment" for e in duel.events)
        if has_assessment or duel.is_complete:
            st.session_state.duel_running = False
            st.session_state.duel_complete = True

        st.session_state.duel_step += 1
        st.rerun()

    # ── Duel complete — final render ──
    if st.session_state.duel_complete:
        decision = None
        for e in duel.events:
            if e.event_type == "assessment":
                try:
                    a = json.loads(e.content)
                    decision = a.get("decision", a.get("recommendation", "?"))
                except Exception:
                    decision = "?"

        if decision:
            st.success(f"Duel complete — Decision: **{decision}**")
        else:
            st.success("Duel complete")

        _render_all_events(duel)

        # Stats
        st.markdown("---")
        stat_cols = st.columns(4)
        msgs = [e for e in duel.events if e.event_type == "message"]
        kyc_msgs = [e for e in msgs if e.source == "kyc"]
        fraud_msgs = [e for e in msgs if e.source == "fraudster"]
        docs = [e for e in duel.events if e.event_type == "document"]

        stat_cols[0].metric("Total Messages", len(msgs))
        stat_cols[1].metric("KYC Messages", len(kyc_msgs))
        stat_cols[2].metric("Fraudster Answers", len(fraud_msgs))
        stat_cols[3].metric("Fake Docs", len(docs))

        # Debug downloads
        render_debug_downloads(duel)


def _render_all_events(duel):
    """Render full conversation + reasoning in two columns."""
    chat_col, reasoning_col = st.columns([3, 2])

    messages = [e for e in duel.events if e.event_type in ("message", "document", "legend", "system")]
    reasoning_events = [e for e in duel.events if e.event_type == "reasoning"]
    assessment_events = [e for e in duel.events if e.event_type == "assessment"]

    with chat_col:
        st.markdown("### 💬 Conversation")
        for event in messages:
            render_event(event)

        for event in assessment_events:
            render_assessment(event)

    with reasoning_col:
        st.markdown("### 🧠 KYC Reasoning")
        if reasoning_events:
            for event in reasoning_events:
                render_reasoning_event(event)
        else:
            st.caption("Waiting for first verification cycle...")

        # Verifications
        if duel.kyc_orchestrator:
            case = duel.case
            if case and case.verifications:
                st.markdown("### 🔍 Verifications")
                for v in case.verifications:
                    source = v.get("source", "?")
                    query = v.get("query", "")
                    with st.expander(f"{source}: {query[:50]}"):
                        st.json(v)


if __name__ == "__main__":
    main()
