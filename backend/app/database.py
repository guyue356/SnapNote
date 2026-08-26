from datetime import datetime, timezone
import json

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.types import TypeDecorator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .config import DATABASE_URL, EMBEDDING_DIMENSIONS

try:
    from pgvector.sqlalchemy import Vector as PgVector
except ImportError:  # Keep the default SQLite demo install lightweight.
    PgVector = None


class EmbeddingVector(TypeDecorator):
    """pgvector on PostgreSQL and JSON text on the local SQLite fallback."""

    impl = Text
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            if PgVector is None:
                raise RuntimeError(
                    "PostgreSQL semantic retrieval requires the pgvector package"
                )
            return dialect.type_descriptor(PgVector(EMBEDDING_DIMENSIONS))
        return dialect.type_descriptor(Text())

    def process_bind_param(self, value, dialect):
        if value is None or dialect.name == "postgresql":
            return value
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    def process_result_value(self, value, dialect):
        if value is None or dialect.name == "postgresql":
            return value
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return None
        return value


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
    note_model: Mapped[str] = mapped_column(String(20), default="mimo")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    frames_json: Mapped[str] = mapped_column(Text, default="[]")
    transcripts_json: Mapped[str] = mapped_column(Text, default="[]")
    notes_json: Mapped[str] = mapped_column(Text, default="[]")
    visual_analysis_json: Mapped[str] = mapped_column(Text, default="{}")
    processing_state_json: Mapped[str] = mapped_column(Text, default="{}")
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


