import asyncio
import base64
import http.client
import json
import random
import re
import time
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Awaitable, Callable
from pathlib import Path

from .config import (
    ENABLE_MIMO_VISION,
    MIMO_API_KEY,
    MIMO_BASE_URL,
    MIMO_VISION_CONCURRENCY,
    MIMO_VISION_IMAGE_BATCH_SIZE,
    MIMO_VISION_MAX_ATTEMPTS,
    MIMO_VISION_MODEL,
    MIMO_VISION_TIMEOUT_SECONDS,
    MIMO_VISION_VIDEO_FPS,
)


ProgressCallback = Callable[[dict], Awaitable[None]]


def _message_content(message: dict) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(item.get("text", "")) for item in content if isinstance(item, dict)
        )
    return str(content)


def _parse_json_content(content: str) -> dict:
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        text = text.rsplit("```", 1)[0].strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        left, right = text.find("{"), text.rfind("}")
        if left < 0 or right <= left:
            raise RuntimeError(f"MiMo returned non-JSON content: {text[:300]}")
        parsed = json.loads(text[left:right + 1])
    if not isinstance(parsed, dict):
        raise RuntimeError("MiMo structured output must be a JSON object")
    return parsed


def _retry_delay(attempt: int, headers=None) -> float:
    retry_after = headers.get("Retry-After") if headers else None
    try:
        base = float(retry_after) if retry_after else 2 ** (attempt - 1)
    except (TypeError, ValueError):
        base = 2 ** (attempt - 1)
    return min(30.0, max(1.0, base) + random.uniform(0.0, 0.5))


def _request_json_sync(
    messages: list[dict],
    purpose: str,
    max_tokens: int = 5000,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    request_headers: dict | None = None,
    timeout: int | None = None,
    max_attempts: int | None = None,
):
    # Resolve module configuration at call time so tests and long-running
    # processes can override provider settings without stale import-time values.
    api_key = MIMO_API_KEY if api_key is None else api_key
    base_url = MIMO_BASE_URL if base_url is None else base_url
    model = MIMO_VISION_MODEL if model is None else model
    timeout = MIMO_VISION_TIMEOUT_SECONDS if timeout is None else timeout
    max_attempts = MIMO_VISION_MAX_ATTEMPTS if max_attempts is None else max_attempts
    if not api_key:
        raise RuntimeError("MIMO_API_KEY is not set. Add it to backend/.env.")
    body = json.dumps({
        "model": model,
        "messages": messages,
        "response_format": {"type": "json_object"},
        "temperature": 0.1,
        "max_completion_tokens": max_tokens,
    }, ensure_ascii=False).encode("utf-8")
    attempts = max(1, int(max_attempts))
    timeout = max(30, int(timeout))
    headers = request_headers or {"api-key": api_key, "Content-Type": "application/json"}
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(
            f"{base_url}/chat/completions",
            data=body,
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            content = _message_content(payload["choices"][0]["message"])
            usage = dict(payload.get("usage", {}) or {})
            usage["request_count"] = 1
            usage["request_attempts"] = attempt
            return _parse_json_content(content), usage
        except urllib.error.HTTPError as error:
            response_body = error.read().decode("utf-8", errors="replace")
            if error.code not in {408, 429, 500, 502, 503, 504} or attempt == attempts:
                raise RuntimeError(
                    f"MiMo {purpose} HTTP {error.code}: {response_body[:500]}"
                ) from error
            time.sleep(_retry_delay(attempt, error.headers))
        except (
            http.client.RemoteDisconnected,
            ConnectionResetError,
            TimeoutError,
            urllib.error.URLError,
        ) as error:
            if attempt == attempts:
                raise RuntimeError(
                    f"MiMo {purpose} connection failed after {attempts} attempts: {error}"
                ) from error
            time.sleep(_retry_delay(attempt))
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
            if attempt == attempts:
                raise RuntimeError(f"Unexpected model {purpose} response") from error
            time.sleep(_retry_delay(attempt))
        except RuntimeError as error:
            if attempt == attempts:
                raise
            time.sleep(_retry_delay(attempt))
    raise RuntimeError(f"MiMo {purpose} request failed")


def _data_url(path: Path, media_type: str) -> str:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{media_type};base64,{encoded}"


def _transcript_excerpt(segments: list[dict], start: float, end: float, limit: int = 1600):
    text = "".join(
        str(item.get("text", ""))
        for item in segments
        if float(item.get("end", 0)) >= start - 3
        and float(item.get("start", 0)) <= end + 3
    ).strip()
    return text[:limit]


def _usage_total(rows: list[dict]) -> dict:
    result = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "request_count": 0,
        "request_attempts": 0,
    }
    for row in rows:
        for key in result:
            result[key] += int(row.get(key, 0) or 0)
    return result


