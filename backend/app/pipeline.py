import asyncio
import json
import math
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from sqlalchemy import delete, select

from .asr import transcribe_audio
from .config import (
    AUDIO_SAMPLE_RATE,
    DEEPSEEK_API_KEY,
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL,
    ENABLE_KNOWLEDGE_AUTO_BUILD,
    ENABLE_PIPELINE_PARALLELISM,
    FFMPEG_BIN,
    FFPROBE_BIN,
    FRAME_COVERAGE_REPAIR_ATTEMPTS,
    MAX_VIDEO_DURATION_SECONDS,
    MIMO_API_KEY,
    MIMO_BASE_URL,
    MIMO_VISION_MAX_ATTEMPTS,
    MIMO_VISION_MAX_CLIPS,
    MIMO_VISION_REQUIRED,
    MIMO_VISION_MODEL,
    MIMO_VISION_TIMEOUT_SECONDS,
    TASKS_DIR,
)
from .database import SnapTask, StageResult, async_session, utcnow
from .mimo_vision import (
    _request_json_sync,
    summarize_video_style,
    understand_clips,
    understand_keyframes,
)
from .knowledge import build_knowledge_asset
from .sse_manager import sse_manager
from .vision import (
    analyze_shots,
    audit_frame_coverage,
    build_semantic_units,
    create_silent_proxy_clips,
    deduplicate_frames,
    extract_keyframes,
    select_coverage_repairs,
    select_shots_for_semantic_coverage,
)


STAGES = [
    ("probing_video", 6, "解析视频", "正在读取视频时长、分辨率与编码信息"),
    ("extracting_audio", 12, "提取音频", "正在生成 16kHz 单声道语音轨"),
    ("transcribing", 28, "语音转写", "正在生成带时间戳的精确转写"),
    ("detecting_frames", 38, "检测镜头", "正在扫描场景变化、运动和画面质量"),
    ("selecting_frames", 46, "选择关键帧", "正在为每个镜头选择清晰稳定的代表画面"),
    ("deduplicating_frames", 51, "关键帧去重", "正在合并重复或高度相似的画面"),
    # 本地 OCR 暂停启用：设备资源有限，关键帧文字由 MiMo 视觉理解返回。
    # 保留 running_ocr 这个阶段名，兼容已有任务状态和前端事件订阅。
    ("running_ocr", 56, "跳过本地 OCR", "直接使用 MiMo 视觉理解关键帧及画面文字"),
    ("understanding_frames", 68, "MiMo 关键帧理解", "正在批量分析主体、场景、构图与素材风格"),
    ("understanding_clips", 76, "MiMo 动态片段理解", "正在补充动作、运镜、转场与节奏信息"),
    (
        "analyzing_style",
        84,
        "AI 整片分析与笔记增强",
        "正在等待 AI 归纳整片风格并增强笔记结构",
    ),
    ("aligning", 89, "图文时间对齐", "正在绑定镜头、视觉语义与语音转写"),
    ("generating_blocks", 94, "生成结构化笔记", "正在生成逐镜头摘要、重点和复习问题"),
    ("generating_note", 98, "生成完整结果", "正在组织 Markdown 与分析产物"),
]

BRANCHES = {
    "common": {"label": "准备视频", "weight": 5},
    "audio": {"label": "音频处理", "weight": 30},
    "vision": {"label": "画面处理", "weight": 25},
    "multimodal": {"label": "多模态理解", "weight": 30},
    "output": {"label": "结果生成", "weight": 10},
}
BRANCH_SUBSTEPS = {
    "vision": [
        ("semantic_selection", "语义覆盖选帧", "等待语音与镜头候选汇合"),
        ("frame_extraction", "关键帧抽取", "等待微语义单元选帧完成"),
        ("coverage_audit", "覆盖审计", "等待检查语义覆盖率和时间空洞"),
        ("coverage_repair", "定向补帧", "仅在存在缺失单元或时间空洞时执行"),
    ],
    "multimodal": [
        ("keyframe_understanding", "关键帧理解", "等待关键帧准备完成"),
        ("clip_understanding", "动态片段理解", "等待关键帧理解完成"),
        ("style_synthesis", "整片分析", "等待静态与动态视觉结果"),
        ("note_enhancement", "笔记增强", "等待图文对齐材料准备完成"),
    ],
}
_progress_locks: dict[str, asyncio.Lock] = {}

_GENERIC_CONTENT_TITLES = {
    "开篇", "开场", "引言", "介绍", "背景", "标准", "分析", "案例",
    "正文", "主体", "展开", "总结", "结尾", "结束", "主要内容", "完整内容",
}


def _clean_content_text(value, maximum: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:maximum]


def _content_focused_title(title, summary, fallback: str) -> str:
    candidate = _clean_content_text(title, 120)
    compact = re.sub(r"[\s:：,，。.!！?？、\-]", "", candidate)
    generic = compact in _GENERIC_CONTENT_TITLES or bool(
        re.fullmatch(r"第?[一二三四五六七八九十\d]+(?:部分|章节|节|段)", compact)
    )
    if candidate and not generic:
        return candidate
    summary_text = _clean_content_text(summary, 300)
    if summary_text:
        return re.split(r"[。！？；\n]", summary_text, maxsplit=1)[0][:30]
    return fallback


