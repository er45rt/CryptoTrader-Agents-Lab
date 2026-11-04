---
name: ct-systematic-debugging
description: CryptoTrader-Agents-Lab only. Use when a test fails, a bug is reported, or behavior is unexpected, before proposing any fix. Read-only root-cause investigation that ends in a written fix proposal for review and human approval.
---

# Systematic debugging (CryptoTrader)

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

## Debugging rules (restated, not added)

- Fix the root cause with the smallest necessary delta; no opportunistic refactor or elegance work. [AGENTS.md]
- Investigation is read-only analysis. Adding instrumentation, tests or fixes is a write and needs approved scope first. [AGENTS.md]
- Record unrelated baseline changes separately and preserve them. [engineering_rules.md, Classification procedure]
- Preserve evidence-based reconciliation after uncertain venue mutation, never add blind retry authority, and preserve fail-closed risk. [engineering_rules.md, Core ownership and preserved invariants]

## Investigation (guidance)

Optional techniques; none of them is a rule.

1. Consider reading errors completely: stack traces, line numbers, file paths, error codes and warnings.
2. Reproducing the issue offline with fixtures and mocks is recommended. If it does not reproduce, gathering more evidence may be more useful than guessing.
   A pytest run can write into the worktree. To keep a reproduction non-mutating, consider running Python with -B and pytest with -p no:cacheprovider, as in the command in ct-verification-before-completion. Those options prevent only bytecode and pytest cache writes, so an optional disposable copy outside the repository is the more complete choice. The offline caveats in ct-verification-before-completion apply to any such run.
3. Git inspection that avoids optional index refresh may show relevant recent changes: for example git --no-optional-locks status --short, and git --no-optional-locks with diff, log or show. Plain git status may rewrite the index, so the --no-optional-locks form (or GIT_OPTIONAL_LOCKS=0) is recommended.
4. At component boundaries (strategy, risk, OMS/execution, venue adapter, journal/ledger, recovery), existing logs, journal entries, traces and offline test output may show where the data first goes wrong.
5. Tracing backward through the call chain to the original trigger is recommended.
6. Consider finding similar working code in this repository, reading it completely, and listing every difference from the failing path.
7. Consider stating one hypothesis ("X is the root cause because Y") with its evidence and testing it by read-only means, one variable at a time.
8. If something is not understood, saying so is recommended.

## Fix proposal

The proposal ends this skill; nothing is implemented without approval. It contains:

- The root cause and the evidence for it. [AGENTS.md]
- The smallest fix at the source. [AGENTS.md]
- Every intended path with its matched rule or default, the policy identities and the resulting tier. [engineering_rules.md, Classification procedure]
- The proposed tests and, as appropriate, traces, logs and mutation checks, with omissions and limitations documented. [AGENTS.md; engineering_rules.md, Evidence and approval workflow]
- Affected invariants, and any persisted contract or enum impact with review of migration and relevant replay tests. [AGENTS.md; engineering_rules.md, Core ownership and preserved invariants]

Guidance (optional): ct-verification-before-completion may be used before claiming any result. If approved fixes keep failing, or each fix reveals a new problem elsewhere, consider saying that the design may be wrong when presenting the evidence.

## Warning signs (guidance)

Optional self-check. These may indicate that the investigation is incomplete: "quick fix for now", "just try X and see", a fix proposed before the data flow is traced, "this might work" without understanding, and "one more fix attempt".
