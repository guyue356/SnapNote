import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import pipeline
from app.database import Base, SnapTask, StageResult


class PipelineSubstepProgressTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        database_path = Path(self.temporary.name) / "pipeline-progress.db"
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.session_patch = patch.object(pipeline, "async_session", self.Session)
        self.session_patch.start()
        async with self.Session() as db:
            db.add(SnapTask(
                id="task-progress",
                filename="progress.mp4",
                video_path="progress.mp4",
                status="processing",
                current_stage="analyzing_style",
                progress=81,
                processing_state_json=json.dumps(pipeline.new_processing_state()),
            ))
            await db.commit()

    async def asyncTearDown(self):
        pipeline._progress_locks.clear()
        self.session_patch.stop()
        await self.engine.dispose()
        self.temporary.cleanup()

    async def test_initial_state_exposes_vision_and_ai_substeps(self):
        state = pipeline.new_processing_state()

        self.assertEqual(
            list(state["vision"]["substeps"]),
            ["semantic_selection", "frame_extraction", "coverage_audit", "coverage_repair"],
        )
        self.assertEqual(
            list(state["multimodal"]["substeps"]),
            ["keyframe_understanding", "clip_understanding", "style_synthesis", "note_enhancement"],
        )
        self.assertTrue(all(
            step["status"] == "queued"
            for branch in ("vision", "multimodal")
            for step in state[branch]["substeps"].values()
        ))

    async def test_substep_is_persisted_and_stage_result_is_completed(self):
        with patch.object(pipeline.sse_manager, "emit", new_callable=AsyncMock):
            await pipeline._record_pipeline_substep(
                "task-progress",
                "multimodal",
                "style_synthesis",
                "整片分析",
                "running",
                "正在分析",
                {"model": "mimo-v2.5"},
            )
            await pipeline._record_pipeline_substep(
                "task-progress",
                "multimodal",
                "style_synthesis",
                "整片分析",
                "completed",
                "分析完成",
                {"model": "mimo-v2.5", "usage": {"request_attempts": 2}},
            )

        async with self.Session() as db:
            task = await db.get(SnapTask, "task-progress")
            state = json.loads(task.processing_state_json)
            step = state["multimodal"]["substeps"]["style_synthesis"]
            self.assertEqual(step["status"], "completed")
            self.assertGreaterEqual(step["elapsed_seconds"], 0)
            self.assertEqual(step["metadata"]["usage"]["request_attempts"], 2)

            rows = (
                await db.execute(
                    select(StageResult).where(
                        StageResult.task_id == "task-progress",
                        StageResult.stage == "multimodal.style_synthesis",
                    )
                )
            ).scalars().all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].status, "completed")
            self.assertIsNotNone(rows[0].completed_at)

    async def test_pipeline_failure_closes_running_substep_records(self):
        with patch.object(pipeline.sse_manager, "emit", new_callable=AsyncMock):
            await pipeline._record_pipeline_substep(
                "task-progress",
                "multimodal",
                "note_enhancement",
                "笔记增强",
                "running",
                "正在增强",
                {"model": "mimo-v2.5"},
            )
            await pipeline._mark_pipeline_failed(
                "task-progress", RuntimeError("provider timeout")
            )

        async with self.Session() as db:
            task = await db.get(SnapTask, "task-progress")
            state = json.loads(task.processing_state_json)
            step = state["multimodal"]["substeps"]["note_enhancement"]
            self.assertEqual(task.status, "failed")
            self.assertEqual(step["status"], "failed")
            self.assertIsNotNone(step["completed_at"])

            row = (
                await db.execute(
                    select(StageResult).where(
                        StageResult.task_id == "task-progress",
                        StageResult.stage == "multimodal.note_enhancement",
                    )
                )
            ).scalar_one()
            self.assertEqual(row.status, "failed")
            self.assertEqual(row.error_message, "provider timeout")
            self.assertIsNotNone(row.completed_at)


if __name__ == "__main__":
    unittest.main()
