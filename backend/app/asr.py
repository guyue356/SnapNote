import asyncio
import base64
import http.client
import json
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from pathlib import Path

from .config import (
    AUDIO_SAMPLE_RATE,
    ENABLE_LOCAL_WHISPER,
    FFMPEG_BIN,
    FFPROBE_BIN,
    MIMO_API_KEY,
    MIMO_ASR_CHUNK_SECONDS,
    MIMO_ASR_CONCURRENCY,
    MIMO_ASR_FALLBACK_RETRY,
    MIMO_ASR_HEARTBEAT_SECONDS,
    MIMO_ASR_LANGUAGE,
    MIMO_ASR_MAX_ATTEMPTS,
    MIMO_ASR_MODEL,
    MIMO_ASR_MP3_BITRATE,
    MIMO_ASR_TIMEOUT_SECONDS,
    MIMO_BASE_URL,
    WHISPER_CACHE_DIR,
    WHISPER_COMPUTE_TYPE,
    WHISPER_DEVICE,
    WHISPER_LANGUAGE,
    WHISPER_MODEL_SIZE,
)


ProgressCallback = Callable[[dict], Awaitable[None]]
MIMO_ASR_MAX_BASE64_BYTES = 10 * 1024 * 1024
_whisper_model = None
_whisper_available = True


class MimoAsrTransientError(RuntimeError):
    """A retryable MIMO-ASR service or connection failure."""


async def _emit(progress_callback: ProgressCallback | None, **payload):
    if progress_callback:
        await progress_callback(payload)


def _get_whisper():
    global _whisper_model, _whisper_available
    if not _whisper_available:
        return None
    if _whisper_model is None:
        try:
            from faster_whisper import WhisperModel

            _whisper_model = WhisperModel(
                WHISPER_MODEL_SIZE,
                device=WHISPER_DEVICE,
                compute_type=WHISPER_COMPUTE_TYPE,
                download_root=str(WHISPER_CACHE_DIR),
            )
        except Exception:
            _whisper_available = False
            raise
    return _whisper_model


def _whisper_transcribe_options(audio_path: str | Path, language: str | None):
    return {
        "audio": str(audio_path),
        "beam_size": 1,
        "best_of": 1,
        "language": language,
        "vad_filter": True,
        "vad_parameters": {
            "min_silence_duration_ms": 500,
            "speech_pad_ms": 200,
        },
        "condition_on_previous_text": False,
        "compression_ratio_threshold": 2.2,
        "log_prob_threshold": -1.0,
        "no_speech_threshold": 0.6,
        "repetition_penalty": 1.1,
        "no_repeat_ngram_size": 3,
        "word_timestamps": True,
        "hallucination_silence_threshold": 2.0,
    }


def _normalize_transcript_text(text: str):
    return "".join(char for char in (text or "").strip().lower() if char.isalnum())


def _is_repeated_whisper_hallucination(segment: dict, kept_segments: list[dict]):
    text_key = _normalize_transcript_text(segment.get("text", ""))
    if not text_key or len(text_key) < 4 or not kept_segments:
        return False
    if text_key != _normalize_transcript_text(kept_segments[-1].get("text", "")):
        return False

    duration = float(segment.get("end", 0) or 0) - float(segment.get("start", 0) or 0)
    previous_duration = (
        float(kept_segments[-1].get("end", 0) or 0)
        - float(kept_segments[-1].get("start", 0) or 0)
    )
    consecutive_same = 1
    for previous in reversed(kept_segments[:-1]):
        if _normalize_transcript_text(previous.get("text", "")) != text_key:
            break
        consecutive_same += 1
    return duration >= 20 or previous_duration >= 20 or consecutive_same >= 2


