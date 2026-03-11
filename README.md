# 🤖 DevOps Agent — Self-Healing GitHub Actions Pipeline

A GitHub Action agent that monitors your CI/CD workflows, automatically fixes code-level failures, and notifies your team for issues that need human intervention.

## ✨ Key Feature: `aifix.md` Memory

The agent maintains an `aifix.md` file in your repo that stores every successful fix. **Before calling any LLM**, the agent checks this file first. If a known fix matches the current error, it's applied instantly — **zero LLM cost, sub-second response**.

The more the agent runs, the smarter it gets for YOUR specific repo.

## 🏗️ Architecture

```
Workflow Fails → Download Logs → Smart Log Parser (10k → ~80 lines)
  → Check aifix.md (FREE, instant)
    ├── Known fix found → Apply → Push branch → Re-run CI → Update aifix.md
    └── No known fix → Classify (pattern match → LLM)
          ├── Code issue → LLM Fix → Branch → CI → PR → Write to aifix.md
          ├── Transient → Auto-retry workflow
          └── Config/Infra/Secret → Teams notification
```

## 🚀 Quick Setup

### 1. Add Secrets to Your Repo

| Secret | Required | Description |
|--------|----------|-------------|
| `OPENAI_API_KEY` | ✅ Yes | Your OpenAI API key for fix generation |
| `TEAMS_WEBHOOK_URL` | ⬜ Optional | Microsoft Teams incoming webhook URL |

> The `GITHUB_TOKEN` is automatically provided by GitHub Actions.

### 2. Copy the Workflow

Copy `.github/workflows/devops-agent.yml` to your repo's `.github/workflows/` directory.

### 3. Copy the Agent Code

Copy the `agent/` directory and `requirements.txt` to your repo root.

### 4. Done!

The agent will automatically trigger whenever any workflow in your repo fails.

## 📁 Project Structure

```
devops-agent/
├── .github/workflows/
│   └── devops-agent.yml     # Trigger: runs on workflow failure
├── agent/
│   ├── main.py              # Orchestrator (state machine)
│   ├── memory.py            # aifix.md read/write/search
│   ├── log_parser.py        # Smart log extraction (10k → 80 lines)
│   ├── classifier.py        # 2-tier failure classification
│   ├── fixer.py             # LLM-powered fix generation
│   ├── github_ops.py        # GitHub API operations
│   └── notifier.py          # Teams webhook notifications
├── tests/
│   └── test_memory.py       # Unit tests
├── requirements.txt
└── README.md
```

## 🧠 How It Works

### Smart Log Parsing
- Downloads log ZIP from GitHub Actions
- Finds the failing step using `##[error]` markers
- Extracts error signature + 30 lines context (not full 10k lines)
- Scrubs secrets before sending anything to LLM
- **Result: 93% cost reduction** vs sending full logs

### aifix.md Memory
- Checked FIRST before any LLM call
- Exact substring matching + fuzzy token overlap (70% threshold)
- Each successful fix is appended with error signature, diff, and confidence
- Confidence increases with each successful reuse
- **Result: Recurring errors fixed for $0**

### Failure Classification
- **Tier 1 (Pattern Match):** Known error patterns → instant classification, no LLM
- **Tier 2 (LLM):** Novel errors → LLM classifies as code/config/infra/transient/secret

### Fix Generation (Reflexion Loop)
- Builds minimal context package for LLM
- Max 3 fix attempts with Reflexion (learns from failed attempts)
- Each attempt creates a branch and waits for CI verification
- Only creates PR if CI passes on the fix branch

## 🧪 Running Tests

```bash
cd devops-agent
pip install -r requirements.txt
pip install pytest
python -m pytest tests/ -v
```

## 🔧 Local Testing (Dry Run)

```bash
cd devops-agent
python -m agent.main \
  --repo "owner/repo" \
  --run-id 12345 \
  --dry-run
```

## ⚙️ Configuration

Environment variables:

| Variable | Description |
|----------|-------------|
| `GITHUB_TOKEN` | GitHub token with repo + actions permissions |
| `OPENAI_API_KEY` | OpenAI API key |
| `TEAMS_WEBHOOK_URL` | Teams webhook URL (optional) |

CLI arguments:

| Argument | Description |
|----------|-------------|
| `--repo` | Repository in `owner/repo` format |
| `--run-id` | Failed workflow run ID |
| `--dry-run` | Analyze but don't push changes |
| `--model` | LLM model to use (default: `gpt-4o`) |

## 🛡️ Safety Features

- **Never auto-merges** — always creates a PR for human review
- **Secret scrubbing** — removes API keys, tokens, passwords from logs before LLM
- **Max 3 fix attempts** — prevents infinite loops
- **15-minute timeout** — workflow kills itself if stuck
- **Concurrency control** — prevents parallel agent runs on same failure

## 📄 License

MIT
