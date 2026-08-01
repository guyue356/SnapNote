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
FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "http://localhost:3000")
MAX_UPLOAD_SIZE_MB = int(os.getenv("MAX_UPLOAD_SIZE_MB", "2048"))
MAX_VIDEO_DURATION_SECONDS = int(os.getenv("MAX_VIDEO_DURATION_SECONDS", "3600"))

DEFAULT_ASR_PROVIDER = os.getenv("DEFAULT_ASR_PROVIDER", "mimo")
WHISPER_MODEL_SIZE = os.getenv("WHISPER_MODEL_SIZE", "small")
WHISPER_LANGUAGE = os.getenv("WHISPER_LANGUAGE", "zh") or None
ENABLE_LOCAL_WHISPER = os.getenv("ENABLE_LOCAL_WHISPER", "0").lower() in {"1", "true", "yes"}
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
MIMO_API_KEY = os.getenv("MIMO_API_KEY", "")

FRAME_FALLBACK_INTERVAL_SECONDS = int(os.getenv("FRAME_FALLBACK_INTERVAL_SECONDS", "60"))
FRAME_MAX_COUNT = int(os.getenv("FRAME_MAX_COUNT", "12"))


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
