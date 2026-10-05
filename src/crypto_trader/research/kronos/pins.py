"""Immutable source/model pins for the Kronos evaluation."""

from __future__ import annotations

KRONOS_CODE_REPOSITORY = "https://github.com/shiyu-coder/Kronos.git"
# Latest upstream code commit selected before the OOS target begins (2025-10-01).
KRONOS_CODE_COMMIT = "083294bd84b901f4f3ced219937c00a4fa61a6d4"
KRONOS_CODE_COMMIT_DATE_UTC = "2025-09-16T02:36:13Z"

KRONOS_MODEL_ID = "NeoQuasar/Kronos-small"
KRONOS_MODEL_REVISION = "901c26c1332695a2a8f243eb2f37243a37bea320"
KRONOS_MODEL_REVISION_DATE_UTC = "2025-09-09T14:10:26Z"

KRONOS_TOKENIZER_ID = "NeoQuasar/Kronos-Tokenizer-base"
KRONOS_TOKENIZER_REVISION = "0e0117387f39004a9016484a186a908917e22426"
KRONOS_TOKENIZER_REVISION_DATE_UTC = "2025-09-09T14:10:02Z"

KRONOS_MAX_CONTEXT = 512
KRONOS_MIN_SAMPLE_COUNT = 20

KRONOS_MODEL_VERSION = (
    f"kronos-small@{KRONOS_MODEL_REVISION[:12]}"
    f"+tokenizer-base@{KRONOS_TOKENIZER_REVISION[:12]}"
    f"+code@{KRONOS_CODE_COMMIT[:12]}"
)


def pin_manifest() -> dict[str, object]:
    return {
        "code_repository": KRONOS_CODE_REPOSITORY,
        "code_commit": KRONOS_CODE_COMMIT,
        "code_commit_date_utc": KRONOS_CODE_COMMIT_DATE_UTC,
        "model_id": KRONOS_MODEL_ID,
        "model_revision": KRONOS_MODEL_REVISION,
        "model_revision_date_utc": KRONOS_MODEL_REVISION_DATE_UTC,
        "tokenizer_id": KRONOS_TOKENIZER_ID,
        "tokenizer_revision": KRONOS_TOKENIZER_REVISION,
        "tokenizer_revision_date_utc": KRONOS_TOKENIZER_REVISION_DATE_UTC,
        "max_context": KRONOS_MAX_CONTEXT,
        "minimum_sample_count": KRONOS_MIN_SAMPLE_COUNT,
        "model_version": KRONOS_MODEL_VERSION,
    }
