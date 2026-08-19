import os
import shutil
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = BACKEND_DIR.parent
load_dotenv(BACKEND_DIR / ".env")

STORAGE_ROOT = Path(os.getenv("STORAGE_ROOT", PROJECT_DIR / "storage")).resolve()
TASKS_DIR = STORAGE_ROOT / "tasks"
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite+aiosqlite:///{STORAGE_ROOT / 'app.db'}")
FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "http://127.0.0.1:43871")
MAX_UPLOAD_SIZE_MB = int(os.getenv("MAX_UPLOAD_SIZE_MB", "2048"))
MAX_VIDEO_DURATION_SECONDS = int(os.getenv("MAX_VIDEO_DURATION_SECONDS", "3600"))
ENABLE_PIPELINE_PARALLELISM = os.getenv("ENABLE_PIPELINE_PARALLELISM", "1").lower() in {
    "1", "true", "yes", "on"
}


def _flag(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).lower() in {"1", "true", "yes", "on"}


ENABLE_KNOWLEDGE_AUTO_BUILD = _flag("ENABLE_KNOWLEDGE_AUTO_BUILD")
ENABLE_KNOWLEDGE_SEARCH = _flag("ENABLE_KNOWLEDGE_SEARCH")
ENABLE_KNOWLEDGE_STATUS_UI = _flag("ENABLE_KNOWLEDGE_STATUS_UI")
ENABLE_KNOWLEDGE_REBUILD = _flag("ENABLE_KNOWLEDGE_REBUILD")
KNOWLEDGE_OWNER_SCOPE = os.getenv("KNOWLEDGE_OWNER_SCOPE", "local").strip() or "local"

DEFAULT_ASR_PROVIDER = os.getenv("DEFAULT_ASR_PROVIDER", "whisper").strip().lower()
if DEFAULT_ASR_PROVIDER not in {"whisper", "mimo"}:
    DEFAULT_ASR_PROVIDER = "whisper"
ENABLE_LOCAL_WHISPER = os.getenv("ENABLE_LOCAL_WHISPER", "1").lower() in {"1", "true", "yes", "on"}
WHISPER_MODEL_SIZE = os.getenv("WHISPER_MODEL_SIZE", "large-v3")
WHISPER_LANGUAGE = os.getenv("WHISPER_LANGUAGE", "zh") or None
_default_whisper_cache = os.getenv("HF_HUB_CACHE") or (
    Path.home() / ".cache" / "huggingface" / "hub"
)
WHISPER_CACHE_DIR = Path(
    os.getenv("WHISPER_CACHE_DIR") or _default_whisper_cache
).expanduser().resolve()


def _detect_whisper_device() -> tuple[str, str]:
    configured_device = os.getenv("WHISPER_DEVICE", "").strip().lower()
    configured_compute = os.getenv("WHISPER_COMPUTE_TYPE", "").strip()
    if configured_device:
        default_compute = "int8_float16" if configured_device == "cuda" else "int8"
        return configured_device, configured_compute or default_compute
    try:
        import ctranslate2

        if ctranslate2.get_supported_compute_types("cuda"):
            return "cuda", configured_compute or "int8_float16"
    except Exception:
        pass
    return "cpu", configured_compute or "int8"


WHISPER_DEVICE, WHISPER_COMPUTE_TYPE = _detect_whisper_device()

