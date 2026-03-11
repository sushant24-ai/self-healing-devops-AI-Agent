"""
memory.py — aifix.md Memory System

The core differentiator: before calling any LLM, the agent checks aifix.md
for known error→fix mappings. If found, the fix is applied instantly (free, fast).
New successful fixes are appended to aifix.md for future use.
"""

import re
import os
import json
from dataclasses import dataclass, field, asdict
from typing import Optional
from datetime import datetime, timezone


AIFIX_FILENAME = "aifix.md"

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class FixEntry:
    """One fix entry stored in aifix.md."""
    title: str                          # Short descriptive title
    error_signature: str                # The error string to match against
    root_cause: str                     # Human-readable explanation
    classification: str                 # "code" | "config" | "infra" | "transient"
    fix_description: str                # What the fix does
    files_changed: list[str]            # List of file paths modified
    diff: str                           # The actual diff (patch)
    date: str = ""                      # ISO date when fix was first applied
    confidence: int = 90                # 0-100 confidence score
    times_applied: int = 1             # How many times this fix has been reused
    last_applied: str = ""              # ISO date of last application
    workflow_name: str = ""             # Which workflow this fix was for


# ---------------------------------------------------------------------------
# Parsing aifix.md → list[FixEntry]
# ---------------------------------------------------------------------------

def parse_aifix(content: str) -> list[FixEntry]:
    """Parse an aifix.md file into structured FixEntry objects."""
    entries: list[FixEntry] = []
    if not content or not content.strip():
        return entries

    # Split on the entry separator (## Fix: ...)
    raw_blocks = re.split(r"(?=^## Fix: )", content, flags=re.MULTILINE)

    for block in raw_blocks:
        block = block.strip()
        if not block.startswith("## Fix:"):
            continue

        entry = _parse_single_block(block)
        if entry:
            entries.append(entry)

    return entries


def _parse_single_block(block: str) -> Optional[FixEntry]:
    """Parse a single ## Fix: block into a FixEntry."""
    # Title
    title_match = re.search(r"^## Fix: (.+)$", block, re.MULTILINE)
    title = title_match.group(1).strip() if title_match else "Unknown"

    # Field extraction helpers
    def extract_field(label: str) -> str:
        pattern = rf"- \*\*{label}:\*\*\s*`?(.+?)`?\s*$"
        m = re.search(pattern, block, re.MULTILINE)
        return m.group(1).strip().strip("`") if m else ""

    error_signature = extract_field("Error Signature")
    root_cause = extract_field("Root Cause")
    classification = extract_field("Classification")
    fix_description = extract_field("Fix Applied")
    workflow_name = extract_field("Workflow")
    date = extract_field("Date")
    last_applied = extract_field("Last Applied")

    # Confidence (integer)
    conf_match = re.search(r"- \*\*Confidence:\*\*\s*(\d+)%?", block)
    confidence = int(conf_match.group(1)) if conf_match else 90

    # Times applied
    times_match = re.search(r"- \*\*Times Applied:\*\*\s*(\d+)", block)
    times_applied = int(times_match.group(1)) if times_match else 1

    # Files changed (can be comma-separated or multi-value)
    files_match = re.search(r"- \*\*Files Changed:\*\*\s*(.+?)$", block, re.MULTILINE)
    files_changed = []
    if files_match:
        raw = files_match.group(1).strip()
        files_changed = [f.strip().strip("`") for f in raw.split(",")]

    # Diff block
    diff = ""
    diff_match = re.search(r"```diff\n(.*?)```", block, re.DOTALL)
    if diff_match:
        diff = diff_match.group(1).strip()

    if not error_signature:
        return None

    return FixEntry(
        title=title,
        error_signature=error_signature,
        root_cause=root_cause,
        classification=classification,
        fix_description=fix_description,
        files_changed=files_changed,
        diff=diff,
        date=date,
        confidence=confidence,
        times_applied=times_applied,
        last_applied=last_applied,
        workflow_name=workflow_name,
    )


