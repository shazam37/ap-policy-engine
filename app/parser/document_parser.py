"""
Document Parser Module
Segments policy documents into structured clauses with cross-reference resolution.
Supports Markdown, plain text, and PDF (via text extraction).
"""
from __future__ import annotations
import re
import logging
from dataclasses import dataclass, field
from typing import Generator
from pathlib import Path

from app.models.schemas import Clause, CrossReference

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Matches: "### Section 2: Title", "## Section 1: Title", or just "### Title"
SECTION_HEADING_RE = re.compile(
    r"^#{1,4}\s+(?:Section\s+)?(\d+(?:\.\d+)*)[\s:\-–—]*(.*)$",
    re.IGNORECASE | re.MULTILINE,
)

# Matches: "2.1 Some text", "2.1.3 Some text"
NUMBERED_CLAUSE_RE = re.compile(
    r"^(\d+\.\d+(?:\.\d+)?)\s+(.+?)$",
    re.MULTILINE,
)

# Matches sub-clauses: "   a. text", "   (a) text", "   a) text"
SUB_CLAUSE_RE = re.compile(
    r"^\s{0,6}([a-z])[.)]\s+(.+?)$",
    re.MULTILINE,
)

# Cross-reference patterns:
# "Refer Section 2.3(b)", "See Section 4", "per Section 6.1", "Section 2.2(c)"
CROSS_REF_RE = re.compile(
    r"(?:Refer\s+|See\s+|per\s+|as\s+per\s+)?Section\s+(\d+(?:\.\d+)*)\s*(?:\(([a-z])\))?",
    re.IGNORECASE,
)

# Tolerance / threshold patterns (used for enrichment)
PERCENTAGE_RE = re.compile(r"([+\-±]?\s*\d+(?:\.\d+)?)\s*%")
AMOUNT_RE = re.compile(r"INR\s+([\d,]+(?:\.\d+)?(?:L|Lakh|lakh|CR|Cr)?)")
MINUTES_RE = re.compile(r"(\d+)\s+minutes?")
HOURS_RE = re.compile(r"(\d+)\s+hours?")


@dataclass
class ParsedSection:
    section_id: str
    title: str
    raw_text: str
    start_pos: int
    sub_clauses: list["ParsedClause"] = field(default_factory=list)


@dataclass
class ParsedClause:
    section_id: str
    sub_clause: str | None
    full_ref: str
    section_title: str
    raw_text: str
    char_position: int
    clause_index: int


def _normalize_amount(raw: str) -> float | None:
    """Convert 'INR 1,00,000' or '10L' to float."""
    raw = raw.replace(",", "").strip()
    multiplier = 1.0
    if raw.endswith(("L", "Lakh", "lakh")):
        multiplier = 100_000.0
        raw = re.sub(r"(L|Lakh|lakh)$", "", raw).strip()
    elif raw.endswith(("CR", "Cr", "cr")):
        multiplier = 10_000_000.0
        raw = re.sub(r"(CR|Cr|cr)$", "", raw).strip()
    try:
        return float(raw) * multiplier
    except ValueError:
        return None


def _extract_cross_refs(text: str) -> list[CrossReference]:
    refs = []
    seen = set()
    for m in CROSS_REF_RE.finditer(text):
        section_id = m.group(1)
        sub = m.group(2)
        ref_text = m.group(0).strip()
        key = (section_id, sub)
        if key not in seen:
            seen.add(key)
            refs.append(CrossReference(
                ref_text=ref_text,
                section_id=section_id,
                sub_clause=sub,
            ))
    return refs


def _split_into_sections(text: str) -> list[ParsedSection]:
    """Split document into top-level sections."""
    sections: list[ParsedSection] = []
    
    # Find all section headers
    heading_matches = list(SECTION_HEADING_RE.finditer(text))
    
    for i, m in enumerate(heading_matches):
        section_id = m.group(1)
        title = m.group(2).strip()
        start = m.end()
        end = heading_matches[i + 1].start() if i + 1 < len(heading_matches) else len(text)
        raw = text[start:end].strip()
        sections.append(ParsedSection(
            section_id=section_id,
            title=title,
            raw_text=raw,
            start_pos=m.start(),
        ))

    # Fallback: if no markdown headings found, try numbered clauses at top level
    if not sections:
        logger.warning("No markdown headings found, falling back to numbered clause detection")
        numbered = list(NUMBERED_CLAUSE_RE.finditer(text))
        for i, m in enumerate(numbered):
            sid = m.group(1).rsplit(".", 1)[0] if "." in m.group(1) else m.group(1)
            title = f"Section {sid}"
            start = m.start()
            end = numbered[i + 1].start() if i + 1 < len(numbered) else len(text)
            sections.append(ParsedSection(
                section_id=sid,
                title=title,
                raw_text=text[start:end].strip(),
                start_pos=start,
            ))

    return sections


