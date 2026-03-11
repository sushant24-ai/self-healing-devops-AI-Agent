"""
test_memory.py — Tests for aifix.md Memory System

Tests parsing, searching (exact + fuzzy), and writing of aifix.md entries.
"""

import pytest
from agent.memory import (
    parse_aifix,
    find_matching_fix,
    format_fix_entry,
    append_fix_to_content,
    update_existing_entry,
    FixEntry,
)


# ---------------------------------------------------------------------------
# Sample aifix.md content for tests
# ---------------------------------------------------------------------------

SAMPLE_AIFIX = """# AI Fix Memory

> This file is auto-managed by the DevOps Agent. It stores known error→fix mappings.
> The agent checks this file FIRST before calling any LLM.

---

## Fix: Missing flask dependency
- **Error Signature:** `ModuleNotFoundError: No module named 'flask'`
- **Root Cause:** Missing dependency in requirements.txt
- **Classification:** code
- **Fix Applied:** Added flask==3.0.0 to requirements.txt
- **Files Changed:** `requirements.txt`
- **Workflow:** CI Tests
- **Date:** 2026-03-10
- **Last Applied:** 2026-03-10
- **Confidence:** 95%
- **Times Applied:** 3

### Diff
```diff
--- a/requirements.txt
+++ b/requirements.txt
@@ -1,3 +1,4 @@
 requests==2.31.0
+flask==3.0.0
 pytest==7.4.0
```

---

## Fix: TypeScript strict null check
- **Error Signature:** `error TS2531: Object is possibly 'null'`
- **Root Cause:** Missing null check on optional parameter
- **Classification:** code
- **Fix Applied:** Added null guard before accessing user.name
- **Files Changed:** `src/utils.ts`
- **Workflow:** Build
- **Date:** 2026-03-09
- **Last Applied:** 2026-03-09
- **Confidence:** 88%
- **Times Applied:** 1

### Diff
```diff
--- a/src/utils.ts
+++ b/src/utils.ts
@@ -10,3 +10,4 @@
-  return user.name;
+  return user?.name ?? 'Unknown';
```

---
"""


# ---------------------------------------------------------------------------
# Tests: Parsing
# ---------------------------------------------------------------------------

class TestParsing:
    def test_parse_valid_content(self):
        entries = parse_aifix(SAMPLE_AIFIX)
        assert len(entries) == 2

    def test_parse_first_entry_fields(self):
        entries = parse_aifix(SAMPLE_AIFIX)
        entry = entries[0]
        assert entry.title == "Missing flask dependency"
        assert "ModuleNotFoundError" in entry.error_signature
        assert entry.classification == "code"
        assert entry.confidence == 95
        assert entry.times_applied == 3
        assert "requirements.txt" in entry.files_changed

    def test_parse_second_entry(self):
        entries = parse_aifix(SAMPLE_AIFIX)
        entry = entries[1]
        assert entry.title == "TypeScript strict null check"
        assert "TS2531" in entry.error_signature
        assert entry.times_applied == 1

    def test_parse_diff(self):
        entries = parse_aifix(SAMPLE_AIFIX)
        assert "flask==3.0.0" in entries[0].diff
        assert "user?.name" in entries[1].diff

    def test_parse_empty_content(self):
        assert parse_aifix("") == []
        assert parse_aifix("   ") == []

    def test_parse_header_only(self):
        content = "# AI Fix Memory\n\nNo fixes yet.\n"
        assert parse_aifix(content) == []


# ---------------------------------------------------------------------------
# Tests: Searching
# ---------------------------------------------------------------------------

class TestSearching:
    def test_exact_match(self):
        entries = parse_aifix(SAMPLE_AIFIX)
        result = find_matching_fix(
            entries,
            "ModuleNotFoundError: No module named 'flask'"
        )
        assert result is not None
        assert result.title == "Missing flask dependency"

    def test_substring_match(self):
        entries = parse_aifix(SAMPLE_AIFIX)
        # The error signature appears as a substring in a longer log
        result = find_matching_fix(
            entries,
            "Step 5: Run tests\nERROR: ModuleNotFoundError: No module named 'flask'\nProcess exited with code 1"
        )
        assert result is not None
        assert "flask" in result.error_signature

    def test_fuzzy_match(self):
        entries = parse_aifix(SAMPLE_AIFIX)
        # Similar but not identical error
        result = find_matching_fix(
            entries,
            "error TS2531: Object is possibly 'null' in component.ts line 42"
        )
        assert result is not None
        assert "TS2531" in result.error_signature

    def test_no_match(self):
        entries = parse_aifix(SAMPLE_AIFIX)
        result = find_matching_fix(
            entries,
            "ECONNRESET: Connection reset by peer"
        )
        assert result is None

    def test_empty_entries(self):
        result = find_matching_fix([], "some error")
        assert result is None

    def test_empty_error(self):
        entries = parse_aifix(SAMPLE_AIFIX)
        result = find_matching_fix(entries, "")
        assert result is None


# ---------------------------------------------------------------------------
# Tests: Writing
# ---------------------------------------------------------------------------

