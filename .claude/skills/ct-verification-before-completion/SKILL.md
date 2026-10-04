---
name: ct-verification-before-completion
description: CryptoTrader-Agents-Lab only. Use immediately before stating that work is complete, fixed, passing, or ready for review or checkpoint. Requires current evidence bound to the exact reviewed candidate.
---

# Verification before completion (CryptoTrader)

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

## Verification rules (restated, not added)

- Completion requires appropriate tests, traces, logs, mutation checks and provenance bound to the reviewed candidate. [AGENTS.md]
- Self-assessment, votes and historical test counts cannot replace current evidence. [AGENTS.md]
- Never claim that a suggested or historical test was run, or that the confidence of an agent is a gate. A skipped test was not run. [engineering_rules.md, Evidence and approval workflow]
- Document omissions and limitations. [engineering_rules.md, Evidence and approval workflow]
- Focused checks run offline, and evidence is not claimed to be offline unless it was. Before calling evidence offline, confirm that environment-gated network and credential tests were disabled for that run; excluding pytest markers alone does not show this. [engineering_rules.md, Evidence and approval workflow]
- Bind final evidence to base HEAD, the prospective tree and exact file hashes; before a commit exists, do not invent one. Record worktree SHA-256 separately and treat any byte difference as evidence to reconcile before edits. [engineering_rules.md, Evidence and approval workflow]
- Mutation and red-green checks stay within approved scope and preserve existing tracked and untracked work. [AGENTS.md]
- Completion also requires explicit final Claude review and recorded provenance. Claude silence, failure or model substitution is never approval. [AGENTS.md]
- Passing verification creates no checkpoint; a local checkpoint requires separate explicit human authorization. [AGENTS.md]

## Recommended project-local test command (guidance)

This is a human-approved, project-local recommendation, not governance. It adds no requirement and no authority beyond AGENTS.md and engineering_rules.md; the offline rules above are the governing requirement.

From the repository root, the recommended full run uses the existing project .venv directly, without uv run, installation or environment sync:

    .\.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider -m "not testnet and not mainnet_readonly"

Focused test paths for the change may be appended. -B and -p no:cacheprovider avoid bytecode and pytest cache writes in the worktree; they do not prevent every write a test may make.

This command excludes the testnet and mainnet_readonly markers only. It is not by itself proof of zero network activity: some opt-in network or credential smoke tests are gated by environment variables rather than markers. Before the evidence is called offline, those environment-gated tests are confirmed disabled, as the rule above states. Known opt-ins at the reviewed base (the test sources remain authoritative and this list may become stale):

- RUN_DEEPSEEK_SMOKE=1 with DEEPSEEK_API_KEY: live DeepSeek smoke tests, with no pytest marker, so the marker filter does not exclude them.
- CRYPTO_TRADER_RUN_TESTNET=1 with BINANCE_TESTNET_API_KEY and BINANCE_TESTNET_API_SECRET: live testnet tests, also marked testnet.
- CRYPTO_TRADER_RUN_MAINNET_READONLY=1 with BINANCE_MAINNET_READONLY_API_KEY and BINANCE_MAINNET_READONLY_API_SECRET: live mainnet read-only tests, also marked mainnet_readonly.

Optional technique: check only whether each opt-in variable is set, without printing any credential value, and confirm in the pytest output that the gated tests were skipped or deselected.

## Checking a claim (guidance)

Optional technique: identify the command that proves the claim, run it fresh and in full, and read the whole output, including the exit code and the pass, fail, error and skip counts. Stating a claim together with that evidence, or otherwise stating the actual status, is recommended.

| Claim | Suggested evidence | Weaker substitute |
|---|---|---|
| Tests pass | Current offline run: 0 failures, 0 errors, skips listed | Previous run, "should pass" |
| Bug fixed | The original symptom, reproduced offline, now passes | Code changed |
| Regression test works | Fails without the fix and passes with it | Passes once |
| Safety invariant protected | A mutation makes the test fail | The test exists |
| Within scope | Diff touches only approved, classified paths; baseline hashes unchanged | "I only edited X" |
| Materialized as reviewed | Every changed file matches the reviewed hashes byte for byte | The file looks the same |
| Subagent finding | Checked against primary evidence | The subagent report |
| Complete | All of the above, plus explicit final Claude review of the same hashes and recorded provenance | Verification alone |

## Warning signs (guidance)

Optional self-check. These may indicate an unsupported claim: "should", "probably" or "seems to"; satisfaction expressed before evidence; trusting a success report from an agent; a partial run presented as complete; evidence from a different state than the candidate.
