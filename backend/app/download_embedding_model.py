"""Download and verify the local Qwen3 Embedding snapshot used by SnapNote."""

from pathlib import Path

from huggingface_hub import snapshot_download

from .config import EMBEDDING_MODEL_NAME, EMBEDDING_MODEL_PATH


def main() -> None:
    target = Path(EMBEDDING_MODEL_PATH)
    target.mkdir(parents=True, exist_ok=True)
    snapshot_download(
        repo_id=EMBEDDING_MODEL_NAME,
        local_dir=str(target),
    )
    required = ("config.json", "modules.json")
    missing = [name for name in required if not (target / name).is_file()]
    weights = list(target.glob("*.safetensors")) + list(target.glob("*.bin"))
    if missing or not weights:
        details = ", ".join([*missing, *([] if weights else ["模型权重"])])
        raise SystemExit(f"Embedding 模型下载不完整：缺少 {details}")
    print(f"Embedding 模型已下载并校验：{target}")


if __name__ == "__main__":
    main()
