"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import BrandHeader from "../../../components/BrandHeader";
import { formatDuration, getTask, saveTask, STAGES, type SnapTask } from "../../../lib/demo";
import { API_BASE, fetchBackendTask, hasBackend, toLocalTask } from "../../../lib/api";

export default function ProcessingPage() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const [task, setTask] = useState<SnapTask | undefined>(() => typeof window === "undefined" || hasBackend ? undefined : getTask(params.id));
  const [elapsed, setElapsed] = useState(0);
  const [ready, setReady] = useState(() => typeof window !== "undefined" && !hasBackend && getTask(params.id)?.status === "completed");
  const localTaskRef = useRef(task);

  useEffect(() => {
    const started = Date.now();
    const clock = window.setInterval(() => setElapsed(Math.floor((Date.now() - started) / 1000)), 1000);

    if (hasBackend) {
      let active = true;
      const refresh = async () => {
        try {
          const remote = await fetchBackendTask(params.id);
          if (!active) return;
          const local = toLocalTask(remote);
          setTask(local);
          if (remote.status === "completed") setReady(true);
        } catch { if (active) router.replace("/"); }
      };
      refresh();
      const stream = new EventSource(`${API_BASE}/api/snapnote/tasks/${params.id}/stream`);
      const names = ["upload_complete", "probing_video", "extracting_audio", "transcribing", "detecting_frames", "selecting_frames", "deduplicating_frames", "running_ocr", "aligning", "generating_blocks", "generating_note", "complete", "step_error"];
      const handler = () => refresh();
      names.forEach((name) => stream.addEventListener(name, handler));
      return () => { active = false; stream.close(); window.clearInterval(clock); };
    }

    const found = localTaskRef.current;
    if (!found) { router.replace("/"); window.clearInterval(clock); return; }
    if (found.status === "completed") return () => window.clearInterval(clock);
    const timer = window.setInterval(() => {
      const seconds = Math.floor((Date.now() - started) / 1000);
      setTask((current) => {
        if (!current) return current;
        const progress = Math.min(100, 8 + seconds * 9);
        const stageIndex = Math.min(STAGES.length - 1, Math.floor(progress / (100 / STAGES.length)));
        const next: SnapTask = {
          ...current,
          progress,
          stageIndex,
          frameCount: Math.min(12, Math.max(0, Math.floor((progress - 32) / 5))),
          status: progress >= 100 ? "completed" : "processing",
          completedAt: progress >= 100 ? new Date().toISOString() : undefined,
        };
        saveTask(next);
        if (progress >= 100) {
          window.clearInterval(timer);
          setReady(true);
        }
        return next;
      });
    }, 700);
    return () => { window.clearInterval(timer); window.clearInterval(clock); };
  }, [params.id, router]);

  useEffect(() => {
    if (!ready) return;
    const redirect = window.setTimeout(() => router.push(`/tasks/${params.id}`), 1200);
    return () => window.clearTimeout(redirect);
  }, [ready, params.id, router]);

  const logs = useMemo(() => {
    const last = task?.stageIndex ?? 0;
    return STAGES.slice(Math.max(0, last - 3), last + 1).map((stage, index) => ({
      title: stage[0], detail: stage[1], time: `${String(10 + index * 2).padStart(2, "0")}:${String(14 + index * 7).padStart(2, "0")}:0${index}`,
    })).reverse();
  }, [task?.stageIndex]);

  if (!task) return <div className="loading-screen">正在读取任务…</div>;
  const stage = STAGES[task.stageIndex] || STAGES[0];

  return (
    <main className="site-shell processing-page">
      <BrandHeader compact />
      <section className="process-wrap wrap">
        <button className="back-link" type="button" onClick={() => router.push("/")}>← 返回处理中心</button>

        <div className="process-hero">
          <div>
            <span className="step-kicker">AI PROCESSING</span>
            <h1>{ready ? "图文笔记已生成" : "正在读懂你的视频"}</h1>
            <p>{ready ? "正在为你打开结果页…" : "画面提取与语音识别正在并行处理，你可以放心离开，进度会自动保存。"}</p>
          </div>
          <div className="progress-orbit" style={{ "--progress": `${task.progress * 3.6}deg` } as React.CSSProperties}>
            <div><b>{Math.round(task.progress)}</b><span>%</span><small>总体进度</small></div>
          </div>
        </div>

        <div className="processing-grid">
          <section className="process-card stage-card">
            <div className="current-stage">
              <div className="pulse-icon"><i /><span>✦</span></div>
              <div><small>当前阶段 · {task.stageIndex + 1}/{STAGES.length}</small><h2>{stage[0]}</h2><p>{stage[1]}</p></div>
              <span className="running-pill">{ready ? "完成" : "运行中"}</span>
            </div>
            <div className="stage-progress"><i><span style={{ width: `${task.progress}%` }} /></i><em>{Math.round(task.progress)}%</em></div>

            <ol className="stage-list">
              {STAGES.map((item, index) => {
                const state = index < task.stageIndex ? "done" : index === task.stageIndex ? "active" : "waiting";
                return (
                  <li className={state} key={item[0]}>
                    <span>{state === "done" ? "✓" : String(index + 1).padStart(2, "0")}</span>
                    <div><b>{item[0]}</b><small>{item[1]}</small></div>
                    <em>{state === "done" ? "完成" : state === "active" ? "处理中" : "等待"}</em>
                  </li>
                );
              })}
            </ol>
          </section>

          <aside>
            <section className="process-card task-info">
              <div className="mini-file"><span>▶</span><div><b>{task.title}</b><small>{task.filename}</small></div></div>
              <dl>
                <div><dt>已运行</dt><dd>{formatDuration(elapsed)}</dd></div>
                <div><dt>识别引擎</dt><dd>{task.asrProvider === "mimo" ? "MIMO-ASR" : "Whisper"}</dd></div>
                <div><dt>转写时长</dt><dd>{formatDuration(Math.min(task.duration, task.duration * task.progress / 100))}</dd></div>
                <div><dt>关键画面</dt><dd>{task.frameCount} 张</dd></div>
              </dl>
            </section>

            <section className="process-card event-card">
              <div className="aside-heading"><h3>实时事件</h3><span><i /> LIVE</span></div>
              <div className="event-list">
                {logs.map((log, index) => <div key={`${log.title}-${index}`}><time>{log.time}</time><i /><p><b>{log.title}</b><span>{log.detail}</span></p></div>)}
              </div>
            </section>

            <div className="quiet-note"><span>☕</span><p><b>可以去喝杯咖啡</b><br />处理完成后，任务会保存在最近记录中。</p></div>
          </aside>
        </div>
      </section>
    </main>
  );
}
