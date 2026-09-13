"""Constrained, local-only orchestration for SnapNote's knowledge assistant."""

import json
import re
import unicodedata
import uuid
from datetime import datetime, timezone
from typing import AsyncIterator

from sqlalchemy import and_, delete, func, or_, select

from .assistant_llm import AssistantLLMUnavailable, generate_answer, validate_citations
from .config import (
    ASSISTANT_MAX_ASSET_RESULTS,
    ASSISTANT_MAX_CONTEXT_CHARS,
    ASSISTANT_MAX_HISTORY_MESSAGES,
    ASSISTANT_MIN_EVIDENCE_SCORE,
    ASSISTANT_TOP_K,
    KNOWLEDGE_OWNER_SCOPE,
)
from .database import (
    AssistantCitation,
    AssistantConversation,
    AssistantMessage,
    KnowledgeAsset,
    KnowledgeAssetVersion,
    KnowledgeChapter,
    KnowledgeChunk,
    KnowledgeMedia,
    async_session,
    utcnow,
)
from .knowledge import SEARCHABLE_STATUSES, search_knowledge

CONTENT_TYPES = {"video_summary", "chapter_summary", "transcript", "note"}
DEFAULT_SCOPE = {"asset_ids": None, "content_types": None, "created_after": None, "created_before": None}
RUNNING_MESSAGE_STATUSES = {"pending", "retrieving", "streaming"}
UNSUPPORTED_RE = re.compile(r"(删除|修改|编辑|重建索引|覆盖|清空).*(视频|资产|笔记|知识)")
RECENT_RE = re.compile(r"最近|最新|刚刚|近来")
ASSET_RE = re.compile(r"视频|资产|课程|会议|笔记|列出|哪些|哪个|找出|找到|搜索|查找")


class AssistantError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _json(raw: str | None, fallback):
    try:
        value = json.loads(raw or "")
        return value if isinstance(value, type(fallback)) else fallback
    except (TypeError, json.JSONDecodeError):
        return fallback


def _iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise AssistantError("INVALID_SCOPE", "时间筛选格式无效") from error
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _clean_scope(raw: dict | None) -> dict:
    raw = raw or {}
    asset_ids = raw.get("asset_ids")
    if asset_ids is not None:
        if not isinstance(asset_ids, list) or len(asset_ids) > 100 or any(not isinstance(x, str) for x in asset_ids):
            raise AssistantError("INVALID_SCOPE", "视频范围最多选择 100 个资产")
        asset_ids = list(dict.fromkeys(asset_ids))
    content_types = raw.get("content_types")
    if content_types is not None:
        if not isinstance(content_types, list) or not set(content_types).issubset(CONTENT_TYPES):
            raise AssistantError("INVALID_SCOPE", "内容类型不受支持")
        content_types = list(dict.fromkeys(content_types))
    _iso(raw.get("created_after"))
    _iso(raw.get("created_before"))
    return {
        "asset_ids": asset_ids,
        "content_types": content_types,
        "created_after": raw.get("created_after"),
        "created_before": raw.get("created_before"),
    }


def _scope_label(scope: dict, asset_count: int | None = None) -> str:
    ids = scope.get("asset_ids")
    if ids is not None:
        video = f"{len(ids)} 个视频"
    else:
        video = "全部资产" if asset_count is None else f"全部资产 · {asset_count} 个视频"
    types = scope.get("content_types")
    type_label = "全部内容" if not types else "、".join({
        "video_summary": "视频摘要", "chapter_summary": "章节", "transcript": "原文", "note": "笔记"
    }[item] for item in types)
    return f"{video} · {type_label}"


async def _accessible_assets(scope: dict, owner_scope: str) -> list[KnowledgeAsset]:
    after, before = _iso(scope.get("created_after")), _iso(scope.get("created_before"))
    filters = [KnowledgeAsset.owner_scope == owner_scope, KnowledgeAsset.status.in_(SEARCHABLE_STATUSES)]
    if scope.get("asset_ids") is not None:
        filters.append(KnowledgeAsset.id.in_(scope["asset_ids"]))
    if after:
        filters.append(KnowledgeAsset.updated_at >= after)
    if before:
        filters.append(KnowledgeAsset.updated_at <= before)
    async with async_session() as db:
        return list((await db.execute(select(KnowledgeAsset).where(*filters).order_by(
            KnowledgeAsset.updated_at.desc(), KnowledgeAsset.id.asc()
        ))).scalars().all())


