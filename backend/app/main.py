import asyncio
import json
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import aiofiles
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sse_starlette.sse import EventSourceResponse

from .config import (
    DEFAULT_ASR_PROVIDER,
    DEFAULT_NOTE_MODEL,
    ENABLE_KNOWLEDGE_REBUILD,
    ENABLE_KNOWLEDGE_SEARCH,
    ENABLE_KNOWLEDGE_STATUS_UI,
    FRONTEND_ORIGIN,
    MAX_UPLOAD_SIZE_MB,
    STORAGE_ROOT,
    TASKS_DIR,
)
from .database import KnowledgeAsset, SnapTask, async_session, init_db
from .knowledge import (
    KnowledgeError,
    build_knowledge_asset,
    get_asset,
    get_chapter,
    get_keyframe,
    get_task_knowledge_status,
    get_transcript,
    list_assets as list_knowledge_assets,
    remove_knowledge_for_task,
    search_knowledge,
)
from .pipeline import new_processing_state, reset_task, run_pipeline
from .schemas import KnowledgeSearchRequest, RetryRequest, TaskCreated
from .sse_manager import sse_manager


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_db()
    yield


app = FastAPI(title="SnapNote API", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=[FRONTEND_ORIGIN], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
app.mount("/storage", StaticFiles(directory=str(STORAGE_ROOT)), name="storage")


def _safe_filename(filename: str) -> str:
    name = Path(filename or "video.mp4").name
    return "".join(char for char in name if char.isalnum() or char in " ._-()")[:180] or "video.mp4"


def _json_payload(raw: str | None, fallback):
    try:
        value = json.loads(raw or json.dumps(fallback))
        return value if isinstance(value, type(fallback)) else fallback
    except (json.JSONDecodeError, TypeError):
        return fallback


def _task_payload(task: SnapTask, knowledge_asset: dict | None = None):
    frames = _json_payload(task.frames_json, [])
    processing_state = _json_payload(task.processing_state_json, {})
    return {
        "id": task.id, "filename": task.filename, "title": Path(task.filename).stem,
        "duration": task.duration, "status": task.status, "current_stage": task.current_stage,
        "progress": task.progress, "asr_provider": task.asr_provider, "note_style": task.note_style,
        "note_model": task.note_model,
        "error_message": task.error_message, "frame_count": len(frames), "frames": frames,
        "transcript_segments": _json_payload(task.transcripts_json, []),
        "note_blocks": _json_payload(task.notes_json, []), "final_markdown": task.final_markdown,
        "visual_analysis": _json_payload(task.visual_analysis_json, {}),
        "processing_state": processing_state,
        "video_url": f"/api/snapnote/tasks/{task.id}/video", "created_at": task.created_at,
        "knowledge_asset": knowledge_asset if ENABLE_KNOWLEDGE_STATUS_UI else None,
    }


@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "snapnote"}


@app.post("/api/snapnote/tasks", response_model=TaskCreated)
async def create_task(
    background_tasks: BackgroundTasks,
    video: UploadFile = File(...),
    asr_provider: str = Form(DEFAULT_ASR_PROVIDER),
    note_style: str = Form("classroom"),
    note_model: str = Form(DEFAULT_NOTE_MODEL),
):
    suffix = Path(video.filename or "").suffix.lower()
    if suffix not in {".mp4", ".mov", ".webm"}:
        raise HTTPException(415, "仅支持 MP4、MOV 和 WebM 视频")
    task_id = str(uuid.uuid4())
    task_dir = TASKS_DIR / task_id
    task_dir.mkdir(parents=True, exist_ok=False)
    safe_name = _safe_filename(video.filename or f"video{suffix}")
    video_path = task_dir / f"source{suffix}"
    written = 0
    try:
        async with aiofiles.open(video_path, "wb") as output:
            while chunk := await video.read(8 * 1024 * 1024):
                written += len(chunk)
                if written > MAX_UPLOAD_SIZE_MB * 1024 * 1024:
                    raise HTTPException(413, f"视频不能超过 {MAX_UPLOAD_SIZE_MB} MB")
                await output.write(chunk)
    except Exception:
        shutil.rmtree(task_dir, ignore_errors=True)
        raise

    async with async_session() as db:
        task = SnapTask(
            id=task_id,
            filename=safe_name,
            video_path=str(video_path),
            status="queued",
            current_stage="upload_complete",
            progress=3,
            asr_provider=asr_provider if asr_provider in {"mimo", "whisper"} else DEFAULT_ASR_PROVIDER,
            note_style=note_style if note_style in {"classroom", "meeting"} else "classroom",
            note_model=note_model if note_model in {"mimo", "deepseek"} else DEFAULT_NOTE_MODEL,
            processing_state_json=json.dumps(new_processing_state(), ensure_ascii=False),
        )
        db.add(task)
        await db.commit()
    await sse_manager.emit(task_id, "upload_complete", {"stage": "upload_complete", "progress": 3, "title": "上传完成", "message": "视频已安全保存"})
    background_tasks.add_task(run_pipeline, task_id)
    return TaskCreated(task_id=task_id, status="queued")


