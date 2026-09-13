"use client";
/* eslint-disable @next/next/no-img-element */

import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { useRouter } from "next/navigation";
import BrandHeader from "../components/BrandHeader";
import SlideVisual from "../components/SlideVisual";
import {
  backendAsset,
  deleteBackendTask,
  fetchBackendTasks,
  hasBackend,
  searchKnowledge,
  toLocalTask,
  type KnowledgeContentType,
  type KnowledgeSearchResponse,
} from "../lib/api";
import {
  deleteLocalTask,
  formatDuration,
  getLocalTasksServerSnapshot,
  getLocalTasksSnapshot,
  getVideoUrl,
  subscribeLocalTasks,
  type SnapTask,
} from "../lib/demo";

type SearchFilter = "all" | "chapter" | "note" | "transcript";

const SEARCH_FILTERS: Array<{ value: SearchFilter; label: string; types?: KnowledgeContentType[] }> = [
  { value: "all", label: "全部" },
  { value: "chapter", label: "章节", types: ["chapter_summary"] },
  { value: "note", label: "笔记", types: ["note"] },
  { value: "transcript", label: "原文", types: ["transcript"] },
];

const CONTENT_LABELS: Record<KnowledgeContentType, string> = {
  video_summary: "视频摘要",
  chapter_summary: "章节",
  transcript: "原文",
  note: "笔记",
};

function normalizeSearch(value: string) {
  return value.trim().replace(/\s+/g, " ");
}

function excerpt(text: string, query: string, length = 220) {
  const clean = text.trim();
  if (clean.length <= length) return clean;
  const matchAt = clean.toLocaleLowerCase().indexOf(query.toLocaleLowerCase());
  const start = Math.max(0, (matchAt < 0 ? 0 : matchAt) - 60);
  const clipped = clean.slice(start, start + length);
  return `${start > 0 ? "…" : ""}${clipped}${start + length < clean.length ? "…" : ""}`;
}

function HighlightedText({ text, query }: { text: string; query: string }) {
  const needle = query.toLocaleLowerCase();
  if (!needle) return text;
  const lowered = text.toLocaleLowerCase();
  const parts = [];
  let cursor = 0;
  let matchAt = lowered.indexOf(needle);
  while (matchAt >= 0) {
    if (matchAt > cursor) parts.push(text.slice(cursor, matchAt));
    parts.push(<mark className="search-highlight" key={`${matchAt}-${parts.length}`}>{text.slice(matchAt, matchAt + query.length)}</mark>);
    cursor = matchAt + query.length;
    matchAt = lowered.indexOf(needle, cursor);
  }
  if (cursor < text.length) parts.push(text.slice(cursor));
  return parts.length ? parts : text;
}

function searchTime(seconds: number | null) {
  return seconds === null ? "视频摘要" : formatDuration(Math.max(0, seconds));
}

function searchHitTitle(hit: KnowledgeSearchResponse["results"][number]) {
  const title = hit.title?.trim() || hit.chapter_title?.trim();
  if (title) return title;
  if (hit.content_type === "transcript") return `原文片段 · ${searchTime(hit.start_time)}`;
  if (hit.content_type === "video_summary") return "视频摘要";
  if (hit.content_type === "note") return "知识笔记";
  return "章节摘要";
}