def detect_intent(content: str) -> str:
    if UNSUPPORTED_RE.search(content):
        return "unsupported_action"
    return "asset_query" if ASSET_RE.search(content) and not re.search(r"如何|为什么|怎么|解释|观点|总结|原话|确定", content) else "knowledge_qa"


async def create_conversation(owner_scope: str = KNOWLEDGE_OWNER_SCOPE, default_scope: dict | None = None) -> dict:
    scope = _clean_scope(default_scope or DEFAULT_SCOPE)
    conversation = AssistantConversation(
        id=str(uuid.uuid4()), owner_scope=owner_scope, title="新会话",
        default_scope_json=json.dumps(scope, ensure_ascii=False),
    )
    async with async_session() as db:
        db.add(conversation)
        await db.commit()
    return _conversation_payload(conversation, scope)


async def recover_interrupted_messages() -> int:
    """Close assistant runs that cannot survive a backend process restart."""
    async with async_session() as db:
        rows = (await db.execute(select(AssistantMessage).where(
            AssistantMessage.role == "assistant",
            AssistantMessage.status.in_(RUNNING_MESSAGE_STATUSES),
        ))).scalars().all()
        for row in rows:
            retrieval = _json(row.retrieval_snapshot_json, {})
            retrieval["interruption"] = "service_restarted"
            row.status = "failed"
            row.error_code = "SERVICE_RESTARTED"
            row.retrieval_snapshot_json = json.dumps(retrieval, ensure_ascii=False)
        if rows:
            await db.commit()
        return len(rows)


def _conversation_payload(item: AssistantConversation, scope: dict | None = None) -> dict:
    scope = scope if scope is not None else _json(item.default_scope_json, DEFAULT_SCOPE)
    return {
        "id": item.id, "owner_scope": item.owner_scope, "title": item.title,
        "status": item.status, "default_scope": scope,
        "scope_label": _scope_label(scope), "created_at": item.created_at, "updated_at": item.updated_at,
    }


async def list_conversations(
    owner_scope: str = KNOWLEDGE_OWNER_SCOPE,
    status: str = "active",
) -> list[dict]:
    if status not in {"active", "archived"}:
        raise AssistantError("INVALID_CONVERSATION_STATUS", "会话状态不受支持")
    async with async_session() as db:
        rows = (await db.execute(select(AssistantConversation).where(
            AssistantConversation.owner_scope == owner_scope,
            AssistantConversation.status == status,
        ).order_by(AssistantConversation.updated_at.desc()).limit(50))).scalars().all()
        return [_conversation_payload(row) for row in rows]


async def _has_running_message(db, conversation_id: str) -> bool:
    count = await db.scalar(select(func.count()).select_from(AssistantMessage).where(
        AssistantMessage.conversation_id == conversation_id,
        AssistantMessage.status.in_(RUNNING_MESSAGE_STATUSES),
    ))
    return bool(count)


async def archive_conversation(
    conversation_id: str,
    owner_scope: str = KNOWLEDGE_OWNER_SCOPE,
) -> dict:
    async with async_session() as db:
        row = await db.get(AssistantConversation, conversation_id)
        if not row or row.owner_scope != owner_scope:
            raise AssistantError("CONVERSATION_NOT_FOUND", "会话不存在或不可访问")
        if row.status == "archived":
            return _conversation_payload(row)
        if await _has_running_message(db, conversation_id):
            raise AssistantError("CONVERSATION_BUSY", "回答生成期间不能归档会话")
        row.status = "archived"
        row.updated_at = utcnow()
        await db.commit()
        return _conversation_payload(row)


async def restore_conversation(
    conversation_id: str,
    owner_scope: str = KNOWLEDGE_OWNER_SCOPE,
) -> dict:
    async with async_session() as db:
        row = await db.get(AssistantConversation, conversation_id)
        if not row or row.owner_scope != owner_scope:
            raise AssistantError("CONVERSATION_NOT_FOUND", "会话不存在或不可访问")
        if row.status == "active":
            return _conversation_payload(row)
        row.status = "active"
        row.updated_at = utcnow()
        await db.commit()
        return _conversation_payload(row)


