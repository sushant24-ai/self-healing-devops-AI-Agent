"""
classifier.py — Failure Classification Engine

Two-tier classification:
  Tier 1: Pattern matching (instant, free, no LLM)
  Tier 2: LLM-based classification (for novel/ambiguous errors)
"""

import re
from dataclasses import dataclass
from enum import Enum

from agent.log_parser import TRANSIENT_PATTERNS, SECRET_PATTERNS


# ---------------------------------------------------------------------------
# Classification types
# ---------------------------------------------------------------------------

class FailureType(Enum):
    CODE = "code"               # Fixable by modifying source code
    CONFIG = "config"           # Workflow YAML or env config issue
    INFRA = "infra"             # Infrastructure / external service issue
    TRANSIENT = "transient"     # Temporary glitch, just retry
    SECRET = "secret"           # Missing or expired secret/credential
    UNKNOWN = "unknown"         # Can't classify


@dataclass
class Classification:
    """Result of classifying a failure."""
    failure_type: FailureType
    confidence: int             # 0-100
    explanation: str            # Human-readable reason
    suggested_action: str       # What should be done
    tier: int                   # 1 = pattern match, 2 = LLM


# ---------------------------------------------------------------------------
# Tier 1: Pattern-based classification (fast, free)
# ---------------------------------------------------------------------------

# Code-level error patterns by category
CODE_PATTERNS: list[tuple[str, str]] = [
    # (pattern, explanation)
    (r"SyntaxError", "Python syntax error in source code"),
    (r"IndentationError", "Python indentation error in source code"),
    (r"NameError", "Python undefined variable reference"),
    (r"TypeError.*argument", "Python type mismatch in function call"),
    (r"ImportError|ModuleNotFoundError", "Missing Python import or dependency"),
    (r"AttributeError", "Accessing non-existent attribute on object"),
    (r"AssertionError|assert.*failed", "Test assertion failure"),
    (r"FAILED.*test", "Test suite failure"),
    (r"npm ERR! code ERESOLVE", "npm dependency resolution conflict"),
    (r"Cannot find module", "Missing Node.js module"),
    (r"TypeError:", "JavaScript type error"),
    (r"ReferenceError:", "JavaScript undefined reference"),
    (r"Compilation failure|error CS\d+", "Compilation error"),
    (r"BUILD FAILURE.*Compilation", "Build compilation failure"),
    (r"error TS\d+", "TypeScript compilation error"),
    (r"ESLint.*error", "Linting error"),
    (r"undefined is not", "JavaScript runtime error"),
]

CONFIG_PATTERNS: list[tuple[str, str]] = [
    (r"yaml.*error|invalid.*yaml", "YAML syntax error in configuration"),
    (r"action.*not found|uses:.*not found", "Invalid GitHub Action reference"),
    (r"runner.*not found|runs-on.*invalid", "Invalid runner specification"),
    (r"env.*not set|variable.*not defined", "Missing environment variable"),
    (r"version.*not found|setup-.*error", "Wrong tool/runtime version"),
    (r"invalid.*workflow", "Workflow file validation error"),
    (r"matrix.*error", "Matrix strategy configuration error"),
]

INFRA_PATTERNS: list[tuple[str, str]] = [
    (r"no space left on device", "Disk space exhausted on runner"),
    (r"out of memory|OOM", "Runner ran out of memory"),
    (r"docker.*daemon", "Docker daemon not available"),
    (r"registry.*unavailable", "Container registry down"),
    (r"deployment.*failed.*timeout", "Deployment timed out"),
    (r"quota.*exceeded", "Cloud resource quota exceeded"),
    (r"service.*unavailable", "External service outage"),
]


