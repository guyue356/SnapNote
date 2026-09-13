import asyncio
import math
import subprocess
from pathlib import Path

import numpy as np

from .config import (
    FFMPEG_BIN,
    FRAME_ANALYSIS_FPS,
    FRAME_ANALYSIS_WIDTH,
    FRAME_MAX_GAP_SECONDS,
    FRAME_DYNAMIC_THRESHOLD,
    FRAME_FALLBACK_INTERVAL_SECONDS,
    FRAME_MAX_COUNT,
    FRAME_OUTPUT_WIDTH,
    FRAME_PHASH_THRESHOLD,
    FRAME_SEMANTIC_MAX_SECONDS,
    FRAME_SEMANTIC_MIN_SECONDS,
    FRAME_TARGET_INTERVAL_SECONDS,
    MIMO_VISION_CLIP_SECONDS,
    SCENE_CHANGE_THRESHOLD,
    SCENE_MAX_DURATION_SECONDS,
    SCENE_MIN_DURATION_SECONDS,
)


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def _frame_metrics(gray: np.ndarray, previous: np.ndarray | None) -> dict:
    pixels = gray.astype(np.float32)
    brightness = float(pixels.mean())
    contrast = float(pixels.std())
    core = pixels[1:-1, 1:-1]
    laplacian = (
        pixels[:-2, 1:-1]
        + pixels[2:, 1:-1]
        + pixels[1:-1, :-2]
        + pixels[1:-1, 2:]
        - 4 * core
    )
    sharpness_raw = float(laplacian.var())
    sharpness = _clamp(math.log1p(sharpness_raw) / math.log1p(1800))
    brightness_score = _clamp(1 - abs(brightness - 128) / 118)
    contrast_score = _clamp(contrast / 62)

    histogram, _ = np.histogram(gray, bins=32, range=(0, 256), density=False)
    histogram = histogram.astype(np.float32)
    histogram /= max(1.0, float(histogram.sum()))
    if previous is None:
        motion = 0.0
        histogram_delta = 0.0
    else:
        motion = float(np.mean(np.abs(pixels - previous.astype(np.float32))) / 255)
        previous_histogram, _ = np.histogram(previous, bins=32, range=(0, 256), density=False)
        previous_histogram = previous_histogram.astype(np.float32)
        previous_histogram /= max(1.0, float(previous_histogram.sum()))
        histogram_delta = float(np.abs(histogram - previous_histogram).sum() / 2)

    transition = _clamp(histogram_delta * 0.55 + motion * 1.8)
    stability = _clamp(1 - motion / 0.18)
    quality = _clamp(
        sharpness * 0.42
        + brightness_score * 0.18
        + contrast_score * 0.20
        + stability * 0.20
    )
    return {
        "sharpness_score": round(sharpness, 4),
        "brightness_score": round(brightness_score, 4),
        "contrast_score": round(contrast_score, 4),
        "stability_score": round(stability, 4),
        "motion_score": round(motion, 4),
        "transition_score": round(transition, 4),
        "quality_score": round(quality, 4),
    }


def _scan_video_sync(video_path: str, duration: float) -> list[dict]:
    fps = max(0.2, min(float(FRAME_ANALYSIS_FPS), 10.0))
    width = max(96, int(FRAME_ANALYSIS_WIDTH))
    frame_bytes = width * width
    command = [
        FFMPEG_BIN,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        video_path,
        "-t",
        f"{duration:.3f}",
        "-an",
        "-vf",
        (
            f"fps={fps},scale={width}:{width}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{width}:(ow-iw)/2:(oh-ih)/2:black,format=gray"
        ),
        "-f",
        "rawvideo",
        "-pix_fmt",
        "gray",
        "pipe:1",
    ]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if process.stdout is None:
        raise RuntimeError("ffmpeg did not expose a frame stream")

    samples: list[dict] = []
    previous = None
    index = 0
    while True:
        raw = process.stdout.read(frame_bytes)
        if not raw:
            break
        if len(raw) != frame_bytes:
            process.kill()
            raise RuntimeError("ffmpeg returned a truncated analysis frame")
        gray = np.frombuffer(raw, dtype=np.uint8).reshape((width, width))
        metrics = _frame_metrics(gray, previous)
        timestamp = min(max(0.0, index / fps), max(0.0, duration - 0.05))
        samples.append({"timestamp": timestamp, **metrics})
        previous = gray.copy()
        index += 1
    stderr = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
    return_code = process.wait(timeout=30)
    if return_code != 0:
        raise RuntimeError(f"ffmpeg scene scan failed: {stderr[-500:]}")
    return samples


