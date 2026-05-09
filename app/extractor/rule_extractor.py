"""
LLM Rule Extractor Module
Uses Groq API (LLaMA 3) to extract deterministic rules from policy clauses.
Features: structured JSON output, confidence scoring, retry logic, few-shot prompting.
"""
from __future__ import annotations
import json
import logging
import time
import re
from typing import Any

from app.models.schemas import (
    Clause, ExtractedRule, Condition, ActionConfig, NotificationConfig,
    RuleAction, DeviationType
)
from app.utils.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Rule ID counter (global, thread-safe enough for single-process use)
# ---------------------------------------------------------------------------
_RULE_COUNTERS: dict[str, int] = {}

CATEGORY_PREFIXES = {
    "INVOICE_VALIDATION": "AP-INV",
    "THREE_WAY_MATCH": "AP-TWM",
    "GRN_MATCHING": "AP-GRN",
    "TAX_COMPLIANCE": "AP-TAX",
    "APPROVAL_MATRIX": "AP-APR",
    "DEVIATION_NOTIFICATIONS": "AP-DEV",
    "QR_DIGITAL_VALIDATION": "AP-QR",
    "GENERAL": "AP-GEN",
}

def _next_rule_id(category: str) -> str:
    prefix = CATEGORY_PREFIXES.get(category, "AP-GEN")
    _RULE_COUNTERS[prefix] = _RULE_COUNTERS.get(prefix, 0) + 1
    return f"{prefix}-{_RULE_COUNTERS[prefix]:03d}"


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are an expert Accounts Payable rule extraction engine.
Your job is to read a single policy clause and extract a deterministic, machine-executable rule from it.

You MUST respond with a single valid JSON object matching the schema below. No markdown, no explanation, no preamble.

RULE SCHEMA:
{
  "description": "one-sentence description of the rule",
  "category": "THREE_WAY_MATCH | INVOICE_VALIDATION | GRN_MATCHING | TAX_COMPLIANCE | APPROVAL_MATRIX | DEVIATION_NOTIFICATIONS | QR_DIGITAL_VALIDATION",
  "condition": {
    "operator": "AND | OR | NOT | LEAF",
    "field": "field_name (for LEAF only)",
    "op": "> | >= | < | <= | == | != | in | not_in | contains | is_null | is_not_null (for LEAF only)",
    "value": "threshold value or list (for LEAF only)",
    "operands": [...nested conditions...],
    "description": "human readable description of this condition"
  },
  "action": {
    "action": "AUTO_APPROVE | ROUTE_TO_DEPT_HEAD | ESCALATE_TO_FINANCE_CONTROLLER | ESCALATE_TO_CFO | REJECT | HOLD | FLAG | COMPLIANCE_HOLD | REQUEST_MANUAL_REVIEW | SEND_NOTIFICATION",
    "requires_justification": true/false,
    "flag_code": "MACHINE_READABLE_FLAG or null",
    "flag_message": "human readable flag message or null",
    "notification": {
      "type": "email | system | both",
      "recipients": ["finance_controller", "internal_audit", "dept_head", "ap_clerk", "procurement", "cfo"],
      "within_minutes": 15,
      "include_fields": ["invoice_number", "vendor_name", "po_number", "deviation_type", "deviation_details", "recommended_action"]
    } or null,
    "next_action_if_unresolved_hours": null or integer,
    "escalate_to": null or action string
  },
  "exceptions": ["list of exception conditions as strings"],
  "confidence": 0.0-1.0,
  "low_confidence_reason": null or "reason why confidence is low"
}

FIELD NAMES (use exactly these):
- invoice_total, po_amount, deviation_pct (invoice vs PO %)
- invoice_qty_total, po_qty_total, grn_qty_total
- invoice_unit_rate, po_unit_rate, rate_deviation_pct
- grn_date, invoice_date, processing_date
- vendor_gstin, gstin_in_master, vendor_pan, pan_in_master
- supply_type (values: "intra_state" | "inter_state")
- total_cgst, total_sgst, total_igst, total_tax, total_taxable_amount, grand_total
- place_of_supply_state_code, buyer_gstin_state_code
- vendor_on_watchlist, is_handwritten, po_status
- qr_code_present, qr_invoice_number, qr_vendor_gstin
- digital_signature_present, digital_signature_valid
- invoice_number (for duplicate detection context)

CONFIDENCE SCORING GUIDELINES:
- 1.0: Clear, unambiguous rule with explicit numbers/conditions
- 0.8-0.9: Rule is clear but has minor ambiguity in field mapping
- 0.6-0.7: Rule contains words like "may", "should", "typically" or has cross-references
- Below 0.6: Rule is highly ambiguous, references external policy, or contains multiple interpretations

