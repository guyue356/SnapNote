from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class TaskCreated(BaseModel):
    task_id: str
    status: str


class TaskListItem(BaseModel):
    id: str
    filename: str
    duration: float
    status: str
    current_stage: str
    progress: int
    frame_count: int
    asr_provider: str
    note_style: str
    created_at: datetime


class RetryRequest(BaseModel):
    asr_provider: Literal["mimo", "whisper"] | None = None
