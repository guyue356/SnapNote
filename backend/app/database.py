from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, Integer, String, Text, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .config import DATABASE_URL


def utcnow():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class SnapTask(Base):
    __tablename__ = "snap_tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    filename: Mapped[str] = mapped_column(String(500))
    video_path: Mapped[str] = mapped_column(Text)
    audio_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration: Mapped[float] = mapped_column(Float, default=0)
    status: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    current_stage: Mapped[str] = mapped_column(String(50), default="upload_complete")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    asr_provider: Mapped[str] = mapped_column(String(20), default="whisper")
    note_style: Mapped[str] = mapped_column(String(20), default="classroom")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    frames_json: Mapped[str] = mapped_column(Text, default="[]")
    transcripts_json: Mapped[str] = mapped_column(Text, default="[]")
    notes_json: Mapped[str] = mapped_column(Text, default="[]")
    visual_analysis_json: Mapped[str] = mapped_column(Text, default="{}")
    final_markdown: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class StageResult(Base):
    __tablename__ = "snap_stage_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(36), index=True)
    stage: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(20))
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


engine = create_async_engine(DATABASE_URL)
async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_db():
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        if DATABASE_URL.startswith("sqlite"):
            columns = (
                await connection.execute(text("PRAGMA table_info(snap_tasks)"))
            ).mappings().all()
            if "visual_analysis_json" not in {row["name"] for row in columns}:
                await connection.execute(text(
                    "ALTER TABLE snap_tasks ADD COLUMN visual_analysis_json "
                    "TEXT NOT NULL DEFAULT '{}'"
                ))
