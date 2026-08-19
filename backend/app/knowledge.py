"""Local, rebuildable knowledge assets derived from completed SnapNote tasks.

This module deliberately has no model, ASR, OCR, or network dependencies.  It is
the domain boundary used by HTTP routes, automatic builds, and the backfill CLI.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
import time
import unicodedata
import uuid
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote

from sqlalchemy import and_, delete, func, or_, select

from .config import KNOWLEDGE_OWNER_SCOPE, TASKS_DIR
from .database import (
    KnowledgeAsset,
    KnowledgeAssetVersion,
    KnowledgeBuildRun,
    KnowledgeChapter,
    KnowledgeChunk,
    KnowledgeMedia,
    KnowledgeTranscriptSegment,
    SnapTask,
    async_session,
    utcnow,
)


SCHEMA_VERSION = "1"
BUILDER_VERSION = "1.0.0"
CONTENT_TYPES = {"video_summary", "chapter_summary", "transcript", "note"}
SEARCHABLE_STATUSES = {"ready", "degraded"}
MAX_TEXT = 12_000
_NAMESPACE = uuid.UUID("449c774f-f3fc-47cf-9b44-dbc45bbf5c54")
_build_locks: dict[str, asyncio.Lock] = {}
logger = logging.getLogger("snapnote.knowledge")


class KnowledgeError(ValueError):
    def __init__(self, code: str, summary: str):
        super().__init__(summary)
        self.code = code
        self.summary = summary[:500]


def _stable_id(kind: str, key: str) -> str:
    return str(uuid.uuid5(_NAMESPACE, f"{kind}:{key}"))


def _normalized_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()


def _searchable_text(value: Any) -> str:
    text = _normalized_text(value)
    return text if re.search(r"[\w\u3400-\u9fff]", text, re.UNICODE) else ""


def _hash_text(value: Any) -> str:
    return hashlib.sha256(_normalized_text(value).encode("utf-8")).hexdigest()


def _canonical_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _like_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped.lower()}%"


def _load_json(raw: str | None, expected: type) -> tuple[Any, bool]:
    try:
        value = json.loads(raw or ("{}" if expected is dict else "[]"))
    except (json.JSONDecodeError, TypeError):
        return expected(), False
    return (value, True) if isinstance(value, expected) else (expected(), False)


def _number(value: Any, default: float = 0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _time_range(start: Any, end: Any, duration: float) -> tuple[float, float] | None:
    start_value = min(max(0.0, _number(start)), max(0.0, duration))
    end_value = min(max(0.0, _number(end)), max(0.0, duration))
    if end_value <= start_value:
        end_value = min(duration, start_value + 1.0)
    if end_value <= start_value:
        return None
    return round(start_value, 3), round(end_value, 3)


def _source_hash(task: SnapTask) -> str:
    frames, _ = _load_json(task.frames_json, list)
    transcripts, _ = _load_json(task.transcripts_json, list)
    notes, _ = _load_json(task.notes_json, list)
    visual, _ = _load_json(task.visual_analysis_json, dict)
    return _canonical_hash({
        "task_id": task.id,
        "filename": task.filename,
        "duration": round(_number(task.duration), 3),
        "frames": frames,
        "transcripts": transcripts,
        "notes": notes,
        "visual_analysis": visual,
        "schema_version": SCHEMA_VERSION,
        "chunking_version": BUILDER_VERSION,
    })


def _safe_title(task: SnapTask) -> str:
    return (_normalized_text(Path(task.filename or "视频资产").stem) or "视频资产")[:500]


def _valid_source_video(task: SnapTask) -> bool:
    try:
        path = Path(task.video_path).resolve()
        task_root = (TASKS_DIR / task.id).resolve()
        return path.is_file() and (path == task_root or task_root in path.parents)
    except (OSError, RuntimeError):
        return False


def _summary(visual: dict, notes: list[dict], markdown: str) -> str:
    direct = _searchable_text(visual.get("summary"))
    if direct:
        return direct[:MAX_TEXT]
    for line in (markdown or "").splitlines():
        candidate = _searchable_text(line.lstrip("#*- "))
        if len(candidate) >= 20:
            return candidate[:MAX_TEXT]
    candidates = [_searchable_text(row.get("summary")) for row in notes[:5]]
    return " ".join(value for value in candidates if value)[:MAX_TEXT]


def _chapters(
    visual: dict, notes: list[dict], transcripts: list[dict], duration: float
) -> tuple[list[dict], str | None]:
    raw = visual.get("narrative_structure") or visual.get("structure")
    source_type = "visual_structure"
    if not isinstance(raw, list) or not raw:
        raw = notes
        source_type = "note_fallback"
    if (not isinstance(raw, list) or not raw) and transcripts:
        raw = [{
            "stage": "完整内容",
            "description": "由现有原文形成的降级章节",
            "start_time": 0,
            "end_time": duration,
        }]
        source_type = "transcript_fallback"

    result: list[dict] = []
    for row in raw if isinstance(raw, list) else []:
        if not isinstance(row, dict):
            continue
        start = row.get("start_time", row.get("timestamp", 0))
        end = row.get("end_time", row.get("end", duration))
        bounds = _time_range(start, end, duration)
        if not bounds:
            continue
        result.append({
            "title": (_searchable_text(row.get("stage") or row.get("title")) or "未命名章节")[:500],
            "summary": _searchable_text(row.get("description") or row.get("summary"))[:MAX_TEXT],
            "start_time": bounds[0],
            "end_time": bounds[1],
            "source_type": source_type,
        })
    result.sort(key=lambda row: (row["start_time"], row["end_time"], row["title"]))
    return result, source_type if result else None


def _transcript_segments(raw: list[dict], duration: float) -> list[dict]:
    result = []
    for source_index, row in enumerate(raw):
        if not isinstance(row, dict):
            continue
        text = _searchable_text(row.get("text"))
        bounds = _time_range(row.get("start"), row.get("end"), duration)
        if not text or not bounds:
            continue
        result.append({
            "source_segment_index": source_index,
            "start_time": bounds[0],
            "end_time": bounds[1],
            "text": text[:MAX_TEXT],
            "content_hash": _hash_text(text),
        })
    return sorted(result, key=lambda row: (row["start_time"], row["source_segment_index"]))


def _chapter_for_time(chapters: list[dict], timestamp: float) -> int | None:
    for index, chapter in enumerate(chapters):
        if chapter["start_time"] <= timestamp < chapter["end_time"]:
            return index
    if not chapters:
        return None
    return min(range(len(chapters)), key=lambda i: abs(chapters[i]["start_time"] - timestamp))


def _transcript_chunks(segments: list[dict], chapters: list[dict]) -> list[dict]:
    grouped: dict[int, list[dict]] = defaultdict(list)
    for segment in segments:
        chapter_index = _chapter_for_time(chapters, segment["start_time"])
        if chapter_index is not None:
            grouped[chapter_index].append(segment)
    chunks = []
    for chapter_index in sorted(grouped):
        pending: list[dict] = []
        length = 0
        for segment in grouped[chapter_index]:
            would_span = pending and segment["end_time"] - pending[0]["start_time"] > 90
            would_overflow = pending and length + len(segment["text"]) > 800
            if would_span or would_overflow:
                chunks.append(_merged_transcript(pending, chapter_index))
                pending, length = [], 0
            pending.append(segment)
            length += len(segment["text"])
            if length >= 300:
                chunks.append(_merged_transcript(pending, chapter_index))
                pending, length = [], 0
        if pending:
            chunks.append(_merged_transcript(pending, chapter_index))
    return chunks


def _merged_transcript(rows: list[dict], chapter_index: int) -> dict:
    text = " ".join(row["text"] for row in rows)
    return {
        "content_type": "transcript",
        "chapter_index": chapter_index,
        "source_ref": "transcript:" + ",".join(str(row["source_segment_index"]) for row in rows),
        "title": "",
        "text": text,
        "start_time": rows[0]["start_time"],
        "end_time": rows[-1]["end_time"],
    }


def _relative_media_uri(task_id: str, raw: Any) -> str | None:
    value = unquote(str(raw or "")).replace("\\", "/").strip()
    expected = f"/storage/tasks/{task_id}/"
    if not value.startswith(expected):
        return None
    path = PurePosixPath(value)
    if ".." in path.parts:
        return None
    return "/" + str(path).lstrip("/")


def _prepare(task: SnapTask) -> dict:
    if task.status != "completed":
        raise KnowledgeError("TASK_NOT_COMPLETED", "源任务尚未完成")
    if not _valid_source_video(task):
        raise KnowledgeError("SOURCE_VIDEO_UNAVAILABLE", "原视频引用无效或文件不可用")
    duration = _number(task.duration)
    if duration <= 0:
        raise KnowledgeError("INVALID_DURATION", "视频时长无效")

    frames, frames_ok = _load_json(task.frames_json, list)
    raw_transcripts, transcripts_ok = _load_json(task.transcripts_json, list)
    notes, notes_ok = _load_json(task.notes_json, list)
    visual, visual_ok = _load_json(task.visual_analysis_json, dict)
    missing = []
    for label, valid in (
        ("画面数据无法解析", frames_ok), ("原文数据无法解析", transcripts_ok),
        ("笔记数据无法解析", notes_ok), ("章节数据无法解析", visual_ok),
    ):
        if not valid:
            missing.append(label)

    transcripts = _transcript_segments(raw_transcripts, duration)
    chapters, chapter_source = _chapters(visual, notes, raw_transcripts, duration)
    summary = _summary(visual, notes, task.final_markdown)
    chunks: list[dict] = []
    if summary:
        chunks.append({
            "content_type": "video_summary", "chapter_index": None,
            "source_ref": "visual_analysis:summary", "title": _safe_title(task),
            "text": summary, "start_time": None, "end_time": None,
        })
    for index, chapter in enumerate(chapters):
        if chapter["summary"]:
            chunks.append({
                "content_type": "chapter_summary", "chapter_index": index,
                "source_ref": f"chapter:{index}", "title": chapter["title"],
                "text": chapter["summary"], "start_time": chapter["start_time"],
                "end_time": chapter["end_time"],
            })
    for note_index, note in enumerate(notes):
        if not isinstance(note, dict):
            continue
        title = _searchable_text(note.get("title"))
        body = _searchable_text(note.get("summary"))
        points = note.get("key_points")
        if isinstance(points, list):
            body = " ".join(filter(None, [body, *(_searchable_text(v) for v in points)]))
        text_value = _searchable_text(" ".join(filter(None, [title, body])))
        bounds = _time_range(note.get("timestamp"), note.get("end_time"), duration)
        if not text_value or not bounds:
            continue
        chunks.append({
            "content_type": "note",
            "chapter_index": _chapter_for_time(chapters, bounds[0]),
            "source_ref": f"note:{note.get('id', note_index)}", "title": title,
            "text": text_value[:MAX_TEXT], "start_time": bounds[0], "end_time": bounds[1],
        })
    chunks.extend(_transcript_chunks(transcripts, chapters))

    deduplicated = []
    hashes = set()
    for chunk in chunks:
        normalized = _searchable_text(chunk["text"])
        content_hash = _hash_text(normalized)
        if not normalized or content_hash in hashes:
            continue
        hashes.add(content_hash)
        chunk["text"] = normalized[:MAX_TEXT]
        chunk["content_hash"] = content_hash
        deduplicated.append(chunk)

    media = []
    for index, frame in enumerate(frames):
        if not isinstance(frame, dict):
            continue
        uri = _relative_media_uri(task.id, frame.get("image_url"))
        timestamp = min(max(0.0, _number(frame.get("timestamp"))), duration)
        if not uri:
            continue
        media_relative = uri.removeprefix(f"/storage/tasks/{task.id}/")
        file_path = TASKS_DIR / task.id / media_relative
        media.append({
            "frame_id": str(frame.get("id") or f"frame-{index}"),
            "relative_uri": uri,
            "timestamp": round(timestamp, 3),
            "content_hash": _canonical_hash({"uri": uri, "timestamp": timestamp}),
            "availability": "available" if file_path.is_file() else "missing",
        })

    if not chapters:
        missing.append("缺少章节")
    elif chapter_source != "visual_structure":
        missing.append("章节使用降级来源")
    if not transcripts:
        missing.append("缺少原文")
    if not media or not any(row["availability"] == "available" for row in media):
        missing.append("缺少画面引用")
    if not deduplicated:
        raise KnowledgeError("NO_SEARCHABLE_CONTENT", "没有可形成检索证据的文本内容")
    if not any(chunk["source_ref"] for chunk in deduplicated):
        raise KnowledgeError("MISSING_SOURCE_REFERENCE", "知识内容缺少可追溯来源")

    return {
        "title": _safe_title(task), "duration": duration, "summary": summary,
        "chapters": chapters, "transcripts": transcripts, "chunks": deduplicated,
        "media": media, "missing": sorted(set(missing)),
        "status": "degraded" if missing else "ready",
    }


async def _get_or_create_asset(db, task: SnapTask) -> KnowledgeAsset:
    asset = (await db.execute(
        select(KnowledgeAsset).where(KnowledgeAsset.task_id == task.id)
    )).scalar_one_or_none()
    if asset:
        return asset
    asset = KnowledgeAsset(
        id=_stable_id("asset", task.id), task_id=task.id, owner_scope=KNOWLEDGE_OWNER_SCOPE,
        title=_safe_title(task), status="not_built",
    )
    db.add(asset)
    await db.flush()
    return asset


async def build_knowledge_asset(
    task_id: str, *, trigger: str = "automatic", force: bool = False
) -> dict:
    lock = _build_locks.setdefault(task_id, asyncio.Lock())
    if lock.locked():
        return {"task_id": task_id, "status": "building", "skipped": True}
    async with lock:
        logger.info("knowledge_build_started", extra={
            "task_id": task_id, "trigger": trigger, "builder_version": BUILDER_VERSION,
        })
        run_id = str(uuid.uuid4())
        prior_status = "not_built"
        prior_version_id = None
        asset_id = _stable_id("asset", task_id)
        source_hash = ""
        effective_builder = BUILDER_VERSION
        try:
            async with async_session() as db:
                task = await db.get(SnapTask, task_id)
                if not task:
                    raise KnowledgeError("TASK_NOT_FOUND", "源任务不存在")
                asset = await _get_or_create_asset(db, task)
                asset_id = asset.id
                running = (await db.execute(select(KnowledgeBuildRun).where(
                    KnowledgeBuildRun.asset_id == asset.id,
                    KnowledgeBuildRun.status == "running",
                ).order_by(KnowledgeBuildRun.started_at.desc()).limit(1))).scalar_one_or_none()
                if running:
                    now = utcnow()
                    started_at = running.started_at
                    comparable_now = now if started_at.tzinfo else now.replace(tzinfo=None)
                    if (comparable_now - started_at).total_seconds() < 600:
                        return {
                            "asset_id": asset.id, "asset_version_id": asset.current_version_id,
                            "status": "building", "skipped": True,
                        }
                    running.status = "failed"
                    running.error_code = "STALE_BUILD_RECOVERED"
                    running.error_summary = "上次构建意外中断，已允许重新构建"
                    running.completed_at = now
                    previous = (await db.execute(select(KnowledgeBuildRun.status).where(
                        KnowledgeBuildRun.asset_id == asset.id,
                        KnowledgeBuildRun.status.in_(["ready", "degraded"]),
                    ).order_by(KnowledgeBuildRun.completed_at.desc()).limit(1))).scalar_one_or_none()
                    asset.status = previous if asset.current_version_id and previous else "not_built"
                    await db.flush()
                prior_status, prior_version_id = asset.status, asset.current_version_id
                source_hash = _source_hash(task)
                if not force:
                    current = await db.get(KnowledgeAssetVersion, asset.current_version_id) if asset.current_version_id else None
                    if (
                        current and current.source_hash == source_hash
                        and current.schema_version == SCHEMA_VERSION
                        and current.builder_version.startswith(BUILDER_VERSION)
                        and asset.status in SEARCHABLE_STATUSES
                    ):
                        run = KnowledgeBuildRun(
                            id=run_id, asset_id=asset.id, trigger=trigger, status="skipped",
                            source_hash=source_hash, builder_version=BUILDER_VERSION,
                            stats_json=json.dumps({"reason": "unchanged"}), completed_at=utcnow(),
                        )
                        db.add(run)
                        await db.commit()
                        return {
                            "asset_id": asset.id, "asset_version_id": current.id,
                            "status": asset.status, "skipped": True,
                        }
                    existing = (await db.execute(select(KnowledgeAssetVersion).where(
                        KnowledgeAssetVersion.asset_id == asset.id,
                        KnowledgeAssetVersion.source_hash == source_hash,
                        KnowledgeAssetVersion.schema_version == SCHEMA_VERSION,
                        KnowledgeAssetVersion.builder_version == BUILDER_VERSION,
                    ))).scalar_one_or_none()
                    if existing and asset.current_version_id == existing.id and asset.status in SEARCHABLE_STATUSES:
                        run = KnowledgeBuildRun(
                            id=run_id, asset_id=asset.id, trigger=trigger, status="skipped",
                            source_hash=source_hash, builder_version=BUILDER_VERSION,
                            stats_json=json.dumps({"reason": "unchanged"}), completed_at=utcnow(),
                        )
                        db.add(run)
                        await db.commit()
                        return {
                            "asset_id": asset.id, "asset_version_id": existing.id,
                            "status": asset.status, "skipped": True,
                        }
                    if existing:
                        chunk_count = (await db.execute(select(func.count()).select_from(
                            KnowledgeChunk
                        ).where(KnowledgeChunk.asset_version_id == existing.id))).scalar_one()
                        if chunk_count:
                            chapter_count = (await db.execute(select(func.count()).select_from(
                                KnowledgeChapter
                            ).where(KnowledgeChapter.asset_version_id == existing.id))).scalar_one()
                            transcript_count = (await db.execute(select(func.count()).select_from(
                                KnowledgeTranscriptSegment
                            ).where(KnowledgeTranscriptSegment.asset_version_id == existing.id))).scalar_one()
                            media_count = (await db.execute(select(func.count()).select_from(
                                KnowledgeMedia
                            ).where(
                                KnowledgeMedia.asset_version_id == existing.id,
                                KnowledgeMedia.availability == "available",
                            ))).scalar_one()
                            missing = []
                            if not chapter_count:
                                missing.append("缺少章节")
                            if not transcript_count:
                                missing.append("缺少原文")
                            if not media_count:
                                missing.append("缺少画面引用")
                            asset.current_version_id = existing.id
                            asset.status = "degraded" if missing else "ready"
                            asset.title = _safe_title(task)
                            asset.missing_items_json = json.dumps(missing, ensure_ascii=False)
                            asset.error_summary = None
                            asset.updated_at = utcnow()
                            db.add(KnowledgeBuildRun(
                                id=run_id, asset_id=asset.id, trigger=trigger, status="skipped",
                                source_hash=source_hash, builder_version=BUILDER_VERSION,
                                stats_json=json.dumps({"reason": "reused_immutable_version"}),
                                completed_at=utcnow(),
                            ))
                            await db.commit()
                            return {
                                "asset_id": asset.id, "asset_version_id": existing.id,
                                "status": asset.status, "skipped": True,
                            }
                        await db.delete(existing)
                        await db.flush()
                if force:
                    effective_builder = f"{BUILDER_VERSION}+rebuild.{run_id[:8]}"
                asset.status = "building"
                asset.error_summary = None
                asset.updated_at = utcnow()
                db.add(KnowledgeBuildRun(
                    id=run_id, asset_id=asset.id, trigger=trigger, status="running",
                    source_hash=source_hash, builder_version=effective_builder,
                ))
                await db.commit()

            prepared = _prepare(task)
            version_id = _stable_id("version", f"{asset_id}:{source_hash}:{effective_builder}")
            async with async_session() as db:
                asset = await db.get(KnowledgeAsset, asset_id)
                run = await db.get(KnowledgeBuildRun, run_id)
                if not asset or asset.status == "deleting":
                    raise KnowledgeError("ASSET_DELETING", "知识资产正在删除")
                version = KnowledgeAssetVersion(
                    id=version_id, asset_id=asset.id, source_hash=source_hash,
                    schema_version=SCHEMA_VERSION, builder_version=effective_builder,
                    summary=prepared["summary"], duration=prepared["duration"], language="zh",
                )
                db.add(version)
                await db.flush()

                chapter_ids = []
                for ordinal, row in enumerate(prepared["chapters"]):
                    chapter_id = _stable_id("chapter", f"{version_id}:{ordinal}")
                    chapter_ids.append(chapter_id)
                    db.add(KnowledgeChapter(
                        id=chapter_id, asset_version_id=version_id, ordinal=ordinal, **row
                    ))
                for row in prepared["transcripts"]:
                    db.add(KnowledgeTranscriptSegment(
                        id=_stable_id("transcript", f"{version_id}:{row['source_segment_index']}"),
                        asset_version_id=version_id, **row,
                    ))
                for ordinal, row in enumerate(prepared["chunks"]):
                    chapter_index = row.pop("chapter_index")
                    db.add(KnowledgeChunk(
                        id=_stable_id("chunk", f"{version_id}:{row['content_type']}:{row['content_hash']}"),
                        asset_version_id=version_id,
                        chapter_id=chapter_ids[chapter_index] if chapter_index is not None else None,
                        **row,
                    ))
                for row in prepared["media"]:
                    db.add(KnowledgeMedia(
                        id=_stable_id("media", f"{version_id}:{row['frame_id']}"),
                        asset_version_id=version_id, media_type="keyframe", **row,
                    ))
                await db.flush()
                asset.current_version_id = version_id
                asset.status = prepared["status"]
                asset.title = prepared["title"]
                asset.missing_items_json = json.dumps(prepared["missing"], ensure_ascii=False)
                asset.error_summary = None
                asset.updated_at = utcnow()
                run.status = prepared["status"]
                run.stats_json = json.dumps({
                    "chapters": len(prepared["chapters"]),
                    "transcript_segments": len(prepared["transcripts"]),
                    "chunks": len(prepared["chunks"]), "media": len(prepared["media"]),
                    "missing": prepared["missing"],
                }, ensure_ascii=False)
                run.completed_at = utcnow()
                await db.commit()
            logger.info("knowledge_build_completed", extra={
                "task_id": task_id, "status": prepared["status"],
                "chapter_count": len(prepared["chapters"]),
                "chunk_count": len(prepared["chunks"]),
                "media_count": len(prepared["media"]),
            })
            return {
                "asset_id": asset_id, "asset_version_id": version_id,
                "status": prepared["status"], "missing_items": prepared["missing"],
                "skipped": False,
            }
        except Exception as error:
            code = error.code if isinstance(error, KnowledgeError) else "BUILD_FAILED"
            summary = error.summary if isinstance(error, KnowledgeError) else "知识资产构建时发生内部错误"
            async with async_session() as db:
                asset = await db.get(KnowledgeAsset, asset_id)
                if asset:
                    asset.status = prior_status if prior_version_id else "failed"
                    asset.current_version_id = prior_version_id
                    asset.error_summary = summary
                    asset.updated_at = utcnow()
                run = await db.get(KnowledgeBuildRun, run_id)
                if run:
                    run.status = "failed"
                    run.error_code = code
                    run.error_summary = summary
                    run.completed_at = utcnow()
                await db.commit()
            logger.warning("knowledge_build_failed", extra={
                "task_id": task_id, "error_code": code, "trigger": trigger,
            })
            return {"asset_id": asset_id, "status": "failed", "error_code": code, "error_summary": summary}
        finally:
            _build_locks.pop(task_id, None)


def _asset_status_payload(asset: KnowledgeAsset | None) -> dict:
    if not asset:
        return {
            "asset_id": None, "status": "not_built", "current_version_id": None,
            "missing_items": [], "error_summary": None,
        }
    try:
        missing = json.loads(asset.missing_items_json or "[]")
    except json.JSONDecodeError:
        missing = []
    return {
        "asset_id": asset.id, "status": asset.status,
        "current_version_id": asset.current_version_id,
        "missing_items": missing if isinstance(missing, list) else [],
        "error_summary": asset.error_summary, "updated_at": asset.updated_at,
    }


async def get_task_knowledge_status(task_id: str) -> dict:
    async with async_session() as db:
        asset = (await db.execute(select(KnowledgeAsset).where(
            KnowledgeAsset.task_id == task_id
        ))).scalar_one_or_none()
        return _asset_status_payload(asset)


async def list_assets(
    *, owner_scope: str = KNOWLEDGE_OWNER_SCOPE, status: str | None = None,
    title_query: str | None = None, limit: int = 50, offset: int = 0,
) -> dict:
    limit = min(max(limit, 1), 100)
    offset = max(offset, 0)
    filters = [KnowledgeAsset.owner_scope == owner_scope]
    if status:
        filters.append(KnowledgeAsset.status == status)
    if title_query:
        filters.append(func.lower(KnowledgeAsset.title).like(f"%{title_query.strip().lower()}%"))
    async with async_session() as db:
        total = (await db.execute(select(func.count()).select_from(KnowledgeAsset).where(*filters))).scalar_one()
        rows = (await db.execute(select(KnowledgeAsset).where(*filters).order_by(
            KnowledgeAsset.updated_at.desc(), KnowledgeAsset.id.asc()
        ).limit(limit).offset(offset))).scalars().all()
        return {
            "items": [{**_asset_status_payload(row), "task_id": row.task_id, "title": row.title,
                       "created_at": row.created_at} for row in rows],
            "total": total, "limit": limit, "offset": offset,
        }


async def get_asset(asset_id: str, owner_scope: str = KNOWLEDGE_OWNER_SCOPE) -> dict | None:
    async with async_session() as db:
        asset = await db.get(KnowledgeAsset, asset_id)
        if not asset or asset.owner_scope != owner_scope or asset.status in {"deleting", "deleted"}:
            return None
        version = await db.get(KnowledgeAssetVersion, asset.current_version_id) if asset.current_version_id else None
        chapters = []
        if version:
            chapters = (await db.execute(select(KnowledgeChapter).where(
                KnowledgeChapter.asset_version_id == version.id
            ).order_by(KnowledgeChapter.ordinal))).scalars().all()
        return {
            **_asset_status_payload(asset), "task_id": asset.task_id, "title": asset.title,
            "summary": version.summary if version else "", "duration": version.duration if version else None,
            "schema_version": version.schema_version if version else None,
            "builder_version": version.builder_version if version else None,
            "chapters": [{
                "id": row.id, "ordinal": row.ordinal, "title": row.title,
                "summary": row.summary, "start_time": row.start_time,
                "end_time": row.end_time, "source_type": row.source_type,
            } for row in chapters],
        }


async def get_chapter(chapter_id: str, owner_scope: str = KNOWLEDGE_OWNER_SCOPE) -> dict | None:
    async with async_session() as db:
        row = (await db.execute(select(KnowledgeChapter, KnowledgeAssetVersion, KnowledgeAsset).join(
            KnowledgeAssetVersion, KnowledgeAssetVersion.id == KnowledgeChapter.asset_version_id
        ).join(KnowledgeAsset, and_(
            KnowledgeAsset.id == KnowledgeAssetVersion.asset_id,
            KnowledgeAsset.current_version_id == KnowledgeAssetVersion.id,
        )).where(
            KnowledgeChapter.id == chapter_id, KnowledgeAsset.owner_scope == owner_scope,
            KnowledgeAsset.status.in_(SEARCHABLE_STATUSES),
        ))).first()
        if not row:
            return None
        chapter, version, asset = row
        media = (await db.execute(select(KnowledgeMedia).where(
            KnowledgeMedia.asset_version_id == version.id,
            KnowledgeMedia.timestamp >= chapter.start_time,
            KnowledgeMedia.timestamp <= chapter.end_time,
        ).order_by(KnowledgeMedia.timestamp))).scalars().all()
        return {
            "id": chapter.id, "asset_id": asset.id, "asset_version_id": version.id,
            "title": chapter.title, "summary": chapter.summary,
            "start_time": chapter.start_time, "end_time": chapter.end_time,
            "media": [_media_payload(item) for item in media],
        }


async def get_transcript(
    asset_id: str, start_time: float = 0, end_time: float | None = None,
    owner_scope: str = KNOWLEDGE_OWNER_SCOPE,
) -> dict | None:
    async with async_session() as db:
        asset = await db.get(KnowledgeAsset, asset_id)
        if not asset or asset.owner_scope != owner_scope or asset.status not in SEARCHABLE_STATUSES or not asset.current_version_id:
            return None
        version = await db.get(KnowledgeAssetVersion, asset.current_version_id)
        upper = version.duration if end_time is None else min(end_time, version.duration)
        if start_time < 0 or start_time >= upper:
            raise KnowledgeError("INVALID_TIME_RANGE", "时间范围必须满足 0 ≤ start < end")
        rows = (await db.execute(select(KnowledgeTranscriptSegment).where(
            KnowledgeTranscriptSegment.asset_version_id == version.id,
            KnowledgeTranscriptSegment.end_time > start_time,
            KnowledgeTranscriptSegment.start_time < upper,
        ).order_by(KnowledgeTranscriptSegment.start_time))).scalars().all()
        return {
            "asset_id": asset.id, "asset_version_id": version.id,
            "start_time": start_time, "end_time": upper,
            "segments": [{
                "id": row.id, "source_segment_index": row.source_segment_index,
                "start_time": row.start_time, "end_time": row.end_time, "text": row.text,
            } for row in rows],
        }


def _media_payload(row: KnowledgeMedia | None) -> dict | None:
    if not row:
        return None
    return {
        "id": row.id, "frame_id": row.frame_id, "media_type": row.media_type,
        "relative_uri": row.relative_uri, "timestamp": row.timestamp,
        "availability": row.availability,
    }


async def get_keyframe(media_id: str, owner_scope: str = KNOWLEDGE_OWNER_SCOPE) -> dict | None:
    async with async_session() as db:
        row = (await db.execute(select(KnowledgeMedia, KnowledgeAssetVersion, KnowledgeAsset).join(
            KnowledgeAssetVersion, KnowledgeAssetVersion.id == KnowledgeMedia.asset_version_id
        ).join(KnowledgeAsset, and_(
            KnowledgeAsset.id == KnowledgeAssetVersion.asset_id,
            KnowledgeAsset.current_version_id == KnowledgeAssetVersion.id,
        )).where(
            KnowledgeMedia.id == media_id, KnowledgeAsset.owner_scope == owner_scope,
            KnowledgeAsset.status.in_(SEARCHABLE_STATUSES),
        ))).first()
        return _media_payload(row[0]) if row else None


async def search_knowledge(
    query: str, *, asset_ids: list[str] | None = None,
    content_types: list[str] | None = None, top_k: int = 8,
    owner_scope: str = KNOWLEDGE_OWNER_SCOPE,
) -> dict:
    started = time.perf_counter()
    query = _normalized_text(query)
    if not 1 <= len(query) <= 500:
        raise KnowledgeError("INVALID_QUERY", "搜索词长度必须为 1–500 个字符")
    if not 1 <= top_k <= 20:
        raise KnowledgeError("INVALID_TOP_K", "top_k 必须在 1–20 之间")
    if asset_ids is not None and len(asset_ids) > 100:
        raise KnowledgeError("SCOPE_TOO_LARGE", "单次资产范围最多 100 条")
    types = set(content_types or CONTENT_TYPES)
    if not types or not types.issubset(CONTENT_TYPES):
        raise KnowledgeError("INVALID_CONTENT_TYPE", "内容类型不受支持")

    terms = list(dict.fromkeys([query, *(term for term in query.split(" ") if term)]))
    term_filters = []
    for term in terms:
        pattern = _like_pattern(term)
        term_filters.extend((
            func.lower(KnowledgeChunk.text).like(pattern, escape="\\"),
            func.lower(KnowledgeChunk.title).like(pattern, escape="\\"),
            func.lower(KnowledgeAsset.title).like(pattern, escape="\\"),
            func.lower(KnowledgeChapter.title).like(pattern, escape="\\"),
        ))
    filters = [
        KnowledgeAsset.owner_scope == owner_scope,
        KnowledgeAsset.status.in_(SEARCHABLE_STATUSES),
        KnowledgeAsset.current_version_id == KnowledgeAssetVersion.id,
        KnowledgeChunk.content_type.in_(types),
        or_(*term_filters),
    ]
    if asset_ids is not None:
        filters.append(KnowledgeAsset.id.in_(asset_ids))
    async with async_session() as db:
        available_count = (await db.execute(select(func.count()).select_from(KnowledgeAsset).where(
            KnowledgeAsset.owner_scope == owner_scope,
            KnowledgeAsset.status.in_(SEARCHABLE_STATUSES),
            *([KnowledgeAsset.id.in_(asset_ids)] if asset_ids is not None else []),
        ))).scalar_one()
        rows = (await db.execute(select(
            KnowledgeChunk, KnowledgeAssetVersion, KnowledgeAsset, KnowledgeChapter
        ).join(KnowledgeAssetVersion, KnowledgeAssetVersion.id == KnowledgeChunk.asset_version_id
        ).join(KnowledgeAsset, KnowledgeAsset.id == KnowledgeAssetVersion.asset_id
        ).outerjoin(KnowledgeChapter, KnowledgeChapter.id == KnowledgeChunk.chapter_id
        ).where(*filters).order_by(
            KnowledgeAsset.updated_at.desc(), KnowledgeChapter.start_time.asc(), KnowledgeChunk.id.asc()
        ).limit(1000))).all()
        media_by_version: dict[str, list[KnowledgeMedia]] = defaultdict(list)
        version_ids = {row[1].id for row in rows}
        if version_ids:
            media_rows = (await db.execute(select(KnowledgeMedia).where(
                KnowledgeMedia.asset_version_id.in_(version_ids)
            ))).scalars().all()
            for media_row in media_rows:
                media_by_version[media_row.asset_version_id].append(media_row)
        results = []
        for chunk, version, asset, chapter in rows:
            lowered_terms = [term.lower() for term in terms]
            title_hit = any(term in asset.title.lower() for term in lowered_terms)
            chapter_hit = bool(chapter and any(term in chapter.title.lower() for term in lowered_terms))
            chunk_title_hit = any(term in (chunk.title or "").lower() for term in lowered_terms)
            occurrences = sum(chunk.text.lower().count(term) for term in lowered_terms) + int(title_hit) + int(chapter_hit)
            keyword = min(1.0, 0.55 + 0.15 * max(0, occurrences - 1))
            field_score = 1.0 if title_hit else 0.8 if chapter_hit or chunk_title_hit else 0.4
            timed = chunk.start_time is not None and chunk.end_time is not None
            evidence = 1.0 if timed else 0.6
            media = None
            if timed:
                candidates = [item for item in media_by_version[version.id]
                              if chunk.start_time <= item.timestamp <= chunk.end_time]
                media = min(candidates, key=lambda item: abs(item.timestamp - chunk.start_time)) if candidates else None
                evidence = 1.0 if media else 0.8
            matched_field = "asset_title" if title_hit else "chapter_title" if chapter_hit else "content_title" if chunk_title_hit else "text"
            score = round(0.75 * keyword + 0.15 * field_score + 0.10 * evidence, 6)
            results.append({
                "chunk_id": chunk.id, "asset_id": asset.id,
                "task_id": asset.task_id,
                "asset_version_id": version.id, "asset_title": asset.title,
                "content_type": chunk.content_type,
                "chapter_id": chapter.id if chapter else None,
                "chapter_title": chapter.title if chapter else None,
                "text": chunk.text, "start_time": chunk.start_time, "end_time": chunk.end_time,
                "keyframe": _media_payload(media), "score": score,
                "matched_field": matched_field, "source_status": asset.status,
                "schema_version": version.schema_version,
                "builder_version": version.builder_version,
                "_updated": asset.updated_at, "_chapter_time": chapter.start_time if chapter else float("inf"),
            })
        results.sort(key=lambda row: (
            -row["score"], -row["_updated"].timestamp(), row["_chapter_time"], row["chunk_id"]
        ))
        results = results[:top_k]
        for row in results:
            row.pop("_updated", None)
            row.pop("_chapter_time", None)
        response = {
            "query": query, "scope": {"asset_ids": asset_ids, "owner_scope": owner_scope},
            "results": results, "total": len(results), "available_assets": available_count,
            "degraded_search": False,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        }
        logger.info("knowledge_search_completed", extra={
            "query_length": len(query), "scope_size": len(asset_ids) if asset_ids is not None else available_count,
            "result_count": len(results), "elapsed_ms": response["elapsed_ms"],
        })
        return response


async def remove_knowledge_for_task(task_id: str, db=None) -> bool:
    owns_session = db is None
    if owns_session:
        db = async_session()
    try:
        asset = (await db.execute(select(KnowledgeAsset).where(
            KnowledgeAsset.task_id == task_id
        ))).scalar_one_or_none()
        if not asset:
            return False
        asset.status = "deleting"
        asset.updated_at = utcnow()
        await db.flush()
        await db.execute(delete(KnowledgeAsset).where(KnowledgeAsset.id == asset.id))
        if owns_session:
            await db.commit()
        return True
    finally:
        if owns_session:
            await db.close()


async def backfill_knowledge(
    *, task_ids: list[str] | None = None, dry_run: bool = False,
    retry_failed: bool = False, force: bool = False,
) -> dict:
    started = time.perf_counter()
    async with async_session() as db:
        query = select(SnapTask).where(SnapTask.status == "completed").order_by(SnapTask.created_at)
        if task_ids:
            query = query.where(SnapTask.id.in_(task_ids))
        tasks = (await db.execute(query)).scalars().all()
        if retry_failed:
            failed_ids = set((await db.execute(select(KnowledgeAsset.task_id).where(
                KnowledgeAsset.status.in_({"failed", "degraded"})
            ))).scalars().all())
            tasks = [task for task in tasks if task.id in failed_ids]
    report = {
        "candidates": len(tasks), "ready": 0, "degraded": 0, "skipped": 0,
        "failed": 0, "failures": [], "dry_run": dry_run,
    }
    if dry_run:
        for task in tasks:
            try:
                prepared = _prepare(task)
                report[prepared["status"]] += 1
            except KnowledgeError as error:
                report["failed"] += 1
                report["failures"].append({"task_id": task.id, "error_code": error.code, "reason": error.summary})
    else:
        for task in tasks:
            result = await build_knowledge_asset(task.id, trigger="backfill", force=force)
            if result.get("skipped"):
                report["skipped"] += 1
            elif result["status"] in {"ready", "degraded"}:
                report[result["status"]] += 1
            else:
                report["failed"] += 1
                report["failures"].append({
                    "task_id": task.id, "error_code": result.get("error_code"),
                    "reason": result.get("error_summary"),
                })
    report["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    logger.info("knowledge_backfill_batch_completed", extra={
        key: report[key] for key in ("candidates", "ready", "degraded", "skipped", "failed", "elapsed_seconds")
    })
    return report
