import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import (
    Base,
    KnowledgeAsset,
    KnowledgeAssetVersion,
    KnowledgeChapter,
    KnowledgeChunk,
    KnowledgeEmbedding,
    KnowledgeMedia,
    KnowledgeTranscriptSegment,
    SnapTask,
)
from app import knowledge
from app.mimo_vision import _normalize_video_analysis
from app.pipeline import _merge_enhanced_blocks


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
        self.semantic_patch = patch.object(
            knowledge, "ENABLE_KNOWLEDGE_SEMANTIC_SEARCH", False
        )
        self.embedding_build_patch = patch.object(
            knowledge, "ENABLE_KNOWLEDGE_EMBEDDINGS", False
        )
        self.session_patch.start()
        self.tasks_patch.start()
        self.semantic_patch.start()
        self.embedding_build_patch.start()
        knowledge._semantic_unavailable_until = 0.0

    async def asyncTearDown(self):
        pending = tuple(knowledge._semantic_background_tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        knowledge._semantic_background_tasks.clear()
        knowledge._semantic_unavailable_until = 0.0
        knowledge._semantic_failure_reason = None
        knowledge._embedding_builds_in_progress = 0
        self.embedding_build_patch.stop()
        self.semantic_patch.stop()
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
            generated_title="Transformer 注意力机制详解",
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
                "video_title": "Transformer 注意力机制详解",
                "content_summary": "本视频介绍 Transformer 注意力机制。",
                "summary": "本视频介绍 Transformer 注意力机制。",
                "narrative_structure": [{
                    "title": "缩放点积注意力的计算步骤", "stage_role": "原理讲解",
                    "start_time": 0, "end_time": 60, "summary": "理解注意力计算。",
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
            asset = await db.get(KnowledgeAsset, first["asset_id"])
            self.assertEqual(asset.title, "Transformer 注意力机制详解")
            chapter = (await db.execute(select(KnowledgeChapter))).scalar_one()
            self.assertEqual(chapter.title, "缩放点积注意力的计算步骤")

        result = await knowledge.search_knowledge(
            "缩放点积注意力", asset_ids=[first["asset_id"]]
        )
        self.assertGreater(result["total"], 0)
        hit = result["results"][0]
        self.assertEqual(hit["asset_id"], first["asset_id"])
        self.assertEqual(hit["task_id"], "task-ready")
        self.assertEqual(hit["asset_version_id"], first["asset_version_id"])
        self.assertIsNotNone(hit["chunk_id"])
        self.assertTrue(hit["title"])
        self.assertGreaterEqual(hit["score"], 0)
        self.assertLessEqual(hit["score"], 1)

        natural_question = await knowledge.search_knowledge(
            "请问这个视频里是怎么计算注意力机制的？",
            asset_ids=[first["asset_id"]],
        )
        self.assertGreater(natural_question["total"], 0)
        self.assertGreaterEqual(natural_question["results"][0]["score"], 0.45)

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

    async def test_sqlite_semantic_index_is_rebuildable_and_combines_with_keyword_search(self):
        await self.create_task()
        built = await knowledge.build_knowledge_asset("task-ready")
        vector = [1.0] + [0.0] * 1023
        with patch("app.embedding.embed_texts", new=AsyncMock(side_effect=lambda texts, **_: [vector for _ in texts])), \
                patch.object(knowledge, "ENABLE_KNOWLEDGE_SEMANTIC_SEARCH", True):
            indexed = await knowledge.rebuild_knowledge_embeddings(
                asset_id=built["asset_id"]
            )
            result = await knowledge.search_knowledge(
                "一个完全不同的自然语言问题", asset_ids=[built["asset_id"]]
            )

        self.assertEqual(indexed["status"], "ready")
        self.assertGreater(indexed["indexed"], 0)
        self.assertEqual(result["retrieval_mode"], "hybrid")
        self.assertEqual(result["semantic_status"], "ready")
        self.assertEqual(
            result["semantic_index"]["indexed_chunks"],
            result["semantic_index"]["total_chunks"],
        )
        self.assertGreater(result["total"], 0)
        self.assertEqual(result["results"][0]["matched_field"], "semantic")
        async with self.Session() as db:
            self.assertGreater(
                await db.scalar(select(func.count()).select_from(KnowledgeEmbedding)), 0
            )

    async def test_semantic_search_skips_model_when_no_matching_index_exists(self):
        await self.create_task()
        built = await knowledge.build_knowledge_asset("task-ready")
        embed_query = AsyncMock()

        with patch.object(knowledge, "ENABLE_KNOWLEDGE_SEMANTIC_SEARCH", True), \
                patch("app.embedding.embed_query", new=embed_query):
            result = await knowledge.search_knowledge(
                "注意力", asset_ids=[built["asset_id"]]
            )

        embed_query.assert_not_awaited()
        self.assertEqual(result["retrieval_mode"], "keyword")
        self.assertTrue(result["degraded_search"])
        self.assertEqual(result["semantic_status"], "partial")
        self.assertEqual(result["semantic_index"]["indexed_chunks"], 0)
        self.assertGreater(result["total"], 0)

    async def test_slow_semantic_model_falls_back_without_blocking_keyword_results(self):
        await self.create_task()
        built = await knowledge.build_knowledge_asset("task-ready")
        vector = [1.0] + [0.0] * 1023
        with patch("app.embedding.embed_texts", new=AsyncMock(
            side_effect=lambda texts, **_: [vector for _ in texts]
        )):
            await knowledge.rebuild_knowledge_embeddings(asset_id=built["asset_id"])

        release_model = asyncio.Event()

        async def slow_query(_query):
            await release_model.wait()
            return vector

        started = time.perf_counter()
        with patch.object(knowledge, "ENABLE_KNOWLEDGE_SEMANTIC_SEARCH", True), \
                patch.object(knowledge, "SEMANTIC_QUERY_TIMEOUT_SECONDS", 0.01), \
                patch.object(knowledge, "SEMANTIC_FAILURE_COOLDOWN_SECONDS", 60), \
                patch("app.embedding.embed_query", new=AsyncMock(side_effect=slow_query)):
            result = await knowledge.search_knowledge(
                "注意力", asset_ids=[built["asset_id"]]
            )
            release_model.set()
            pending = tuple(knowledge._semantic_background_tasks)
            if pending:
                await asyncio.gather(*pending)

        self.assertLess(time.perf_counter() - started, 0.5)
        self.assertEqual(result["retrieval_mode"], "keyword")
        self.assertTrue(result["degraded_search"])
        self.assertEqual(result["semantic_status"], "indexing")
        self.assertGreater(result["total"], 0)
        self.assertEqual(knowledge._semantic_unavailable_until, 0.0)

    async def test_semantic_model_error_is_reported_separately_from_index_coverage(self):
        await self.create_task()
        built = await knowledge.build_knowledge_asset("task-ready")
        vector = [1.0] + [0.0] * 1023
        with patch("app.embedding.embed_texts", new=AsyncMock(
            side_effect=lambda texts, **_: [vector for _ in texts]
        )):
            await knowledge.rebuild_knowledge_embeddings(asset_id=built["asset_id"])

        with patch.object(knowledge, "ENABLE_KNOWLEDGE_SEMANTIC_SEARCH", True), \
                patch("app.embedding.embed_query", new=AsyncMock(
                    side_effect=RuntimeError("local model is incomplete")
                )):
            result = await knowledge.search_knowledge(
                "注意力", asset_ids=[built["asset_id"]]
            )

        self.assertEqual(result["retrieval_mode"], "keyword")
        self.assertEqual(result["semantic_status"], "model_error")
        self.assertIn("local model is incomplete", result["semantic_error"])

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

    def test_chinese_question_extracts_topic_terms(self):
        terms = knowledge._query_terms("请问这个视频里是怎么计算注意力机制的？")

        self.assertIn("注意力机制", terms)
        self.assertIn("计算", terms)
        self.assertNotIn("怎么", terms)


class GeneratedTitleValidationTests(unittest.TestCase):
    def test_normalizes_content_title_and_keeps_stage_role_separate(self):
        result = _normalize_video_analysis({
            "video_title": "  关键帧提取与质量评估方法  ",
            "content_summary": "本视频讲解如何综合清晰度、亮度和稳定性筛选关键帧。",
            "visual_summary": "画面以录屏和指标示意图为主。",
            "narrative_structure": [{
                "title": "开篇", "stage_role": "引入",
                "start_time": 0, "end_time": 30,
                "summary": "介绍清晰度、亮度和稳定性三个关键帧评价指标。",
            }],
        })

        self.assertEqual(result["video_title"], "关键帧提取与质量评估方法")
        self.assertEqual(result["summary"], result["content_summary"])
        chapter = result["narrative_structure"][0]
        self.assertEqual(chapter["title"], "介绍清晰度、亮度和稳定性三个关键帧评价指标")
        self.assertEqual(chapter["stage_role"], "引入")
        self.assertEqual(chapter["stage"], "引入")

    def test_preserves_legacy_structure_fields(self):
        result = _normalize_video_analysis({
            "summary": "介绍缩放点积注意力。",
            "narrative_structure": [{
                "stage": "缩放点积注意力", "start_time": 0, "end_time": 60,
                "description": "解释 Query 和 Key 的点积计算。",
            }],
        })

        chapter = result["narrative_structure"][0]
        self.assertEqual(chapter["title"], "解释 Query 和 Key 的点积计算")
        self.assertEqual(chapter["stage_role"], "缩放点积注意力")
        self.assertEqual(chapter["summary"], "解释 Query 和 Key 的点积计算。")

    def test_model_can_only_replace_editable_note_content(self):
        base = [{
            "id": "block-1", "frame_id": "frame-1", "timestamp": 10,
            "end_time": 20, "title": "关键镜头 1", "summary": "原摘要",
            "key_points": ["原知识点"], "review_questions": ["原问题"],
            "ocr_text": "可信 OCR", "image_url": "/frames/1.jpg", "confidence": 0.8,
            "visual_analysis": {"scene": "录屏"}, "clip_analysis": {"motion": "静止"},
            "shot_metrics": {"quality_score": 0.9},
        }]
        enhanced = [{
            "id": "block-1", "frame_id": "tampered-frame", "timestamp": 999,
            "image_url": "/tampered.jpg", "ocr_text": "篡改", "title": "标准",
            "summary": "讲解清晰度和稳定性如何共同决定关键帧质量。",
            "key_points": ["清晰度反映画面可读性", "稳定性影响截图质量"],
            "review_questions": ["筛选关键帧时为什么要兼顾清晰度和稳定性？"],
        }]

        result = _merge_enhanced_blocks(base, enhanced)[0]

        self.assertEqual(result["frame_id"], "frame-1")
        self.assertEqual(result["timestamp"], 10)
        self.assertEqual(result["image_url"], "/frames/1.jpg")
        self.assertEqual(result["ocr_text"], "可信 OCR")
        self.assertEqual(result["title"], "讲解清晰度和稳定性如何共同决定关键帧质量")


if __name__ == "__main__":
    unittest.main()