def _clean_string_list(value, maximum: int = 12) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip()[:160] for item in value if str(item).strip()][:maximum]


_GENERIC_SECTION_TITLES = {
    "开篇", "开场", "引言", "介绍", "背景", "标准", "分析", "案例",
    "正文", "主体", "展开", "总结", "结尾", "结束", "主要内容", "完整内容",
}
_GENERIC_VIDEO_TITLES = {
    "未命名视频", "视频内容", "课程介绍", "会议记录", "精彩分享", "视频总结",
}


def _clean_text(value, maximum: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:maximum]


def _is_generic_section_title(value: str) -> bool:
    compact = re.sub(r"[\s:：,，。.!！?？、\-]", "", value)
    return compact in _GENERIC_SECTION_TITLES or bool(
        re.fullmatch(r"第?[一二三四五六七八九十\d]+(?:部分|章节|节|段)", compact)
    )


def _content_title(title, summary, role, index: int) -> str:
    candidate = _clean_text(title, 80)
    if candidate and not _is_generic_section_title(candidate):
        return candidate
    summary_text = _clean_text(summary, 300)
    if summary_text:
        return re.split(r"[。！？；\n]", summary_text, maxsplit=1)[0][:30]
    role_text = _clean_text(role, 30)
    return role_text or f"章节 {index + 1}"


def _normalize_video_analysis(parsed: dict, fallback: dict | None = None) -> dict:
    """Normalize content-first fields while keeping legacy consumers working."""
    source = parsed if isinstance(parsed, dict) else {}
    fallback = fallback or {}
    analysis = {**fallback, **source}
    content_summary = _clean_text(
        source.get("content_summary")
        or source.get("summary")
        or fallback.get("content_summary")
        or fallback.get("summary"),
        4000,
    )
    visual_summary = _clean_text(
        source.get("visual_summary") or fallback.get("visual_summary"), 2000
    )
    video_title = _clean_text(
        source.get("video_title") or fallback.get("video_title"), 500
    )
    if video_title in _GENERIC_VIDEO_TITLES:
        video_title = ""
    if content_summary:
        analysis["content_summary"] = content_summary
        analysis["summary"] = content_summary
    if visual_summary:
        analysis["visual_summary"] = visual_summary
    if video_title:
        analysis["video_title"] = video_title
        try:
            analysis["title_confidence"] = max(
                0.0, min(1.0, float(analysis.get("title_confidence", 0.5)))
            )
        except (TypeError, ValueError):
            analysis["title_confidence"] = 0.5
    else:
        analysis.pop("video_title", None)
        analysis.pop("title_confidence", None)

    normalized_sections = []
    raw_sections = analysis.get("narrative_structure") or analysis.get("structure") or []
    for index, section in enumerate(raw_sections if isinstance(raw_sections, list) else []):
        if not isinstance(section, dict):
            continue
        role = _clean_text(section.get("stage_role") or section.get("stage"), 50)
        summary = _clean_text(section.get("summary") or section.get("description"), 2000)
        normalized_sections.append({
            **section,
            "title": _content_title(section.get("title"), summary, role, index),
            "stage_role": role,
            # Preserve aliases while old clients and previously generated data coexist.
            "stage": role,
            "summary": summary,
            "description": summary,
        })
    analysis["narrative_structure"] = normalized_sections
    return analysis