def _clean_content_list(value, fallback: list[str], maximum: int = 8) -> list[str]:
    if not isinstance(value, list):
        return fallback
    cleaned = [_clean_content_text(item, 500) for item in value]
    result = list(dict.fromkeys(item for item in cleaned if item))[:maximum]
    return result or fallback


def _merge_enhanced_blocks(base_blocks: list[dict], enhanced_blocks: list[dict]) -> list[dict]:
    """Accept only editable content from the model and preserve source-grounded fields."""
    by_id = {
        str(item.get("id")): item
        for item in enhanced_blocks
        if isinstance(item, dict) and item.get("id")
    }
    has_identifiers = bool(by_id)
    merged = []
    for index, base in enumerate(base_blocks):
        candidate = by_id.get(str(base.get("id")))
        if candidate is None and not has_identifiers and index < len(enhanced_blocks):
            indexed = enhanced_blocks[index]
            candidate = indexed if isinstance(indexed, dict) else {}
        candidate = candidate or {}
        summary = _clean_content_text(candidate.get("summary"), 1200) or base["summary"]
        merged.append({
            **base,
            "summary": summary,
            "title": _content_focused_title(
                candidate.get("title"), summary, base["title"]
            ),
            "key_points": _clean_content_list(
                candidate.get("key_points"), base.get("key_points", []), 8
            ),
            "review_questions": _clean_content_list(
                candidate.get("review_questions"), base.get("review_questions", []), 6
            ),
        })
    return merged


def _video_title(visual_analysis: dict) -> str | None:
    title = _clean_content_text(visual_analysis.get("video_title"), 500)
    return title or None


def new_processing_state() -> dict:
    return {
        name: {
            "label": metadata["label"],
            "stage": "waiting",
            "title": "等待开始",
            "message": "等待上游步骤完成",
            "progress": 0,
            "status": "queued",
            "updated_at": None,
            "stage_started_at": None,
            "substeps": {
                key: {
                    "key": key,
                    "label": label,
                    "status": "queued",
                    "message": message,
                }
                for key, label, message in BRANCH_SUBSTEPS.get(name, [])
            },
        }
        for name, metadata in BRANCHES.items()
    }


def _decode_processing_state(raw: str | None) -> dict:
    try:
        state = json.loads(raw or "{}")
    except json.JSONDecodeError:
        state = {}
    initial = new_processing_state()
    for name in initial:
        if isinstance(state.get(name), dict):
            initial[name].update(state[name])
    return initial


def _overall_progress(state: dict) -> int:
    weighted = sum(
        BRANCHES[name]["weight"] * max(0, min(100, int(state[name]["progress"]))) / 100
        for name in BRANCHES
    )
    return max(3, min(100, int(round(weighted))))


async def _run(command: list[str], timeout: int = 300):
    def execute():
        return subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=True
        )
    return await asyncio.to_thread(execute)