FEW-SHOT EXAMPLE:

Input clause: "If the Invoice Total Amount exceeds the PO Amount by 10% or more, the invoice is escalated to the Finance Controller with a mandatory justification note."

Output:
{
  "description": "Escalate invoice to Finance Controller when amount exceeds PO by 10% or more",
  "category": "THREE_WAY_MATCH",
  "condition": {
    "operator": "LEAF",
    "field": "deviation_pct",
    "op": ">=",
    "value": 10,
    "operands": [],
    "description": "Invoice total exceeds PO amount by 10% or more"
  },
  "action": {
    "action": "ESCALATE_TO_FINANCE_CONTROLLER",
    "requires_justification": true,
    "flag_code": "AMOUNT_EXCEEDS_PO_10PCT",
    "flag_message": "Invoice amount exceeds PO by 10% or more. Escalated to Finance Controller.",
    "notification": {
      "type": "email",
      "recipients": ["finance_controller", "internal_audit"],
      "within_minutes": 15,
      "include_fields": ["invoice_number", "vendor_name", "po_number", "deviation_type", "deviation_details", "recommended_action"]
    },
    "next_action_if_unresolved_hours": 48,
    "escalate_to": "ESCALATE_TO_CFO"
  },
  "exceptions": [],
  "confidence": 1.0,
  "low_confidence_reason": null
}"""


def _build_user_prompt(clause: Clause, category: str) -> str:
    cross_ref_note = ""
    if clause.cross_refs:
        refs = ", ".join(r.ref_text for r in clause.cross_refs)
        cross_ref_note = f"\nNote: This clause references {refs}. Include these as exceptions or related context."

    return f"""Extract a deterministic rule from this policy clause.

Section: {clause.full_ref}
Category hint: {category}
Clause text:
\"\"\"{clause.raw_text}\"\"\"{cross_ref_note}

