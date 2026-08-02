import asyncio
import json
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import aiofiles
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sse_starlette.sse import EventSourceResponse

from .config import FRONTEND_ORIGIN, MAX_UPLOAD_SIZE_MB, STORAGE_ROOT, TASKS_DIR
from .database import SnapTask, async_session, init_db
from .pipeline import reset_task, run_pipeline
from .schemas import RetryRequest, TaskCreated
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


def _task_payload(task: SnapTask):
    frames = json.loads(task.frames_json or "[]")
    return {
        "id": task.id, "filename": task.filename, "title": Path(task.filename).stem,
        "duration": task.duration, "status": task.status, "current_stage": task.current_stage,
        "progress": task.progress, "asr_provider": task.asr_provider, "note_style": task.note_style,
        "error_message": task.error_message, "frame_count": len(frames), "frames": frames,
        "transcript_segments": json.loads(task.transcripts_json or "[]"),
        "note_blocks": json.loads(task.notes_json or "[]"), "final_markdown": task.final_markdown,
        "video_url": f"/api/snapnote/tasks/{task.id}/video", "created_at": task.created_at,
    }


@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "snapnote"}


@app.post("/api/snapnote/tasks", response_model=TaskCreated)
async def create_task(background_tasks: BackgroundTasks, video: UploadFile = File(...), asr_provider: str = Form("mimo"), note_style: str = Form("classroom")):
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
        task = SnapTask(id=task_id, filename=safe_name, video_path=str(video_path), status="queued", current_stage="upload_complete", progress=3, asr_provider=asr_provider if asr_provider in {"mimo", "whisper"} else "mimo", note_style=note_style if note_style in {"classroom", "meeting"} else "classroom")
        db.add(task)
        await db.commit()
    await sse_manager.emit(task_id, "upload_complete", {"stage": "upload_complete", "progress": 3, "title": "上传完成", "message": "视频已安全保存"})
    background_tasks.add_task(run_pipeline, task_id)
    return TaskCreated(task_id=task_id, status="queued")


@app.get("/api/snapnote/tasks")
async def list_tasks():
    async with async_session() as db:
        rows = (await db.execute(select(SnapTask).order_by(SnapTask.created_at.desc()))).scalars().all()
        return [_task_payload(task) for task in rows]


@app.get("/api/snapnote/tasks/{task_id}")
async def get_task(task_id: str):
    async with async_session() as db:
        task = await db.get(SnapTask, task_id)
        if not task:
            raise HTTPException(404, "任务不存在")
        return _task_payload(task)


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
        await db.delete(task)
        await db.commit()
    target = (TASKS_DIR / task_id).resolve()
    if TASKS_DIR.resolve() in target.parents:
        shutil.rmtree(target, ignore_errors=True)
    sse_manager.clear(task_id)
    return {"ok": True}