def _normalize_frame_analysis(value: dict) -> dict:
    confidence = value.get("confidence", 0.7)
    try:
        confidence = max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        confidence = 0.7
    return {
        "title": str(value.get("title", "")).strip()[:120],
        "description": str(value.get("description", "")).strip()[:1200],
        "summary": str(value.get("summary", "")).strip()[:800],
        "subjects": _clean_string_list(value.get("subjects")),
        "scene": str(value.get("scene", "")).strip()[:500],
        "actions": _clean_string_list(value.get("actions")),
        "visible_text": str(value.get("visible_text", "")).strip()[:3000],
        "composition": str(value.get("composition", "")).strip()[:600],
        "camera": str(value.get("camera", "")).strip()[:500],
        "lighting": str(value.get("lighting", "")).strip()[:500],
        "colors": _clean_string_list(value.get("colors")),
        "style_tags": _clean_string_list(value.get("style_tags")),
        "hook_elements": _clean_string_list(value.get("hook_elements")),
        "key_points": _clean_string_list(value.get("key_points"), 8),
        "needs_motion_context": bool(value.get("needs_motion_context", False)),
        "confidence": round(confidence, 4),
    }


def _image_messages(batch: list[dict], frames_dir: Path, segments: list[dict]) -> list[dict]:
    manifest = []
    content: list[dict] = []
    for order, frame in enumerate(batch, start=1):
        image_path = frames_dir / Path(frame["image_url"]).name
        content.append({
            "type": "image_url",
            "image_url": {"url": _data_url(image_path, "image/jpeg")},
        })
        start = float(frame.get("start_time", frame["timestamp"]))
        end = float(frame.get("end_time", frame["timestamp"] + 5))
        manifest.append({
            "image_order": order,
            "frame_id": frame["id"],
            "timestamp": frame["timestamp"],
            "shot_range": [start, end],
            "local_metrics": {
                key: frame.get(key)
                for key in (
                    "quality_score", "motion_score", "dynamic_score",
                    "transition_score", "is_dynamic",
                )
            },
            "transcript": _transcript_excerpt(segments, start, end),
        })
    prompt = {
        "task": (
            "按 image_order 分析所有关键帧。场景不限于 PPT：也要识别人物、实拍、产品、"
            "镜头语言、构图、光线、配色、字幕、剪辑钩子和可复用素材风格。转写只作上下文，"
            "不要把转写中未出现在画面的内容臆测成视觉事实。若静态帧不足以判断动作或转场，"
            "needs_motion_context 必须为 true。"
        ),
        "frames": manifest,
        "output_json_schema": {
            "frames": [{
                "frame_id": "string，与输入严格一致",
                "title": "string",
                "description": "string，客观视觉描述",
                "summary": "string，结合转写的简洁含义",
                "subjects": ["string"],
                "scene": "string",
                "actions": ["string"],
                "visible_text": "string",
                "composition": "string",
                "camera": "string",
                "lighting": "string",
                "colors": ["string"],
                "style_tags": ["string"],
                "hook_elements": ["string"],
                "key_points": ["string"],
                "needs_motion_context": "boolean",
                "confidence": "0 到 1 的 number",
            }]
        },
        "rules": ["只输出 JSON 对象", "每个输入 frame_id 恰好输出一次", "不要输出 Markdown"],
    }
    content.append({"type": "text", "text": json.dumps(prompt, ensure_ascii=False)})
    return [
        {"role": "system", "content": "你是专业的视频视觉导演、镜头分析师和内容研究员。严格输出指定 JSON。"},
        {"role": "user", "content": content},
    ]


