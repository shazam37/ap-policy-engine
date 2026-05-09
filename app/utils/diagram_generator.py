"""
Mermaid Diagram Generator
Converts extracted rules into Mermaid flowchart diagrams for documentation and README.
"""
from __future__ import annotations
import logging
from collections import defaultdict

from app.models.schemas import ExtractedRule, Conflict, RuleAction, Severity

logger = logging.getLogger(__name__)

# Mermaid-safe label: strip special chars
def _safe(text: str, max_len: int = 55) -> str:
    text = text.replace('"', "'").replace("\n", " ").replace("|", "or")
    return text[:max_len] + "…" if len(text) > max_len else text


# Action → Mermaid shape suffix
ACTION_SHAPE = {
    RuleAction.AUTO_APPROVE:                   ("([", "])"),
    RuleAction.REJECT:                         ("([", "])"),
    RuleAction.HOLD:                           ("[", "]"),
    RuleAction.FLAG:                           ("[", "]"),
    RuleAction.COMPLIANCE_HOLD:               ("[", "]"),
    RuleAction.ROUTE_TO_DEPT_HEAD:            (">", "]"),
    RuleAction.ESCALATE_TO_FINANCE_CONTROLLER:(">", "]"),
    RuleAction.ESCALATE_TO_CFO:               (">", "]"),
    RuleAction.REQUEST_MANUAL_REVIEW:          ("[", "]"),
    RuleAction.SEND_NOTIFICATION:              ("(", ")"),
}

ACTION_STYLE = {
    RuleAction.AUTO_APPROVE:                   "fill:#C8F0D8,stroke:#2D8A50,color:#1A4D2E",
    RuleAction.REJECT:                         "fill:#F5C5C5,stroke:#A32D2D,color:#6B1A1A",
    RuleAction.HOLD:                           "fill:#FFF0CC,stroke:#BA7517,color:#6B3D00",
    RuleAction.FLAG:                           "fill:#FFF0CC,stroke:#BA7517,color:#6B3D00",
    RuleAction.COMPLIANCE_HOLD:               "fill:#F5C5C5,stroke:#A32D2D,color:#6B1A1A",
    RuleAction.ROUTE_TO_DEPT_HEAD:            "fill:#D4E8FF,stroke:#1A5FAD,color:#0A2D5C",
    RuleAction.ESCALATE_TO_FINANCE_CONTROLLER:"fill:#E8D4FF,stroke:#6633AA,color:#2D0066",
    RuleAction.ESCALATE_TO_CFO:               "fill:#FFD4F5,stroke:#AA3380,color:#660033",
    RuleAction.REQUEST_MANUAL_REVIEW:          "fill:#E8E6DD,stroke:#888780,color:#2C2C2A",
    RuleAction.SEND_NOTIFICATION:              "fill:#D4F5FF,stroke:#0A7FAD,color:#003D5C",
}


