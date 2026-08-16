import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import (
    Base,
    KnowledgeAsset,
    KnowledgeAssetVersion,
    KnowledgeChapter,
    KnowledgeChunk,
    KnowledgeMedia,
    KnowledgeTranscriptSegment,
    SnapTask,
)
from app import knowledge


class KnowledgeAssetIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "tasks"
        self.root.mkdir()
        database_path = Path(self.temporary.name) / "knowledge.db"
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")

        @event.listens_for(self.engine.sync_engine, "connect")
        def enable_foreign_keys(connection, _):
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.session_patch = patch.object(knowledge, "async_session", self.Session)
        self.tasks_patch = patch.object(knowledge, "TASKS_DIR", self.root)
        self.session_patch.start()
        self.tasks_patch.start()

    async def asyncTearDown(self):
        self.session_patch.stop()
        self.tasks_patch.stop()
        await self.engine.dispose()
        self.temporary.cleanup()

    async def create_task(self, task_id="task-ready", with_frame=True):
        task_dir = self.root / task_id
        frames_dir = task_dir / "frames"
        frames_dir.mkdir(parents=True)
        video = task_dir / "source.mp4"
        video.write_bytes(b"video")
        image_url = f"/storage/tasks/{task_id}/frames/frame-1.jpg"
        if with_frame:
            (frames_dir / "frame-1.jpg").write_bytes(b"image")
        frames = [{"id": "frame-1", "timestamp": 8, "image_url": image_url}] if with_frame else []
        task = SnapTask(
            id=task_id,
            filename="Transformer 核心原理.mp4",
            video_path=str(video),
            duration=120,
            status="completed",
            current_stage="complete",
            progress=100,
            frames_json=json.dumps(frames, ensure_ascii=False),
            transcripts_json=json.dumps([
                {"start": 2, "end": 12, "text": "缩放点积注意力先计算 Query 和 Key 的点积。"},
                {"start": 12, "end": 20, "text": "然后除以维度的平方根并进行归一化。"},
            ], ensure_ascii=False),
            notes_json=json.dumps([{
                "id": "note-1", "timestamp": 0, "end_time": 60,
                "title": "注意力计算", "summary": "解释缩放点积注意力的完整步骤。",
                "key_points": ["Query 与 Key 点积"],
            }], ensure_ascii=False),
            visual_analysis_json=json.dumps({
                "summary": "本视频介绍 Transformer 注意力机制。",
                "narrative_structure": [{
                    "stage": "缩放点积注意力", "start_time": 0,
                    "end_time": 60, "description": "理解注意力计算。",
                }],
            }, ensure_ascii=False),
        )
        async with self.Session() as db:
            db.add(task)
            await db.commit()
        return task

    async def test_build_is_idempotent_and_search_results_are_citable(self):
        await self.create_task()

        first = await knowledge.build_knowledge_asset("task-ready")
        second = await knowledge.build_knowledge_asset("task-ready")

        self.assertEqual(first["status"], "ready")
        self.assertTrue(second["skipped"])
        async with self.Session() as db:
            self.assertEqual((await db.scalar(select(func.count()).select_from(KnowledgeAsset))), 1)
            self.assertEqual((await db.scalar(select(func.count()).select_from(KnowledgeAssetVersion))), 1)
            self.assertGreater((await db.scalar(select(func.count()).select_from(KnowledgeChunk))), 0)

        result = await knowledge.search_knowledge(
            "缩放点积注意力", asset_ids=[first["asset_id"]]
        )
        self.assertGreater(result["total"], 0)
        hit = result["results"][0]
        self.assertEqual(hit["asset_id"], first["asset_id"])
        self.assertEqual(hit["asset_version_id"], first["asset_version_id"])
        self.assertIsNotNone(hit["chunk_id"])
        self.assertGreaterEqual(hit["score"], 0)
        self.assertLessEqual(hit["score"], 1)

        transcript = await knowledge.get_transcript(first["asset_id"], 0, 30)
        self.assertEqual(len(transcript["segments"]), 2)
        self.assertIn("Query", transcript["segments"][0]["text"])

        wildcard = await knowledge.search_knowledge("%", asset_ids=[first["asset_id"]])
        self.assertEqual(wildcard["results"], [])

    async def test_source_change_creates_a_new_immutable_version(self):
        await self.create_task()
        first = await knowledge.build_knowledge_asset("task-ready")
        async with self.Session() as db:
            task = await db.get(SnapTask, "task-ready")
            task.transcripts_json = json.dumps([
                {"start": 2, "end": 12, "text": "更新后的注意力原文。"}
            ], ensure_ascii=False)
            await db.commit()

        second = await knowledge.build_knowledge_asset("task-ready")

        self.assertNotEqual(first["asset_version_id"], second["asset_version_id"])
        async with self.Session() as db:
            count = await db.scalar(select(func.count()).select_from(KnowledgeAssetVersion))
            self.assertEqual(count, 2)

    async def test_missing_media_builds_a_degraded_but_searchable_asset(self):
        await self.create_task("task-degraded", with_frame=False)

        result = await knowledge.build_knowledge_asset("task-degraded")

        self.assertEqual(result["status"], "degraded")
        self.assertIn("缺少画面引用", result["missing_items"])
        search = await knowledge.search_knowledge(
            "注意力", asset_ids=[result["asset_id"]]
        )
        self.assertGreater(search["total"], 0)

    async def test_remove_cascades_all_derived_entities(self):
        await self.create_task()
        result = await knowledge.build_knowledge_asset("task-ready")

        async with self.Session() as db:
            removed = await knowledge.remove_knowledge_for_task("task-ready", db)
            await db.commit()
        self.assertTrue(removed)

        async with self.Session() as db:
            for model in (
                KnowledgeAsset, KnowledgeAssetVersion, KnowledgeChapter,
                KnowledgeTranscriptSegment, KnowledgeChunk, KnowledgeMedia,
            ):
                count = await db.scalar(select(func.count()).select_from(model))
                self.assertEqual(count, 0, model.__tablename__)
        search = await knowledge.search_knowledge(
            "注意力", asset_ids=[result["asset_id"]]
        )
        self.assertEqual(search["results"], [])

    async def test_damaged_json_fails_without_modifying_source_task(self):
        task_dir = self.root / "task-broken"
        task_dir.mkdir()
        video = task_dir / "source.mp4"
        video.write_bytes(b"video")
        async with self.Session() as db:
            db.add(SnapTask(
                id="task-broken", filename="broken.mp4", video_path=str(video),
                duration=5, status="completed", current_stage="complete", progress=100,
                frames_json="{", transcripts_json="{", notes_json="{",
                visual_analysis_json="{", final_markdown="",
            ))
            await db.commit()

        result = await knowledge.build_knowledge_asset("task-broken")

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "NO_SEARCHABLE_CONTENT")
        async with self.Session() as db:
            task = await db.get(SnapTask, "task-broken")
            asset = await db.get(KnowledgeAsset, result["asset_id"])
            self.assertEqual(task.status, "completed")
            self.assertEqual(asset.status, "failed")


class KnowledgeValidationTests(unittest.TestCase):
    def test_media_path_traversal_is_rejected(self):
        self.assertIsNone(knowledge._relative_media_uri("task", "/storage/tasks/task/../secret"))
        self.assertIsNone(knowledge._relative_media_uri("task", "/storage/tasks/task/%2e%2e/secret"))

    def test_search_text_normalization_preserves_chinese(self):
        self.assertEqual(knowledge._normalized_text("  缩放\n点积  "), "缩放 点积")


if __name__ == "__main__":
    unittest.main()