def _shots_from_samples(samples: list[dict], duration: float) -> list[dict]:
    if not samples:
        return []
    boundaries = [(0, "start")]
    last_boundary_time = samples[0]["timestamp"]
    for index, sample in enumerate(samples[1:], start=1):
        elapsed = sample["timestamp"] - last_boundary_time
        changed = (
            sample["transition_score"] >= SCENE_CHANGE_THRESHOLD
            and elapsed >= SCENE_MIN_DURATION_SECONDS
        )
        forced = elapsed >= SCENE_MAX_DURATION_SECONDS
        if changed or forced:
            boundaries.append((index, "scene_change" if changed else "max_duration"))
            last_boundary_time = sample["timestamp"]
    boundaries.append((len(samples), "end"))

    shots = []
    for shot_index, (left_boundary, right_boundary) in enumerate(
        zip(boundaries, boundaries[1:]), start=1
    ):
        left, boundary_reason = left_boundary
        right, end_reason = right_boundary
        group = samples[left:right]
        if not group:
            continue
        start = float(group[0]["timestamp"])
        end = float(samples[right]["timestamp"]) if right < len(samples) else float(duration)
        eligible = [sample for sample in group if sample["timestamp"] >= start + min(0.5, (end - start) / 3)] or group
        representative = max(
            eligible,
            key=lambda item: item["quality_score"] - item["transition_score"] * 0.18,
        )
        mean_motion = float(np.mean([item["motion_score"] for item in group]))
        peak_motion = max(item["motion_score"] for item in group)
        transition = max(item["transition_score"] for item in group)
        shots.append({
            **representative,
            "shot_id": f"shot-{shot_index}",
            "boundary_reason": boundary_reason,
            "end_reason": end_reason,
            "start_time": round(start, 3),
            "end_time": round(max(start + 0.1, end), 3),
            "duration": round(max(0.1, end - start), 3),
            "dynamic_score": round(max(mean_motion, peak_motion * 0.55), 4),
            "is_dynamic": bool(
                mean_motion >= FRAME_DYNAMIC_THRESHOLD
                or peak_motion >= FRAME_DYNAMIC_THRESHOLD * 1.75
            ),
            "sample_count": len(group),
        })
    return shots


def _cap_shots(shots: list[dict], duration: float) -> list[dict]:
    maximum = max(1, int(FRAME_MAX_COUNT))
    if len(shots) <= maximum:
        return shots
    selected = []
    bucket = max(duration / maximum, 0.1)
    for index in range(maximum):
        start, end = index * bucket, (index + 1) * bucket
        candidates = [shot for shot in shots if start <= shot["timestamp"] < end]
        if not candidates:
            midpoint = (start + end) / 2
            candidates = [min(shots, key=lambda shot: abs(shot["timestamp"] - midpoint))]
        selected.append(max(
            candidates,
            key=lambda shot: shot["quality_score"] + shot["transition_score"] * 0.12,
        ))
    unique = {shot["shot_id"]: shot for shot in selected}
    return sorted(unique.values(), key=lambda shot: shot["timestamp"])


def _fallback_shots(duration: float) -> list[dict]:
    interval = max(5.0, float(FRAME_FALLBACK_INTERVAL_SECONDS))
    count = max(1, min(int(FRAME_MAX_COUNT), math.ceil(duration / interval)))
    step = duration / count if duration else interval
    rows = []
    for index in range(count):
        start = index * step
        end = min(duration, (index + 1) * step)
        rows.append({
            "shot_id": f"fallback-{index + 1}",
            "timestamp": min(max(0.2, start + min(1.0, step / 3)), max(0.2, duration - 0.1)),
            "start_time": round(start, 3),
            "end_time": round(max(start + 0.1, end), 3),
            "duration": round(max(0.1, end - start), 3),
            "sharpness_score": 0.5,
            "brightness_score": 0.5,
            "contrast_score": 0.5,
            "stability_score": 0.5,
            "motion_score": 0.0,
            "transition_score": 0.0,
            "quality_score": 0.5,
            "dynamic_score": 0.0,
            "is_dynamic": False,
            "sample_count": 0,
        })
    return rows