async def _update_pipeline_state(
    task_id: str,
    branch: str,
    stage: str,
    branch_progress: int,
    title: str,
    message: str,
    *,
    result: dict | None = None,
    record_stage: bool = True,
):
    if branch not in BRANCHES:
        raise ValueError(f"Unknown pipeline branch: {branch}")
    lock = _progress_locks.setdefault(task_id, asyncio.Lock())
    async with lock:
        async with async_session() as db:
            task = await db.get(SnapTask, task_id)
            if not task:
                raise RuntimeError("任务已不存在")
            state = _decode_processing_state(task.processing_state_json)
            progress = max(
                int(state[branch].get("progress", 0)),
                max(0, min(100, int(branch_progress))),
            )
            completed = result is not None
            timestamp = utcnow()
            stage_started_at = state[branch].get("stage_started_at")
            if state[branch].get("stage") != stage or not stage_started_at:
                stage_started_at = timestamp.isoformat()
            state[branch].update({
                "stage": stage,
                "title": title,
                "message": message,
                "progress": progress,
                "status": "completed" if completed and progress >= 100 else "running",
                "updated_at": timestamp.isoformat(),
                "stage_started_at": stage_started_at,
            })
            overall = max(int(task.progress or 0), _overall_progress(state))
            task.status = "processing"
            task.current_stage = stage
            task.progress = overall
            task.processing_state_json = json.dumps(state, ensure_ascii=False)
            task.updated_at = utcnow()

            if record_stage:
                if completed:
                    running = (
                        await db.execute(
                            select(StageResult)
                            .where(
                                StageResult.task_id == task_id,
                                StageResult.stage == stage,
                                StageResult.status == "running",
                            )
                            .order_by(StageResult.id.desc())
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                    if running:
                        running.status = "completed"
                        running.result_json = json.dumps(result or {}, ensure_ascii=False)
                        running.completed_at = utcnow()
                    else:
                        db.add(StageResult(
                            task_id=task_id,
                            stage=stage,
                            status="completed",
                            result_json=json.dumps(result or {}, ensure_ascii=False),
                            completed_at=utcnow(),
                        ))
                else:
                    db.add(StageResult(
                        task_id=task_id,
                        stage=stage,
                        status="running",
                        result_json="{}",
                    ))
            await db.commit()
            payload = {
                "branch": branch,
                "stage": stage,
                "progress": overall,
                "branch_progress": progress,
                "title": title,
                "message": message,
                "processing_state": state,
                **(result or {}),
            }
    await sse_manager.emit(task_id, stage, payload)


async def _record_pipeline_substep(
    task_id: str,
    branch: str,
    key: str,
    label: str,
    status: str,
    message: str,
    metadata: dict | None = None,
):
    """Persist independently-running work without changing overall progress.

    Substeps live inside processing_state_json so they are available through the
    existing task API and survive SSE reconnects, page refreshes, and restarts.
    """
    if branch not in BRANCHES:
        raise ValueError(f"Unknown pipeline branch: {branch}")
    if status not in {"running", "completed", "degraded", "failed", "skipped"}:
        raise ValueError(f"Unknown pipeline substep status: {status}")
    lock = _progress_locks.setdefault(task_id, asyncio.Lock())
    async with lock:
        async with async_session() as db:
            task = await db.get(SnapTask, task_id)
            if not task:
                raise RuntimeError("任务已不存在")
            state = _decode_processing_state(task.processing_state_json)
            branch_state = state[branch]
            substeps = dict(branch_state.get("substeps") or {})
            previous = dict(substeps.get(key) or {})
            timestamp = utcnow()
            started_at = previous.get("started_at") or timestamp.isoformat()
            row = {
                **previous,
                "key": key,
                "label": label,
                "status": status,
                "message": message,
                "started_at": started_at,
                "updated_at": timestamp.isoformat(),
            }
            if metadata is not None:
                row["metadata"] = metadata
            if status != "running":
                row["completed_at"] = timestamp.isoformat()
                try:
                    started = datetime.fromisoformat(started_at)
                    row["elapsed_seconds"] = round(
                        max(0.0, (timestamp - started).total_seconds()), 3
                    )
                except (TypeError, ValueError):
                    row["elapsed_seconds"] = 0
            substeps[key] = row
            branch_state["substeps"] = substeps
            branch_state["updated_at"] = timestamp.isoformat()
            task.processing_state_json = json.dumps(state, ensure_ascii=False)
            task.updated_at = timestamp

            stage_name = f"{branch}.{key}"
            stage_payload = json.dumps({
                "label": label,
                "message": message,
                "elapsed_seconds": row.get("elapsed_seconds"),
                "metadata": metadata or {},
            }, ensure_ascii=False)
            if status == "running":
                db.add(StageResult(
                    task_id=task_id,
                    stage=stage_name,
                    status="running",
                    result_json=stage_payload,
                ))
            else:
                running = (
                    await db.execute(
                        select(StageResult)
                        .where(
                            StageResult.task_id == task_id,
                            StageResult.stage == stage_name,
                            StageResult.status == "running",
                        )
                        .order_by(StageResult.id.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                error_message = message if status in {"failed", "degraded"} else None
                if running:
                    running.status = status
                    running.result_json = stage_payload
                    running.error_message = error_message
                    running.completed_at = timestamp
                else:
                    db.add(StageResult(
                        task_id=task_id,
                        stage=stage_name,
                        status=status,
                        result_json=stage_payload,
                        error_message=error_message,
                        completed_at=timestamp,
                    ))
            await db.commit()
            event_name = branch_state.get("stage") or "pipeline_detail"
            payload = {
                "branch": branch,
                "stage": event_name,
                "substep": row,
                "processing_state": state,
            }
    await sse_manager.emit(task_id, event_name, payload)


async def _set_stage(
    task_id: str,
    stage_info: tuple,
    branch: str,
    branch_progress: int,
    message: str | None = None,
    result: dict | None = None,
):
    stage, _, title, default_message = stage_info
    await _update_pipeline_state(
        task_id,
        branch,
        stage,
        branch_progress,
        title,
        message or default_message,
        result=result,
    )


async def _report_branch_progress(
    task_id: str,
    stage_info: tuple,
    branch: str,
    branch_progress: int,
    message: str,
):
    stage, _, title, _ = stage_info
    await _update_pipeline_state(
        task_id,
        branch,
        stage,
        branch_progress,
        title,
        message,
        record_stage=False,
    )


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
    """Legacy local OCR fallback; intentionally not called in the main pipeline.

    MiMo now receives the original keyframe and returns ``visible_text``. Keep
    this implementation for a future offline/high-accuracy OCR option without
    paying the local PaddleOCR cost on the default path.
    """
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
        ocr_lines = (frame.get("ocr_text") or "").splitlines()
        title = (
            visual.get("title")
            or (ocr_lines[0][:80] if ocr_lines else "")
            or f"关键镜头 {index + 1}"
        )
        visual_summary = visual.get("summary") or visual.get("description") or ""
        motion_summary = motion.get("motion") or ""
        summary_parts = [part for part in (visual_summary, motion_summary, transcript) if part]
        summary = "；".join(summary_parts)[:900] or "该片段暂无可用语音或视觉摘要。"
        title = _content_focused_title(title, summary, f"关键镜头 {index + 1}")
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
            "review_questions": [f"{title} 的核心内容和关键依据是什么？"],
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


def _llm_enhance_sync(blocks: list[dict], note_style: str, note_model: str):
    if note_model != "deepseek":
        api_key = MIMO_API_KEY
        base_url = MIMO_BASE_URL
        model = MIMO_VISION_MODEL
        headers = {"api-key": api_key, "Content-Type": "application/json"}
        provider = "MiMo"
        attempts = MIMO_VISION_MAX_ATTEMPTS
        timeout = MIMO_VISION_TIMEOUT_SECONDS
    else:
        api_key = DEEPSEEK_API_KEY
        base_url = DEEPSEEK_BASE_URL
        model = DEEPSEEK_MODEL
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        provider = "DeepSeek"
        attempts = 2
        timeout = 120
    if not api_key:
        return blocks, {
            "enabled": False,
            "model": model,
            "reason": "missing_api_key",
        }
    prompt = (
        "将以下已按镜头对齐的视频材料整理为 JSON 对象 {\"blocks\": [...]}。"
        "必须保留每项 id、frame_id、timestamp、end_time、image_url、ocr_text、confidence、"
        "visual_analysis、clip_analysis、shot_metrics，仅改进 title、summary、key_points 和"
        "review_questions。先准确生成 summary 和 key_points，再根据它们提炼 title。"
        "title 必须突出当前片段具体讨论的对象、方法、观点或结论，让用户只看标题就知道本段内容；"
        "建议 8 到 20 个汉字。禁止单独使用开篇、介绍、背景、标准、分析、案例、总结、结尾、"
        "主要内容、第几部分等结构性词语。相邻标题不得仅有序号差异。"
        "review_questions 必须检验内容理解，不得只询问片段的叙事作用。"
        "不得添加输入材料中不存在的事实。"
        f"笔记类型：{note_style}\n"
        + json.dumps(blocks, ensure_ascii=False)[:60000]
    )
    parsed, usage = _request_json_sync(
        [
            {"role": "system", "content": "你是严谨的视频内容编辑，只输出 JSON。"},
            {"role": "user", "content": prompt},
        ],
        "note enhancement",
        5000,
        api_key=api_key,
        base_url=base_url,
        model=model,
        request_headers=headers,
        timeout=timeout,
        max_attempts=attempts,
    )
    enhanced = parsed.get("blocks", []) if isinstance(parsed, dict) else []
    if not isinstance(enhanced, list) or not enhanced:
        raise RuntimeError(f"{provider} note enhancement returned no blocks")
    return _merge_enhanced_blocks(blocks, enhanced), {
        "enabled": True,
        "model": model,
        "provider": provider,
        "usage": usage,
    }


def _markdown(task: SnapTask, blocks: list[dict], visual_analysis: dict):
    overall = (
        visual_analysis.get("content_summary")
        or visual_analysis.get("summary")
        or "视频已完成镜头、视觉语义与语音内容对齐。"
    )
    document_title = (
        task.generated_title
        or visual_analysis.get("video_title")
        or Path(task.filename).stem
    )
    lines = [
        f"# {document_title}",
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


async def _run_audio_branch(
    task_id: str,
    video_path: str,
    audio_path: Path,
    duration: float,
    asr_provider: str,
):
    await _set_stage(task_id, STAGES[1], "audio", 0)
    await _extract_audio(video_path, audio_path, duration)
    async with async_session() as db:
        task = await db.get(SnapTask, task_id)
        if task:
            task.audio_path = str(audio_path)
            await db.commit()
    await _set_stage(
        task_id,
        STAGES[1],
        "audio",
        20,
        "音频提取完成",
        {"sample_rate": AUDIO_SAMPLE_RATE},
    )

    await _set_stage(task_id, STAGES[2], "audio", 20)

    async def report_asr_progress(payload: dict):
        stage_progress = max(0, min(100, int(payload.get("progress_pct", 0))))
        await _report_branch_progress(
            task_id,
            STAGES[2],
            "audio",
            20 + round(stage_progress * 0.8),
            payload.get("detail", "正在执行语音转写"),
        )

    segments, engine = await transcribe_audio(
        audio_path, duration, asr_provider, report_asr_progress
    )
    async with async_session() as db:
        task = await db.get(SnapTask, task_id)
        if task:
            task.transcripts_json = json.dumps(segments, ensure_ascii=False)
            await db.commit()
    await _set_stage(
        task_id,
        STAGES[2],
        "audio",
        100,
        f"转写完成，共 {len(segments)} 个片段",
        {"segment_count": len(segments), "engine": engine},
    )
    return segments, engine


async def _run_local_vision_branch(
    task_id: str,
    video_path: str,
    duration: float,
):
    await _set_stage(task_id, STAGES[3], "vision", 0)
    shots, shot_stats = await analyze_shots(video_path, duration)
    await _set_stage(
        task_id,
        STAGES[3],
        "vision",
        45,
        f"检测到 {shot_stats['detected_shots']} 个候选镜头，等待语音语义选帧",
        shot_stats,
    )
    return shots, shot_stats


async def _select_extract_and_repair_frames(
    task_id: str,
    video_path: str,
    duration: float,
    frames_dir: Path,
    shot_candidates: list[dict],
    segments: list[dict],
):
    """Select frames after the ASR/vision join and repair uncovered ranges."""
    await _set_stage(task_id, STAGES[4], "vision", 45)
    await _record_pipeline_substep(
        task_id,
        "vision",
        "semantic_selection",
        "语义覆盖选帧",
        "running",
        "正在根据转写边界与场景变化建立微语义单元",
        {"candidate_shots": len(shot_candidates), "transcript_segments": len(segments)},
    )
    units = build_semantic_units(segments, shot_candidates, duration)
    selected_shots, selection_stats = select_shots_for_semantic_coverage(
        shot_candidates, units, duration
    )
    await _record_pipeline_substep(
        task_id,
        "vision",
        "semantic_selection",
        "语义覆盖选帧",
        "completed",
        f"已从 {len(shot_candidates)} 个候选中选择 {len(selected_shots)} 个语义锚点",
        selection_stats,
    )
    await _record_pipeline_substep(
        task_id,
        "vision",
        "frame_extraction",
        "关键帧抽取",
        "running",
        f"正在抽取 {len(selected_shots)} 张关键帧",
        {"selected_shots": len(selected_shots)},
    )
    frames = await extract_keyframes(video_path, selected_shots, frames_dir)
    if not frames:
        raise RuntimeError("未能提取任何关键帧")
    await _record_pipeline_substep(
        task_id,
        "vision",
        "frame_extraction",
        "关键帧抽取",
        "completed",
        f"成功抽取 {len(frames)} / {len(selected_shots)} 张关键帧",
        {
            "selected_shots": len(selected_shots),
            "extracted_frames": len(frames),
            "failed_extractions": len(selected_shots) - len(frames),
        },
    )
    await _set_stage(
        task_id,
        STAGES[4],
        "vision",
        75,
        f"按 {len(units)} 个微语义单元提取 {len(frames)} 张关键帧",
        {
            "frame_count": len(frames),
            "semantic_unit_count": len(units),
            **selection_stats,
        },
    )

    await _set_stage(task_id, STAGES[5], "vision", 75)
    await _record_pipeline_substep(
        task_id,
        "vision",
        "coverage_audit",
        "覆盖审计",
        "running",
        "正在执行语义保护去重并检查覆盖空洞",
        {"frame_count": len(frames), "semantic_unit_count": len(units)},
    )
    frames = await asyncio.to_thread(deduplicate_frames, frames, frames_dir)
    initial_coverage = audit_frame_coverage(frames, units, duration)
    await _record_pipeline_substep(
        task_id,
        "vision",
        "coverage_audit",
        "覆盖审计",
        "completed",
        (
            f"语义覆盖率 {initial_coverage['semantic_coverage_ratio']:.0%}，"
            f"最大空洞 {initial_coverage['max_frame_gap_seconds']:.1f} 秒"
        ),
        initial_coverage,
    )
    attempted_shot_ids = {
        str(shot.get("shot_id")) for shot in selected_shots if shot.get("shot_id")
    }
    next_frame_index = len(selected_shots) + 1
    repair_rounds = 0
    repair_count = 0
    maximum_repair_rounds = max(0, int(FRAME_COVERAGE_REPAIR_ATTEMPTS))
    repairs = select_coverage_repairs(
        frames,
        shot_candidates,
        units,
        duration,
        attempted_shot_ids,
    ) if maximum_repair_rounds else []
    if repairs:
        await _record_pipeline_substep(
            task_id,
            "vision",
            "coverage_repair",
            "定向补帧",
            "running",
            f"发现 {len(repairs)} 个缺失位置，开始第 1 轮定向补帧",
            {"repair_round": 1, "planned_repairs": len(repairs)},
        )
    for attempt in range(maximum_repair_rounds):
        if attempt:
            repairs = select_coverage_repairs(
                frames,
                shot_candidates,
                units,
                duration,
                attempted_shot_ids,
            )
        if not repairs:
            break
        await _set_stage(
            task_id,
            STAGES[5],
            "vision",
            min(96, 82 + round((attempt + 1) / max(1, maximum_repair_rounds) * 12)),
            f"正在进行第 {attempt + 1} / {maximum_repair_rounds} 轮定向补帧",
        )
        repair_rounds += 1
        repair_count += len(repairs)
        attempted_shot_ids.update(
            str(shot.get("shot_id")) for shot in repairs if shot.get("shot_id")
        )
        repaired_frames = await extract_keyframes(
            video_path,
            repairs,
            frames_dir,
            start_index=next_frame_index,
        )
        next_frame_index += len(repairs)
        frames.extend(repaired_frames)
        frames.sort(key=lambda frame: float(frame.get("timestamp", 0)))
        frames = await asyncio.to_thread(deduplicate_frames, frames, frames_dir)

    final_coverage = audit_frame_coverage(frames, units, duration)
    if repair_rounds:
        await _record_pipeline_substep(
            task_id,
            "vision",
            "coverage_repair",
            "定向补帧",
            "completed",
            (
                f"完成 {repair_rounds} 轮、{repair_count} 个位置的定向补帧；"
                f"最终覆盖率 {final_coverage['semantic_coverage_ratio']:.0%}"
            ),
            {
                **final_coverage,
                "coverage_repair_rounds": repair_rounds,
                "repair_candidates": repair_count,
            },
        )
    else:
        await _record_pipeline_substep(
            task_id,
            "vision",
            "coverage_repair",
            "定向补帧",
            "skipped",
            (
                "定向补帧未启用"
                if not maximum_repair_rounds
                else "覆盖检查未发现可执行的补帧位置"
            ),
            final_coverage,
        )

    coverage_stats = {
        **selection_stats,
        **final_coverage,
        "extracted_frames": len(frames),
        "coverage_repair_rounds": repair_rounds,
    }
    await _save_frames(task_id, frames)
    await _set_stage(
        task_id,
        STAGES[5],
        "vision",
        100,
        (
            f"保留 {len(frames)} 张关键帧，"
            f"语义覆盖率 {coverage_stats['semantic_coverage_ratio']:.0%}，"
            f"最大空洞 {coverage_stats['max_frame_gap_seconds']:.1f} 秒"
        ),
        {"frame_count": len(frames), **coverage_stats},
    )
    return frames, coverage_stats


async def _gather_required(*coroutines):
    tasks = [asyncio.create_task(coroutine) for coroutine in coroutines]
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


def _merge_frame_results(
    frames: list[dict], image_frames: list[dict], ocr_frames: list[dict]
) -> list[dict]:
    image_by_id = {str(frame["id"]): frame for frame in image_frames}
    ocr_by_id = {str(frame["id"]): frame for frame in ocr_frames}
    merged = []
    for frame in frames:
        frame_id = str(frame["id"])
        row = {**frame, **image_by_id.get(frame_id, {})}
        local_ocr = ocr_by_id.get(frame_id, {}).get("ocr_text")
        if local_ocr:
            row["ocr_text"] = local_ocr
        merged.append(row)
    return merged


async def _run_multimodal_branch(
    task_id: str,
    video_path: str,
    frames: list[dict],
    frames_dir: Path,
    clips_dir: Path,
    segments: list[dict],
    duration: float,
    note_style: str,
    note_model: str,
):
    # 本地 PaddleOCR 暂停执行。MiMo 直接看关键帧，并在 visual_analysis.visible_text
    # 中返回画面文字；_ocr_frames 保留在上方，供未来离线/高精度模式恢复。
    await _set_stage(
        task_id,
        STAGES[6],
        "multimodal",
        100,
        "已跳过本地 OCR，直接使用 MiMo 视觉理解",
        {"enabled": False, "reason": "disabled_for_local_performance"},
    )
    await _set_stage(
        task_id,
        STAGES[7],
        "multimodal",
        0,
        "正在直接使用 MiMo 理解关键帧",
    )

    async def report_vision_progress(payload: dict):
        completed = int(payload.get("completed", 0))
        total = max(1, int(payload.get("total", 1)))
        await _report_branch_progress(
            task_id,
            STAGES[7],
            "multimodal",
            round(completed / total * 40),
            payload.get("detail", STAGES[7][3]),
        )

    async def run_image_understanding():
        try:
            image_frames, image_stats = await understand_keyframes(
                [dict(frame) for frame in frames],
                frames_dir,
                segments,
                report_vision_progress,
            )
            if MIMO_VISION_REQUIRED and not image_stats.get("enabled"):
                raise RuntimeError(
                    f"MiMo 视觉理解不可用：{image_stats.get('reason', 'unknown')}"
                )
            return image_frames, image_stats
        except Exception as error:
            if MIMO_VISION_REQUIRED:
                raise
            return [dict(frame) for frame in frames], {
                "enabled": False,
                "error": str(error)[:500],
                "usage": {},
            }

    await _record_pipeline_substep(
        task_id,
        "multimodal",
        "keyframe_understanding",
        "关键帧理解",
        "running",
        f"正在分析 {len(frames)} 张关键帧",
        {"model": MIMO_VISION_MODEL, "frame_count": len(frames)},
    )
    try:
        image_result = await run_image_understanding()
    except Exception as error:
        await _record_pipeline_substep(
            task_id,
            "multimodal",
            "keyframe_understanding",
            "关键帧理解",
            "failed",
            str(error)[:500],
            {"model": MIMO_VISION_MODEL, "error": str(error)[:500]},
        )
        raise
    image_frames, image_stats = image_result
    image_status = (
        "completed" if image_stats.get("enabled")
        else "degraded" if image_stats.get("error")
        else "skipped"
    )
    await _record_pipeline_substep(
        task_id,
        "multimodal",
        "keyframe_understanding",
        "关键帧理解",
        image_status,
        (
            f"完成 {image_stats.get('analyzed_frames', 0)} 张关键帧分析"
            if image_status == "completed"
            else image_stats.get("error") or "视觉理解未启用，保留本地关键帧"
        ),
        image_stats,
    )
    # 第三个参数保留为空，确保合并逻辑兼容旧的 OCR 字段；ocr_text 由 MiMo
    # 的 visible_text 回填，详见 mimo_vision.understand_keyframes。
    frames = _merge_frame_results(frames, image_frames, [])
    await _save_frames(task_id, frames)
    await _set_stage(
        task_id,
        STAGES[7],
        "multimodal",
        45,
        f"已理解 {image_stats.get('analyzed_frames', 0)} 张关键帧",
        image_stats,
    )

    await _set_stage(task_id, STAGES[8], "multimodal", 45)
    clip_stats = {"enabled": False, "clip_count": 0, "usage": {}}
    await _record_pipeline_substep(
        task_id,
        "multimodal",
        "clip_understanding",
        "动态片段理解",
        "running",
        "正在生成并分析动态代理片段",
        {"model": MIMO_VISION_MODEL, "max_clips": MIMO_VISION_MAX_CLIPS},
    )
    try:
        clips = await create_silent_proxy_clips(
            video_path, frames, clips_dir, MIMO_VISION_MAX_CLIPS
        )
        frames, clip_stats = await understand_clips(clips, frames, segments)
    except Exception as error:
        if MIMO_VISION_REQUIRED:
            await _record_pipeline_substep(
                task_id,
                "multimodal",
                "clip_understanding",
                "动态片段理解",
                "failed",
                str(error)[:500],
                {"model": MIMO_VISION_MODEL, "error": str(error)[:500]},
            )
            raise
        clip_stats = {"enabled": False, "error": str(error)[:500], "usage": {}}
    finally:
        if clips_dir.parent == frames_dir.parent:
            shutil.rmtree(clips_dir, ignore_errors=True)
    clip_status = (
        "completed" if clip_stats.get("enabled")
        else "degraded" if clip_stats.get("error")
        else "skipped"
    )
    await _record_pipeline_substep(
        task_id,
        "multimodal",
        "clip_understanding",
        "动态片段理解",
        clip_status,
        (
            f"完成 {clip_stats.get('analyzed_clips', 0)} 个动态片段分析"
            if clip_status == "completed"
            else clip_stats.get("error") or "没有需要补充分析的动态片段"
        ),
        clip_stats,
    )
    await _save_frames(task_id, frames)
    await _set_stage(
        task_id,
        STAGES[8],
        "multimodal",
        70,
        f"动态片段分析完成，共 {clip_stats.get('analyzed_clips', 0)} 段",
        clip_stats,
    )

    # 逐镜头块只依赖已完成的静态/动态分析和转写，因此可以和整片风格
    # 归纳同时执行。两个分支各自负责 fallback，避免一个模型失败拖垮另一个。
    await _set_stage(
        task_id,
        STAGES[9],
        "multimodal",
        70,
        "正在等待 AI 整片分析与笔记增强；模型响应和重试可能需要几分钟",
    )
    base_blocks = _aligned_blocks(frames, segments, duration)

    async def run_style_synthesis():
        await _record_pipeline_substep(
            task_id,
            "multimodal",
            "style_synthesis",
            "整片分析",
            "running",
            "正在归纳整片内容、风格和叙事结构",
            {"model": MIMO_VISION_MODEL},
        )
        try:
            result = await summarize_video_style(frames, segments, duration)
            analysis, stats = result
            status = "completed" if stats.get("enabled") else "skipped"
            await _record_pipeline_substep(
                task_id,
                "multimodal",
                "style_synthesis",
                "整片分析",
                status,
                "整片内容、风格和叙事结构已生成" if status == "completed" else "整片 AI 分析未启用，已使用本地摘要",
                stats,
            )
            return analysis, stats
        except Exception as error:
            if MIMO_VISION_REQUIRED:
                await _record_pipeline_substep(
                    task_id,
                    "multimodal",
                    "style_synthesis",
                    "整片分析",
                    "failed",
                    str(error)[:500],
                    {"model": MIMO_VISION_MODEL, "error": str(error)[:500]},
                )
                raise
            fallback = await summarize_video_style([], [], duration)
            fallback[1]["error"] = str(error)[:500]
            await _record_pipeline_substep(
                task_id,
                "multimodal",
                "style_synthesis",
                "整片分析",
                "degraded",
                "整片 AI 分析失败，已使用本地摘要",
                fallback[1],
            )
            return fallback

    async def run_note_enhancement():
        model = MIMO_VISION_MODEL if note_model != "deepseek" else DEEPSEEK_MODEL
        provider = "MiMo" if note_model != "deepseek" else "DeepSeek"
        await _record_pipeline_substep(
            task_id,
            "multimodal",
            "note_enhancement",
            "笔记增强",
            "running",
            "正在增强标题、摘要、重点和复习问题",
            {"model": model, "provider": provider},
        )
        try:
            enhanced, stats = await asyncio.to_thread(
                _llm_enhance_sync, base_blocks, note_style, note_model
            )
            status = "completed" if stats.get("enabled") else "skipped"
            await _record_pipeline_substep(
                task_id,
                "multimodal",
                "note_enhancement",
                "笔记增强",
                status,
                "笔记标题、摘要和复习内容已增强" if status == "completed" else "笔记增强未启用，已保留基础对齐结果",
                stats,
            )
            return enhanced, stats
        except Exception as error:
            stats = {
                "enabled": False,
                "model": model,
                "provider": provider,
                "error": str(error)[:500],
                "fallback": "aligned_blocks",
            }
            await _record_pipeline_substep(
                task_id,
                "multimodal",
                "note_enhancement",
                "笔记增强",
                "degraded",
                "笔记增强失败，已保留基础对齐结果",
                stats,
            )
            return base_blocks, stats

    (visual_analysis, style_stats), (note_blocks, note_stats) = await asyncio.gather(
        run_style_synthesis(),
        run_note_enhancement(),
    )
    visual_analysis["pipeline_stats"] = {
        "images": image_stats,
        "clips": clip_stats,
        "synthesis": style_stats,
        "note_enhancement": note_stats,
    }
    async with async_session() as db:
        task = await db.get(SnapTask, task_id)
        if task:
            task.visual_analysis_json = json.dumps(visual_analysis, ensure_ascii=False)
            task.generated_title = _video_title(visual_analysis)
            await db.commit()
    await _set_stage(
        task_id,
        STAGES[9],
        "multimodal",
        100,
        "AI 整片分析与笔记增强已完成",
        {"provider": visual_analysis.get("provider"), **style_stats},
    )
    return frames, visual_analysis, image_stats, clip_stats, style_stats, note_blocks, note_stats


async def _mark_pipeline_failed(task_id: str, error: Exception):
    lock = _progress_locks.setdefault(task_id, asyncio.Lock())
    async with lock:
        async with async_session() as db:
            task = await db.get(SnapTask, task_id)
            if not task:
                return
            state = _decode_processing_state(task.processing_state_json)
            failed_at = utcnow()
            timestamp = failed_at.isoformat()
            for branch in state.values():
                if branch.get("status") == "running":
                    branch["status"] = "failed"
                    branch["message"] = str(error)[:500]
                    branch["updated_at"] = timestamp
                for substep in (branch.get("substeps") or {}).values():
                    if substep.get("status") != "running":
                        continue
                    substep["status"] = "failed"
                    substep["message"] = str(error)[:500]
                    substep["updated_at"] = timestamp
                    substep["completed_at"] = timestamp
                    try:
                        started = datetime.fromisoformat(substep.get("started_at", ""))
                        substep["elapsed_seconds"] = round(
                            max(0.0, (failed_at - started).total_seconds()), 3
                        )
                    except (TypeError, ValueError):
                        substep["elapsed_seconds"] = 0
            running_results = (
                await db.execute(
                    select(StageResult).where(
                        StageResult.task_id == task_id,
                        StageResult.status == "running",
                    )
                )
            ).scalars().all()
            for stage_result in running_results:
                stage_result.status = "failed"
                stage_result.error_message = str(error)[:1000]
                stage_result.completed_at = failed_at
            task.status = "failed"
            task.current_stage = "step_error"
            task.error_message = str(error)[:1000]
            task.processing_state_json = json.dumps(state, ensure_ascii=False)
            task.updated_at = utcnow()
            await db.commit()
            payload = {
                "stage": "step_error",
                "message": str(error)[:500],
                "progress": task.progress,
                "processing_state": state,
            }
    await sse_manager.emit(task_id, "step_error", payload)


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
            note_style = task.note_style
            note_model = task.note_model

        await _set_stage(task_id, STAGES[0], "common", 20)
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
        await _set_stage(
            task_id, STAGES[0], "common", 100, "视频解析完成", metadata
        )

        audio_branch = _run_audio_branch(
            task_id, video_path, audio_path, duration, asr_provider
        )
        vision_branch = _run_local_vision_branch(
            task_id, video_path, duration
        )
        if ENABLE_PIPELINE_PARALLELISM:
            audio_result, vision_result = await _gather_required(
                audio_branch, vision_branch
            )
        else:
            audio_result = await audio_branch
            vision_result = await vision_branch
        segments, engine = audio_result
        shot_candidates, shot_stats = vision_result
        frames, coverage_stats = await _select_extract_and_repair_frames(
            task_id,
            video_path,
            duration,
            frames_dir,
            shot_candidates,
            segments,
        )
        shot_stats = {**shot_stats, **coverage_stats}

        (
            frames,
            visual_analysis,
            image_stats,
            clip_stats,
            style_stats,
            note_blocks,
            note_stats,
        ) = await _run_multimodal_branch(
            task_id,
            video_path,
            frames,
            frames_dir,
            clips_dir,
            segments,
            duration,
            note_style,
            note_model,
        )
        visual_analysis["pipeline_stats"] = {
            "shots": shot_stats,
            "images": image_stats,
            "clips": clip_stats,
            "synthesis": style_stats,
            "note_enhancement": note_stats,
        }
        async with async_session() as db:
            task = await db.get(SnapTask, task_id)
            task.visual_analysis_json = json.dumps(visual_analysis, ensure_ascii=False)
            task.generated_title = _video_title(visual_analysis)
            await db.commit()

        await _set_stage(task_id, STAGES[10], "output", 0)
        blocks = note_blocks
        await _set_stage(
            task_id,
            STAGES[10],
            "output",
            25,
            f"完成 {len(blocks)} 个镜头与转写片段对齐",
            {"alignment_count": len(blocks)},
        )

        await _set_stage(task_id, STAGES[11], "output", 25)
        await _set_stage(
            task_id,
            STAGES[11],
            "output",
            75,
            "结构化内容生成完成",
            {"block_count": len(blocks)},
        )

        await _set_stage(task_id, STAGES[12], "output", 75)
        async with async_session() as db:
            task = await db.get(SnapTask, task_id)
            task.notes_json = json.dumps(blocks, ensure_ascii=False)
            task.final_markdown = _markdown(task, blocks, visual_analysis)
            task.updated_at = utcnow()
            await db.commit()
        await _set_stage(
            task_id,
            STAGES[12],
            "output",
            100,
            "完整结果已生成",
            {"block_count": len(blocks)},
        )
        async with async_session() as db:
            task = await db.get(SnapTask, task_id)
            if task:
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
        if ENABLE_KNOWLEDGE_AUTO_BUILD:
            # Knowledge is derived and isolated: a failure here must never turn a
            # successfully processed source task into a failed pipeline task.
            try:
                await build_knowledge_asset(task_id, trigger="automatic")
            except Exception:
                pass
    except Exception as error:
        await _mark_pipeline_failed(task_id, error)
    finally:
        _progress_locks.pop(task_id, None)


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
        task.generated_title = None
        task.processing_state_json = json.dumps(new_processing_state(), ensure_ascii=False)
        task.final_markdown = ""
        if provider:
            task.asr_provider = provider
        await db.execute(delete(StageResult).where(StageResult.task_id == task_id))
        await db.commit()
    sse_manager.clear(task_id)
    return True