def _parse_sub_clauses(section: ParsedSection, clause_counter: list[int]) -> list[ParsedClause]:
    """Extract numbered clauses and lettered sub-clauses from a section."""
    clauses: list[ParsedClause] = []
    text = section.raw_text
    
    # Find numbered sub-sections (e.g., 2.1, 2.2, 2.3)
    numbered = list(NUMBERED_CLAUSE_RE.finditer(text))
    
    if not numbered:
        # Whole section is one clause
        clause_counter[0] += 1
        clauses.append(ParsedClause(
            section_id=section.section_id,
            sub_clause=None,
            full_ref=f"Section {section.section_id}",
            section_title=section.title,
            raw_text=text.strip(),
            char_position=section.start_pos,
            clause_index=clause_counter[0],
        ))
        return clauses

    for i, m in enumerate(numbered):
        clause_id = m.group(1)
        clause_text_start = m.start()
        clause_text_end = numbered[i + 1].start() if i + 1 < len(numbered) else len(text)
        clause_raw = text[clause_text_start:clause_text_end].strip()

        # Look for lettered sub-clauses within this numbered clause
        sub_matches = list(SUB_CLAUSE_RE.finditer(clause_raw))

        if sub_matches:
            # Emit the numbered clause itself (without sub-clauses) as a parent
            preamble_end = sub_matches[0].start()
            preamble = clause_raw[:preamble_end].strip()
            if preamble:
                clause_counter[0] += 1
                clauses.append(ParsedClause(
                    section_id=clause_id,
                    sub_clause=None,
                    full_ref=f"Section {clause_id}",
                    section_title=section.title,
                    raw_text=preamble,
                    char_position=section.start_pos + clause_text_start,
                    clause_index=clause_counter[0],
                ))

            # Emit each lettered sub-clause
            for j, sm in enumerate(sub_matches):
                letter = sm.group(1)
                sub_start = sm.start()
                sub_end = sub_matches[j + 1].start() if j + 1 < len(sub_matches) else len(clause_raw)
                sub_raw = clause_raw[sub_start:sub_end].strip()
                clause_counter[0] += 1
                clauses.append(ParsedClause(
                    section_id=clause_id,
                    sub_clause=letter,
                    full_ref=f"Section {clause_id}({letter})",
                    section_title=section.title,
                    raw_text=sub_raw,
                    char_position=section.start_pos + clause_text_start + sub_start,
                    clause_index=clause_counter[0],
                ))
        else:
            clause_counter[0] += 1
            clauses.append(ParsedClause(
                section_id=clause_id,
                sub_clause=None,
                full_ref=f"Section {clause_id}",
                section_title=section.title,
                raw_text=clause_raw,
                char_position=section.start_pos + clause_text_start,
                clause_index=clause_counter[0],
            ))

    return clauses


def parse_policy_document(text: str) -> list[Clause]:
    """
    Main entry point. Takes raw policy text and returns a list of Clause objects
    with cross-references resolved.
    """
    # Normalize line endings and strip BOM
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")

    sections = _split_into_sections(text)
    if not sections:
        logger.error("Could not parse any sections from document")
        return []

    logger.info(f"Found {len(sections)} sections in document")

    clause_counter = [0]
    all_parsed: list[ParsedClause] = []

    for section in sections:
        parsed = _parse_sub_clauses(section, clause_counter)
        all_parsed.extend(parsed)

    logger.info(f"Extracted {len(all_parsed)} clauses total")

    # Convert to Clause model with cross-references
    clauses: list[Clause] = []
    for pc in all_parsed:
        cross_refs = _extract_cross_refs(pc.raw_text)
        clauses.append(Clause(
            section_id=pc.section_id,
            sub_clause=pc.sub_clause,
            full_ref=pc.full_ref,
            section_title=pc.section_title,
            raw_text=pc.raw_text,
            cross_refs=cross_refs,
            char_position=pc.char_position,
            clause_index=pc.clause_index,
        ))

    return clauses


def parse_policy_file(file_path: str | Path) -> list[Clause]:
    """Parse a policy document from a file path (supports .md, .txt, .pdf)."""
    path = Path(file_path)
    
    if path.suffix.lower() == ".pdf":
        try:
            import pypdf
            reader = pypdf.PdfReader(str(path))
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
        except ImportError:
            raise RuntimeError("pypdf required for PDF parsing: pip install pypdf")
    else:
        text = path.read_text(encoding="utf-8")

    return parse_policy_document(text)


def get_section_map(clauses: list[Clause]) -> dict[str, list[Clause]]:
    """Group clauses by section_id for quick lookup."""
    result: dict[str, list[Clause]] = {}
    for c in clauses:
        result.setdefault(c.section_id, []).append(c)
    return result


def resolve_cross_references(clauses: list[Clause]) -> dict[str, list[Clause]]:
    """
    Build a map from clause full_ref → list of referencing clauses.
    Used to flag chains: if Section 3.2(b) references Section 2.3(b),
    the executor knows to evaluate both.
    """
    ref_map: dict[str, list[Clause]] = {}
    for clause in clauses:
        for xref in clause.cross_refs:
            key = f"Section {xref.section_id}"
            if xref.sub_clause:
                key += f"({xref.sub_clause})"
            ref_map.setdefault(key, []).append(clause)
    return ref_map


def iter_clauses_by_category(clauses: list[Clause]) -> Generator[tuple[str, Clause], None, None]:
    """
    Yield (category, clause) pairs based on section_id.
    Categories map to the major sections in the AP policy.
    """
    SECTION_CATEGORIES = {
        "1": "INVOICE_VALIDATION",
        "2": "THREE_WAY_MATCH",
        "3": "GRN_MATCHING",
        "4": "TAX_COMPLIANCE",
        "5": "APPROVAL_MATRIX",
        "6": "DEVIATION_NOTIFICATIONS",
        "7": "QR_DIGITAL_VALIDATION",
    }
    for clause in clauses:
        top_section = clause.section_id.split(".")[0]
        category = SECTION_CATEGORIES.get(top_section, "GENERAL")
        yield category, clause