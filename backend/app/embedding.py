"""Lazy local embedding provider for optional semantic knowledge search."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Sequence

from .config import (
    EMBEDDING_BATCH_SIZE,
    EMBEDDING_DEVICE,
    EMBEDDING_DIMENSIONS,
    EMBEDDING_LOCAL_FILES_ONLY,
    EMBEDDING_MODEL_NAME,
    EMBEDDING_MODEL_PATH,
)

logger = logging.getLogger("snapnote.embedding")
_model = None
_model_lock = asyncio.Lock()
_model_loading = False
_model_error: str | None = None


class EmbeddingUnavailable(RuntimeError):
    pass


def _device() -> str | None:
    if EMBEDDING_DEVICE and EMBEDDING_DEVICE != "auto":
        return EMBEDDING_DEVICE
    return None


def _load_model():
    global _model, _model_error, _model_loading
    if _model is not None:
        return _model
    model_source = Path(EMBEDDING_MODEL_PATH)
    if EMBEDDING_LOCAL_FILES_ONLY and not model_source.is_dir():
        _model_error = f"本地 Embedding 模型不存在：{model_source}"
        raise EmbeddingUnavailable(_model_error)
    _model_loading = True
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as error:
        _model_error = "未安装 sentence-transformers，无法启用本地语义检索"
        _model_loading = False
        raise EmbeddingUnavailable(_model_error) from error
    try:
        _model = SentenceTransformer(
            str(model_source) if EMBEDDING_LOCAL_FILES_ONLY else EMBEDDING_MODEL_NAME,
            device=_device(),
            truncate_dim=EMBEDDING_DIMENSIONS,
            local_files_only=EMBEDDING_LOCAL_FILES_ONLY,
        )
    except Exception as error:
        _model_error = f"Embedding 模型加载失败：{EMBEDDING_MODEL_NAME}"
        raise EmbeddingUnavailable(_model_error) from error
    finally:
        _model_loading = False
    _model_error = None
    logger.info("embedding_model_loaded", extra={
        "model": EMBEDDING_MODEL_NAME,
        "path": str(model_source),
        "local_files_only": EMBEDDING_LOCAL_FILES_ONLY,
    })
    return _model


def embedding_runtime_state() -> dict:
    """Expose safe runtime state for search-mode diagnostics."""
    return {
        "loaded": _model is not None,
        "loading": _model_loading,
        "error": _model_error,
    }


def _encode(texts: Sequence[str], prompt_name: str | None = None) -> list[list[float]]:
    model = _load_model()
    try:
        prompt = {"prompt_name": prompt_name} if prompt_name else {}
        vectors = model.encode(
            list(texts),
            batch_size=EMBEDDING_BATCH_SIZE,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
            **prompt,
        )
        return [vector.astype("float32").tolist() for vector in vectors]
    except Exception as error:
        raise EmbeddingUnavailable("Embedding 文本编码失败") from error


async def embed_texts(
    texts: Sequence[str], *, prompt_name: str | None = None
) -> list[list[float]]:
    if not texts:
        return []
    async with _model_lock:
        return await asyncio.to_thread(_encode, texts, prompt_name)


async def embed_query(query: str) -> list[float]:
    vectors = await embed_texts([query], prompt_name="query")
    return vectors[0]
