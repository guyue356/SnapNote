"use client";

import { useEffect, useMemo, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import BrandHeader from "../../../components/BrandHeader";
import { deleteLocalTask, formatDuration, getTask, retryLocalTask, saveTask, STAGES, type ProcessingBranchState, type ProcessingSubstepState, type SnapTask } from "../../../lib/demo";
import { API_BASE, deleteBackendTask, fetchBackendTask, hasBackend, retryBackendTask, toLocalTask } from "../../../lib/api";

// “结果生成”只做本地对齐、序列化和落库，通常在毫秒级完成。将它合并到
// 用户可感知的 AI 收尾阶段，避免把短暂的技术步骤渲染成第 5 个长耗时阶段。
const BRANCH_ORDER = ["common", "audio", "vision", "multimodal"] as const;
const BRANCH_ICONS: Record<(typeof BRANCH_ORDER)[number], string> = {
  common: "01",
  audio: "♫",
  vision: "◫",
  multimodal: "✦",
};
const AI_SUBSTEPS = [
  { key: "keyframe_understanding", label: "关键帧理解" },
  { key: "clip_understanding", label: "动态片段理解" },
  { key: "style_synthesis", label: "整片分析" },
  { key: "note_enhancement", label: "笔记增强" },
] as const;
const VISION_SUBSTEPS = [
  { key: "semantic_selection", label: "语义覆盖选帧", message: "等待语音与镜头候选汇合" },
  { key: "frame_extraction", label: "关键帧抽取", message: "等待微语义单元选帧完成" },
  { key: "coverage_audit", label: "覆盖审计", message: "等待检查语义覆盖率和时间空洞" },
  { key: "coverage_repair", label: "定向补帧", message: "仅在存在缺失单元或时间空洞时执行" },
] as const;

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
    multimodal: branch("AI 分析与结果生成", "理解画面并增强笔记", "结合转写理解画面，完成整片分析、笔记增强与结果生成", 58, 100),
  };
}

function eventTime(value?: string | null) {
  if (!value) return "等待";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "刚刚" : date.toLocaleTimeString("zh-CN", { hour12: false });
}

function substepElapsed(step: ProcessingSubstepState, now: number) {
  if (typeof step.elapsed_seconds === "number") return Math.max(0, step.elapsed_seconds);
  if (step.status !== "running" || !step.started_at) return 0;
  const started = new Date(step.started_at).getTime();
  return Number.isNaN(started) ? 0 : Math.max(0, Math.floor((now - started) / 1000));
}

function substepMeta(step: ProcessingSubstepState) {
  const metadata = step.metadata || {};
  const model = typeof metadata.model === "string" ? metadata.model : "";
  const usage = metadata.usage && typeof metadata.usage === "object"
    ? metadata.usage as Record<string, unknown>
    : {};
  const attempts = Number(usage.request_attempts || 0);
  const calls = Number(usage.request_count || 0);
  const candidateShots = Number(metadata.candidate_shots || 0);
  const selectedShots = Number(metadata.selected_shots || 0);
  const extractedFrames = Number(metadata.extracted_frames || 0);
  const coverage = Number(metadata.semantic_coverage_ratio);
  const maxGap = Number(metadata.max_frame_gap_seconds);
  const repairRound = Number(metadata.repair_round || metadata.coverage_repair_rounds || 0);
  return [
    model,
    candidateShots ? `${candidateShots} 个候选` : "",
    selectedShots ? `${selectedShots} 个锚点` : "",
    extractedFrames ? `${extractedFrames} 张` : "",
    Number.isFinite(coverage) ? `覆盖 ${Math.round(coverage * 100)}%` : "",
    Number.isFinite(maxGap) ? `最大空洞 ${maxGap.toFixed(1)} 秒` : "",
    repairRound ? `${repairRound} 轮补帧` : "",
    calls ? `${calls} 次请求` : "",
    attempts > calls ? `${attempts} 次尝试` : "",
  ]
    .filter(Boolean)
    .slice(0, 3)
    .join(" · ");
}

