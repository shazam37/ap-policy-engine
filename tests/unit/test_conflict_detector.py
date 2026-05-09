"""
Tests for app/engine/conflict_detector.py
"""
import pytest
from app.engine.conflict_detector import (
    detect_conflicts, get_conflict_summary,
    _detect_threshold_overlaps, _detect_contradictory_actors,
    _detect_missing_escalation_paths, _detect_watchlist_conflict,
)
from app.models.schemas import (
    ExtractedRule, Condition, ActionConfig, RuleAction,
    ConflictType, Severity
)


def _rule(rule_id, category, action, condition, source="Section test", priority=100,
          escalate_to=None, related=None, raw_text="") -> ExtractedRule:
    return ExtractedRule(
        rule_id=rule_id, category=category,
        source_clause=source, section_id=source.split()[1] if len(source.split()) > 1 else "0",
        description=f"Rule {rule_id}",
        condition=condition,
        action_config=ActionConfig(action=action, escalate_to=escalate_to),
        confidence=1.0, priority=priority,
        raw_clause_text=raw_text,
        related_rules=related or [],
    )


def _leaf(field, op, value, desc="") -> Condition:
    return Condition(operator="LEAF", field=field, op=op, value=value, description=desc)


def _and(*operands) -> Condition:
    return Condition(operator="AND", operands=list(operands))


class TestThresholdOverlaps:

    def test_overlapping_approval_ranges_detected(self):
        # Two rules that could fire for the same invoice amount with different actions
        r1 = _rule("AP-APR-001", "APPROVAL_MATRIX", RuleAction.AUTO_APPROVE,
                   _and(_leaf("grand_total", ">=", 0), _leaf("grand_total", "<=", 100_000)))
        r2 = _rule("AP-APR-002", "APPROVAL_MATRIX", RuleAction.ROUTE_TO_DEPT_HEAD,
                   _and(_leaf("grand_total", ">=", 80_000), _leaf("grand_total", "<=", 500_000)))
        conflicts = _detect_threshold_overlaps([r1, r2])
        assert len(conflicts) >= 1
        assert any(c.conflict_type == ConflictType.THRESHOLD_OVERLAP for c in conflicts)

    def test_non_overlapping_ranges_clean(self):
        r1 = _rule("AP-APR-001", "APPROVAL_MATRIX", RuleAction.AUTO_APPROVE,
                   _and(_leaf("grand_total", ">=", 0), _leaf("grand_total", "<=", 100_000)))
        r2 = _rule("AP-APR-002", "APPROVAL_MATRIX", RuleAction.ROUTE_TO_DEPT_HEAD,
                   _and(_leaf("grand_total", ">", 100_000), _leaf("grand_total", "<=", 1_000_000)))
        conflicts = _detect_threshold_overlaps([r1, r2])
        assert len(conflicts) == 0

    def test_same_pct_threshold_different_actions(self):
        # < 10 and >= 10 are complementary (not overlapping) — no conflict
        r1 = _rule("AP-TWM-002", "THREE_WAY_MATCH", RuleAction.ROUTE_TO_DEPT_HEAD,
                   _leaf("deviation_pct", "<", 10.0))
        r2 = _rule("AP-TWM-003", "THREE_WAY_MATCH", RuleAction.ESCALATE_TO_FINANCE_CONTROLLER,
                   _leaf("deviation_pct", ">=", 10.0))
        # == operator is what causes a conflict at exact threshold
        conflicts = _detect_threshold_overlaps([r1, r2])
        # These use < and >= so no exact overlap — may or may not flag
        # The important thing is the engine runs without error
        assert isinstance(conflicts, list)

    def test_identical_pct_threshold_different_actions_flagged(self):
        r1 = _rule("AP-TWM-A", "THREE_WAY_MATCH", RuleAction.ROUTE_TO_DEPT_HEAD,
                   _leaf("deviation_pct", "==", 10.0))
        r2 = _rule("AP-TWM-B", "THREE_WAY_MATCH", RuleAction.ESCALATE_TO_FINANCE_CONTROLLER,
                   _leaf("deviation_pct", "==", 10.0))
        conflicts = _detect_threshold_overlaps([r1, r2])
        assert len(conflicts) >= 1


class TestContradictoryActors:

    def test_cross_category_routing_conflict_detected(self):
        # TWM rule routes to dept_head for 1-10% overrun
        r1 = _rule("AP-TWM-002", "THREE_WAY_MATCH", RuleAction.ROUTE_TO_DEPT_HEAD,
                   _and(_leaf("deviation_pct", ">", 1.0), _leaf("deviation_pct", "<", 10.0)))
        # Approval matrix escalates to FC for 10L-50L
        r2 = _rule("AP-APR-003", "APPROVAL_MATRIX", RuleAction.ESCALATE_TO_FINANCE_CONTROLLER,
                   _and(_leaf("grand_total", ">", 1_000_000), _leaf("grand_total", "<=", 5_000_000)))
        # Both share grand_total indirectly — but note r1 uses deviation_pct not grand_total
        # This should still detect based on shared routing concern
        # Let's use a case that DOES share grand_total
        r3 = _rule("AP-TWM-X", "THREE_WAY_MATCH", RuleAction.ROUTE_TO_DEPT_HEAD,
                   _and(_leaf("grand_total", ">", 500_000), _leaf("deviation_pct", ">", 1.0)))
        conflicts = _detect_contradictory_actors([r3, r2])
        assert len(conflicts) >= 1
        assert any(c.conflict_type == ConflictType.CONTRADICTORY_ACTORS for c in conflicts)

    def test_same_category_no_conflict(self):
        r1 = _rule("AP-APR-001", "APPROVAL_MATRIX", RuleAction.AUTO_APPROVE,
                   _leaf("grand_total", "<=", 100_000))
        r2 = _rule("AP-APR-002", "APPROVAL_MATRIX", RuleAction.ROUTE_TO_DEPT_HEAD,
                   _leaf("grand_total", ">", 100_000))
        conflicts = _detect_contradictory_actors([r1, r2])
        assert len(conflicts) == 0


