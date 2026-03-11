🏗️ System-Level Architecture
┌─────────────────────────────────────────────────────────────────────┐
│                     TRIGGER LAYER                                    │
│  GitHub webhook (workflow_run.completed) → devops-agent.yml          │
│                                                                      │
│  Tradeoff: PULL (poll for failures) vs PUSH (event-driven)           │
│  We chose: PUSH (webhook) — zero latency, zero wasted compute       │
└──────────────────────────┬──────────────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────────────┐
│                     ORCHESTRATOR (main.py)                           │
│  Sequential state machine, not async event-driven                    │
│  Deliberate choice: predictable > fast for MVP                       │
└──────────────────────────┬──────────────────────────────────────────┘
                           │
          ┌────────────────┼────────────────────┐
          │                │                    │
    ┌─────▼─────┐   ┌─────▼─────┐   ┌─────────▼────────┐
    │ log_parser │   │ memory.py │   │ classifier.py     │
    │ (no LLM)  │   │ (no LLM)  │   │ (LLM optional)    │
    └───────────┘   └───────────┘   └──────────────────┘
                                           │
                    ┌──────────────────────┼──────────────┐
                    │                      │              │
              ┌─────▼─────┐         ┌─────▼────┐  ┌─────▼────┐
              │ fixer.py   │         │ notifier │  │ github   │
              │ (LLM req)  │         │ (Teams)  │  │ _ops.py  │
              └────────────┘         └──────────┘  └──────────┘
Key architectural decision: The system is designed as a cost funnel. Notice how everything above the 

fixer.py
 line is free (no LLM). Most failures get resolved or routed before ever touching an LLM.

Component-by-Component: Decisions & Tradeoffs
1. 

main.py
 — The Orchestrator
Pattern chosen: Sequential state machine Pattern rejected: Event-driven / async / multi-agent swarm

Decision	What we chose	Alternative	Why
Execution model	Sequential	Async/parallel	Predictability > speed. A CI fix doesn't need to be fast — it needs to be correct. Sequential means easier debugging, simpler error handling, deterministic behavior
State management	In-memory (single run)	Persistent DB (Redis/Postgres)	MVP tradeoff. Each GitHub Action run is isolated anyway. A DB adds infra complexity for zero gain in a single-run model
Max attempts	Hard cap of 3	Infinite / dynamic	Safety first. Infinite loops are the #1 risk in autonomous agents. 3 gives enough room for Reflexion without runaway costs. In prod, this should be configurable
Error handling	Catch-all with notification	Crash and let GitHub retry	We never want to silently fail. Even if the agent crashes, it tries to notify the team. This is the "always respond" principle from the reliability research
Tradeoff I'd change in V2: The orchestrator is a monolith. In V2, I'd split it into discrete agents (analyzer, fixer, notifier) communicating via a queue. This gives you bulkhead isolation — the fixer crashing doesn't take down the notifier.

2. 

memory.py
 — The aifix.md System
This is the most opinionated design decision in the entire system.

Decision	What we chose	Alternative	Why
Storage format	Markdown in the repo	Database / JSON / SQLite	Human-readable + version-controlled. Any dev can open aifix.md, see every fix, and understand the agent's history. A database is invisible and unauditable. Markdown diffs show up in PRs, so you can review what the agent learned
Search algorithm	Exact substring + fuzzy token overlap (70%)	Vector embeddings / semantic search	Cost and complexity. Embeddings need an embedding model, a vector store, and add latency. Token overlap handles 90% of cases (CI errors are structured, not natural language). V2 should add embeddings for novel errors
Match threshold	70% token overlap	Higher (90%) or lower (50%)	Balance of precision and recall. 70% catches "same error, different line number" while avoiding "vaguely similar but different root cause." This was a gut call — production data would tune it
Update strategy	Bump count + confidence on reuse	Immutable log (append-only)	Mutable entries evolve. Confidence should increase with repeated success. But we lose history of individual applications — V2 should add a history section per entry
The big tradeoff: Markdown parsing is brittle. If someone hand-edits aifix.md and breaks the format, the parser might skip entries. I chose to fail open (skip unparseable blocks, not crash) rather than fail closed. For a DevOps tool, availability > correctness.

