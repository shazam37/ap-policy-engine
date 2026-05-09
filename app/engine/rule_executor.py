"""
Rule Execution Engine
Evaluates an Invoice against all extracted rules deterministically.
Returns a structured ExecutionReport with full audit trail.
"""
from __future__ import annotations
import logging
import time
import re
from datetime import datetime
from typing import Any

from app.models.schemas import (
    Invoice, ExtractedRule, Condition, RuleResult, ExecutionReport,
    RuleAction, InvoiceStatus, DeviationType, NotificationPayload
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Condition evaluator
# ---------------------------------------------------------------------------

def _get_field_value(invoice: Invoice, field: str) -> Any:
    """
    Resolve a field name to its value on an Invoice instance.
    Handles computed fields like deviation_pct and rate_deviation_pct.
    """
    # Computed fields not directly on the model
    computed = _compute_derived_fields(invoice)
    if field in computed:
        return computed[field]

    # Direct model attributes
    try:
        return getattr(invoice, field)
    except AttributeError:
        logger.debug(f"Field '{field}' not found on Invoice model")
        return None


def _compute_derived_fields(invoice: Invoice) -> dict[str, Any]:
    """Compute derived fields for rule evaluation."""
    derived: dict[str, Any] = {}

    # deviation_pct: how much does invoice exceed PO?
    if invoice.po_amount and invoice.po_amount > 0:
        diff = invoice.grand_total - invoice.po_amount
        derived["deviation_pct"] = (diff / invoice.po_amount) * 100
        derived["under_invoiced_pct"] = max(0.0, -derived["deviation_pct"])
        derived["over_invoiced_pct"] = max(0.0, derived["deviation_pct"])
    else:
        derived["deviation_pct"] = 0.0
        derived["under_invoiced_pct"] = 0.0
        derived["over_invoiced_pct"] = 0.0

    # invoice_total (alias for grand_total)
    derived["invoice_total"] = invoice.grand_total

    # Tax calculation verification
    computed_total = invoice.total_taxable_amount + invoice.total_tax
    derived["tax_calculation_error"] = abs(computed_total - invoice.grand_total) > 1.0  # INR 1 tolerance
    derived["tax_calc_diff"] = computed_total - invoice.grand_total

    # Quantity checks
    if invoice.invoice_qty_total is not None and invoice.po_qty_total is not None:
        derived["qty_exceeds_po"] = invoice.invoice_qty_total > invoice.po_qty_total
    else:
        derived["qty_exceeds_po"] = False

    if invoice.invoice_qty_total is not None and invoice.grn_qty_total is not None:
        derived["qty_exceeds_grn"] = invoice.invoice_qty_total > invoice.grn_qty_total
    else:
        derived["qty_exceeds_grn"] = False

    # GRN date validation
    if invoice.grn_date and invoice.invoice_date:
        derived["grn_post_dated"] = invoice.grn_date > invoice.invoice_date
    else:
        derived["grn_post_dated"] = False

    # Future-dated invoice
    derived["is_future_dated"] = invoice.invoice_date > invoice.processing_date

    # GSTIN match
    derived["gstin_mismatch"] = (
        invoice.gstin_in_master is not None
        and invoice.vendor_gstin != invoice.gstin_in_master
    )

    # PAN check (PAN is chars 3-12 of GSTIN, 0-indexed: positions 2:12)
    if invoice.vendor_gstin and len(invoice.vendor_gstin) >= 12:
        derived["pan_in_gstin"] = invoice.vendor_gstin[2:12]
    else:
        derived["pan_in_gstin"] = None

    derived["pan_gstin_mismatch"] = (
        invoice.pan_in_master is not None
        and derived.get("pan_in_gstin") != invoice.pan_in_master
    )

    # Tax type checks — always compute all fields regardless of supply type
    derived["cgst_sgst_equal"] = abs(invoice.total_cgst - invoice.total_sgst) <= 0.01
    derived["igst_zero"] = invoice.total_igst == 0.0
    derived["igst_present"] = invoice.total_igst > 0
    derived["cgst_zero"] = invoice.total_cgst == 0.0
    derived["sgst_zero"] = invoice.total_sgst == 0.0
    derived["intra_state_tax_valid"] = (
        invoice.supply_type.value == "intra_state"
        and invoice.total_cgst > 0
        and invoice.total_sgst > 0
        and derived["cgst_sgst_equal"]
        and derived["igst_zero"]
    )
    derived["inter_state_tax_valid"] = (
        invoice.supply_type.value == "inter_state"
        and derived["igst_present"]
        and derived["cgst_zero"]
        and derived["sgst_zero"]
    )

    # Place of supply match
    derived["place_of_supply_mismatch"] = (
        invoice.place_of_supply_state_code is not None
        and invoice.buyer_gstin_state_code is not None
        and invoice.place_of_supply_state_code != invoice.buyer_gstin_state_code
    )

    # QR code validation
    if invoice.qr_code_present:
        qr_inv_match = (invoice.qr_invoice_number == invoice.invoice_number
                        if invoice.qr_invoice_number else True)
        qr_gstin_match = (invoice.qr_vendor_gstin == invoice.vendor_gstin
                          if invoice.qr_vendor_gstin else True)
        derived["qr_validation_failed"] = not (qr_inv_match and qr_gstin_match)
    else:
        derived["qr_validation_failed"] = False

    # Section 1.4 — Duplicate invoice detection
    # (field set externally; engine evaluates the flag if provided)
    derived["is_duplicate"] = getattr(invoice, "_is_duplicate", False)

    # Section 2.1 — Invalid PO reference
    derived["invalid_po"] = (
        invoice.po_status is not None
        and invoice.po_status.upper() not in ("ACTIVE", "OPEN")
    )

    # Section 2.3(c) — Rate deviation per line item (max across all line items)
    max_rate_deviation = 0.0
    for li in invoice.line_items:
        if li.po_unit_rate and li.po_unit_rate > 0:
            rate_dev = abs((li.invoice_unit_rate - li.po_unit_rate) / li.po_unit_rate) * 100
            max_rate_deviation = max(max_rate_deviation, rate_dev)
    derived["rate_deviation_pct"] = max_rate_deviation
    derived["rate_mismatch"] = max_rate_deviation > 2.0

    # Section 3.1 — GRN existence check for goods-based POs
    derived["grn_missing"] = (
        invoice.is_goods_based_po
        and (not invoice.grn_numbers or len(invoice.grn_numbers) == 0)
    )

    # Handwritten + amount check
    derived["handwritten_needs_ap_manager"] = (
        invoice.is_handwritten and invoice.grand_total > 50_000
    )

    return derived


def _eval_condition(condition: Condition, invoice: Invoice) -> tuple[bool, str]:
    """
    Recursively evaluate a condition tree against an invoice.
    Returns (result: bool, explanation: str).
    """
    if condition.operator == "LEAF":
        if not condition.field:
            return True, "No field specified (pass-through)"

        actual = _get_field_value(invoice, condition.field)
        if actual is None:
            return False, f"Field '{condition.field}' is null/missing"

        op = condition.op
        expected = condition.value

        try:
            result = _apply_op(actual, op, expected)
            explanation = f"{condition.field} {op} {expected} → actual={actual!r} → {result}"
            return result, explanation
        except Exception as e:
            return False, f"Evaluation error for {condition.field} {op} {expected}: {e}"

    elif condition.operator == "AND":
        results = []
        explanations = []
        for operand in condition.operands:
            r, e = _eval_condition(operand, invoice)
            results.append(r)
            explanations.append(e)
        overall = all(results)
        return overall, " AND ".join(f"({e})" for e in explanations)

    elif condition.operator == "OR":
        results = []
        explanations = []
        for operand in condition.operands:
            r, e = _eval_condition(operand, invoice)
            results.append(r)
            explanations.append(e)
        overall = any(results)
        return overall, " OR ".join(f"({e})" for e in explanations)

    elif condition.operator == "NOT":
        if not condition.operands:
            return True, "Empty NOT (pass-through)"
        r, e = _eval_condition(condition.operands[0], invoice)
        return not r, f"NOT ({e})"

    else:
        logger.warning(f"Unknown operator: {condition.operator}")
        return False, f"Unknown operator: {condition.operator}"


def _apply_op(actual: Any, op: str | None, expected: Any) -> bool:
    """Apply a comparison operator."""
    if op is None:
        return bool(actual)

    # Null checks
    if op == "is_null":
        return actual is None
    if op == "is_not_null":
        return actual is not None

    # Convert to float for numeric comparisons when possible
    try:
        a_num = float(actual) if actual is not None else None
        e_num = float(expected) if expected is not None else None
    except (TypeError, ValueError):
        a_num, e_num = None, None

    if op == ">" and a_num is not None and e_num is not None:
        return a_num > e_num
    elif op == ">=" and a_num is not None and e_num is not None:
        return a_num >= e_num
    elif op == "<" and a_num is not None and e_num is not None:
        return a_num < e_num
    elif op == "<=" and a_num is not None and e_num is not None:
        return a_num <= e_num
    elif op == "==":
        if a_num is not None and e_num is not None:
            return abs(a_num - e_num) < 0.001
        return str(actual).lower() == str(expected).lower()
    elif op == "!=":
        if a_num is not None and e_num is not None:
            return abs(a_num - e_num) >= 0.001
        return str(actual).lower() != str(expected).lower()
    elif op == "in":
        if isinstance(expected, list):
            return actual in expected or str(actual).lower() in [str(e).lower() for e in expected]
        return False
    elif op == "not_in":
        if isinstance(expected, list):
            return actual not in expected
        return actual != expected
    elif op == "contains":
        return str(expected).lower() in str(actual).lower()
    elif op == "regex":
        return bool(re.search(str(expected), str(actual)))
    else:
        logger.warning(f"Unknown op: {op}")
        return False


# ---------------------------------------------------------------------------
# Deviation type inference
# ---------------------------------------------------------------------------

def _infer_deviation_type(rule: ExtractedRule) -> DeviationType | None:
    """Infer the deviation type from a rule based on its fields and category."""
    fields = set()

    def collect_fields(cond: Condition) -> None:
        if cond.field:
            fields.add(cond.field)
        for op in cond.operands:
            collect_fields(op)

    collect_fields(rule.condition)

    if "deviation_pct" in fields or "invoice_total" in fields:
        return DeviationType.AMOUNT_MISMATCH
    if "qty_exceeds_grn" in fields or "qty_exceeds_po" in fields or "invoice_qty_total" in fields or "grn_qty_total" in fields:
        return DeviationType.QUANTITY_MISMATCH
    if "rate_deviation_pct" in fields:
        return DeviationType.RATE_MISMATCH
    if "grn_date" in fields or "grn_qty" in fields:
        return DeviationType.MISSING_GRN
    if "total_cgst" in fields or "total_igst" in fields or "tax_calculation_error" in fields:
        return DeviationType.TAX_ERROR
    if "vendor_gstin" in fields or "gstin_mismatch" in fields:
        return DeviationType.GSTIN_MISMATCH
    if "qr_code_present" in fields or "qr_validation_failed" in fields:
        return DeviationType.QR_VALIDATION_FAILED
    return None


# ---------------------------------------------------------------------------
# Verdict resolution
# ---------------------------------------------------------------------------

# Priority order: higher index = more severe / takes precedence
ACTION_PRIORITY = [
    RuleAction.AUTO_APPROVE,
    RuleAction.SEND_NOTIFICATION,
    RuleAction.FLAG,
    RuleAction.ROUTE_TO_DEPT_HEAD,
    RuleAction.ESCALATE_TO_FINANCE_CONTROLLER,
    RuleAction.ESCALATE_TO_CFO,
    RuleAction.HOLD,
    RuleAction.COMPLIANCE_HOLD,
    RuleAction.REJECT,
    RuleAction.REQUEST_MANUAL_REVIEW,
]

ACTION_TO_STATUS = {
    RuleAction.AUTO_APPROVE: InvoiceStatus.AUTO_APPROVED,
    RuleAction.ROUTE_TO_DEPT_HEAD: InvoiceStatus.ROUTED_DEPT_HEAD,
    RuleAction.ESCALATE_TO_FINANCE_CONTROLLER: InvoiceStatus.ESCALATED_FC,
    RuleAction.ESCALATE_TO_CFO: InvoiceStatus.ESCALATED_CFO,
    RuleAction.REJECT: InvoiceStatus.REJECTED,
    RuleAction.HOLD: InvoiceStatus.HELD,
    RuleAction.FLAG: InvoiceStatus.FLAGGED,
    RuleAction.COMPLIANCE_HOLD: InvoiceStatus.COMPLIANCE_HOLD,
    RuleAction.REQUEST_MANUAL_REVIEW: InvoiceStatus.HELD,
    RuleAction.SEND_NOTIFICATION: InvoiceStatus.PENDING,
}


def _resolve_final_verdict(triggered: list[RuleResult]) -> InvoiceStatus:
    """
    Determine the final invoice status from all triggered rules.
    The most severe action wins.
    """
    if not triggered:
        return InvoiceStatus.AUTO_APPROVED

    winning_priority = -1
    winning_status = InvoiceStatus.PENDING

    for result in triggered:
        if result.action_triggered:
            try:
                priority = ACTION_PRIORITY.index(result.action_triggered)
            except ValueError:
                priority = 0
            if priority > winning_priority:
                winning_priority = priority
                winning_status = ACTION_TO_STATUS.get(result.action_triggered, InvoiceStatus.HELD)

    return winning_status


# ---------------------------------------------------------------------------
# Main execution function
# ---------------------------------------------------------------------------

def execute_rules(
    invoice: Invoice,
    rules: list[ExtractedRule],
    rule_ids: list[str] | None = None,
) -> ExecutionReport:
    """
    Execute all applicable rules against an invoice.
    If rule_ids is provided, only run those rules.
    Returns a complete ExecutionReport.
    """
    start_time = time.perf_counter()
    audit_trail: list[str] = []
    triggered: list[RuleResult] = []
    failed: list[RuleResult] = []
    skipped: list[RuleResult] = []
    deviations: set[DeviationType] = set()
    notifications_queued = 0

    # Filter rules if specific IDs requested
    rules_to_run = rules
    if rule_ids:
        rules_to_run = [r for r in rules if r.rule_id in rule_ids]

    # Sort by priority
    rules_to_run.sort(key=lambda r: r.priority)

    audit_trail.append(f"Starting execution of {len(rules_to_run)} rules for invoice {invoice.invoice_number}")
    audit_trail.append(f"Processing date: {invoice.processing_date.isoformat()}")
    audit_trail.append(f"Grand total: INR {invoice.grand_total:,.2f}")
    if invoice.po_amount:
        pct = ((invoice.grand_total - invoice.po_amount) / invoice.po_amount) * 100
        audit_trail.append(f"PO amount: INR {invoice.po_amount:,.2f} (deviation: {pct:+.2f}%)")

    for rule in rules_to_run:
        # Skip low-confidence rules that need manual review
        if rule.action_config.action == RuleAction.REQUEST_MANUAL_REVIEW and rule.confidence == 0.0:
            skipped.append(RuleResult(
                rule_id=rule.rule_id,
                source_clause=rule.source_clause,
                description=rule.description,
                condition_met=False,
                action_triggered=None,
                reason=f"Skipped: low-confidence extraction (confidence={rule.confidence})",
            ))
            continue

        try:
            condition_met, explanation = _eval_condition(rule.condition, invoice)
        except Exception as e:
            logger.error(f"Rule {rule.rule_id} evaluation error: {e}")
            failed.append(RuleResult(
                rule_id=rule.rule_id,
                source_clause=rule.source_clause,
                description=rule.description,
                condition_met=False,
                action_triggered=None,
                reason=f"Evaluation error: {e}",
                execution_error=str(e),
            ))
            continue

        if condition_met:
            dev_type = _infer_deviation_type(rule)
            if dev_type:
                deviations.add(dev_type)

            derived = _compute_derived_fields(invoice)
            deviation_details = _build_deviation_details(rule, invoice, derived)

            result = RuleResult(
                rule_id=rule.rule_id,
                source_clause=rule.source_clause,
                description=rule.description,
                condition_met=True,
                action_triggered=rule.action_config.action,
                flag_code=rule.action_config.flag_code,
                flag_message=rule.action_config.flag_message,
                reason=explanation,
                deviation_type=dev_type,
                deviation_details=deviation_details,
                requires_notification=rule.action_config.notification is not None,
            )
            triggered.append(result)

            if rule.action_config.notification:
                notifications_queued += 1

            audit_trail.append(
                f"[TRIGGERED] {rule.rule_id} ({rule.source_clause}): "
                f"{rule.action_config.action.value} — {rule.description}"
            )
        else:
            audit_trail.append(
                f"[PASS] {rule.rule_id} ({rule.source_clause}): condition not met"
            )

    final_verdict = _resolve_final_verdict(triggered)
    execution_time = (time.perf_counter() - start_time) * 1000

    audit_trail.append(f"Final verdict: {final_verdict.value}")
    audit_trail.append(f"Execution time: {execution_time:.2f}ms")

    return ExecutionReport(
        invoice_number=invoice.invoice_number,
        final_verdict=final_verdict,
        triggered_rules=triggered,
        failed_rules=failed,
        skipped_rules=skipped,
        total_rules_evaluated=len(rules_to_run),
        deviations=list(deviations),
        notifications_queued=notifications_queued,
        audit_trail=audit_trail,
        execution_time_ms=round(execution_time, 2),
    )


def _build_deviation_details(
    rule: ExtractedRule, invoice: Invoice, derived: dict
) -> dict | None:
    """Build a human-readable deviation details dict."""
    details: dict = {}

    if invoice.po_amount:
        details["expected_amount"] = invoice.po_amount
        details["actual_amount"] = invoice.grand_total
        details["deviation_pct"] = round(derived.get("deviation_pct", 0), 2)

    if invoice.po_qty_total:
        details["expected_qty"] = invoice.po_qty_total
        details["actual_qty"] = invoice.invoice_qty_total

    if invoice.grn_qty_total:
        details["grn_qty"] = invoice.grn_qty_total

    if derived.get("rate_deviation_pct", 0) > 0:
        details["rate_deviation_pct"] = round(derived["rate_deviation_pct"], 2)

    if derived.get("tax_calculation_error"):
        details["tax_calc_diff"] = round(derived.get("tax_calc_diff", 0), 2)

    return details if details else None


def generate_notification_payload(
    invoice: Invoice,
    result: RuleResult,
    rule: ExtractedRule,
    escalation_level: int = 0,
) -> NotificationPayload:
    """Build a notification payload for a triggered rule."""
    from datetime import timedelta

    recipients = []
    if rule.action_config.notification:
        recipients = rule.action_config.notification.recipients

    within = 15
    if rule.action_config.notification:
        within = rule.action_config.notification.within_minutes

    resolve_deadline = None
    if rule.action_config.next_action_if_unresolved_hours:
        from datetime import timezone
        now = datetime.now(timezone.utc)
        resolve_deadline = now + timedelta(hours=rule.action_config.next_action_if_unresolved_hours)

    return NotificationPayload(
        invoice_number=invoice.invoice_number,
        vendor_name=invoice.vendor_name,
        po_number=invoice.po_number,
        deviation_type=result.deviation_type or DeviationType.AMOUNT_MISMATCH,
        deviation_details=result.deviation_details or {},
        recommended_action=rule.action_config.action.value,
        notify_roles=recipients,
        escalation_level=escalation_level,
        resolve_deadline=resolve_deadline,
    )
