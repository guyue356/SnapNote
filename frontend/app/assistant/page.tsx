"use client";
/* eslint-disable @next/next/no-img-element */

import { useEffect, useRef, useState, type ReactNode } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import BrandHeader from "../components/BrandHeader";
import {
  archiveAssistantConversation,
  backendAsset,
  cancelAssistantMessage,
  createAssistantConversation,
  fetchAssistantConversations,
  fetchAssistantMessages,
  fetchAssistantScopeAssets,
  hasBackend,
  restoreAssistantConversation,
  streamAssistantMessage,
  type AssistantAssetResult,
  type AssistantCitation,
  type AssistantConversation,
  type AssistantMessage,
  type AssistantScope,
  type AssistantSseEvent,
} from "../lib/api";
import { formatDuration, getLocalTasksSnapshot, SAMPLE_NOTES, type SnapTask } from "../lib/demo";

const DEMO_CONVERSATIONS = "snapnote.assistant.conversations.v1";
const SUGGESTIONS = ["总结最近的知识资产", "列出和注意力机制相关的视频", "这些视频有哪些共同观点？", "查找 Query、Key、Value 的原话"];
const EMPTY_SCOPE: AssistantScope = { asset_ids: null, content_types: null, created_after: null, created_before: null };

type ViewMessage = AssistantMessage & { localId?: string; asset_results?: { items: AssistantAssetResult[]; total: number } };
type DemoConversation = AssistantConversation & { messages: ViewMessage[] };

function emptyMessage(content = ""): ViewMessage {
  return { id: `local-${Date.now()}`, role: "assistant", intent: "knowledge_qa", status: "completed", content, scope_snapshot: EMPTY_SCOPE, retrieval: {}, created_at: new Date().toISOString(), citations: [] };
}

function demoAssets(): AssistantAssetResult[] {
  return getLocalTasksSnapshot().filter((task) => task.status === "completed").map((task) => ({
    asset_id: task.id, task_id: task.id, title: task.title, status: "ready", updated_at: task.createdAt,
  }));
}

function demoCitation(index: number, task: SnapTask, note: typeof SAMPLE_NOTES[number]): AssistantCitation {
  return {
    ordinal: index + 1, asset_id: task.id, task_id: task.id, asset_version_id: `demo-version-${task.id}`, chunk_id: `demo-chunk-${index}`,
    chapter_id: `demo-chapter-${index}`, asset_title: task.title, content_type: "note", chapter_title: note.title,
    start_time: note.time, end_time: note.end, text: note.demoTranscript, keyframe: null, availability: "available",
  };
}

function demoReply(question: string, scope: AssistantScope): { content: string; citations: AssistantCitation[]; assets?: { items: AssistantAssetResult[]; total: number } } {
  const assets = demoAssets().filter((asset) => !scope.asset_ids || scope.asset_ids.includes(asset.asset_id));
  const terms = question.toLocaleLowerCase();
  if (/(删除|修改|编辑).*(视频|资产|笔记)/.test(question)) return { content: "知识助手目前只支持查询、筛选、浏览和定位视频知识资产，不能删除或修改内容。请前往笔记管理页完成这类操作。", citations: [] };
  if (/(列出|哪些视频|找出|搜索|查找).*(视频|资产|课程|会议)/.test(question) && !/(原话|解释|观点)/.test(question)) {
    const matches = assets.filter((asset) => !terms.includes("注意力") || asset.title.includes("Transformer"));
    return { content: matches.length ? `找到 ${matches.length} 个符合当前范围的知识资产。` : "当前范围内没有找到符合条件的知识资产。", citations: [], assets: { items: matches, total: matches.length } };
  }
  const task = getLocalTasksSnapshot().find((item) => assets.some((asset) => asset.task_id === item.id)) || getLocalTasksSnapshot()[0];
  if (!task) return { content: "还没有可查询的知识资产。先上传并处理一个视频，再回来向我提问吧。", citations: [] };
  const wanted = SAMPLE_NOTES.filter((note) => !terms || note.title.toLocaleLowerCase().split(/[：、， ]/).some((word) => terms.includes(word.toLocaleLowerCase())) || terms.includes("注意力") || terms.includes("观点") || terms.includes("总结"));
  const notes = (wanted.length ? wanted : SAMPLE_NOTES).slice(0, 3);
  const citations = notes.map((note, index) => demoCitation(index, task, note));
  return {
    content: `综合当前本地知识片段看，${task.title} 主要介绍了注意力机制如何根据当前任务动态选择重要信息，并通过 Query、Key、Value 完成内容寻址。[1]\n\n其中，Query 表示当前要寻找什么，Key 用于判断候选内容是否相关，Value 携带最终被聚合的信息。[2] 进一步的缩放点积计算通过除以维度平方根保持 Softmax 的数值稳定。[3]`,
    citations,
  };
}