async def understand_keyframes(
    frames: list[dict],
    frames_dir: Path,
    segments: list[dict],
    progress_callback: ProgressCallback | None = None,
) -> tuple[list[dict], dict]:
    if not frames:
        return frames, {"enabled": False, "reason": "no_frames", "usage": {}}
    if not ENABLE_MIMO_VISION or not MIMO_API_KEY:
        return frames, {
            "enabled": False,
            "reason": "disabled" if not ENABLE_MIMO_VISION else "missing_api_key",
            "usage": {},
        }
    size = max(1, min(20, int(MIMO_VISION_IMAGE_BATCH_SIZE)))
    batches = [frames[index:index + size] for index in range(0, len(frames), size)]
    semaphore = asyncio.Semaphore(max(1, int(MIMO_VISION_CONCURRENCY)))
    completed = 0

    async def run_batch(index: int, batch: list[dict]):
        nonlocal completed
        async with semaphore:
            if progress_callback:
                await progress_callback({
                    "completed": completed,
                    "total": len(batches),
                    "detail": f"MiMo 正在理解第 {index}/{len(batches)} 批关键帧",
                })
            expected = {frame["id"] for frame in batch}
            parsed = None
            usage_rows = []
            for validation_attempt in range(2):
                parsed, usage = await asyncio.to_thread(
                    _request_json_sync,
                    _image_messages(batch, frames_dir, segments),
                    "image understanding",
                    5000,
                )
                usage_rows.append(usage)
                returned = {
                    str(item.get("frame_id"))
                    for item in parsed.get("frames", [])
                    if isinstance(item, dict) and item.get("frame_id")
                }
                if returned == expected:
                    break
                if validation_attempt == 1:
                    raise RuntimeError(
                        "MiMo image JSON validation failed: "
                        f"expected {sorted(expected)}, returned {sorted(returned)}"
                    )
            completed += 1
            if progress_callback:
                await progress_callback({
                    "completed": completed,
                    "total": len(batches),
                    "detail": f"MiMo 已完成 {completed}/{len(batches)} 批关键帧",
                })
            return parsed, _usage_total(usage_rows)

    results = await asyncio.gather(*[
        run_batch(index, batch) for index, batch in enumerate(batches, start=1)
    ])
    by_id = {}
    usages = []
    for parsed, usage in results:
        usages.append(usage)
        for item in parsed.get("frames", []):
            if isinstance(item, dict) and item.get("frame_id"):
                by_id[str(item["frame_id"])] = _normalize_frame_analysis(item)
    enriched = []
    for frame in frames:
        analysis = by_id.get(frame["id"])
        if analysis:
            frame = {**frame, "visual_analysis": analysis}
            if not frame.get("ocr_text") and analysis.get("visible_text"):
                frame["ocr_text"] = analysis["visible_text"]
            frame["confidence"] = round(
                float(frame.get("confidence", 0.7)) * 0.4 + analysis["confidence"] * 0.6,
                4,
            )
        enriched.append(frame)
    return enriched, {
        "enabled": True,
        "model": MIMO_VISION_MODEL,
        "batch_count": len(batches),
        "analyzed_frames": len(by_id),
        "usage": _usage_total(usages),
    }


def _clip_messages(clips: list[dict], frames_by_id: dict, segments: list[dict]) -> list[dict]:
    content: list[dict] = []
    manifest = []
    for order, clip in enumerate(clips, start=1):
        path = Path(clip["path"])
        content.append({
            "type": "video_url",
            "video_url": {
                "url": _data_url(path, "video/mp4"),
                "fps": MIMO_VISION_VIDEO_FPS,
                "media_resolution": "default",
            },
        })
        frame = frames_by_id[clip["frame_id"]]
        manifest.append({
            "video_order": order,
            "frame_id": clip["frame_id"],
            "range": [clip["start_time"], clip["end_time"]],
            "static_analysis": frame.get("visual_analysis", {}),
            "transcript": _transcript_excerpt(
                segments, clip["start_time"], clip["end_time"], 1800
            ),
        })
    prompt = {
        "task": (
            "分析这些无声短视频的动作过程、镜头运动、转场、节奏与视觉钩子。"
            "视频本身无音轨，transcript 是同时间段的语音转写，仅用于语义对齐。"
        ),
        "clips": manifest,
        "output_json_schema": {
            "clips": [{
                "frame_id": "string",
                "motion": "string",
                "camera_movement": "string",
                "transition": "string",
                "action_sequence": ["string"],
                "pacing": "string",
                "audio_visual_notes": "string",
                "style_tags": ["string"],
                "hook_elements": ["string"],
                "confidence": "0 到 1 的 number",
            }]
        },
        "rules": ["只输出 JSON 对象", "frame_id 与输入严格一致", "不要输出 Markdown"],
    }
    content.append({"type": "text", "text": json.dumps(prompt, ensure_ascii=False)})
    return [
        {"role": "system", "content": "你是专业剪辑师和动态镜头分析师。严格输出指定 JSON。"},
        {"role": "user", "content": content},
    ]


