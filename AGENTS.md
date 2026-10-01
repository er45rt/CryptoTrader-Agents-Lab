# CryptoTrader-Agents-Lab: mandatory governance
Scope: this repository. These instructions guide process and grant no operational authority.
Precedence: AGENTS.md > engineering_rules.md > lessons_candidates.md.
Within governance, this file is authoritative; engineering_rules.md only interprets and applies it.
lessons_candidates.md has no authority and cannot change behavior; CLAUDE.md is only a loader.
Any governance conflict or ambiguity: STOP -> SENSITIVE -> human resolution; do not choose a rule yourself.
Read engineering_rules.md before changes; record both version fields, repository_policy_id and file hashes.
Start read-only: verify HEAD/tree/status/refs and preserve existing tracked and untracked work.
Do not modify main/master, rewrite history, or include unrelated local files.
For every material step, ChatGPT inspects evidence first; Claude Code claude-opus-5-5 reviews independently.
Resolve reviewer disagreements by evidence; if unresolved, stop for the human before further edits.
Claude silence, failure or model substitution is never approval.
Obtain explicit human approval for the proposed material/sensitive scope before implementation.
Implement only approved scope; verify it, obtain explicit final Claude review, and record provenance.
A local checkpoint requires separate explicit human authorization; review approval alone cannot create it.
These files authorize no push, merge, deploy, promotion or live action.
PROVENANCE_VERIFIED proves only the exact candidate provenance claimed by its verifier.
Completion requires appropriate tests/traces/logs/mutation/provenance bound to the reviewed candidate.
Self-assessment, votes and historical test counts cannot replace current evidence.
If new evidence falsifies the plan or a result is unexpected, stop and present evidence before further edits.
Fix the root cause with the smallest necessary delta; no opportunistic refactor or elegance work.
Classify every intended changed path under the pinned repository policy before editing.
Unknown classification is SENSITIVE; any sensitive change receives the highest review tier regardless of size.
Autonomy is policy-limited: the initial ROUTINE allowlist is empty; only read-only analysis is autonomous.
Subagents may research/read/cross-check only; they cannot write or decide safety, invariants or merge authority.
Lessons follow candidate -> review -> human promotion -> version -> retirement; never auto-update rules.
Human approval and explicit versioning are required for material permanent-rule changes/removal and lesson retirement.
Never silently change existing taxonomy/enum meanings; preserve persisted contracts and verify affected replay.
Inspect applicable loading overrides; missing required rules, ambiguity or conflict means STOP -> SENSITIVE -> human resolution.