def generate_section_flowchart(rules: list[ExtractedRule], section_title: str = "") -> str:
    """
    Generate a Mermaid flowchart for a list of rules (typically one section).
    Returns the raw Mermaid string.
    """
    if not rules:
        return "flowchart TD\n    EMPTY[No rules to display]"

    lines = ["flowchart TD"]
    style_lines: list[str] = []
    node_ids: dict[str, str] = {}      # rule_id -> mermaid node id
    action_node_ids: dict[str, str] = {}  # action value -> node id (shared terminals)

    # Sort by priority
    sorted_rules = sorted(rules, key=lambda r: r.priority)

    # --- Start node ---
    lines.append("    START([📄 Invoice Received])")
    lines.append("    style START fill:#2C2C2A,stroke:#2C2C2A,color:#F1EFE8")
    lines.append("")

    prev_node = "START"

    for i, rule in enumerate(sorted_rules):
        safe_id = rule.rule_id.replace("-", "_")
        node_ids[rule.rule_id] = safe_id

        # Condition diamond
        cond_label = _safe(rule.condition.description or rule.description)
        cond_node = f"COND_{safe_id}"
        lines.append(f'    {cond_node}{{"{cond_label}?"}}')
        lines.append(f"    style {cond_node} fill:#F1EFE8,stroke:#888780,color:#2C2C2A")

        # Connect from previous
        lines.append(f"    {prev_node} --> {cond_node}")

        # Action terminal node
        action = rule.action_config.action
        action_key = f"{action.value}_{i}"
        open_s, close_s = ACTION_SHAPE.get(action, ("[", "]"))
        flag = rule.action_config.flag_code or ""
        action_label = _safe(action.value.replace("_", " ") + (f"\n{flag}" if flag else ""))
        action_node = f"ACT_{safe_id}"
        lines.append(f'    {action_node}{open_s}"{action_label}"{close_s}')
        astyle = ACTION_STYLE.get(action, "fill:#E8E6DD,stroke:#888780")
        lines.append(f"    style {action_node} {astyle}")

        # YES branch → action
        lines.append(f'    {cond_node} -- "YES ✓" --> {action_node}')

        # NO branch → next rule or end
        if i < len(sorted_rules) - 1:
            next_cond = f"COND_{sorted_rules[i+1].rule_id.replace('-','_')}"
            lines.append(f'    {cond_node} -- "NO" --> {next_cond}')
            prev_node = cond_node   # not used since we explicitly wire NO
        else:
            end_node = "END_PASS"
            if end_node not in action_node_ids:
                lines.append(f'    {end_node}(["✅ All checks passed"])')
                lines.append(f"    style {end_node} fill:#C8F0D8,stroke:#2D8A50,color:#1A4D2E")
                action_node_ids[end_node] = end_node
            lines.append(f'    {cond_node} -- "NO" --> {end_node}')

        lines.append("")

    # Add section title as graph title comment
    header = f"    %% {section_title} — {len(rules)} rules\n" if section_title else ""

    return "flowchart TD\n" + header + "\n".join(lines[1:])


def generate_approval_matrix_diagram(rules: list[ExtractedRule]) -> str:
    """
    Special diagram for the approval matrix (Section 5) showing amount tiers.
    """
    approval_rules = [r for r in rules if r.category == "APPROVAL_MATRIX"]
    if not approval_rules:
        return generate_section_flowchart(rules, "Approval Matrix")

    lines = [
        "flowchart LR",
        '    INV(["💰 Invoice Amount"])',
        "    style INV fill:#2C2C2A,stroke:#2C2C2A,color:#F1EFE8",
        "",
    ]

    for rule in sorted(approval_rules, key=lambda r: r.priority):
        safe_id = rule.rule_id.replace("-", "_")
        cond_label = _safe(rule.condition.description or rule.description)
        action = rule.action_config.action
        open_s, close_s = ACTION_SHAPE.get(action, ("[", "]"))
        action_label = _safe(action.value.replace("_", " "))
        astyle = ACTION_STYLE.get(action, "fill:#E8E6DD,stroke:#888780")

        lines.append(f'    TIER_{safe_id}{{"{cond_label}"}}')
        lines.append(f"    style TIER_{safe_id} fill:#F1EFE8,stroke:#888780,color:#2C2C2A")
        lines.append(f'    OUT_{safe_id}{open_s}"{action_label}"{close_s}')
        lines.append(f"    style OUT_{safe_id} {astyle}")
        lines.append(f"    INV --> TIER_{safe_id}")
        lines.append(f'    TIER_{safe_id} -- "Yes" --> OUT_{safe_id}')
        lines.append("")

    return "\n".join(lines)