async def understand_clips(
    clips: list[dict],
    frames: list[dict],
    segments: list[dict],
) -> tuple[list[dict], dict]:
    if not clips or not ENABLE_MIMO_VISION or not MIMO_API_KEY:
        return frames, {"enabled": False, "clip_count": 0, "usage": {}}
    frames_by_id = {frame["id"]: frame for frame in frames}
    parsed, usage = await asyncio.to_thread(
        _request_json_sync,
        _clip_messages(clips, frames_by_id, segments),
        "video understanding",
        4000,
    )
    analyzed = 0
    for item in parsed.get("clips", []):
        if not isinstance(item, dict) or str(item.get("frame_id")) not in frames_by_id:
            continue
        frame = frames_by_id[str(item["frame_id"])]
        try:
            confidence = max(0.0, min(1.0, float(item.get("confidence", 0.7) or 0.7)))
        except (TypeError, ValueError):
            confidence = 0.7
        frame["clip_analysis"] = {
            "motion": str(item.get("motion", ""))[:1000],
            "camera_movement": str(item.get("camera_movement", ""))[:500],
            "transition": str(item.get("transition", ""))[:500],
            "action_sequence": _clean_string_list(item.get("action_sequence")),
            "pacing": str(item.get("pacing", ""))[:500],
            "audio_visual_notes": str(item.get("audio_visual_notes", ""))[:800],
            "style_tags": _clean_string_list(item.get("style_tags")),
            "hook_elements": _clean_string_list(item.get("hook_elements")),
            "confidence": confidence,
        }
        analyzed += 1
    return frames, {
        "enabled": True,
        "model": MIMO_VISION_MODEL,
        "clip_count": len(clips),
        "analyzed_clips": analyzed,
        "usage": _usage_total([usage]),
    }


def _local_style_summary(frames: list[dict], duration: float) -> dict:
    tags = Counter(
        tag
        for frame in frames
        for tag in (
            frame.get("visual_analysis", {}).get("style_tags", [])
            + frame.get("clip_analysis", {}).get("style_tags", [])
        )
    )
    hooks = []
    for frame in frames:
        hooks.extend(frame.get("visual_analysis", {}).get("hook_elements", []))
        hooks.extend(frame.get("clip_analysis", {}).get("hook_elements", []))
    dynamic_count = sum(bool(frame.get("is_dynamic")) for frame in frames)
    return _normalize_video_analysis({
        "content_summary": "已完成关键镜头、语音转写和时间轴对齐。",
        "visual_summary": "已完成本地镜头运动和画面风格统计。",
        "content_type": "视频内容",
        "target_audience": "待人工确认",
        "hook": hooks[0] if hooks else "未识别到明确钩子",
        "narrative_structure": [],
        "pacing": {
            "description": "动态镜头占比由本地运动检测估算",
            "dynamic_ratio": round(dynamic_count / max(1, len(frames)), 4),
        },
        "visual_style": "、".join(tag for tag, _ in tags.most_common(6)) or "待人工确认",
        "editing_style": "已对动态或不确定镜头补充短视频分析",
        "recurring_patterns": [],
        "viral_elements": list(dict.fromkeys(hooks))[:8],
        "storyboard": [
            {
                "start_time": frame.get("start_time", frame["timestamp"]),
                "end_time": frame.get("end_time", frame["timestamp"]),
                "shot": frame.get("visual_analysis", {}).get("description", "关键镜头"),
                "purpose": frame.get("visual_analysis", {}).get("summary", ""),
            }
            for frame in frames
        ],
        "recommendations": [],
        "confidence": 0.5,
        "duration": duration,
        "provider": "local-fallback",
    })