class SchemaMigration(Base):
    __tablename__ = "schema_migrations"

    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class KnowledgeAsset(Base):
    __tablename__ = "knowledge_assets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("snap_tasks.id", ondelete="CASCADE"), unique=True, index=True
    )
    owner_scope: Mapped[str] = mapped_column(String(100), default="local", index=True)
    status: Mapped[str] = mapped_column(String(20), default="not_built", index=True)
    current_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(500))
    missing_items_json: Mapped[str] = mapped_column(Text, default="[]")
    error_summary: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class KnowledgeAssetVersion(Base):
    __tablename__ = "knowledge_asset_versions"
    __table_args__ = (
        UniqueConstraint(
            "asset_id", "source_hash", "schema_version", "builder_version",
            name="uq_knowledge_asset_source_version",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    asset_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("knowledge_assets.id", ondelete="CASCADE"), index=True
    )
    source_hash: Mapped[str] = mapped_column(String(64), index=True)
    schema_version: Mapped[str] = mapped_column(String(30))
    builder_version: Mapped[str] = mapped_column(String(80))
    summary: Mapped[str] = mapped_column(Text, default="")
    duration: Mapped[float] = mapped_column(Float, default=0)
    language: Mapped[str] = mapped_column(String(20), default="zh")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class KnowledgeChapter(Base):
    __tablename__ = "knowledge_chapters"
    __table_args__ = (
        UniqueConstraint("asset_version_id", "ordinal", name="uq_knowledge_chapter_ordinal"),
        Index("ix_knowledge_chapter_time", "asset_version_id", "start_time"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    asset_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("knowledge_asset_versions.id", ondelete="CASCADE"), index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(500))
    summary: Mapped[str] = mapped_column(Text, default="")
    start_time: Mapped[float] = mapped_column(Float)
    end_time: Mapped[float] = mapped_column(Float)
    source_type: Mapped[str] = mapped_column(String(30))


class KnowledgeTranscriptSegment(Base):
    __tablename__ = "knowledge_transcript_segments"
    __table_args__ = (
        UniqueConstraint(
            "asset_version_id", "source_segment_index",
            name="uq_knowledge_transcript_source_index",
        ),
        Index("ix_knowledge_transcript_time", "asset_version_id", "start_time", "end_time"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    asset_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("knowledge_asset_versions.id", ondelete="CASCADE"), index=True
    )
    source_segment_index: Mapped[int] = mapped_column(Integer)
    start_time: Mapped[float] = mapped_column(Float)
    end_time: Mapped[float] = mapped_column(Float)
    text: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        UniqueConstraint("asset_version_id", "content_hash", name="uq_knowledge_chunk_content"),
        Index("ix_knowledge_chunk_type", "asset_version_id", "content_type"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    asset_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("knowledge_asset_versions.id", ondelete="CASCADE"), index=True
    )
    chapter_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("knowledge_chapters.id", ondelete="CASCADE"), nullable=True, index=True
    )
    content_type: Mapped[str] = mapped_column(String(30), index=True)
    source_ref: Mapped[str] = mapped_column(String(500))
    title: Mapped[str] = mapped_column(String(500), default="")
    text: Mapped[str] = mapped_column(Text)
    start_time: Mapped[float | None] = mapped_column(Float, nullable=True)
    end_time: Mapped[float | None] = mapped_column(Float, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)


class KnowledgeEmbedding(Base):
    """Rebuildable semantic index for a knowledge chunk.

    The chunk and its asset version remain the source of truth. This table is
    safe to delete and regenerate when the embedding model changes.
    """

    __tablename__ = "knowledge_embeddings"
    __table_args__ = (
        UniqueConstraint(
            "chunk_id", "model_name", "model_version",
            name="uq_knowledge_embedding_chunk_model",
        ),
        Index("ix_knowledge_embedding_version", "asset_version_id"),
        Index(
            "ix_knowledge_embedding_vector_hnsw", "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    chunk_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("knowledge_chunks.id", ondelete="CASCADE"), index=True
    )
    asset_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("knowledge_asset_versions.id", ondelete="CASCADE"), index=True
    )
    model_name: Mapped[str] = mapped_column(String(200))
    model_version: Mapped[str] = mapped_column(String(100), default="default")
    dimensions: Mapped[int] = mapped_column(Integer, default=EMBEDDING_DIMENSIONS)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    embedding: Mapped[list[float] | None] = mapped_column(EmbeddingVector(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class KnowledgeMedia(Base):
    __tablename__ = "knowledge_media"
    __table_args__ = (
        UniqueConstraint("asset_version_id", "frame_id", name="uq_knowledge_media_frame"),
        Index("ix_knowledge_media_time", "asset_version_id", "timestamp"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    asset_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("knowledge_asset_versions.id", ondelete="CASCADE"), index=True
    )
    frame_id: Mapped[str] = mapped_column(String(200))
    media_type: Mapped[str] = mapped_column(String(30), default="keyframe")
    relative_uri: Mapped[str] = mapped_column(String(1000))
    timestamp: Mapped[float] = mapped_column(Float)
    content_hash: Mapped[str] = mapped_column(String(64))
    availability: Mapped[str] = mapped_column(String(20), default="available")


class KnowledgeBuildRun(Base):
    __tablename__ = "knowledge_build_runs"
    __table_args__ = (
        Index("ix_knowledge_build_asset_started", "asset_id", "started_at"),
        Index(
            "uq_knowledge_build_running", "asset_id", unique=True,
            sqlite_where=text("status = 'running'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    asset_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("knowledge_assets.id", ondelete="CASCADE"), index=True
    )
    trigger: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(30), index=True)
    source_hash: Mapped[str] = mapped_column(String(64), default="")
    builder_version: Mapped[str] = mapped_column(String(80))
    stats_json: Mapped[str] = mapped_column(Text, default="{}")
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_summary: Mapped[str | None] = mapped_column(String(500), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


engine = create_async_engine(DATABASE_URL)
async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


if DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine.sync_engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, _):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


async def init_db():
    async with engine.begin() as connection:
        if DATABASE_URL.startswith("postgresql"):
            await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await connection.execute(text(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER PRIMARY KEY, name VARCHAR(200) NOT NULL, "
            "applied_at DATETIME NOT NULL)"
        ))
        if DATABASE_URL.startswith("sqlite"):
            tables = (await connection.execute(text(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='snap_tasks'"
            ))).all()
            if tables:
                columns = (
                    await connection.execute(text("PRAGMA table_info(snap_tasks)"))
                ).mappings().all()
                existing = {row["name"] for row in columns}
                if "visual_analysis_json" not in existing:
                    await connection.execute(text(
                        "ALTER TABLE snap_tasks ADD COLUMN visual_analysis_json "
                        "TEXT NOT NULL DEFAULT '{}'"
                    ))
                if "processing_state_json" not in existing:
                    await connection.execute(text(
                        "ALTER TABLE snap_tasks ADD COLUMN processing_state_json "
                        "TEXT NOT NULL DEFAULT '{}'"
                    ))
                if "note_model" not in existing:
                    await connection.execute(text(
                        "ALTER TABLE snap_tasks ADD COLUMN note_model "
                        "VARCHAR(20) NOT NULL DEFAULT 'mimo'"
                    ))
        await connection.run_sync(Base.metadata.create_all)
        applied = (await connection.execute(text(
            "SELECT version FROM schema_migrations WHERE version = 1"
        ))).first()
        if not applied:
            await connection.execute(
                text("INSERT INTO schema_migrations(version, name, applied_at) "
                     "VALUES (1, :name, :applied_at)"),
                {"name": "knowledge_asset_schema_v1", "applied_at": utcnow()},
            )
        vector_migration = (await connection.execute(text(
            "SELECT version FROM schema_migrations WHERE version = 2"
        ))).first()
        if not vector_migration:
            await connection.execute(
                text("INSERT INTO schema_migrations(version, name, applied_at) "
                     "VALUES (2, :name, :applied_at)"),
                {"name": "knowledge_embedding_index_v1", "applied_at": utcnow()},
            )