async def transcribe_with_whisper(
    audio_path: str | Path,
    duration: float,
    progress_callback: ProgressCallback | None = None,
):
    if not ENABLE_LOCAL_WHISPER:
        raise RuntimeError("Local Whisper is disabled. Set ENABLE_LOCAL_WHISPER=1.")
    audio_path = Path(audio_path)
    if not audio_path.is_file():
        raise RuntimeError(f"Audio file does not exist: {audio_path}")

    language = WHISPER_LANGUAGE or None
    await _emit(
        progress_callback,
        progress_pct=0,
        detail=(
            f"Loading Whisper {WHISPER_MODEL_SIZE} on "
            f"{WHISPER_DEVICE}/{WHISPER_COMPUTE_TYPE} from {WHISPER_CACHE_DIR}"
        ),
    )
    segment_queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()
    error_holder = [None]
    language_holder = [None]

    def worker():
        try:
            model = _get_whisper()
            if model is None:
                raise RuntimeError(
                    "Whisper model could not be loaded. Install faster-whisper first."
                )
            stream, info = model.transcribe(
                **_whisper_transcribe_options(audio_path, language)
            )
            language_holder[0] = info.language
            for item in stream:
                segment = {
                    "start": float(item.start),
                    "end": float(item.end),
                    "text": item.text.strip(),
                }
                loop.call_soon_threadsafe(segment_queue.put_nowait, segment)
        except Exception as error:
            error_holder[0] = error
        finally:
            loop.call_soon_threadsafe(segment_queue.put_nowait, None)

    worker_task = asyncio.create_task(asyncio.to_thread(worker))
    segments: list[dict] = []
    skipped_repeats = 0
    while True:
        try:
            segment = await asyncio.wait_for(segment_queue.get(), timeout=600)
        except asyncio.TimeoutError as error:
            raise RuntimeError("Whisper transcription produced no progress for 600 seconds") from error
        if segment is None:
            break
        if _is_repeated_whisper_hallucination(segment, segments):
            skipped_repeats += 1
            continue
        if segment["text"]:
            segments.append(segment)
        if len(segments) % 10 == 0 and segments:
            progress = min(95, int(segment["end"] / duration * 100)) if duration > 0 else 50
            await _emit(
                progress_callback,
                progress_pct=progress,
                detail=f"{segment['end']:.0f}s / {duration:.0f}s",
                segment_count=len(segments),
            )

    await worker_task
    if error_holder[0] is not None:
        raise error_holder[0]
    if not segments:
        raise RuntimeError("Whisper returned an empty transcript")
    await _emit(
        progress_callback,
        progress_pct=100,
        detail=f"{len(segments)} segments, skipped {skipped_repeats} repeats",
        segment_count=len(segments),
        language=language_holder[0],
    )
    return segments, f"faster-whisper:{WHISPER_MODEL_SIZE}"


def _mimo_audio_format(audio_path: str | Path):
    return "mp3" if Path(audio_path).suffix.lower() == ".mp3" else "wav"


def _mimo_data_url(audio_path: str | Path, audio_format: str):
    encoded = base64.b64encode(Path(audio_path).read_bytes()).decode("ascii")
    return f"data:audio/{audio_format};base64,{encoded}"


def _mimo_data_url_size(audio_path: str | Path, audio_format: str):
    return len(_mimo_data_url(audio_path, audio_format).encode("ascii"))


def _mimo_retry_delay(attempt: int, headers=None):
    delay = 2 ** (attempt - 1)
    if headers:
        retry_after = headers.get("Retry-After")
        if retry_after:
            try:
                delay = max(delay, int(float(retry_after)))
            except ValueError:
                pass
    return min(delay, 30)


def _extract_message_content(message: dict):
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            item["text"]
            for item in content
            if isinstance(item, dict) and item.get("text")
        )
    return str(content)


