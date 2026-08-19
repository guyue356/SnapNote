import asyncio
import base64
import http.client
import json
import random
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
    api_key: str = MIMO_API_KEY,
    base_url: str = MIMO_BASE_URL,
    model: str = MIMO_VISION_MODEL,
    request_headers: dict | None = None,
    timeout: int = MIMO_VISION_TIMEOUT_SECONDS,
    max_attempts: int = MIMO_VISION_MAX_ATTEMPTS,
):
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
            return _parse_json_content(content), payload.get("usage", {})
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
    result = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    for row in rows:
        for key in result:
            result[key] += int(row.get(key, 0) or 0)
    return result


def _clean_string_list(value, maximum: int = 12) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip()[:160] for item in value if str(item).strip()][:maximum]


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
    return {
        "summary": "已完成关键镜头、语音转写和时间轴对齐。",
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
    }


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
            "把镜头分析与转写综合成可用于长视频、批量爆款研究、素材风格学习和分镜复刻的整片画像。"
            "区分事实观察与推断，不要只按 PPT/课程场景分析。"
        ),
        "material": material,
        "output_json_schema": {
            "summary": "string",
            "content_type": "string",
            "target_audience": "string",
            "hook": "string",
            "narrative_structure": [{"stage": "string", "start_time": 0, "end_time": 0, "description": "string"}],
            "pacing": {"description": "string", "patterns": ["string"]},
            "visual_style": "string",
            "editing_style": "string",
            "recurring_patterns": ["string"],
            "viral_elements": ["string"],
            "storyboard": [{"start_time": 0, "end_time": 0, "shot": "string", "purpose": "string"}],
            "recommendations": ["string"],
            "confidence": "0 到 1 的 number",
        },
        "rules": ["只输出 JSON 对象", "时间必须来自输入时间轴", "不要输出 Markdown"],
    }
    messages = [
        {"role": "system", "content": "你是资深视频导演、剪辑策略师和爆款内容研究员。严格输出指定 JSON。"},
        {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
    ]
    parsed, usage = await asyncio.to_thread(
        _request_json_sync, messages, "video style synthesis", 6000
    )
    if not parsed.get("summary"):
        raise RuntimeError("MiMo video style synthesis returned no summary")
    parsed["provider"] = MIMO_VISION_MODEL
    return parsed, {
        "enabled": True,
        "model": MIMO_VISION_MODEL,
        "usage": _usage_total([usage]),
    }
