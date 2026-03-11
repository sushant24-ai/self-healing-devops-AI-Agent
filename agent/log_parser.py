"""
log_parser.py — Smart Log Parser

Handles 10k+ line raw CI logs and extracts ONLY the relevant error section.
Three-tier extraction:
  1. Find the failing step (10k → ~500 lines)
  2. Extract error signature (500 → ~50 lines)
  3. Enrich with context (adds ~30 lines of relevant info)

No LLM needed — this is all pattern matching + heuristics.
"""

import re
import io
import zipfile
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Error patterns by language / tool
# ---------------------------------------------------------------------------

ERROR_MARKERS: dict[str, list[str]] = {
    "python": [
        r"Traceback \(most recent call last\)",
        r"^\w+Error:",
        r"^FAILED\b",
        r"^E\s+assert",
        r"pytest.*ERRORS?",
        r"ModuleNotFoundError",
        r"ImportError",
        r"SyntaxError",
    ],
    "node": [
        r"npm ERR!",
        r"Error:",
        r"TypeError:",
        r"ReferenceError:",
        r"Cannot find module",
        r"ERESOLVE",
        r"ERR_MODULE_NOT_FOUND",
        r"SyntaxError:",
        r"FAIL\s+\S+\.test\.",
    ],
    "java": [
        r"BUILD FAILURE",
        r"^\[ERROR\]",
        r"Exception in thread",
        r"java\.\w+\.(\w+Exception|\w+Error)",
        r"Compilation failure",
    ],
    "dotnet": [
        r"error CS\d+",
        r"Build FAILED",
        r"Failed\s+\S+\.\S+\.\S+",
        r"System\.\w+Exception",
    ],
    "docker": [
        r"ERROR\b.*docker",
        r"denied:",
        r"manifest unknown",
        r"pull access denied",
        r"no space left on device",
    ],
    "terraform": [
        r"│\s*Error:",
        r"Error:\s+",
        r"terraform.*error",
    ],
    "generic": [
        r"##\[error\]",
        r"exit code \d+",
        r"FATAL",
        r"CRITICAL",
        r"Process completed with exit code [^0]",
        r"command not found",
        r"permission denied",
        r"No such file or directory",
    ],
}

# Patterns indicating transient / infra issues (NOT code bugs)
TRANSIENT_PATTERNS: list[str] = [
    r"ETIMEDOUT",
    r"ECONNRESET",
    r"ECONNREFUSED",
    r"rate limit",
    r"503 Service",
    r"502 Bad Gateway",
    r"429 Too Many Requests",
    r"socket hang up",
    r"network timeout",
    r"Could not resolve host",
    r"TLS handshake timeout",
]

# Patterns indicating secret / permission issues
SECRET_PATTERNS: list[str] = [
    r"secret.*not\s+(found|set|available)",
    r"authentication failed",
    r"unauthorized",
    r"403 Forbidden",
    r"permission.*denied",
    r"GITHUB_TOKEN",
    r"credentials.*expired",
    r"access.*denied",
]

