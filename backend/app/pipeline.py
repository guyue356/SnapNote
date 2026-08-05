import asyncio
import json
import math
import re
import shutil
import subprocess
import urllib.request
from pathlib import Path

from sqlalchemy import delete

from .asr import transcribe_audio
from .config import (
    AUDIO_SAMPLE_RATE,
    DEEPSEEK_API_KEY,
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL,
    FFMPEG_BIN,
    FFPROBE_BIN,
    MAX_VIDEO_DURATION_SECONDS,
    MIMO_VISION_MAX_CLIPS,
    MIMO_VISION_REQUIRED,
    TASKS_DIR,
)
from .database import SnapTask, StageResult, async_session, utcnow
from .mimo_vision import (
    summarize_video_style,
    understand_clips,
    understand_keyframes,
)
from .sse_manager import sse_manager
from .vision import (
    analyze_shots,
    create_silent_proxy_clips,
    deduplicate_frames,
    extract_keyframes,
)


STAGES = [
    ("probing_video", 6, "解析视频", "正在读取视频时长、分辨率与编码信息"),
    ("extracting_audio", 12, "提取音频", "正在生成 16kHz 单声道语音轨"),
    ("transcribing", 28, "语音转写", "正在生成带时间戳的精确转写"),
    ("detecting_frames", 38, "检测镜头", "正在扫描场景变化、运动和画面质量"),
    ("selecting_frames", 46, "选择关键帧", "正在为每个镜头选择清晰稳定的代表画面"),
    ("deduplicating_frames", 51, "关键帧去重", "正在合并重复或高度相似的画面"),
    ("running_ocr", 56, "本地 OCR", "正在尝试提取画面中的文字"),
    ("understanding_frames", 68, "MiMo 关键帧理解", "正在批量分析主体、场景、构图与素材风格"),
    ("understanding_clips", 76, "MiMo 动态片段理解", "正在补充动作、运镜、转场与节奏信息"),
    ("analyzing_style", 84, "整片风格与分镜分析", "正在归纳叙事结构、视觉风格和爆款元素"),
    ("aligning", 89, "图文时间对齐", "正在绑定镜头、视觉语义与语音转写"),
    ("generating_blocks", 94, "生成结构化笔记", "正在生成逐镜头摘要、重点和复习问题"),
    ("generating_note", 98, "生成完整结果", "正在组织 Markdown 与分析产物"),
]


async def _run(command: list[str], timeout: int = 300):
    def execute():
        return subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=True
        )
    return await asyncio.to_thread(execute)


async def _set_stage(
    task_id: str,
    stage: str,
    progress: int,
    title: str,
    message: str,
    result: dict | None = None,
):
    async with async_session() as db:
        task = await db.get(SnapTask, task_id)
        if not task:
            raise RuntimeError("任务已不存在")
        task.status = "processing"
        task.current_stage = stage
        task.progress = progress
        task.updated_at = utcnow()
        db.add(StageResult(
            task_id=task_id,
            stage=stage,
            status="completed" if result is not None else "running",
            result_json=json.dumps(result or {}, ensure_ascii=False),
            completed_at=utcnow() if result is not None else None,
        ))
        await db.commit()
    await sse_manager.emit(task_id, stage, {
        "stage": stage,
        "progress": progress,
        "title": title,
        "message": message,
        **(result or {}),
    })


async def _probe(video_path: str) -> dict:
    result = await _run([
        FFPROBE_BIN,
        "-v",
        "error",
        "-show_entries",
        "format=duration:stream=width,height,codec_name",
        "-of",
        "json",
        video_path,
    ], 60)
    data = json.loads(result.stdout)
    video_stream = next(
        (stream for stream in data.get("streams", []) if "width" in stream), {}
    )
    return {
        "duration": float(data.get("format", {}).get("duration", 0) or 0),
        "width": video_stream.get("width"),
        "height": video_stream.get("height"),
        "codec": video_stream.get("codec_name"),
    }


