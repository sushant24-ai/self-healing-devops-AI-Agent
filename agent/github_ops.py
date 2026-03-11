"""
github_ops.py — GitHub API Operations

Handles all interactions with GitHub:
  - Download workflow run logs
  - Read file contents from repo
  - Create branches, commit, push
  - Create pull requests
  - Trigger workflow re-runs
  - Read/write aifix.md from repo
"""

import os
import re
import base64
from dataclasses import dataclass

try:
    from github import Github, GithubException, InputGitTreeElement
    from github.Repository import Repository
    from github.WorkflowRun import WorkflowRun
except ImportError:
    Github = None  # type: ignore


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class WorkflowFailure:
    """Information about a failed workflow run."""
    run_id: int
    workflow_name: str
    repo_full_name: str          # "owner/repo"
    branch: str                  # Branch the workflow ran on
    commit_sha: str              # The commit that triggered the run
    head_message: str            # Commit message
    run_attempt: int             # Which attempt this is
    logs_url: str                # URL to download logs
    html_url: str                # URL to view run in browser
    event: str                   # "push", "pull_request", etc.


# ---------------------------------------------------------------------------
# GitHub Client
# ---------------------------------------------------------------------------

class GitHubClient:
    """Wrapper around PyGithub for DevOps Agent operations."""

    def __init__(self, token: str | None = None):
        self.token = token or os.environ.get("GITHUB_TOKEN", "")
        if Github is None:
            raise ImportError("PyGithub not installed. Run: pip install PyGithub")
        self._gh = Github(self.token)

    def _get_repo(self, repo_full_name: str) -> "Repository":
        return self._gh.get_repo(repo_full_name)

    # ------------------------------------------------------------------
    # Workflow run info
    # ------------------------------------------------------------------

    def get_failed_run(self, repo_full_name: str, run_id: int) -> WorkflowFailure:
        """Get details about a specific failed workflow run."""
        repo = self._get_repo(repo_full_name)
        run = repo.get_workflow_run(run_id)

        return WorkflowFailure(
            run_id=run.id,
            workflow_name=run.name or "Unknown",
            repo_full_name=repo_full_name,
            branch=run.head_branch or "unknown",
            commit_sha=run.head_sha,
            head_message=run.head_commit.message if run.head_commit else "",
            run_attempt=run.run_attempt,
            logs_url=run.logs_url,
            html_url=run.html_url,
            event=run.event,
        )

    # ------------------------------------------------------------------
    # Log downloading
    # ------------------------------------------------------------------

    def download_logs(self, repo_full_name: str, run_id: int) -> bytes:
        """
        Download the log archive (ZIP) for a workflow run.
        Returns raw ZIP bytes.
        """
        repo = self._get_repo(repo_full_name)
        run = repo.get_workflow_run(run_id)
        # PyGithub doesn't have a direct method; use the REST API
        url = f"/repos/{repo_full_name}/actions/runs/{run_id}/logs"
        headers, data = self._gh._Github__requester.requestBlobAndCheck(  # type: ignore
            "GET", url
        )
        return data

    def download_logs_via_url(self, logs_url: str) -> bytes:
        """Download logs using the direct URL (alternative method)."""
        import requests
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github.v3+json",
        }
        resp = requests.get(logs_url, headers=headers, allow_redirects=True)
        resp.raise_for_status()
        return resp.content

    # ------------------------------------------------------------------
    # File operations
    # ------------------------------------------------------------------

    def get_file_content(self, repo_full_name: str, file_path: str, ref: str = "main") -> str | None:
        """Read a file's content from the repo. Returns None if not found."""
        repo = self._get_repo(repo_full_name)
        try:
            content_file = repo.get_contents(file_path, ref=ref)
            if isinstance(content_file, list):
                return None  # It's a directory
            return content_file.decoded_content.decode("utf-8", errors="replace")
        except GithubException as e:
            if e.status == 404:
                return None
            raise

    def get_aifix_content(self, repo_full_name: str, ref: str = "main") -> str:
        """Read the aifix.md file from the repo. Returns empty string if not found."""
        content = self.get_file_content(repo_full_name, "aifix.md", ref=ref)
        return content or ""

    # ------------------------------------------------------------------
    # Branch and commit operations
    # ------------------------------------------------------------------

    def create_fix_branch(self, repo_full_name: str, base_sha: str, branch_name: str) -> str:
        """
        Create a new branch for the fix.
        Returns the branch name.
        """
        repo = self._get_repo(repo_full_name)

        ref_name = f"refs/heads/{branch_name}"

        # Check if branch already exists
        try:
            existing = repo.get_git_ref(f"heads/{branch_name}")
            # Branch exists — update it to point to base_sha
            existing.edit(sha=base_sha, force=True)
            return branch_name
        except GithubException:
            pass

        # Create new branch
        repo.create_git_ref(ref=ref_name, sha=base_sha)
        return branch_name

    def commit_fix(
        self,
        repo_full_name: str,
        branch_name: str,
        file_changes: dict[str, str],
        commit_message: str,
    ) -> str:
        """
        Commit file changes to a branch.

        Args:
            repo_full_name: "owner/repo"
            branch_name: Target branch
            file_changes: Dict of file_path → new_content
            commit_message: Commit message

        Returns:
            The new commit SHA
        """
        repo = self._get_repo(repo_full_name)

        # Get the current commit on the branch
        ref = repo.get_git_ref(f"heads/{branch_name}")
        base_sha = ref.object.sha
        base_commit = repo.get_git_commit(base_sha)
        base_tree = base_commit.tree

        # Create tree elements for changed files
        tree_elements = []
        for file_path, content in file_changes.items():
            blob = repo.create_git_blob(content, "utf-8")
            tree_elements.append(InputGitTreeElement(
                path=file_path,
                mode="100644",
                type="blob",
                sha=blob.sha,
            ))

        # Create new tree
        new_tree = repo.create_git_tree(tree_elements, base_tree)

        # Create commit
        new_commit = repo.create_git_commit(
            message=commit_message,
            tree=new_tree,
            parents=[base_commit],
        )

        # Update branch reference
        ref.edit(sha=new_commit.sha)

        return new_commit.sha

    def update_aifix_md(
        self,
        repo_full_name: str,
        branch_name: str,
        new_content: str,
        commit_message: str = "chore: update aifix.md with new fix pattern",
    ) -> str:
        """Update the aifix.md file on the given branch."""
        return self.commit_fix(
            repo_full_name=repo_full_name,
            branch_name=branch_name,
            file_changes={"aifix.md": new_content},
            commit_message=commit_message,
        )

    # ------------------------------------------------------------------
    # Pull request operations
    # ------------------------------------------------------------------

    def create_pull_request(
        self,
        repo_full_name: str,
        branch_name: str,
        base_branch: str,
        title: str,
        body: str,
    ) -> str:
        """
        Create a pull request for the fix.
        Returns the PR URL.
        """
        repo = self._get_repo(repo_full_name)

        pr = repo.create_pull(
            title=title,
            body=body,
            head=branch_name,
            base=base_branch,
        )

        # Add labels
        try:
            pr.add_to_labels("ai-fix", "devops-agent")
        except GithubException:
            pass  # Labels might not exist

        return pr.html_url

    # ------------------------------------------------------------------
    # Workflow re-run
    # ------------------------------------------------------------------

    def rerun_workflow(self, repo_full_name: str, run_id: int) -> bool:
        """Re-run a failed workflow. Returns True if successful."""
        repo = self._get_repo(repo_full_name)
        try:
            run = repo.get_workflow_run(run_id)
            run.rerun()
            return True
        except GithubException:
            return False

    def trigger_workflow(
        self,
        repo_full_name: str,
        workflow_file: str,
        branch: str,
    ) -> bool:
        """
        Trigger a workflow_dispatch on a specific branch.
        Returns True if successful.
        """
        repo = self._get_repo(repo_full_name)
        try:
            workflow = repo.get_workflow(workflow_file)
            workflow.create_dispatch(ref=branch)
            return True
        except GithubException:
            return False

    def get_latest_run_status(
        self, repo_full_name: str, branch: str, workflow_name: str
    ) -> str | None:
        """
        Get the status of the latest workflow run on a branch.
        Returns "completed", "in_progress", "queued", or None.
        """
        repo = self._get_repo(repo_full_name)
        runs = repo.get_workflow_runs(branch=branch)
        for run in runs:
            if run.name == workflow_name:
                return run.status
        return None

    def get_latest_run_conclusion(
        self, repo_full_name: str, branch: str, workflow_name: str
    ) -> str | None:
        """
        Get the conclusion of the latest completed run.
        Returns "success", "failure", "cancelled", or None.
        """
        repo = self._get_repo(repo_full_name)
        runs = repo.get_workflow_runs(branch=branch, status="completed")
        for run in runs:
            if run.name == workflow_name:
                return run.conclusion
        return None

    # ------------------------------------------------------------------
    # Diff operations
    # ------------------------------------------------------------------

    def get_commit_diff(self, repo_full_name: str, commit_sha: str) -> str:
        """Get the diff for a specific commit."""
        repo = self._get_repo(repo_full_name)
        commit = repo.get_commit(commit_sha)

        diff_parts = []
        for file in commit.files:
            diff_parts.append(f"--- a/{file.filename}")
            diff_parts.append(f"+++ b/{file.filename}")
            if file.patch:
                diff_parts.append(file.patch)
            diff_parts.append("")

        return "\n".join(diff_parts)

    def get_workflow_yaml(
        self, repo_full_name: str, workflow_name: str, ref: str = "main"
    ) -> str | None:
        """Try to find and read the workflow YAML for a given workflow name."""
        repo = self._get_repo(repo_full_name)

        # Common workflow paths
        possible_paths = [
            f".github/workflows/{workflow_name}.yml",
            f".github/workflows/{workflow_name}.yaml",
        ]

        # Also try to list all workflows and find by name
        try:
            contents = repo.get_contents(".github/workflows", ref=ref)
            if isinstance(contents, list):
                for f in contents:
                    if f.name.endswith((".yml", ".yaml")):
                        possible_paths.append(f.path)
        except GithubException:
            pass

        for path in possible_paths:
            content = self.get_file_content(repo_full_name, path, ref=ref)
            if content:
                return content

        return None


# ---------------------------------------------------------------------------
# Helper: generate branch name
# ---------------------------------------------------------------------------

def generate_fix_branch_name(workflow_name: str, run_id: int) -> str:
    """Generate a descriptive branch name for the fix."""
    # Sanitize workflow name for branch name
    clean_name = re.sub(r"[^a-zA-Z0-9-]", "-", workflow_name.lower())
    clean_name = re.sub(r"-+", "-", clean_name).strip("-")
    return f"aifix/{clean_name}-{run_id}"
