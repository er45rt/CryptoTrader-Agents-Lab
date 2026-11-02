# AP2 P1 isolated repair — review only

Patch: `AP2_P1_REPAIRS_20261102.patch`.

{
  "scope": "AP2 isolated P1 only",
  "source_tree_claim": "b2137aff82e3b7acae4c7529eed416bcc194ece2",
  "no_commit_push_merge_deploy": true,
  "tests": {
    "focused": "156 passed before fourth regression",
    "final_regressions": "4 passed",
    "full_pytest": "4229 passed, 25 skipped, 0 failed, exit 0"
  },
  "patch_sha256": "1b1c27c295fcd3488b445704d9eb5176431e7ad796f9175de9a6220c862331e4",
  "zip_sha256": "84c430955206d41ab09af69343d8f3f7aea510ce9b3179c5ffa56cde9fef2fa8",
  "files": [
    "src/crypto_trader/paper/session.py",
    "src/crypto_trader/crew_paper_bridge.py",
    "src/crypto_trader/llm/__init__.py",
    "src/crypto_trader/llm/schemas.py",
    "src/crypto_trader/tests/test_ap2_p1_regression.py"
  ],
  "note": "Review-only patch. No integration, release, live trading or testnet authorization. Original full manifest and JUnit/logs are in the local ZIP; this is a concise public handoff index."
}

Do not apply to the older default branch. Apply only against matching AP2 source tree in a separate checkout after review. No merge, no deploy, no Testnet/Live orders.
