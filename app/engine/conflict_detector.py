"""
Conflict Detection Engine
Detects contradictions, overlapping thresholds, and missing escalation paths
across extracted rules using graph analysis (NetworkX).
"""
from __future__ import annotations
import logging
from itertools import combinations
from typing import Any

try:
    import networkx as nx
    HAS_NETWORKX = True
except ImportError:
    HAS_NETWORKX = False
    logging.warning("NetworkX not installed. Graph-based conflict detection disabled.")

from app.models.schemas import (
    ExtractedRule, Conflict, ConflictType, Severity,
    RuleAction, Condition
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers: condition tree analysis
# ---------------------------------------------------------------------------

def _extract_threshold(condition: Condition, field: str) -> tuple[float | None, str | None]:
    """
    Extract (threshold_value, operator) for a specific field from a condition tree.
    Returns (None, None) if field not found.
    """
    if condition.operator == "LEAF":
        if condition.field == field and condition.value is not None:
            try:
                return float(condition.value), condition.op
            except (TypeError, ValueError):
                return None, None
        return None, None

    for operand in condition.operands:
        val, op = _extract_threshold(operand, field)
        if val is not None:
            return val, op
    return None, None


def _extract_all_fields(condition: Condition) -> set[str]:
    """Get all field names referenced in a condition tree."""
    fields: set[str] = set()
    if condition.operator == "LEAF" and condition.field:
        fields.add(condition.field)
    for op in condition.operands:
        fields |= _extract_all_fields(op)
    return fields


def _get_amount_range(rule: ExtractedRule) -> tuple[float | None, float | None]:
    """
    Try to extract an (min_amount, max_amount) range from an approval-matrix rule.
    Returns (None, None) if not determinable.
    """
    cond = rule.condition

    # Simple LEAF: "grand_total >= X"
    if cond.operator == "LEAF" and cond.field == "grand_total":
        val = cond.value
        try:
            fval = float(val)
        except (TypeError, ValueError):
            return None, None
        if cond.op in (">=", ">"):
            return fval, None
        elif cond.op in ("<=", "<"):
            return None, fval
        return None, None

    # AND: "grand_total > X AND grand_total <= Y"
    if cond.operator == "AND":
        low, high = None, None
        for op in cond.operands:
            if op.operator == "LEAF" and op.field == "grand_total":
                try:
                    fval = float(op.value)
                except (TypeError, ValueError):
                    continue
                if op.op in (">=", ">"):
                    low = fval
                elif op.op in ("<=", "<"):
                    high = fval
        return low, high

    return None, None


def _get_deviation_pct_threshold(rule: ExtractedRule) -> float | None:
    """Extract the deviation_pct threshold from a rule."""
    val, _ = _extract_threshold(rule.condition, "deviation_pct")
    return val


# ---------------------------------------------------------------------------
# Conflict detectors
# ---------------------------------------------------------------------------

def _detect_threshold_overlaps(rules: list[ExtractedRule]) -> list[Conflict]:
    """
    Find rules in APPROVAL_MATRIX or THREE_WAY_MATCH that have overlapping amount ranges.
    """
    conflicts: list[Conflict] = []
    approval_rules = [r for r in rules if r.category == "APPROVAL_MATRIX"]

    range_rules: list[tuple[ExtractedRule, float | None, float | None]] = []
    for rule in approval_rules:
        lo, hi = _get_amount_range(rule)
        if lo is not None or hi is not None:
            range_rules.append((rule, lo, hi))

    for (r1, lo1, hi1), (r2, lo2, hi2) in combinations(range_rules, 2):
        # Check overlap: two ranges [lo1, hi1] and [lo2, hi2] overlap if
        # lo1 < hi2 and lo2 < hi1 (treating None as 0 or inf)
        effective_lo1 = lo1 if lo1 is not None else 0
        effective_hi1 = hi1 if hi1 is not None else float("inf")
        effective_lo2 = lo2 if lo2 is not None else 0
        effective_hi2 = hi2 if hi2 is not None else float("inf")

        if effective_lo1 < effective_hi2 and effective_lo2 < effective_hi1:
            # Genuine overlap — only flag if they have different actions
            if r1.action_config.action != r2.action_config.action:
                conflicts.append(Conflict(
                    conflict_id=f"CONF-OVERLAP-{r1.rule_id}-{r2.rule_id}",
                    rule_ids=[r1.rule_id, r2.rule_id],
                    conflict_type=ConflictType.THRESHOLD_OVERLAP,
                    description=(
                        f"Rules {r1.rule_id} and {r2.rule_id} have overlapping amount ranges "
                        f"[{effective_lo1:,.0f}-{effective_hi1:,.0f}] ∩ [{effective_lo2:,.0f}-{effective_hi2:,.0f}] "
                        f"but trigger different actions: {r1.action_config.action} vs {r2.action_config.action}"
                    ),
                    severity=Severity.HIGH,
                    affected_field="grand_total",
                    overlap_range={
                        "min": max(effective_lo1, effective_lo2),
                        "max": min(effective_hi1, effective_hi2),
                    },
                    suggested_resolution=(
                        f"Clarify which rule takes precedence for the overlapping range. "
                        f"Consider adding explicit priority ordering or a tie-breaker condition."
                    ),
                ))

    # Also check deviation_pct overlaps in THREE_WAY_MATCH rules
    match_rules = [r for r in rules if r.category == "THREE_WAY_MATCH"]
    pct_rules: list[tuple[ExtractedRule, float | None]] = [
        (r, _get_deviation_pct_threshold(r))
        for r in match_rules
        if _get_deviation_pct_threshold(r) is not None
    ]
    pct_rules.sort(key=lambda x: x[1] or 0)

    for i in range(len(pct_rules) - 1):
        r1, pct1 = pct_rules[i]
        r2, pct2 = pct_rules[i + 1]
        if pct1 == pct2 and r1.action_config.action != r2.action_config.action:
            conflicts.append(Conflict(
                conflict_id=f"CONF-DEVPCT-{r1.rule_id}-{r2.rule_id}",
                rule_ids=[r1.rule_id, r2.rule_id],
                conflict_type=ConflictType.THRESHOLD_OVERLAP,
                description=(
                    f"Rules {r1.rule_id} and {r2.rule_id} share the same deviation_pct threshold "
                    f"({pct1}%) but trigger different actions: "
                    f"{r1.action_config.action} vs {r2.action_config.action}"
                ),
                severity=Severity.HIGH,
                affected_field="deviation_pct",
                overlap_range={"exact_pct": pct1},
                suggested_resolution=(
                    "Define explicit boundary ownership (e.g., '>= 10' for escalation, "
                    "'> 1 and < 10' for routing to avoid at-boundary ambiguity)."
                ),
            ))

    return conflicts


def _detect_contradictory_actors(rules: list[ExtractedRule]) -> list[Conflict]:
    """
    Find cases where two rules could fire on the same invoice but route to different approvers.
    Key case: Section 2.2(b) routes to Dept Head for 1-10% overrun,
    but Section 5.3 routes to Finance Controller for INR 10L-50L.
    A INR 15L invoice at 8% overrun satisfies both.
    """
    conflicts: list[Conflict] = []

    # Group rules by action
    action_groups: dict[str, list[ExtractedRule]] = {}
    for rule in rules:
        action = rule.action_config.action.value
        action_groups.setdefault(action, []).append(rule)

    APPROVER_ACTIONS = {
        RuleAction.ROUTE_TO_DEPT_HEAD,
        RuleAction.ESCALATE_TO_FINANCE_CONTROLLER,
        RuleAction.ESCALATE_TO_CFO,
    }

    routing_rules = [r for r in rules if r.action_config.action in APPROVER_ACTIONS]

    # Find pairs that both use amount-based conditions AND different approvers
    for r1, r2 in combinations(routing_rules, 2):
        if r1.action_config.action == r2.action_config.action:
            continue

        fields1 = _extract_all_fields(r1.condition)
        fields2 = _extract_all_fields(r2.condition)
        shared_fields = fields1 & fields2

        # If they share amount fields, they can conflict
        amount_fields = {"grand_total", "deviation_pct", "invoice_total"}
        if shared_fields & amount_fields:
            # Only flag cross-category conflicts (e.g., TWM rule vs approval matrix rule)
            if r1.category != r2.category:
                conflicts.append(Conflict(
                    conflict_id=f"CONF-ACTOR-{r1.rule_id}-{r2.rule_id}",
                    rule_ids=[r1.rule_id, r2.rule_id],
                    conflict_type=ConflictType.CONTRADICTORY_ACTORS,
                    description=(
                        f"Rules {r1.rule_id} ({r1.source_clause}) and {r2.rule_id} ({r2.source_clause}) "
                        f"can both fire for the same invoice but route to different approvers: "
                        f"{r1.action_config.action.value} vs {r2.action_config.action.value}. "
                        f"Shared condition fields: {', '.join(shared_fields & amount_fields)}"
                    ),
                    severity=Severity.MEDIUM,
                    affected_field=", ".join(shared_fields & amount_fields),
                    suggested_resolution=(
                        f"Define explicit precedence: which rule takes priority when both conditions are met? "
                        f"Typically the stricter action (higher approver level) should win. "
                        f"Consider adding a combined condition that covers this scenario explicitly."
                    ),
                ))

    return conflicts


def _detect_missing_escalation_paths(rules: list[ExtractedRule]) -> list[Conflict]:
    """
    Find rules that escalate_to an action but no rule handles that escalated state.
    """
    conflicts: list[Conflict] = []
    handled_actions = {r.action_config.action for r in rules}

    for rule in rules:
        escalate_to = rule.action_config.escalate_to
        if escalate_to and escalate_to not in handled_actions:
            # Check if the escalated action is a known terminal state
            terminal_actions = {RuleAction.ESCALATE_TO_CFO, RuleAction.AUTO_APPROVE, RuleAction.REJECT}
            if escalate_to not in terminal_actions:
                conflicts.append(Conflict(
                    conflict_id=f"CONF-MISSING-ESC-{rule.rule_id}",
                    rule_ids=[rule.rule_id],
                    conflict_type=ConflictType.MISSING_ESCALATION,
                    description=(
                        f"Rule {rule.rule_id} ({rule.source_clause}) escalates to "
                        f"'{escalate_to.value}' after {rule.action_config.next_action_if_unresolved_hours}h, "
                        f"but no rule in the extracted set handles that escalation state."
                    ),
                    severity=Severity.MEDIUM,
                    suggested_resolution=(
                        f"Add an explicit rule that handles the '{escalate_to.value}' action, "
                        f"or verify this is covered by the approval matrix in Section 5."
                    ),
                ))

    return conflicts


def _detect_watchlist_conflict(rules: list[ExtractedRule]) -> list[Conflict]:
    """
    Section 5.5 says watchlist vendors always need Dept Head regardless of amount.
    Section 5.1 says auto-approve up to 1L. These conflict for small watchlist invoices.
    """
    conflicts: list[Conflict] = []
    auto_approve_rules = [
        r for r in rules
        if r.action_config.action == RuleAction.AUTO_APPROVE
        and r.category == "APPROVAL_MATRIX"
    ]
    watchlist_rules = [
        r for r in rules
        if "watchlist" in (r.condition.description or "").lower()
        or "watchlist" in r.raw_clause_text.lower()
    ]

    if auto_approve_rules and watchlist_rules:
        conflict_rule_ids = [r.rule_id for r in auto_approve_rules + watchlist_rules]
        conflicts.append(Conflict(
            conflict_id="CONF-WATCHLIST-AUTOAPPROVE",
            rule_ids=conflict_rule_ids,
            conflict_type=ConflictType.CONTRADICTORY_ACTORS,
            description=(
                "Auto-approve rules (Section 5.1) may conflict with watchlist vendor rules (Section 5.5). "
                "A watchlist vendor invoice under INR 1,00,000 triggers both auto-approve and mandatory "
                "Department Head approval. Section 5.5 exception should take precedence."
            ),
            severity=Severity.HIGH,
            suggested_resolution=(
                "Auto-approve rules should include an explicit exclusion: "
                "'AND vendor_on_watchlist == False'. Section 5.5 takes precedence as a named exception."
            ),
            auto_resolvable=True,
        ))
    return conflicts


def _detect_duplicate_rules(rules: list[ExtractedRule]) -> list[Conflict]:
    """Find rules with identical conditions that trigger different actions."""
    conflicts: list[Conflict] = []
    
    # Simple dedup by (source_clause, action)
    seen: dict[str, list[ExtractedRule]] = {}
    for rule in rules:
        key = rule.source_clause
        seen.setdefault(key, []).append(rule)

    for source_clause, group in seen.items():
        if len(group) > 1:
            actions = {r.action_config.action for r in group}
            if len(actions) > 1:
                conflicts.append(Conflict(
                    conflict_id=f"CONF-DUP-{group[0].rule_id}",
                    rule_ids=[r.rule_id for r in group],
                    conflict_type=ConflictType.DUPLICATE_RULE,
                    description=(
                        f"Multiple rules extracted from {source_clause} with different actions: "
                        f"{', '.join(a.value for a in actions)}"
                    ),
                    severity=Severity.LOW,
                    suggested_resolution="Review and merge duplicate extractions from the same clause.",
                ))
    return conflicts


# ---------------------------------------------------------------------------
# Graph-based analysis
# ---------------------------------------------------------------------------

def build_rule_graph(rules: list[ExtractedRule]) -> Any:
    """
    Build a directed graph where:
    - Nodes are rules
    - Edges represent escalation paths or cross-references
    Returns NetworkX DiGraph or None if networkx not available.
    """
    if not HAS_NETWORKX:
        return None

    G = nx.DiGraph()
    for rule in rules:
        G.add_node(
            rule.rule_id,
            category=rule.category,
            action=rule.action_config.action.value,
            source_clause=rule.source_clause,
            confidence=rule.confidence,
        )

    # Add escalation edges
    rule_by_id = {r.rule_id: r for r in rules}
    for rule in rules:
        if rule.action_config.escalate_to:
            # Find rules that handle the escalated action
            for target_rule in rules:
                if target_rule.action_config.action == rule.action_config.escalate_to:
                    G.add_edge(rule.rule_id, target_rule.rule_id, edge_type="escalates_to")

    # Add cross-reference edges (if rule_ids are in related_rules)
    for rule in rules:
        for related_id in rule.related_rules:
            if related_id in rule_by_id:
                G.add_edge(rule.rule_id, related_id, edge_type="references")

    return G


def detect_circular_references(rules: list[ExtractedRule]) -> list[Conflict]:
    """Detect circular escalation paths using graph cycle detection."""
    if not HAS_NETWORKX:
        return []

    G = build_rule_graph(rules)
    if G is None:
        return []

    conflicts: list[Conflict] = []
    try:
        cycles = list(nx.simple_cycles(G))
        for cycle in cycles:
            if len(cycle) > 1:
                conflicts.append(Conflict(
                    conflict_id=f"CONF-CYCLE-{'--'.join(cycle[:3])}",
                    rule_ids=cycle,
                    conflict_type=ConflictType.CIRCULAR_REFERENCE,
                    description=f"Circular escalation path detected: {' → '.join(cycle)} → {cycle[0]}",
                    severity=Severity.CRITICAL,
                    suggested_resolution=(
                        "Break the escalation cycle by adding a terminal action (CFO approval or rejection) "
                        "to one of the rules in the cycle."
                    ),
                ))
    except Exception as e:
        logger.error(f"Cycle detection error: {e}")

    return conflicts


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def detect_conflicts(rules: list[ExtractedRule]) -> list[Conflict]:
    """
    Run all conflict detectors and return deduplicated conflict list.
    """
    all_conflicts: list[Conflict] = []

    detectors = [
        ("threshold_overlaps", _detect_threshold_overlaps),
        ("contradictory_actors", _detect_contradictory_actors),
        ("missing_escalation", _detect_missing_escalation_paths),
        ("watchlist_conflict", _detect_watchlist_conflict),
        ("duplicate_rules", _detect_duplicate_rules),
        ("circular_references", detect_circular_references),
    ]

    for name, detector in detectors:
        try:
            found = detector(rules)
            logger.info(f"Conflict detector '{name}': {len(found)} conflicts found")
            all_conflicts.extend(found)
        except Exception as e:
            logger.error(f"Conflict detector '{name}' failed: {e}")

    # Deduplicate by conflict_id
    seen_ids: set[str] = set()
    unique_conflicts: list[Conflict] = []
    for c in all_conflicts:
        if c.conflict_id not in seen_ids:
            seen_ids.add(c.conflict_id)
            unique_conflicts.append(c)

    # Sort by severity (CRITICAL > HIGH > MEDIUM > LOW)
    severity_order = {Severity.CRITICAL: 0, Severity.HIGH: 1, Severity.MEDIUM: 2, Severity.LOW: 3}
    unique_conflicts.sort(key=lambda c: severity_order.get(c.severity, 99))

    logger.info(f"Total unique conflicts detected: {len(unique_conflicts)}")
    return unique_conflicts


def get_conflict_summary(conflicts: list[Conflict]) -> dict:
    """Return a summary dict of conflicts by type and severity."""
    summary: dict = {
        "total": len(conflicts),
        "by_severity": {},
        "by_type": {},
        "auto_resolvable": sum(1 for c in conflicts if c.auto_resolvable),
    }
    for c in conflicts:
        summary["by_severity"][c.severity.value] = summary["by_severity"].get(c.severity.value, 0) + 1
        summary["by_type"][c.conflict_type.value] = summary["by_type"].get(c.conflict_type.value, 0) + 1
    return summary