"""
Policy Engine Service
Main orchestration layer. Wires together parser → extractor → conflict detector → executor.
"""
from __future__ import annotations
import logging
import time

from app.parser.document_parser import (
    parse_policy_document, iter_clauses_by_category,
    get_section_map, resolve_cross_references
)
from app.extractor.rule_extractor import extract_rules_from_clauses, reset_rule_counters
from app.engine.conflict_detector import detect_conflicts, get_conflict_summary
from app.engine.rule_executor import execute_rules
from app.notifier.email_notifier import build_payloads_from_report, dispatch_notification
from app.utils.rules_store import get_store
from app.utils.diagram_generator import generate_all_diagrams
from app.models.schemas import (
    ExtractionRequest, ExtractionResponse,
    ValidationRequest, ValidationResponse,
    ExtractedRule, Invoice, ExecutionReport
)

logger = logging.getLogger(__name__)


class PolicyEngineService:
    """Main service class. Can be used directly or via FastAPI dependency injection."""

    def __init__(self):
        self._store = get_store()

    # ------------------------------------------------------------------
    # Extraction pipeline
    # ------------------------------------------------------------------

    def extract_from_text(self, request: ExtractionRequest) -> ExtractionResponse:
        """
        Full extraction pipeline:
        text → clauses → rules → conflict detection → persist → respond
        """
        start = time.perf_counter()
        logger.info(f"Starting extraction for policy: {request.policy_name}")

        # Reset rule ID counters for a fresh extraction
        reset_rule_counters()

        # 1. Parse document into clauses
        clauses = parse_policy_document(request.policy_text)
        logger.info(f"Parsed {len(clauses)} clauses")

        if not clauses:
            return ExtractionResponse(
                policy_name=request.policy_name,
                total_clauses=0,
                total_rules_extracted=0,
                low_confidence_rules=0,
                rules=[],
                conflicts=[],
                extraction_time_ms=0,
                warnings=["No clauses could be parsed from the document."],
            )

        # 2. Build category map {full_ref -> category}
        category_map: dict[str, str] = {}
        for category, clause in iter_clauses_by_category(clauses):
            category_map[clause.full_ref] = category

        # 3. Extract rules via LLM
        rules, warnings = extract_rules_from_clauses(
            clauses,
            category_map,
            confidence_threshold=request.confidence_threshold,
        )
        logger.info(f"Extracted {len(rules)} rules")

        # 4. Enrich: attach related rules via cross-reference map
        xref_map = resolve_cross_references(clauses)
        self._attach_related_rules(rules, xref_map)

        # 5. Detect conflicts
        conflicts = detect_conflicts(rules)
        conflict_summary = get_conflict_summary(conflicts)
        logger.info(f"Conflict detection: {conflict_summary}")

        # 6. Persist
        self._store.save_rules(rules, request.policy_name)
        self._store.save_conflicts(conflicts, request.policy_name)

        elapsed_ms = (time.perf_counter() - start) * 1000
        low_conf = sum(1 for r in rules if r.confidence < request.confidence_threshold)

        if conflict_summary.get("total", 0) > 0:
            warnings.append(
                f"{conflict_summary['total']} conflicts detected "
                f"({conflict_summary.get('by_severity', {})}). "
                "Review GET /api/v1/conflicts for details."
            )

        return ExtractionResponse(
            policy_name=request.policy_name,
            total_clauses=len(clauses),
            total_rules_extracted=len(rules),
            low_confidence_rules=low_conf,
            rules=rules,
            conflicts=conflicts,
            extraction_time_ms=round(elapsed_ms, 2),
            warnings=warnings,
        )

    def _attach_related_rules(
        self, rules: list[ExtractedRule], xref_map: dict
    ) -> None:
        """Link rules that reference each other via cross-references."""
        clause_ref_to_rules: dict[str, list[str]] = {}
        for rule in rules:
            clause_ref_to_rules.setdefault(rule.source_clause, []).append(rule.rule_id)

        for rule in rules:
            related: set[str] = set()
            # If the source clause is referenced by other clauses, link those rules too
            for ref_text, referencing_clauses in xref_map.items():
                # ref_text is the target (e.g., "Section 2.3(b)")
                # referencing_clauses are clauses that mention this ref
                if rule.source_clause == ref_text:
                    for rc in referencing_clauses:
                        for rid in clause_ref_to_rules.get(rc.full_ref, []):
                            if rid != rule.rule_id:
                                related.add(rid)
            rule.related_rules = list(related)

    # ------------------------------------------------------------------
    # Validation (execution) pipeline
    # ------------------------------------------------------------------

    def validate_invoice(self, request: ValidationRequest) -> ValidationResponse:
        """
        Run an invoice through all extracted rules and return execution report.
        """
        # Load rules from store (or use in-memory if dry_run)
        rules = self._store.load_rules()

        if not rules:
            return ValidationResponse(
                report=ExecutionReport(
                    invoice_number=request.invoice.invoice_number,
                    final_verdict=__import__(
                        "app.models.schemas", fromlist=["InvoiceStatus"]
                    ).InvoiceStatus.HELD,
                    triggered_rules=[],
                    failed_rules=[],
                    skipped_rules=[],
                    total_rules_evaluated=0,
                    deviations=[],
                    audit_trail=["No rules loaded. Run extraction first."],
                ),
                summary="No rules available. Please extract rules from a policy document first.",
            )

        report = execute_rules(
            invoice=request.invoice,
            rules=rules,
            rule_ids=request.rule_ids,
        )

        # Queue notifications unless dry_run
        if not request.dry_run and report.notifications_queued > 0:
            rules_map = {r.rule_id: r for r in rules}
            payloads = build_payloads_from_report(
                request.invoice,
                report.triggered_rules,
                rules_map,
            )
            for payload in payloads:
                dispatch_notification(payload)

        summary = self._build_summary(report)
        return ValidationResponse(report=report, summary=summary)

    def _build_summary(self, report: ExecutionReport) -> str:
        verdict = report.final_verdict.value.replace("_", " ")
        n = len(report.triggered_rules)
        devs = ", ".join(d.value for d in report.deviations) if report.deviations else "none"
        return (
            f"Invoice {report.invoice_number}: {verdict}. "
            f"{n} rule(s) triggered. Deviations: {devs}. "
            f"Processed in {report.execution_time_ms:.1f}ms."
        )

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def get_rules(
        self,
        policy_name: str | None = None,
        category: str | None = None,
        min_confidence: float = 0.0,
    ) -> list[ExtractedRule]:
        rules = self._store.load_rules(policy_name)
        if category:
            rules = [r for r in rules if r.category == category]
        if min_confidence > 0:
            rules = [r for r in rules if r.confidence >= min_confidence]
        return rules

    def get_diagrams(self, policy_name: str | None = None) -> dict[str, str]:
        rules = self._store.load_rules(policy_name)
        conflicts = self._store.load_conflicts(policy_name)
        return generate_all_diagrams(rules, conflicts)

    def get_conflicts(
        self, policy_name: str | None = None, severity: str | None = None
    ) -> list:
        conflicts = self._store.load_conflicts(policy_name)
        if severity:
            conflicts = [c for c in conflicts if c.severity.value == severity.upper()]
        return conflicts


# Singleton factory
_service: PolicyEngineService | None = None

def get_service() -> PolicyEngineService:
    global _service
    if _service is None:
        _service = PolicyEngineService()
    return _service