async def delete_conversation(
    conversation_id: str,
    owner_scope: str = KNOWLEDGE_OWNER_SCOPE,
) -> dict:
    async with async_session() as db:
        row = await db.get(AssistantConversation, conversation_id)
        if not row or row.owner_scope != owner_scope:
            raise AssistantError("CONVERSATION_NOT_FOUND", "会话不存在或不可访问")
        if row.status != "archived":
            raise AssistantError("CONVERSATION_NOT_ARCHIVED", "请先归档会话，再永久删除")
        if await _has_running_message(db, conversation_id):
            raise AssistantError("CONVERSATION_BUSY", "回答生成期间不能删除会话")
        message_ids = select(AssistantMessage.id).where(
            AssistantMessage.conversation_id == conversation_id
        )
        await db.execute(delete(AssistantCitation).where(
            AssistantCitation.message_id.in_(message_ids)
        ))
        await db.execute(delete(AssistantMessage).where(
            AssistantMessage.conversation_id == conversation_id
        ))
        await db.delete(row)
        await db.commit()
        return {"ok": True, "conversation_id": conversation_id}


async def update_scope(conversation_id: str, scope: dict, owner_scope: str = KNOWLEDGE_OWNER_SCOPE) -> dict:
    normalized = _clean_scope(scope)
    async with async_session() as db:
        row = await db.get(AssistantConversation, conversation_id)
        if not row or row.owner_scope != owner_scope or row.status != "active":
            raise AssistantError("CONVERSATION_NOT_FOUND", "会话不存在或不可访问")
        row.default_scope_json = json.dumps(normalized, ensure_ascii=False)
        row.updated_at = utcnow()
        await db.commit()
        return _conversation_payload(row, normalized)


async def scope_assets(scope: dict, owner_scope: str = KNOWLEDGE_OWNER_SCOPE) -> dict:
    normalized = _clean_scope(scope)
    assets = await _accessible_assets(normalized, owner_scope)
    return {
        "scope": normalized, "scope_label": _scope_label(normalized, len(assets)),
        "total": len(assets), "items": [{
            "asset_id": item.id, "task_id": item.task_id, "title": item.title,
            "status": item.status, "updated_at": item.updated_at,
        } for item in assets]
    }


