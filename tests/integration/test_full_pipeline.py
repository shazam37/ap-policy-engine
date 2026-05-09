"""
Integration tests using the real AP Policy document.
These tests run without the LLM (using mock rules) to validate end-to-end flow.
"""
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone

from app.parser.document_parser import parse_policy_document
from app.engine.rule_executor import execute_rules
from app.engine.conflict_detector import detect_conflicts
from app.models.schemas import (
    Invoice, ExtractedRule, Condition, ActionConfig,
    RuleAction, InvoiceStatus, SupplyType
)

# Load the real policy text
import pathlib
POLICY_PATH = pathlib.Path(__file__).parent.parent.parent / "sample_data" / "ap_policy.md"


def load_policy() -> str:
    if POLICY_PATH.exists():
        return POLICY_PATH.read_text(encoding="utf-8")
    # Inline minimal policy for CI environments
    return """
### Section 1: Invoice Receipt & Basic Validation

1.1 Every invoice received must contain the following mandatory fields: Invoice Number, Invoice Date, Vendor GSTIN, Purchase Order Number, and Grand Total Amount.

1.2 If the invoice date is in the future, the invoice must be rejected with reason "Future-Dated Invoice Not Permitted."

### Section 2: Purchase Order Matching

2.2 Invoice Amount vs PO Amount:
   a. If the Invoice Total Amount is within +/- 1% of the PO Amount, the invoice is auto-approved for booking.
   b. If the Invoice Total Amount exceeds the PO Amount by more than 1% but less than 10%, the invoice is routed to the Department Head for approval.
   c. If the Invoice Total Amount exceeds the PO Amount by 10% or more, the invoice is escalated to the Finance Controller with a mandatory justification note.

### Section 5: Amount Thresholds & Approval Matrix

5.1 Invoices up to INR 1,00,000: Auto-approved if all validations pass.
5.2 Invoices between INR 1,00,001 and INR 10,00,000: Require approval from the Department Head.
5.5 If the vendor is on the Watch List, all invoices require Department Head approval.
"""


class TestParserIntegration:

    def test_real_policy_parses_correctly(self):
        text = load_policy()
        clauses = parse_policy_document(text)
        assert len(clauses) >= 5

    def test_section_2_sub_clauses_extracted(self):
        text = load_policy()
        clauses = parse_policy_document(text)
        refs = {c.full_ref for c in clauses}
        # Should have at least some of these
        expected = {"Section 2.2(a)", "Section 2.2(b)", "Section 2.2(c)"}
        found = refs & expected
        assert len(found) >= 2, f"Expected sub-clauses, got: {refs}"

    def test_cross_references_linked(self):
        text = """
### Section 3: GRN Matching

3.2 GRN Quantity check:
   b. If Invoice Quantity > GRN Quantity, the invoice is rejected. Refer Section 2.3(b) for PO-level escalation.
"""
        clauses = parse_policy_document(text)
        xref_clause = next((c for c in clauses if c.cross_refs), None)
        assert xref_clause is not None
        assert any(x.section_id == "2.3" for x in xref_clause.cross_refs)


