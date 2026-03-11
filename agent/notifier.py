"""
notifier.py — Teams Notification System

Sends adaptive card notifications to Microsoft Teams via Incoming Webhook.
Color-coded by severity, includes failure details and suggested actions.
"""

import os
import json
import requests
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Notification types
# ---------------------------------------------------------------------------

@dataclass
class NotificationPayload:
    """Data for a Teams notification."""
    repo_name: str
    workflow_name: str
    branch: str
    error_summary: str
    classification: str          # "code", "config", "infra", "secret", "unknown"
    suggested_action: str
    confidence: int
    run_url: str                 # URL to the failed run
    details: str = ""            # Additional details
    fix_attempted: bool = False  # Whether the agent tried to fix it
    fix_result: str = ""         # Result of fix attempt


# Color scheme
SEVERITY_COLORS = {
    "code": "warning",          # Yellow — agent may fix
    "config": "attention",      # Red — needs human
    "infra": "attention",       # Red — needs human
    "secret": "attention",      # Red — needs human
    "transient": "good",        # Green — auto-retried
    "unknown": "attention",     # Red — needs human
    "fixed": "good",            # Green — agent fixed it
}


# ---------------------------------------------------------------------------
# Teams notification sender
# ---------------------------------------------------------------------------

class TeamsNotifier:
    """Send notifications to Microsoft Teams via Incoming Webhook."""

    def __init__(self, webhook_url: str | None = None):
        self.webhook_url = webhook_url or os.environ.get("TEAMS_WEBHOOK_URL", "")

    def send(self, payload: NotificationPayload) -> bool:
        """
        Send a notification to Teams.
        Returns True if successful, False otherwise.
        """
        if not self.webhook_url:
            print("[NOTIFIER] No Teams webhook URL configured. Logging notification:")
            print(f"  Repo: {payload.repo_name}")
            print(f"  Workflow: {payload.workflow_name}")
            print(f"  Error: {payload.error_summary}")
            print(f"  Classification: {payload.classification}")
            print(f"  Action: {payload.suggested_action}")
            return False

        card = self._build_adaptive_card(payload)

        try:
            response = requests.post(
                self.webhook_url,
                json=card,
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            response.raise_for_status()
            return True
        except requests.RequestException as e:
            print(f"[NOTIFIER] Failed to send Teams notification: {e}")
            return False

    def _build_adaptive_card(self, payload: NotificationPayload) -> dict:
        """Build a Teams Adaptive Card for the notification."""
        color = SEVERITY_COLORS.get(payload.classification, "attention")

        # Status emoji
        status_emoji = {
            "code": "🔧",
            "config": "⚙️",
            "infra": "🏗️",
            "secret": "🔐",
            "transient": "🔄",
            "unknown": "❓",
            "fixed": "✅",
        }.get(payload.classification, "⚠️")

        # Title
        if payload.fix_attempted and payload.classification == "fixed":
            title = f"✅ Auto-Fixed: {payload.workflow_name}"
        else:
            title = f"{status_emoji} CI Failure: {payload.workflow_name}"

        card = {
            "type": "message",
            "attachments": [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": {
                        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                        "type": "AdaptiveCard",
                        "version": "1.4",
                        "body": [
                            {
                                "type": "TextBlock",
                                "text": title,
                                "weight": "Bolder",
                                "size": "Large",
                                "color": color,
                            },
                            {
                                "type": "FactSet",
                                "facts": [
                                    {"title": "Repository", "value": payload.repo_name},
                                    {"title": "Workflow", "value": payload.workflow_name},
                                    {"title": "Branch", "value": payload.branch},
                                    {"title": "Classification", "value": f"{status_emoji} {payload.classification.upper()}"},
                                    {"title": "Confidence", "value": f"{payload.confidence}%"},
                                ],
                            },
                            {
                                "type": "TextBlock",
                                "text": "Error Summary",
                                "weight": "Bolder",
                                "spacing": "Medium",
                            },
                            {
                                "type": "TextBlock",
                                "text": payload.error_summary[:500],
                                "wrap": True,
                                "fontType": "Monospace",
                                "size": "Small",
                            },
                            {
                                "type": "TextBlock",
                                "text": "Suggested Action",
                                "weight": "Bolder",
                                "spacing": "Medium",
                            },
                            {
                                "type": "TextBlock",
                                "text": payload.suggested_action,
                                "wrap": True,
                                "color": color,
                            },
                        ],
                        "actions": [
                            {
                                "type": "Action.OpenUrl",
                                "title": "View Failed Run",
                                "url": payload.run_url,
                            },
                        ],
                    },
                }
            ],
        }

        # Add fix result details if applicable
        if payload.fix_attempted:
            card["attachments"][0]["content"]["body"].append({
                "type": "TextBlock",
                "text": f"🤖 Agent Result: {payload.fix_result}",
                "wrap": True,
                "spacing": "Medium",
                "weight": "Bolder",
            })

        # Add extra details if provided
        if payload.details:
            card["attachments"][0]["content"]["body"].append({
                "type": "TextBlock",
                "text": payload.details[:1000],
                "wrap": True,
                "spacing": "Small",
                "size": "Small",
                "isSubtle": True,
            })

        return card


# ---------------------------------------------------------------------------
# Convenience function
# ---------------------------------------------------------------------------

def notify_failure(
    repo_name: str,
    workflow_name: str,
    branch: str,
    error_summary: str,
    classification: str,
    suggested_action: str,
    confidence: int,
    run_url: str,
    webhook_url: str | None = None,
    **kwargs,
) -> bool:
    """Quick helper to send a failure notification."""
    notifier = TeamsNotifier(webhook_url)
    payload = NotificationPayload(
        repo_name=repo_name,
        workflow_name=workflow_name,
        branch=branch,
        error_summary=error_summary,
        classification=classification,
        suggested_action=suggested_action,
        confidence=confidence,
        run_url=run_url,
        **kwargs,
    )
    return notifier.send(payload)
