# V8 research-only release approval — 2026-10-12

**Verdict: RESEARCH GO only.** This is not approval for Paper deployment, trading, Testnet, Mainnet, OMS or execution.

## Frozen reviewed code

- V8 source commit: `c240d1c32409b4234eabb584c9a67664fb57925f`
- Verified reviewed Git Tree: `f55dc4e813dc388b9f6971dfca013ac86351b4e4`
- Base commit: `d4fde0a8deea09f4b6b364f9defeacd6593aae7a`
- Four individual item commits: V8-01 `b701ac938907a0d9cd83684bb1a88a97a9752aa8`; V8-02 `7b93387289845595bb46438a77e7e4eec050a0e4`; V8-03 `775f69eba6f45a7d12d592eced177fbff3b39115`; V8-04 `c240d1c32409b4234eabb584c9a67664fb57925f`.
- Exact source-tree offline full pytest: **2,717 passed, 22 skipped, 65 warnings, exit 0**; immutable evidence log SHA256 `6ddd1ec2a8f53182058bd0294012d9a1c0d56d04f7065c271a6685184298ee5c`.
- Six original source reviews: GPT-6.1 Sol; Claude Opus 5.5; Claude Sonnet 5.5; Claude Opus 4.6 Thinking; Claude Sonnet 4.6; Gemini 3.1 Pro High. The seventh, DeepSeek Saeed, has a fresh successful read-only assessment with no evidenced blockers.

## Reviewer provenance and arbitration

- Local external evidence directory: `C:\Users\Jamal\Documents\Codex\2026-10-12\referenced-chatgpt-conversation-this-is-an\outputs\v8-evidence\review\`.
- DeepSeek one authorized retry: `07-deepseek-finalretry-20261012-output.txt`, parsed report `07-deepseek-finalretry-20261012-report.txt`, external record `07-deepseek-finalretry-execution-provenance.json`.
- DeepSeek process exit 0; session `session-92131933-6488-4737-978d-83ec9b7a306e`; `turn_end=completed`; one final response; source report SHA256 `ea832cf41fa9c6806648313d27a0360758b9a25b7dd9c6cb206423e108edb822`.
- `deepseek-v4-pro` was **configured** by a read-only patch. No claim of independently attested serving-model identity or separate Thinking mode.
- Final independent Sol arbitration after external provenance: `08-adjudication-provenance-reconcile-result.txt`: **GO research**, **0 verified blockers**, seven source-pinned reviews accepted; earlier provisional NO-GO records retained as historical evidence.
- Preserve the existing nonblocking v9 backlog, including direct-constructor aliasing boundary, UTC9 replay advisory, Unicode runtime pin, custody limits, Windows symlink/live opt-in skips and warnings.

This approval records the *reviewed code tree* above, not the unrelated DerivArchive research additions in the later approval-commit tree. No source code was rewritten for this approval. Protected WIP `src/crypto_trader/execution/binance_testnet/endpoints.py` remains out of scope and uncommitted. No push/merge/deploy/trading is authorized.
