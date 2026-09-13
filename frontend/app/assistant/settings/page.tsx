"use client";

import { useEffect, useMemo, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import BrandHeader from "../../components/BrandHeader";
import {
  deleteAssistantConversation,
  fetchAssistantConversations,
  fetchAssistantScopeAssets,
  hasBackend,
  restoreAssistantConversation,
  updateAssistantScope,
  type AssistantAssetResult,
  type AssistantConversation,
  type AssistantMessage,
  type AssistantScope,
  type KnowledgeContentType,
} from "../../lib/api";
import { getLocalTasksSnapshot } from "../../lib/demo";

const DEMO_CONVERSATIONS = "snapnote.assistant.conversations.v1";
const EMPTY_SCOPE: AssistantScope = { asset_ids: null, content_types: null, created_after: null, created_before: null };
const CONTENT_TYPES: Array<{ value: KnowledgeContentType; label: string; detail: string }> = [
  { value: "video_summary", label: "视频摘要", detail: "检索视频整体结论" },
  { value: "chapter_summary", label: "章节概要", detail: "检索分章节知识结构" },
  { value: "transcript", label: "原文片段", detail: "检索带时间坐标的转写" },
  { value: "note", label: "知识笔记", detail: "检索整理后的笔记内容" },
];

type DemoConversation = AssistantConversation & { messages: AssistantMessage[] };

function demoAssets(): AssistantAssetResult[] {
  return getLocalTasksSnapshot().filter((task) => task.status === "completed").map((task) => ({
    asset_id: task.id, task_id: task.id, title: task.title, status: "ready", updated_at: task.createdAt,
  }));
}

function scopeLabel(scope: AssistantScope) {
  const asset = scope.asset_ids !== null ? `${scope.asset_ids.length} 个视频` : "全部资产";
  const content = scope.content_types !== null ? `${scope.content_types.length} 类内容` : "全部内容";
  return `${asset} · ${content}`;
}

export default function AssistantSettingsPage() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [tab, setTab] = useState<"scope" | "archive">(searchParams.get("tab") === "archive" ? "archive" : "scope");
  const [conversations, setConversations] = useState<AssistantConversation[]>([]);
  const [archived, setArchived] = useState<AssistantConversation[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [scope, setScope] = useState<AssistantScope>(EMPTY_SCOPE);
  const [savedScope, setSavedScope] = useState<AssistantScope>(EMPTY_SCOPE);
  const [assets, setAssets] = useState<AssistantAssetResult[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [busyId, setBusyId] = useState("");
  const [pendingDelete, setPendingDelete] = useState<AssistantConversation | null>(null);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");

  const dirty = useMemo(() => JSON.stringify(scope) !== JSON.stringify(savedScope), [scope, savedScope]);
  const selectedConversation = conversations.find((item) => item.id === selectedId);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        let active: AssistantConversation[];
        let archivedItems: AssistantConversation[];
        let assetItems: AssistantAssetResult[];
        if (hasBackend) {
          [active, archivedItems, assetItems] = await Promise.all([
            fetchAssistantConversations("active"),
            fetchAssistantConversations("archived"),
            fetchAssistantScopeAssets(EMPTY_SCOPE).then((payload) => payload.items),
          ]);
        } else {
          const stored = JSON.parse(localStorage.getItem(DEMO_CONVERSATIONS) || "[]") as DemoConversation[];
          active = stored.filter((item) => item.status === "active");
          archivedItems = stored.filter((item) => item.status === "archived");
          assetItems = demoAssets();
        }
        if (cancelled) return;
        setConversations(active);
        setArchived(archivedItems);
        setAssets(assetItems);
        const requested = searchParams.get("conversation");
        const selected = active.find((item) => item.id === requested) || active[0];
        if (selected) {
          setSelectedId(selected.id);
          setScope(selected.default_scope);
          setSavedScope(selected.default_scope);
        }
      } catch (problem) { if (!cancelled) setError(problem instanceof Error ? problem.message : "无法打开知识范围与会话"); }
      finally { if (!cancelled) setLoading(false); }
    })();
    return () => { cancelled = true; };
  }, [searchParams]);

  function selectConversation(id: string) {
    const selected = conversations.find((item) => item.id === id);
    if (!selected) return;
    setSelectedId(id);
    setScope(selected.default_scope);
    setSavedScope(selected.default_scope);
    setNotice(""); setError("");
  }

  async function saveScope() {
    if (!selectedId || saving || !dirty) return;
    setSaving(true); setError(""); setNotice("");
    try {
      let updated: AssistantConversation;
      if (hasBackend) {
        updated = await updateAssistantScope(selectedId, scope);
      } else {
        const current = conversations.find((item) => item.id === selectedId)!;
        updated = { ...current, default_scope: scope, scope_label: scopeLabel(scope), updated_at: new Date().toISOString() };
        const stored = JSON.parse(localStorage.getItem(DEMO_CONVERSATIONS) || "[]") as DemoConversation[];
        localStorage.setItem(DEMO_CONVERSATIONS, JSON.stringify(stored.map((item) => item.id === selectedId ? { ...item, ...updated } : item)));
      }
      setConversations((list) => list.map((item) => item.id === updated.id ? updated : item));
      setSavedScope(scope);
      setNotice("知识范围已保存");
    } catch (problem) { setError(problem instanceof Error ? problem.message : "知识范围保存失败"); }
    finally { setSaving(false); }
  }

  async function restoreConversation(item: AssistantConversation) {
    if (busyId) return;
    setBusyId(item.id); setError(""); setNotice("");
    try {
      let restored: AssistantConversation;
      if (hasBackend) {
        restored = await restoreAssistantConversation(item.id);
      } else {
        restored = { ...item, status: "active", updated_at: new Date().toISOString() };
        const stored = JSON.parse(localStorage.getItem(DEMO_CONVERSATIONS) || "[]") as DemoConversation[];
        localStorage.setItem(DEMO_CONVERSATIONS, JSON.stringify(stored.map((entry) => entry.id === item.id ? { ...entry, ...restored } : entry)));
      }
      setArchived((list) => list.filter((entry) => entry.id !== item.id));
      setConversations((list) => [restored, ...list]);
      if (!selectedId) {
        setSelectedId(restored.id);
        setScope(restored.default_scope);
        setSavedScope(restored.default_scope);
      }
      setNotice("会话已恢复");
    } catch (problem) { setError(problem instanceof Error ? problem.message : "无法恢复会话"); }
    finally { setBusyId(""); }
  }

  async function permanentlyDelete() {
    const item = pendingDelete;
    if (!item || busyId) return;
    setBusyId(item.id); setError(""); setNotice("");
    try {
      if (hasBackend) {
        await deleteAssistantConversation(item.id);
      } else {
        const stored = JSON.parse(localStorage.getItem(DEMO_CONVERSATIONS) || "[]") as DemoConversation[];
        localStorage.setItem(DEMO_CONVERSATIONS, JSON.stringify(stored.filter((entry) => entry.id !== item.id)));
      }
      setArchived((list) => list.filter((entry) => entry.id !== item.id));
      setPendingDelete(null);
      setNotice("会话已永久删除");
    } catch (problem) { setError(problem instanceof Error ? problem.message : "无法永久删除会话"); }
    finally { setBusyId(""); }
  }

  function returnToAssistant() {
    router.push(`/assistant${selectedId ? `?conversation=${selectedId}` : ""}`);
  }

  if (loading) return <div className="loading-screen">正在打开知识范围与会话…</div>;

  return (
    <main className="site-shell assistant-settings-page">
      <BrandHeader variant="library" />
      <section className="assistant-settings-shell">
        <header className="assistant-settings-header">
          <button type="button" className="settings-back" onClick={returnToAssistant}>← 返回助手</button>
          <div><span className="eyebrow">KNOWLEDGE &amp; CONVERSATIONS</span><h1>知识范围与会话</h1><p>管理当前会话的检索边界，以及不常用的归档会话。</p></div>
        </header>
        <div className="assistant-settings-layout">
          <nav className="assistant-settings-nav" aria-label="知识范围与会话分类">
            <button type="button" className={tab === "scope" ? "active" : ""} onClick={() => setTab("scope")}><span>⌁</span><b>知识范围</b><small>检索资产与内容类型</small></button>
            <button type="button" className={tab === "archive" ? "active" : ""} onClick={() => setTab("archive")}><span>▣</span><b>归档会话</b><small>{archived.length} 个归档会话</small></button>
          </nav>
          <section className="assistant-settings-content">
            {tab === "scope" ? <>
              <div className="settings-section-heading"><div><h2>知识范围</h2><p>设置助手回答时允许检索的本地知识边界。</p></div>{selectedConversation && <span>{selectedConversation.scope_label}</span>}</div>
              {conversations.length ? <>
                <label className="settings-field"><span>应用到会话</span><select value={selectedId} onChange={(event) => selectConversation(event.target.value)}>{conversations.map((item) => <option key={item.id} value={item.id}>{item.title}</option>)}</select></label>
                <section className="settings-card"><div className="settings-card-title"><h3>内容类型</h3><p>选择回答可以引用的内容类型，至少保留一项。</p></div><div className="settings-type-grid">{CONTENT_TYPES.map((item) => { const checked = !scope.content_types || scope.content_types.includes(item.value); return <button type="button" className={checked ? "checked" : ""} key={item.value} onClick={() => { const current = scope.content_types || CONTENT_TYPES.map((entry) => entry.value); const next = current.includes(item.value) ? current.filter((value) => value !== item.value) : [...current, item.value]; if (!next.length) return; setScope({ ...scope, content_types: next.length === CONTENT_TYPES.length ? null : next }); }}><i>{checked ? "✓" : ""}</i><span><b>{item.label}</b><small>{item.detail}</small></span></button>; })}</div></section>
                <section className="settings-card"><div className="settings-card-title"><h3>视频资产</h3><p>默认检索全部可用资产，也可以缩小到指定视频。</p></div><div className="settings-asset-toolbar"><span>{scope.asset_ids !== null ? `已选择 ${scope.asset_ids.length} 个` : `全部 ${assets.length} 个资产`}</span><button type="button" onClick={() => setScope({ ...scope, asset_ids: scope.asset_ids !== null ? null : [] })}>{scope.asset_ids !== null ? "选择全部" : "取消全选"}</button></div><div className="settings-asset-list">{assets.length ? assets.map((asset) => { const checked = !scope.asset_ids || scope.asset_ids.includes(asset.asset_id); return <label key={asset.asset_id} className={checked ? "checked" : ""}><input type="checkbox" checked={checked} onChange={() => { const current = scope.asset_ids || assets.map((entry) => entry.asset_id); const next = current.includes(asset.asset_id) ? current.filter((id) => id !== asset.asset_id) : [...current, asset.asset_id]; setScope({ ...scope, asset_ids: next.length === assets.length ? null : next }); }} /><i>✓</i><span><b>{asset.title}</b><small>{asset.status === "degraded" ? "部分可用" : "知识资产可用"}</small></span></label>; }) : <p className="settings-empty">还没有可查询的知识资产。</p>}</div></section>
                <section className="settings-card"><div className="settings-card-title"><h3>更新时间</h3><p>可选，仅检索指定更新时间范围内的资产。</p></div><div className="settings-date-row"><label><span>开始日期</span><input type="date" value={scope.created_after?.slice(0, 10) || ""} onChange={(event) => setScope({ ...scope, created_after: event.target.value ? `${event.target.value}T00:00:00Z` : null })} /></label><span>—</span><label><span>结束日期</span><input type="date" value={scope.created_before?.slice(0, 10) || ""} onChange={(event) => setScope({ ...scope, created_before: event.target.value ? `${event.target.value}T23:59:59Z` : null })} /></label></div></section>
                <div className="settings-savebar"><button type="button" className="settings-reset" disabled={!dirty || saving} onClick={() => setScope(savedScope)}>撤销修改</button><button type="button" className="settings-save" disabled={!dirty || saving} onClick={saveScope}>{saving ? "正在保存…" : "保存知识范围"}</button></div>
              </> : <div className="settings-empty-state"><span>⌁</span><h3>暂无活跃会话</h3><p>返回助手新建会话后，即可配置知识范围。</p><button type="button" onClick={returnToAssistant}>返回助手</button></div>}
            </> : <>
              <div className="settings-section-heading"><div><h2>归档会话</h2><p>恢复仍需使用的会话，或永久清理不再需要的记录。</p></div><span>{archived.length} 个会话</span></div>
              <div className="settings-archive-list">{archived.length ? archived.map((item) => <article key={item.id}><div className="archive-icon">▣</div><div><h3>{item.title}</h3><p>{item.scope_label} · 更新于 {new Date(item.updated_at).toLocaleDateString("zh-CN")}</p></div><div className="archive-actions"><button type="button" disabled={busyId === item.id} onClick={() => restoreConversation(item)}>恢复</button><button type="button" className="danger" disabled={busyId === item.id} onClick={() => setPendingDelete(item)}>永久删除</button></div></article>) : <div className="settings-empty-state"><span>▣</span><h3>暂无归档会话</h3><p>主界面归档的会话会集中显示在这里。</p></div>}</div>
            </>}
            {(notice || error) && <div className={`settings-notice ${error ? "error" : ""}`} role="status">{error || notice}</div>}
          </section>
        </div>
      </section>
      {pendingDelete && <div className="confirm-overlay" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget && !busyId) setPendingDelete(null); }}><section className="confirm-dialog" role="alertdialog" aria-modal="true" aria-labelledby="settings-delete-title"><div className="confirm-dialog-icon">!</div><h2 id="settings-delete-title">永久删除会话？</h2><p>全部消息和来源引用快照都会被删除，但不会影响视频、笔记与知识资产。此操作无法撤销。</p><div className="confirm-video-name">{pendingDelete.title}</div><div className="confirm-dialog-actions"><button className="confirm-cancel" type="button" disabled={Boolean(busyId)} onClick={() => setPendingDelete(null)}>取消</button><button className="confirm-delete" type="button" disabled={Boolean(busyId)} onClick={permanentlyDelete}>{busyId ? "正在删除…" : "永久删除"}</button></div></section></div>}
    </main>
  );
}