def _mimo_asr_transcribe_sync(audio_path: str | Path, language: str):
    if not MIMO_API_KEY:
        raise RuntimeError("MIMO_API_KEY is not set. Add it to backend/.env.")
    audio_format = _mimo_audio_format(audio_path)
    audio_data_url = _mimo_data_url(audio_path, audio_format)
    if len(audio_data_url.encode("ascii")) > MIMO_ASR_MAX_BASE64_BYTES:
        raise RuntimeError(
            "MIMO-ASR audio chunk exceeds the 10MB base64 payload limit. "
            "Lower MIMO_ASR_CHUNK_SECONDS or MIMO_ASR_MP3_BITRATE."
        )

    payload = json.dumps(
        {
            "model": MIMO_ASR_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {
                                "data": audio_data_url,
                                "format": audio_format,
                            },
                        }
                    ],
                }
            ],
            "asr_options": {"language": language or "auto"},
        }
    ).encode("utf-8")

    max_attempts = max(1, int(MIMO_ASR_MAX_ATTEMPTS or 1))
    timeout = max(10, int(MIMO_ASR_TIMEOUT_SECONDS or 90))
    last_error = None
    for attempt in range(1, max_attempts + 1):
        request = urllib.request.Request(
            f"{MIMO_BASE_URL}/chat/completions",
            data=payload,
            headers={"api-key": MIMO_API_KEY, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as error:
            if error.code not in {429, 500, 502, 503, 504}:
                body = error.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"MIMO-ASR HTTP {error.code}: {body[:500]}") from error
            last_error = error
            if attempt == max_attempts:
                body = error.read().decode("utf-8", errors="replace")
                raise MimoAsrTransientError(
                    f"MIMO-ASR HTTP {error.code} after {max_attempts} attempts: {body[:500]}"
                ) from error
            time.sleep(_mimo_retry_delay(attempt, error.headers))
        except (
            http.client.RemoteDisconnected,
            ConnectionResetError,
            TimeoutError,
            urllib.error.URLError,
        ) as error:
            last_error = error
            if attempt == max_attempts:
                raise MimoAsrTransientError(
                    f"MIMO-ASR connection failed after {max_attempts} attempts: "
                    f"{type(error).__name__}: {error}"
                ) from error
            time.sleep(_mimo_retry_delay(attempt))
    else:
        raise RuntimeError(f"MIMO-ASR request failed: {last_error}")

    try:
        return _extract_message_content(data["choices"][0]["message"]).strip()
    except (KeyError, IndexError, TypeError) as error:
        raise RuntimeError(f"Unexpected MIMO-ASR response: {data}") from error


def _probe_audio_duration(audio_path: str | Path):
    result = subprocess.run(
        [
            FFPROBE_BIN,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(audio_path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return float(result.stdout.strip() or 0)


def _prepare_mimo_mp3_chunks(audio_path: str | Path, total_duration: float):
    audio_path = Path(audio_path).resolve()
    chunks_dir = audio_path.parent / "mimo_chunks"
    if chunks_dir.parent != audio_path.parent or chunks_dir.name != "mimo_chunks":
        raise RuntimeError(f"Refusing to clear unexpected chunk directory: {chunks_dir}")
    chunk_seconds = max(30, int(MIMO_ASR_CHUNK_SECONDS or 90))

    while True:
        shutil.rmtree(chunks_dir, ignore_errors=True)
        chunks_dir.mkdir(parents=True, exist_ok=True)
        pattern = chunks_dir / "chunk_%04d.mp3"
        result = subprocess.run(
            [
                FFMPEG_BIN,
                "-y",
                "-i",
                str(audio_path),
                "-vn",
                "-map",
                "0:a:0",
                "-ac",
                "1",
                "-ar",
                str(AUDIO_SAMPLE_RATE),
                "-b:a",
                MIMO_ASR_MP3_BITRATE,
                "-f",
                "segment",
                "-segment_time",
                str(chunk_seconds),
                "-reset_timestamps",
                "1",
                str(pattern),
            ],
            capture_output=True,
            text=True,
            timeout=600,
        )
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg MIMO-ASR chunking failed: {result.stderr[:300]}")
        paths = sorted(chunks_dir.glob("chunk_*.mp3"))
        if not paths:
            raise RuntimeError("ffmpeg did not create any MIMO-ASR audio chunks")
        oversized = [
            path for path in paths
            if _mimo_data_url_size(path, "mp3") > MIMO_ASR_MAX_BASE64_BYTES
        ]
        if not oversized:
            chunks = []
            cursor = 0.0
            for index, path in enumerate(paths):
                duration = _probe_audio_duration(path) or float(chunk_seconds)
                end = cursor + duration
                if total_duration and index == len(paths) - 1:
                    end = min(end, float(total_duration))
                chunks.append({"path": str(path), "start": cursor, "end": end})
                cursor = end
            return chunks_dir, chunks
        if chunk_seconds <= 30:
            names = ", ".join(path.name for path in oversized[:3])
            raise RuntimeError(
                "MIMO-ASR payload still exceeds 10MB at 30-second chunks: "
                f"{names}. Lower MIMO_ASR_MP3_BITRATE."
            )
        chunk_seconds = max(30, chunk_seconds // 2)


async def transcribe_with_mimo(
    audio_path: str | Path,
    duration: float,
    progress_callback: ProgressCallback | None = None,
):
    audio_path = Path(audio_path)
    if not audio_path.is_file():
        raise RuntimeError(f"Audio file does not exist: {audio_path}")
    if not MIMO_API_KEY:
        raise RuntimeError("MIMO_API_KEY is not set. Add it to backend/.env.")

    language = MIMO_ASR_LANGUAGE or "auto"
    await _emit(progress_callback, progress_pct=0, detail="Preparing MIMO-ASR MP3 chunks")
    chunks_dir = None
    try:
        chunks_dir, chunks = await asyncio.to_thread(
            _prepare_mimo_mp3_chunks, audio_path, duration
        )
        total_chunks = len(chunks)
        concurrency = max(1, min(int(MIMO_ASR_CONCURRENCY or 1), total_chunks))
        await _emit(
            progress_callback,
            progress_pct=10,
            detail=f"Prepared {total_chunks} chunks (concurrency {concurrency})",
            api_requests_completed=0,
            api_requests_total=total_chunks,
        )
        completed_chunks = 0
        semaphore = asyncio.Semaphore(concurrency)

        async def transcribe_chunk(index: int, chunk: dict, fallback: bool = False):
            nonlocal completed_chunks
            async with semaphore:
                progress = 10 + int(completed_chunks / total_chunks * 85)
                mode = "fallback " if fallback else ""
                await _emit(
                    progress_callback,
                    progress_pct=progress,
                    detail=f"Calling MIMO-ASR {mode}chunk {index}/{total_chunks}",
                    api_requests_completed=completed_chunks,
                    api_requests_total=total_chunks,
                    api_request_current=index,
                )
                worker = asyncio.create_task(
                    asyncio.to_thread(_mimo_asr_transcribe_sync, chunk["path"], language)
                )
                started_at = asyncio.get_running_loop().time()
                heartbeat = max(5, int(MIMO_ASR_HEARTBEAT_SECONDS or 10))
                while not worker.done():
                    done, _ = await asyncio.wait({worker}, timeout=heartbeat)
                    if done:
                        break
                    elapsed = int(asyncio.get_running_loop().time() - started_at)
                    await _emit(
                        progress_callback,
                        progress_pct=progress,
                        detail=(
                            f"Waiting for MIMO-ASR {mode}chunk "
                            f"{index}/{total_chunks} ({elapsed}s)"
                        ),
                        api_requests_completed=completed_chunks,
                        api_requests_total=total_chunks,
                        api_request_current=index,
                    )
                try:
                    text = await worker
                except MimoAsrTransientError as error:
                    raise MimoAsrTransientError(
                        f"MIMO-ASR chunk {index}/{total_chunks} failed "
                        f"({chunk['start']:.1f}s-{chunk['end']:.1f}s): {error}"
                    ) from error
                except Exception as error:
                    raise RuntimeError(
                        f"MIMO-ASR chunk {index}/{total_chunks} failed "
                        f"({chunk['start']:.1f}s-{chunk['end']:.1f}s): {error}"
                    ) from error
                completed_chunks += 1
                await _emit(
                    progress_callback,
                    progress_pct=min(95, 10 + int(completed_chunks / total_chunks * 85)),
                    detail=f"Finished MIMO-ASR chunk {index}/{total_chunks}",
                    api_requests_completed=completed_chunks,
                    api_requests_total=total_chunks,
                    api_request_current=index,
                )
                return {
                    "start": chunk["start"],
                    "end": chunk["end"],
                    "text": text.strip(),
                }

        tasks = [
            asyncio.create_task(transcribe_chunk(index, chunk))
            for index, chunk in enumerate(chunks, start=1)
        ]
        initial_results = await asyncio.gather(*tasks, return_exceptions=True)
        segments: list[dict | None] = [None] * total_chunks
        fallback_chunks = []
        permanent_errors = []
        for index, (chunk, result) in enumerate(zip(chunks, initial_results), start=1):
            if isinstance(result, MimoAsrTransientError):
                fallback_chunks.append((index, chunk, result))
            elif isinstance(result, Exception):
                permanent_errors.append(result)
            else:
                segments[index - 1] = result
        if permanent_errors:
            raise permanent_errors[0]
        if fallback_chunks:
            if not MIMO_ASR_FALLBACK_RETRY:
                index, _, error = fallback_chunks[0]
                raise RuntimeError(
                    f"{error}. Set MIMO_ASR_FALLBACK_RETRY=1 to retry failed "
                    f"chunks sequentially. First failed chunk: {index}/{total_chunks}"
                )
            semaphore = asyncio.Semaphore(1)
            for index, chunk, first_error in fallback_chunks:
                await asyncio.sleep(_mimo_retry_delay(1))
                try:
                    segments[index - 1] = await transcribe_chunk(index, chunk, fallback=True)
                except MimoAsrTransientError as error:
                    raise RuntimeError(
                        f"{error}. Fallback retry also failed after: {first_error}"
                    ) from error

        final_segments = [segment for segment in segments if segment is not None]
        if not any(segment["text"] for segment in final_segments):
            raise RuntimeError("MIMO-ASR returned an empty transcript")
        await _emit(
            progress_callback,
            progress_pct=100,
            detail=f"{len(final_segments)} MIMO-ASR chunks",
            api_requests_completed=len(final_segments),
            api_requests_total=total_chunks,
            language=language,
        )
        return final_segments, f"mimo:{MIMO_ASR_MODEL}"
    finally:
        if chunks_dir:
            shutil.rmtree(chunks_dir, ignore_errors=True)


async def transcribe_audio(
    audio_path: str | Path,
    duration: float,
    provider: str,
    progress_callback: ProgressCallback | None = None,
):
    selected = (provider or "").strip().lower()
    if selected == "mimo":
        return await transcribe_with_mimo(audio_path, duration, progress_callback)
    if selected == "whisper":
        return await transcribe_with_whisper(audio_path, duration, progress_callback)
    raise ValueError(f"Unsupported ASR provider: {provider}")
