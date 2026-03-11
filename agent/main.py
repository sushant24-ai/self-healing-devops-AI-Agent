"""
main.py — DevOps Agent Orchestrator

The main entry point and state machine that coordinates the entire self-healing pipeline:

1. Receive failed workflow run ID
2. Extract & parse logs (smart extraction, not full 10k lines)
3. Check aifix.md for known fixes (FREE — no LLM call)
4. If known fix → apply directly
5. If unknown → classify → fix (LLM) or notify (Teams)
6. If fix succeeds → write to aifix.md for future use
7. Max 3 fix attempts per failure (Reflexion loop)
"""

import os
import sys
import time
import argparse
import traceback
from datetime import datetime, timezone

from agent.log_parser import parse_workflow_logs, extract_logs_from_zip
from agent.memory import (
    parse_aifix,
    find_matching_fix,
    format_fix_entry,
    append_fix_to_content,
    update_existing_entry,
    FixEntry,
)
from agent.classifier import (
    classify_by_pattern,
    build_classification_prompt,
    parse_classification_response,
    FailureType,
)
from agent.fixer import LLMClient, generate_fix, build_fix_context
from agent.github_ops import GitHubClient, generate_fix_branch_name
from agent.notifier import TeamsNotifier, NotificationPayload


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MAX_FIX_ATTEMPTS = 3
WAIT_FOR_CI_SECONDS = 300   # 5 minutes max wait for CI to complete
CI_POLL_INTERVAL = 30       # Check every 30 seconds


# ---------------------------------------------------------------------------
# Main Orchestrator
# ---------------------------------------------------------------------------