Respond with the JSON rule object only."""


# ---------------------------------------------------------------------------
# Groq API client
# ---------------------------------------------------------------------------

def _call_groq(messages: list[dict], retries: int = 3) -> str:
    """Call Groq API with retry logic."""
    try:
        from groq import Groq
    except ImportError:
        raise RuntimeError("groq package required: pip install groq")

    client = Groq(api_key=settings.GROQ_API_KEY)

    for attempt in range(retries):
        try:
            response = client.chat.completions.create(
                model=settings.GROQ_MODEL,
                messages=messages,
                temperature=0.1,        # low temp for determinism
                # max_tokens=1200,
                response_format={"type": "json_object"},
            )
            return response.choices[0].message.content or ""
        except Exception as e:
            if attempt == retries - 1:
                raise
            wait = min(2 ** attempt + 1, 20)
            logger.warning(f"Groq API error (attempt {attempt + 1}/{retries}): {e}. Retrying in {wait}s...")
            time.sleep(wait)

    return ""


# ---------------------------------------------------------------------------
# Post-processing helpers
# ---------------------------------------------------------------------------

def _safe_parse_json(raw: str) -> dict | None:
    """Try to parse JSON, stripping markdown fences if present."""
    raw = raw.strip()
    # Strip ```json ... ``` fences
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error(f"JSON parse error: {e}. Raw: {raw[:200]}")
        return None


def _parse_condition(data: dict) -> Condition:
    """Recursively parse condition dict into Condition model."""
    if not isinstance(data, dict):
        raise ValueError(f"Expected dict for condition, got {type(data)}")

    operator = data.get("operator", "LEAF")
    operands_raw = data.get("operands", [])
    operands = [_parse_condition(op) for op in operands_raw] if operands_raw else []

    return Condition(
        operator=operator,
        field=data.get("field"),
        op=data.get("op"),
        value=data.get("value"),
        operands=operands,
        description=data.get("description"),
    )


def _parse_action(data: dict) -> ActionConfig:
    """Parse action dict into ActionConfig model."""
    notif_data = data.get("notification")
    notification = None
    if notif_data and isinstance(notif_data, dict):
        notification = NotificationConfig(
            type=notif_data.get("type", "email"),
            recipients=notif_data.get("recipients", []),
            within_minutes=notif_data.get("within_minutes", 15),
            include_fields=notif_data.get("include_fields", []),
        )

    escalate_to_raw = data.get("escalate_to")
    escalate_to = None
    if escalate_to_raw:
        try:
            escalate_to = RuleAction(escalate_to_raw)
        except ValueError:
            logger.warning(f"Unknown escalate_to value: {escalate_to_raw}")

    return ActionConfig(
        action=RuleAction(data["action"]),
        requires_justification=data.get("requires_justification", False),
        flag_code=data.get("flag_code"),
        flag_message=data.get("flag_message"),
        notification=notification,
        next_action_if_unresolved_hours=data.get("next_action_if_unresolved_hours"),
        escalate_to=escalate_to,
    )


def _build_fallback_rule(clause: Clause, category: str, rule_id: str, error: str) -> ExtractedRule:
    """Create a low-confidence placeholder rule when extraction fails."""
    return ExtractedRule(
        rule_id=rule_id,
        category=category,
        source_clause=clause.full_ref,
        section_id=clause.section_id,
        sub_clause=clause.sub_clause,
        description=f"[EXTRACTION FAILED] {clause.raw_text[:80]}...",
        condition=Condition(
            operator="LEAF",
            field="__manual_review__",
            op="==",
            value=True,
            description="Manual review required — automatic extraction failed",
        ),
        action_config=ActionConfig(
            action=RuleAction.REQUEST_MANUAL_REVIEW,
            requires_justification=True,
            flag_code="EXTRACTION_FAILED",
            flag_message=f"Automatic rule extraction failed: {error}",
        ),
        exceptions=[],
        confidence=0.0,
        low_confidence_reason=f"Extraction error: {error}",
        raw_clause_text=clause.raw_text,
    )


# ---------------------------------------------------------------------------
# Main extraction function
# ---------------------------------------------------------------------------

def extract_rule_from_clause(clause: Clause, category: str) -> ExtractedRule:
    """
    Extract a single deterministic rule from a policy clause using the LLM.
    Falls back to a placeholder rule on failure.
    """
    rule_id = _next_rule_id(category)
    
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _build_user_prompt(clause, category)},
    ]

    try:
        raw_response = _call_groq(messages)
    except Exception as e:
        logger.error(f"LLM call failed for {clause.full_ref}: {e}")
        return _build_fallback_rule(clause, category, rule_id, str(e))

    parsed = _safe_parse_json(raw_response)
    if not parsed:
        return _build_fallback_rule(clause, category, rule_id, "Invalid JSON response from LLM")

    try:
        if "condition" not in parsed or "action" not in parsed:
            raise ValueError("Missing required fields in LLM response")

        condition = _parse_condition(parsed["condition"])
        action = _parse_action(parsed["action"])

        rule = ExtractedRule(
            rule_id=rule_id,
            category=category,
            source_clause=clause.full_ref,
            section_id=clause.section_id,
            sub_clause=clause.sub_clause,
            description=parsed.get("description", ""),
            condition=condition,
            action_config=action,
            exceptions=parsed.get("exceptions", []),
            confidence=float(parsed.get("confidence", 0.8)),
            low_confidence_reason=parsed.get("low_confidence_reason"),
            raw_clause_text=clause.raw_text,
        )

        logger.info(f"Extracted rule {rule_id} from {clause.full_ref} (confidence={rule.confidence:.2f})")
        return rule

    except Exception as e:
        logger.error(f"Rule model construction failed for {clause.full_ref}: {e}")
        return _build_fallback_rule(clause, category, rule_id, str(e))


def extract_rules_from_clauses(
    clauses: list[Clause],
    category_map: dict[str, str],
    confidence_threshold: float = 0.6,
) -> tuple[list[ExtractedRule], list[str]]:
    """
    Extract rules from all clauses. Returns (rules, warnings).
    category_map: {clause.full_ref -> category}
    """
    rules: list[ExtractedRule] = []
    warnings: list[str] = []

    for clause in clauses:
        category = category_map.get(clause.full_ref, "GENERAL")

        # Skip empty or very short clauses (headings, cross-ref-only lines)
        if len(clause.raw_text.strip()) < 20:
            logger.debug(f"Skipping short clause {clause.full_ref}: {clause.raw_text[:30]}")
            continue

        NON_RULE_PATTERNS = [
            "introduction",
            "purpose",
            "scope",
            "overview",
            "definitions"
        ]

        text_lower = clause.raw_text.lower()

        if any(p in text_lower for p in NON_RULE_PATTERNS):
            continue

        rule = extract_rule_from_clause(clause, category)
        rules.append(rule)

        if rule.confidence < confidence_threshold:
            msg = (
                f"Low-confidence rule {rule.rule_id} at {clause.full_ref} "
                f"(confidence={rule.confidence:.2f}): {rule.low_confidence_reason}"
            )
            warnings.append(msg)
            logger.warning(msg)

        # Small delay to avoid rate limiting
        time.sleep(2)

    return rules, warnings


def reset_rule_counters() -> None:
    """Reset rule ID counters (used in tests)."""
    _RULE_COUNTERS.clear()