async def _extract_audio(video_path: str, audio_path: Path, duration: float):
    await _run([
        FFMPEG_BIN,
        "-y",
        "-i",
        video_path,
        "-t",
        f"{duration:.3f}",
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(AUDIO_SAMPLE_RATE),
        "-c:a",
        "pcm_s16le",
        str(audio_path),
    ], 600)


def _ocr_frames(frames: list[dict], frames_dir: Path):
    try:
        from paddleocr import PaddleOCR

        engine = PaddleOCR(use_angle_cls=True, lang="ch", show_log=False)
        for frame in frames:
            try:
                result = engine.ocr(
                    str(frames_dir / Path(frame["image_url"]).name), cls=True
                )
                lines = [
                    item[1][0]
                    for group in (result or [])
                    for item in (group or [])
                ]
                frame["ocr_text"] = "\n".join(lines)
            except Exception:
                continue
    except Exception:
        pass
    return frames


def _sentences(text: str, maximum: int = 3) -> list[str]:
    return [
        part.strip()
        for part in re.split(r"[。！？!?；;\n]", text)
        if part.strip()
    ][:maximum]


def _aligned_blocks(frames: list[dict], segments: list[dict], duration: float):
    if not frames:
        fallback_count = max(1, min(6, math.ceil(duration / 90)))
        frames = [{
            "id": f"virtual-{index + 1}",
            "timestamp": index * duration / fallback_count,
            "start_time": index * duration / fallback_count,
            "end_time": (index + 1) * duration / fallback_count,
            "image_url": "",
            "ocr_text": "",
            "confidence": 0.5,
        } for index in range(fallback_count)]

    blocks = []
    for index, frame in enumerate(frames):
        start = float(frame.get("start_time", frame["timestamp"]))
        end = float(frame.get(
            "end_time",
            frames[index + 1]["timestamp"] if index + 1 < len(frames) else duration,
        ))
        related = [
            str(item.get("text", ""))
            for item in segments
            if float(item.get("end", 0)) >= start - 3
            and float(item.get("start", 0)) <= end + 3
        ]
        transcript = "".join(related).strip()
        visual = frame.get("visual_analysis", {})
        motion = frame.get("clip_analysis", {})
        title = (
            visual.get("title")
            or (frame.get("ocr_text") or "").splitlines()[0][:80]
            or f"关键镜头 {index + 1}"
        )
        visual_summary = visual.get("summary") or visual.get("description") or ""
        motion_summary = motion.get("motion") or ""
        summary_parts = [part for part in (visual_summary, motion_summary, transcript) if part]
        summary = "；".join(summary_parts)[:900] or "该片段暂无可用语音或视觉摘要。"
        points = list(visual.get("key_points") or [])
        points.extend(_sentences(transcript, 4))
        points = list(dict.fromkeys(point for point in points if point))[:5]
        if not points:
            points = ["回看对应时间段并确认该镜头在整体叙事中的作用。"]
        blocks.append({
            "id": f"block-{index + 1}",
            "frame_id": frame["id"],
            "timestamp": start,
            "end_time": max(start, end),
            "title": title,
            "summary": summary,
            "key_points": points,
            "review_questions": [f"{title} 在这段视频中承担了什么叙事或表达作用？"],
            "ocr_text": frame.get("ocr_text", ""),
            "image_url": frame.get("image_url", ""),
            "confidence": frame.get("confidence", 0.7),
            "visual_analysis": visual,
            "clip_analysis": motion,
            "shot_metrics": {
                key: frame.get(key)
                for key in (
                    "quality_score", "sharpness_score", "brightness_score",
                    "contrast_score", "stability_score", "dynamic_score",
                )
            },
        })
    return blocks