async def analyze_shots(video_path: str, duration: float) -> tuple[list[dict], dict]:
    try:
        samples = await asyncio.to_thread(_scan_video_sync, video_path, duration)
        detected = _shots_from_samples(samples, duration)
        if not detected:
            raise RuntimeError("scene scan produced no usable shots")
        return detected, {
            "engine": "ffmpeg-rawvideo+numpy",
            "sample_fps": FRAME_ANALYSIS_FPS,
            "sample_count": len(samples),
            "detected_shots": len(detected),
            "candidate_shots": len(detected),
            "fallback": False,
        }
    except Exception as error:
        shots = _fallback_shots(duration)
        return shots, {
            "engine": "fixed-interval-fallback",
            "sample_count": 0,
            "detected_shots": len(shots),
            "candidate_shots": len(shots),
            "fallback": True,
            "warning": str(error)[:500],
        }


def build_semantic_units(
    segments: list[dict], shots: list[dict], duration: float
) -> list[dict]:
    """Build small time-grounded content units from ASR and real scene changes.

    ASR provider chunks are transport boundaries, not chapters.  Real scene
    changes refine those coarse ranges, while a maximum duration guarantees
    that speech-heavy or static videos still receive regular visual coverage.
    """
    duration = max(0.1, float(duration))
    minimum = max(5.0, float(FRAME_SEMANTIC_MIN_SECONDS))
    maximum = max(minimum, float(FRAME_SEMANTIC_MAX_SECONDS))
    proposed: list[tuple[float, str]] = [(0.0, "start"), (duration, "end")]
    for segment in segments:
        for key in ("start", "end"):
            try:
                value = max(0.0, min(duration, float(segment.get(key, 0))))
            except (TypeError, ValueError):
                continue
            if 0 < value < duration:
                proposed.append((value, "transcript"))
    for shot in shots:
        if shot.get("boundary_reason") != "scene_change":
            continue
        try:
            value = max(0.0, min(duration, float(shot.get("start_time", 0))))
        except (TypeError, ValueError):
            continue
        if 0 < value < duration:
            proposed.append((value, "scene_change"))

    priority = {"start": 4, "end": 4, "scene_change": 3, "transcript": 2}
    ordered = sorted(proposed, key=lambda item: (item[0], -priority[item[1]]))
    boundaries: list[tuple[float, str]] = []
    for value, reason in ordered:
        if boundaries and abs(value - boundaries[-1][0]) < minimum:
            if priority[reason] > priority[boundaries[-1][1]] and value < duration:
                boundaries[-1] = (value, reason)
            continue
        boundaries.append((value, reason))
    if not boundaries or boundaries[0][0] > 0:
        boundaries.insert(0, (0.0, "start"))
    if boundaries[-1][0] < duration:
        boundaries.append((duration, "end"))
    elif boundaries[-1][0] > duration:
        boundaries[-1] = (duration, "end")

    expanded: list[tuple[float, str]] = [boundaries[0]]
    for value, reason in boundaries[1:]:
        previous = expanded[-1][0]
        gap = value - previous
        parts = max(1, math.ceil(gap / maximum))
        for index in range(1, parts):
            expanded.append((previous + gap * index / parts, "coverage_split"))
        expanded.append((value, reason))
    if len(expanded) > 2 and duration - expanded[-2][0] < minimum:
        expanded.pop(-2)

    units = []
    for index, ((start, reason), (end, end_reason)) in enumerate(
        zip(expanded, expanded[1:]), start=1
    ):
        related = []
        segment_indices = []
        for segment_index, segment in enumerate(segments):
            try:
                segment_start = float(segment.get("start", 0))
                segment_end = float(segment.get("end", segment_start))
            except (TypeError, ValueError):
                continue
            if segment_end > start and segment_start < end:
                text = str(segment.get("text", "")).strip()
                if text:
                    related.append(text)
                segment_indices.append(segment_index)
        units.append({
            "id": f"unit-{index}",
            "start_time": round(start, 3),
            "end_time": round(max(start + 0.1, end), 3),
            "boundary_reason": reason,
            "end_reason": end_reason,
            "transcript_text": " ".join(related)[:4000],
            "segment_indices": segment_indices,
        })
    return units


