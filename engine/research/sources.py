"""File source verification module for the research engine.

This module provides functionality to verify file-based evidence sources
by checking path safety and content span matching. It supports both
full verification and sampled verification modes with automatic
escalation to 100% coverage on any failure.
"""

import os
import re
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Set, Tuple


class VerificationStatus(Enum):
    """Enum representing the status of a verification check."""
    PASSED = "passed"
    FAILED = "failed"


@dataclass
class SourceCheckResult:
    """Result of a single evidence source verification.

    Attributes:
        source: The source path or identifier that was verified.
        status: Whether the verification passed or failed.
        message: Human-readable description of the result.
        is_stage_4_sub_check: Whether this is a stage-4 sub-check failure.
    """
    source: str
    status: VerificationStatus
    message: str = ""
    is_stage_4_sub_check: bool = False


# Backward-compatible alias (deprecated: use SourceCheckResult).
EvidenceResult = SourceCheckResult


def _normalize_whitespace(text: str) -> str:
    """Normalize whitespace in text by collapsing runs of whitespace.

    Replaces all sequences of whitespace characters with a single space
    and strips leading/trailing whitespace.

    Args:
        text: The input text to normalize.

    Returns:
        The normalized text with collapsed whitespace.
    """
    return " ".join(text.split())


def _is_path_safe(file_path: str, project_root: str) -> bool:
    """Check if a file path is safe and within the project root.

    Validates that the path does not contain traversal sequences (..),
    does not start with ~ or absolute path markers outside the project,
    and resolves to a location within the project root directory.

    Args:
        file_path: The file path to validate.
        project_root: The root directory of the project.

    Returns:
        True if the path is safe and within the project root, False otherwise.
    """
    # Reject paths starting with ~ (home directory expansion)
    if file_path.startswith("~"):
        return False

    # Reject absolute paths outside project root
    if file_path.startswith("/"):
        try:
            resolved = os.path.realpath(file_path)
            project_resolved = os.path.realpath(project_root)
            return resolved.startswith(project_resolved + os.sep) or resolved == project_resolved
        except (ValueError, OSError):
            return False

    # Reject paths containing .. traversal sequences
    if ".." in file_path.split(os.sep):
        return False

    # Resolve the full path and verify it stays within project root
    try:
        full_path = os.path.join(project_root, file_path)
        resolved = os.path.realpath(full_path)
        project_resolved = os.path.realpath(project_root)
        return resolved.startswith(project_resolved + os.sep) or resolved == project_resolved
    except (ValueError, OSError):
        return False


def _strip_diff_markers(text: str) -> str:
    """Strip diff/patch line prefixes for normalized comparison.

    Diff files (.diff, .patch) prefix each line with '+' , '-', ' ', or '@@'.
    When a citation span is copied from a diff, the model may or may not
    reproduce these prefixes verbatim.  To avoid false negatives in span
    verification, we provide a normalized form that strips these markers.

    Only strips at the START of each line (line-prefix), not interior '+'
    characters (e.g. "a + b" is untouched).  Lines starting with '+++' or
    '---' (diff file headers) have the prefix stripped to the bare path so
    that "+++ b/src/foo.cpp" matches a span reading "b/src/foo.cpp".
    """
    lines = text.split("\n")
    stripped = []
    for line in lines:
        # Unified-diff file headers: "+++ b/path" or "--- a/path"
        stripped_line = re.sub(r'^\+\+\+\s+', '', line)
        stripped_line = re.sub(r'^---\s+', '', stripped_line)
        # Context/add/remove lines: "+text", "-text", " text"
        # Only strip a single leading + or - followed by content.
        stripped_line = re.sub(r'^[+\-]\s?', '', stripped_line)
        # Diff hunk headers: "@@ -x,y +a,b @@" — strip entirely.
        stripped_line = re.sub(r'^@@\s+.*@@\s*$', '', stripped_line)
        stripped.append(stripped_line)
    return "\n".join(stripped)