def _llm_enhance_sync(blocks: list[dict], note_style: str):
    if not DEEPSEEK_API_KEY:
        return blocks
    prompt = (
        "将以下已按镜头对齐的视频材料整理为 JSON 对象 {\"blocks\": [...]}。"
        "必须保留每项 id、frame_id、timestamp、end_time、image_url、ocr_text、confidence、"
        "visual_analysis、clip_analysis、shot_metrics，仅改进 title、summary、key_points 和"
        f"review_questions。笔记类型：{note_style}\n"
        + json.dumps(blocks, ensure_ascii=False)[:60000]
    )
    body = json.dumps({
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": "你是严谨的视频内容编辑，只输出 JSON。"},
            {"role": "user", "content": prompt},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
    }, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{DEEPSEEK_BASE_URL}/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        content = json.loads(response.read().decode("utf-8"))["choices"][0]["message"]["content"]
    parsed = json.loads(content)
    enhanced = parsed.get("blocks", []) if isinstance(parsed, dict) else []
    return enhanced if isinstance(enhanced, list) and enhanced else blocks


def _markdown(task: SnapTask, blocks: list[dict], visual_analysis: dict):
    overall = visual_analysis.get("summary") or "视频已完成镜头、视觉语义与语音内容对齐。"
    lines = [
        f"# {Path(task.filename).stem}",
        "",
        f"> 视频时长：{task.duration:.0f} 秒 · SnapNote 自动生成",
        "",
        "## 总体摘要",
        "",
        overall,
        "",
    ]
    if visual_analysis.get("visual_style"):
        lines += [
            "## 视觉与剪辑画像",
            "",
            f"- 内容类型：{visual_analysis.get('content_type', '未分类')}",
            f"- 视觉风格：{visual_analysis.get('visual_style')}",
            f"- 剪辑风格：{visual_analysis.get('editing_style', '未识别')}",
            f"- 开场钩子：{visual_analysis.get('hook', '未识别')}",
            "",
        ]
    lines += ["## 章节目录", ""]
    for index, block in enumerate(blocks):
        lines.append(f"- [{block['title']}](#section-{index + 1}) · {block['timestamp']:.0f}s")
    for index, block in enumerate(blocks):
        lines += [
            "",
            f"<a id=\"section-{index + 1}\"></a>",
            f"## {index + 1}. {block['title']}",
            "",
            f"**视频时间：{block['timestamp']:.0f}s–{block['end_time']:.0f}s**",
            "",
        ]
        if block.get("image_url"):
            lines += [f"![关键画面]({block['image_url']})", ""]
        lines += [block["summary"], "", "### 关键点", ""]
        lines += [f"- {point}" for point in block.get("key_points", [])]
        lines += ["", "### 复习 / 分析问题", ""]
        lines += [f"- {question}" for question in block.get("review_questions", [])]
        if block.get("ocr_text"):
            lines += [
                "",
                "<details><summary>画面文字</summary>",
                "",
                block["ocr_text"],
                "",
                "</details>",
            ]
    return "\n".join(lines) + "\n"


async def _save_frames(task_id: str, frames: list[dict]):
    async with async_session() as db:
        task = await db.get(SnapTask, task_id)
        if task:
            task.frames_json = json.dumps(frames, ensure_ascii=False)
            await db.commit()


async def run_pipeline(task_id: str):
    clips_dir = None
    try:
        async with async_session() as db:
            task = await db.get(SnapTask, task_id)
            if not task:
                return
            video_path = task.video_path
            task_dir = TASKS_DIR / task_id
            frames_dir = task_dir / "frames"
            clips_dir = task_dir / "visual_clips"
            audio_path = task_dir / "audio.wav"
            asr_provider = task.asr_provider

        await _set_stage(task_id, *STAGES[0])
        metadata = await _probe(video_path)
        source_duration = metadata.get("duration") or 0
        duration = min(source_duration, MAX_VIDEO_DURATION_SECONDS)
        if duration <= 0:
            raise RuntimeError("无法读取视频时长，请确认视频编码是否受支持")
        metadata["processed_duration"] = duration
        metadata["truncated"] = bool(source_duration > duration)
        async with async_session() as db:
            task = await db.get(SnapTask, task_id)
            task.duration = duration
            await db.commit()
        await _set_stage(task_id, STAGES[0][0], STAGES[0][1], STAGES[0][2], "视频解析完成", metadata)

        await _set_stage(task_id, *STAGES[1])
        await _extract_audio(video_path, audio_path, duration)
        async with async_session() as db:
            task = await db.get(SnapTask, task_id)
            task.audio_path = str(audio_path)
            await db.commit()
        await _set_stage(task_id, STAGES[1][0], STAGES[1][1], STAGES[1][2], "音频提取完成", {"sample_rate": AUDIO_SAMPLE_RATE})

        await _set_stage(task_id, *STAGES[2])

        async def report_asr_progress(payload: dict):
            await sse_manager.emit(task_id, "transcribing", {
                "stage": "transcribing",
                "progress": STAGES[2][1],
                "stage_progress": int(payload.get("progress_pct", 0)),
                "title": STAGES[2][2],
                "message": payload.get("detail", "正在执行语音转写"),
                **payload,
            })

        segments, engine = await transcribe_audio(
            audio_path, duration, asr_provider, report_asr_progress
        )
        async with async_session() as db:
            task = await db.get(SnapTask, task_id)
            task.transcripts_json = json.dumps(segments, ensure_ascii=False)
            await db.commit()
        await _set_stage(task_id, STAGES[2][0], STAGES[2][1], STAGES[2][2], f"转写完成，共 {len(segments)} 个片段", {"segment_count": len(segments), "engine": engine})

        await _set_stage(task_id, *STAGES[3])
        shots, shot_stats = await analyze_shots(video_path, duration)
        await _set_stage(task_id, STAGES[3][0], STAGES[3][1], STAGES[3][2], f"检测到 {shot_stats['detected_shots']} 个镜头", shot_stats)

        await _set_stage(task_id, *STAGES[4])
        frames = await extract_keyframes(video_path, shots, frames_dir)
        if not frames:
            raise RuntimeError("未能提取任何关键帧")
        await _set_stage(task_id, STAGES[4][0], STAGES[4][1], STAGES[4][2], f"提取 {len(frames)} 张高质量关键帧", {"frame_count": len(frames)})

        await _set_stage(task_id, *STAGES[5])
        frames = await asyncio.to_thread(deduplicate_frames, frames, frames_dir)
        await _save_frames(task_id, frames)
        await _set_stage(task_id, STAGES[5][0], STAGES[5][1], STAGES[5][2], f"去重后保留 {len(frames)} 张关键帧", {"frame_count": len(frames)})

        await _set_stage(task_id, *STAGES[6])
        frames = await asyncio.to_thread(_ocr_frames, frames, frames_dir)
        await _save_frames(task_id, frames)
        await _set_stage(task_id, STAGES[6][0], STAGES[6][1], STAGES[6][2], "本地 OCR 阶段完成", {"ocr_count": sum(bool(frame.get('ocr_text')) for frame in frames)})

        await _set_stage(task_id, *STAGES[7])

        async def report_vision_progress(payload: dict):
            await sse_manager.emit(task_id, "understanding_frames", {
                "stage": "understanding_frames",
                "progress": STAGES[7][1],
                "stage_progress": int(payload.get("completed", 0) / max(1, payload.get("total", 1)) * 100),
                "title": STAGES[7][2],
                "message": payload.get("detail", STAGES[7][3]),
                **payload,
            })

        try:
            frames, image_stats = await understand_keyframes(
                frames, frames_dir, segments, report_vision_progress
            )
            if MIMO_VISION_REQUIRED and not image_stats.get("enabled"):
                raise RuntimeError(f"MiMo 视觉理解不可用：{image_stats.get('reason', 'unknown')}")
        except Exception as error:
            if MIMO_VISION_REQUIRED:
                raise
            image_stats = {"enabled": False, "error": str(error)[:500], "usage": {}}
        await _save_frames(task_id, frames)
        await _set_stage(task_id, STAGES[7][0], STAGES[7][1], STAGES[7][2], f"已理解 {image_stats.get('analyzed_frames', 0)} 张关键帧", image_stats)

        await _set_stage(task_id, *STAGES[8])
        clip_stats = {"enabled": False, "clip_count": 0, "usage": {}}
        try:
            clips = await create_silent_proxy_clips(
                video_path, frames, clips_dir, MIMO_VISION_MAX_CLIPS
            )
            frames, clip_stats = await understand_clips(clips, frames, segments)
        except Exception as error:
            if MIMO_VISION_REQUIRED:
                raise
            clip_stats = {"enabled": False, "error": str(error)[:500], "usage": {}}
        finally:
            if clips_dir and clips_dir.parent == task_dir:
                shutil.rmtree(clips_dir, ignore_errors=True)
        await _save_frames(task_id, frames)
        await _set_stage(task_id, STAGES[8][0], STAGES[8][1], STAGES[8][2], f"动态片段分析完成，共 {clip_stats.get('analyzed_clips', 0)} 段", clip_stats)

        await _set_stage(task_id, *STAGES[9])
        try:
            visual_analysis, style_stats = await summarize_video_style(
                frames, segments, duration
            )
        except Exception as error:
            if MIMO_VISION_REQUIRED:
                raise
            visual_analysis, style_stats = await summarize_video_style([], [], duration)
            style_stats["error"] = str(error)[:500]
        visual_analysis["pipeline_stats"] = {
            "shots": shot_stats,
            "images": image_stats,
            "clips": clip_stats,
            "synthesis": style_stats,
        }
        async with async_session() as db:
            task = await db.get(SnapTask, task_id)
            task.visual_analysis_json = json.dumps(visual_analysis, ensure_ascii=False)
            await db.commit()
        await _set_stage(task_id, STAGES[9][0], STAGES[9][1], STAGES[9][2], "整片风格、叙事和分镜画像已生成", {"provider": visual_analysis.get("provider"), **style_stats})

        await _set_stage(task_id, *STAGES[10])
        blocks = _aligned_blocks(frames, segments, duration)
        await _set_stage(task_id, STAGES[10][0], STAGES[10][1], STAGES[10][2], f"完成 {len(blocks)} 个镜头与转写片段对齐", {"alignment_count": len(blocks)})

        await _set_stage(task_id, *STAGES[11])
        try:
            async with async_session() as db:
                task = await db.get(SnapTask, task_id)
                blocks = await asyncio.to_thread(
                    _llm_enhance_sync, blocks, task.note_style
                )
        except Exception:
            pass
        await _set_stage(task_id, STAGES[11][0], STAGES[11][1], STAGES[11][2], "结构化内容生成完成", {"block_count": len(blocks)})

        await _set_stage(task_id, *STAGES[12])
        async with async_session() as db:
            task = await db.get(SnapTask, task_id)
            task.notes_json = json.dumps(blocks, ensure_ascii=False)
            task.final_markdown = _markdown(task, blocks, visual_analysis)
            task.status = "completed"
            task.current_stage = "complete"
            task.progress = 100
            task.updated_at = utcnow()
            await db.commit()
        await sse_manager.emit(task_id, "complete", {
            "stage": "complete",
            "progress": 100,
            "title": "处理完成",
            "message": "转写、关键帧、动态镜头与整片风格分析均已完成",
        })
    except Exception as error:
        async with async_session() as db:
            task = await db.get(SnapTask, task_id)
            if task:
                task.status = "failed"
                task.current_stage = "step_error"
                task.error_message = str(error)[:1000]
                task.updated_at = utcnow()
                await db.commit()
        await sse_manager.emit(task_id, "step_error", {
            "stage": "step_error",
            "message": str(error)[:500],
        })


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
        task.visual_analysis_json = "{}"
        task.final_markdown = ""
        if provider:
            task.asr_provider = provider
        await db.execute(delete(StageResult).where(StageResult.task_id == task_id))
        await db.commit()
    sse_manager.clear(task_id)
    return True
