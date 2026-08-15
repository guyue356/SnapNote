"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import BrandHeader from "../../../components/BrandHeader";
import { formatDuration, getTask, saveTask, STAGES, type ProcessingBranchState, type SnapTask } from "../../../lib/demo";
import { API_BASE, fetchBackendTask, hasBackend, toLocalTask } from "../../../lib/api";

const BRANCH_ORDER = ["common", "audio", "vision", "multimodal", "output"] as const;
const BRANCH_ICONS: Record<(typeof BRANCH_ORDER)[number], string> = {
  common: "01",
  audio: "♫",
  vision: "◫",
  multimodal: "✦",
  output: "✓",
};

function fallbackProcessingState(task: SnapTask): Record<string, ProcessingBranchState> {
  const progress = Math.max(0, Math.min(100, task.progress));
  const branch = (
    label: string,
    title: string,
    message: string,
    start: number,
    end: number,
  ): ProcessingBranchState => {
    const value = progress >= end ? 100 : progress <= start ? 0 : Math.round((progress - start) / (end - start) * 100);
    return {
      label,
      stage: value === 0 ? "waiting" : title,
      title: value === 0 ? "等待开始" : title,
      message: value === 0 ? "等待上游步骤完成" : message,
      progress: value,
      status: task.status === "failed" && value > 0 && value < 100 ? "failed" : value >= 100 ? "completed" : value > 0 ? "running" : "queued",
    };
  };
  return {
    common: branch("准备视频", "解析视频", "读取视频时长、分辨率与编码信息", 0, 8),
    audio: branch("音频处理", "语音转写", "提取音频并生成带时间戳的转写", 8, 58),
    vision: branch("画面处理", "检测与筛选关键帧", "扫描镜头变化并选择代表画面", 8, 58),
    multimodal: branch("多模态理解", "理解画面与视频", "结合转写理解关键帧、动态片段和整片风格", 58, 90),
    output: branch("结果生成", "生成图文笔记", "完成时间对齐并组织最终结果", 90, 100),
  };
}

