"""
Tests for app/parser/document_parser.py
"""
import pytest
from app.parser.document_parser import (
    parse_policy_document, get_section_map,
    resolve_cross_references, iter_clauses_by_category,
)

SAMPLE_POLICY = """
# Cashflo Accounts Payable Policy
## Invoice Booking & Three-Way Match Rules

### Section 1: Invoice Receipt & Basic Validation

1.1 Every invoice received must contain the following mandatory fields: Invoice Number, Invoice Date, Vendor GSTIN, Purchase Order Number, and Grand Total Amount. If any mandatory field is missing, the invoice shall be flagged as "Incomplete" and routed to the AP Clerk for manual review.

1.2 If the invoice date is in the future (i.e., later than the current processing date), the invoice must be rejected with reason "Future-Dated Invoice Not Permitted."

1.3 Handwritten invoices above INR 50,000 require additional approval from the AP Manager before booking.

### Section 2: Purchase Order Matching

2.1 Every invoice must reference a valid Purchase Order (PO). If the PO Number on the invoice does not match any active PO in the system, the invoice shall be rejected with reason "Invalid PO Reference."

2.2 Invoice Amount vs PO Amount:
   a. If the Invoice Total Amount is within +/- 1% of the PO Amount (tolerance), the invoice is auto-approved for booking.
   b. If the Invoice Total Amount exceeds the PO Amount by more than 1% but less than 10%, the invoice is routed to the Department Head for approval.
   c. If the Invoice Total Amount exceeds the PO Amount by 10% or more, the invoice is escalated to the Finance Controller with a mandatory justification note.
   d. If the Invoice Total Amount is less than the PO Amount by more than 5%, a flag "Under-Invoiced — Verify Partial Delivery" must be raised.
"""


class TestParseDocument:
    def test_returns_clauses(self):
        clauses = parse_policy_document(SAMPLE_POLICY)
        assert len(clauses) > 0

    def test_section_ids_extracted(self):
        clauses = parse_policy_document(SAMPLE_POLICY)
        section_ids = {c.section_id for c in clauses}
        assert "1.1" in section_ids
        assert "1.2" in section_ids
        assert "2.2" in section_ids

    def test_sub_clauses_extracted(self):
        clauses = parse_policy_document(SAMPLE_POLICY)
        sub_refs = {c.full_ref for c in clauses}
        assert "Section 2.2(a)" in sub_refs
        assert "Section 2.2(b)" in sub_refs
        assert "Section 2.2(c)" in sub_refs
        assert "Section 2.2(d)" in sub_refs

    def test_full_ref_format(self):
        clauses = parse_policy_document(SAMPLE_POLICY)
        for c in clauses:
            assert c.full_ref.startswith("Section ")

    def test_section_title_populated(self):
        clauses = parse_policy_document(SAMPLE_POLICY)
        titles = {c.section_title for c in clauses}
        assert any("Invoice" in t for t in titles)

    def test_raw_text_not_empty(self):
        clauses = parse_policy_document(SAMPLE_POLICY)
        for c in clauses:
            assert len(c.raw_text.strip()) > 0

    def test_clause_index_sequential(self):
        clauses = parse_policy_document(SAMPLE_POLICY)
        indices = [c.clause_index for c in clauses]
        assert indices == sorted(indices)
        assert len(set(indices)) == len(indices), "Clause indices must be unique"

    def test_empty_document(self):
        clauses = parse_policy_document("")
        assert clauses == []

    def test_plain_text_fallback(self):
        plain = "1.1 Invoice must have a PO number.\n2.1 Match must be done."
        clauses = parse_policy_document(plain)
        assert len(clauses) >= 1


class TestCrossReferences:
    def test_cross_refs_extracted(self):
        text = """
### Section 3: GRN Matching

3.2 Invoice Quantity vs GRN Quantity:
   b. If Invoice Quantity > GRN Quantity, the invoice is rejected. Refer Section 2.3(b) for PO-level escalation.
"""
        clauses = parse_policy_document(text)
        clause_with_ref = next((c for c in clauses if c.sub_clause == "b"), None)
        assert clause_with_ref is not None
        assert len(clause_with_ref.cross_refs) > 0
        assert any(xr.section_id == "2.3" for xr in clause_with_ref.cross_refs)

    def test_no_false_positive_refs(self):
        text = """
### Section 1: Basic Validation

1.1 Every invoice must have a valid PO number and vendor GSTIN.
"""
        clauses = parse_policy_document(text)
        for c in clauses:
            assert len(c.cross_refs) == 0, f"Unexpected cross-ref in {c.full_ref}"


class TestSectionMap:
    def test_section_map_groups_correctly(self):
        clauses = parse_policy_document(SAMPLE_POLICY)
        smap = get_section_map(clauses)
        assert "1.1" in smap or "1.2" in smap  # at least one top-level section

    def test_section_map_values_are_lists(self):
        clauses = parse_policy_document(SAMPLE_POLICY)
        smap = get_section_map(clauses)
        for k, v in smap.items():
            assert isinstance(v, list)
            assert all(hasattr(c, "full_ref") for c in v)


class TestCategoryIterator:
    def test_categories_assigned(self):
        clauses = parse_policy_document(SAMPLE_POLICY)
        pairs = list(iter_clauses_by_category(clauses))
        categories = {cat for cat, _ in pairs}
        assert "INVOICE_VALIDATION" in categories
        assert "THREE_WAY_MATCH" in categories

    def test_all_clauses_have_category(self):
        clauses = parse_policy_document(SAMPLE_POLICY)
        pairs = list(iter_clauses_by_category(clauses))
        assert len(pairs) == len(clauses)