def _unit_for_timestamp(units: list[dict], timestamp: float) -> dict | None:
    for index, unit in enumerate(units):
        start = float(unit["start_time"])
        end = float(unit["end_time"])
        if start <= timestamp < end or (index == len(units) - 1 and timestamp <= end):
            return unit
    return None


def _shot_score(shot: dict, unit: dict) -> float:
    start, end = float(unit["start_time"]), float(unit["end_time"])
    midpoint = (start + end) / 2
    radius = max(0.1, (end - start) / 2)
    proximity = max(0.0, 1 - abs(float(shot["timestamp"]) - midpoint) / radius)
    return (
        float(shot.get("quality_score", 0.5))
        + proximity * 0.16
        + float(shot.get("stability_score", 0.5)) * 0.05
        - float(shot.get("transition_score", 0)) * 0.05
    )


def _coverage_placeholder(unit: dict, reason: str = "semantic_anchor") -> dict:
    start, end = float(unit["start_time"]), float(unit["end_time"])
    timestamp = min(end - 0.05, start + max(0.2, min(1.0, (end - start) / 3)))
    return {
        "shot_id": f"coverage-{unit['id']}",
        "timestamp": round(max(0.0, timestamp), 3),
        "start_time": round(start, 3),
        "end_time": round(end, 3),
        "duration": round(max(0.1, end - start), 3),
        "sharpness_score": 0.5,
        "brightness_score": 0.5,
        "contrast_score": 0.5,
        "stability_score": 0.5,
        "motion_score": 0.0,
        "transition_score": 0.0,
        "quality_score": 0.5,
        "dynamic_score": 0.0,
        "is_dynamic": False,
        "sample_count": 0,
        "boundary_reason": "coverage_repair",
        "selection_reason": reason,
        "semantic_unit_id": unit["id"],
        "coverage_anchor": True,
    }


def _annotate_shot(shot: dict, unit: dict, reason: str, anchor: bool) -> dict:
    return {
        **shot,
        "semantic_unit_id": unit["id"],
        "selection_reason": reason,
        "coverage_anchor": anchor,
    }


def _maximum_gap(timestamps: list[float], duration: float) -> float:
    timeline = [0.0, *sorted(max(0.0, min(duration, item)) for item in timestamps), duration]
    return max((right - left for left, right in zip(timeline, timeline[1:])), default=duration)