@app.get("/api/snapnote/tasks")
async def list_tasks():
    async with async_session() as db:
        rows = (await db.execute(select(SnapTask).order_by(SnapTask.created_at.desc()))).scalars().all()
        knowledge = (await db.execute(select(KnowledgeAsset))).scalars().all()
        by_task = {asset.task_id: {
            "asset_id": asset.id, "status": asset.status,
            "current_version_id": asset.current_version_id,
            "missing_items": _json_payload(asset.missing_items_json, []),
            "error_summary": asset.error_summary, "updated_at": asset.updated_at,
        } for asset in knowledge}
        return [_task_payload(task, by_task.get(task.id, {
            "asset_id": None, "status": "not_built", "current_version_id": None,
            "missing_items": [], "error_summary": None,
        })) for task in rows]


@app.get("/api/snapnote/tasks/{task_id}")
async def get_task(task_id: str):
    async with async_session() as db:
        task = await db.get(SnapTask, task_id)
        if not task:
            raise HTTPException(404, "任务不存在")
        knowledge = await get_task_knowledge_status(task_id) if ENABLE_KNOWLEDGE_STATUS_UI else None
        return _task_payload(task, knowledge)


@app.get("/api/snapnote/tasks/{task_id}/knowledge")
async def task_knowledge_status(task_id: str):
    async with async_session() as db:
        if not await db.get(SnapTask, task_id):
            raise HTTPException(404, "任务不存在")
    return await get_task_knowledge_status(task_id)


@app.post("/api/snapnote/tasks/{task_id}/knowledge/rebuild")
async def rebuild_task_knowledge(task_id: str):
    if not ENABLE_KNOWLEDGE_REBUILD:
        raise HTTPException(503, "知识资产重建功能未启用")
    async with async_session() as db:
        task = await db.get(SnapTask, task_id)
        if not task:
            raise HTTPException(404, "任务不存在")
        asset = (await db.execute(select(KnowledgeAsset).where(
            KnowledgeAsset.task_id == task_id
        ))).scalar_one_or_none()
        if asset and asset.status in {"building", "deleting"}:
            raise HTTPException(409, "知识资产正在处理，请勿重复提交")
    result = await build_knowledge_asset(task_id, trigger="manual", force=True)
    if result["status"] == "failed":
        raise HTTPException(422, result.get("error_summary", "知识资产构建失败"))
    return result