class DevOpsAgent:
    """The main self-healing agent."""

    def __init__(
        self,
        github_token: str | None = None,
        openai_api_key: str | None = None,
        teams_webhook_url: str | None = None,
        llm_model: str = "gpt-4o",
        dry_run: bool = False,
    ):
        self.gh = GitHubClient(github_token)
        self.llm = LLMClient(openai_api_key, model=llm_model)
        self.notifier = TeamsNotifier(teams_webhook_url)
        self.dry_run = dry_run

    def handle_failure(self, repo_full_name: str, run_id: int) -> None:
        """
        Main entry point: handle a failed workflow run.
        This is the full orchestration loop.
        """
        print(f"\n{'='*60}")
        print(f"🤖 DevOps Agent — Handling failure")
        print(f"   Repo: {repo_full_name}")
        print(f"   Run ID: {run_id}")
        print(f"   Dry Run: {self.dry_run}")
        print(f"{'='*60}\n")

        try:
            # Step 1: Get workflow run details
            print("[1/7] Fetching workflow run details...")
            run_info = self.gh.get_failed_run(repo_full_name, run_id)
            print(f"  Workflow: {run_info.workflow_name}")
            print(f"  Branch: {run_info.branch}")
            print(f"  Commit: {run_info.commit_sha[:8]}")
            print(f"  Event: {run_info.event}")

            # Step 2: Download and parse logs
            print("\n[2/7] Downloading workflow logs...")
            try:
                log_bytes = self.gh.download_logs_via_url(run_info.logs_url)
            except Exception:
                log_bytes = self.gh.download_logs(repo_full_name, run_id)

            job_logs = extract_logs_from_zip(log_bytes)
            print(f"  Found {len(job_logs)} job log(s)")

            # Combine all job logs for parsing
            combined_log = "\n".join(job_logs.values())

            # Get git diff for context
            git_diff = ""
            try:
                git_diff = self.gh.get_commit_diff(repo_full_name, run_info.commit_sha)
            except Exception as e:
                print(f"  Warning: Could not get commit diff: {e}")

            # Get workflow YAML for context
            workflow_yaml = ""
            try:
                workflow_yaml = self.gh.get_workflow_yaml(
                    repo_full_name, run_info.workflow_name, ref=run_info.branch
                ) or ""
            except Exception:
                pass

            # Smart log parsing
            print("\n[3/7] Parsing logs (smart extraction)...")
            parsed = parse_workflow_logs(combined_log, git_diff, workflow_yaml)
            print(f"  Raw log: {parsed.raw_length} lines")
            print(f"  Extracted: {parsed.extracted_length} lines")
            print(f"  Failing step: {parsed.failing_step}")
            print(f"  Error signature: {parsed.error_signature[:100]}")
            print(f"  Detected language: {parsed.detected_language}")
            print(f"  Is transient: {parsed.is_transient}")
            print(f"  Is secret issue: {parsed.is_secret_issue}")

            # Step 3: Check aifix.md FIRST (no LLM needed!)
            print("\n[4/7] Checking aifix.md for known fixes...")
            aifix_content = self.gh.get_aifix_content(repo_full_name, ref=run_info.branch)
            known_entries = parse_aifix(aifix_content)
            print(f"  Known fixes in aifix.md: {len(known_entries)}")

            known_fix = find_matching_fix(known_entries, parsed.error_signature)

            if known_fix:
                print(f"  ✅ MATCH FOUND: '{known_fix.title}' (applied {known_fix.times_applied}x before)")
                self._apply_known_fix(
                    repo_full_name, run_info, known_fix, aifix_content, parsed
                )
                return

            print("  No known fix found — proceeding to classification")

            # Step 4: Handle transient errors (just retry)
            if parsed.is_transient:
                print("\n[5/7] Transient error detected — retrying workflow...")
                if not self.dry_run:
                    self.gh.rerun_workflow(repo_full_name, run_id)
                print("  ✅ Workflow re-run triggered")
                return

            # Step 5: Classify the failure
            print("\n[5/7] Classifying failure...")
            classification = classify_by_pattern(parsed.error_section)

            if classification is None:
                # Tier 2: LLM classification
                print("  Pattern match: no match → using LLM...")
                prompt = build_classification_prompt(parsed.error_section)
                llm_response = self.llm.generate(
                    "You are a CI/CD failure classifier.",
                    prompt,
                )
                classification = parse_classification_response(llm_response)

            print(f"  Type: {classification.failure_type.value}")
            print(f"  Confidence: {classification.confidence}%")
            print(f"  Explanation: {classification.explanation}")
            print(f"  Tier: {classification.tier}")

            # Step 6: Route based on classification
            if classification.failure_type == FailureType.CODE:
                print("\n[6/7] Code issue detected — attempting auto-fix...")
                self._attempt_fix(
                    repo_full_name, run_info, parsed, aifix_content, classification
                )
            elif classification.failure_type == FailureType.TRANSIENT:
                print("\n[6/7] Classified as transient — retrying...")
                if not self.dry_run:
                    self.gh.rerun_workflow(repo_full_name, run_id)
            else:
                # Config, Infra, Secret, Unknown → notify team
                print(f"\n[6/7] Non-code issue ({classification.failure_type.value}) — notifying team...")
                self._notify_team(run_info, parsed, classification)

        except Exception as e:
            print(f"\n❌ Agent error: {e}")
            traceback.print_exc()
            # Try to notify about agent failure
            try:
                self.notifier.send(NotificationPayload(
                    repo_name=repo_full_name,
                    workflow_name="DevOps Agent",
                    branch="N/A",
                    error_summary=f"Agent itself failed: {str(e)[:200]}",
                    classification="unknown",
                    suggested_action="Check agent logs and configuration",
                    confidence=0,
                    run_url=f"https://github.com/{repo_full_name}/actions/runs/{run_id}",
                ))
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Apply a known fix from aifix.md
    # ------------------------------------------------------------------

    def _apply_known_fix(
        self,
        repo_full_name: str,
        run_info,
        fix_entry: FixEntry,
        aifix_content: str,
        parsed,
    ):
        """Apply a fix already known in aifix.md — NO LLM call needed."""
        print(f"\n  📦 Applying known fix: {fix_entry.title}")
        print(f"     Previously applied {fix_entry.times_applied} time(s)")
        print(f"     Confidence: {fix_entry.confidence}%")

        if self.dry_run:
            print("  [DRY RUN] Would apply fix and create PR")
            return

        # Parse the diff to determine file changes
        file_changes = self._parse_diff_to_changes(
            repo_full_name, fix_entry.diff, fix_entry.files_changed, run_info.branch
        )

        if not file_changes:
            print("  ⚠️ Could not parse diff into file changes — skipping")
            return

        # Create branch and apply fix
        branch_name = generate_fix_branch_name(run_info.workflow_name, run_info.run_id)
        self.gh.create_fix_branch(repo_full_name, run_info.commit_sha, branch_name)
        self.gh.commit_fix(
            repo_full_name,
            branch_name,
            file_changes,
            f"🤖 aifix: {fix_entry.title}\n\nKnown fix applied from aifix.md (applied {fix_entry.times_applied + 1}x)",
        )

        # Update aifix.md with incremented count
        updated_aifix = update_existing_entry(aifix_content, fix_entry)
        self.gh.update_aifix_md(repo_full_name, branch_name, updated_aifix)

        # Create PR
        pr_url = self.gh.create_pull_request(
            repo_full_name,
            branch_name,
            run_info.branch,
            f"🤖 AI Fix: {fix_entry.title}",
            self._build_pr_body(fix_entry, is_known=True),
        )
        print(f"\n  ✅ PR created: {pr_url}")

    # ------------------------------------------------------------------
    # Attempt LLM-powered fix (Reflexion loop)
    # ------------------------------------------------------------------

    def _attempt_fix(
        self,
        repo_full_name: str,
        run_info,
        parsed,
        aifix_content: str,
        classification,
    ):
        """Attempt to fix a code issue using LLM with Reflexion loop."""
        previous_attempts: list[dict] = []

        for attempt in range(1, MAX_FIX_ATTEMPTS + 1):
            print(f"\n  --- Fix Attempt {attempt}/{MAX_FIX_ATTEMPTS} ---")

            # Get relevant source files for context
            file_contents = self._get_relevant_files(
                repo_full_name, parsed, run_info.branch
            )

            # Build context for LLM
            context = build_fix_context(parsed.full_context, file_contents)

            # Generate fix
            print("  Generating fix via LLM...")
            fix_proposal = generate_fix(self.llm, context, previous_attempts or None)

            if not fix_proposal.changes:
                print("  ⚠️ LLM returned no changes")
                previous_attempts.append({
                    "explanation": "LLM returned empty response",
                    "result": "No changes generated",
                })
                continue

            print(f"  Fix: {fix_proposal.fix_title}")
            print(f"  Root cause: {fix_proposal.root_cause}")
            print(f"  Confidence: {fix_proposal.confidence}%")
            print(f"  Files to change: {[c.file_path for c in fix_proposal.changes]}")

            if self.dry_run:
                print(f"  [DRY RUN] Would apply {len(fix_proposal.changes)} file change(s)")
                print(f"  [DRY RUN] Fix explanation: {fix_proposal.explanation}")
                return

            # Apply fix: create branch, commit, push
            branch_name = generate_fix_branch_name(
                run_info.workflow_name, run_info.run_id
            )
            if attempt == 1:
                self.gh.create_fix_branch(
                    repo_full_name, run_info.commit_sha, branch_name
                )

            file_changes = {
                change.file_path: change.content
                for change in fix_proposal.changes
                if change.content
            }

            self.gh.commit_fix(
                repo_full_name,
                branch_name,
                file_changes,
                f"🤖 aifix (attempt {attempt}): {fix_proposal.fix_title}\n\n{fix_proposal.explanation}",
            )

            print(f"  Committed to branch: {branch_name}")

            # Wait for CI to run on the new branch
            print(f"  Waiting for CI on branch {branch_name}...")
            ci_passed = self._wait_for_ci(
                repo_full_name, branch_name, run_info.workflow_name
            )

            if ci_passed:
                print(f"\n  ✅ Fix successful on attempt {attempt}!")

                # Write to aifix.md
                diff_text = "\n".join(
                    f"--- {c.file_path}\n{c.diff}" for c in fix_proposal.changes
                )
                new_entry = FixEntry(
                    title=fix_proposal.fix_title,
                    error_signature=parsed.error_signature,
                    root_cause=fix_proposal.root_cause,
                    classification="code",
                    fix_description=fix_proposal.explanation,
                    files_changed=[c.file_path for c in fix_proposal.changes],
                    diff=diff_text,
                    date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                    confidence=fix_proposal.confidence,
                    times_applied=1,
                    workflow_name=run_info.workflow_name,
                )

                updated_aifix = append_fix_to_content(aifix_content, new_entry)
                self.gh.update_aifix_md(repo_full_name, branch_name, updated_aifix)

                # Create PR
                pr_url = self.gh.create_pull_request(
                    repo_full_name,
                    branch_name,
                    run_info.branch,
                    f"🤖 AI Fix: {fix_proposal.fix_title}",
                    self._build_pr_body_from_proposal(fix_proposal, attempt),
                )
                print(f"  PR created: {pr_url}")

                # Notify success
                self.notifier.send(NotificationPayload(
                    repo_name=repo_full_name,
                    workflow_name=run_info.workflow_name,
                    branch=run_info.branch,
                    error_summary=parsed.error_signature,
                    classification="fixed",
                    suggested_action=f"Review and merge PR: {pr_url}",
                    confidence=fix_proposal.confidence,
                    run_url=run_info.html_url,
                    fix_attempted=True,
                    fix_result=f"Auto-fixed on attempt {attempt}. PR: {pr_url}",
                ))
                return
            else:
                print(f"  ❌ Fix attempt {attempt} failed — CI still failing")
                previous_attempts.append({
                    "explanation": fix_proposal.explanation,
                    "result": "CI still failing after applying fix",
                    "changes": [c.file_path for c in fix_proposal.changes],
                })

        # All attempts exhausted
        print(f"\n  ❌ All {MAX_FIX_ATTEMPTS} fix attempts failed — escalating to team")
        self._notify_team(
            run_info,
            parsed,
            classification,
            extra_details=f"Agent attempted {MAX_FIX_ATTEMPTS} fixes but CI still fails.",
        )

    # ------------------------------------------------------------------
    # Helper methods
    # ------------------------------------------------------------------

    def _get_relevant_files(
        self, repo_full_name: str, parsed, ref: str
    ) -> dict[str, str]:
        """Extract filenames from error logs and fetch their contents."""
        file_contents: dict[str, str] = {}

        # Extract file paths from the error section
        # Common patterns: "File "path/to/file.py", line 42"
        #                   "at Object.<anonymous> (path/to/file.js:42:10)"
        #                   "src/main.py:42: error"
        patterns = [
            r'File "([^"]+)", line \d+',          # Python
            r'at\s+.*?\(([^:)]+):\d+:\d+\)',       # JavaScript
            r'(\S+\.\w{1,4}):\d+:\s',              # Generic file:line
            r'in\s+(\S+\.\w{1,4})\s+',             # "in file.py"
        ]

        mentioned_files = set()
        for pattern in patterns:
            matches = re.findall(pattern, parsed.error_section)
            for m in matches:
                # Filter out stdlib / node_modules paths
                if not any(skip in m for skip in [
                    "node_modules", "site-packages", "/usr/lib",
                    "\\Python", "/Python", "<frozen",
                ]):
                    mentioned_files.add(m)

        # Fetch each file
        for file_path in list(mentioned_files)[:5]:  # Max 5 files
            try:
                content = self.gh.get_file_content(repo_full_name, file_path, ref=ref)
                if content:
                    file_contents[file_path] = content
            except Exception:
                pass

        return file_contents

    def _wait_for_ci(
        self, repo_full_name: str, branch: str, workflow_name: str
    ) -> bool:
        """Wait for CI to complete on a branch and return True if passed."""
        waited = 0
        while waited < WAIT_FOR_CI_SECONDS:
            time.sleep(CI_POLL_INTERVAL)
            waited += CI_POLL_INTERVAL

            status = self.gh.get_latest_run_status(
                repo_full_name, branch, workflow_name
            )
            if status == "completed":
                conclusion = self.gh.get_latest_run_conclusion(
                    repo_full_name, branch, workflow_name
                )
                return conclusion == "success"

            print(f"    CI status: {status} (waited {waited}s)")

        print(f"    CI timed out after {WAIT_FOR_CI_SECONDS}s")
        return False

    def _parse_diff_to_changes(
        self,
        repo_full_name: str,
        diff_text: str,
        files_changed: list[str],
        ref: str,
    ) -> dict[str, str]:
        """
        Parse a diff from aifix.md and apply it to get new file contents.
        Simplified: for MVP, we re-apply by reading current file and applying changes.
        """
        # For known fixes with simple diffs, we ask the LLM to apply the diff
        # to the current file content. This handles drift.
        file_contents = {}
        for file_path in files_changed:
            current = self.gh.get_file_content(repo_full_name, file_path, ref=ref)
            if current is not None:
                # Use LLM to apply the diff to current content
                prompt = f"""Apply this diff to the current file content.
Return ONLY the complete new file content, nothing else.

CURRENT FILE ({file_path}):
{current}

DIFF TO APPLY:
{diff_text}
"""
                try:
                    result = self.llm.generate(
                        "You apply diffs to files. Return only the new file content.",
                        prompt,
                    )
                    # Strip markdown code blocks if present
                    import re
                    code_match = re.search(r"```\w*\n(.*?)```", result, re.DOTALL)
                    if code_match:
                        result = code_match.group(1)
                    file_contents[file_path] = result
                except Exception as e:
                    print(f"  Warning: Could not apply diff to {file_path}: {e}")

        return file_contents

    def _notify_team(self, run_info, parsed, classification, extra_details: str = ""):
        """Send notification to DevOps team."""
        self.notifier.send(NotificationPayload(
            repo_name=run_info.repo_full_name,
            workflow_name=run_info.workflow_name,
            branch=run_info.branch,
            error_summary=parsed.error_signature,
            classification=classification.failure_type.value,
            suggested_action=classification.suggested_action,
            confidence=classification.confidence,
            run_url=run_info.html_url,
            details=extra_details,
        ))

    def _build_pr_body(self, fix_entry: FixEntry, is_known: bool = False) -> str:
        """Build PR description body."""
        source = "from **aifix.md** (known fix)" if is_known else "via **LLM analysis**"
        return f"""## 🤖 Auto-generated fix {source}

### Error
```
{fix_entry.error_signature}
```

### Root Cause
{fix_entry.root_cause}

### Fix Applied
{fix_entry.fix_description}

### Files Changed
{', '.join(f'`{f}`' for f in fix_entry.files_changed)}

### Confidence
{fix_entry.confidence}%

{"### Note" if is_known else ""}
{"This fix has been successfully applied " + str(fix_entry.times_applied) + " time(s) before." if is_known else ""}

---
*Generated by [DevOps Agent](https://github.com) — self-healing CI/CD pipeline*
"""

    def _build_pr_body_from_proposal(self, proposal, attempt: int) -> str:
        """Build PR description from a fix proposal."""
        files_str = ", ".join(f"`{c.file_path}`" for c in proposal.changes)
        changes_detail = "\n".join(
            f"- **{c.file_path}**: {c.explanation}" for c in proposal.changes
        )
        return f"""## 🤖 Auto-generated fix via LLM analysis

### Error Root Cause
{proposal.root_cause}

### Fix (attempt {attempt}/{MAX_FIX_ATTEMPTS})
{proposal.explanation}

### Changes
{changes_detail}

### Files Modified
{files_str}

### Confidence
{proposal.confidence}%

---
*Generated by [DevOps Agent](https://github.com) — self-healing CI/CD pipeline*
*This fix has been verified: CI passes on the fix branch ✅*
"""


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="DevOps Agent — Self-Healing CI/CD")
    parser.add_argument("--repo", required=True, help="Repository (owner/repo)")
    parser.add_argument("--run-id", required=True, type=int, help="Workflow run ID")
    parser.add_argument("--dry-run", action="store_true", help="Don't push changes")
    parser.add_argument("--model", default="gpt-4o", help="LLM model to use")
    args = parser.parse_args()

    agent = DevOpsAgent(
        github_token=os.environ.get("GITHUB_TOKEN"),
        openai_api_key=os.environ.get("OPENAI_API_KEY"),
        teams_webhook_url=os.environ.get("TEAMS_WEBHOOK_URL"),
        llm_model=args.model,
        dry_run=args.dry_run,
    )

    agent.handle_failure(args.repo, args.run_id)


if __name__ == "__main__":
    main()