def _verify_span_match(file_path: str, expected_span: str, project_root: str) -> bool:
    """Verify that an expected text span exists in a file with normalized whitespace.

    Reads the file content, normalizes whitespace in both the file content
    and the expected span, then checks if the span is a substring of the content.

    For diff/patch files, also tries a comparison that strips diff line
    markers ('+', '-', '+++ b/path', '@@ ... @@') — models copying spans
    from diffs often drop or mangle these prefixes.

    Args:
        file_path: Path to the file relative to project root.
        expected_span: The text span to search for in the file.
        project_root: The root directory of the project.

    Returns:
        True if the normalized span is found in the normalized file content,
        False otherwise.
    """
    full_path = os.path.join(project_root, file_path)
    try:
        with open(full_path, "r", encoding="utf-8") as f:
            content = f.read()
    except (IOError, OSError, UnicodeDecodeError):
        return False

    normalized_content = _normalize_whitespace(content)
    normalized_span = _normalize_whitespace(expected_span)

    # Primary check: exact substring match (normalized whitespace).
    if normalized_span in normalized_content:
        return True

    # Diff-marker accommodation: strip '+'/'-'/'@@' prefixes from both sides
    # and retry.  This handles the common case where the model copies a diff
    # hunk but omits or alters the line prefixes.  Only applied when the
    # file looks like a diff (contains 'diff --git' or '@@' markers) to
    # avoid loosening verification for non-diff files.
    if "diff --git" in content or "\n@@" in content or content.startswith("@@"):
        stripped_content = _normalize_whitespace(_strip_diff_markers(content))
        stripped_span = _normalize_whitespace(_strip_diff_markers(expected_span))
        if stripped_span and stripped_span in stripped_content:
            return True

    return False


def _verify_single_source(
    source: dict,
    project_root: str,
) -> SourceCheckResult:
    """Verify a single file source for path safety and span matching.

    Checks that the file path is safe (no traversal attacks) and that
    the expected span substring-matches the file content with normalized
    whitespace.

    Args:
        source: Dictionary containing 'path' and 'span' keys.
        project_root: The root directory of the project.

    Returns:
        SourceCheckResult with verification status and message.
    """
    file_path: str = source.get("path", "")
    expected_span: str = source.get("span", "")

    # Check path safety first
    if not _is_path_safe(file_path, project_root):
        return SourceCheckResult(
            source=file_path,
            status=VerificationStatus.FAILED,
            message="Path traversal detected or path outside project root",
            is_stage_4_sub_check=True,
        )

    # Verify span match
    match: bool = _verify_span_match(file_path, expected_span, project_root)

    if match:
        return SourceCheckResult(
            source=file_path,
            status=VerificationStatus.PASSED,
            message="Span matched",
        )
    else:
        return SourceCheckResult(
            source=file_path,
            status=VerificationStatus.FAILED,
            message="Span not found in file content",
            is_stage_4_sub_check=True,
        )


def verify_file_sources(
    sources: List[dict],
    project_root: str,
    sampled: bool = False,
) -> List[SourceCheckResult]:
    """Verify file sources for evidence with optional sampling and escalation.

    Verifies file-based evidence sources by checking path safety and
    content span matching. In sampled mode, only 20% of sources are
    verified initially; if any failure occurs, verification escalates
    to 100% coverage for all remaining sources.

    Args:
        sources: List of source dictionaries, each containing 'path' and 'span' keys.
        project_root: The root directory of the project for path validation.
        sampled: If True, verify 20% of sources initially and escalate to 100%
                 on any failure. If False, verify all sources.

    Returns:
        List of SourceCheckResult objects for each source.
    """
    results: List[SourceCheckResult] = []
    total: int = len(sources)

    if total == 0:
        return results

    # Calculate sample size (20% of sources)
    sample_size: int = max(1, int(total * 0.2))

    # Determine which indices to verify
    indices_to_verify: Set[int] = set(range(sample_size))
    verified_indices: Set[int] = set()

    # Phase 1: Verify the initial sample
    for i in range(sample_size):
        source = sources[i]
        result = _verify_single_source(source, project_root)
        results.append(result)
        verified_indices.add(i)

        # On any failure, escalate to 100% verification
        if result.status == VerificationStatus.FAILED:
            # Add remaining unverified indices
            for j in range(sample_size, total):
                if j not in verified_indices:
                    indices_to_verify.add(j)
            break

    # Phase 2: Verify remaining sources if escalated or in full mode
    if not sampled:
        # Full verification mode: verify all sources
        for i in range(total):
            if i not in verified_indices:
                source = sources[i]
                result = _verify_single_source(source, project_root)
                results.append(result)
                verified_indices.add(i)
    else:
        # Sampled mode: verify remaining only if escalated
        if len(indices_to_verify) > sample_size:
            for i in range(sample_size, total):
                if i not in verified_indices:
                    source = sources[i]
                    result = _verify_single_source(source, project_root)
                    results.append(result)
                    verified_indices.add(i)
        else:
            # No escalation needed: mark remaining as skipped
            for i in range(sample_size, total):
                results.append(SourceCheckResult(
                    source=sources[i].get("path", ""),
                    status=VerificationStatus.PASSED,
                    message="Skipped (sampled)",
                ))

    return results