def generate_conflict_diagram(conflicts: list[Conflict]) -> str:
    """Generate a diagram showing conflict relationships between rules."""
    if not conflicts:
        return "flowchart TD\n    OK([✅ No conflicts detected])\n    style OK fill:#C8F0D8,stroke:#2D8A50"

    lines = ["flowchart TD"]
    severity_color = {
        Severity.CRITICAL: "fill:#F5C5C5,stroke:#A32D2D,color:#6B1A1A",
        Severity.HIGH:     "fill:#FFD4C5,stroke:#C04A1A,color:#6B2200",
        Severity.MEDIUM:   "fill:#FFF0CC,stroke:#BA7517,color:#6B3D00",
        Severity.LOW:      "fill:#E8E6DD,stroke:#888780,color:#2C2C2A",
    }
    seen_rules: set[str] = set()

    for i, conflict in enumerate(conflicts):
        conf_node = f"CONF_{i}"
        conf_label = _safe(f"{conflict.conflict_type.value}\n{conflict.conflict_id}", 50)
        cstyle = severity_color.get(conflict.severity, "fill:#E8E6DD,stroke:#888780")
        lines.append(f'    {conf_node}["{conf_label}"]')
        lines.append(f"    style {conf_node} {cstyle}")

        for rule_id in conflict.rule_ids:
            safe_rid = rule_id.replace("-", "_")
            if safe_rid not in seen_rules:
                lines.append(f'    RULE_{safe_rid}(("{rule_id}"))')
                lines.append(f"    style RULE_{safe_rid} fill:#D4E8FF,stroke:#1A5FAD,color:#0A2D5C")
                seen_rules.add(safe_rid)
            lines.append(f"    RULE_{safe_rid} --- {conf_node}")

    return "\n".join(lines)


def generate_full_pipeline_diagram() -> str:
    """Static diagram of the full extraction pipeline for README."""
    return """flowchart TD
    A([📄 Policy Document\nPDF / Markdown / Text]) --> B
    B[🔍 Document Parser\nSection & clause segmentation] --> C
    C[✂️ Semantic Chunker\nRule candidate boundaries] --> D
    D[🤖 LLM Rule Extractor\nGroq LLaMA-3 · Structured JSON] --> E
    E[✅ Pydantic Schema Validator\nType enforcement · Traceability] --> F
    F[⚠️ Conflict Detection Engine\nNetworkX graph · Overlap analysis] --> G
    G[⚙️ Rule Execution Engine\nDeterministic evaluation · Audit trail] --> H
    H[📧 Deviation Notifier\nAPScheduler · 48h escalation] --> I
    I([🌐 FastAPI Layer\nREST endpoints · OpenAPI docs])

    style A fill:#2C2C2A,stroke:#2C2C2A,color:#F1EFE8
    style B fill:#D4F5FF,stroke:#0A7FAD,color:#003D5C
    style C fill:#D4F5FF,stroke:#0A7FAD,color:#003D5C
    style D fill:#E8D4FF,stroke:#6633AA,color:#2D0066
    style E fill:#E8D4FF,stroke:#6633AA,color:#2D0066
    style F fill:#FFD4C5,stroke:#C04A1A,color:#6B2200
    style G fill:#FFF0CC,stroke:#BA7517,color:#6B3D00
    style H fill:#FFF0CC,stroke:#BA7517,color:#6B3D00
    style I fill:#C8F0D8,stroke:#2D8A50,color:#1A4D2E"""


def generate_all_diagrams(rules: list[ExtractedRule], conflicts: list[Conflict]) -> dict[str, str]:
    """
    Generate all diagrams keyed by name.
    Returns dict of {diagram_name: mermaid_string}.
    """
    diagrams: dict[str, str] = {}

    diagrams["full_pipeline"] = generate_full_pipeline_diagram()
    diagrams["approval_matrix"] = generate_approval_matrix_diagram(rules)
    diagrams["conflicts"] = generate_conflict_diagram(conflicts)

    # Per-category diagrams
    category_map: dict[str, list[ExtractedRule]] = defaultdict(list)
    for rule in rules:
        category_map[rule.category].append(rule)

    for category, cat_rules in category_map.items():
        diagrams[f"section_{category.lower()}"] = generate_section_flowchart(
            cat_rules, category.replace("_", " ").title()
        )

    return diagrams