def select_shots_for_semantic_coverage(
    shots: list[dict], units: list[dict], duration: float
) -> tuple[list[dict], dict]:
    maximum = max(1, int(FRAME_MAX_COUNT))
    if len(units) <= maximum:
        anchor_units = units
    elif maximum == 1:
        anchor_units = [units[len(units) // 2]]
    else:
        indexes = {
            round(index * (len(units) - 1) / (maximum - 1))
            for index in range(maximum)
        }
        anchor_units = [units[index] for index in sorted(indexes)]

    selected: list[dict] = []
    selected_ids: set[str] = set()
    for unit in anchor_units:
        candidates = [
            shot for shot in shots
            if float(unit["start_time"]) <= float(shot["timestamp"]) < float(unit["end_time"])
            and str(shot.get("shot_id")) not in selected_ids
        ]
        if candidates:
            winner = max(candidates, key=lambda shot: _shot_score(shot, unit))
            selected.append(_annotate_shot(winner, unit, "semantic_anchor", True))
            selected_ids.add(str(winner.get("shot_id")))
        else:
            placeholder = _coverage_placeholder(unit)
            selected.append(placeholder)
            selected_ids.add(str(placeholder["shot_id"]))

    target_interval = max(10.0, float(FRAME_TARGET_INTERVAL_SECONDS))
    target_count = min(maximum, max(len(selected), math.ceil(duration / target_interval)))
    max_gap = max(target_interval, float(FRAME_MAX_GAP_SECONDS))
    remaining = [shot for shot in shots if str(shot.get("shot_id")) not in selected_ids]
    while remaining and len(selected) < maximum:
        timestamps = [float(shot["timestamp"]) for shot in selected]
        current_gap = _maximum_gap(timestamps, duration)
        if len(selected) >= target_count and current_gap <= max_gap:
            break
        winner = max(
            remaining,
            key=lambda shot: (
                min(
                    [abs(float(shot["timestamp"]) - value) for value in timestamps]
                    + [float(shot["timestamp"]), duration - float(shot["timestamp"])]
                ),
                float(shot.get("quality_score", 0.5)),
            ),
        )
        unit = _unit_for_timestamp(units, float(winner["timestamp"])) or units[-1]
        selected.append(_annotate_shot(winner, unit, "coverage_fill", False))
        selected_ids.add(str(winner.get("shot_id")))
        remaining.remove(winner)

    selected.sort(key=lambda shot: float(shot["timestamp"]))
    stats = audit_frame_coverage(selected, units, duration)
    stats.update({
        "candidate_shots": len(shots),
        "selected_shots": len(selected),
        "frame_budget": maximum,
        "target_interval_seconds": target_interval,
    })
    return selected, stats


def audit_frame_coverage(
    frames: list[dict], units: list[dict], duration: float
) -> dict:
    covered = set()
    for frame in frames:
        unit_id = frame.get("semantic_unit_id")
        if unit_id:
            covered.add(str(unit_id))
            continue
        unit = _unit_for_timestamp(units, float(frame.get("timestamp", 0)))
        if unit:
            covered.add(str(unit["id"]))
    unit_ids = {str(unit["id"]) for unit in units}
    uncovered = sorted(unit_ids - covered)
    timestamps = [float(frame.get("timestamp", 0)) for frame in frames]
    return {
        "semantic_unit_count": len(units),
        "covered_semantic_units": len(unit_ids & covered),
        "semantic_coverage_ratio": round(len(unit_ids & covered) / max(1, len(unit_ids)), 4),
        "uncovered_semantic_unit_ids": uncovered,
        "max_frame_gap_seconds": round(_maximum_gap(timestamps, float(duration)), 3),
    }


def select_coverage_repairs(
    frames: list[dict],
    shots: list[dict],
    units: list[dict],
    duration: float,
    attempted_shot_ids: set[str] | None = None,
) -> list[dict]:
    """Choose alternate or synthetic shots for units/gaps left after extraction."""
    attempted = set(attempted_shot_ids or set())
    existing_ids = {str(frame.get("shot_id")) for frame in frames}
    unavailable = attempted | existing_ids
    repairs: list[dict] = []
    audit = audit_frame_coverage(frames, units, duration)
    uncovered = set(audit["uncovered_semantic_unit_ids"])
    by_id = {str(unit["id"]): unit for unit in units}
    for unit_id in sorted(uncovered):
        unit = by_id[unit_id]
        candidates = [
            shot for shot in shots
            if str(shot.get("shot_id")) not in unavailable
            and float(unit["start_time"]) <= float(shot["timestamp"]) < float(unit["end_time"])
        ]
        if candidates:
            winner = max(candidates, key=lambda shot: _shot_score(shot, unit))
            repair = _annotate_shot(winner, unit, "uncovered_unit_repair", True)
        else:
            repair = _coverage_placeholder(unit, "uncovered_unit_repair")
        if str(repair["shot_id"]) not in unavailable:
            repairs.append(repair)
            unavailable.add(str(repair["shot_id"]))

    max_gap = max(10.0, float(FRAME_MAX_GAP_SECONDS))
    timeline = [(0.0, None), *sorted(
        (float(frame.get("timestamp", 0)), frame) for frame in [*frames, *repairs]
    ), (float(duration), None)]
    for (left, _), (right, _) in zip(timeline, timeline[1:]):
        if right - left <= max_gap:
            continue
        midpoint = (left + right) / 2
        candidates = [
            shot for shot in shots
            if str(shot.get("shot_id")) not in unavailable
            and left < float(shot["timestamp"]) < right
        ]
        if candidates:
            winner = min(candidates, key=lambda shot: abs(float(shot["timestamp"]) - midpoint))
            unit = _unit_for_timestamp(units, float(winner["timestamp"])) or units[-1]
            repair = _annotate_shot(winner, unit, "temporal_gap_repair", False)
        else:
            unit = _unit_for_timestamp(units, midpoint) or units[-1]
            repair = _coverage_placeholder(unit, "temporal_gap_repair")
            repair["shot_id"] = f"coverage-gap-{midpoint:.3f}"
            repair["timestamp"] = round(midpoint, 3)
        if str(repair["shot_id"]) not in unavailable:
            repairs.append(repair)
            unavailable.add(str(repair["shot_id"]))
    return repairs[:max(0, int(FRAME_MAX_COUNT) - len(frames))]


async def extract_keyframes(
    video_path: str,
    shots: list[dict],
    frames_dir: Path,
    start_index: int = 1,
) -> list[dict]:
    frames_dir.mkdir(parents=True, exist_ok=True)
    frames = []
    for index, shot in enumerate(shots, start=start_index):
        output = frames_dir / f"frame_{index:03d}.jpg"
        command = [
            FFMPEG_BIN,
            "-y",
            "-ss",
            f"{shot['timestamp']:.3f}",
            "-i",
            video_path,
            "-frames:v",
            "1",
            "-vf",
            f"scale={FRAME_OUTPUT_WIDTH}:-2:force_original_aspect_ratio=decrease",
            "-q:v",
            "3",
            str(output),
        ]
        try:
            await _run(command, 90)
        except Exception:
            continue
        if output.is_file() and output.stat().st_size:
            frames.append({
                **shot,
                "id": f"frame-{index}",
                "timestamp": round(float(shot["timestamp"]), 3),
                "image_url": f"/storage/tasks/{frames_dir.parent.name}/frames/{output.name}",
                "ocr_text": "",
                "confidence": round(0.55 + float(shot["quality_score"]) * 0.4, 4),
            })
    return frames


async def _run(command: list[str], timeout: int):
    def execute():
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=True)
    return await asyncio.to_thread(execute)


def deduplicate_frames(frames: list[dict], frames_dir: Path) -> list[dict]:
    try:
        import imagehash
        from PIL import Image

        selected, hashes = [], []
        for frame in frames:
            path = frames_dir / Path(frame["image_url"]).name
            with Image.open(path) as image:
                current = imagehash.phash(image)
            if not frame.get("coverage_anchor"):
                unit_id = frame.get("semantic_unit_id")
                comparable = [
                    previous_hash
                    for previous_frame, previous_hash in zip(selected, hashes)
                    if (
                        unit_id and previous_frame.get("semantic_unit_id") == unit_id
                    ) or (
                        not unit_id and previous_frame is selected[-1]
                    )
                ]
                if comparable and min(current - previous for previous in comparable) <= FRAME_PHASH_THRESHOLD:
                    continue
            hashes.append(current)
            selected.append(frame)
        return selected
    except Exception:
        return frames


async def create_silent_proxy_clips(
    video_path: str,
    frames: list[dict],
    clips_dir: Path,
    maximum: int,
) -> list[dict]:
    clips_dir.mkdir(parents=True, exist_ok=True)
    dynamic = [
        frame for frame in frames
        if frame.get("is_dynamic")
        or frame.get("visual_analysis", {}).get("needs_motion_context")
    ]
    dynamic.sort(
        key=lambda frame: (
            float(frame.get("dynamic_score", 0)),
            float(frame.get("visual_analysis", {}).get("confidence", 0)),
        ),
        reverse=True,
    )
    clips = []
    for frame in dynamic[:max(0, maximum)]:
        shot_start = max(0.0, float(frame.get("start_time", frame["timestamp"] - 2)))
        shot_end = float(frame.get("end_time", shot_start + MIMO_VISION_CLIP_SECONDS))
        clip_duration = min(
            float(MIMO_VISION_CLIP_SECONDS), max(0.5, shot_end - shot_start)
        )
        centered = float(frame["timestamp"]) - clip_duration / 2
        start = max(shot_start, min(centered, max(shot_start, shot_end - clip_duration)))
        output = clips_dir / f"{frame['id']}.mp4"
        command = [
            FFMPEG_BIN,
            "-y",
            "-ss",
            f"{start:.3f}",
            "-i",
            video_path,
            "-t",
            f"{clip_duration:.3f}",
            "-an",
            "-vf",
            f"scale={FRAME_OUTPUT_WIDTH}:-2:force_original_aspect_ratio=decrease",
            "-r",
            "12",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "30",
            "-movflags",
            "+faststart",
            str(output),
        ]
        try:
            await _run(command, 180)
        except Exception:
            continue
        if output.is_file() and output.stat().st_size:
            clips.append({
                "frame_id": frame["id"],
                "path": str(output),
                "start_time": round(start, 3),
                "end_time": round(start + clip_duration, 3),
                "duration": round(clip_duration, 3),
            })
    return clips