def classify_by_pattern(error_text: str) -> Classification | None:
    """
    Tier 1: Classify failure using pattern matching.
    Returns None if no pattern matches (needs LLM).
    """
    text_lower = error_text.lower()

    # Check transient first (cheapest to handle — just retry)
    for pattern in TRANSIENT_PATTERNS:
        if re.search(pattern, error_text, re.IGNORECASE):
            return Classification(
                failure_type=FailureType.TRANSIENT,
                confidence=85,
                explanation=f"Transient error detected (pattern: {pattern})",
                suggested_action="Retry the workflow",
                tier=1,
            )

    # Check secrets / permissions
    for pattern in SECRET_PATTERNS:
        if re.search(pattern, error_text, re.IGNORECASE):
            return Classification(
                failure_type=FailureType.SECRET,
                confidence=90,
                explanation="Secret or permission issue detected",
                suggested_action="Notify DevOps team — check secrets and permissions",
                tier=1,
            )

    # Check code-level errors
    for pattern, explanation in CODE_PATTERNS:
        if re.search(pattern, error_text, re.IGNORECASE):
            return Classification(
                failure_type=FailureType.CODE,
                confidence=80,
                explanation=explanation,
                suggested_action="Generate code fix via LLM",
                tier=1,
            )

    # Check config errors
    for pattern, explanation in CONFIG_PATTERNS:
        if re.search(pattern, error_text, re.IGNORECASE):
            return Classification(
                failure_type=FailureType.CONFIG,
                confidence=75,
                explanation=explanation,
                suggested_action="Notify DevOps team — configuration change needed",
                tier=1,
            )

    # Check infra errors
    for pattern, explanation in INFRA_PATTERNS:
        if re.search(pattern, error_text, re.IGNORECASE):
            return Classification(
                failure_type=FailureType.INFRA,
                confidence=80,
                explanation=explanation,
                suggested_action="Notify DevOps team — infrastructure issue",
                tier=1,
            )

    return None  # No pattern matched → needs LLM


# ---------------------------------------------------------------------------
# Tier 2: LLM-based classification
# ---------------------------------------------------------------------------

CLASSIFICATION_PROMPT = """You are a CI/CD failure classifier. Analyze the following error log and classify it.

ERROR LOG:
{error_text}

Classify this failure into EXACTLY ONE of these categories:
1. CODE — A bug in the source code (syntax error, failing test, missing import, type error)
2. CONFIG — A configuration issue (wrong env var, bad YAML, wrong tool version)
3. INFRA — An infrastructure problem (disk full, OOM, service outage, network issue)
4. TRANSIENT — A temporary glitch that would likely pass on retry (timeout, rate limit)
5. SECRET — A missing or expired secret, token, or credential

Respond in this EXACT JSON format (no markdown, no explanation, just JSON):
{{"type": "CODE|CONFIG|INFRA|TRANSIENT|SECRET", "confidence": 0-100, "explanation": "brief reason", "suggested_action": "what to do"}}
"""

def build_classification_prompt(error_text: str) -> str:
    """Build the LLM prompt for Tier 2 classification."""
    return CLASSIFICATION_PROMPT.format(error_text=error_text[:3000])


def parse_classification_response(response_text: str) -> Classification:
    """Parse the LLM's JSON response into a Classification object."""
    import json

    # Try to extract JSON from the response
    text = response_text.strip()

    # Handle markdown code block wrapping
    if "```" in text:
        json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if json_match:
            text = json_match.group(1)

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Last resort: try to find JSON object in text
        json_match = re.search(r"\{[^{}]+\}", text, re.DOTALL)
        if json_match:
            data = json.loads(json_match.group(0))
        else:
            return Classification(
                failure_type=FailureType.UNKNOWN,
                confidence=30,
                explanation="Failed to parse LLM classification response",
                suggested_action="Notify DevOps team for manual review",
                tier=2,
            )

    type_str = data.get("type", "UNKNOWN").upper()
    type_map = {
        "CODE": FailureType.CODE,
        "CONFIG": FailureType.CONFIG,
        "INFRA": FailureType.INFRA,
        "TRANSIENT": FailureType.TRANSIENT,
        "SECRET": FailureType.SECRET,
    }

    return Classification(
        failure_type=type_map.get(type_str, FailureType.UNKNOWN),
        confidence=int(data.get("confidence", 50)),
        explanation=data.get("explanation", "LLM classification"),
        suggested_action=data.get("suggested_action", "Review manually"),
        tier=2,
    )
