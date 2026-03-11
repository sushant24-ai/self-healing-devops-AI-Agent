# 🤖 DevOps Agent — Self-Healing GitHub Actions Pipeline

A GitHub Action agent that monitors your CI/CD workflows, automatically fixes code-level failures, and notifies your team for issues that need human intervention.

**Supports OpenAI (GPT), Anthropic (Claude), and Google (Gemini)** — configurable provider and model version.

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

### 1. Add API Key Secret (ONE of these)

| Secret | Provider | Models |
|--------|----------|--------|
| `OPENAI_API_KEY` | OpenAI | `gpt-4o`, `gpt-4o-mini`, `gpt-4-turbo`, `o3-mini` |
| `ANTHROPIC_API_KEY` | Anthropic | `claude-sonnet-4-20250514`, `claude-3-5-haiku-20241022` |
| `GEMINI_API_KEY` | Google | `gemini-2.0-flash`, `gemini-2.5-pro` |
| `LLM_API_KEY` | Any (generic) | Works with any provider |

> Only ONE API key is needed. Choose your preferred LLM provider.

### 2. Optional Secrets

| Secret | Description |
|--------|-------------|
| `TEAMS_WEBHOOK_URL` | Microsoft Teams incoming webhook URL |

> The `GITHUB_TOKEN` is automatically provided by GitHub Actions.

### 3. Configure Provider (in workflow YAML)

Edit `.github/workflows/devops-agent.yml`:

```yaml
env:
  LLM_PROVIDER: "anthropic"              # openai | anthropic | gemini
  LLM_MODEL: "claude-sonnet-4-20250514"         # specific model version
```

Or leave both empty — the provider is auto-detected from the model name.

### 4. Copy & Deploy

Copy these into your repo:
- `agent/` directory
- `requirements.txt`
- `.github/workflows/devops-agent.yml`

Done! The agent triggers automatically on any workflow failure.

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
│   ├── fixer.py             # Multi-provider LLM fix generation
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

### Multi-Provider LLM Support
- **Auto-detection**: Pass `--model claude-sonnet-4-20250514` and the agent automatically uses the Anthropic API
- **Explicit control**: Use `--provider gemini --model gemini-2.5-pro` for full control
- **Smart API key resolution**: Checks `LLM_API_KEY` → provider-specific env var → configured default

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
# Using OpenAI (default)
python -m agent.main --repo "owner/repo" --run-id 12345 --dry-run

# Using Claude
python -m agent.main --repo "owner/repo" --run-id 12345 --dry-run \
  --provider anthropic --model claude-sonnet-4-20250514

# Using Gemini
python -m agent.main --repo "owner/repo" --run-id 12345 --dry-run \
  --provider gemini --model gemini-2.0-flash

# Auto-detect provider from model name
python -m agent.main --repo "owner/repo" --run-id 12345 --dry-run \
  --model gpt-4o-mini
```

## ⚙️ Configuration

### Environment Variables

| Variable | Description |
|----------|-------------|
| `GITHUB_TOKEN` | GitHub token with repo + actions permissions |
| `LLM_API_KEY` | Generic API key (works with any provider) |
| `OPENAI_API_KEY` | OpenAI-specific API key |
| `ANTHROPIC_API_KEY` | Anthropic-specific API key |
| `GEMINI_API_KEY` | Google Gemini-specific API key |
| `TEAMS_WEBHOOK_URL` | Teams webhook URL (optional) |

### CLI Arguments

| Argument | Description |
|----------|-------------|
| `--repo` | Repository in `owner/repo` format |
| `--run-id` | Failed workflow run ID |
| `--provider` | LLM provider: `openai`, `anthropic`, `gemini` (auto-detected if not set) |
| `--model` | Specific model version (e.g. `gpt-4o-mini`, `claude-sonnet-4-20250514`, `gemini-2.0-flash`) |
| `--dry-run` | Analyze but don't push changes |

## 🛡️ Safety Features

- **Never auto-merges** — always creates a PR for human review
- **Secret scrubbing** — removes API keys, tokens, passwords from logs before LLM
- **Max 3 fix attempts** — prevents infinite loops
- **15-minute timeout** — workflow kills itself if stuck
- **Concurrency control** — prevents parallel agent runs on same failure

## 📄 License

MIT
