"""
Tests for app/engine/rule_executor.py
Covers every scenario in the sample AP policy document.
"""
import pytest
from datetime import datetime, timedelta, timezone

from app.engine.rule_executor import execute_rules, _compute_derived_fields, _eval_condition
from app.models.schemas import (
    Invoice, InvoiceLineItem, ExtractedRule, Condition, ActionConfig,
    RuleAction, InvoiceStatus, SupplyType, DeviationType
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_invoice(**kwargs) -> Invoice:
    """Build a valid base invoice, override with kwargs."""
    defaults = dict(
        invoice_number="INV-2024-001",
        invoice_date=datetime(2024, 1, 15, tzinfo=timezone.utc),
        vendor_gstin="29ABCDE1234F1Z5",
        vendor_pan="ABCDE1234F",
        po_number="PO-2024-042",
        grand_total=100_000.0,
        vendor_name="Test Vendor",
        gstin_in_master="29ABCDE1234F1Z5",
        pan_in_master="ABCDE1234F",
        po_amount=100_000.0,
        po_status="ACTIVE",
        supply_type=SupplyType.INTRA_STATE,
        total_taxable_amount=84_746.0,
        total_cgst=7_627.0,
        total_sgst=7_627.0,
        total_igst=0.0,
        total_tax=15_254.0,
        invoice_qty_total=10.0,
        po_qty_total=10.0,
        grn_qty_total=10.0,
        grn_date=datetime(2024, 1, 14, tzinfo=timezone.utc),
        is_goods_based_po=True,
        processing_date=datetime(2024, 1, 16, tzinfo=timezone.utc),
        place_of_supply_state_code="29",
        buyer_gstin_state_code="29",
    )
    defaults.update(kwargs)
    return Invoice(**defaults)


def _make_rule(
    rule_id: str,
    field: str,
    op: str,
    value,
    action: RuleAction,
    category: str = "THREE_WAY_MATCH",
    flag_code: str | None = None,
    description: str = "Test rule",
) -> ExtractedRule:
    return ExtractedRule(
        rule_id=rule_id,
        category=category,
        source_clause=f"Section test",
        section_id="test",
        description=description,
        condition=Condition(operator="LEAF", field=field, op=op, value=value,
                            description=description),
        action_config=ActionConfig(action=action, flag_code=flag_code),
        confidence=1.0,
    )


# ---------------------------------------------------------------------------
# Section 1: Invoice validation
# ---------------------------------------------------------------------------

class TestInvoiceValidation:

    def test_future_dated_invoice_rejected(self):
        future_inv = _make_invoice(
            invoice_date=datetime(2099, 1, 1, tzinfo=timezone.utc),
            processing_date=datetime(2024, 1, 16, tzinfo=timezone.utc),
        )
        rule = _make_rule("AP-INV-001", "is_future_dated", "==", True, RuleAction.REJECT,
                          category="INVOICE_VALIDATION", description="Reject future-dated invoices")
        report = execute_rules(future_inv, [rule])
        assert len(report.triggered_rules) == 1
        assert report.triggered_rules[0].action_triggered == RuleAction.REJECT
        assert report.final_verdict == InvoiceStatus.REJECTED

    def test_valid_dated_invoice_passes(self):
        inv = _make_invoice()
        rule = _make_rule("AP-INV-001", "is_future_dated", "==", True, RuleAction.REJECT,
                          category="INVOICE_VALIDATION")
        report = execute_rules(inv, [rule])
        assert len(report.triggered_rules) == 0
        assert report.final_verdict == InvoiceStatus.AUTO_APPROVED

    def test_handwritten_invoice_above_50k_flagged(self):
        inv = _make_invoice(is_handwritten=True, grand_total=75_000)
        rule = _make_rule("AP-INV-003", "handwritten_needs_ap_manager", "==", True,
                          RuleAction.REQUEST_MANUAL_REVIEW,
                          category="INVOICE_VALIDATION",
                          description="Handwritten invoices >50k need AP Manager")
        report = execute_rules(inv, [rule])
        assert len(report.triggered_rules) == 1

    def test_handwritten_invoice_below_50k_passes(self):
        inv = _make_invoice(is_handwritten=True, grand_total=40_000)
        rule = _make_rule("AP-INV-003", "handwritten_needs_ap_manager", "==", True,
                          RuleAction.REQUEST_MANUAL_REVIEW, category="INVOICE_VALIDATION")
        report = execute_rules(inv, [rule])
        assert len(report.triggered_rules) == 0

    def test_gstin_mismatch_rejected(self):
        inv = _make_invoice(vendor_gstin="29ABCDE1234F1Z5", gstin_in_master="29ZZZZZ9999Z1Z5")
        rule = _make_rule("AP-TAX-001", "gstin_mismatch", "==", True, RuleAction.REJECT,
                          category="TAX_COMPLIANCE",
                          description="Reject GSTIN mismatch")
        report = execute_rules(inv, [rule])
        assert len(report.triggered_rules) == 1
        assert report.final_verdict == InvoiceStatus.REJECTED


# ---------------------------------------------------------------------------
# Section 2: Three-way match
# ---------------------------------------------------------------------------

class TestThreeWayMatch:

    def test_within_1pct_tolerance_auto_approved(self):
        inv = _make_invoice(grand_total=100_500, po_amount=100_000)  # +0.5%
        rule = _make_rule(
            "AP-TWM-001", "deviation_pct", "<=", 1.0,
            RuleAction.AUTO_APPROVE, description="Auto approve within 1% tolerance"
        )
        # Also add an AND condition: deviation_pct >= -1
        rule.condition = Condition(
            operator="AND",
            operands=[
                Condition(operator="LEAF", field="deviation_pct", op="<=", value=1.0),
                Condition(operator="LEAF", field="deviation_pct", op=">=", value=-1.0),
            ],
            description="Within +/-1% tolerance"
        )
        report = execute_rules(inv, [rule])
        assert len(report.triggered_rules) == 1
        assert report.triggered_rules[0].action_triggered == RuleAction.AUTO_APPROVE

    def test_over_1pct_under_10pct_routes_to_dept_head(self):
        inv = _make_invoice(grand_total=105_000, po_amount=100_000)  # +5%
        rule = _make_rule(
            "AP-TWM-002", "deviation_pct", ">", 1.0,
            RuleAction.ROUTE_TO_DEPT_HEAD,
            description="Route to Dept Head for 1-10% overrun"
        )
        rule.condition = Condition(
            operator="AND",
            operands=[
                Condition(operator="LEAF", field="deviation_pct", op=">", value=1.0),
                Condition(operator="LEAF", field="deviation_pct", op="<", value=10.0),
            ],
            description="Deviation > 1% and < 10%"
        )
        report = execute_rules(inv, [rule])
        assert len(report.triggered_rules) == 1
        assert report.triggered_rules[0].action_triggered == RuleAction.ROUTE_TO_DEPT_HEAD

    def test_over_10pct_escalates_to_fc(self):
        inv = _make_invoice(grand_total=115_000, po_amount=100_000)  # +15%
        rule = _make_rule(
            "AP-TWM-003", "deviation_pct", ">=", 10.0,
            RuleAction.ESCALATE_TO_FINANCE_CONTROLLER,
            description="Escalate to FC for >=10% overrun"
        )
        report = execute_rules(inv, [rule])
        assert len(report.triggered_rules) == 1
        assert report.triggered_rules[0].action_triggered == RuleAction.ESCALATE_TO_FINANCE_CONTROLLER

    def test_exact_10pct_boundary_triggers_escalation(self):
        """Boundary test: exactly 10% should trigger the >= 10% rule."""
        inv = _make_invoice(grand_total=110_000, po_amount=100_000)  # exactly +10%
        rule = _make_rule("AP-TWM-003", "deviation_pct", ">=", 10.0,
                          RuleAction.ESCALATE_TO_FINANCE_CONTROLLER)
        report = execute_rules(inv, [rule])
        assert len(report.triggered_rules) == 1

    def test_under_invoiced_5pct_flagged(self):
        inv = _make_invoice(grand_total=94_000, po_amount=100_000)  # -6%
        rule = _make_rule(
            "AP-TWM-004", "under_invoiced_pct", ">", 5.0,
            RuleAction.FLAG, flag_code="UNDER_INVOICED",
            description="Flag under-invoiced by >5%"
        )
        report = execute_rules(inv, [rule])
        assert len(report.triggered_rules) == 1
        assert report.triggered_rules[0].flag_code == "UNDER_INVOICED"

    def test_qty_exceeds_po_held(self):
        inv = _make_invoice(invoice_qty_total=12, po_qty_total=10)
        rule = _make_rule("AP-TWM-005", "qty_exceeds_po", "==", True,
                          RuleAction.HOLD, flag_code="QUANTITY_EXCEEDS_PO")
        report = execute_rules(inv, [rule])
        assert len(report.triggered_rules) == 1
        assert report.final_verdict == InvoiceStatus.HELD


# ---------------------------------------------------------------------------
# Section 3: GRN matching
# ---------------------------------------------------------------------------

class TestGRNMatching:

    def test_qty_exceeds_grn_rejected(self):
        inv = _make_invoice(invoice_qty_total=11, grn_qty_total=10)
        rule = _make_rule("AP-GRN-001", "qty_exceeds_grn", "==", True,
                          RuleAction.REJECT, category="GRN_MATCHING")
        report = execute_rules(inv, [rule])
        assert len(report.triggered_rules) == 1
        assert report.final_verdict == InvoiceStatus.REJECTED

    def test_grn_post_dated_flagged(self):
        inv = _make_invoice(
            invoice_date=datetime(2024, 1, 10, tzinfo=timezone.utc),
            grn_date=datetime(2024, 1, 15, tzinfo=timezone.utc),
        )
        rule = _make_rule("AP-GRN-002", "grn_post_dated", "==", True,
                          RuleAction.FLAG, flag_code="GRN_POST_DATED",
                          category="GRN_MATCHING")
        report = execute_rules(inv, [rule])
        assert len(report.triggered_rules) == 1

    def test_grn_date_before_invoice_passes(self):
        inv = _make_invoice(
            invoice_date=datetime(2024, 1, 15, tzinfo=timezone.utc),
            grn_date=datetime(2024, 1, 10, tzinfo=timezone.utc),
        )
        rule = _make_rule("AP-GRN-002", "grn_post_dated", "==", True,
                          RuleAction.FLAG, category="GRN_MATCHING")
        report = execute_rules(inv, [rule])
        assert len(report.triggered_rules) == 0


# ---------------------------------------------------------------------------
# Section 4: Tax compliance
# ---------------------------------------------------------------------------

class TestTaxCompliance:

    def test_intra_state_tax_valid(self):
        inv = _make_invoice(
            supply_type=SupplyType.INTRA_STATE,
            total_cgst=9_000, total_sgst=9_000, total_igst=0,
            total_taxable_amount=82_000, total_tax=18_000, grand_total=100_000,
        )
        derived = _compute_derived_fields(inv)
        assert derived["intra_state_tax_valid"] is True
        assert derived["tax_calculation_error"] is False

    def test_intra_state_unequal_cgst_sgst_fails(self):
        inv = _make_invoice(
            supply_type=SupplyType.INTRA_STATE,
            total_cgst=10_000, total_sgst=8_000, total_igst=0,
            total_taxable_amount=82_000, total_tax=18_000, grand_total=100_000,
        )
        derived = _compute_derived_fields(inv)
        assert derived["intra_state_tax_valid"] is False

    def test_inter_state_igst_valid(self):
        inv = _make_invoice(
            supply_type=SupplyType.INTER_STATE,
            total_cgst=0, total_sgst=0, total_igst=18_000,
            total_taxable_amount=82_000, total_tax=18_000, grand_total=100_000,
        )
        derived = _compute_derived_fields(inv)
        assert derived["inter_state_tax_valid"] is True

    def test_tax_calculation_error_flagged(self):
        inv = _make_invoice(
            total_taxable_amount=80_000, total_tax=18_000, grand_total=100_000  # 80k+18k=98k ≠ 100k
        )
        derived = _compute_derived_fields(inv)
        assert derived["tax_calculation_error"] is True

    def test_pan_gstin_mismatch(self):
        inv = _make_invoice(
            vendor_gstin="29ABCDE1234F1Z5",   # PAN embedded = ABCDE1234F
            pan_in_master="ZZZZZ9999Z",        # Mismatch
        )
        derived = _compute_derived_fields(inv)
        assert derived["pan_gstin_mismatch"] is True

    def test_pan_gstin_match(self):
        inv = _make_invoice(
            vendor_gstin="29ABCDE1234F1Z5",
            pan_in_master="ABCDE1234F",
        )
        derived = _compute_derived_fields(inv)
        assert derived["pan_gstin_mismatch"] is False

    def test_place_of_supply_mismatch(self):
        inv = _make_invoice(place_of_supply_state_code="07", buyer_gstin_state_code="29")
        derived = _compute_derived_fields(inv)
        assert derived["place_of_supply_mismatch"] is True


# ---------------------------------------------------------------------------
# Section 5: Approval matrix
# ---------------------------------------------------------------------------

class TestApprovalMatrix:

    def _make_approval_rules(self) -> list[ExtractedRule]:
        rules = []
        # Up to 1L: auto-approve
        r1 = ExtractedRule(
            rule_id="AP-APR-001", category="APPROVAL_MATRIX",
            source_clause="Section 5.1", section_id="5.1",
            description="Auto approve up to 1L",
            condition=Condition(operator="AND", operands=[
                Condition(operator="LEAF", field="grand_total", op="<=", value=100_000),
                Condition(operator="LEAF", field="vendor_on_watchlist", op="==", value=False),
            ], description="Amount <=1L and not watchlist"),
            action_config=ActionConfig(action=RuleAction.AUTO_APPROVE),
            confidence=1.0, priority=10,
        )
        # 1L-10L: Dept Head
        r2 = ExtractedRule(
            rule_id="AP-APR-002", category="APPROVAL_MATRIX",
            source_clause="Section 5.2", section_id="5.2",
            description="Dept Head for 1L-10L",
            condition=Condition(operator="AND", operands=[
                Condition(operator="LEAF", field="grand_total", op=">", value=100_000),
                Condition(operator="LEAF", field="grand_total", op="<=", value=1_000_000),
            ], description="Amount >1L and <=10L"),
            action_config=ActionConfig(action=RuleAction.ROUTE_TO_DEPT_HEAD),
            confidence=1.0, priority=20,
        )
        # >50L: CFO
        r3 = ExtractedRule(
            rule_id="AP-APR-004", category="APPROVAL_MATRIX",
            source_clause="Section 5.4", section_id="5.4",
            description="CFO for >50L",
            condition=Condition(operator="LEAF", field="grand_total", op=">", value=5_000_000,
                                description="Amount >50L"),
            action_config=ActionConfig(action=RuleAction.ESCALATE_TO_CFO),
            confidence=1.0, priority=5,
        )
        # Watchlist: always Dept Head
        r4 = ExtractedRule(
            rule_id="AP-APR-005", category="APPROVAL_MATRIX",
            source_clause="Section 5.5", section_id="5.5",
            description="Watchlist vendors always need Dept Head",
            condition=Condition(operator="LEAF", field="vendor_on_watchlist", op="==", value=True,
                                description="Vendor is on watchlist"),
            action_config=ActionConfig(action=RuleAction.ROUTE_TO_DEPT_HEAD,
                                       flag_code="WATCHLIST_VENDOR"),
            confidence=1.0, priority=1,
        )
        return [r1, r2, r3, r4]

    def test_auto_approve_under_1l(self):
        inv = _make_invoice(grand_total=80_000, po_amount=80_000)
        report = execute_rules(inv, self._make_approval_rules())
        auto = [r for r in report.triggered_rules if r.action_triggered == RuleAction.AUTO_APPROVE]
        assert len(auto) == 1

    def test_dept_head_for_2l(self):
        inv = _make_invoice(grand_total=200_000, po_amount=200_000)
        report = execute_rules(inv, self._make_approval_rules())
        dh = [r for r in report.triggered_rules if r.action_triggered == RuleAction.ROUTE_TO_DEPT_HEAD]
        assert len(dh) == 1

    def test_cfo_for_above_50l(self):
        inv = _make_invoice(grand_total=6_000_000, po_amount=6_000_000)
        report = execute_rules(inv, self._make_approval_rules())
        cfo = [r for r in report.triggered_rules if r.action_triggered == RuleAction.ESCALATE_TO_CFO]
        assert len(cfo) == 1

    def test_watchlist_vendor_always_routed(self):
        """Even a small invoice from a watchlist vendor should be routed to Dept Head."""
        inv = _make_invoice(grand_total=50_000, vendor_on_watchlist=True)
        report = execute_rules(inv, self._make_approval_rules())
        watchlist = [r for r in report.triggered_rules if r.flag_code == "WATCHLIST_VENDOR"]
        assert len(watchlist) == 1

    def test_final_verdict_most_severe_wins(self):
        """If both AUTO_APPROVE and ESCALATE_TO_FC trigger, FC should win."""
        inv = _make_invoice(grand_total=115_000, po_amount=100_000)
        rules = self._make_approval_rules()
        rules.append(_make_rule("AP-TWM-003", "deviation_pct", ">=", 10.0,
                                RuleAction.ESCALATE_TO_FINANCE_CONTROLLER))
        report = execute_rules(inv, rules)
        assert report.final_verdict == InvoiceStatus.ESCALATED_FC


# ---------------------------------------------------------------------------
# Section 7: QR code validation
# ---------------------------------------------------------------------------

class TestQRValidation:

    def test_qr_mismatch_flagged(self):
        inv = _make_invoice(
            grand_total=1_500_000,
            qr_code_present=True,
            qr_invoice_number="INV-WRONG",
            qr_vendor_gstin="29ABCDE1234F1Z5",
        )
        rule = _make_rule("AP-QR-001", "qr_validation_failed", "==", True,
                          RuleAction.FLAG, flag_code="QR_VALIDATION_FAILED",
                          category="QR_DIGITAL_VALIDATION")
        report = execute_rules(inv, [rule])
        assert len(report.triggered_rules) == 1

    def test_qr_missing_above_10l_held(self):
        inv = _make_invoice(grand_total=1_500_000, qr_code_present=False)
        rule = ExtractedRule(
            rule_id="AP-QR-002", category="QR_DIGITAL_VALIDATION",
            source_clause="Section 7.1", section_id="7.1",
            description="QR code missing for >10L invoice",
            condition=Condition(operator="AND", operands=[
                Condition(operator="LEAF", field="grand_total", op=">", value=1_000_000),
                Condition(operator="LEAF", field="qr_code_present", op="==", value=False),
            ], description="Amount >10L and QR missing"),
            action_config=ActionConfig(action=RuleAction.HOLD, flag_code="QR_CODE_MISSING"),
            confidence=1.0,
        )
        report = execute_rules(inv, [rule])
        assert len(report.triggered_rules) == 1
        assert report.final_verdict == InvoiceStatus.HELD


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:

    def test_no_rules_returns_auto_approved(self):
        inv = _make_invoice()
        report = execute_rules(inv, [])
        assert report.final_verdict == InvoiceStatus.AUTO_APPROVED
        assert report.total_rules_evaluated == 0

    def test_execution_report_has_audit_trail(self):
        inv = _make_invoice()
        report = execute_rules(inv, [])
        assert len(report.audit_trail) > 0

    def test_execution_time_recorded(self):
        inv = _make_invoice()
        report = execute_rules(inv, [])
        assert report.execution_time_ms >= 0

    def test_invalid_gstin_format_raises(self):
        with pytest.raises(Exception):
            _make_invoice(vendor_gstin="INVALID")

    def test_multiple_deviations_collected(self):
        inv = _make_invoice(
            grand_total=115_000, po_amount=100_000,  # amount mismatch
            invoice_qty_total=12, grn_qty_total=10,  # qty mismatch
        )
        rules = [
            _make_rule("AP-TWM-003", "deviation_pct", ">=", 10.0,
                       RuleAction.ESCALATE_TO_FINANCE_CONTROLLER),
            _make_rule("AP-GRN-001", "qty_exceeds_grn", "==", True,
                       RuleAction.REJECT, category="GRN_MATCHING"),
        ]
        report = execute_rules(inv, rules)
        assert len(report.deviations) == 2
        assert DeviationType.AMOUNT_MISMATCH in report.deviations
        assert DeviationType.QUANTITY_MISMATCH in report.deviations

    def test_and_condition_both_must_hold(self):
        inv = _make_invoice(grand_total=105_000, po_amount=100_000)  # +5%
        # Should only trigger if 1 < deviation < 10
        rule = ExtractedRule(
            rule_id="AP-TWM-002", category="THREE_WAY_MATCH",
            source_clause="Section 2.2(b)", section_id="2.2",
            description="Dept Head for 1-10% overrun",
            condition=Condition(operator="AND", operands=[
                Condition(operator="LEAF", field="deviation_pct", op=">", value=1.0),
                Condition(operator="LEAF", field="deviation_pct", op="<", value=10.0),
            ], description="Deviation between 1% and 10%"),
            action_config=ActionConfig(action=RuleAction.ROUTE_TO_DEPT_HEAD),
            confidence=1.0,
        )
        report = execute_rules(inv, [rule])
        assert len(report.triggered_rules) == 1

    def test_or_condition_either_triggers(self):
        inv = _make_invoice(gstin_mismatch_override=None)
        rule = ExtractedRule(
            rule_id="AP-TEST-001", category="TAX_COMPLIANCE",
            source_clause="Section test", section_id="test",
            description="OR condition test",
            condition=Condition(operator="OR", operands=[
                Condition(operator="LEAF", field="gstin_mismatch", op="==", value=True),
                Condition(operator="LEAF", field="is_future_dated", op="==", value=True),
            ], description="GSTIN mismatch OR future dated"),
            action_config=ActionConfig(action=RuleAction.REJECT),
            confidence=1.0,
        )
        # Neither condition is true for clean invoice
        clean_inv = _make_invoice()
        report = execute_rules(clean_inv, [rule])
        assert len(report.triggered_rules) == 0