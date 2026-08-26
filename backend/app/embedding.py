"""Lazy local embedding provider for optional semantic knowledge search."""

from __future__ import annotations

import asyncio
import logging
from typing import Sequence

from .config import (
    EMBEDDING_BATCH_SIZE,
    EMBEDDING_DEVICE,
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MODEL_NAME,
)

logger = logging.getLogger("snapnote.embedding")
_model = None
_model_lock = asyncio.Lock()


class EmbeddingUnavailable(RuntimeError):
    pass


def _device() -> str | None:
    if EMBEDDING_DEVICE and EMBEDDING_DEVICE != "auto":
        return EMBEDDING_DEVICE
    return None


def _load_model():
    global _model
    if _model is not None:
        return _model
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as error:
        raise EmbeddingUnavailable(
            "未安装 sentence-transformers，无法启用本地语义检索"
        ) from error
    try:
        _model = SentenceTransformer(
            EMBEDDING_MODEL_NAME,
            device=_device(),
            truncate_dim=EMBEDDING_DIMENSIONS,
        )
    except Exception as error:
        raise EmbeddingUnavailable(
            f"Embedding 模型加载失败：{EMBEDDING_MODEL_NAME}"
        ) from error
    logger.info("embedding_model_loaded", extra={"model": EMBEDDING_MODEL_NAME})
    return _model


def _encode(texts: Sequence[str]) -> list[list[float]]:
    model = _load_model()
    try:
        vectors = model.encode(
            list(texts),
            batch_size=EMBEDDING_BATCH_SIZE,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return [vector.astype("float32").tolist() for vector in vectors]
    except Exception as error:
        raise EmbeddingUnavailable("Embedding 文本编码失败") from error


async def embed_texts(texts: Sequence[str]) -> list[list[float]]:
    if not texts:
        return []
    async with _model_lock:
        return await asyncio.to_thread(_encode, texts)


async def embed_query(query: str) -> list[float]:
    vectors = await embed_texts([query])
    return vectors[0]
