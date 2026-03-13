"""
Data models for KYC onboarding agent.
Defines the structure of case data collected during the interview.
"""
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Optional
import json


class KYCPhase(str, Enum):
    IDENTIFICATION = "identification"
    BUSINESS_BASICS = "business_basics"
    BUSINESS_UNDERSTANDING = "business_understanding"
    DOCUMENT_COLLECTION = "document_collection"
    VERIFICATION = "verification"
    DEEP_PROBING = "deep_probing"
    RISK_ASSESSMENT = "risk_assessment"
    COMPLETED = "completed"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Decision(str, Enum):
    APPROVE = "approve"           # Pass to next stage
    MANUAL_REVIEW = "manual_review"  # Escalate to human
    REJECT = "reject"             # Clear red flags


@dataclass
class PersonInfo:
    full_name: str = ""
    date_of_birth: str = ""
    nationality: str = ""
    country_of_residence: str = ""
    residential_address: str = ""
    phone: str = ""
    email: str = ""
    id_document_type: str = ""  # passport, driving licence, etc.
    id_document_number: str = ""
    role_in_business: str = ""  # director, shareholder, etc.


@dataclass
class BusinessInfo:
    company_name: str = ""
    trading_name: str = ""
    company_number: str = ""  # Companies House number
    company_type: str = ""    # Ltd, LLP, sole trader, etc.
    incorporation_date: str = ""
    registered_address: str = ""
    trading_address: str = ""
    industry_sector: str = ""
    sic_codes: list = field(default_factory=list)
    website: str = ""
    social_media: list = field(default_factory=list)
    description: str = ""


@dataclass
class BusinessActivity:
    products_services: str = ""
    target_customers: str = ""
    customer_location: str = ""  # UK, international, etc.
    supplier_info: str = ""
    revenue_model: str = ""
    monthly_turnover_expected: str = ""
    annual_turnover_expected: str = ""
    number_of_employees: str = ""
    payment_methods: str = ""  # card, bank transfer, cash, etc.
    cash_intensive: bool = False
    international_transactions: bool = False
    countries_involved: list = field(default_factory=list)


@dataclass
class DocumentRecord:
    doc_type: str = ""          # passport, utility_bill, certificate_of_incorporation, etc.
    file_path: str = ""
    upload_time: str = ""
    extracted_data: dict = field(default_factory=dict)
    analysis_notes: str = ""
    verified: bool = False


@dataclass
class VerificationResult:
    source: str = ""            # companies_house, web_search, document_cross_check
    query: str = ""
    result: dict = field(default_factory=dict)
    findings: str = ""
    flags: list = field(default_factory=list)
    timestamp: str = ""


@dataclass
class RedFlag:
    category: str = ""          # inconsistency, missing_info, suspicious_pattern, etc.
    severity: str = ""          # low, medium, high
    description: str = ""
    evidence: str = ""


@dataclass
class RiskAssessment:
    overall_risk: str = ""
    decision: str = ""
    confidence_score: float = 0.0  # 0-1
    red_flags: list = field(default_factory=list)
    positive_indicators: list = field(default_factory=list)
    summary: str = ""
    recommendation: str = ""
    assessed_at: str = ""


@dataclass
class KYCCase:
    case_id: str = ""
    created_at: str = ""
    updated_at: str = ""
    current_phase: str = KYCPhase.IDENTIFICATION.value
    person: PersonInfo = field(default_factory=PersonInfo)
    business: BusinessInfo = field(default_factory=BusinessInfo)
    activity: BusinessActivity = field(default_factory=BusinessActivity)
    documents: list = field(default_factory=list)
    verifications: list = field(default_factory=list)
    conversation_log: list = field(default_factory=list)
    red_flags: list = field(default_factory=list)
    risk_assessment: Optional[dict] = None

    def to_dict(self):
        d = asdict(self)
        return d

    def save(self, path: str):
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)

    @classmethod
    def load(cls, path: str):
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)

    def add_conversation_entry(self, role: str, content: str):
        self.conversation_log.append({
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat()
        })
        self.updated_at = datetime.now().isoformat()

    def add_red_flag(self, category: str, severity: str, description: str, evidence: str = ""):
        self.red_flags.append({
            "category": category,
            "severity": severity,
            "description": description,
            "evidence": evidence,
            "detected_at": datetime.now().isoformat()
        })