# ---------------------------------------------------------------------------
# Searching for known fixes
# ---------------------------------------------------------------------------

def find_matching_fix(entries: list[FixEntry], error_text: str) -> Optional[FixEntry]:
    """
    Find the best matching fix for a given error text.

    Strategy (ordered by precision):
    1. Exact substring match of error_signature in error_text
    2. Token-overlap scoring (fuzzy match)
    3. Return None if no match above threshold
    """
    if not entries or not error_text:
        return None

    error_lower = error_text.lower()
    best_match: Optional[FixEntry] = None
    best_score: float = 0.0

    for entry in entries:
        sig_lower = entry.error_signature.lower()

        # --- Exact substring match → highest confidence ---
        if sig_lower in error_lower:
            score = 1.0
        else:
            # --- Token overlap (fuzzy) ---
            sig_tokens = set(_tokenize(sig_lower))
            err_tokens = set(_tokenize(error_lower))
            if not sig_tokens:
                continue
            overlap = sig_tokens & err_tokens
            score = len(overlap) / len(sig_tokens)

        if score > best_score:
            best_score = score
            best_match = entry

    # Threshold: require at least 70% token overlap for fuzzy matches
    if best_score >= 0.70 and best_match is not None:
        return best_match

    return None


def _tokenize(text: str) -> list[str]:
    """Split text into meaningful tokens (words, identifiers)."""
    return re.findall(r"[a-z0-9_]+", text.lower())


# ---------------------------------------------------------------------------
# Writing new fixes to aifix.md
# ---------------------------------------------------------------------------

def format_fix_entry(entry: FixEntry) -> str:
    """Format a single FixEntry as a markdown block for aifix.md."""
    files_str = ", ".join(f"`{f}`" for f in entry.files_changed) if entry.files_changed else "N/A"
    date_str = entry.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    last_str = entry.last_applied or date_str

    block = f"""## Fix: {entry.title}
- **Error Signature:** `{entry.error_signature}`
- **Root Cause:** {entry.root_cause}
- **Classification:** {entry.classification}
- **Fix Applied:** {entry.fix_description}
- **Files Changed:** {files_str}
- **Workflow:** {entry.workflow_name}
- **Date:** {date_str}
- **Last Applied:** {last_str}
- **Confidence:** {entry.confidence}%
- **Times Applied:** {entry.times_applied}

### Diff
```diff
{entry.diff}
```

---
"""
    return block


def append_fix_to_content(existing_content: str, entry: FixEntry) -> str:
    """Append a new fix entry to existing aifix.md content."""
    if not existing_content or not existing_content.strip():
        existing_content = "# AI Fix Memory\n\n> This file is auto-managed by the DevOps Agent. It stores known error→fix mappings.\n> The agent checks this file FIRST before calling any LLM.\n\n---\n\n"

    new_block = format_fix_entry(entry)
    return existing_content.rstrip() + "\n\n" + new_block


def update_existing_entry(content: str, entry: FixEntry) -> str:
    """
    If a fix already exists in aifix.md, bump its times_applied and last_applied.
    Returns updated content string.
    """
    entries = parse_aifix(content)
    updated = False
    for existing in entries:
        if existing.error_signature.lower() == entry.error_signature.lower():
            existing.times_applied += 1
            existing.last_applied = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            # Update confidence based on repeated success
            existing.confidence = min(99, existing.confidence + 1)
            updated = True
            break

    if not updated:
        return append_fix_to_content(content, entry)

    # Rebuild the full content
    return _rebuild_content(entries)


def _rebuild_content(entries: list[FixEntry]) -> str:
    """Rebuild the entire aifix.md content from a list of entries."""
    header = "# AI Fix Memory\n\n> This file is auto-managed by the DevOps Agent. It stores known error→fix mappings.\n> The agent checks this file FIRST before calling any LLM.\n\n---\n\n"
    body = "\n".join(format_fix_entry(e) for e in entries)
    return header + body