def _build_realistic_ruleset() -> list[ExtractedRule]:
    """Build a representative ruleset mirroring the AP policy for integration testing."""

    def leaf(f, op, v, d=""): return Condition(operator="LEAF", field=f, op=op, value=v, description=d)
    def and_(*ops): return Condition(operator="AND", operands=list(ops))

    def rule(rid, cat, src, desc, cond, action, flag=None, priority=100, escalate=None):
        return ExtractedRule(
            rule_id=rid, category=cat, source_clause=src,
            section_id=src.split()[1] if len(src.split()) > 1 else "0",
            description=desc, condition=cond,
            action_config=ActionConfig(action=action, flag_code=flag, escalate_to=escalate),
            confidence=1.0, priority=priority,
        )

    return [
        rule("AP-INV-001", "INVOICE_VALIDATION", "Section 1.2",
             "Reject future-dated invoices",
             leaf("is_future_dated", "==", True), RuleAction.REJECT,
             flag="FUTURE_DATED_INVOICE", priority=5),

        rule("AP-INV-003", "INVOICE_VALIDATION", "Section 1.3",
             "Handwritten >50k needs AP Manager",
             leaf("handwritten_needs_ap_manager", "==", True),
             RuleAction.REQUEST_MANUAL_REVIEW, priority=10),

        rule("AP-TWM-001", "THREE_WAY_MATCH", "Section 2.2(a)",
             "Auto-approve within 1% tolerance",
             and_(leaf("deviation_pct", "<=", 1.0), leaf("deviation_pct", ">=", -1.0)),
             RuleAction.AUTO_APPROVE, priority=20),

        rule("AP-TWM-002", "THREE_WAY_MATCH", "Section 2.2(b)",
             "Route to Dept Head for 1-10% overrun",
             and_(leaf("deviation_pct", ">", 1.0), leaf("deviation_pct", "<", 10.0)),
             RuleAction.ROUTE_TO_DEPT_HEAD, priority=30),

        rule("AP-TWM-003", "THREE_WAY_MATCH", "Section 2.2(c)",
             "Escalate to FC for >=10% overrun",
             leaf("deviation_pct", ">=", 10.0),
             RuleAction.ESCALATE_TO_FINANCE_CONTROLLER,
             flag="AMOUNT_EXCEEDS_PO_10PCT", priority=25,
             escalate=RuleAction.ESCALATE_TO_CFO),

        rule("AP-TWM-004", "THREE_WAY_MATCH", "Section 2.2(d)",
             "Flag under-invoiced by >5%",
             leaf("under_invoiced_pct", ">", 5.0),
             RuleAction.FLAG, flag="UNDER_INVOICED", priority=35),

        rule("AP-GRN-001", "GRN_MATCHING", "Section 3.2(b)",
             "Reject when invoice qty > GRN qty",
             leaf("qty_exceeds_grn", "==", True),
             RuleAction.REJECT, flag="INVOICE_QTY_EXCEEDS_GRN", priority=15),

        rule("AP-GRN-002", "GRN_MATCHING", "Section 3.3",
             "Flag GRN post-dated vs invoice",
             leaf("grn_post_dated", "==", True),
             RuleAction.FLAG, flag="GRN_POST_DATED", priority=40),

        rule("AP-TAX-001", "TAX_COMPLIANCE", "Section 4.1",
             "Reject GSTIN mismatch",
             leaf("gstin_mismatch", "==", True),
             RuleAction.REJECT, flag="GSTIN_MISMATCH", priority=5),

        rule("AP-TAX-002", "TAX_COMPLIANCE", "Section 4.2",
             "Compliance hold on PAN mismatch",
             leaf("pan_gstin_mismatch", "==", True),
             RuleAction.COMPLIANCE_HOLD, flag="PAN_GSTIN_MISMATCH", priority=6),

        rule("AP-APR-001", "APPROVAL_MATRIX", "Section 5.1",
             "Auto-approve up to 1L for clean invoices",
             and_(leaf("grand_total", "<=", 100_000),
                  leaf("vendor_on_watchlist", "==", False)),
             RuleAction.AUTO_APPROVE, priority=50),

        rule("AP-APR-002", "APPROVAL_MATRIX", "Section 5.2",
             "Dept Head for 1L-10L",
             and_(leaf("grand_total", ">", 100_000),
                  leaf("grand_total", "<=", 1_000_000)),
             RuleAction.ROUTE_TO_DEPT_HEAD, priority=55),

        rule("AP-APR-003", "APPROVAL_MATRIX", "Section 5.3",
             "FC for 10L-50L",
             and_(leaf("grand_total", ">", 1_000_000),
                  leaf("grand_total", "<=", 5_000_000)),
             RuleAction.ESCALATE_TO_FINANCE_CONTROLLER, priority=55),

        rule("AP-APR-004", "APPROVAL_MATRIX", "Section 5.4",
             "CFO for >50L",
             leaf("grand_total", ">", 5_000_000),
             RuleAction.ESCALATE_TO_CFO, priority=55),

        rule("AP-APR-005", "APPROVAL_MATRIX", "Section 5.5",
             "Watchlist vendors need Dept Head regardless",
             leaf("vendor_on_watchlist", "==", True),
             RuleAction.ROUTE_TO_DEPT_HEAD,
             flag="WATCHLIST_VENDOR", priority=1),

        rule("AP-QR-001", "QR_DIGITAL_VALIDATION", "Section 7.1",
             "QR code required for >10L invoices",
             and_(leaf("grand_total", ">", 1_000_000),
                  leaf("qr_code_present", "==", False)),
             RuleAction.HOLD, flag="QR_CODE_MISSING", priority=20),
    ]


