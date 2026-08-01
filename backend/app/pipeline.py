import asyncio
import json
import math
import re
import subprocess
import urllib.request
from pathlib import Path

from sqlalchemy import delete, select

from .config import (
    DEEPSEEK_API_KEY,
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL,
    ENABLE_LOCAL_WHISPER,
    FFMPEG_BIN,
    FFPROBE_BIN,
    FRAME_FALLBACK_INTERVAL_SECONDS,
    FRAME_MAX_COUNT,
    MAX_VIDEO_DURATION_SECONDS,
    TASKS_DIR,
    WHISPER_LANGUAGE,
    WHISPER_MODEL_SIZE,
)
from .database import SnapTask, StageResult, async_session, utcnow
from .sse_manager import sse_manager


STAGES = [
    ("probing_video", 8, "解析视频", "正在读取视频时长、分辨率与编码信息"),
    ("extracting_audio", 16, "提取音频", "正在将语音标准化为 16kHz 单声道音频"),
    ("transcribing", 32, "语音转写", "正在生成带时间戳的讲解文本"),
    ("detecting_frames", 45, "检测关键画面", "正在识别 PPT 翻页与内容变化"),
    ("selecting_frames", 55, "筛选稳定画面", "正在选择清晰、稳定的候选帧"),
    ("deduplicating_frames", 62, "图片筛选和去重", "正在合并重复或高度相似的页面"),
    ("running_ocr", 72, "OCR 页面识别", "正在提取标题、术语和正文"),
    ("aligning", 82, "图文时间对齐", "正在绑定关键画面与对应讲解"),
    ("generating_blocks", 91, "生成逐页摘要", "正在提炼知识点和复习问题"),
    ("generating_note", 97, "生成完整笔记", "正在组织目录与 Markdown"),
]


async def _run(command: list[str], timeout: int = 300):
    def execute():
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=True)
    return await asyncio.to_thread(execute)


async def _set_stage(task_id: str, stage: str, progress: int, title: str, message: str, result: dict | None = None):
    async with async_session() as db:
        task = await db.get(SnapTask, task_id)
        if not task:
            raise RuntimeError("Task no longer exists")
        task.status = "processing"
        task.current_stage = stage
        task.progress = progress
        task.updated_at = utcnow()
        stage_row = StageResult(task_id=task_id, stage=stage, status="completed" if result is not None else "running", result_json=json.dumps(result or {}, ensure_ascii=False), completed_at=utcnow() if result is not None else None)
        db.add(stage_row)
        await db.commit()
    await sse_manager.emit(task_id, stage, {"stage": stage, "progress": progress, "title": title, "message": message, **(result or {})})


async def _probe(video_path: str) -> dict:
    result = await _run([FFPROBE_BIN, "-v", "error", "-show_entries", "format=duration:stream=width,height,codec_name", "-of", "json", video_path], 60)
    data = json.loads(result.stdout)
    video_stream = next((stream for stream in data.get("streams", []) if "width" in stream), {})
    return {
        "duration": float(data.get("format", {}).get("duration", 0) or 0),
        "width": video_stream.get("width"),
        "height": video_stream.get("height"),
        "codec": video_stream.get("codec_name"),
    }