Alternative view: Some would argue for a .aifix.json file instead — structured, parseable, not ambiguous. I chose markdown because the #1 adoption barrier for AI tooling is trust. When a developer can read the agent's "brain" in plain markdown and edit it, they trust it faster.

3. 

log_parser.py
 — Smart Log Extraction
Design philosophy: The LLM should only see what it NEEDS to see.

Decision	What we chose	Alternative	Why
Parsing approach	3-tier deterministic (markers → patterns → context)	Send full log to LLM and let it figure it out	Cost: $0.15/call → $0.01/call. 93% reduction. Also reduces hallucination — LLMs get confused by irrelevant context. Less is literally more
Error patterns	Regex patterns per language (Python, Node, Java, etc.)	Single generic "find error" approach	Precision. Traceback (most recent call last) is unambiguous in Python. Generic approaches miss language-specific patterns or match false positives
Context window	30 lines above + 10 lines below error	Fixed window (e.g., last 100 lines)	Asymmetric context. Stack traces grow UPWARD from the error. You need more above than below. 30+10 captures most stack traces without including setup noise
Secret scrubbing	Regex patterns for known secret formats	No scrubbing (trust the LLM) / AST-based detection	Defense in depth. Even though GitHub redacts secrets in logs, we add a second layer. Regex is fast and catches the 80/20 case. AST-based would be perfect but overkill for MVP
Log ZIP handling	Extract all jobs, combine, then parse	Parse each job separately and merge results	Simplicity. Cross-job errors (e.g., a build step fails because a setup step had a warning) are caught by combining. V2 should do per-job analysis with cross-referencing
What I'd change: The pattern system is hardcoded. In V2, patterns should be pluggable — loaded from a config file or even from aifix.md itself ("when you see this pattern, classify as X"). This makes the system extensible without code changes.

4. 

classifier.py
 — Failure Classification
Core principle: Don't use an LLM when a regex will do.

