"use client";

import { ChangeEvent, DragEvent, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { useRouter } from "next/navigation";
import BrandHeader from "./components/BrandHeader";
import { createBackendTask, deleteBackendTask, fetchBackendTasks, hasBackend, retryBackendTask, toLocalTask } from "./lib/api";
import {
  createLocalTask,
  deleteLocalTask,
  formatBytes,
  formatDuration,
  getLocalTasksServerSnapshot,
  getLocalTasksSnapshot,
  saveVideoUrl,
  retryLocalTask,
  subscribeLocalTasks,
  type SnapTask,
} from "./lib/demo";

const ACCEPTED = ["video/mp4", "video/quicktime", "video/webm"];

export default function Home() {
  const router = useRouter();
  const inputRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [previewUrl, setPreviewUrl] = useState("");
  const [asr, setAsr] = useState<"mimo" | "whisper">("whisper");
  const [style, setStyle] = useState<"classroom" | "meeting">("classroom");
  const [noteModel, setNoteModel] = useState<"mimo" | "deepseek">("mimo");
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState("");
  const [uploading, setUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState(0);
  const [taskActionBusy, setTaskActionBusy] = useState("");
  const [taskActionError, setTaskActionError] = useState("");
  const localTasks = useSyncExternalStore(subscribeLocalTasks, getLocalTasksSnapshot, getLocalTasksServerSnapshot);
  const [remoteTasks, setRemoteTasks] = useState<SnapTask[]>([]);
  const tasks = hasBackend ? remoteTasks : localTasks;
  const recentTasks = [...tasks]
    .sort((left, right) => new Date(right.createdAt).getTime() - new Date(left.createdAt).getTime())
    .slice(0, 3);

  useEffect(() => {
    if (!hasBackend) return;
    let cancelled = false;
    fetchBackendTasks()
      .then((items) => {
        if (!cancelled) setRemoteTasks(items.map(toLocalTask));
      })
      .catch(() => undefined);
    return () => { cancelled = true; };
  }, []);

  function chooseFile(next: File | undefined) {
    if (!next) return;
    const ext = next.name.split(".").pop()?.toLowerCase();
    if (!ACCEPTED.includes(next.type) && !["mp4", "mov", "webm"].includes(ext || "")) {
      setError("暂不支持该格式，请选择 MP4、MOV 或 WebM 视频。 ");
      return;
    }
    if (next.size > 2 * 1024 * 1024 * 1024) {
      setError("视频超过 2 GB，请压缩后重试。 ");
      return;
    }
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    const url = URL.createObjectURL(next);
    setFile(next);
    setPreviewUrl(url);
    setError("");
  }

  function onDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    setDragging(false);
    chooseFile(event.dataTransfer.files[0]);
  }

  async function startTask() {
    if (!file || uploading) return;
    setUploading(true);
    setUploadProgress(4);

    if (hasBackend) {
      try {
        const created = await createBackendTask(file, asr, style, noteModel, setUploadProgress);
        setUploadProgress(100);
        router.push(`/tasks/${created.task_id}/processing`);
      } catch (problem) {
        setError(problem instanceof Error ? problem.message : "上传失败，请稍后重试。 ");
        setUploading(false);
      }
      return;
    }

    const task = createLocalTask(file, asr, style, noteModel);
    saveVideoUrl(task.id, previewUrl);

    const timer = window.setInterval(() => {
      setUploadProgress((value) => Math.min(96, value + Math.max(2, Math.round((100 - value) / 5))));
    }, 110);

    await new Promise((resolve) => window.setTimeout(resolve, 850));
    window.clearInterval(timer);
    setUploadProgress(100);
    router.push(`/tasks/${task.id}/processing`);
  }

  async function retryFailedTask(task: SnapTask) {
    if (taskActionBusy) return;
    setTaskActionBusy(task.id);
    setTaskActionError("");
    try {
      if (hasBackend) await retryBackendTask(task.id, task.asrProvider);
      else retryLocalTask(task);
      router.push(`/tasks/${task.id}/processing`);
    } catch (problem) {
      setTaskActionError(problem instanceof Error ? problem.message : "暂时无法重新生成，请稍后重试。");
      setTaskActionBusy("");
    }
  }

  async function deleteFailedTask(task: SnapTask) {
    if (taskActionBusy || !window.confirm(
      `确认删除失败任务“${task.title}”？\n\n原视频、处理结果和关联知识数据都会被清理，且无法恢复。`
    )) return;
    setTaskActionBusy(task.id);
    setTaskActionError("");
    try {
      if (hasBackend) {
        await deleteBackendTask(task.id);
        setRemoteTasks((items) => items.filter((item) => item.id !== task.id));
      } else {
        deleteLocalTask(task.id);
      }
    } catch (problem) {
      setTaskActionError(problem instanceof Error ? problem.message : "删除清理未完成，请稍后重试。");
    } finally {
      setTaskActionBusy("");
    }
  }

  return (
    <main className="site-shell">
      <BrandHeader />

      <section className="hero wrap">
        <div className="hero-copy">
          <div className="eyebrow"><span className="spark">✦</span> AI 多模态笔记</div>
          <h1>SnapNote<br /><span>把知识视频，变成可检索的多模态笔记</span></h1>
          <p className="hero-lead">
            自动提取关键画面、转写语音内容，并与时间轴精准对齐，每条笔记都能定位关键画面，处理 30 分钟视频，模型成本仅需约 0.5 元。
          </p>
          <div className="value-row">
            <div><b>01</b><span>关键画面<br />自动捕捉</span></div>
            <div><b>02</b><span>语音画面<br />精准对齐</span></div>
            <div><b>03</b><span>风格分镜<br />结构输出</span></div>
          </div>
          <div className="proof-row">
            <div className="avatar-stack"><i>林</i><i>周</i><i>陈</i><i>+</i></div>
            <p><strong>让视频变成可复用素材</strong><br />适合长视频整理、内容研究与爆款分析</p>
          </div>
        </div>

        <div className="upload-card" id="upload">
          <div className="card-heading">
            <div><span className="step-kicker">STEP 01</span><h2>上传你的视频</h2></div>
            <span className="privacy-pill">本地安全处理</span>
          </div>

          <div
            className={`dropzone ${dragging ? "is-dragging" : ""} ${file ? "has-file" : ""}`}
            onDragOver={(event) => { event.preventDefault(); setDragging(true); }}
            onDragLeave={() => setDragging(false)}
            onDrop={onDrop}
            onClick={() => !file && inputRef.current?.click()}
            role="button"
            tabIndex={0}
            onKeyDown={(event) => event.key === "Enter" && inputRef.current?.click()}
          >
            <input
              ref={inputRef}
              className="sr-only"
              type="file"
              accept="video/mp4,video/quicktime,video/webm,.mp4,.mov,.webm"
              onChange={(event: ChangeEvent<HTMLInputElement>) => chooseFile(event.target.files?.[0])}
            />
            {file ? (
              <div className="selected-file">
                <video src={previewUrl} muted playsInline />
                <div>
                  <span className="file-ready">✓ 视频已就绪</span>
                  <h3 title={file.name}>{file.name}</h3>
                  <p>{formatBytes(file.size)} · {file.type.replace("video/", "").toUpperCase() || "VIDEO"}</p>
                  <button type="button" onClick={(event) => { event.stopPropagation(); inputRef.current?.click(); }}>更换视频</button>
                </div>
              </div>
            ) : (
              <div className="dropzone-empty">
                <div className="upload-glyph"><span>↑</span></div>
                <h3>拖放视频到这里</h3>
                <p>或 <button type="button">浏览本地文件</button></p>
                <small>MP4、MOV、WebM · 最大 2 GB · 建议 60 分钟内</small>
              </div>
            )}
          </div>

          {error && <div className="inline-error" role="alert">! {error}</div>}

          <div className="field-grid">
            <fieldset>
              <legend>语音识别</legend>
              <label className={asr === "mimo" ? "selected" : ""}>
                <input type="radio" name="asr" checked={asr === "mimo"} onChange={() => setAsr("mimo")} />
                <span><b>MIMO-ASR</b><small>云端中文与方言</small></span>
              </label>
              <label className={asr === "whisper" ? "selected" : ""}>
                <input type="radio" name="asr" checked={asr === "whisper"} onChange={() => setAsr("whisper")} />
                <span><b>Whisper</b><small>复用本机缓存 · 低成本</small></span><em>推荐</em>
              </label>
            </fieldset>
            <fieldset>
              <legend>笔记增强模型</legend>
              <label className={noteModel === "mimo" ? "selected" : ""}>
                <input type="radio" name="note-model" checked={noteModel === "mimo"} onChange={() => setNoteModel("mimo")} />
                <span><b>MiMo v2.5</b><small>视觉理解与笔记统一</small></span><em>推荐</em>
              </label>
              <label className={noteModel === "deepseek" ? "selected" : ""}>
                <input type="radio" name="note-model" checked={noteModel === "deepseek"} onChange={() => setNoteModel("deepseek")} />
                <span><b>DeepSeek Chat</b><small>文本整理与表达增强</small></span>
              </label>
            </fieldset>
            <fieldset>
              <legend>笔记类型</legend>
              <div className="toggle-group">
                <button type="button" className={style === "classroom" ? "active" : ""} onClick={() => setStyle("classroom")}><b>课堂笔记</b><small>知识点与复习题</small></button>
                <button type="button" className={style === "meeting" ? "active" : ""} onClick={() => setStyle("meeting")}><b>会议笔记</b><small>结论与行动项</small></button>
              </div>
            </fieldset>
          </div>

          {uploading && (
            <div className="upload-progress" aria-live="polite">
              <div><span>正在创建任务</span><b>{uploadProgress}%</b></div>
              <i><span style={{ width: `${uploadProgress}%` }} /></i>
            </div>
          )}

          <button className="primary-button" type="button" onClick={startTask} disabled={!file || uploading}>
            <span>{uploading ? "正在上传…" : "开始生成图文笔记"}</span><b>→</b>
          </button>
          <p className="upload-tip">支持课程、会议、访谈、产品、剧情、短视频和屏幕录制；动态镜头会自动补充时序理解。</p>
        </div>
      </section>

      <section className="recent-section wrap" id="recent">
        <div className="section-title">
          <div><span className="step-kicker">YOUR LIBRARY</span><h2>最近任务</h2></div>
          <button type="button" className="section-link" onClick={() => router.push("/notes")}>查看全部 {tasks.length} 个视频 →</button>
        </div>
        <div className="task-table">
          <div className="task-row task-head"><span>视频</span><span>创建时间</span><span>时长</span><span>关键帧</span><span>状态</span><span /></div>
          {recentTasks.map((task) => (
            <div className="task-row" key={task.id}>
              <div className="task-name"><i>{task.noteStyle === "classroom" ? "课" : "会"}</i><span><b>{task.title}</b><small>{task.filename}</small></span></div>
              <span>{new Date(task.createdAt).toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" })}</span>
              <span>{formatDuration(task.duration)}</span>
              <span>{task.frameCount || "—"}</span>
              <span><em className={`status ${task.status}`}>{task.status === "completed" ? "已完成" : task.status === "failed" ? "失败" : "处理中"}</em></span>
              <div className="task-row-actions">
                {task.status === "failed" && (
                  <>
                    <button type="button" className="row-action retry icon-tooltip" data-tooltip="重新生成" onClick={() => retryFailedTask(task)} disabled={taskActionBusy === task.id} aria-label={`重新生成 ${task.title}`}>↻</button>
                    <button type="button" className="row-action delete icon-tooltip" data-tooltip="删除任务" onClick={() => deleteFailedTask(task)} disabled={taskActionBusy === task.id} aria-label={`删除 ${task.title}`}>×</button>
                  </>
                )}
                <button type="button" className="row-action icon-tooltip" data-tooltip="查看详情" onClick={() => router.push(task.status === "completed" ? `/tasks/${task.id}` : `/tasks/${task.id}/processing`)} aria-label={`查看 ${task.title}`}>↗</button>
              </div>
            </div>
          ))}
          {!recentTasks.length && <div className="task-empty">还没有任务，上传第一个视频开始生成笔记吧。</div>}
        </div>
        {taskActionError && <p className="recent-action-error" role="alert">{taskActionError}</p>}
      </section>

      <footer className="footer wrap"><span>SnapNote <b>✦</b></span><p>让视频不再只是看过，而是留下真正可复习的知识。</p><small>Web Demo · 2026</small></footer>
    </main>
  );
}
