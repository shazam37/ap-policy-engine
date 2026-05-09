"""
Core Pydantic models for the AP Policy Engine.
All data structures used across the entire pipeline are defined here.
"""
from __future__ import annotations
from enum import Enum
from typing import Any, Literal
from pydantic import BaseModel, Field, field_validator, model_validator
from datetime import datetime


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class RuleAction(str, Enum):
    AUTO_APPROVE = "AUTO_APPROVE"
    ROUTE_TO_DEPT_HEAD = "ROUTE_TO_DEPT_HEAD"
    ESCALATE_TO_FINANCE_CONTROLLER = "ESCALATE_TO_FINANCE_CONTROLLER"
    ESCALATE_TO_CFO = "ESCALATE_TO_CFO"
    REJECT = "REJECT"
    HOLD = "HOLD"
    FLAG = "FLAG"
    COMPLIANCE_HOLD = "COMPLIANCE_HOLD"
    REQUEST_MANUAL_REVIEW = "REQUEST_MANUAL_REVIEW"
    SEND_NOTIFICATION = "SEND_NOTIFICATION"


class ConflictType(str, Enum):
    THRESHOLD_OVERLAP = "THRESHOLD_OVERLAP"
    CONTRADICTORY_ACTORS = "CONTRADICTORY_ACTORS"
    MISSING_ESCALATION = "MISSING_ESCALATION"
    DUPLICATE_RULE = "DUPLICATE_RULE"
    CIRCULAR_REFERENCE = "CIRCULAR_REFERENCE"