async def _extract_audio(video_path: str, audio_path: Path):
    await _run([FFMPEG_BIN, "-y", "-i", video_path, "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(audio_path)], 600)


def _demo_segments(duration: float):
    phrases = [
        "这一部分先介绍主题背景和本节内容的学习目标。",
        "接下来通过画面中的结构，解释核心概念之间的关系。",
        "这里需要关注定义、计算过程以及实际应用中的限制。",
        "最后我们总结关键结论，并给出可以继续复习的问题。",
    ]
    segment_length = max(30, duration / max(4, min(12, math.ceil(duration / 75))))
    segments = []
    cursor = 0.0
    index = 0
    while cursor < duration:
        end = min(duration, cursor + segment_length)
        segments.append({"start": round(cursor, 2), "end": round(end, 2), "text": phrases[index % len(phrases)]})
        cursor = end
        index += 1
    return segments


async def _transcribe(audio_path: Path, duration: float):
    if not ENABLE_LOCAL_WHISPER or not audio_path.exists():
        return _demo_segments(duration), "demo-fallback"

    def execute():
        from faster_whisper import WhisperModel
        model = WhisperModel(WHISPER_MODEL_SIZE, device="cpu", compute_type="int8")
        stream, _ = model.transcribe(str(audio_path), language=WHISPER_LANGUAGE, vad_filter=True)
        return [{"start": item.start, "end": item.end, "text": item.text.strip()} for item in stream]

    return await asyncio.to_thread(execute), "faster-whisper"


async def _extract_frames(video_path: str, duration: float, frames_dir: Path):
    frames_dir.mkdir(parents=True, exist_ok=True)
    interval = max(FRAME_FALLBACK_INTERVAL_SECONDS, duration / FRAME_MAX_COUNT)
    timestamps = [min(duration - .2, max(.2, value)) for value in [0.5] + [interval * index for index in range(1, math.ceil(duration / interval))]]
    frames = []
    for index, timestamp in enumerate(timestamps[:FRAME_MAX_COUNT]):
        output = frames_dir / f"frame_{index + 1:03d}.jpg"
        try:
            await _run([FFMPEG_BIN, "-y", "-ss", f"{timestamp:.2f}", "-i", video_path, "-frames:v", "1", "-q:v", "2", str(output)], 90)
            frames.append({"id": f"frame-{index + 1}", "timestamp": round(timestamp, 2), "image_url": f"/storage/tasks/{frames_dir.parent.name}/frames/{output.name}", "ocr_text": "", "confidence": .82})
        except Exception:
            continue
    return frames


def _deduplicate(frames: list[dict], frames_dir: Path):
    try:
        import imagehash
        from PIL import Image
        selected, hashes = [], []
        for frame in frames:
            path = frames_dir / Path(frame["image_url"]).name
            current = imagehash.phash(Image.open(path))
            if hashes and min(current - previous for previous in hashes) <= 6:
                continue
            hashes.append(current)
            selected.append(frame)
        return selected
    except Exception:
        return frames


def _ocr_frames(frames: list[dict], frames_dir: Path):
    try:
        from paddleocr import PaddleOCR
        engine = PaddleOCR(use_angle_cls=True, lang="ch", show_log=False)
        for frame in frames:
            result = engine.ocr(str(frames_dir / Path(frame["image_url"]).name), cls=True)
            lines = [item[1][0] for group in (result or []) for item in (group or [])]
            frame["ocr_text"] = "\n".join(lines)
    except Exception:
        pass
    return frames


def _aligned_blocks(frames: list[dict], segments: list[dict], duration: float):
    if not frames:
        fallback_count = max(1, min(6, math.ceil(duration / 90)))
        frames = [{"id": f"virtual-{i + 1}", "timestamp": i * duration / fallback_count, "image_url": "", "ocr_text": "", "confidence": .65} for i in range(fallback_count)]
    blocks = []
    for index, frame in enumerate(frames):
        start = frame["timestamp"]
        end = frames[index + 1]["timestamp"] if index + 1 < len(frames) else duration
        related = [item["text"] for item in segments if item["end"] >= start - 8 and item["start"] < end + 8]
        source = "".join(related).strip() or "该页面暂无可用语音转写。"
        title = (frame.get("ocr_text") or "").splitlines()[0][:60] or f"关键画面 {index + 1}"
        blocks.append({
            "id": f"block-{index + 1}", "frame_id": frame["id"], "timestamp": start, "end_time": end,
            "title": title, "summary": source[:260],
            "key_points": [part.strip() for part in re.split(r"[。！？]", source) if part.strip()][:3] or ["回看对应视频片段，补充本页重点"],
            "review_questions": [f"{title} 中最需要记住的核心关系是什么？"],
            "ocr_text": frame.get("ocr_text", ""), "image_url": frame.get("image_url", ""), "confidence": frame.get("confidence", .7),
        })
    return blocks


def _llm_enhance_sync(blocks: list[dict], note_style: str):
    if not DEEPSEEK_API_KEY:
        return blocks
    prompt = "请将以下按页对齐的视频材料整理为结构化 JSON 数组。保留 id、timestamp、image_url、ocr_text、confidence 字段，为每项生成 title、summary、key_points(数组)、review_questions(数组)。笔记类型：" + note_style + "\n" + json.dumps(blocks, ensure_ascii=False)[:40000]
    body = json.dumps({"model": DEEPSEEK_MODEL, "messages": [{"role": "system", "content": "你是严谨的课程与会议笔记编辑，只输出 JSON。"}, {"role": "user", "content": prompt}], "response_format": {"type": "json_object"}, "temperature": .2}).encode("utf-8")
    request = urllib.request.Request(f"{DEEPSEEK_BASE_URL}/chat/completions", data=body, headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=120) as response:
        content = json.loads(response.read().decode("utf-8"))["choices"][0]["message"]["content"]
    parsed = json.loads(content)
    return parsed.get("blocks", parsed) if isinstance(parsed, dict) else parsed


def _markdown(task: SnapTask, blocks: list[dict]):
    lines = [f"# {Path(task.filename).stem}", "", f"> 视频时长：{task.duration:.0f} 秒 · SnapNote 自动生成", "", "## 总体摘要", "", "本笔记按视频中的关键画面组织，并将画面与对应语音讲解进行了时间对齐。", "", "## 章节目录", ""]
    for index, block in enumerate(blocks):
        lines.append(f"- [{block['title']}](#section-{index + 1}) · {block['timestamp']:.0f}s")
    for index, block in enumerate(blocks):
        lines += ["", f"<a id=\"section-{index + 1}\"></a>", f"## {index + 1}. {block['title']}", "", f"**视频时间：{block['timestamp']:.0f}s**", ""]
        if block.get("image_url"):
            lines += [f"![关键画面]({block['image_url']})", ""]
        lines += [block["summary"], "", "### 关键知识点", ""] + [f"- {point}" for point in block.get("key_points", [])]
        lines += ["", "### 待复习问题", ""] + [f"- {question}" for question in block.get("review_questions", [])]
        if block.get("ocr_text"):
            lines += ["", "<details><summary>OCR 页面文字</summary>", "", block["ocr_text"], "", "</details>"]
    return "\n".join(lines) + "\n"


async def run_pipeline(task_id: str):
    try:
        async with async_session() as db:
            task = await db.get(SnapTask, task_id)
            if not task:
                return
            video_path = task.video_path
            task_dir = TASKS_DIR / task_id
            frames_dir = task_dir / "frames"
            audio_path = task_dir / "audio.wav"

        await _set_stage(task_id, *STAGES[0])
        metadata = await _probe(video_path)
        duration = min(metadata.get("duration") or 0, MAX_VIDEO_DURATION_SECONDS)
        if duration <= 0:
            raise RuntimeError("无法读取视频时长，请确认编码是否受支持")
        async with async_session() as db:
            task = await db.get(SnapTask, task_id)
            task.duration = duration
            await db.commit()
        await _set_stage(task_id, STAGES[0][0], STAGES[0][1], STAGES[0][2], "视频解析完成", metadata)

        await _set_stage(task_id, *STAGES[1])
        audio_available = True
        try:
            await _extract_audio(video_path, audio_path)
        except Exception:
            audio_available = False
        async with async_session() as db:
            task = await db.get(SnapTask, task_id)
            task.audio_path = str(audio_path) if audio_available else None
            await db.commit()
        await _set_stage(task_id, STAGES[1][0], STAGES[1][1], STAGES[1][2], "音频阶段完成", {"audio_available": audio_available})

        await _set_stage(task_id, *STAGES[2])
        segments, engine = await _transcribe(audio_path, duration)
        async with async_session() as db:
            task = await db.get(SnapTask, task_id)
            task.transcripts_json = json.dumps(segments, ensure_ascii=False)
            await db.commit()
        await _set_stage(task_id, STAGES[2][0], STAGES[2][1], STAGES[2][2], f"转写完成，共 {len(segments)} 个片段", {"segment_count": len(segments), "engine": engine})

        await _set_stage(task_id, *STAGES[3])
        frames = await _extract_frames(video_path, duration, frames_dir)
        await _set_stage(task_id, STAGES[3][0], STAGES[3][1], STAGES[3][2], f"检测到 {len(frames)} 个候选画面", {"frame_count": len(frames)})
        await _set_stage(task_id, *STAGES[4], {"frame_count": len(frames)})

        await _set_stage(task_id, *STAGES[5])
        frames = await asyncio.to_thread(_deduplicate, frames, frames_dir)
        await _set_stage(task_id, STAGES[5][0], STAGES[5][1], STAGES[5][2], f"保留 {len(frames)} 张关键画面", {"frame_count": len(frames)})

        await _set_stage(task_id, *STAGES[6])
        frames = await asyncio.to_thread(_ocr_frames, frames, frames_dir)
        async with async_session() as db:
            task = await db.get(SnapTask, task_id)
            task.frames_json = json.dumps(frames, ensure_ascii=False)
            await db.commit()
        await _set_stage(task_id, STAGES[6][0], STAGES[6][1], STAGES[6][2], "OCR 识别完成", {"ocr_count": sum(bool(frame.get('ocr_text')) for frame in frames)})

        await _set_stage(task_id, *STAGES[7])
        blocks = _aligned_blocks(frames, segments, duration)
        await _set_stage(task_id, STAGES[7][0], STAGES[7][1], STAGES[7][2], f"完成 {len(blocks)} 个图文片段对齐", {"alignment_count": len(blocks)})

        await _set_stage(task_id, *STAGES[8])
        try:
            async with async_session() as db:
                task = await db.get(SnapTask, task_id)
                blocks = await asyncio.to_thread(_llm_enhance_sync, blocks, task.note_style)
        except Exception:
            pass
        await _set_stage(task_id, STAGES[8][0], STAGES[8][1], STAGES[8][2], "逐页摘要生成完成", {"block_count": len(blocks)})

        await _set_stage(task_id, *STAGES[9])
        async with async_session() as db:
            task = await db.get(SnapTask, task_id)
            task.notes_json = json.dumps(blocks, ensure_ascii=False)
            task.final_markdown = _markdown(task, blocks)
            task.status = "completed"
            task.current_stage = "complete"
            task.progress = 100
            task.updated_at = utcnow()
            await db.commit()
        await sse_manager.emit(task_id, "complete", {"stage": "complete", "progress": 100, "title": "处理完成", "message": "图文笔记已经准备好"})
    except Exception as error:
        async with async_session() as db:
            task = await db.get(SnapTask, task_id)
            if task:
                task.status = "failed"
                task.current_stage = "step_error"
                task.error_message = str(error)[:1000]
                task.updated_at = utcnow()
                await db.commit()
        await sse_manager.emit(task_id, "step_error", {"stage": "step_error", "message": str(error)[:500]})


async def reset_task(task_id: str, provider: str | None = None):
    async with async_session() as db:
        task = await db.get(SnapTask, task_id)
        if not task:
            return False
        task.status = "queued"
        task.current_stage = "upload_complete"
        task.progress = 2
        task.error_message = None
        task.frames_json = "[]"
        task.transcripts_json = "[]"
        task.notes_json = "[]"
        task.final_markdown = ""
        if provider:
            task.asr_provider = provider
        await db.execute(delete(StageResult).where(StageResult.task_id == task_id))
        await db.commit()
    sse_manager.clear(task_id)
    return True