MIMO_API_KEY = os.getenv("MIMO_API_KEY", "")
_default_mimo_base_url = (
    "https://token-plan-cn.xiaomimimo.com/v1"
    if MIMO_API_KEY.strip().startswith("tp-")
    else "https://api.xiaomimimo.com/v1"
)
MIMO_BASE_URL = (os.getenv("MIMO_BASE_URL") or _default_mimo_base_url).rstrip("/")
MIMO_ASR_MODEL = os.getenv("MIMO_ASR_MODEL", "mimo-v2.5-asr")
MIMO_ASR_LANGUAGE = os.getenv("MIMO_ASR_LANGUAGE", os.getenv("WHISPER_LANGUAGE", "zh")) or "auto"
MIMO_ASR_CHUNK_SECONDS = int(os.getenv("MIMO_ASR_CHUNK_SECONDS", "90"))
MIMO_ASR_MP3_BITRATE = os.getenv("MIMO_ASR_MP3_BITRATE", "32k")
MIMO_ASR_CONCURRENCY = int(os.getenv("MIMO_ASR_CONCURRENCY", "3"))
MIMO_ASR_TIMEOUT_SECONDS = int(os.getenv("MIMO_ASR_TIMEOUT_SECONDS", "90"))
MIMO_ASR_MAX_ATTEMPTS = int(os.getenv("MIMO_ASR_MAX_ATTEMPTS", "2"))
MIMO_ASR_HEARTBEAT_SECONDS = int(os.getenv("MIMO_ASR_HEARTBEAT_SECONDS", "10"))
MIMO_ASR_FALLBACK_RETRY = os.getenv("MIMO_ASR_FALLBACK_RETRY", "0").lower() in {
    "1", "true", "yes", "on"
}
ENABLE_MIMO_VISION = os.getenv("ENABLE_MIMO_VISION", "1").lower() in {
    "1", "true", "yes", "on"
}
MIMO_VISION_REQUIRED = os.getenv("MIMO_VISION_REQUIRED", "0").lower() in {
    "1", "true", "yes", "on"
}
MIMO_VISION_MODEL = os.getenv("MIMO_VISION_MODEL", "mimo-v2.5")
DEFAULT_NOTE_MODEL = os.getenv("DEFAULT_NOTE_MODEL", "mimo").strip().lower()
if DEFAULT_NOTE_MODEL not in {"mimo", "deepseek"}:
    DEFAULT_NOTE_MODEL = "mimo"
MIMO_VISION_IMAGE_BATCH_SIZE = int(os.getenv("MIMO_VISION_IMAGE_BATCH_SIZE", "8"))
MIMO_VISION_CONCURRENCY = int(os.getenv("MIMO_VISION_CONCURRENCY", "2"))
MIMO_VISION_TIMEOUT_SECONDS = int(os.getenv("MIMO_VISION_TIMEOUT_SECONDS", "180"))
MIMO_VISION_MAX_ATTEMPTS = int(os.getenv("MIMO_VISION_MAX_ATTEMPTS", "3"))
MIMO_VISION_MAX_CLIPS = int(os.getenv("MIMO_VISION_MAX_CLIPS", "4"))
MIMO_VISION_CLIP_SECONDS = float(os.getenv("MIMO_VISION_CLIP_SECONDS", "8"))
MIMO_VISION_VIDEO_FPS = float(os.getenv("MIMO_VISION_VIDEO_FPS", "2"))
AUDIO_SAMPLE_RATE = 16000

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")

FRAME_FALLBACK_INTERVAL_SECONDS = int(os.getenv("FRAME_FALLBACK_INTERVAL_SECONDS", "60"))
FRAME_MAX_COUNT = int(os.getenv("FRAME_MAX_COUNT", "24"))
FRAME_ANALYSIS_FPS = float(os.getenv("FRAME_ANALYSIS_FPS", "2"))
FRAME_ANALYSIS_WIDTH = int(os.getenv("FRAME_ANALYSIS_WIDTH", "320"))
FRAME_OUTPUT_WIDTH = int(os.getenv("FRAME_OUTPUT_WIDTH", "736"))
SCENE_CHANGE_THRESHOLD = float(os.getenv("SCENE_CHANGE_THRESHOLD", "0.24"))
SCENE_MIN_DURATION_SECONDS = float(os.getenv("SCENE_MIN_DURATION_SECONDS", "1.5"))
SCENE_MAX_DURATION_SECONDS = float(os.getenv("SCENE_MAX_DURATION_SECONDS", "45"))
FRAME_DYNAMIC_THRESHOLD = float(os.getenv("FRAME_DYNAMIC_THRESHOLD", "0.12"))
FRAME_PHASH_THRESHOLD = int(os.getenv("FRAME_PHASH_THRESHOLD", "6"))


def _binary(name: str, explicit: str) -> str:
    configured = os.getenv(explicit, "")
    if configured and Path(configured).exists():
        return configured
    discovered = shutil.which(name)
    if discovered:
        return discovered
    bundled = PROJECT_DIR.parent / "ffmpeg" / "bin" / f"{name}.exe"
    return str(bundled) if bundled.exists() else name


FFMPEG_BIN = _binary("ffmpeg", "FFMPEG_BIN")
FFPROBE_BIN = _binary("ffprobe", "FFPROBE_BIN")

TASKS_DIR.mkdir(parents=True, exist_ok=True)