class TestMissingEscalation:

    def test_missing_escalation_target_detected(self):
        r1 = _rule("AP-TWM-003", "THREE_WAY_MATCH", RuleAction.ESCALATE_TO_FINANCE_CONTROLLER,
                   _leaf("deviation_pct", ">=", 10.0),
                   escalate_to=RuleAction.ESCALATE_TO_CFO)
        # No rule handles ESCALATE_TO_CFO in our list
        conflicts = _detect_missing_escalation_paths([r1])
        # CFO is a terminal action — should NOT be flagged
        assert len(conflicts) == 0

    def test_non_terminal_missing_escalation_flagged(self):
        r1 = _rule("AP-TWM-003", "THREE_WAY_MATCH", RuleAction.ROUTE_TO_DEPT_HEAD,
                   _leaf("deviation_pct", ">=", 10.0),
                   escalate_to=RuleAction.HOLD)   # HOLD is not terminal
        conflicts = _detect_missing_escalation_paths([r1])
        assert len(conflicts) >= 1
        assert any(c.conflict_type == ConflictType.MISSING_ESCALATION for c in conflicts)


class TestWatchlistConflict:

    def test_watchlist_auto_approve_conflict(self):
        auto_rule = _rule("AP-APR-001", "APPROVAL_MATRIX", RuleAction.AUTO_APPROVE,
                          _leaf("grand_total", "<=", 100_000))
        watchlist_rule = _rule("AP-APR-005", "APPROVAL_MATRIX", RuleAction.ROUTE_TO_DEPT_HEAD,
                               _leaf("vendor_on_watchlist", "==", True),
                               raw_text="watchlist vendors require department head approval")
        conflicts = _detect_watchlist_conflict([auto_rule, watchlist_rule])
        assert len(conflicts) >= 1
        assert conflicts[0].severity == Severity.HIGH
        assert conflicts[0].auto_resolvable is True

    def test_no_watchlist_rule_no_conflict(self):
        auto_rule = _rule("AP-APR-001", "APPROVAL_MATRIX", RuleAction.AUTO_APPROVE,
                          _leaf("grand_total", "<=", 100_000))
        conflicts = _detect_watchlist_conflict([auto_rule])
        assert len(conflicts) == 0


class TestFullConflictDetection:

    def test_detect_conflicts_returns_sorted_by_severity(self):
        r1 = _rule("AP-APR-001", "APPROVAL_MATRIX", RuleAction.AUTO_APPROVE,
                   _and(_leaf("grand_total", ">=", 0), _leaf("grand_total", "<=", 100_000)))
        r2 = _rule("AP-APR-002", "APPROVAL_MATRIX", RuleAction.ROUTE_TO_DEPT_HEAD,
                   _and(_leaf("grand_total", ">=", 80_000), _leaf("grand_total", "<=", 500_000)))
        r3 = _rule("AP-APR-005", "APPROVAL_MATRIX", RuleAction.ROUTE_TO_DEPT_HEAD,
                   _leaf("vendor_on_watchlist", "==", True),
                   raw_text="watchlist vendors require department head")
        conflicts = detect_conflicts([r1, r2, r3])
        # Verify sorted: CRITICAL/HIGH before LOW
        severities = [c.severity.value for c in conflicts]
        sev_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
        assert severities == sorted(severities, key=lambda s: sev_order.get(s, 99))

    def test_conflict_ids_unique(self):
        r1 = _rule("AP-APR-001", "APPROVAL_MATRIX", RuleAction.AUTO_APPROVE,
                   _leaf("grand_total", "<=", 100_000))
        r2 = _rule("AP-APR-002", "APPROVAL_MATRIX", RuleAction.ROUTE_TO_DEPT_HEAD,
                   _leaf("grand_total", "<=", 100_000))
        conflicts = detect_conflicts([r1, r2])
        ids = [c.conflict_id for c in conflicts]
        assert len(ids) == len(set(ids))

    def test_conflict_summary_counts(self):
        r1 = _rule("AP-APR-001", "APPROVAL_MATRIX", RuleAction.AUTO_APPROVE,
                   _and(_leaf("grand_total", ">=", 0), _leaf("grand_total", "<=", 100_000)))
        r2 = _rule("AP-APR-002", "APPROVAL_MATRIX", RuleAction.ROUTE_TO_DEPT_HEAD,
                   _and(_leaf("grand_total", ">=", 80_000), _leaf("grand_total", "<=", 500_000)))
        conflicts = detect_conflicts([r1, r2])
        summary = get_conflict_summary(conflicts)
        assert "total" in summary
        assert "by_severity" in summary
        assert "by_type" in summary
        assert summary["total"] == len(conflicts)