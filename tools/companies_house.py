"""
Companies House API integration.
Free API: https://developer.company-information.service.gov.uk/
Provides company search, officer lookup, filing history.
"""
import os
import httpx
from typing import Optional


API_KEY = os.getenv("COMPANIES_HOUSE_API_KEY", "")
BASE_URL = "https://api.company-information.service.gov.uk"


async def search_company(query: str, items_per_page: int = 5) -> dict:
    """Search for a company by name or number."""
    if not API_KEY:
        return {
            "error": "COMPANIES_HOUSE_API_KEY not set. Register at https://developer.company-information.service.gov.uk/",
            "mock_mode": True,
            "note": "Returning empty results. Set the API key in .env to use real data."
        }

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(
                f"{BASE_URL}/search/companies",
                params={"q": query, "items_per_page": items_per_page},
                auth=(API_KEY, ""),
                timeout=15
            )
            if resp.status_code == 200:
                return resp.json()
            else:
                return {"error": f"HTTP {resp.status_code}", "body": resp.text}
        except Exception as e:
            return {"error": str(e)}


async def get_company_profile(company_number: str) -> dict:
    """Get detailed company profile by company number."""
    if not API_KEY:
        return {"error": "COMPANIES_HOUSE_API_KEY not set", "mock_mode": True}

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(
                f"{BASE_URL}/company/{company_number}",
                auth=(API_KEY, ""),
                timeout=15
            )
            if resp.status_code == 200:
                return resp.json()
            else:
                return {"error": f"HTTP {resp.status_code}", "body": resp.text}
        except Exception as e:
            return {"error": str(e)}


async def get_company_officers(company_number: str) -> dict:
    """Get officers (directors, secretaries) for a company."""
    if not API_KEY:
        return {"error": "COMPANIES_HOUSE_API_KEY not set", "mock_mode": True}

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(
                f"{BASE_URL}/company/{company_number}/officers",
                auth=(API_KEY, ""),
                timeout=15
            )
            if resp.status_code == 200:
                return resp.json()
            else:
                return {"error": f"HTTP {resp.status_code}", "body": resp.text}
        except Exception as e:
            return {"error": str(e)}


async def get_filing_history(company_number: str, items_per_page: int = 10) -> dict:
    """Get recent filing history for a company."""
    if not API_KEY:
        return {"error": "COMPANIES_HOUSE_API_KEY not set", "mock_mode": True}

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(
                f"{BASE_URL}/company/{company_number}/filing-history",
                params={"items_per_page": items_per_page},
                auth=(API_KEY, ""),
                timeout=15
            )
            if resp.status_code == 200:
                return resp.json()
            else:
                return {"error": f"HTTP {resp.status_code}", "body": resp.text}
        except Exception as e:
            return {"error": str(e)}


async def get_persons_with_significant_control(company_number: str) -> dict:
    """Get PSC (persons with significant control) register."""
    if not API_KEY:
        return {"error": "COMPANIES_HOUSE_API_KEY not set", "mock_mode": True}

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(
                f"{BASE_URL}/company/{company_number}/persons-with-significant-control",
                auth=(API_KEY, ""),
                timeout=15
            )
            if resp.status_code == 200:
                return resp.json()
            else:
                return {"error": f"HTTP {resp.status_code}", "body": resp.text}
        except Exception as e:
            return {"error": str(e)}