export default function NotesPage() {
  const router = useRouter();
  const localTasks = useSyncExternalStore(subscribeLocalTasks, getLocalTasksSnapshot, getLocalTasksServerSnapshot);
  const [remoteTasks, setRemoteTasks] = useState<SnapTask[]>([]);
  const [loading, setLoading] = useState(hasBackend);
  const [error, setError] = useState("");
  const [actionError, setActionError] = useState("");
  const [deletingTaskId, setDeletingTaskId] = useState("");
  const [pendingDeleteTask, setPendingDeleteTask] = useState<SnapTask | null>(null);
  const [query, setQuery] = useState("");
  const [searchFilter, setSearchFilter] = useState<SearchFilter>("all");
  const [searchReady, setSearchReady] = useState(false);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState("");
  const [searchResponse, setSearchResponse] = useState<KnowledgeSearchResponse>();
  const [searchRevision, setSearchRevision] = useState(0);
  const semanticPollCountRef = useRef(0);
  const tasks = (hasBackend ? remoteTasks : localTasks)
    .slice()
    .sort((left, right) => new Date(right.createdAt).getTime() - new Date(left.createdAt).getTime());
  const completedCount = tasks.filter((task) => task.status === "completed").length;
  const cleanQuery = normalizeSearch(query);
  const hasQuery = Boolean(cleanQuery);
  const selectedFilter = SEARCH_FILTERS.find((item) => item.value === searchFilter) || SEARCH_FILTERS[0];
  const localSearchResults = hasQuery
    ? tasks.filter((task) => `${task.title} ${task.filename}`.toLocaleLowerCase().includes(cleanQuery.toLocaleLowerCase()))
    : [];

  useEffect(() => {
    const timer = window.setTimeout(() => {
      const urlParams = new URLSearchParams(window.location.search);
      const initialFilter = urlParams.get("type");
      setQuery((urlParams.get("q") || "").slice(0, 500));
      if (SEARCH_FILTERS.some((item) => item.value === initialFilter)) setSearchFilter(initialFilter as SearchFilter);
      setSearchReady(true);
    }, 0);
    return () => window.clearTimeout(timer);
  }, []);

  useEffect(() => {
    if (!pendingDeleteTask) return;
    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape" && !deletingTaskId) setPendingDeleteTask(null);
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [deletingTaskId, pendingDeleteTask]);

  useEffect(() => {
    if (!searchReady) return;
    const timer = window.setTimeout(() => {
      const url = new URL(window.location.href);
      if (cleanQuery) url.searchParams.set("q", cleanQuery); else url.searchParams.delete("q");
      if (cleanQuery && searchFilter !== "all") url.searchParams.set("type", searchFilter); else url.searchParams.delete("type");
      window.history.replaceState(window.history.state, "", `${url.pathname}${url.search}${url.hash}`);
    }, 250);
    return () => window.clearTimeout(timer);
  }, [cleanQuery, searchFilter, searchReady]);

  useEffect(() => {
    if (!searchReady || !hasBackend || !cleanQuery) return;
    const controller = new AbortController();
    let slowTimer: number | undefined;
    const timer = window.setTimeout(() => {
      setSearchError("");
      slowTimer = window.setTimeout(() => setSearching(true), 500);
      searchKnowledge(cleanQuery, selectedFilter.types, controller.signal)
        .then((result) => {
          if (!controller.signal.aborted) setSearchResponse(result);
        })
        .catch((problem: Error) => {
          if (problem.name !== "AbortError" && !controller.signal.aborted) {
            setSearchError(problem.message || "知识检索暂时不可用，请稍后重试");
          }
        })
        .finally(() => {
          if (slowTimer !== undefined) window.clearTimeout(slowTimer);
          if (!controller.signal.aborted) setSearching(false);
        });
    }, 300);
    return () => {
      window.clearTimeout(timer);
      if (slowTimer !== undefined) window.clearTimeout(slowTimer);
      controller.abort();
    };
  }, [cleanQuery, searchFilter, searchReady, searchRevision, selectedFilter.types]);

  useEffect(() => {
    semanticPollCountRef.current = 0;
  }, [cleanQuery, searchFilter]);

  useEffect(() => {
    if (!cleanQuery || searchResponse?.query !== cleanQuery || searchResponse.semantic_status !== "indexing") return;
    if (semanticPollCountRef.current >= 8) return;
    const delay = Math.min(1500 * (2 ** semanticPollCountRef.current), 10_000);
    const timer = window.setTimeout(() => {
      semanticPollCountRef.current += 1;
      setSearchRevision((value) => value + 1);
    }, delay);
    return () => window.clearTimeout(timer);
  }, [cleanQuery, searchResponse]);

  useEffect(() => {
    if (!hasBackend) return;
    let cancelled = false;
    fetchBackendTasks()
      .then((items) => {
        if (!cancelled) setRemoteTasks(items.map(toLocalTask));
      })
      .catch(() => {
        if (!cancelled) setError("笔记列表暂时无法读取，请稍后重试。");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, []);

  function openTask(task: SnapTask) {
    router.push(task.status === "completed" ? `/tasks/${task.id}` : `/tasks/${task.id}/processing`);
  }

  function openSearchHit(taskId: string, startTime: number | null) {
    const queryString = startTime === null ? "from=search" : `t=${Math.floor(startTime)}&from=search`;
    router.push(`/tasks/${taskId}?${queryString}`);
  }

  function clearSearch() {
    setQuery("");
    setSearchFilter("all");
  }

  function requestDeleteTask(task: SnapTask) {
    if (deletingTaskId) return;
    setActionError("");
    setPendingDeleteTask(task);
  }

  async function confirmDeleteTask() {
    const task = pendingDeleteTask;
    if (!task || deletingTaskId) return;
    setDeletingTaskId(task.id);
    setActionError("");
    try {
      if (hasBackend) {
        await deleteBackendTask(task.id);
        setRemoteTasks((items) => items.filter((item) => item.id !== task.id));
      } else {
        deleteLocalTask(task.id);
      }
      setPendingDeleteTask(null);
    } catch (problem) {
      setActionError(problem instanceof Error ? problem.message : "删除清理未完成，请稍后重试。");
    } finally {
      setDeletingTaskId("");
    }
  }

  function taskGrid(items: SnapTask[]) {
    return (
      <div className="notes-grid">
        {items.map((task, index) => {
          const localVideo = !hasBackend ? getVideoUrl(task.id) : "";
          return (
            <article className="note-library-card" key={task.id}>
              <button className="library-card-open" type="button" onClick={() => openTask(task)} aria-label={`查看 ${task.title}`}>
                <div className="library-thumbnail">
                  {task.thumbnailUrl ? (
                    <img src={task.thumbnailUrl} alt="" />
                  ) : localVideo ? (
                    <video src={localVideo} muted preload="metadata" />
                  ) : (
                    <SlideVisual index={index} compact />
                  )}
                  <span className={`status ${task.status}`}>{task.status === "completed" ? "已完成" : task.status === "failed" ? "失败" : "处理中"}</span>
                  <time>{formatDuration(task.duration)}</time>
                </div>
                <div className="library-card-copy"><h2 title={task.title}>{task.title}</h2></div>
              </button>
              <button className="library-delete icon-tooltip" data-tooltip="删除视频" type="button" onClick={() => requestDeleteTask(task)} disabled={Boolean(deletingTaskId)} aria-label={`删除 ${task.title}`}>
                {deletingTaskId === task.id ? (
                  <span className="library-delete-busy" aria-hidden="true" />
                ) : (
                  <svg viewBox="0 0 24 24" aria-hidden="true">
                    <path d="M4 7h16M9 7V4h6v3m3 0-1 13H7L6 7m4 4v5m4-5v5" />
                  </svg>
                )}
              </button>
            </article>
          );
        })}
      </div>
    );
  }

  return (
    <main className="site-shell notes-page">
      <BrandHeader variant="library" />
      <section className="notes-library wrap">
        <div className="library-heading">
          <div>
            <span className="step-kicker">知识资产</span>
            <h1>笔记管理</h1>
            <p>浏览、搜索和管理所有视频知识资产。</p>
            <div className="library-heading-meta"><span>{tasks.length} 个视频</span><span>{completedCount} 个已完成</span></div>
          </div>
          <div className="library-heading-actions">
            <button type="button" className="secondary-compact" onClick={() => router.push("/assistant")}>✦ 打开知识助手</button>
            <button type="button" className="primary-compact" onClick={() => router.push("/#upload")}>＋ 上传视频</button>
          </div>
        </div>

        <section className="library-search" aria-label="搜索视频知识资产">
          <div className="library-search-row">
            <span className="library-search-icon" aria-hidden="true">⌕</span>
            <input
              type="search"
              value={query}
              maxLength={500}
              aria-label="搜索视频标题、章节、笔记或原文"
              placeholder={hasBackend ? "搜索视频标题、章节、笔记或原文…" : "搜索视频标题…"}
              onChange={(event) => setQuery(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Escape") clearSearch();
                if (event.key === "Enter") setSearchRevision((value) => value + 1);
              }}
            />
            {query && <button className="library-search-clear" type="button" onClick={clearSearch}>清除</button>}
          </div>
          <div className="library-search-tools">
            {hasBackend ? (
              <div className="library-search-filters" role="group" aria-label="搜索内容类型">
                {SEARCH_FILTERS.map((item) => (
                  <button type="button" key={item.value} className={searchFilter === item.value ? "active" : ""} aria-pressed={searchFilter === item.value} onClick={() => setSearchFilter(item.value)}>
                    {item.label}
                  </button>
                ))}
              </div>
            ) : <small>演示模式仅检索视频标题</small>}
            {hasQuery && hasBackend && searchResponse && (
              <span className="library-search-count" aria-live="polite">{searchResponse.total} 条结果 · {searchResponse.available_assets} 个可检索视频{searching && <i className="library-search-refresh" aria-label="正在后台更新" />}</span>
            )}
          </div>
        </section>

        {error && <div className="library-message error" role="alert">{error}</div>}
        {actionError && <p className="library-action-error" role="alert">{actionError}</p>}
        {hasQuery && hasBackend ? (
          <section className="search-result-area" aria-label="搜索结果" aria-busy={searching}>
            {searchError && searchResponse && (
              <div className="search-result-inline-error" role="alert">
                <span>结果更新失败，当前仍显示上一次结果。</span>
                <button type="button" onClick={() => setSearchRevision((value) => value + 1)}>重试</button>
              </div>
            )}
            {searchError && !searchResponse ? (
              <div className="search-result-empty search-result-error" role="alert">
                <span>!</span><h2>暂时无法完成检索</h2><p>{searchError}</p>
                <button type="button" onClick={() => setSearchRevision((value) => value + 1)}>重试</button>
              </div>
            ) : searchResponse?.results.length ? (
              <div className="search-result-list">
                {(searchResponse.semantic_status === "partial" || searchResponse.semantic_status === "model_error") && (
                  <div className="search-result-mode" role="status">
                  {searchResponse.semantic_status === "partial" && (
                    <span className="partial">
                      部分笔记未索引 · {searchResponse.semantic_index.indexed_chunks}/{searchResponse.semantic_index.total_chunks}
                    </span>
                  )}
                  {searchResponse.semantic_status === "model_error" && (
                    <span className="model-error" title={searchResponse.semantic_error || undefined}>模型加载失败</span>
                  )}
                  </div>
                )}
                {searchResponse.results.map((hit) => (
                  <button className="search-result-card" type="button" key={hit.chunk_id} onClick={() => openSearchHit(hit.task_id, hit.start_time)}>
                    {hit.keyframe?.availability === "available" && hit.keyframe.relative_uri ? (
                      <img className="search-result-image" src={backendAsset(hit.keyframe.relative_uri)} alt="" />
                    ) : <span className="search-result-glyph" aria-hidden="true">⌁</span>}
                    <span className="search-result-copy">
                      <span className="search-result-topline">
                        <strong><HighlightedText text={searchHitTitle(hit)} query={searchResponse.query} /></strong>
                        <em>{CONTENT_LABELS[hit.content_type]}</em>
                      </span>
                      <b className="search-result-source">来自《<HighlightedText text={hit.asset_title} query={searchResponse.query} />》</b>
                      <span className="search-result-snippet"><HighlightedText text={excerpt(hit.text, searchResponse.query)} query={searchResponse.query} /></span>
                      <span className="search-result-meta">
                        <time>▶ {searchTime(hit.start_time)}</time>
                        {hit.source_status === "degraded" && <i>部分内容可用</i>}
                        <u>查看原视频 →</u>
                      </span>
                    </span>
                  </button>
                ))}
              </div>
            ) : searchResponse ? (
              <div className="search-result-empty">
                <span>⌕</span>
                <h2>{searchResponse.available_assets ? `没有找到“${cleanQuery}”相关内容` : "还没有可检索的知识资产"}</h2>
                <p>{searchResponse.available_assets ? "试试缩短关键词、检查用词，或切换到“全部”范围。" : "已完成的视频需要先形成可用知识资产，之后才能搜索章节和原文。"}</p>
                <button type="button" onClick={clearSearch}>返回全部视频</button>
              </div>
            ) : null}
          </section>
        ) : hasQuery ? (
          localSearchResults.length ? taskGrid(localSearchResults) : (
            <div className="search-result-empty">
              <span>⌕</span><h2>没有找到“{cleanQuery}”相关视频</h2><p>演示模式仅支持匹配视频标题和文件名。</p>
              <button type="button" onClick={clearSearch}>返回全部视频</button>
            </div>
          )
        ) : loading ? (
          <div className="library-message">正在读取视频资产…</div>
        ) : tasks.length ? taskGrid(tasks) : (
          <div className="library-empty">
            <span>⌁</span><h2>还没有视频笔记</h2><p>上传视频后，生成的笔记会集中显示在这里。</p>
            <button type="button" onClick={() => router.push("/#upload")}>上传第一个视频</button>
          </div>
        )}
      </section>
      <footer className="footer wrap"><span>SnapNote <b>✦</b></span><p>所有视频笔记，都在一个地方。</p><small>{tasks.length} 个视频资产</small></footer>
      {pendingDeleteTask && (
        <div className="confirm-overlay" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget && !deletingTaskId) setPendingDeleteTask(null); }}>
          <section className="confirm-dialog" role="dialog" aria-modal="true" aria-labelledby="delete-dialog-title" aria-describedby="delete-dialog-description">
            <div className="confirm-dialog-icon" aria-hidden="true">×</div>
            <h2 id="delete-dialog-title">确认删除视频？</h2>
            <p id="delete-dialog-description">以下视频及其处理结果、关联知识数据都会被清理，且无法恢复。</p>
            <div className="confirm-video-name" title={pendingDeleteTask.title}>{pendingDeleteTask.title}</div>
            <div className="confirm-dialog-actions">
              <button type="button" className="confirm-cancel" onClick={() => setPendingDeleteTask(null)} disabled={Boolean(deletingTaskId)} autoFocus>取消</button>
              <button type="button" className="confirm-delete" onClick={confirmDeleteTask} disabled={Boolean(deletingTaskId)}>{deletingTaskId ? "正在删除…" : "确认删除"}</button>
            </div>
          </section>
        </div>
      )}
    </main>
  );
}