class TestWriting:
    def test_format_entry(self):
        entry = FixEntry(
            title="Test fix",
            error_signature="TestError: something broke",
            root_cause="Bad code",
            classification="code",
            fix_description="Fixed the bad code",
            files_changed=["src/app.py"],
            diff="-bad\n+good",
            date="2026-03-10",
            confidence=90,
            times_applied=1,
            workflow_name="CI",
        )
        formatted = format_fix_entry(entry)
        assert "## Fix: Test fix" in formatted
        assert "TestError: something broke" in formatted
        assert "Bad code" in formatted
        assert "`src/app.py`" in formatted
        assert "```diff" in formatted

    def test_append_to_empty(self):
        entry = FixEntry(
            title="First fix",
            error_signature="Error1",
            root_cause="Cause1",
            classification="code",
            fix_description="Fix1",
            files_changed=["f.py"],
            diff="-a\n+b",
        )
        result = append_fix_to_content("", entry)
        assert "# AI Fix Memory" in result
        assert "## Fix: First fix" in result

    def test_append_to_existing(self):
        entry = FixEntry(
            title="New fix",
            error_signature="NewError",
            root_cause="NewCause",
            classification="code",
            fix_description="NewFix",
            files_changed=["new.py"],
            diff="-old\n+new",
        )
        result = append_fix_to_content(SAMPLE_AIFIX, entry)
        # Should still have original entries
        assert "Missing flask dependency" in result
        # Should have new entry
        assert "## Fix: New fix" in result

    def test_update_existing_entry_bumps_count(self):
        entry = FixEntry(
            title="Missing flask dependency",
            error_signature="ModuleNotFoundError: No module named 'flask'",
            root_cause="Missing dep",
            classification="code",
            fix_description="Added flask",
            files_changed=["requirements.txt"],
            diff="+flask",
        )
        result = update_existing_entry(SAMPLE_AIFIX, entry)
        # Should contain updated count
        entries = parse_aifix(result)
        flask_entry = None
        for e in entries:
            if "flask" in e.error_signature:
                flask_entry = e
                break
        assert flask_entry is not None
        assert flask_entry.times_applied == 4  # Was 3, now 4


# ---------------------------------------------------------------------------
# Tests: Log Parser (basic)
# ---------------------------------------------------------------------------

class TestLogParser:
    def test_parse_python_error(self):
        from agent.log_parser import parse_workflow_logs

        log = """
##[group]Run pytest
Collecting tests...
FAILED tests/test_app.py::test_login - AssertionError: expected 200 but got 401
##[error]Process completed with exit code 1.
##[endgroup]
"""
        result = parse_workflow_logs(log)
        assert result.raw_length > 0
        assert result.extracted_length < result.raw_length or result.raw_length < 50
        assert "pytest" in result.failing_step or "test" in result.error_signature.lower()

    def test_parse_node_error(self):
        from agent.log_parser import parse_workflow_logs

        log = """
##[group]Run npm test
npm ERR! code ERESOLVE
npm ERR! ERESOLVE unable to resolve dependency tree
npm ERR! Found: react@18.2.0
##[error]Process completed with exit code 1.
##[endgroup]
"""
        result = parse_workflow_logs(log)
        assert "node" in result.detected_language or "npm" in result.error_signature.lower()

    def test_transient_detection(self):
        from agent.log_parser import parse_workflow_logs

        log = """
##[group]Run npm install
npm ERR! code ETIMEDOUT
npm ERR! network request to https://registry.npmjs.org failed
##[error]Process completed with exit code 1.
"""
        result = parse_workflow_logs(log)
        assert result.is_transient is True

    def test_secret_detection(self):
        from agent.log_parser import parse_workflow_logs

        log = """
##[group]Run deploy
Error: authentication failed for deployment
403 Forbidden: credentials expired
##[error]Process completed with exit code 1.
"""
        result = parse_workflow_logs(log)
        assert result.is_secret_issue is True

    def test_secret_scrubbing(self):
        from agent.log_parser import parse_workflow_logs

        log = """
##[group]Run script
Using API key: sk-abc123def456ghi789jkl012mno345pqr678stu901vwxyz12
##[error]Process completed with exit code 1.
"""
        result = parse_workflow_logs(log)
        assert "sk-abc" not in result.error_section
        assert "REDACTED" in result.error_section


# ---------------------------------------------------------------------------
# Tests: Classifier
# ---------------------------------------------------------------------------

class TestClassifier:
    def test_classify_transient(self):
        from agent.classifier import classify_by_pattern, FailureType

        result = classify_by_pattern("npm ERR! code ETIMEDOUT connecting to registry")
        assert result is not None
        assert result.failure_type == FailureType.TRANSIENT

    def test_classify_code_error(self):
        from agent.classifier import classify_by_pattern, FailureType

        result = classify_by_pattern("ModuleNotFoundError: No module named 'flask'")
        assert result is not None
        assert result.failure_type == FailureType.CODE

    def test_classify_secret(self):
        from agent.classifier import classify_by_pattern, FailureType

        result = classify_by_pattern("Error: authentication failed - credentials expired")
        assert result is not None
        assert result.failure_type == FailureType.SECRET

    def test_classify_unknown(self):
        from agent.classifier import classify_by_pattern

        result = classify_by_pattern("something completely novel happened")
        assert result is None  # Should return None for LLM classification