function mergeSubsteps(
  definitions: ReadonlyArray<{ key: string; label: string; message?: string }>,
  recorded: Record<string, ProcessingSubstepState> = {},
): ProcessingSubstepState[] {
  return definitions.map((item) => recorded[item.key] || {
    ...item,
    status: "queued",
    message: item.message || "等待上游步骤完成",
  });
}

export default function ProcessingPage() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const [task, setTask] = useState<SnapTask | undefined>();
  const [elapsed, setElapsed] = useState(0);
  const [clockNow, setClockNow] = useState(() => Date.now());
  const [ready, setReady] = useState(false);
  const [actionBusy, setActionBusy] = useState<"retry" | "delete" | "">("");
  const [actionError, setActionError] = useState("");

  useEffect(() => {
    const started = Date.now();
    const clock = window.setInterval(() => {
      const now = Date.now();
      setElapsed(Math.floor((now - started) / 1000));
      setClockNow(now);
    }, 1000);

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

    const found = getTask(params.id);
    if (!found) { router.replace("/"); window.clearInterval(clock); return; }
    if (found.status === "completed") {
      const hydrate = window.setTimeout(() => {
        setTask(found);
        setReady(true);
      }, 0);
      return () => { window.clearTimeout(hydrate); window.clearInterval(clock); };
    }
    const hydrate = window.setTimeout(() => setTask(found), 0);
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
    return () => { window.clearTimeout(hydrate); window.clearInterval(timer); window.clearInterval(clock); };
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
      return BRANCH_ORDER.map((key) => ({
        key,
        ...(processingState[key] || fallback[key]),
        ...(key === "multimodal" ? { label: "AI 分析与结果生成" } : {}),
      }));
    },
    [processingState, task],
  );
  const failed = task?.status === "failed";
  const activeBranches = branches.filter((branch) => branch.status === "running");
  const aiFinishing = !failed && processingState.multimodal?.status === "running"
    && processingState.multimodal?.stage === "analyzing_style";
  const aiFinishingStartedAt = aiFinishing
    ? new Date(processingState.multimodal?.stage_started_at || processingState.multimodal?.updated_at || "").getTime()
    : Number.NaN;
  const aiFinishingElapsed = Number.isNaN(aiFinishingStartedAt)
    ? 0
    : Math.max(0, Math.floor((clockNow - aiFinishingStartedAt) / 1000));
  const recordedAiSubsteps = processingState.multimodal?.substeps || {};
  const aiSubsteps = mergeSubsteps(AI_SUBSTEPS, recordedAiSubsteps);
  const visionSubsteps = mergeSubsteps(VISION_SUBSTEPS, processingState.vision?.substeps || {});
  const logs = useMemo(
    () => branches
      .filter((branch) => branch.updated_at || branch.status !== "queued")
      .sort((left, right) => String(right.updated_at || "").localeCompare(String(left.updated_at || "")))
      .slice(0, 5)
      .map((branch) => ({ title: branch.title, detail: `${branch.label} · ${branch.message}`, time: eventTime(branch.updated_at) })),
    [branches],
  );

  async function retryTask() {
    if (!task || actionBusy) return;
    setActionBusy("retry");
    setActionError("");
    try {
      if (hasBackend) {
        await retryBackendTask(task.id, task.asrProvider);
        setTask({ ...task, status: "processing", progress: 4, stageIndex: 0, errorMessage: undefined, processingState: undefined });
      } else {
        setTask(retryLocalTask(task));
      }
      setReady(false);
      setElapsed(0);
      setClockNow(Date.now());
    } catch (problem) {
      setActionError(problem instanceof Error ? problem.message : "暂时无法重新生成，请稍后重试。");
    } finally {
      setActionBusy("");
    }
  }

  async function deleteTask() {
    if (!task || actionBusy || !window.confirm(
      `确认删除失败任务“${task.title}”？\n\n原视频、处理结果和关联知识数据都会被清理，且无法恢复。`
    )) return;
    setActionBusy("delete");
    setActionError("");
    try {
      if (hasBackend) await deleteBackendTask(task.id);
      else deleteLocalTask(task.id);
      router.push("/notes");
    } catch (problem) {
      setActionError(problem instanceof Error ? problem.message : "删除清理未完成，请稍后重试。");
      setActionBusy("");
    }
  }

  if (!task) return <div className="loading-screen">正在读取任务…</div>;
  const currentTitle = failed
    ? "处理遇到问题"
    : aiFinishing
      ? "正在等待 AI 整片分析/笔记增强"
    : activeBranches.length > 1
      ? `${activeBranches.map((branch) => branch.label).join("与")}正在并行`
      : activeBranches[0]?.title || (ready ? "所有步骤已完成" : "正在等待下一步");
  const currentMessage = failed
    ? task.errorMessage || "处理未能完成，请返回任务页重试。"
    : aiFinishing
      ? `模型正在并行归纳整片风格并增强笔记，已等待 ${formatDuration(aiFinishingElapsed)}；模型响应和重试可能需要几分钟。`
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
            {failed && (
              <div className="failure-actions">
                <button className="retry-button" type="button" onClick={retryTask} disabled={Boolean(actionBusy)}>{actionBusy === "retry" ? "正在重新生成…" : "↻ 重新生成"}</button>
                <button className="danger-button" type="button" onClick={deleteTask} disabled={Boolean(actionBusy)}>{actionBusy === "delete" ? "正在删除…" : "删除任务"}</button>
                <button className="secondary-button" type="button" onClick={() => router.push("/notes")} disabled={Boolean(actionBusy)}>返回笔记管理</button>
              </div>
            )}
            {actionError && <p className="failure-action-error" role="alert">{actionError}</p>}
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

            <div className="pipeline-route" aria-label="处理流程，共 4 个阶段">
              <span><b>1</b>准备视频</span><i>→</i><span className="split-route"><b>2</b>音频 + 画面</span><i>→</i><span><b>3</b>AI 分析与增强</span><i>→</i><span><b>4</b>生成结果</span>
            </div>

            <ol className="branch-list">
              {branches.map((branch) => {
                const state = branch.status === "completed" ? "done" : branch.status === "running" ? "active" : branch.status === "failed" ? "failed" : "waiting";
                const waitingForAi = branch.key === "multimodal" && aiFinishing;
                const statusText = state === "done" ? "已完成" : waitingForAi ? `AI 等待 ${formatDuration(aiFinishingElapsed)}` : state === "active" ? "处理中" : state === "failed" ? "失败" : "等待";
                const substeps = branch.key === "vision" ? visionSubsteps : branch.key === "multimodal" ? aiSubsteps : [];
                return (
                  <li className={state} key={branch.key}>
                    <div className="branch-icon">{state === "done" ? "✓" : BRANCH_ICONS[branch.key]}</div>
                    <div className="branch-copy">
                      <div><span>{branch.label}</span><em>{statusText}</em></div>
                      <h3>{branch.title}</h3>
                      <p>{waitingForAi ? "正在等待 AI 整片分析/笔记增强；完成后会立即生成结果。" : branch.message}</p>
                      {substeps.length > 0 && (
                        <div className="ai-substeps" aria-label={`${branch.label}各环节运行记录`}>
                          {substeps.map((step) => {
                            const duration = substepElapsed(step, clockNow);
                            const metadata = substepMeta(step);
                            const statusLabel = {
                              queued: "未开始",
                              running: "处理中",
                              completed: "完成",
                              degraded: "已降级",
                              failed: "失败",
                              skipped: "已跳过",
                            }[step.status];
                            return (
                              <div className={`ai-substep ${step.status}`} key={step.key}>
                                <i aria-hidden="true" />
                                <span><b>{step.label}</b><small>{step.message}{metadata ? ` · ${metadata}` : ""}</small></span>
                                <em>{statusLabel}{duration ? ` · ${formatDuration(duration)}` : ""}</em>
                              </div>
                            );
                          })}
                        </div>
                      )}
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