Decision	What we chose	Alternative	Why
Architecture	2-tier (pattern match → LLM fallback)	LLM-only / pure pattern match only	Cost cascade. ~70% of CI failures match known patterns (ETIMEDOUT, ModuleNotFoundError, etc.). These are classified in <1ms for $0. Only novel errors need the LLM at ~$0.01/call
Classification taxonomy	5 types (code, config, infra, transient, secret)	Binary (fixable / not fixable) / richer taxonomy	Actionable granularity. Each type maps to a clear action: code→fix, transient→retry, secret→notify. Binary is too coarse (you'd retry a secret issue unnecessarily). Richer (e.g., "dependency" vs "syntax") would complicate routing without changing the action
LLM response format	Strict JSON with defined schema	Free text / function calling	Reliability. JSON is parseable, validatable, and works across all providers. Free text needs another LLM call to parse. Function calling is OpenAI-specific and would break multi-provider
Confidence scoring	Pattern match: fixed scores (75-90). LLM: self-reported	No confidence / external calibration	Tradeoff: we trust the LLM's self-assessment, which is often poorly calibrated. V2 should calibrate confidence against actual outcomes (did the fix work?). For now, the confidence gate in the orchestrator prevents reckless fixes
Honest tradeoff: Tier 1 patterns are static intelligence. They don't learn. If a new error pattern starts appearing frequently, someone needs to add it to the pattern list manually. The aifix.md system partially compensates — if the LLM fixes it once, it becomes a "known fix" — but the classifier doesn't learn from aifix.md. V2 should merge these: classifier reads aifix.md patterns as Tier 1.5.

5. 

fixer.py
 — Multi-Provider LLM Client
Design choice: Provider-agnostic interface with lazy imports

Decision	What we chose	Alternative	Why
Provider abstraction	Single 

LLMClient
 class with internal routing	Abstract base class + provider subclasses (strategy pattern)	Simplicity. 3 providers don't justify a class hierarchy. The single class with if/elif is 100 lines. A strategy pattern would be 200+ lines across 5 files. When you have 10+ providers, refactor
Auto-detection	Prefix-based model name matching	Config file / environment variable only	Developer ergonomics. --model claude-sonnet-4-20250514 just works without knowing "anthropic" is the provider. Less to configure = less to misconfigure
SDK imports	Lazy (import inside method)	Eager (import at top of file)	Only install what you use. If someone uses OpenAI, they shouldn't need 

anthropic
 installed. Lazy imports mean pip install openai is enough for OpenAI-only usage
Temperature	0.2 (hard-coded)	Configurable per-call	Determinism for code fixes. You want the LLM to be as consistent as possible. 0.0 gives perfect determinism but sometimes gets stuck. 0.2 adds just enough exploratory variation for fixes
Prompting	Structured JSON output with strict rules	Chain-of-thought / free text / function calling	Cross-provider compatibility. Function calling is OpenAI-specific. CoT adds latency and cost. Structured JSON works with all 3 providers and is machine-parseable
The big provider tradeoff:

OpenAI: Best function calling, fastest, but most expensive
Claude: Best at code understanding (arguably), excellent at following rules, system prompt as first-class citizen
Gemini: Cheapest, good for simple fixes, but weaker at complex multi-file reasoning
We made NO opinion on which is "best" — the user chooses. But the default is gpt-4o because it has the broadest CI/CD training data.

6. 

github_ops.py
 — GitHub API Operations
Decision	What we chose	Alternative	Why
Library	PyGithub (synchronous)	httpx + raw REST API / gql (GraphQL)	Battle-tested. PyGithub handles auth, pagination, rate limits. Raw REST means reimplementing all that. GraphQL is more efficient but PyGithub's REST wrapper is simpler for our use case
Branch strategy	Always new branch (aifix/<workflow>-<runid>)	Fix on the same branch / use forks	Safety. New branches are non-destructive and idempotent. If the branch exists, we force-update it. Fixing on the source branch could break other PRs. Forks add complexity
PR strategy	Always create PR, never merge	Auto-merge if confidence > 95%	Trust building. The agent should NEVER merge its own code in MVP. Humans review. Auto-merge is a V2 feature after the team has built confidence through shadow mode
Log download	ZIP download via REST API	GraphQL log query / streaming	Only option. GitHub only provides logs as ZIP downloads. No streaming API exists. We download, extract, then parse in-memory
Tradeoff: PyGithub is synchronous, which means every API call blocks. For a single-run agent, this is fine. For V2 with parallel repo monitoring, you'd want httpx async or aiohttp.

7. 

notifier.py
 — Teams Notifications
Decision	What we chose	Alternative	Why
Notification method	Incoming Webhook (Adaptive Cards)	Microsoft Graph API / Bot Framework	Zero setup. An incoming webhook is a URL you paste into the repo secrets. Graph API needs Azure AD app registration, OAuth, tenant config — 10x more setup for the same result
Card format	Adaptive Card v1.4	Simple text message / MessageCard (legacy)	Rich formatting. Color-coded severity, structured facts, action buttons. Adaptive Cards are Microsoft's current standard and render well on desktop, mobile, and web
Notification aggregation	None (1 notification per failure)	Batched digest (aggregate multiple failures)	MVP tradeoff. Aggregation needs state persistence across runs. For MVP, one notification per failure is acceptable. V2 should add a DLQ + digest to prevent alert fatigue
Fallback	Console logging if no webhook URL	Fail silently / GitHub Issue / email	Transparency. If Teams isn't configured, the agent still logs its actions. This makes dry-run testing possible without any external dependencies
Cross-Cutting Tradeoffs
🔴 What We Consciously Sacrificed (MVP Tax)
No persistent state — Each run is isolated. No database, no Redis. This means no cross-run learning within a single execution, but aifix.md compensates at the repo level
No authentication/RBAC — Anyone with repo access can trigger the agent. V2 needs a "who can configure the agent" model
No cost tracking — We don't track LLM spend per run. V2 should log token count and cost per fix
Single-repo scope — Org-wide learning via semantic memory (from the architecture doc) isn't implemented. Each repo's aifix.md is independent
No observability — Just print() statements. V2 needs structured logging (JSON), distributed tracing (OpenTelemetry), and metrics (Prometheus)
🟢 What We Got Right (Architectural Wins)
Cost funnel — Most failures never touch an LLM. Pattern match → aifix.md → classifier → fixer. Each layer is cheaper than the next
aifix.md as human-readable memory — Auditable, version-controlled, editable by humans. Trust is the #1 adoption problem for AI agents
Provider-agnostic from day 1 — No vendor lock-in. Switch from GPT-4o to Claude to Gemini with a single env var
Never auto-merge — The agent creates PRs, never merges. This is the right trust boundary for V1
Secret scrubbing — Defense in depth before any data leaves the repo
