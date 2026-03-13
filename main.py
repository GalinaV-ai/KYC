#!/usr/bin/env python3
"""
KYC Onboarding Agent — CLI Interface
UK Bank KYC interview system for small business customers.

Usage:
    python main.py                    # Start a new KYC interview
    python main.py --resume CASE_ID   # Resume an existing interview
    python main.py --review CASE_ID   # Review a completed case

Requires:
    ANTHROPIC_API_KEY environment variable
    Optionally: COMPANIES_HOUSE_API_KEY for Companies House verification
"""
import asyncio
import argparse
import json
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path

# Add parent dir to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from models import KYCCase, PersonInfo, BusinessInfo, BusinessActivity
from agents.orchestrator import KYCOrchestrator, run_risk_assessment
import anthropic


# Directory for case files
CASES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cases")
os.makedirs(CASES_DIR, exist_ok=True)


# Terminal colors
class C:
    BLUE = '\033[94m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    CYAN = '\033[96m'
    BOLD = '\033[1m'
    DIM = '\033[2m'
    END = '\033[0m'


def print_banner():
    print(f"""
{C.CYAN}{C.BOLD}╔══════════════════════════════════════════════════════════════╗
║                  KYC ONBOARDING AGENT                        ║
║              UK Small Business Banking                       ║
╚══════════════════════════════════════════════════════════════╝{C.END}
""")


def print_agent(message: str):
    """Print agent's message to customer."""
    print(f"\n{C.BLUE}{C.BOLD}🏦 KYC Agent:{C.END}")
    # Word wrap for readability
    words = message.split()
    line = "   "
    for word in words:
        if len(line) + len(word) + 1 > 80:
            print(line)
            line = "   " + word
        else:
            line += " " + word if line.strip() else "   " + word
    if line.strip():
        print(line)
    print()


def print_system(message: str):
    """Print system message (not from agent or customer)."""
    print(f"{C.DIM}  [{message}]{C.END}")


def print_phase(phase: str):
    """Print phase transition."""
    labels = {
        "identification": "📋 IDENTIFICATION",
        "business_basics": "🏢 BUSINESS BASICS",
        "business_understanding": "🔍 BUSINESS UNDERSTANDING",
        "document_collection": "📄 DOCUMENT COLLECTION",
        "verification": "✅ VERIFICATION",
        "deep_probing": "🕵️ DEEP PROBING",
        "risk_assessment": "⚖️ RISK ASSESSMENT",
    }
    label = labels.get(phase, phase.upper())
    print(f"\n{C.YELLOW}{'─' * 60}")
    print(f"  Phase: {label}")
    print(f"{'─' * 60}{C.END}\n")


def print_risk_assessment(assessment: dict):
    """Pretty-print the risk assessment result."""
    print(f"\n{C.BOLD}{'═' * 60}")
    print(f"            RISK ASSESSMENT REPORT")
    print(f"{'═' * 60}{C.END}\n")

    risk = assessment.get("overall_risk_level", "unknown").upper()
    decision = assessment.get("decision", "unknown").upper()
    confidence = assessment.get("confidence_score", 0)

    color = C.GREEN if risk == "LOW" else C.YELLOW if risk == "MEDIUM" else C.RED
    print(f"  Risk Level:  {color}{C.BOLD}{risk}{C.END}")
    print(f"  Decision:    {color}{C.BOLD}{decision}{C.END}")
    print(f"  Confidence:  {confidence:.0%}")

    scores = {
        "Identity": assessment.get("identity_score", "N/A"),
        "Business Legitimacy": assessment.get("business_legitimacy_score", "N/A"),
        "Business Understanding": assessment.get("business_understanding_score", "N/A"),
        "Consistency": assessment.get("consistency_score", "N/A"),
    }
    print(f"\n  {C.BOLD}Scores:{C.END}")
    for name, score in scores.items():
        if isinstance(score, (int, float)):
            bar = "█" * int(score * 20) + "░" * (20 - int(score * 20))
            print(f"    {name:<25} [{bar}] {score:.0%}")
        else:
            print(f"    {name:<25} {score}")

    flags = assessment.get("red_flags", [])
    if flags:
        print(f"\n  {C.RED}{C.BOLD}Red Flags:{C.END}")
        for flag in flags:
            if isinstance(flag, dict):
                sev = flag.get("severity", "").upper()
                desc = flag.get("flag", flag.get("description", str(flag)))
                print(f"    {C.RED}⚠ [{sev}]{C.END} {desc}")
            else:
                print(f"    {C.RED}⚠{C.END} {flag}")

    positives = assessment.get("positive_indicators", [])
    if positives:
        print(f"\n  {C.GREEN}{C.BOLD}Positive Indicators:{C.END}")
        for p in positives:
            print(f"    {C.GREEN}✓{C.END} {p}")

    summary = assessment.get("summary", "")
    if summary:
        print(f"\n  {C.BOLD}Summary:{C.END}")
        print(f"    {summary[:500]}")

    recommendation = assessment.get("recommendation", "")
    if recommendation:
        print(f"\n  {C.BOLD}Recommendation:{C.END}")
        print(f"    {recommendation[:300]}")

    print(f"\n{'═' * 60}\n")


def get_customer_input() -> str:
    """Get input from the customer (user playing the customer role)."""
    try:
        user_input = input(f"{C.GREEN}👤 Customer: {C.END}").strip()
        return user_input
    except (EOFError, KeyboardInterrupt):
        return "/quit"


def get_document_path(doc_type: str, reason: str) -> str:
    """Prompt user to provide a document file path."""
    print(f"\n{C.YELLOW}📎 Document requested: {doc_type}{C.END}")
    print(f"   Reason: {reason}")
    print(f"   {C.DIM}(Enter the file path to the document, or 'skip' to skip){C.END}")

    while True:
        path = input(f"   File path: ").strip()
        if path.lower() == 'skip':
            return ""
        if path and os.path.exists(path):
            return path
        if path:
            print(f"   {C.RED}File not found: {path}{C.END}")
        else:
            return ""


async def run_interview(case_id: str = None, resume: bool = False):
    """Run the KYC interview loop."""
    # Create or load case
    if resume and case_id:
        case_path = os.path.join(CASES_DIR, f"{case_id}.json")
        if not os.path.exists(case_path):
            print(f"{C.RED}Case not found: {case_id}{C.END}")
            return
        case_data = KYCCase.load(case_path)
        case = KYCCase(**{k: v for k, v in case_data.items() if k != 'person' and k != 'business' and k != 'activity'})
        # Reconstruct nested objects
        case.person = PersonInfo(**case_data.get('person', {}))
        case.business = BusinessInfo(**case_data.get('business', {}))
        case.activity = BusinessActivity(**case_data.get('activity', {}))
        print_system(f"Resuming case: {case_id}")
    else:
        case_id = case_id or f"KYC-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
        case = KYCCase(
            case_id=case_id,
            created_at=datetime.now().isoformat(),
            updated_at=datetime.now().isoformat(),
        )

    case_path = os.path.join(CASES_DIR, f"{case_id}.json")
    case.save(case_path)

    print_system(f"Case ID: {case_id}")
    print_system(f"Case file: {case_path}")
    print_system("Type '/quit' to end, '/status' to see case data, '/phase' to see current phase")
    print()

    # Create orchestrator
    orchestrator = KYCOrchestrator(case, case_path)

    # Start the interview
    print_phase("identification")
    greeting = await orchestrator.start_interview()
    print_agent(greeting)
    case.add_conversation_entry("agent", greeting)

    # Main conversation loop
    previous_phase = case.current_phase
    while not orchestrator.interview_complete:
        # Check if there's a pending document request
        if orchestrator.pending_document_request:
            doc_req = orchestrator.pending_document_request
            doc_path = get_document_path(doc_req["doc_type"], doc_req["reason"])
            orchestrator.pending_document_request = None

            if doc_path:
                print_system(f"Processing document: {doc_path}")
                response = await orchestrator.process_document_upload(doc_path, doc_req["doc_type"])
                case.add_conversation_entry("system", f"Document uploaded: {doc_req['doc_type']} at {doc_path}")
                print_agent(response)
                case.add_conversation_entry("agent", response)
                continue
            else:
                # Customer skipped the document
                response = await orchestrator.process_customer_input(
                    f"[Customer chose not to provide the {doc_req['doc_type']} at this time]"
                )
                print_agent(response)
                case.add_conversation_entry("agent", response)
                continue

        # Get customer input
        user_input = get_customer_input()

        if not user_input:
            continue

        # Handle commands
        if user_input.startswith('/'):
            if user_input == '/quit':
                print_system("Interview ended by operator")
                break
            elif user_input == '/status':
                print(f"\n{C.DIM}{json.dumps(case.to_dict(), indent=2, ensure_ascii=False)}{C.END}\n")
                continue
            elif user_input == '/phase':
                print_system(f"Current phase: {case.current_phase}")
                continue
            elif user_input == '/flags':
                if case.red_flags:
                    for flag in case.red_flags:
                        print(f"  {C.RED}⚠ [{flag['severity']}] {flag['description']}{C.END}")
                else:
                    print_system("No red flags recorded")
                continue
            elif user_input.startswith('/doc '):
                # Manual document upload: /doc passport /path/to/file.jpg
                parts = user_input.split(' ', 2)
                if len(parts) == 3:
                    doc_type, file_path = parts[1], parts[2]
                    if os.path.exists(file_path):
                        print_system(f"Processing document: {file_path}")
                        response = await orchestrator.process_document_upload(file_path, doc_type)
                        print_agent(response)
                        case.add_conversation_entry("agent", response)
                    else:
                        print(f"  {C.RED}File not found: {file_path}{C.END}")
                else:
                    print_system("Usage: /doc <type> <file_path>")
                continue
            else:
                print_system(f"Unknown command: {user_input}")
                continue

        # Process customer response
        case.add_conversation_entry("customer", user_input)

        # Check for phase change
        if case.current_phase != previous_phase:
            print_phase(case.current_phase)
            previous_phase = case.current_phase

        response = await orchestrator.process_customer_input(user_input)

        # Check for phase change after response
        if case.current_phase != previous_phase:
            print_phase(case.current_phase)
            previous_phase = case.current_phase

        print_agent(response)
        case.add_conversation_entry("agent", response)

    # Run risk assessment if interview was completed
    if orchestrator.interview_complete:
        print(f"\n{C.CYAN}Running risk assessment...{C.END}\n")
        client = anthropic.AsyncAnthropic()
        assessment = await run_risk_assessment(
            case, client,
            reasoning_log=orchestrator.get_reasoning_log(),
            verification_findings=orchestrator.investigator.get_detailed_findings(),
            sanctions_results=orchestrator.investigator.sanctions_results,
        )

        case.risk_assessment = assessment
        case.current_phase = "completed"
        case.save(case_path)

        print_risk_assessment(assessment)
        print_system(f"Full case saved to: {case_path}")
    else:
        case.save(case_path)
        print_system(f"Interview paused. Resume with: python main.py --resume {case_id}")
        print_system(f"Case saved to: {case_path}")


async def review_case(case_id: str):
    """Review a completed or in-progress case."""
    case_path = os.path.join(CASES_DIR, f"{case_id}.json")
    if not os.path.exists(case_path):
        print(f"{C.RED}Case not found: {case_id}{C.END}")
        return

    with open(case_path, 'r') as f:
        case_data = json.load(f)

    print(f"\n{C.BOLD}Case: {case_id}{C.END}")
    print(f"Created: {case_data.get('created_at', 'N/A')}")
    print(f"Phase: {case_data.get('current_phase', 'N/A')}")
    print(f"\n{C.BOLD}Person:{C.END}")
    person = case_data.get('person', {})
    for k, v in person.items():
        if v:
            print(f"  {k}: {v}")

    print(f"\n{C.BOLD}Business:{C.END}")
    business = case_data.get('business', {})
    for k, v in business.items():
        if v:
            print(f"  {k}: {v}")

    print(f"\n{C.BOLD}Activity:{C.END}")
    activity = case_data.get('activity', {})
    for k, v in activity.items():
        if v:
            print(f"  {k}: {v}")

    flags = case_data.get('red_flags', [])
    if flags:
        print(f"\n{C.RED}{C.BOLD}Red Flags ({len(flags)}):{C.END}")
        for flag in flags:
            print(f"  ⚠ [{flag.get('severity', '')}] {flag.get('description', '')}")

    docs = case_data.get('documents', [])
    if docs:
        print(f"\n{C.BOLD}Documents ({len(docs)}):{C.END}")
        for doc in docs:
            print(f"  📄 {doc.get('doc_type', '')} — {doc.get('file_path', '')}")

    assessment = case_data.get('risk_assessment')
    if assessment:
        print_risk_assessment(assessment)
    else:
        print(f"\n{C.YELLOW}No risk assessment yet.{C.END}")
        # Offer to run one
        if input("Run risk assessment now? (y/n): ").strip().lower() == 'y':
            case = KYCCase(**{k: v for k, v in case_data.items()
                            if k not in ('person', 'business', 'activity')})
            case.person = PersonInfo(**case_data.get('person', {}))
            case.business = BusinessInfo(**case_data.get('business', {}))
            case.activity = BusinessActivity(**case_data.get('activity', {}))

            client = anthropic.AsyncAnthropic()
            assessment = await run_risk_assessment(case, client)
            case.risk_assessment = assessment
            case.save(case_path)
            print_risk_assessment(assessment)


def main():
    parser = argparse.ArgumentParser(description="KYC Onboarding Agent")
    parser.add_argument("--resume", type=str, help="Resume an existing case by ID")
    parser.add_argument("--review", type=str, help="Review a completed case by ID")
    parser.add_argument("--list", action="store_true", help="List all cases")
    args = parser.parse_args()

    # Check for API key
    if not os.getenv("ANTHROPIC_API_KEY"):
        print(f"{C.RED}Error: ANTHROPIC_API_KEY environment variable not set.{C.END}")
        print(f"Get your API key at: https://console.anthropic.com/")
        print(f"Then run: export ANTHROPIC_API_KEY='your-key-here'")
        sys.exit(1)

    print_banner()

    if args.list:
        cases = sorted(Path(CASES_DIR).glob("*.json"))
        if not cases:
            print("No cases found.")
        for case_file in cases:
            with open(case_file) as f:
                data = json.load(f)
            status = "✅" if data.get("current_phase") == "completed" else "⏳"
            name = data.get("person", {}).get("full_name", "Unknown")
            biz = data.get("business", {}).get("company_name", "Unknown")
            print(f"  {status} {case_file.stem}  |  {name}  |  {biz}  |  Phase: {data.get('current_phase', '?')}")
        return

    if args.review:
        asyncio.run(review_case(args.review))
    elif args.resume:
        asyncio.run(run_interview(case_id=args.resume, resume=True))
    else:
        asyncio.run(run_interview())


if __name__ == "__main__":
    main()
