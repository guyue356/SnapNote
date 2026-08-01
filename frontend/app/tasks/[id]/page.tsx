"use client";
/* eslint-disable @next/next/no-img-element */

import { useEffect, useRef, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import BrandHeader from "../../components/BrandHeader";
import SlideVisual from "../../components/SlideVisual";
import { exportMarkdown, formatDuration, getTask, getVideoUrl, SAMPLE_NOTES, saveTask, type SnapTask } from "../../lib/demo";
import { API_BASE, backendAsset, fetchBackendTask, hasBackend, toLocalTask } from "../../lib/api";

type NoteView = {
  time: number;
  end: number;
  title: string;
  summary: string;
  points: readonly string[];
  question: string;
  ocr: string;
  imageUrl?: string;
};

export default function ResultPage() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const videoRef = useRef<HTMLVideoElement>(null);
  const tickerRef = useRef<number | undefined>(undefined);
  const [task, setTask] = useState<SnapTask | undefined>(() => typeof window === "undefined" || hasBackend ? undefined : getTask(params.id));
  const [videoUrl, setVideoUrl] = useState(() => typeof window === "undefined" || hasBackend ? "" : getVideoUrl(params.id));
  const [currentTime, setCurrentTime] = useState(18);
  const [playing, setPlaying] = useState(false);
  const [tab, setTab] = useState<"notes" | "transcript">("notes");
  const [expanded, setExpanded] = useState<Record<number, boolean>>({});
  const [copied, setCopied] = useState(false);
  const [notes, setNotes] = useState<NoteView[]>(SAMPLE_NOTES.map((note) => ({ ...note })));

  useEffect(() => {
    if (hasBackend) {
      fetchBackendTask(params.id).then((remote) => {
        setTask(toLocalTask(remote));
        setVideoUrl(backendAsset(remote.video_url));
        if (remote.note_blocks.length) {
          setNotes(remote.note_blocks.map((note) => ({
            time: note.timestamp,
            end: note.end_time,
            title: note.title,
            summary: note.summary,
            points: note.key_points,
            question: note.review_questions[0] || "这一页最值得复习的内容是什么？",
            ocr: note.ocr_text || "本页未识别到可用文字",
            imageUrl: backendAsset(note.image_url),
          })));
        }
      }).catch(() => router.replace("/"));
      return;
    }
    if (!getTask(params.id)) router.replace("/");
  }, [params.id, router]);

  useEffect(() => () => tickerRef.current && window.clearInterval(tickerRef.current), []);

  function seek(seconds: number, autoplay = true) {
    setCurrentTime(seconds);
    if (videoRef.current) {
      videoRef.current.currentTime = seconds;
      if (autoplay) videoRef.current.play().catch(() => undefined);
    } else if (autoplay) startMockPlayback();
  }

  function startMockPlayback() {
    setPlaying(true);
    if (tickerRef.current) window.clearInterval(tickerRef.current);
    tickerRef.current = window.setInterval(() => setCurrentTime((value) => value >= (task?.duration || 447) ? 0 : value + 1), 1000);
  }

  function togglePlayback() {
    if (videoRef.current) {
      if (videoRef.current.paused) videoRef.current.play(); else videoRef.current.pause();
      return;
    }
    if (playing) { setPlaying(false); if (tickerRef.current) window.clearInterval(tickerRef.current); }
    else startMockPlayback();
  }

  async function copySummary() {
    await navigator.clipboard.writeText("本节课程系统介绍注意力机制、QKV、缩放点积注意力与多头注意力。 ");
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1400);
  }

  async function regenerate() {
    if (!task) return;
    if (hasBackend) {
      await fetch(`${API_BASE}/api/snapnote/tasks/${task.id}/retry`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      router.push(`/tasks/${task.id}/processing`);
      return;
    }
    const next: SnapTask = { ...task, status: "processing", progress: 4, stageIndex: 0, frameCount: 0, completedAt: undefined };
    saveTask(next);
    router.push(`/tasks/${task.id}/processing`);
  }

  if (!task) return <div className="loading-screen">正在打开笔记…</div>;
  const currentIndex = notes.findIndex((note) => currentTime >= note.time && currentTime <= note.end);

  return (
    <main className="site-shell result-page">
      <BrandHeader compact />
      <section className="result-header wrap">
        <button className="back-link" type="button" onClick={() => router.push("/")}>← 返回处理中心</button>
        <div className="title-actions">
          <div><div className="result-meta"><span>课堂笔记</span><time>{new Date(task.createdAt).toLocaleDateString("zh-CN")} 生成</time><em>已完成</em></div><h1>{task.title}</h1><p>{formatDuration(task.duration)} · {task.frameCount || 12} 个关键画面 · 中文</p></div>
          <div><button className="secondary-button" type="button" onClick={regenerate}>↻ 重新生成</button><button className="dark-button" type="button" onClick={() => hasBackend ? window.location.assign(`${API_BASE}/api/snapnote/tasks/${task.id}/export/markdown`) : exportMarkdown(task)}>↓ 导出 Markdown</button></div>
        </div>
        <div className="summary-banner">
          <span className="summary-glyph">✦</span>
          <div><small>AI 总体摘要</small><p>本节课程从注意力机制的动机出发，逐步讲解 Query、Key、Value 的作用，缩放点积注意力的计算过程，以及多头注意力如何并行捕捉不同关系。理解这四个部分，是掌握 Transformer 架构的关键。</p></div>
          <button type="button" onClick={copySummary}>{copied ? "已复制" : "复制"}</button>
        </div>
      </section>

      <section className="result-workspace wrap">
        <aside className="video-column">
          <div className="video-sticky">
            <div className="video-stage">
              {videoUrl ? (
                <video ref={videoRef} src={videoUrl} controls onTimeUpdate={(event) => setCurrentTime(event.currentTarget.currentTime)} onPlay={() => setPlaying(true)} onPause={() => setPlaying(false)} />
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
                <span>{formatDuration(task.duration)}</span><button type="button">⛶</button>
              </div>
            )}
            <div className="chapter-card">
              <div className="aside-heading"><h3>章节目录</h3><span>{notes.length} 章</span></div>
              {notes.map((note, index) => <button className={index === currentIndex ? "active" : ""} type="button" key={note.title} onClick={() => seek(note.time)}><span>{String(index + 1).padStart(2, "0")}</span><p><b>{note.title}</b><small>{formatDuration(note.time)}</small></p></button>)}
            </div>
          </div>
        </aside>

        <div className="notes-column">
          <div className="notes-tabs">
            <div><button className={tab === "notes" ? "active" : ""} onClick={() => setTab("notes")} type="button">图文笔记 <span>{notes.length}</span></button><button className={tab === "transcript" ? "active" : ""} onClick={() => setTab("transcript")} type="button">原始转写</button></div>
            <label>⌕ <input placeholder="搜索笔记" aria-label="搜索笔记" /></label>
          </div>

          {tab === "notes" ? (
            <div className="note-feed">
              {notes.map((note, index) => (
                <article className={`note-card ${index === currentIndex ? "active" : ""}`} key={note.title}>
                  <div className="note-number"><span>{String(index + 1).padStart(2, "0")}</span><i /></div>
                  <div className="note-body">
                    <button className="note-frame" type="button" onClick={() => seek(note.time)} aria-label={`跳转到 ${formatDuration(note.time)}`}>{note.imageUrl ? <img src={note.imageUrl} alt={`${note.title} 关键画面`} /> : <SlideVisual index={index} compact />}<span>▶ 点击画面跳转</span></button>
                    <div className="note-content">
                      <div className="note-title"><div><button type="button" onClick={() => seek(note.time)}>▶ {formatDuration(note.time)}</button><h2>{note.title}</h2></div><em>{index === currentIndex ? "正在播放" : `${Math.round(note.end - note.time)} 秒`}</em></div>
                      <p>{note.summary}</p>
                      <div className="knowledge-box"><small>关键知识点</small><ul>{note.points.map((point) => <li key={point}>{point}</li>)}</ul></div>
                      <div className="question-box"><span>?</span><div><small>复习一下</small><p>{note.question}</p></div></div>
                      <button className="ocr-toggle" type="button" onClick={() => setExpanded((state) => ({ ...state, [index]: !state[index] }))}>OCR 页面文字 <span>{expanded[index] ? "−" : "+"}</span></button>
                      {expanded[index] && <pre className="ocr-content">{note.ocr}</pre>}
                    </div>
                  </div>
                </article>
              ))}
            </div>
          ) : (
            <div className="transcript-panel">
              <div className="transcript-heading"><div><span>原始转写</span><h2>带时间戳的完整讲解</h2></div><button type="button" onClick={() => exportMarkdown(task)}>↓ 下载文本</button></div>
              {notes.flatMap((note, index) => [0, 1].map((part) => (
                <button type="button" key={`${index}-${part}`} onClick={() => seek(note.time + part * 32)}><time>{formatDuration(note.time + part * 32)}</time><p>{part ? `${note.summary} 这里需要特别注意公式中缩放项对训练稳定性的影响。` : note.summary}</p></button>
              )))}
            </div>
          )}
        </div>
      </section>

      <footer className="footer wrap"><span>SnapNote <b>✦</b></span><p>这份笔记已自动保存到最近任务。</p><small>按时间锚点，随时回到原视频</small></footer>
    </main>
  );
}