def _inv(**kwargs):
    defaults = dict(
        invoice_number="INV-2024-TEST",
        invoice_date=datetime(2024, 1, 15, tzinfo=timezone.utc),
        vendor_gstin="29ABCDE1234F1Z5",
        po_number="PO-2024-001",
        grand_total=100_000.0,
        vendor_name="ACME Supplies",
        gstin_in_master="29ABCDE1234F1Z5",
        pan_in_master="ABCDE1234F",
        po_amount=100_000.0,
        supply_type=SupplyType.INTRA_STATE,
        total_taxable_amount=84_746.0,
        total_cgst=7_627.0, total_sgst=7_627.0, total_igst=0.0, total_tax=15_254.0,
        invoice_qty_total=10.0, po_qty_total=10.0, grn_qty_total=10.0,
        grn_date=datetime(2024, 1, 14, tzinfo=timezone.utc),
        processing_date=datetime(2024, 1, 16, tzinfo=timezone.utc),
        place_of_supply_state_code="29", buyer_gstin_state_code="29",
        qr_code_present=False,
    )
    defaults.update(kwargs)
    return Invoice(**defaults)


class TestEndToEndScenarios:

    def setup_method(self):
        self.rules = _build_realistic_ruleset()

    def test_clean_invoice_auto_approved(self):
        """A perfectly clean invoice under 1L should be auto-approved."""
        inv = _inv()
        report = execute_rules(inv, self.rules)
        assert report.final_verdict == InvoiceStatus.AUTO_APPROVED

    def test_15pct_overrun_escalates_to_fc(self):
        inv = _inv(grand_total=115_000, po_amount=100_000)
        report = execute_rules(inv, self.rules)
        assert report.final_verdict == InvoiceStatus.ESCALATED_FC

    def test_future_dated_rejected(self):
        inv = _inv(invoice_date=datetime(2099, 1, 1, tzinfo=timezone.utc))
        report = execute_rules(inv, self.rules)
        assert report.final_verdict == InvoiceStatus.REJECTED

    def test_gstin_mismatch_rejected(self):
        inv = _inv(gstin_in_master="29ZZZZZ9999Z1Z5")
        report = execute_rules(inv, self.rules)
        assert report.final_verdict == InvoiceStatus.REJECTED

    def test_grn_qty_exceeded_rejected(self):
        inv = _inv(invoice_qty_total=12, grn_qty_total=10)
        report = execute_rules(inv, self.rules)
        assert report.final_verdict == InvoiceStatus.REJECTED

    def test_5l_invoice_routes_to_fc(self):
        inv = _inv(grand_total=3_000_000, po_amount=3_000_000)
        report = execute_rules(inv, self.rules)
        # FC handles 10L-50L; also check the triggered rules directly
        fc_rules = [r for r in report.triggered_rules
                    if r.action_triggered == RuleAction.ESCALATE_TO_FINANCE_CONTROLLER]
        assert len(fc_rules) >= 1

    def test_60l_invoice_escalates_to_cfo(self):
        inv = _inv(grand_total=6_000_000, po_amount=6_000_000,
                   qr_code_present=True,
                   qr_invoice_number="INV-2024-TEST",
                   qr_vendor_gstin="29ABCDE1234F1Z5")
        report = execute_rules(inv, self.rules)
        cfo_rules = [r for r in report.triggered_rules
                     if r.action_triggered == RuleAction.ESCALATE_TO_CFO]
        assert len(cfo_rules) >= 1

    def test_watchlist_vendor_small_invoice_routed(self):
        inv = _inv(grand_total=50_000, vendor_on_watchlist=True)
        report = execute_rules(inv, self.rules)
        dh_rules = [r for r in report.triggered_rules
                    if r.action_triggered == RuleAction.ROUTE_TO_DEPT_HEAD]
        assert len(dh_rules) >= 1

    def test_qr_missing_large_invoice_held(self):
        inv = _inv(grand_total=1_500_000, po_amount=1_500_000, qr_code_present=False)
        report = execute_rules(inv, self.rules)
        assert InvoiceStatus.HELD == report.final_verdict or any(
            r.flag_code == "QR_CODE_MISSING" for r in report.triggered_rules
        )

    def test_audit_trail_references_clauses(self):
        inv = _inv(grand_total=115_000, po_amount=100_000)
        report = execute_rules(inv, self.rules)
        # Audit trail should mention Section references
        trail_text = " ".join(report.audit_trail)
        assert "Section" in trail_text

    def test_conflict_detection_on_realistic_rules(self):
        conflicts = detect_conflicts(self.rules)
        # Should detect at least the watchlist/auto-approve conflict
        assert len(conflicts) >= 1
        conflict_types = {c.conflict_type for c in conflicts}
        assert ConflictType.CONTRADICTORY_ACTORS in conflict_types or \
               ConflictType.THRESHOLD_OVERLAP in conflict_types


from app.models.schemas import ConflictType