async def summarize_video_style(
    frames: list[dict],
    segments: list[dict],
    duration: float,
) -> tuple[dict, dict]:
    fallback = _local_style_summary(frames, duration)
    if not ENABLE_MIMO_VISION or not MIMO_API_KEY or not frames:
        return fallback, {"enabled": False, "usage": {}}
    material = {
        "duration": duration,
        "timeline": [
            {
                "frame_id": frame["id"],
                "start_time": frame.get("start_time", frame["timestamp"]),
                "end_time": frame.get("end_time", frame["timestamp"]),
                "visual": frame.get("visual_analysis", {}),
                "motion": frame.get("clip_analysis", {}),
                "local_metrics": {
                    "dynamic_score": frame.get("dynamic_score", 0),
                    "quality_score": frame.get("quality_score", 0),
                },
            }
            for frame in frames
        ],
        "transcript": [
            {"start": item.get("start"), "end": item.get("end"), "text": item.get("text", "")}
            for item in segments
        ],
    }
    prompt = {
        "task": (
            "先以完整转写为主、画面与镜头分析为辅，理解整个视频具体讲了什么；再生成内容标题、"
            "内容摘要、内容型章节和独立的视觉剪辑画像。区分事实观察与推断，不要只按 PPT/课程"
            "场景分析，也不要让视觉风格描述替代内容总结。"
        ),
        "material": material,
        "output_json_schema": {
            "video_title": "string，10 到 30 个汉字，准确概括整片核心主题",
            "title_confidence": "0 到 1 的 number",
            "content_summary": "string，概括主题、主要内容、关键结论和内容脉络",
            "visual_summary": "string，只概括画面、运镜、节奏和剪辑呈现",
            "content_type": "string",
            "target_audience": "string",
            "hook": "string",
            "narrative_structure": [{
                "title": "string，8 到 20 个汉字，说明本章具体讲了什么",
                "stage_role": "string，例如开篇、论证、案例或总结，仅表示叙事作用",
                "start_time": 0,
                "end_time": 0,
                "summary": "string，本章核心内容摘要",
            }],
            "pacing": {"description": "string", "patterns": ["string"]},
            "visual_style": "string",
            "editing_style": "string",
            "recurring_patterns": ["string"],
            "viral_elements": ["string"],
            "storyboard": [{"start_time": 0, "end_time": 0, "shot": "string", "purpose": "string"}],
            "recommendations": ["string"],
            "confidence": "0 到 1 的 number",
        },
        "rules": [
            "只输出 JSON 对象，时间必须来自输入时间轴，不要输出 Markdown",
            "video_title 不得照抄文件名，不得使用未命名视频、视频内容、课程介绍、会议记录、精彩分享等空泛标题",
            "章节 title 必须由本章 summary 的核心对象、方法、观点或结论提炼，让用户只看标题就知道本章内容",
            "章节 title 禁止单独使用开篇、介绍、背景、标准、分析、案例、总结、结尾、主要内容、第几部分等结构词",
            "stage_role 与 title 分开表达，任何叙事角色词都不能代替内容标题",
            "不得添加转写、画面和镜头分析中不存在的事实",
        ],
    }
    messages = [
        {"role": "system", "content": "你是严谨的视频内容编辑和视觉导演。先准确归纳内容，再分析表达方式，严格输出指定 JSON。"},
        {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
    ]
    parsed, usage = await asyncio.to_thread(
        _request_json_sync, messages, "video style synthesis", 6000
    )
    parsed = _normalize_video_analysis(parsed, fallback)
    if not parsed.get("content_summary"):
        raise RuntimeError("MiMo video style synthesis returned no summary")
    parsed["provider"] = MIMO_VISION_MODEL
    return parsed, {
        "enabled": True,
        "model": MIMO_VISION_MODEL,
        "usage": _usage_total([usage]),
    }