@app.get("/api/knowledge/assets")
async def knowledge_assets(
    status: str | None = None,
    title: str | None = None,
    owner_scope: str = "local",
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    return await list_knowledge_assets(
        status=status, title_query=title, owner_scope=owner_scope, limit=limit, offset=offset
    )


@app.get("/api/knowledge/assets/{asset_id}")
async def knowledge_asset_detail(asset_id: str, owner_scope: str = "local"):
    result = await get_asset(asset_id, owner_scope)
    if not result:
        raise HTTPException(404, "未找到或不可访问")
    return result


@app.post("/api/knowledge/search")
async def knowledge_search(payload: KnowledgeSearchRequest):
    if not ENABLE_KNOWLEDGE_SEARCH:
        raise HTTPException(503, "知识检索功能未启用")
    try:
        return await search_knowledge(
            payload.query, asset_ids=payload.asset_ids,
            content_types=payload.content_types, top_k=payload.top_k,
            owner_scope=payload.owner_scope,
        )
    except KnowledgeError as error:
        raise HTTPException(422, {"code": error.code, "summary": error.summary}) from None


@app.get("/api/knowledge/chapters/{chapter_id}")
async def knowledge_chapter_detail(chapter_id: str, owner_scope: str = "local"):
    result = await get_chapter(chapter_id, owner_scope)
    if not result:
        raise HTTPException(404, "未找到或不可访问")
    return result


@app.get("/api/knowledge/assets/{asset_id}/transcript")
async def knowledge_transcript(
    asset_id: str,
    start_time: float = Query(0, ge=0),
    end_time: float | None = Query(None, gt=0),
    owner_scope: str = "local",
):
    try:
        result = await get_transcript(asset_id, start_time, end_time, owner_scope)
    except KnowledgeError as error:
        raise HTTPException(422, {"code": error.code, "summary": error.summary}) from None
    if not result:
        raise HTTPException(404, "未找到或不可访问")
    return result


@app.get("/api/knowledge/media/{media_id}")
async def knowledge_media(media_id: str, owner_scope: str = "local"):
    result = await get_keyframe(media_id, owner_scope)
    if not result:
        raise HTTPException(404, "未找到或不可访问")
    return result


@app.get("/api/snapnote/tasks/{task_id}/stream")
async def stream_task(task_id: str):
    queue = sse_manager.subscribe(task_id)
    history = sse_manager.history(task_id)

    async def events():
        try:
            for item in history:
                yield {"event": item["event"], "data": json.dumps(item["data"], ensure_ascii=False)}
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), 15)
                    yield {"event": item["event"], "data": json.dumps(item["data"], ensure_ascii=False)}
                    if item["event"] in {"complete", "step_error"}:
                        return
                except asyncio.TimeoutError:
                    yield {"event": "heartbeat", "data": "{}"}
        finally:
            sse_manager.unsubscribe(task_id, queue)

    return EventSourceResponse(events())


@app.post("/api/snapnote/tasks/{task_id}/retry")
async def retry_task(task_id: str, payload: RetryRequest, background_tasks: BackgroundTasks):
    if not await reset_task(task_id, payload.asr_provider):
        raise HTTPException(404, "任务不存在")
    background_tasks.add_task(run_pipeline, task_id)
    return {"task_id": task_id, "status": "queued"}


@app.get("/api/snapnote/tasks/{task_id}/export/markdown")
async def export_markdown(task_id: str):
    async with async_session() as db:
        task = await db.get(SnapTask, task_id)
        if not task or not task.final_markdown:
            raise HTTPException(404, "笔记尚未生成")
        output = TASKS_DIR / task_id / "SnapNote.md"
        output.write_text(task.final_markdown, encoding="utf-8")
        return FileResponse(output, filename=f"{Path(task.filename).stem}-SnapNote.md", media_type="text/markdown")


@app.get("/api/snapnote/tasks/{task_id}/video")
async def task_video(task_id: str):
    async with async_session() as db:
        task = await db.get(SnapTask, task_id)
        if not task or not Path(task.video_path).is_file():
            raise HTTPException(404, "视频不存在")
        return FileResponse(task.video_path)


@app.delete("/api/snapnote/tasks/{task_id}")
async def delete_task(task_id: str):
    async with async_session() as db:
        task = await db.get(SnapTask, task_id)
        if not task:
            raise HTTPException(404, "任务不存在")
        asset = (await db.execute(select(KnowledgeAsset).where(
            KnowledgeAsset.task_id == task_id
        ))).scalar_one_or_none()
        if asset:
            asset.status = "deleting"
        await db.commit()
    target = (TASKS_DIR / task_id).resolve()
    if TASKS_DIR.resolve() not in target.parents:
        raise HTTPException(500, "任务目录校验失败，知识资产已停止参与检索")
    try:
        if target.exists():
            shutil.rmtree(target)
    except OSError:
        raise HTTPException(503, "视频已停止提供知识检索，部分数据清理未完成，可重试") from None
    async with async_session() as db:
        task = await db.get(SnapTask, task_id)
        if task:
            await remove_knowledge_for_task(task_id, db)
            await db.delete(task)
            await db.commit()
    sse_manager.clear(task_id)
    return {"ok": True}
