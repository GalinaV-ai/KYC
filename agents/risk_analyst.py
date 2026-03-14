"""
Risk Analyst Agent — produces the final compliance report after interview completion.

Receives:
  - Full case data (KYCCase)
  - Interviewer reasoning log
  - All assessments from the Assessor
  - All verification results from the Verification Engine
  - Sanctions screening results

Produces:
  - Structured risk assessment with scores and decision
"""
import json
from datetime import datetime
from typing import Optional

import anthropic
from anthropic import AsyncAnthropic

from agents.prompts import RISK_ANALYST_PROMPT
from models import KYCCase

MODEL_ANALYST = "claude-sonnet-4-20250514"


async def run_risk_assessment(
    case: KYCCase,
    reasoning_log: list = None,
    assessor_findings: dict = None,
    verification_engine_summary: dict = None,
    verification_results: list = None,
    sanctions_results: dict = None,
) -> dict:
    """Run the final risk assessment on a completed case.

    Args:
        case: The KYCCase with all interview data
        reasoning_log: Interviewer's internal reasoning entries
        assessor_findings: All assessments from the Assessor agent
        verification_engine_summary: Summary from the Verification Engine
        verification_results: Raw verification results
        sanctions_results: Sanctions screening results

    Returns:
        Structured risk assessment dict
    """
    client = AsyncAnthropic()
    case_data = case.to_dict()

    extra_context = ""

    if reasoning_log:
        extra_context += (
            f"\n\nINTERVIEWER REASONING LOG (internal notes from the interviewing agent):\n"
            f"{json.dumps(reasoning_log, indent=2, ensure_ascii=False, default=str)}"
        )

    if assessor_findings:
        extra_context += (
            f"\n\nASSESSOR FINDINGS (verification assessment results):\n"
            f"{json.dumps(assessor_findings, indent=2, ensure_ascii=False, default=str)}"
        )

    if verification_engine_summary:
        extra_context += (
            f"\n\nVERIFICATION ENGINE SUMMARY (automated checks overview):\n"
            f"{json.dumps(verification_engine_summary, indent=2, ensure_ascii=False, default=str)}"
        )

    if verification_results:
        # Include detailed results but cap to avoid token limits
        capped = verification_results[:50]  # Cap at 50 most recent results
        extra_context += (
            f"\n\nDETAILED VERIFICATION RESULTS ({len(verification_results)} total, showing {len(capped)}):\n"
            f"{json.dumps(capped, indent=2, ensure_ascii=False, default=str)}"
        )

    if sanctions_results:
        extra_context += (
            f"\n\nSANCTIONS SCREENING RESULTS:\n"
            f"{json.dumps(sanctions_results, indent=2, ensure_ascii=False, default=str)}"
        )

    response = await client.messages.create(
        model=MODEL_ANALYST,
        max_tokens=6000,
        system=RISK_ANALYST_PROMPT,
        messages=[{
            "role": "user",
            "content": f"""Assess this KYC case.

COMPLETE CASE DATA:
{json.dumps(case_data, indent=2, ensure_ascii=False)}
{extra_context}

Return your assessment as a JSON object."""
        }]
    )

    result_text = response.content[0].text
    try:
        json_start = result_text.find('{')
        json_end = result_text.rfind('}') + 1
        if json_start >= 0 and json_end > json_start:
            return json.loads(result_text[json_start:json_end])
    except json.JSONDecodeError:
        pass

    return {"raw_assessment": result_text, "error": "Could not parse structured assessment"}
