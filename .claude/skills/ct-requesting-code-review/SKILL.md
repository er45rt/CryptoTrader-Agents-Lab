---
name: ct-requesting-code-review
description: CryptoTrader-Agents-Lab only. Use when a material step needs review, either to prepare the review package for ChatGPT and the independent Claude Code claude-opus-5-5 review, or when performing that independent review.
---

# Requesting code review (CryptoTrader)

## Governance rules (restated, not added)

This skill is tooling below governance. AGENTS.md > engineering_rules.md > lessons_candidates.md is the complete governance chain. This precedence describes repository governance only; it does not override platform instructions or the human's explicit scope and authorization. [engineering_rules.md, Authority and loading] This skill grants no authority and adds no rule; each rule below restates its cited source, and every section marked guidance is optional technique.

- Any governance conflict or ambiguity: STOP -> SENSITIVE -> human resolution. [AGENTS.md]
- Only read-only analysis is autonomous. The ROUTINE allowlist is empty, so every write is SENSITIVE. [AGENTS.md; engineering_rules.md, Repository policy]
- Classify every intended changed path under the pinned repository policy before editing. [AGENTS.md]
- For every material step, ChatGPT inspects evidence first; Claude Code claude-opus-5-5 reviews independently. [AGENTS.md]
- Obtain explicit human approval for the proposed scope before implementation, and implement only approved scope. [AGENTS.md]
- A local checkpoint requires separate explicit human authorization. This skill grants no authority for push, merge, deploy, promotion, live action or checkpoint; any such authority must come from existing governance and explicit human authorization. [AGENTS.md]
- Run relevant focused checks using offline fixtures/mocks. [engineering_rules.md, Evidence and approval workflow]
- Preserve existing tracked and untracked work. [AGENTS.md]
- Subagents may research, read and cross-check only; they cannot write or decide safety, invariants or merge authority. [AGENTS.md]
- If new evidence falsifies the plan or a result is unexpected, stop and present evidence before further edits. [AGENTS.md]

## Review rules (restated, not added)

- Any sensitive change receives the highest review tier regardless of size. [AGENTS.md]
- Claude silence, failure or model substitution is never approval. [AGENTS.md]
- Record both analyses and the disagreement resolution. Resolve reviewer disagreements by evidence; if unresolved, stop for the human before further edits. [AGENTS.md; engineering_rules.md, Evidence and approval workflow]
- A reviewer verdict does not replace explicit human authorization, and review approval alone cannot create a checkpoint. [AGENTS.md; engineering_rules.md, Evidence and approval workflow]
- Bind the review to base HEAD, the prospective tree and exact file hashes; before a commit exists, do not invent one. Keep the combined manifest outside the repository. [engineering_rules.md, Evidence and approval workflow]
- An intervening material delta invalidates the affected review and returns to evidence review and human approval. [engineering_rules.md, Evidence and approval workflow]

## Review package (implementer)

The package contains:

- The approved scope, quoted or referenced exactly. [AGENTS.md]
- Candidate identity: base HEAD, prospective tree, and every path with its operation, matched rule or default, tier and exact file hashes. [engineering_rules.md, Classification procedure; Evidence and approval workflow]
- Unrelated baseline changes, recorded separately. [engineering_rules.md, Classification procedure]
- Evidence bound to the same hashes, with omissions and limitations. [AGENTS.md; engineering_rules.md, Evidence and approval workflow]

Guidance (optional): the package may also include a short description of what changed and why, and the exact diff with its hash.

## Independent review (reviewer)

- The review is read-only analysis. [AGENTS.md]
- First confirm that the candidate bytes match the reviewed hashes; treat any byte difference as evidence to reconcile, and stop and present it. [AGENTS.md; engineering_rules.md, Evidence and approval workflow]
- Check the approved scope and the classification of every path; the root cause fixed with the smallest necessary delta and no opportunistic refactor; no unrelated local files; and current evidence from offline checks bound to the candidate. [AGENTS.md; engineering_rules.md, Evidence and approval workflow]
- Check the invariants in engineering_rules.md "Core ownership and preserved invariants": durable intent/attempt distinctions; evidence-based reconciliation after uncertain venue mutation; no blind retry authority; fail-closed risk; persisted safety/writer fencing; hard no-submit boundaries; journal/replay/idempotency; existing persisted contract/enum meanings, with review of migration and relevant replay tests for any change. [engineering_rules.md, Core ownership and preserved invariants]
- Findings do not expand the approved scope. [AGENTS.md]
- If the approved scope is silent or ambiguous on a point that may affect governance or scope, report it as a finding for human resolution; the reviewer does not settle it, choose a rule, or expand the scope. [AGENTS.md]
- End with an explicit statement that the candidate, identified by its exact hashes, is approved or not approved, with reasons. The verdict grants no authority for checkpoint, push, merge, deploy, promotion or live action. [AGENTS.md]

Output (guidance): a recommended format is strengths, stated specifically; findings by severity (Critical, Important, Minor), each with file:line, what is wrong, why it matters and a suggested fix; a declined-to-judge list of behaviors set aside as out of scope, with reasons; and out-of-scope notes. Points where the approved scope is silent or ambiguous belong in the findings for human resolution, as the rule above states.

## Warning signs (guidance)

Optional self-check. These may indicate a review problem: skipping review because a change is small; treating a subagent opinion as a decision; editing in response to findings before approval; evidence bound to different bytes than the candidate.