function timeLabel(value: number | null, end?: number | null) {
  if (value === null || value === undefined) return "无时间坐标";
  return `${formatDuration(value)}${end !== null && end !== undefined ? `–${formatDuration(end)}` : ""}`;
}

function renderInlineText(content: string, keyPrefix: string, onCitation: (ordinal: number) => void) {
  return content.split(/(\[\d+\]|\*\*[^*\n]+\*\*)/g).filter(Boolean).map((part, index) => {
    const citation = part.match(/^\[(\d+)\]$/);
    if (citation) return <button className="assistant-inline-citation" type="button" key={`${keyPrefix}-${index}`} onClick={() => onCitation(Number(citation[1]))}>{part}</button>;
    if (part.startsWith("**") && part.endsWith("**")) return <strong key={`${keyPrefix}-${index}`}>{part.slice(2, -2)}</strong>;
    return part;
  });
}

function renderText(content: string, onCitation: (ordinal: number) => void) {
  const lines = content.split("\n");
  const blocks: ReactNode[] = [];

  for (let index = 0; index < lines.length;) {
    const line = lines[index].trim();
    if (!line) { index += 1; continue; }

    const heading = line.match(/^#{1,3}\s+(.+)$/);
    if (heading) {
      blocks.push(<h4 key={`heading-${index}`}>{renderInlineText(heading[1], `heading-${index}`, onCitation)}</h4>);
      index += 1;
      continue;
    }

    if (/^[-*]\s+/.test(line)) {
      const items: ReactNode[] = [];
      while (index < lines.length) {
        const item = lines[index].trim().match(/^[-*]\s+(.+)$/);
        if (!item) break;
        items.push(<li key={`bullet-${index}`}>{renderInlineText(item[1], `bullet-${index}`, onCitation)}</li>);
        index += 1;
      }
      blocks.push(<ul key={`bullets-${index}`}>{items}</ul>);
      continue;
    }

    if (/^\d+[.)]\s+/.test(line)) {
      const items: ReactNode[] = [];
      while (index < lines.length) {
        const item = lines[index].trim().match(/^\d+[.)]\s+(.+)$/);
        if (!item) break;
        items.push(<li key={`number-${index}`}>{renderInlineText(item[1], `number-${index}`, onCitation)}</li>);
        index += 1;
      }
      blocks.push(<ol key={`numbers-${index}`}>{items}</ol>);
      continue;
    }

    blocks.push(<p key={`paragraph-${index}`}>{renderInlineText(line, `paragraph-${index}`, onCitation)}</p>);
    index += 1;
  }

  return blocks;
}

function UserIcon() {
  return <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden="true"><circle cx="12" cy="8" r="3.5" /><path d="M5.5 20c.5-4 2.7-6 6.5-6s6 2 6.5 6" /></svg>;
}

