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


class KnowledgeSearchRequest(BaseModel):
    query: str
    asset_ids: list[str] | None = None
    content_types: list[
        Literal["video_summary", "chapter_summary", "transcript", "note"]
    ] | None = None
    top_k: int = 8
    owner_scope: str = "local"


class AssistantScope(BaseModel):
    asset_ids: list[str] | None = None
    content_types: list[Literal["video_summary", "chapter_summary", "transcript", "note"]] | None = None
    created_after: str | None = None
    created_before: str | None = None


class AssistantConversationCreate(BaseModel):
    owner_scope: str = "local"
    default_scope: AssistantScope = AssistantScope()


class AssistantScopeUpdate(BaseModel):
    scope: AssistantScope


class AssistantMessageCreate(BaseModel):
    content: str
    client_request_id: str
    scope_override: AssistantScope | None = None