async def _asset_query(content: str, scope: dict, owner_scope: str) -> dict:
    assets = await _accessible_assets(scope, owner_scope)
    generic = {"列出", "最近", "处理", "的视频", "视频", "资产", "知识", "课程", "会议", "哪些", "找出", "查找", "搜索", "相关", "内容"}
    terms = [term for term in re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{2,}", content.lower()) if term not in generic]
    filtered = [asset for asset in assets if not terms or any(term in asset.title.lower() for term in terms)]
    if RECENT_RE.search(content):
        filtered.sort(key=lambda asset: asset.updated_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return {
        "items": [{
            "asset_id": asset.id, "task_id": asset.task_id, "title": asset.title,
            "status": asset.status, "updated_at": asset.updated_at,
        } for asset in filtered[:ASSISTANT_MAX_ASSET_RESULTS]],
        "total": len(filtered), "scope": scope,
    }


async def _history(conversation_id: str) -> list[dict]:
    async with async_session() as db:
        rows = (await db.execute(select(AssistantMessage).where(
            AssistantMessage.conversation_id == conversation_id,
            AssistantMessage.status == "completed",
        ).order_by(AssistantMessage.created_at.desc()).limit(ASSISTANT_MAX_HISTORY_MESSAGES))).scalars().all()
        return [{"role": row.role, "content": row.content} for row in reversed(rows)]


def _citation_payload(hit: dict, ordinal: int) -> dict:
    return {
        "ordinal": ordinal, "asset_id": hit["asset_id"], "task_id": hit["task_id"],
        "asset_version_id": hit["asset_version_id"], "chunk_id": hit["chunk_id"],
        "chapter_id": hit.get("chapter_id"), "asset_title": hit["asset_title"],
        "content_type": hit["content_type"], "chapter_title": hit.get("chapter_title"),
        "start_time": hit.get("start_time"), "end_time": hit.get("end_time"),
        "text": hit["text"][:600], "keyframe": hit.get("keyframe"),
        "source_status": hit.get("source_status", "ready"), "availability": "available",
    }


def _normalized_citation_text(value: str) -> str:
    """Normalize formatting-only differences without widening citation ranges."""
    return " ".join(unicodedata.normalize("NFKC", value or "").split()).casefold()


def _deduplicate_evidence(hits: list[dict]) -> list[dict]:
    """Drop exact duplicate evidence while preserving ranking and time precision."""
    result = []
    chunk_ids: set[str] = set()
    snapshots: set[tuple] = set()
    for hit in hits:
        chunk_id = str(hit.get("chunk_id") or "")
        snapshot = (
            hit.get("asset_version_id"),
            hit.get("start_time"),
            hit.get("end_time"),
            _normalized_citation_text(str(hit.get("text") or "")),
        )
        if (chunk_id and chunk_id in chunk_ids) or snapshot in snapshots:
            continue
        if chunk_id:
            chunk_ids.add(chunk_id)
        snapshots.add(snapshot)
        result.append(hit)
    return result


def _retain_cited_evidence(answer: str, evidence: list[dict]) -> tuple[str, list[dict]]:
    """Keep only evidence cited by the answer and rewrite markers contiguously."""
    cited_ordinals = sorted({int(value) for value in re.findall(r"\[(\d+)\]", answer)})
    ordinal_map = {old: new for new, old in enumerate(cited_ordinals, 1)}
    rewritten = re.sub(
        r"\[(\d+)\]",
        lambda match: f"[{ordinal_map[int(match.group(1))]}]",
        answer,
    )
    return rewritten, [evidence[ordinal - 1] for ordinal in cited_ordinals]


async def _persist_citations(db, message_id: str, citations: list[dict]):
    for citation in citations:
        db.add(AssistantCitation(
            id=str(uuid.uuid4()), message_id=message_id, ordinal=citation["ordinal"],
            asset_id=citation["asset_id"], task_id=citation["task_id"],
            asset_version_id=citation["asset_version_id"], chunk_id=citation["chunk_id"],
            chapter_id=citation.get("chapter_id"), start_time=citation.get("start_time"),
            end_time=citation.get("end_time"), text_snapshot=citation["text"],
            media_id=(citation.get("keyframe") or {}).get("id"), availability="available",
        ))


async def _load_citations(db, message_id: str) -> list[dict]:
    rows = (await db.execute(select(AssistantCitation).where(
        AssistantCitation.message_id == message_id
    ).order_by(AssistantCitation.ordinal))).scalars().all()
    result = []
    for row in rows:
        asset = await db.get(KnowledgeAsset, row.asset_id)
        available = bool(asset and asset.status in SEARCHABLE_STATUSES and asset.current_version_id == row.asset_version_id)
        chunk = await db.get(KnowledgeChunk, row.chunk_id) if available else None
        chapter = await db.get(KnowledgeChapter, row.chapter_id) if available and row.chapter_id else None
        media = await db.get(KnowledgeMedia, row.media_id) if available and row.media_id else None
        result.append({
            "ordinal": row.ordinal, "asset_id": row.asset_id, "task_id": row.task_id,
            "asset_version_id": row.asset_version_id, "chunk_id": row.chunk_id,
            "chapter_id": row.chapter_id, "asset_title": asset.title if asset else "来源已不可用",
            "content_type": chunk.content_type if chunk else None,
            "chapter_title": chapter.title if chapter else None,
            "start_time": row.start_time, "end_time": row.end_time,
            "text": row.text_snapshot if available else "该来源已不可用，无法展示原文。",
            "keyframe": None if not media or media.availability != "available" else {
                "id": media.id, "relative_uri": media.relative_uri, "timestamp": media.timestamp,
                "availability": media.availability,
            },
            "availability": "available" if available else "unavailable",
        })
    return result


async def get_messages(conversation_id: str, owner_scope: str = KNOWLEDGE_OWNER_SCOPE) -> dict:
    async with async_session() as db:
        conversation = await db.get(AssistantConversation, conversation_id)
        if not conversation or conversation.owner_scope != owner_scope:
            raise AssistantError("CONVERSATION_NOT_FOUND", "会话不存在或不可访问")
        rows = (await db.execute(select(AssistantMessage).where(
            AssistantMessage.conversation_id == conversation_id
        ).order_by(AssistantMessage.created_at).limit(100))).scalars().all()
        messages = []
        for row in rows:
            messages.append({
                "id": row.id, "role": row.role, "intent": row.intent, "status": row.status,
                "content": row.content, "scope_snapshot": _json(row.scope_snapshot_json, DEFAULT_SCOPE),
                "retrieval": _json(row.retrieval_snapshot_json, {}), "error_code": row.error_code,
                "created_at": row.created_at,
                "citations": await _load_citations(db, row.id) if row.role == "assistant" else [],
            })
        return {"conversation": _conversation_payload(conversation), "messages": messages}


async def cancel_message(message_id: str, owner_scope: str = KNOWLEDGE_OWNER_SCOPE) -> bool:
    async with async_session() as db:
        row = await db.get(AssistantMessage, message_id)
        if not row:
            return False
        conversation = await db.get(AssistantConversation, row.conversation_id)
        if not conversation or conversation.owner_scope != owner_scope:
            return False
        if row.status in {"pending", "retrieving", "streaming"}:
            row.status, row.error_code = "cancelled", "CANCELLED"
            await db.commit()
        return True


async def message_stream(conversation_id: str, request: dict, owner_scope: str = KNOWLEDGE_OWNER_SCOPE) -> AsyncIterator[dict]:
    content = str(request.get("content", "")).strip()
    client_id = str(request.get("client_request_id", "")).strip()
    if not content or len(content) > 2000:
        raise AssistantError("INVALID_MESSAGE", "问题不能为空且不能超过 2000 个字符")
    if not client_id or len(client_id) > 100:
        raise AssistantError("INVALID_REQUEST_ID", "缺少客户端请求标识")

    async with async_session() as db:
        conversation = await db.get(AssistantConversation, conversation_id)
        if not conversation or conversation.owner_scope != owner_scope or conversation.status != "active":
            raise AssistantError("CONVERSATION_NOT_FOUND", "会话不存在或不可访问")
        existing = (await db.execute(select(AssistantMessage).where(
            AssistantMessage.conversation_id == conversation_id,
            AssistantMessage.role == "user", AssistantMessage.client_request_id == client_id,
        ))).scalar_one_or_none()
        if existing:
            assistant = (await db.execute(select(AssistantMessage).where(
                AssistantMessage.conversation_id == conversation_id,
                AssistantMessage.role == "assistant", AssistantMessage.client_request_id == f"{client_id}:assistant",
            ))).scalar_one_or_none()
            citations = await _load_citations(db, assistant.id) if assistant else []
            yield {"event": "message_completed", "data": {"message_id": assistant.id if assistant else existing.id, "status": assistant.status if assistant else "streaming", "content": assistant.content if assistant else "", "citations": citations}}
            return
        scope = _clean_scope(request.get("scope_override") or _json(conversation.default_scope_json, DEFAULT_SCOPE))
        intent = detect_intent(content)
        user_message = AssistantMessage(
            id=str(uuid.uuid4()), conversation_id=conversation_id, role="user", intent=intent,
            status="completed", content=content, client_request_id=client_id,
            scope_snapshot_json=json.dumps(scope, ensure_ascii=False),
        )
        assistant_message = AssistantMessage(
            id=str(uuid.uuid4()), conversation_id=conversation_id, role="assistant", intent=intent,
            status="pending", client_request_id=f"{client_id}:assistant",
            scope_snapshot_json=json.dumps(scope, ensure_ascii=False),
        )
        db.add_all([user_message, assistant_message])
        if conversation.title == "新会话":
            conversation.title = content[:24]
        conversation.updated_at = utcnow()
        await db.commit()
    yield {"event": "message_started", "data": {"message_id": assistant_message.id, "user_message_id": user_message.id, "intent": intent}}

    if intent == "unsupported_action":
        answer = "知识助手目前只支持查询、筛选、浏览和定位视频知识资产，不能删除或修改内容。请前往笔记管理页完成这类操作。"
        async with async_session() as db:
            row = await db.get(AssistantMessage, assistant_message.id)
            row.status, row.content = "completed", answer
            row.retrieval_snapshot_json = json.dumps({"mode": "unsupported_action", "result_count": 0}, ensure_ascii=False)
            await db.commit()
        yield {"event": "token", "data": {"message_id": assistant_message.id, "content": answer}}
        yield {"event": "message_completed", "data": {"message_id": assistant_message.id, "status": "completed", "content": answer, "citations": [], "scope": scope}}
        return

    yield {"event": "retrieval_started", "data": {"message_id": assistant_message.id, "scope": scope}}
    try:
        accessible = await _accessible_assets(scope, owner_scope)
        effective_ids = [asset.id for asset in accessible]
        results = {"results": [], "total": 0, "available_assets": len(accessible), "retrieval_mode": "keyword", "degraded_search": False, "elapsed_ms": 0}
        asset_results = {"items": [], "total": 0, "scope": scope}
        if intent == "asset_query":
            asset_results = await _asset_query(content, scope, owner_scope)
            results["total"] = asset_results["total"]
        elif effective_ids:
            results = await search_knowledge(
                content, asset_ids=effective_ids,
                content_types=scope.get("content_types"), top_k=max(1, min(ASSISTANT_TOP_K, 20)), owner_scope=owner_scope,
            )
        hits = _deduplicate_evidence(results.get("results", []))
        citations = []
        yield {"event": "retrieval_completed", "data": {
            "message_id": assistant_message.id, "result_count": len(hits), "asset_count": len(accessible),
            "retrieval": {key: results.get(key) for key in ("retrieval_mode", "degraded_search", "elapsed_ms", "total")},
        }}
        if asset_results["items"]:
            yield {"event": "asset_results", "data": asset_results}

        answer = ""
        retrieval = {"mode": results.get("retrieval_mode", "keyword"), "degraded_search": results.get("degraded_search", False), "result_count": len(hits), "asset_count": len(accessible), "elapsed_ms": results.get("elapsed_ms", 0), "asset_results": asset_results}
        if intent == "asset_query":
            answer = f"找到 {asset_results['total']} 个符合当前范围的知识资产。" if asset_results["total"] else "当前范围内没有找到符合条件的知识资产。"
        elif not hits:
            answer = "当前范围内没有找到足够相关的本地知识证据。可以尝试换个关键词，或扩大右侧的知识范围。"
        elif max(hit["score"] for hit in hits) < ASSISTANT_MIN_EVIDENCE_SCORE:
            answer = "我找到了几条弱相关片段，但现有本地证据不足以确认这个问题。你可以换个更具体的问法。"
        else:
            context = []
            size = 0
            for hit in hits:
                if size + len(hit["text"]) > ASSISTANT_MAX_CONTEXT_CHARS:
                    break
                context.append(hit)
                size += len(hit["text"])
            try:
                llm = await generate_answer(content, context, await _history(conversation_id))
                answer = validate_citations(llm["content"], len(context))
                answer, cited_hits = _retain_cited_evidence(answer, context)
                citations = [
                    _citation_payload(hit, index)
                    for index, hit in enumerate(cited_hits, 1)
                ]
                retrieval["model"] = llm.get("model")
                retrieval["usage"] = llm.get("usage", {})
            except AssistantLLMUnavailable:
                answer = "已找到本地相关片段，但回答生成服务尚未配置。请在 backend/.env 中配置文本模型 Key 后重试。"
                retrieval["generation"] = "unconfigured"
            except Exception:
                raise AssistantError("GENERATION_FAILED", "回答生成失败，但已保留本次检索到的来源。请重试回答。")

        async with async_session() as db:
            row = await db.get(AssistantMessage, assistant_message.id)
            row.status, row.content = "streaming", ""
            row.retrieval_snapshot_json = json.dumps(retrieval, ensure_ascii=False)
            await db.commit()
        # Keep the transport visibly streaming even when a compatible provider
        # returns a non-streaming response.
        for index in range(0, len(answer), 48):
            token = answer[index:index + 48]
            async with async_session() as db:
                row = await db.get(AssistantMessage, assistant_message.id)
                row.content += token
                await db.commit()
            yield {"event": "token", "data": {"message_id": assistant_message.id, "content": token}}
        async with async_session() as db:
            row = await db.get(AssistantMessage, assistant_message.id)
            row.status = "completed"
            await _persist_citations(db, assistant_message.id, citations)
            await db.commit()
        if citations:
            yield {"event": "citations", "data": {"message_id": assistant_message.id, "citations": citations}}
        yield {"event": "message_completed", "data": {"message_id": assistant_message.id, "status": "completed", "content": answer, "citations": citations, "asset_results": asset_results, "retrieval": retrieval, "scope": scope}}
    except AssistantError as error:
        async with async_session() as db:
            row = await db.get(AssistantMessage, assistant_message.id)
            if row:
                row.status, row.error_code, row.content = "failed", error.code, ""
                row.retrieval_snapshot_json = json.dumps({"result_count": 0}, ensure_ascii=False)
                await db.commit()
        yield {"event": "message_failed", "data": {"message_id": assistant_message.id, "error_code": error.code, "message": error.message}}
