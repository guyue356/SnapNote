import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import assistant
from app.database import AssistantCitation, AssistantConversation, AssistantMessage, Base


class AssistantConversationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.temp.name}/assistant.db")
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.patch = patch.object(assistant, "async_session", self.session)
        self.patch.start()

    async def asyncTearDown(self):
        self.patch.stop()
        await self.engine.dispose()
        self.temp.cleanup()

    async def test_unsupported_action_is_read_only_and_idempotent(self):
        conversation = await assistant.create_conversation()
        payload = {
            "content": "删除这个视频",
            "client_request_id": "request-1",
            "scope_override": None,
        }
        events = [event async for event in assistant.message_stream(conversation["id"], payload)]
        self.assertEqual([event["event"] for event in events], [
            "message_started", "token", "message_completed",
        ])
        self.assertIn("不能删除或修改", events[-1]["data"]["content"])

        duplicate = [event async for event in assistant.message_stream(conversation["id"], payload)]
        self.assertEqual([event["event"] for event in duplicate], ["message_completed"])
        history = await assistant.get_messages(conversation["id"])
        self.assertEqual(len(history["messages"]), 2)
        self.assertEqual(history["messages"][1]["status"], "completed")

    async def test_archive_restore_and_hard_delete_cascade(self):
        conversation = await assistant.create_conversation()
        payload = {
            "content": "删除这个视频",
            "client_request_id": "lifecycle-request",
            "scope_override": None,
        }
        await self._consume(assistant.message_stream(conversation["id"], payload))
        async with self.session() as db:
            message = (await db.execute(select(AssistantMessage).where(
                AssistantMessage.conversation_id == conversation["id"],
                AssistantMessage.role == "assistant",
            ))).scalar_one()
            db.add(AssistantCitation(
                id="citation-1", message_id=message.id, ordinal=1,
                asset_id="asset-1", task_id="task-1", asset_version_id="version-1",
                chunk_id="chunk-1", text_snapshot="引用快照",
            ))
            await db.commit()

        with self.assertRaises(assistant.AssistantError) as error:
            await assistant.delete_conversation(conversation["id"])
        self.assertEqual(error.exception.code, "CONVERSATION_NOT_ARCHIVED")

        archived = await assistant.archive_conversation(conversation["id"])
        self.assertEqual(archived["status"], "archived")
        self.assertEqual(await assistant.list_conversations(status="active"), [])
        self.assertEqual([item["id"] for item in await assistant.list_conversations(status="archived")], [conversation["id"]])

        restored = await assistant.restore_conversation(conversation["id"])
        self.assertEqual(restored["status"], "active")
        await assistant.archive_conversation(conversation["id"])
        result = await assistant.delete_conversation(conversation["id"])
        self.assertTrue(result["ok"])

        async with self.session() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(AssistantConversation)), 0)
            self.assertEqual(await db.scalar(select(func.count()).select_from(AssistantMessage)), 0)
            self.assertEqual(await db.scalar(select(func.count()).select_from(AssistantCitation)), 0)

    async def test_running_conversation_cannot_be_archived(self):
        conversation = await assistant.create_conversation()
        async with self.session() as db:
            db.add(AssistantMessage(
                id="pending-message", conversation_id=conversation["id"], role="assistant",
                status="streaming", client_request_id="pending-request",
            ))
            await db.commit()

        with self.assertRaises(assistant.AssistantError) as error:
            await assistant.archive_conversation(conversation["id"])
        self.assertEqual(error.exception.code, "CONVERSATION_BUSY")

    async def test_backend_startup_marks_interrupted_messages_failed(self):
        conversation = await assistant.create_conversation()
        async with self.session() as db:
            for status in ("pending", "retrieving", "streaming"):
                db.add(AssistantMessage(
                    id=f"{status}-message", conversation_id=conversation["id"], role="assistant",
                    status=status, client_request_id=f"{status}-request",
                    retrieval_snapshot_json='{"result_count": 2}',
                ))
            db.add(AssistantMessage(
                id="completed-message", conversation_id=conversation["id"], role="assistant",
                status="completed", client_request_id="completed-request", content="保留结果",
            ))
            await db.commit()

        recovered = await assistant.recover_interrupted_messages()

        self.assertEqual(recovered, 3)
        async with self.session() as db:
            rows = (await db.execute(select(AssistantMessage).where(
                AssistantMessage.conversation_id == conversation["id"],
            ))).scalars().all()
        by_id = {row.id: row for row in rows}
        for status in ("pending", "retrieving", "streaming"):
            row = by_id[f"{status}-message"]
            self.assertEqual(row.status, "failed")
            self.assertEqual(row.error_code, "SERVICE_RESTARTED")
            self.assertIn('"interruption": "service_restarted"', row.retrieval_snapshot_json)
        self.assertEqual(by_id["completed-message"].status, "completed")
        self.assertEqual(by_id["completed-message"].content, "保留结果")

    @staticmethod
    async def _consume(stream):
        return [event async for event in stream]

    def test_deduplicate_evidence_keeps_adjacent_time_ranges_separate(self):
        base = {
            "asset_version_id": "version-1",
            "text": "同一段 原文",
        }
        first = {**base, "chunk_id": "chunk-1", "start_time": 10.0, "end_time": 20.0}
        duplicate_id = {**first, "text": "不应覆盖首个命中"}
        duplicate_snapshot = {**first, "chunk_id": "chunk-2", "text": "同一段\n原文"}
        adjacent = {**base, "chunk_id": "chunk-3", "start_time": 20.0, "end_time": 30.0}

        result = assistant._deduplicate_evidence([
            first, duplicate_id, duplicate_snapshot, adjacent,
        ])

        self.assertEqual([item["chunk_id"] for item in result], ["chunk-1", "chunk-3"])
        self.assertEqual(
            [(item["start_time"], item["end_time"]) for item in result],
            [(10.0, 20.0), (20.0, 30.0)],
        )

    def test_retain_only_cited_evidence_and_renumber_markers(self):
        evidence = [
            {"chunk_id": "chunk-1", "start_time": 10.0, "end_time": 20.0},
            {"chunk_id": "chunk-2", "start_time": 20.0, "end_time": 30.0},
            {"chunk_id": "chunk-3", "start_time": 30.0, "end_time": 40.0},
        ]

        answer, cited = assistant._retain_cited_evidence(
            "结论来自第三段 [3]，第一段也支持 [1]，再次引用第三段 [3]。",
            evidence,
        )

        self.assertEqual(answer, "结论来自第三段 [2]，第一段也支持 [1]，再次引用第三段 [2]。")
        self.assertEqual([item["chunk_id"] for item in cited], ["chunk-1", "chunk-3"])
        self.assertEqual(
            [(item["start_time"], item["end_time"]) for item in cited],
            [(10.0, 20.0), (30.0, 40.0)],
        )


if __name__ == "__main__":
    unittest.main()