# Patterns that look like secrets in logs (for scrubbing)
SECRET_SCRUB_PATTERNS: list[str] = [
    r"(?i)(api[_-]?key|token|secret|password|passwd|auth)\s*[:=]\s*\S+",
    r"ghp_[a-zA-Z0-9]{36}",                  # GitHub PAT
    r"gho_[a-zA-Z0-9]{36}",                  # GitHub OAuth
    r"sk-[a-zA-Z0-9]{48}",                   # OpenAI API key
    r"(?i)bearer\s+[a-zA-Z0-9\-_.]+",         # Bearer tokens
    r"[a-zA-Z0-9+/]{40,}={0,2}",             # Long base64 strings (possible keys)
]


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class ParsedLog:
    """Result of parsing a CI log."""
    raw_length: int              # Total lines in raw log
    extracted_length: int        # Lines sent for analysis
    failing_step: str            # Name of the failing step
    error_section: str           # The extracted error context
    error_signature: str         # One-line error summary
    detected_language: str       # Detected language/tool
    is_transient: bool           # Looks like a transient issue?
    is_secret_issue: bool        # Looks like a secret/permission issue?
    full_context: str            # Final context package for LLM


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def parse_workflow_logs(raw_log: str, git_diff: str = "", workflow_yaml: str = "") -> ParsedLog:
    """
    Parse raw GitHub Actions log and extract only the relevant error context.

    Args:
        raw_log: Full raw log text (can be 10k+ lines)
        git_diff: The git diff of the triggering commit (optional)
        workflow_yaml: The workflow YAML content (optional)

    Returns:
        ParsedLog with extracted, scrubbed, enriched context ready for LLM
    """
    lines = raw_log.splitlines()
    raw_length = len(lines)

    # Step 1: Find the failing step
    failing_step, step_lines = _extract_failing_step(lines)

    # Step 2: Extract error signature and surrounding context
    error_section, error_signature, detected_lang = _extract_error_section(step_lines)

    # Step 3: Check for transient / secret issues
    is_transient = _matches_patterns(error_section, TRANSIENT_PATTERNS)
    is_secret_issue = _matches_patterns(error_section, SECRET_PATTERNS)

    # Step 4: Scrub potential secrets
    error_section = _scrub_secrets(error_section)

    # Step 5: Build context package for LLM
    full_context = _build_context(
        error_section=error_section,
        error_signature=error_signature,
        failing_step=failing_step,
        git_diff=git_diff,
        workflow_yaml=workflow_yaml,
    )

    return ParsedLog(
        raw_length=raw_length,
        extracted_length=len(error_section.splitlines()),
        failing_step=failing_step,
        error_section=error_section,
        error_signature=error_signature,
        detected_language=detected_lang,
        is_transient=is_transient,
        is_secret_issue=is_secret_issue,
        full_context=full_context,
    )


# ---------------------------------------------------------------------------
# Step 1: Find the failing step
# ---------------------------------------------------------------------------

def _extract_failing_step(lines: list[str]) -> tuple[str, list[str]]:
    """
    Find the failing step in GitHub Actions logs and return its lines.
    GitHub Actions marks steps with ##[group] and errors with ##[error].
    """
    # Find all ##[error] lines — these mark where failures happen
    error_line_indices = []
    for i, line in enumerate(lines):
        if "##[error]" in line or "Process completed with exit code" in line:
            error_line_indices.append(i)

    if not error_line_indices:
        # No explicit error markers — use the last 200 lines as fallback
        return "Unknown Step", lines[-200:]

    # Find the step name by looking backwards from the first error
    first_error_idx = error_line_indices[0]
    step_name = _find_step_name(lines, first_error_idx)

    # Find the step boundaries
    step_start, step_end = _find_step_boundaries(lines, first_error_idx)

    return step_name, lines[step_start:step_end]


def _find_step_name(lines: list[str], error_idx: int) -> str:
    """Search backwards from error to find the step/group name."""
    for i in range(error_idx, max(error_idx - 100, -1), -1):
        line = lines[i]
        # ##[group]Step Name
        group_match = re.search(r"##\[group\](.+)", line)
        if group_match:
            return group_match.group(1).strip()
        # Common step indicators
        step_match = re.search(r"^Run\s+(.+)$", line)
        if step_match:
            return step_match.group(1).strip()
    return "Unknown Step"


def _find_step_boundaries(lines: list[str], error_idx: int) -> tuple[int, int]:
    """
    Find the start and end of the step containing the error.
    Uses ##[group] / ##[endgroup] markers, or falls back to ±100 lines.
    """
    # Search backwards for step start
    start = max(0, error_idx - 100)
    for i in range(error_idx, max(error_idx - 200, -1), -1):
        if "##[group]" in lines[i] or re.match(r"^Run\s+", lines[i]):
            start = i
            break

    # Search forwards for step end
    end = min(len(lines), error_idx + 100)
    for i in range(error_idx, min(error_idx + 200, len(lines))):
        if "##[endgroup]" in lines[i]:
            end = i + 1
            break

    return start, end


# ---------------------------------------------------------------------------
# Step 2: Extract error signature
# ---------------------------------------------------------------------------

