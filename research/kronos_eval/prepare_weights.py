from __future__ import annotations

import hashlib
import json
from pathlib import Path

from huggingface_hub import snapshot_download

from crypto_trader.research.kronos.pins import (
    KRONOS_MODEL_ID,
    KRONOS_MODEL_REVISION,
    KRONOS_TOKENIZER_ID,
    KRONOS_TOKENIZER_REVISION,
    pin_manifest,
)

ROOT = Path(__file__).resolve().parent
ARTIFACTS = ROOT / "artifacts"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch(repo_id: str, revision: str, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    snapshot_download(
        repo_id=repo_id,
        revision=revision,
        local_dir=str(destination),
        allow_patterns=["config.json", "model.safetensors"],
    )
    (destination / ".kronos_revision").write_text(revision + "\n", encoding="utf-8")


def main() -> None:
    model_dir = ARTIFACTS / "Kronos-small"
    tokenizer_dir = ARTIFACTS / "Kronos-Tokenizer-base"
    fetch(KRONOS_MODEL_ID, KRONOS_MODEL_REVISION, model_dir)
    fetch(KRONOS_TOKENIZER_ID, KRONOS_TOKENIZER_REVISION, tokenizer_dir)

    manifest = pin_manifest()
    manifest["model_safetensors_sha256"] = sha256(model_dir / "model.safetensors")
    manifest["tokenizer_safetensors_sha256"] = sha256(tokenizer_dir / "model.safetensors")
    (ROOT / "pins.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