function eventTime(value?: string | null) {
  if (!value) return "等待";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "刚刚" : date.toLocaleTimeString("zh-CN", { hour12: false });
}

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
      const names = ["upload_complete", "probing_video", "extracting_audio", "transcribing", "detecting_frames", "selecting_frames", "deduplicating_frames", "running_ocr", "understanding_frames", "understanding_clips", "analyzing_style", "aligning", "generating_blocks", "generating_note", "complete", "step_error"];
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

  const processingState = useMemo(
    () => task ? (task.processingState && Object.keys(task.processingState).length ? task.processingState : fallbackProcessingState(task)) : {},
    [task],
  );
  const branches = useMemo(
    () => {
      if (!task) return [];
      const fallback = fallbackProcessingState(task);
      return BRANCH_ORDER.map((key) => ({ key, ...(processingState[key] || fallback[key]) }));
    },
    [processingState, task],
  );
  const activeBranches = branches.filter((branch) => branch.status === "running");
  const logs = useMemo(
    () => branches
      .filter((branch) => branch.updated_at || branch.status !== "queued")
      .sort((left, right) => String(right.updated_at || "").localeCompare(String(left.updated_at || "")))
      .slice(0, 5)
      .map((branch) => ({ title: branch.title, detail: `${branch.label} · ${branch.message}`, time: eventTime(branch.updated_at) })),
    [branches],
  );

  if (!task) return <div className="loading-screen">正在读取任务…</div>;
  const failed = task.status === "failed";
  const currentTitle = failed
    ? "处理遇到问题"
    : activeBranches.length > 1
      ? `${activeBranches.map((branch) => branch.label).join("与")}正在并行`
      : activeBranches[0]?.title || (ready ? "所有步骤已完成" : "正在等待下一步");
  const currentMessage = failed
    ? task.errorMessage || "处理未能完成，请返回任务页重试。"
    : activeBranches.length > 1
      ? activeBranches.map((branch) => `${branch.label}：${branch.title}`).join("；")
      : activeBranches[0]?.message || (ready ? "图文笔记已经生成" : "正在准备处理资源");
  const audioProgress = processingState.audio?.progress || 0;

  return (
    <main className="site-shell processing-page">
      <BrandHeader compact />
      <section className="process-wrap wrap">
        <button className="back-link" type="button" onClick={() => router.push("/")}>← 返回处理中心</button>

        <div className="process-hero">
          <div>
            <span className="step-kicker">AI PROCESSING</span>
            <h1>{ready ? "图文笔记已生成" : failed ? "这次处理没有完成" : "正在读懂你的视频"}</h1>
            <p>{ready ? "正在为你打开结果页…" : failed ? "你可以返回任务页调整识别引擎后重试。" : "音频与画面会并行处理；每条分支的状态和进度都会自动保存。"}</p>
          </div>
          <div className="progress-orbit" style={{ "--progress": `${task.progress * 3.6}deg` } as React.CSSProperties}>
            <div><b>{Math.round(task.progress)}</b><span>%</span><small>总体进度</small></div>
          </div>
        </div>

        <div className="processing-grid">
          <section className="process-card stage-card">
            <div className="current-stage">
              <div className={`pulse-icon${failed ? " failed" : ""}`}><i /><span>{failed ? "!" : "✦"}</span></div>
              <div><small>当前正在进行</small><h2>{currentTitle}</h2><p>{currentMessage}</p></div>
              <span className={`running-pill${failed ? " failed" : ""}`}>{ready ? "完成" : failed ? "失败" : "运行中"}</span>
            </div>
            <div className="stage-progress"><i><span style={{ width: `${task.progress}%` }} /></i><em>{Math.round(task.progress)}%</em></div>

            <div className="pipeline-route" aria-label="处理流程">
              <span>准备视频</span><i>→</i><span className="split-route">音频 + 画面</span><i>→</i><span>多模态理解</span><i>→</i><span>生成结果</span>
            </div>

            <ol className="branch-list">
              {branches.map((branch) => {
                const state = branch.status === "completed" ? "done" : branch.status === "running" ? "active" : branch.status === "failed" ? "failed" : "waiting";
                const statusText = state === "done" ? "已完成" : state === "active" ? "处理中" : state === "failed" ? "失败" : "等待";
                return (
                  <li className={state} key={branch.key}>
                    <div className="branch-icon">{state === "done" ? "✓" : BRANCH_ICONS[branch.key]}</div>
                    <div className="branch-copy">
                      <div><span>{branch.label}</span><em>{statusText}</em></div>
                      <h3>{branch.title}</h3>
                      <p>{branch.message}</p>
                      <div className="branch-progress" role="progressbar" aria-label={`${branch.label}进度`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(branch.progress)}>
                        <i><span style={{ width: `${branch.progress}%` }} /></i><b>{Math.round(branch.progress)}%</b>
                      </div>
                    </div>
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
                <div><dt>转写时长</dt><dd>{formatDuration(Math.min(task.duration, task.duration * audioProgress / 100))}</dd></div>
                <div><dt>关键画面</dt><dd>{task.frameCount} 张</dd></div>
              </dl>
            </section>

            <section className="process-card event-card">
              <div className="aside-heading"><h3>实时事件</h3><span><i /> LIVE</span></div>
              <div className="event-list">
                {logs.length ? logs.map((log, index) => <div key={`${log.title}-${index}`}><time>{log.time}</time><i /><p><b>{log.title}</b><span>{log.detail}</span></p></div>) : <p className="event-empty">正在等待第一个处理事件…</p>}
              </div>
            </section>

            <div className="quiet-note"><span>☕</span><p><b>可以去喝杯咖啡</b><br />处理完成后，任务会保存在最近记录中。</p></div>
          </aside>
        </div>
      </section>
    </main>
  );
}
