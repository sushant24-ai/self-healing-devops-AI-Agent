"""
fixer.py — LLM-Powered Fix Generation

Generates code fixes using any LLM provider (OpenAI, Anthropic Claude, Google Gemini).
Provider and model version are fully configurable.
Builds a focused context package and parses structured fix responses.
"""

import os
import re
import json
from dataclasses import dataclass
from enum import Enum


# ---------------------------------------------------------------------------
# Provider detection
# ---------------------------------------------------------------------------

class LLMProvider(Enum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GEMINI = "gemini"


# Default models per provider
DEFAULT_MODELS: dict[str, str] = {
    "openai": "gpt-4o",
    "anthropic": "claude-sonnet-4-20250514",
    "gemini": "gemini-2.0-flash",
}

# Mapping: model name prefix → provider (for auto-detection)
MODEL_PREFIX_MAP: list[tuple[str, LLMProvider]] = [
    ("gpt-", LLMProvider.OPENAI),
    ("o1", LLMProvider.OPENAI),
    ("o3", LLMProvider.OPENAI),
    ("o4", LLMProvider.OPENAI),
    ("claude-", LLMProvider.ANTHROPIC),
    ("gemini-", LLMProvider.GEMINI),
]

# Env var for API key per provider
API_KEY_ENV_MAP: dict[LLMProvider, str] = {
    LLMProvider.OPENAI: "OPENAI_API_KEY",
    LLMProvider.ANTHROPIC: "ANTHROPIC_API_KEY",
    LLMProvider.GEMINI: "GEMINI_API_KEY",
}


def detect_provider(model: str) -> LLMProvider:
    """Auto-detect the LLM provider from the model name."""
    model_lower = model.lower()
    for prefix, provider in MODEL_PREFIX_MAP:
        if model_lower.startswith(prefix):
            return provider
    # Default fallback
    return LLMProvider.OPENAI


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class FileChange:
    """A single file modification proposed by the LLM."""
    file_path: str       # Path relative to repo root
    action: str          # "modify" | "create" | "delete"
    content: str         # New file content (for modify/create)
    diff: str            # Human-readable diff description
    explanation: str     # Why this change fixes the issue


@dataclass
class FixProposal:
    """A complete fix proposal from the LLM."""
    changes: list[FileChange]
    explanation: str         # Overall explanation of the fix
    root_cause: str          # What caused the failure
    confidence: int          # 0-100 how confident the LLM is
    fix_title: str           # Short title for aifix.md


# ---------------------------------------------------------------------------
# LLM Client — Multi-Provider
# ---------------------------------------------------------------------------

class LLMClient:
    """
    Universal LLM client supporting OpenAI, Anthropic Claude, and Google Gemini.

    Usage:
        # Auto-detect provider from model name
        llm = LLMClient(model="claude-sonnet-4-20250514")
        llm = LLMClient(model="gpt-4o")
        llm = LLMClient(model="gemini-2.0-flash")

        # Explicit provider
        llm = LLMClient(provider="anthropic", model="claude-sonnet-4-20250514")

        # Custom API key
        llm = LLMClient(api_key="sk-...", model="gpt-4o-mini")
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        provider: str | None = None,
    ):
        # Resolve provider
        if provider:
            self.provider = LLMProvider(provider.lower())
        elif model:
            self.provider = detect_provider(model)
        else:
            self.provider = LLMProvider.OPENAI

        # Resolve model
        self.model = model or DEFAULT_MODELS.get(self.provider.value, "gpt-4o")

        # Resolve API key: explicit > env var
        env_key = API_KEY_ENV_MAP.get(self.provider, "OPENAI_API_KEY")
        self.api_key = api_key or os.environ.get(env_key, "")

        self._client = None

        print(f"  [LLM] Provider: {self.provider.value} | Model: {self.model}")

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        """Send a prompt to the LLM and return the response text."""
        if self.provider == LLMProvider.OPENAI:
            return self._generate_openai(system_prompt, user_prompt)
        elif self.provider == LLMProvider.ANTHROPIC:
            return self._generate_anthropic(system_prompt, user_prompt)
        elif self.provider == LLMProvider.GEMINI:
            return self._generate_gemini(system_prompt, user_prompt)
        else:
            raise ValueError(f"Unsupported provider: {self.provider}")

    # --- OpenAI ---
    def _generate_openai(self, system_prompt: str, user_prompt: str) -> str:
        try:
            from openai import OpenAI
        except ImportError:
            raise ImportError("openai package not installed. Run: pip install openai")

        client = OpenAI(api_key=self.api_key)
        response = client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
            max_tokens=4000,
        )
        return response.choices[0].message.content or ""

    # --- Anthropic Claude ---
    def _generate_anthropic(self, system_prompt: str, user_prompt: str) -> str:
        try:
            import anthropic
        except ImportError:
            raise ImportError("anthropic package not installed. Run: pip install anthropic")

        client = anthropic.Anthropic(api_key=self.api_key)
        response = client.messages.create(
            model=self.model,
            max_tokens=4000,
            system=system_prompt,
            messages=[
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
        )
        # Anthropic returns content blocks
        return response.content[0].text if response.content else ""

    # --- Google Gemini ---
    def _generate_gemini(self, system_prompt: str, user_prompt: str) -> str:
        try:
            from google import genai
        except ImportError:
            raise ImportError(
                "google-genai package not installed. Run: pip install google-genai"
            )

        client = genai.Client(api_key=self.api_key)
        response = client.models.generate_content(
            model=self.model,
            contents=f"{system_prompt}\n\n{user_prompt}",
            config={
                "temperature": 0.2,
                "max_output_tokens": 4000,
            },
        )
        return response.text or ""


# ---------------------------------------------------------------------------
# Fix generation
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are an expert CI/CD debugging agent. You analyze failed GitHub Actions workflows and generate precise code fixes.

RULES:
1. Generate the MINIMUM change needed to fix the error. Don't refactor, don't add features.
2. Never remove or weaken test assertions. Fix the code under test instead.
3. Never remove security checks, authentication, or authorization logic.
4. If the fix requires adding a dependency, include the change to the package manifest.
5. Always explain WHY the change fixes the issue.
6. Be specific about file paths (relative to repo root).

RESPONSE FORMAT — respond with ONLY this JSON (no markdown wrapping, no commentary):
{
  "fix_title": "Short descriptive title of the fix",
  "root_cause": "What caused the failure",
  "confidence": 0-100,
  "explanation": "Overall explanation of the fix",
  "changes": [
    {
      "file_path": "path/to/file.py",
      "action": "modify",
      "content": "FULL new content of the file",
      "diff_description": "Changed line X from A to B",
      "explanation": "Why this specific change"
    }
  ]
}
"""

USER_PROMPT_TEMPLATE = """A GitHub Actions workflow has failed. Analyze the error and generate a fix.

{context}

IMPORTANT:
- Only fix the specific error shown above
- Provide the COMPLETE new file content for each changed file
- Do NOT guess file contents you haven't seen — only modify files whose content is provided
- Be surgical: smallest change possible
"""

REFLECTION_PROMPT = """Your previous fix attempt FAILED. The CI still fails after applying your fix.

PREVIOUS FIX ATTEMPT:
{previous_fix}

NEW ERROR AFTER YOUR FIX:
{new_error}

REFLECTIONS FROM PREVIOUS ATTEMPTS:
{reflections}

Generate a NEW fix that addresses the remaining issue. Learn from what went wrong.
Consider:
- Did you fix the wrong file?
- Did you fix the symptom instead of the root cause?
- Did your fix introduce a new error?
"""


def generate_fix(
    llm: LLMClient,
    context: str,
    previous_attempts: list[dict] | None = None,
) -> FixProposal:
    """
    Generate a fix proposal using the LLM.

    Args:
        llm: LLM client instance
        context: The full context package from log_parser
        previous_attempts: List of previous fix attempts (for Reflexion loop)

    Returns:
        FixProposal with the proposed changes
    """
    if previous_attempts:
        # Build reflection context from previous failed attempts
        reflections = []
        for i, attempt in enumerate(previous_attempts, 1):
            reflections.append(
                f"Attempt {i}: {attempt.get('explanation', 'N/A')} → "
                f"Result: {attempt.get('result', 'Failed')}"
            )

        user_prompt = REFLECTION_PROMPT.format(
            previous_fix=previous_attempts[-1].get("explanation", "N/A"),
            new_error=context,
            reflections="\n".join(reflections),
        )
    else:
        user_prompt = USER_PROMPT_TEMPLATE.format(context=context)

    # Call LLM
    response_text = llm.generate(SYSTEM_PROMPT, user_prompt)

    # Parse response
    return _parse_fix_response(response_text)


def _parse_fix_response(response_text: str) -> FixProposal:
    """Parse the LLM's JSON response into a FixProposal."""
    text = response_text.strip()

    # Handle markdown code block wrapping
    if "```" in text:
        json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if json_match:
            text = json_match.group(1)

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Try to find JSON in text
        json_match = re.search(r"\{.*\}", text, re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group(0))
            except json.JSONDecodeError:
                return FixProposal(
                    changes=[],
                    explanation="Failed to parse LLM response",
                    root_cause="Unknown",
                    confidence=0,
                    fix_title="Parse failure",
                )
        else:
            return FixProposal(
                changes=[],
                explanation="Failed to parse LLM response",
                root_cause="Unknown",
                confidence=0,
                fix_title="Parse failure",
            )

    changes = []
    for change_data in data.get("changes", []):
        changes.append(FileChange(
            file_path=change_data.get("file_path", ""),
            action=change_data.get("action", "modify"),
            content=change_data.get("content", ""),
            diff=change_data.get("diff_description", ""),
            explanation=change_data.get("explanation", ""),
        ))

    return FixProposal(
        changes=changes,
        explanation=data.get("explanation", ""),
        root_cause=data.get("root_cause", ""),
        confidence=int(data.get("confidence", 50)),
        fix_title=data.get("fix_title", "LLM generated fix"),
    )


# ---------------------------------------------------------------------------
# Context builder
# ---------------------------------------------------------------------------

def build_fix_context(
    error_context: str,
    file_contents: dict[str, str] | None = None,
) -> str:
    """
    Build the full context for the LLM fix generation.

    Args:
        error_context: Output from log_parser (already trimmed)
        file_contents: Dict of file_path → content for relevant source files

    Returns:
        Complete context string for the LLM
    """
    parts = [error_context]

    if file_contents:
        parts.append("\n=== RELEVANT SOURCE FILES ===")
        for path, content in file_contents.items():
            # Truncate very large files — send only first 200 lines
            lines = content.splitlines()
            if len(lines) > 200:
                truncated = "\n".join(lines[:200])
                parts.append(f"\n--- {path} (first 200 of {len(lines)} lines) ---")
                parts.append(truncated)
            else:
                parts.append(f"\n--- {path} ---")
                parts.append(content)

    return "\n".join(parts)