class Severity(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class SupplyType(str, Enum):
    INTRA_STATE = "intra_state"
    INTER_STATE = "inter_state"


class InvoiceStatus(str, Enum):
    PENDING = "PENDING"
    AUTO_APPROVED = "AUTO_APPROVED"
    ROUTED_DEPT_HEAD = "ROUTED_DEPT_HEAD"
    ESCALATED_FC = "ESCALATED_FC"
    ESCALATED_CFO = "ESCALATED_CFO"
    REJECTED = "REJECTED"
    HELD = "HELD"
    FLAGGED = "FLAGGED"
    COMPLIANCE_HOLD = "COMPLIANCE_HOLD"


class DeviationType(str, Enum):
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
    QUANTITY_MISMATCH = "QUANTITY_MISMATCH"
    RATE_MISMATCH = "RATE_MISMATCH"
    MISSING_GRN = "MISSING_GRN"
    TAX_ERROR = "TAX_ERROR"
    GSTIN_MISMATCH = "GSTIN_MISMATCH"
    DUPLICATE_INVOICE = "DUPLICATE_INVOICE"
    FUTURE_DATED = "FUTURE_DATED"
    QR_VALIDATION_FAILED = "QR_VALIDATION_FAILED"


# ---------------------------------------------------------------------------
# Document Parsing Models
# ---------------------------------------------------------------------------

class CrossReference(BaseModel):
    ref_text: str           # "Section 2.3(b)"
    section_id: str         # "2.3"
    sub_clause: str | None  # "b"


class Clause(BaseModel):
    section_id: str             # "2.2"
    sub_clause: str | None      # "c"
    full_ref: str               # "Section 2.2(c)"
    section_title: str          # "Purchase Order Matching"
    raw_text: str
    cross_refs: list[CrossReference] = Field(default_factory=list)
    char_position: int = 0      # position in source doc for traceability
    clause_index: int = 0       # sequential index


# ---------------------------------------------------------------------------
# Rule Extraction Models
# ---------------------------------------------------------------------------

class Condition(BaseModel):
    """
    Recursive condition tree. Leaf nodes have field/op/value.
    Branch nodes have operator + operands.
    """
    operator: Literal["AND", "OR", "NOT", "LEAF"] = "LEAF"
    field: str | None = None
    op: str | None = None        # ">", ">=", "==", "!=", "in", "not_in", "contains", "regex"
    value: Any = None            # str | float | int | list | None
    operands: list[Condition] = Field(default_factory=list)
    description: str | None = None  # human-readable of this node

    model_config = {"arbitrary_types_allowed": True}

    @model_validator(mode="after")
    def validate_structure(self) -> "Condition":
        if self.operator == "LEAF":
            if not self.field:
                raise ValueError("LEAF condition must have a field")
            if not self.op:
                raise ValueError("LEAF condition must have an op")
        else:
            if not self.operands:
                raise ValueError(f"{self.operator} condition must have operands")
        return self


class NotificationConfig(BaseModel):
    type: Literal["email", "system", "both"] = "email"
    recipients: list[str] = Field(default_factory=list)
    within_minutes: int = 15
    include_fields: list[str] = Field(
        default_factory=lambda: [
            "invoice_number", "vendor_name", "po_number",
            "deviation_type", "deviation_details", "recommended_action"
        ]
    )


class ActionConfig(BaseModel):
    action: RuleAction
    requires_justification: bool = False
    flag_code: str | None = None         # e.g. "QUANTITY_EXCEEDS_PO"
    flag_message: str | None = None
    notification: NotificationConfig | None = None
    next_action_if_unresolved_hours: int | None = None   # for escalation ladder
    escalate_to: RuleAction | None = None


class ExtractedRule(BaseModel):
    rule_id: str                         # "AP-TWM-001"
    category: str                        # "THREE_WAY_MATCH", "TAX_COMPLIANCE", "APPROVAL_MATRIX", etc.
    source_clause: str                   # "Section 2.2(c)"
    section_id: str                      # "2.2"
    sub_clause: str | None = None       # "c"
    description: str
    condition: Condition
    action_config: ActionConfig
    exceptions: list[str] = Field(default_factory=list)
    related_rules: list[str] = Field(default_factory=list)  # rule_ids
    priority: int = 100                  # lower = evaluated first
    confidence: float = Field(ge=0.0, le=1.0, default=1.0)
    low_confidence_reason: str | None = None
    raw_clause_text: str = ""
    extracted_at: datetime = Field(default_factory=datetime.utcnow)

    @field_validator("rule_id")
    @classmethod
    def validate_rule_id(cls, v: str) -> str:
        if not v.startswith("AP-"):
            raise ValueError("rule_id must start with AP-")
        return v


# ---------------------------------------------------------------------------
# Conflict Detection Models
# ---------------------------------------------------------------------------

class Conflict(BaseModel):
    conflict_id: str
    rule_ids: list[str]
    conflict_type: ConflictType
    description: str
    severity: Severity
    affected_field: str | None = None
    overlap_range: dict | None = None   # {"min": x, "max": y} for threshold overlaps
    suggested_resolution: str
    auto_resolvable: bool = False


# ---------------------------------------------------------------------------
# Invoice Input Model (for rule execution)
# ---------------------------------------------------------------------------

class InvoiceLineItem(BaseModel):
    line_item_id: str
    product_code: str | None = None
    description: str
    po_line_item_id: str | None = None
    invoice_qty: float
    po_qty: float | None = None
    grn_qty: float | None = None
    invoice_unit_rate: float
    po_unit_rate: float | None = None
    taxable_amount: float
    cgst: float = 0.0
    sgst: float = 0.0
    igst: float = 0.0
    line_total: float


class Invoice(BaseModel):
    # Mandatory fields per Section 1.1
    invoice_number: str
    invoice_date: datetime
    vendor_gstin: str
    vendor_pan: str | None = None
    po_number: str
    grand_total: float

    # Vendor info
    vendor_name: str = "Unknown Vendor"
    vendor_id: str | None = None
    vendor_on_watchlist: bool = False
    gstin_in_master: str | None = None
    pan_in_master: str | None = None

    # PO/GRN matching
    po_amount: float | None = None
    po_status: str | None = None          # "ACTIVE", "CLOSED", "CANCELLED"
    grn_numbers: list[str] = Field(default_factory=list)
    grn_date: datetime | None = None
    grn_qty_total: float | None = None
    invoice_qty_total: float | None = None
    po_qty_total: float | None = None
    is_goods_based_po: bool = True

    # Tax
    supply_type: SupplyType = SupplyType.INTRA_STATE
    place_of_supply_state_code: str | None = None
    buyer_gstin_state_code: str | None = None
    total_taxable_amount: float = 0.0
    total_cgst: float = 0.0
    total_sgst: float = 0.0
    total_igst: float = 0.0
    total_tax: float = 0.0

    # Line items
    line_items: list[InvoiceLineItem] = Field(default_factory=list)

    # Additional
    is_handwritten: bool = False
    qr_code_present: bool | None = None
    qr_invoice_number: str | None = None
    qr_vendor_gstin: str | None = None
    digital_signature_present: bool = False
    digital_signature_valid: bool | None = None

    # Meta
    processing_date: datetime = Field(default_factory=datetime.utcnow)

    @field_validator("vendor_gstin")
    @classmethod
    def validate_gstin_format(cls, v: str) -> str:
        import re
        if v and not re.match(r"^\d{2}[A-Z]{5}\d{4}[A-Z]{1}[A-Z\d]{1}Z[A-Z\d]{1}$", v):
            raise ValueError(f"Invalid GSTIN format: {v}")
        return v

    @model_validator(mode="after")
    def compute_totals(self) -> "Invoice":
        if self.line_items and self.total_taxable_amount == 0:
            self.total_taxable_amount = sum(li.taxable_amount for li in self.line_items)
            self.total_cgst = sum(li.cgst for li in self.line_items)
            self.total_sgst = sum(li.sgst for li in self.line_items)
            self.total_igst = sum(li.igst for li in self.line_items)
            self.total_tax = self.total_cgst + self.total_sgst + self.total_igst
        if self.invoice_qty_total is None and self.line_items:
            self.invoice_qty_total = sum(li.invoice_qty for li in self.line_items)
        if self.po_qty_total is None and self.line_items:
            self.po_qty_total = sum(li.po_qty or 0 for li in self.line_items)
        if self.grn_qty_total is None and self.line_items:
            self.grn_qty_total = sum(li.grn_qty or 0 for li in self.line_items)
        return self


# ---------------------------------------------------------------------------
# Execution Result Models
# ---------------------------------------------------------------------------

class RuleResult(BaseModel):
    rule_id: str
    source_clause: str
    description: str
    condition_met: bool
    action_triggered: RuleAction | None
    flag_code: str | None = None
    flag_message: str | None = None
    reason: str
    deviation_type: DeviationType | None = None
    deviation_details: dict | None = None
    requires_notification: bool = False
    execution_error: str | None = None


class ExecutionReport(BaseModel):
    invoice_number: str
    processed_at: datetime = Field(default_factory=datetime.utcnow)
    final_verdict: InvoiceStatus
    triggered_rules: list[RuleResult]
    failed_rules: list[RuleResult]         # rules that errored during eval
    skipped_rules: list[RuleResult]        # rules not applicable
    total_rules_evaluated: int
    deviations: list[DeviationType]
    notifications_queued: int = 0
    audit_trail: list[str] = Field(default_factory=list)
    execution_time_ms: float = 0.0


# ---------------------------------------------------------------------------
# API Request/Response Models
# ---------------------------------------------------------------------------

class ExtractionRequest(BaseModel):
    policy_text: str
    policy_name: str = "AP Policy"
    extract_notifications: bool = True
    confidence_threshold: float = Field(default=0.6, ge=0.0, le=1.0)


class ExtractionResponse(BaseModel):
    policy_name: str
    total_clauses: int
    total_rules_extracted: int
    low_confidence_rules: int
    rules: list[ExtractedRule]
    conflicts: list[Conflict]
    extraction_time_ms: float
    warnings: list[str] = Field(default_factory=list)


class ValidationRequest(BaseModel):
    invoice: Invoice
    rule_ids: list[str] | None = None     # if None, run all rules
    dry_run: bool = False


class ValidationResponse(BaseModel):
    report: ExecutionReport
    summary: str


class NotificationPayload(BaseModel):
    invoice_number: str
    vendor_name: str
    po_number: str
    deviation_type: DeviationType
    deviation_details: dict
    recommended_action: str
    notify_roles: list[str]
    escalation_level: int = 0
    created_at: datetime = Field(default_factory=datetime.utcnow)
    resolved: bool = False
    resolve_deadline: datetime | None = None