def _extract_error_section(step_lines: list[str]) -> tuple[str, str, str]:
    """
    From the failing step's lines, extract the core error and surrounding context.

    Returns:
        (error_section_text, one_line_signature, detected_language)
    """
    all_patterns = {}
    for lang, patterns in ERROR_MARKERS.items():
        for p in patterns:
            all_patterns[p] = lang

    # Find lines matching any error pattern
    error_hits: list[tuple[int, str, str]] = []  # (line_idx, matched_pattern, language)
    for i, line in enumerate(step_lines):
        for pattern, lang in all_patterns.items():
            if re.search(pattern, line, re.IGNORECASE):
                error_hits.append((i, pattern, lang))
                break  # One match per line is enough

    if not error_hits:
        # No pattern matched — return last 50 lines with "generic"
        section = "\n".join(step_lines[-50:])
        signature = step_lines[-1].strip() if step_lines else "Unknown error"
        return section, signature, "generic"

    # Determine dominant language
    lang_counts: dict[str, int] = {}
    for _, _, lang in error_hits:
        lang_counts[lang] = lang_counts.get(lang, 0) + 1
    detected_lang = max(lang_counts, key=lang_counts.get)  # type: ignore

    # Take the FIRST error hit as the primary error location
    primary_idx = error_hits[0][0]

    # Extract: 30 lines before (stack trace) + error line + 10 lines after
    context_before = 30
    context_after = 10
    start = max(0, primary_idx - context_before)
    end = min(len(step_lines), primary_idx + context_after + 1)
    section_lines = step_lines[start:end]

    # One-line signature: the first error line itself
    signature = step_lines[primary_idx].strip()
    # Clean up GitHub Actions formatting
    signature = re.sub(r"##\[\w+\]", "", signature).strip()

    return "\n".join(section_lines), signature, detected_lang


# ---------------------------------------------------------------------------
# Pattern matching helpers
# ---------------------------------------------------------------------------

def _matches_patterns(text: str, patterns: list[str]) -> bool:
    """Check if text matches any of the given patterns."""
    for pattern in patterns:
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False


# ---------------------------------------------------------------------------
# Step 4: Secret scrubbing
# ---------------------------------------------------------------------------

def _scrub_secrets(text: str) -> str:
    """Remove potential secrets from log text before sending to LLM."""
    scrubbed = text
    for pattern in SECRET_SCRUB_PATTERNS:
        scrubbed = re.sub(pattern, "[REDACTED]", scrubbed, flags=re.IGNORECASE)
    return scrubbed


# ---------------------------------------------------------------------------
# Step 5: Build context package
# ---------------------------------------------------------------------------

def _build_context(
    error_section: str,
    error_signature: str,
    failing_step: str,
    git_diff: str = "",
    workflow_yaml: str = "",
) -> str:
    """
    Build the final context package that goes to the LLM.
    This is the ONLY thing the LLM sees — not the full 10k log.
    """
    parts = []

    parts.append("=== ERROR SUMMARY ===")
    parts.append(f"Failing Step: {failing_step}")
    parts.append(f"Error: {error_signature}")
    parts.append("")

    parts.append("=== ERROR LOG (relevant section) ===")
    parts.append(error_section)
    parts.append("")

    if git_diff:
        # Truncate diff if too large
        diff_lines = git_diff.splitlines()
        if len(diff_lines) > 100:
            parts.append("=== GIT DIFF (truncated to relevant files) ===")
            parts.append("\n".join(diff_lines[:100]))
            parts.append(f"... ({len(diff_lines) - 100} more lines truncated)")
        else:
            parts.append("=== GIT DIFF ===")
            parts.append(git_diff)
        parts.append("")

    if workflow_yaml:
        parts.append("=== WORKFLOW YAML ===")
        parts.append(workflow_yaml)
        parts.append("")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Log ZIP extraction (GitHub Actions downloads logs as ZIP)
# ---------------------------------------------------------------------------

def extract_logs_from_zip(zip_bytes: bytes) -> dict[str, str]:
    """
    Extract log files from a GitHub Actions log ZIP archive.

    Returns:
        dict mapping job_name → log_content
    """
    logs: dict[str, str] = {}
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for name in zf.namelist():
            if name.endswith(".txt"):
                content = zf.read(name).decode("utf-8", errors="replace")
                # Job name is usually the directory name in the ZIP
                job_name = name.rsplit("/", 1)[0] if "/" in name else name
                if job_name in logs:
                    logs[job_name] += "\n" + content
                else:
                    logs[job_name] = content
    return logs
