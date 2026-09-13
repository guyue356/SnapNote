"use client";
/* eslint-disable @next/next/no-img-element */

import { useCallback, useEffect, useRef, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import BrandHeader from "../../components/BrandHeader";
import SlideVisual from "../../components/SlideVisual";
import { deleteLocalTask, exportMarkdown, formatDuration, getTask, getVideoUrl, SAMPLE_NOTES, saveTask, type SnapTask } from "../../lib/demo";
import {
  API_BASE,
  backendAsset,
  fetchBackendTask,
  fetchKnowledgeStatus,
  hasBackend,
  rebuildKnowledge,
  toLocalTask,
  type BackendVisualAnalysis,
  type KnowledgeAssetStatus,
} from "../../lib/api";

type TranscriptSegment = {
  start: number;
  end: number;
  text: string;
};

type ChapterView = {
  time: number;
  end: number;
  keyframeTime?: number;
  title: string;
  summary: string;
  imageUrl?: string;
  demoTranscript?: string;
};

export default function ResultPage() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const videoRef = useRef<HTMLVideoElement>(null);
  const tickerRef = useRef<number | undefined>(undefined);
  const skeletonRef = useRef<HTMLElement>(null);
  const chapterRefs = useRef<Array<HTMLElement | null>>([]);
  const initialSeekRef = useRef<number | null>(null);
  const initialSeekAppliedRef = useRef(false);
  const [task, setTask] = useState<SnapTask>();
  const [videoUrl, setVideoUrl] = useState("");
  const [currentTime, setCurrentTime] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [expanded, setExpanded] = useState<Record<number, boolean>>({});
  const [chapters, setChapters] = useState<ChapterView[]>(SAMPLE_NOTES.map((note) => ({ ...note })));
  const [visualAnalysis, setVisualAnalysis] = useState<BackendVisualAnalysis>({});
  const [transcript, setTranscript] = useState<TranscriptSegment[]>([]);
  const [loadError, setLoadError] = useState("");
  const [seekError, setSeekError] = useState("");
  const [seekNotice, setSeekNotice] = useState("");
  const [knowledgeStatus, setKnowledgeStatus] = useState<KnowledgeAssetStatus>();
  const [knowledgeError, setKnowledgeError] = useState("");
  const [knowledgeBusy, setKnowledgeBusy] = useState(false);

  useEffect(() => {
    const rawTime = new URLSearchParams(window.location.search).get("t");
    const parsedTime = rawTime === null ? Number.NaN : Number(rawTime);
    initialSeekRef.current = Number.isFinite(parsedTime) && parsedTime >= 0 ? parsedTime : null;
    initialSeekAppliedRef.current = false;
    const timer = window.setTimeout(() => setSeekNotice(""), 0);
    return () => window.clearTimeout(timer);
  }, [params.id]);

  useEffect(() => {
    if (hasBackend) {
      fetchBackendTask(params.id).then((remote) => {
        setTask(toLocalTask(remote));
        setVideoUrl(backendAsset(remote.video_url));
        setVisualAnalysis(remote.visual_analysis || {});
        setTranscript(remote.transcript_segments || []);
        if (remote.knowledge_asset) setKnowledgeStatus(remote.knowledge_asset);
        const structure = remote.visual_analysis?.narrative_structure || remote.visual_analysis?.structure || [];
        if (structure.length) {
          setChapters(structure.map((section) => {
            const inRange = remote.note_blocks.filter((note) => note.timestamp >= section.start_time && note.timestamp < section.end_time);
            const candidates = inRange.length ? inRange : remote.note_blocks;
            const keyframe = candidates.reduce<(typeof remote.note_blocks)[number] | undefined>((nearest, note) => (
              !nearest || Math.abs(note.timestamp - section.start_time) < Math.abs(nearest.timestamp - section.start_time) ? note : nearest
            ), undefined);
            return {
              time: section.start_time,
              end: section.end_time,
              keyframeTime: keyframe?.timestamp ?? section.start_time,
              title: section.title || section.summary || section.description || section.stage || "未命名章节",
              summary: section.summary || section.description || "",
              imageUrl: backendAsset(keyframe?.image_url || ""),
            };
          }));
        } else {
          setChapters(remote.note_blocks.map((note) => ({
            time: note.timestamp,
            end: note.end_time,
            keyframeTime: note.timestamp,
            title: note.title,
            summary: note.summary,
            imageUrl: backendAsset(note.image_url),
          })));
        }
      }).catch(() => setLoadError("视频资产暂时无法读取，请返回后重试。"));
      return;
    }
    const localTask = getTask(params.id);
    if (!localTask) {
      router.replace("/");
      return;
    }
    const timer = window.setTimeout(() => {
      setTask(localTask);
      setVideoUrl(getVideoUrl(params.id));
    }, 0);
    return () => window.clearTimeout(timer);
  }, [params.id, router]);

  useEffect(() => {
    if (!hasBackend || knowledgeStatus?.status !== "building") return;
    const timer = window.setInterval(() => {
      fetchKnowledgeStatus(params.id)
        .then((status) => {
          setKnowledgeStatus(status);
          setKnowledgeError("");
        })
        .catch((error: Error) => setKnowledgeError(error.message));
    }, 1500);
    return () => window.clearInterval(timer);
  }, [knowledgeStatus?.status, params.id]);

  useEffect(() => () => { if (tickerRef.current) window.clearInterval(tickerRef.current); }, []);

  const applyInitialSeek = useCallback((video?: HTMLVideoElement) => {
    const requested = initialSeekRef.current;
    if (!task || requested === null || initialSeekAppliedRef.current) return;
    const knownDuration = video && Number.isFinite(video.duration) && video.duration > 0 ? video.duration : task.duration;
    const target = knownDuration > 0 ? Math.min(requested, knownDuration) : requested;
    try {
      if (video) video.currentTime = target;
      setCurrentTime(target);
      initialSeekAppliedRef.current = true;
      setSeekNotice(`已定位到 ${formatDuration(target)}`);
    } catch {
      setSeekError("暂时无法定位搜索结果，请重试。");
    }
  }, [task]);

  useEffect(() => {
    if (!task || initialSeekAppliedRef.current || initialSeekRef.current === null) return;
    if (!videoUrl || (videoRef.current && videoRef.current.readyState >= 1)) applyInitialSeek(videoRef.current || undefined);
  }, [applyInitialSeek, task, videoUrl]);

  const currentIndex = chapters.reduce((active, chapter, index) => currentTime >= chapter.time ? index : active, chapters.length ? 0 : -1);

  useEffect(() => {
    const container = skeletonRef.current;
    const chapter = chapterRefs.current[currentIndex];
    if (!container || !chapter || container.scrollHeight <= container.clientHeight) return;
    const visibleTop = container.scrollTop + 72;
    const visibleBottom = container.scrollTop + container.clientHeight;
    const chapterTop = chapter.offsetTop;
    const chapterBottom = chapterTop + chapter.offsetHeight;
    if (chapterTop < visibleTop) container.scrollTo({ top: Math.max(0, chapterTop - 84), behavior: "smooth" });
    else if (chapterBottom > visibleBottom) container.scrollTo({ top: chapterBottom - container.clientHeight + 20, behavior: "smooth" });
  }, [currentIndex]);

  function startMockPlayback() {
    setPlaying(true);
    if (tickerRef.current) window.clearInterval(tickerRef.current);
    tickerRef.current = window.setInterval(() => setCurrentTime((value) => value >= (task?.duration || 447) ? 0 : value + 1), 1000);
  }

  function seek(seconds: number, autoplay = true) {
    setSeekError("");
    setSeekNotice("");
    setCurrentTime(seconds);
    if (videoRef.current) {
      try {
        videoRef.current.currentTime = seconds;
        if (autoplay) videoRef.current.play().catch(() => setSeekError("已定位到对应时间，浏览器阻止了自动播放。"));
      } catch {
        setSeekError("暂时无法跳转，请重试。");
      }
    } else if (autoplay) {
      startMockPlayback();
    }
  }

  function togglePlayback() {
    if (videoRef.current) {
      if (videoRef.current.paused) videoRef.current.play(); else videoRef.current.pause();
      return;
    }
    if (playing) {
      setPlaying(false);
      if (tickerRef.current) window.clearInterval(tickerRef.current);
    } else {
      startMockPlayback();
    }
  }

  function chapterTranscript(chapter: ChapterView, index: number): TranscriptSegment[] {
    const nextStart = chapters[index + 1]?.time ?? chapter.end ?? task?.duration ?? Number.POSITIVE_INFINITY;
    const segments = transcript.filter((segment) => segment.end > chapter.time && segment.start < nextStart);
    if (segments.length) return segments;
    return chapter.demoTranscript ? [{ start: chapter.time, end: nextStart, text: chapter.demoTranscript }] : [];
  }

  async function regenerate() {
    if (!task) return;
    if (hasBackend) {
      await fetch(`${API_BASE}/api/snapnote/tasks/${task.id}/retry`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
      router.push(`/tasks/${task.id}/processing`);
      return;
    }
    const next: SnapTask = { ...task, status: "processing", progress: 4, stageIndex: 0, frameCount: 0, completedAt: undefined };
    saveTask(next);
    router.push(`/tasks/${task.id}/processing`);
  }

  async function rebuildKnowledgeAsset() {
    if (!task || !knowledgeStatus || knowledgeBusy) return;
    if (knowledgeStatus.status === "ready" && !window.confirm(
      "将重新解析现有章节、原文和画面，并重建检索索引。\n\n不会重新运行语音识别或 MiMo 视觉分析，也不会覆盖原视频任务结果。"
    )) return;
    const previous = knowledgeStatus;
    setKnowledgeBusy(true);
    setKnowledgeError("");
    setKnowledgeStatus({ ...previous, status: "building" });
    try {
      setKnowledgeStatus(await rebuildKnowledge(task.id));
    } catch (error) {
      setKnowledgeStatus(previous);
      setKnowledgeError(error instanceof Error ? error.message : "暂时无法开始重新构建，请稍后重试");
    } finally {
      setKnowledgeBusy(false);
    }
  }

  async function deleteSourceAsset() {
    if (!task || knowledgeBusy || !window.confirm(
      "确认删除这个视频资产？\n\n原视频、处理结果和关联知识数据都会被清理，且无法恢复。"
    )) return;
    if (!hasBackend) {
      deleteLocalTask(task.id);
      router.push("/notes");
      return;
    }
    setKnowledgeBusy(true);
    setKnowledgeError("");
    if (knowledgeStatus) setKnowledgeStatus({ ...knowledgeStatus, status: "deleting" });
    try {
      const response = await fetch(`${API_BASE}/api/snapnote/tasks/${task.id}`, { method: "DELETE" });
      if (!response.ok) throw new Error("删除清理未完成，请稍后重试");
      router.push("/notes");
    } catch (error) {
      setKnowledgeError(error instanceof Error ? error.message : "删除清理未完成，请稍后重试");
      if (hasBackend) fetchKnowledgeStatus(task.id).then(setKnowledgeStatus).catch(() => undefined);
      setKnowledgeBusy(false);
    }
  }

  const knowledgeCopy = knowledgeStatus ? {
    not_built: { label: "未构建", detail: "尚未生成可检索知识资产" },
    building: { label: "构建中", detail: "正在整理章节、原文与检索索引…" },
    ready: { label: "可用", detail: knowledgeStatus.error_summary
      ? `当前版本仍可用；最近一次重建失败：${knowledgeStatus.error_summary}`
      : "可供后续搜索与 AI 助手使用" },
    degraded: { label: "部分可用", detail: `知识资产可用，但部分内容缺失：${knowledgeStatus.missing_items.join("、") || "部分证据"}` },
    failed: { label: "构建失败", detail: `知识资产构建失败：${knowledgeStatus.error_summary || "现有内容无法形成可检索证据"}` },
    deleting: { label: "删除中", detail: "正在清理知识数据…" },
  }[knowledgeStatus.status] : undefined;

  if (loadError) {
    return (
      <main className="site-shell result-page">
        <BrandHeader compact />
        <section className="asset-error wrap">
          <span>!</span>
          <h1>无法打开视频资产</h1>
          <p>{loadError}</p>
          <button type="button" onClick={() => router.push("/")}>返回处理中心</button>
        </section>
      </main>
    );
  }

  if (!task) return <div className="loading-screen">正在打开视频资产…</div>;

  return (
    <main className="site-shell result-page">
      <BrandHeader compact />
      <section className="result-header wrap">
        <button className="back-link" type="button" onClick={() => new URLSearchParams(window.location.search).get("from") === "search" ? router.back() : router.push("/notes")}>← 返回笔记管理</button>
      </section>

      <section className="asset-workspace wrap">
        <div className="asset-video-column">
          <div className="asset-video-sticky">
            <div className={`video-stage ${videoUrl ? "" : "with-external-controls"}`}>
              {videoUrl ? (
                <video
                  ref={videoRef}
                  src={videoUrl}
                  controls
                  onLoadedMetadata={(event) => applyInitialSeek(event.currentTarget)}
                  onTimeUpdate={(event) => setCurrentTime(event.currentTarget.currentTime)}
                  onPlay={() => setPlaying(true)}
                  onPause={() => setPlaying(false)}
                  onError={() => setSeekError("视频暂时无法加载，请稍后重试。")}
                />
              ) : (
                <div className="mock-video">
                  <SlideVisual index={Math.max(0, currentIndex)} />
                  <button type="button" onClick={togglePlayback} aria-label={playing ? "暂停视频" : "播放视频"}>{playing ? "Ⅱ" : "▶"}</button>
                </div>
              )}
            </div>
            {!videoUrl && (
              <div className="mock-controls">
                <button type="button" onClick={togglePlayback}>{playing ? "Ⅱ" : "▶"}</button>
                <span>{formatDuration(currentTime)}</span>
                <input aria-label="视频进度" type="range" min="0" max={task.duration} value={currentTime} onChange={(event) => seek(Number(event.target.value), false)} />
                <span>{formatDuration(task.duration)}</span>
                <button type="button" aria-label="全屏">⛶</button>
              </div>
            )}
            <div className="asset-details">
              <h1>{task.title}</h1>
              <div className="result-meta">
                <span>{visualAnalysis.content_type || "视频资产"}</span>
                <time>{new Date(task.createdAt).toLocaleDateString("zh-CN")} 生成</time>
                <em>已完成</em>
              </div>
              <p className="asset-facts">{formatDuration(task.duration)} · {chapters.length} 个章节 · {visualAnalysis.provider || "多模态流水线"}</p>
              {(visualAnalysis.content_summary || visualAnalysis.summary) && (
                <p className="asset-overview">{visualAnalysis.content_summary || visualAnalysis.summary}</p>
              )}
              <div className="asset-actions">
                <button className="assistant-quick-action" type="button" onClick={() => router.push(`/assistant?asset=${task.id}`)}>✦ 问问这段视频</button>
                <button className="secondary-button" type="button" onClick={regenerate}>↻ 重新生成</button>
                <button className="dark-button" type="button" onClick={() => hasBackend ? window.location.assign(`${API_BASE}/api/snapnote/tasks/${task.id}/export/markdown`) : exportMarkdown(task)}>↓ 导出 Markdown</button>
                <button className="danger-button" type="button" onClick={deleteSourceAsset} disabled={knowledgeBusy}>删除视频</button>
              </div>
              {knowledgeStatus && knowledgeCopy && (
                <div className={`knowledge-status knowledge-${knowledgeStatus.status}`} role="status">
                  <span>知识资产：<strong>{knowledgeCopy.label}</strong></span>
                  <small>{knowledgeCopy.detail}</small>
                  {!(["building", "deleting"] as string[]).includes(knowledgeStatus.status) && (
                    <button type="button" onClick={rebuildKnowledgeAsset} disabled={knowledgeBusy}>
                      {knowledgeStatus.status === "not_built" ? "开始构建" : "重新构建"}
                    </button>
                  )}
                </div>
              )}
              {knowledgeError && <p className="knowledge-status-error" role="alert">{knowledgeError}</p>}
            </div>
            <div className="current-chapter-line" aria-live="polite">
              <span>当前章节</span>
              <strong>{currentIndex >= 0 ? chapters[currentIndex]?.title : "暂无章节"}</strong>
              <time>{currentIndex >= 0 ? formatDuration(chapters[currentIndex].time) : "--:--"}</time>
            </div>
            {seekError && <p className="asset-inline-notice" role="status">{seekError}</p>}
            {seekNotice && !seekError && <p className="asset-inline-notice asset-seek-notice" role="status">{seekNotice}</p>}
          </div>
        </div>

        <aside className="chapter-skeleton" aria-label="章节骨架" ref={skeletonRef}>
          <div className="skeleton-heading">
            <div><span>VIDEO OUTLINE</span><h2>章节骨架</h2></div>
            <em>{chapters.length} 章</em>
          </div>

          {chapters.length ? (
            <div className="chapter-list">
              {chapters.map((chapter, index) => {
                const segments = chapterTranscript(chapter, index);
                const isExpanded = Boolean(expanded[index]);
                return (
                  <article
                    className={`chapter-item ${index === currentIndex ? "active" : ""}`}
                    key={`${chapter.time}-${chapter.title}`}
                    ref={(element) => { chapterRefs.current[index] = element; }}
                  >
                    <div className="chapter-item-topline">
                      <button className="chapter-time" type="button" onClick={() => seek(chapter.time)} aria-label={`跳转到 ${formatDuration(chapter.time)}`}>▶ {formatDuration(chapter.time)}</button>
                      {index === currentIndex && <span>正在播放</span>}
                    </div>

                    <button className="chapter-keyframe" type="button" onClick={() => seek(chapter.keyframeTime ?? chapter.time)} aria-label={`通过画面关键帧跳转到 ${formatDuration(chapter.keyframeTime ?? chapter.time)}`}>
                      {chapter.imageUrl ? <img src={chapter.imageUrl} alt={`${chapter.title} 画面关键帧`} /> : <SlideVisual index={index} compact />}
                      <span>▶ 点击画面跳转</span>
                    </button>

                    <div className="chapter-overview">
                      <h3>{chapter.title}</h3>
                      <p>{chapter.summary || "本章节暂无概要。"}</p>
                    </div>

                    <button
                      className="transcript-toggle"
                      type="button"
                      aria-expanded={isExpanded}
                      aria-controls={`chapter-transcript-${index}`}
                      onClick={() => setExpanded((state) => ({ ...state, [index]: !state[index] }))}
                    >
                      {isExpanded ? "收起原文" : "展开原文"}<span>{isExpanded ? "⌃" : "⌄"}</span>
                    </button>

                    {isExpanded && (
                      <div className="chapter-transcript" id={`chapter-transcript-${index}`}>
                        {segments.length ? segments.map((segment, segmentIndex) => (
                          <div key={`${segment.start}-${segmentIndex}`}>
                            <button type="button" onClick={() => seek(segment.start)}>{formatDuration(segment.start)}</button>
                            <p>{segment.text}</p>
                          </div>
                        )) : <p className="transcript-empty">该章节暂无可用原文。</p>}
                      </div>
                    )}
                  </article>
                );
              })}
            </div>
          ) : (
            <div className="chapter-empty">
              <span>⌁</span>
              <h3>暂未生成章节骨架</h3>
              <p>原视频仍可正常播放，你可以重新生成章节内容。</p>
              <button type="button" onClick={regenerate}>重新生成</button>
            </div>
          )}
        </aside>
      </section>

      <footer className="footer wrap"><span>SnapNote <b>✦</b></span><p>这份视频资产已自动保存到最近任务。</p><small>按章节定位，随时回到原视频</small></footer>
    </main>
  );
}