export default function AssistantPage() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [conversations, setConversations] = useState<AssistantConversation[]>([]);
  const [activeId, setActiveId] = useState("");
  const [messages, setMessages] = useState<ViewMessage[]>([]);
  const [scope, setScope] = useState<AssistantScope>(EMPTY_SCOPE);
  const [draft, setDraft] = useState("");
  const [loading, setLoading] = useState(true);
  const [sending, setSending] = useState(false);
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const [showSessions, setShowSessions] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [sessionMenuId, setSessionMenuId] = useState("");
  const [lifecycleBusy, setLifecycleBusy] = useState("");
  const [archiveToast, setArchiveToast] = useState<{ item: AssistantConversation; wasActive: boolean } | null>(null);
  const [highlighted, setHighlighted] = useState(0);
  const abortRef = useRef<AbortController | null>(null);
  const activeAssistantIdRef = useRef<string | null>(null);
  const messageRefs = useRef<Record<number, HTMLElement | null>>({});
  const assetAppliedRef = useRef(false);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const archiveTimerRef = useRef<number | null>(null);

  const hasMessages = messages.length > 0;

  function saveDemo(next: DemoConversation[]) {
    localStorage.setItem(DEMO_CONVERSATIONS, JSON.stringify(next));
  }

  function makeDemoConversation(defaultScope = EMPTY_SCOPE): DemoConversation {
    const id = `demo-conversation-${Date.now()}`;
    return { id, owner_scope: "local", title: "新会话", status: "active", default_scope: defaultScope, scope_label: "全部资产 · 全部内容", created_at: new Date().toISOString(), updated_at: new Date().toISOString(), messages: [] };
  }

  async function loadConversation(id: string) {
    setActiveId(id);
    setError("");
    setDraft(sessionStorage.getItem(`snapnote.assistant.draft.${id}`) || "");
    if (hasBackend) {
      try {
        const payload = await fetchAssistantMessages(id);
        setMessages(payload.messages as ViewMessage[]);
        setScope(payload.conversation.default_scope);
        window.setTimeout(() => {
          if (scrollRef.current) scrollRef.current.scrollTop = Number(sessionStorage.getItem(`snapnote.assistant.scroll.${id}`) || 0);
        }, 0);
      } catch (problem) { setError(problem instanceof Error ? problem.message : "无法读取会话"); }
      return;
    }
    const stored = JSON.parse(localStorage.getItem(DEMO_CONVERSATIONS) || "[]") as DemoConversation[];
    const found = stored.find((item) => item.id === id);
    setMessages(found?.messages || []);
    setScope(found?.default_scope || EMPTY_SCOPE);
    window.setTimeout(() => {
      if (scrollRef.current) scrollRef.current.scrollTop = Number(sessionStorage.getItem(`snapnote.assistant.scroll.${id}`) || 0);
    }, 0);
  }

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        if (hasBackend) {
          const [active, archived] = await Promise.all([
            fetchAssistantConversations("active"), fetchAssistantConversations("archived"),
          ]);
          let list = active;
          if (!list.length && !archived.length) { const created = await createAssistantConversation(); list = [created]; }
          if (cancelled) return;
          setConversations(list);
          const requested = searchParams.get("conversation");
          const requestedActive = requested && list.some((item) => item.id === requested);
          const initialId = requestedActive ? requested! : list[0]?.id;
          if (initialId) await loadConversation(initialId);
        } else {
          const stored = JSON.parse(localStorage.getItem(DEMO_CONVERSATIONS) || "[]") as DemoConversation[];
          const seeded = stored.length ? stored : [makeDemoConversation()];
          if (!stored.length) saveDemo(seeded);
          if (cancelled) return;
          const list = seeded.filter((item) => item.status === "active");
          setConversations(list);
          if (list[0]) await loadConversation(list[0].id);
        }
      } catch (problem) { if (!cancelled) setError(problem instanceof Error ? problem.message : "助手暂时无法打开"); }
      finally { if (!cancelled) setLoading(false); }
    })();
    return () => { cancelled = true; abortRef.current?.abort(); if (archiveTimerRef.current) window.clearTimeout(archiveTimerRef.current); };
    // The initial route query is intentionally read once when opening the page.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (activeId) sessionStorage.setItem(`snapnote.assistant.draft.${activeId}`, draft);
  }, [activeId, draft]);

  useEffect(() => {
    if (!hasBackend) {
      const nextAssets = demoAssets();
      const requestedAsset = searchParams.get("asset");
      if (requestedAsset && !assetAppliedRef.current && nextAssets.some((item) => item.task_id === requestedAsset)) {
        assetAppliedRef.current = true;
        setScope({ ...EMPTY_SCOPE, asset_ids: [requestedAsset] });
      }
      return;
    }
    fetchAssistantScopeAssets(EMPTY_SCOPE).then((payload) => {
      const requestedAsset = searchParams.get("asset");
      const matching = requestedAsset && payload.items.find((item) => item.task_id === requestedAsset);
      if (matching && !assetAppliedRef.current) { assetAppliedRef.current = true; setScope({ ...scope, asset_ids: [matching.asset_id] }); }
    }).catch(() => undefined);
  }, [scope, searchParams]);

  function newConversation() {
    if (sending) return;
    if (hasBackend) {
      createAssistantConversation(scope).then((created) => { setConversations((list) => [created, ...list]); loadConversation(created.id); });
    } else {
      const created = makeDemoConversation();
      const stored = JSON.parse(localStorage.getItem(DEMO_CONVERSATIONS) || "[]") as DemoConversation[];
      saveDemo([created, ...stored]); setConversations((list) => [created, ...list]); loadConversation(created.id);
    }
    setSessionMenuId(""); setShowSessions(false);
  }

  function toggleSidebar() {
    setSidebarCollapsed((collapsed) => {
      if (!collapsed) setSessionMenuId("");
      return !collapsed;
    });
  }

  function clearConversation() {
    setActiveId("");
    setMessages([]);
    setScope(EMPTY_SCOPE);
  }

  async function archiveConversation(item: AssistantConversation) {
    if (sending || lifecycleBusy) return;
    setLifecycleBusy(item.id); setError(""); setSessionMenuId("");
    const previous = conversations;
    const nextActive = previous.filter((entry) => entry.id !== item.id);
    const wasActive = activeId === item.id;
    setConversations(nextActive);
    if (wasActive) {
      if (nextActive[0]) await loadConversation(nextActive[0].id);
      else clearConversation();
    }
    try {
      if (hasBackend) {
        await archiveAssistantConversation(item.id);
      } else {
        const archived = { ...item, status: "archived" as const, updated_at: new Date().toISOString() };
        const stored = JSON.parse(localStorage.getItem(DEMO_CONVERSATIONS) || "[]") as DemoConversation[];
        saveDemo(stored.map((entry) => entry.id === item.id ? { ...entry, ...archived } : entry));
      }
      setArchiveToast({ item: { ...item, status: "archived" }, wasActive });
      if (archiveTimerRef.current) window.clearTimeout(archiveTimerRef.current);
      archiveTimerRef.current = window.setTimeout(() => setArchiveToast(null), 5000);
    } catch (problem) {
      setConversations(previous);
      if (wasActive) await loadConversation(item.id);
      setError(problem instanceof Error ? problem.message : "无法归档会话");
    }
    finally { setLifecycleBusy(""); }
  }

  async function undoArchive() {
    const toast = archiveToast;
    if (!toast || lifecycleBusy) return;
    setLifecycleBusy(toast.item.id); setError("");
    try {
      let restored = { ...toast.item, status: "active" as const, updated_at: new Date().toISOString() };
      if (hasBackend) {
        restored = await restoreAssistantConversation(toast.item.id);
      } else {
        const stored = JSON.parse(localStorage.getItem(DEMO_CONVERSATIONS) || "[]") as DemoConversation[];
        saveDemo(stored.map((entry) => entry.id === toast.item.id ? { ...entry, ...restored } : entry));
      }
      setConversations((list) => [restored, ...list.filter((entry) => entry.id !== restored.id)]);
      if (toast.wasActive) await loadConversation(restored.id);
      if (archiveTimerRef.current) window.clearTimeout(archiveTimerRef.current);
      setArchiveToast(null);
    } catch (problem) { setError(problem instanceof Error ? problem.message : "无法恢复会话"); }
    finally { setLifecycleBusy(""); }
  }

  function openSettings(tab: "scope" | "archive" = "scope") {
    if (activeId) sessionStorage.setItem(`snapnote.assistant.draft.${activeId}`, draft);
    router.push(`/assistant/settings?tab=${tab}${activeId ? `&conversation=${activeId}` : ""}`);
  }

  function patchAssistant(id: string, patch: Partial<ViewMessage>) {
    setMessages((list) => list.map((item) => item.id === id ? { ...item, ...patch } : item));
  }

  async function sendQuestion(question = draft, retryMessage?: ViewMessage) {
    const content = question.trim();
    if (!content || sending || !activeId) return;
    setDraft(""); setError(""); setStatus("正在准备检索…"); setSending(true);
    const clientRequestId = typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : "local-request";
    const user: ViewMessage = { id: `user-${clientRequestId}`, role: "user", intent: "knowledge_qa", status: "completed", content, scope_snapshot: scope, retrieval: {}, created_at: new Date().toISOString(), citations: [] };
    const placeholder = retryMessage || emptyMessage();
    placeholder.status = "retrieving"; placeholder.content = ""; placeholder.scope_snapshot = scope;
    if (!retryMessage) setMessages((list) => [...list, user, placeholder]); else setMessages((list) => list.map((item) => item.id === retryMessage.id ? { ...item, status: "retrieving", content: "", citations: [] } : item));
    if (!hasBackend) {
      await new Promise((resolve) => window.setTimeout(resolve, 480));
      const reply = demoReply(content, scope);
      patchAssistant(placeholder.id, { status: "completed", content: reply.content, citations: reply.citations, asset_results: reply.assets, retrieval: { result_count: reply.citations.length, mode: "demo" } });
      const stored = JSON.parse(localStorage.getItem(DEMO_CONVERSATIONS) || "[]") as DemoConversation[];
      saveDemo(stored.map((item) => item.id === activeId ? { ...item, title: item.title === "新会话" ? content.slice(0, 24) : item.title, messages: [...item.messages, ...(retryMessage ? [] : [user]), { ...placeholder, ...reply, status: "completed" }], updated_at: new Date().toISOString() } : item));
      setConversations((list) => list.map((item) => item.id === activeId ? { ...item, title: item.title === "新会话" ? content.slice(0, 24) : item.title, updated_at: new Date().toISOString() } : item));
      setSending(false); setStatus(""); return;
    }
    const controller = new AbortController(); abortRef.current = controller;
    let assistantId = placeholder.id;
    let terminalEventReceived = false;
    try {
      await streamAssistantMessage(activeId, { content, client_request_id: clientRequestId, scope_override: scope }, (event: AssistantSseEvent) => {
        const data = event.data;
        if (event.event === "message_started") { assistantId = String(data.message_id); activeAssistantIdRef.current = assistantId; patchAssistant(placeholder.id, { id: assistantId }); }
        if (event.event === "retrieval_started") setStatus("正在查找本地知识…");
        if (event.event === "retrieval_completed") { setStatus(`已找到 ${Number(data.result_count || 0)} 条相关内容`); patchAssistant(assistantId, { status: "streaming", retrieval: (data.retrieval || {}) as Record<string, unknown> }); }
        if (event.event === "asset_results") patchAssistant(assistantId, { asset_results: data as ViewMessage["asset_results"] });
        if (event.event === "token") { setStatus("正在整理回答…"); setMessages((list) => list.map((item) => item.id === assistantId ? { ...item, status: "streaming", content: item.content + String(data.content || "") } : item)); }
        if (event.event === "citations") patchAssistant(assistantId, { citations: (data.citations || []) as AssistantCitation[] });
        if (event.event === "message_completed") { terminalEventReceived = true; patchAssistant(assistantId, { status: "completed", content: String(data.content || ""), citations: (data.citations || []) as AssistantCitation[], asset_results: data.asset_results as ViewMessage["asset_results"], retrieval: (data.retrieval || {}) as Record<string, unknown> }); setStatus(""); }
        if (event.event === "message_failed") { terminalEventReceived = true; patchAssistant(assistantId, { status: "failed", error_code: String(data.error_code || "GENERATION_FAILED") }); setError(String(data.message || "回答生成失败")); setStatus(""); }
      }, controller.signal);
      if (!terminalEventReceived) throw new Error("助手连接意外中断，请重试回答");
    } catch (problem) {
      const aborted = (problem as Error).name === "AbortError";
      patchAssistant(assistantId, { status: aborted ? "cancelled" : "failed", error_code: aborted ? "CANCELLED" : "CONNECTION_INTERRUPTED" });
      setStatus("");
      if (!aborted) setError(problem instanceof Error ? problem.message : "助手连接中断");
    }
    finally { setSending(false); abortRef.current = null; activeAssistantIdRef.current = null; }
  }

  function focusCitation(ordinal: number) {
    setHighlighted(ordinal);
    messageRefs.current[ordinal]?.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  function openAsset(taskId: string, startTime?: number | null) {
    router.push(`/tasks/${taskId}?from=assistant${startTime !== null && startTime !== undefined ? `&t=${Math.round(startTime)}` : ""}`);
  }

  if (loading) return <div className="loading-screen">正在打开知识助手…</div>;

  return (
    <main className="site-shell assistant-page">
      <BrandHeader variant="library" />
      <section className={`assistant-shell ${sidebarCollapsed ? "sidebar-collapsed" : ""}`}>
        <aside className={`assistant-sidebar ${showSessions ? "drawer-open" : ""}`} aria-label="助手会话">
          <button className="assistant-sidebar-toggle" type="button" onClick={toggleSidebar} aria-label={sidebarCollapsed ? "展开会话导航" : "收起会话导航"} aria-expanded={!sidebarCollapsed} title={sidebarCollapsed ? "展开会话导航" : "收起会话导航"}>
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="m14 7-5 5 5 5" /></svg>
          </button>
          <div className="assistant-side-title"><button className="assistant-close-drawer" type="button" onClick={() => setShowSessions(false)} aria-label="关闭会话列表">×</button></div>
          <button className="assistant-new-chat" type="button" onClick={newConversation} aria-label="新建会话"><span className="assistant-action-icon">＋</span><span className="assistant-action-label">新建会话</span></button>
          <div className="assistant-session-label">最近会话</div>
          <div className="assistant-session-list">
            {conversations.length ? conversations.map((item) => <div className="assistant-session-row" key={item.id}><button type="button" tabIndex={sidebarCollapsed ? -1 : 0} className={`assistant-session ${item.id === activeId ? "active" : ""}`} onClick={() => { loadConversation(item.id); setShowSessions(false); }}><span className="session-dot" /><span><strong>{item.title}</strong><small>{item.scope_label} · {new Date(item.updated_at).toLocaleDateString("zh-CN", { month: "numeric", day: "numeric" })}</small></span></button><button className="assistant-session-more" type="button" tabIndex={sidebarCollapsed ? -1 : 0} aria-label={`管理会话：${item.title}`} aria-expanded={sessionMenuId === item.id} disabled={lifecycleBusy === item.id} onClick={() => setSessionMenuId((value) => value === item.id ? "" : item.id)}>•••</button>{sessionMenuId === item.id && <div className="assistant-session-menu"><button type="button" onClick={() => archiveConversation(item)}>归档会话</button></div>}</div>) : <p className="assistant-session-empty">还没有活跃会话</p>}
          </div>
          <button className="assistant-settings-link" type="button" onClick={() => openSettings("scope")} aria-label="知识范围与会话"><span className="assistant-settings-icon">⚙</span><span className="assistant-action-label">知识范围与会话</span></button>
          <div className="assistant-side-footer"><span className="assistant-local-dot" /> 仅查询本地知识资产</div>
        </aside>

        <section className="assistant-main">
          <button className="assistant-mobile-menu" type="button" onClick={() => { setSidebarCollapsed(false); setShowSessions(true); }} aria-label="打开会话列表">☰</button>
          <div className="assistant-scroll" aria-live="polite" ref={scrollRef} onScroll={(event) => { if (activeId) sessionStorage.setItem(`snapnote.assistant.scroll.${activeId}`, String(event.currentTarget.scrollTop)); }}>
            {!hasMessages ? <div className="assistant-empty"><div className="assistant-orbit"><span>✦</span></div><h3>{!activeId ? "选择或新建一个会话" : "从你的本地知识开始"}</h3><p>{!activeId ? "你可以从左侧选择会话，或新建会话开始提问。" : "我会先检索 SnapNote 已整理的视频资产，再用可追溯的片段回答。"}</p>{activeId && <div className="suggestion-grid">{SUGGESTIONS.map((suggestion) => <button type="button" key={suggestion} onClick={() => sendQuestion(suggestion)}><span>{suggestion.includes("列出") ? "⌕" : suggestion.includes("原话") ? "❞" : "✧"}</span>{suggestion}<b>↗</b></button>)}</div>}{!activeId && <button className="assistant-empty-action" type="button" onClick={newConversation}>＋ 新建会话</button>}</div> : <div className="assistant-thread">{messages.map((message) => message.role === "user" ? <div className="chat-row user-row" key={message.id}><div className="user-bubble">{message.content}</div><div className="user-avatar"><UserIcon /></div></div> : <article className={`chat-row assistant-row status-${message.status}`} key={message.id}><div className="assistant-avatar">S</div><div className="assistant-message"><div className="message-label">SNAPNOTE 助手 {message.status === "retrieving" && <span className="typing-label">正在检索</span>}</div>{message.status === "failed" ? <div className="failed-message"><strong>回答生成失败</strong><p>已保留问题和检索状态，你可以重试这条回答。</p><button type="button" onClick={() => sendQuestion(messages.find((item) => item.role === "user" && new Date(item.created_at).getTime() < new Date(message.created_at).getTime())?.content || "", message)}>↻ 重试回答</button></div> : <>{message.content ? <div className="assistant-copy">{renderText(message.content, focusCitation)}</div> : message.status !== "completed" && <div className="assistant-thinking"><i /><i /><i /> {status || "正在查找本地知识…"}</div>}{message.asset_results?.items?.length ? <div className="asset-result-group"><div className="source-heading"><span>资产结果</span><small>{message.asset_results.total} 个相关视频</small></div><div className="asset-result-list">{message.asset_results.items.map((asset) => <button type="button" className="asset-result-card" key={asset.asset_id} onClick={() => openAsset(asset.task_id)}><span className="asset-result-thumb"><span>▶</span></span><span><strong>{asset.title}</strong><small>{asset.status === "degraded" ? "部分内容可用" : "知识资产可用"} · 更新时间 {new Date(asset.updated_at).toLocaleDateString("zh-CN")}</small></span><b>↗</b></button>)}</div></div> : null}{message.citations.length > 0 && <div className="citation-group"><div className="source-heading"><span>来源索引</span><small>{message.citations.length} 条可核验片段</small></div><div className="citation-list">{message.citations.map((citation) => <article ref={(node) => { messageRefs.current[citation.ordinal] = node; }} className={`citation-card ${highlighted === citation.ordinal ? "highlighted" : ""} ${citation.availability === "unavailable" ? "unavailable" : ""}`} key={`${message.id}-${citation.ordinal}`}><div className="citation-media">{citation.keyframe?.availability === "available" && citation.keyframe.relative_uri ? <img src={backendAsset(citation.keyframe.relative_uri)} alt="" /> : <span>{citation.availability === "unavailable" ? "⊘" : "⌁"}</span>}</div><div className="citation-copy"><div className="citation-topline"><b>[{citation.ordinal}]</b><strong>{citation.asset_title}</strong><em>{citation.content_type === "transcript" ? "原文" : citation.content_type === "note" ? "笔记" : "章节"}</em></div><small>{citation.chapter_title || "知识片段"} · {timeLabel(citation.start_time, citation.end_time)}</small><p>{citation.text}</p><div className="citation-actions"><button type="button" onClick={() => openAsset(citation.task_id)}>{citation.availability === "unavailable" ? "查看资产" : "打开视频"} ↗</button>{citation.start_time !== null && citation.availability === "available" && <button type="button" onClick={() => openAsset(citation.task_id, citation.start_time)}>▶ 跳转观看</button>}</div></div></article>)}</div></div>}</>}</div></article>)}</div>}
          </div>
          <div className="assistant-composer-wrap">{activeId ? <div className="assistant-composer"><textarea value={draft} onChange={(event) => setDraft(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); sendQuestion(); } }} placeholder="问问本地知识资产…" rows={1} disabled={sending} aria-label="输入问题" /><div className="composer-bottom"><span>Enter 发送 · Shift + Enter 换行</span>{sending ? <button className="stop-button" type="button" onClick={() => { abortRef.current?.abort(); if (activeAssistantIdRef.current) cancelAssistantMessage(activeAssistantIdRef.current); setSending(false); }}>停止</button> : <button className="send-button" type="button" onClick={() => sendQuestion()} disabled={!draft.trim()}>↑</button>}</div></div> : <div className="assistant-readonly"><span>当前没有活跃会话。</span><button type="button" onClick={newConversation}>新建会话</button></div>}{error && <p className="assistant-error" role="alert">{error}</p>}</div>
        </section>

      </section>
      {archiveToast && <div className="assistant-toast" role="status"><span>会话已归档</span><button type="button" onClick={undoArchive} disabled={Boolean(lifecycleBusy)}>撤销</button></div>}
    </main>
  );
}
