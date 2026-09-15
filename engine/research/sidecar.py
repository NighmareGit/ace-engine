"""
sidecar.py — Deterministic post-processor for verdict.md markers.

Parses structured markdown verdict reports into a validated JSON schema
containing verdict, claims, sources, citations, and contradictions.
Enforces anti-mush checks on verdict position and confidence.
"""

import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

# Allowed verdict positions for anti-mush validation
ALLOWED_POSITIONS: Tuple[str, ...] = ("supported", "refuted", "inconclusive")


def _extract_section(text: str, header: str) -> str:
    """Extract content between a markdown header and the next header or EOF.

    Args:
        text: Full markdown text.
        header: Header name to search for (e.g., 'Verdict', 'Claims').

    Returns:
        Content string for the section, or empty string if not found.
    """
    pattern = rf'^##\s+{re.escape(header)}\s*\n(.*?)(?=^##\s+\w+|$)'
    match = re.search(pattern, text, re.DOTALL | re.MULTILINE)
    return match.group(1).strip() if match else ""


def _parse_verdict(content: str) -> Tuple[str, float]:
    """Extract verdict position and confidence from section content.

    Args:
        content: Text content of the Verdict section.

    Returns:
        Tuple of (position, confidence).

    Raises:
        ValueError: If position is unsupported or confidence is missing/invalid.
    """
    # Extract position
    pos_match = re.search(
        r'(?:Position|Verdict)\s*[:\-]?\s*(supported|refuted|inconclusive)',
        content,
        re.IGNORECASE,
    )
    if not pos_match:
        raise ValueError(
            "Verdict section missing 'Position' field. Expected format: "
            "'Position: supported|refuted|inconclusive'"
        )

    position = pos_match.group(1).lower()

    # Extract confidence
    conf_match = re.search(
        r'Confidence\s*[:\-]?\s*([\d.]+)', content, re.IGNORECASE
    )
    if not conf_match:
        raise ValueError(
            "Verdict section missing 'Confidence' field. "
            "Expected format: 'Confidence: 0.0-1.0'"
        )

    try:
        confidence = float(conf_match.group(1))
    except ValueError:
        raise ValueError(
            f"Invalid confidence value '{conf_match.group(1)}'. Must be a number."
        )

    if not (0.0 <= confidence <= 1.0):
        raise ValueError(
            f"Confidence {confidence} out of range [0.0, 1.0]."
        )

    return position, confidence


def _parse_claims(content: str) -> List[Dict[str, Any]]:
    """Parse claims from markdown section content.

    Args:
        content: Text content of the Claims section.

    Returns:
        List of claim dicts with id, text, source_ids.
    """
    claims: List[Dict[str, Any]] = []
    if not content.strip():
        return claims

    pattern = r'(?:^|\n)\s*[-*]?\s*\[?(\d+)\]?\s*[:\-]?\s*(.+?)(?=\n\s*[-*]?\s*\[?\d+\]?|\Z)'
    for match in re.finditer(pattern, content, re.DOTALL):
        claim_id = match.group(1)
        claim_text = match.group(2).strip()

        # Extract source IDs from text like [source:1, source:2] or [1, 2]
        source_ids = [
            s for s in re.findall(r'\[(\d+)\]', claim_text) if s != claim_id
        ]

        claims.append({
            "id": claim_id,
            "text": claim_text,
            "source_ids": source_ids,
        })
    return claims


def _parse_sources(content: str) -> List[Dict[str, Any]]:
    """Parse sources from markdown section content.

    Args:
        content: Text content of the Sources section.

    Returns:
        List of source dicts with id, type, uri, title.
    """
    sources: List[Dict[str, Any]] = []
    if not content.strip():
        return sources

    pattern = r'(?:^|\n)\s*[-*]?\s*\[?(\d+)\]?\s*[:\-]?\s*(.+?)(?=\n\s*[-*]?\s*\[?\d+\]?|\Z)'
    for match in re.finditer(pattern, content, re.DOTALL):
        source_id = match.group(1)
        source_text = match.group(2).strip()

        # Extract type and uri
        type_match = re.search(r'type\s*[:\-]?\s*(\w+)', source_text, re.IGNORECASE)
        uri_match = re.search(r'uri\s*[:\-]?\s*(\S+)', source_text, re.IGNORECASE)

        # Title is the remaining text after removing type and uri markers
        title = re.sub(r'(?:type|uri)\s*[:\-]?\s*\S+', '', source_text).strip()

        sources.append({
            "id": source_id,
            "type": type_match.group(1) if type_match else "unknown",
            "uri": uri_match.group(1) if uri_match else "",
            "title": title,
        })
    return sources


def _parse_citations(content: str) -> List[Dict[str, Any]]:
    """Parse citations from markdown section content.

    Args:
        content: Text content of the Citations section.

    Returns:
        List of citation dicts with claim_id, source_id, span, confidence.
    """
    citations: List[Dict[str, Any]] = []
    if not content.strip():
        return citations

    pattern = (
        r'(?:^|\n)\s*[-*]?\s*'
        r'(?:claim\s*[:\-]?\s*)?(\d+)\s*,?\s*'
        r'(?:source\s*[:\-]?\s*)?(\d+)\s*,?\s*'
        r'(?:span\s*[:\-]?\s*)?(\S+)\s*,?\s*'
        r'(?:confidence\s*[:\-]?\s*)?([\d.]+)'
    )
    for match in re.finditer(pattern, content, re.IGNORECASE):
        citations.append({
            "claim_id": match.group(1),
            "source_id": match.group(2),
            "span": match.group(3),
            "confidence": float(match.group(4)),
        })
    return citations


def _parse_contradictions(content: str) -> List[Dict[str, Any]]:
    """Parse contradictions from markdown section content.

    Args:
        content: Text content of the Contradictions section.

    Returns:
        List of contradiction dicts with id, between, resolution.
    """
    contradictions: List[Dict[str, Any]] = []
    if not content.strip():
        return contradictions

    pattern = (
        r'(?:^|\n)\s*[-*]?\s*'
        r'(?:between\s*[:\-]?\s*)?\[?(\d+)\s*,?\s*(\d+)\]?\s*,?\s*'
        r'(?:resolution\s*[:\-]?\s*)?(.+?)(?=\n\s*[-*]?\s*\[?\d+\]?|\Z)'
    )
    for match in re.finditer(pattern, content, re.DOTALL):
        contradictions.append({
            "id": str(uuid.uuid4()),
            "between": [match.group(1), match.group(2)],
            "resolution": match.group(3).strip(),
        })
    return contradictions


def parse_verdict_md(md_text: str) -> Dict[str, Any]:
    """Parse a verdict.md markdown string into a structured evidence JSON object.

    Args:
        md_text: Raw markdown text containing verdict markers.

    Returns:
        Dict matching the verdict.evidence.json schema.

    Raises:
        ValueError: If required sections are missing or validation fails.
    """
    verdict_content = _extract_section(md_text, "Verdict")
    if not verdict_content:
        raise ValueError("Missing '## Verdict' section in verdict.md")

    position, confidence = _parse_verdict(verdict_content)

    return {
        "verdict": position,
        "confidence": confidence,
        "verdict_id": str(uuid.uuid4()),
        "claims": _parse_claims(_extract_section(md_text, "Claims")),
        "sources": _parse_sources(_extract_section(md_text, "Sources")),
        "citations": _parse_citations(_extract_section(md_text, "Citations")),
        "contradictions": _parse_contradictions(
            _extract_section(md_text, "Contradictions")
        ),
    }