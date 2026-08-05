export type TaskStatus = "processing" | "completed" | "failed";

export type SnapTask = {
  id: string;
  title: string;
  filename: string;
  fileSize: number;
  duration: number;
  status: TaskStatus;
  progress: number;
  stageIndex: number;
  asrProvider: "mimo" | "whisper";
  noteStyle: "classroom" | "meeting";
  frameCount: number;
  createdAt: string;
  completedAt?: string;
};

export const STAGES = [
  ["上传完成", "视频已安全保存，准备进入处理队列"],
  ["解析视频", "读取时长、分辨率、编码与音轨信息"],
  ["提取音频", "将语音标准化为适合识别的音频格式"],
  ["精确语音转写", "生成带开始与结束时间的语音文本"],
  ["检测镜头变化", "扫描场景切换、运动强度与画面质量"],
  ["选择关键帧", "为每个镜头选择清晰稳定的代表画面"],
  ["关键帧去重", "合并重复或高度相似的视觉素材"],
  ["本地 OCR", "低成本提取画面中的可见文字"],
  ["MiMo 关键帧理解", "批量识别主体、场景、构图与视觉风格"],
  ["MiMo 动态片段理解", "补充动作、运镜、转场和节奏信息"],
  ["整片风格与分镜", "归纳叙事结构、爆款元素和可复用模式"],
  ["多模态时间对齐", "绑定镜头、视觉语义与语音转写"],
  ["生成结构化内容", "生成逐镜头摘要、重点与分析问题"],
  ["生成完整结果", "组织 Markdown 与整片分析产物"],
  ["处理完成", "你的图文笔记已经准备好"],
] as const;

export const SAMPLE_NOTES = [
  {
    time: 18,
    end: 92,
    title: "注意力机制：从固定窗口到动态聚焦",
    summary: "讲解从传统序列模型的局限切入：当输入变长时，固定长度的上下文很难保留全部信息。注意力机制让模型根据当前任务，动态判断输入中哪些部分更重要。",
    points: ["权重由当前查询与上下文内容共同决定", "长距离依赖不再需要逐步传递", "注意力权重具有一定的可解释性"],
    question: "为什么注意力机制更适合处理长序列中的远距离关系？",
    ocr: "Attention Mechanism / Dynamic Focus / Long-range Dependency",
  },
  {
    time: 93,
    end: 188,
    title: "Query、Key、Value 的角色分工",
    summary: "Query 表示当前需要寻找的信息，Key 用于衡量候选内容与 Query 的相关程度，Value 则承载最终被聚合的信息。三者共同完成一次内容寻址。",
    points: ["Query 与 Key 的相似度决定注意力分数", "Softmax 将分数归一化为权重", "Value 按权重加权求和得到输出"],
    question: "如果 Key 不变而 Value 改变，注意力权重与最终输出会如何变化？",
    ocr: "Q = XWq / K = XWk / V = XWv / softmax(QKᵀ)",
  },
  {
    time: 189,
    end: 302,
    title: "缩放点积注意力的计算过程",
    summary: "点积衡量 Query 与 Key 的匹配程度；除以维度平方根可以避免高维下数值过大，让 Softmax 保持稳定梯度。随后用归一化权重对 Value 求和。",
    points: ["点积结果除以 √dk 完成缩放", "Mask 可以屏蔽未来位置或无效填充", "矩阵运算便于 GPU 并行计算"],
    question: "为什么不缩放 QKᵀ 会让 Softmax 更容易进入饱和区？",
    ocr: "Attention(Q,K,V) = softmax(QKᵀ / √dk)V",
  },
  {
    time: 303,
    end: 447,
    title: "多头注意力：并行学习多种关系",
    summary: "单个注意力头往往只捕捉一种匹配模式。多头注意力把表示投影到多个子空间，让不同的头分别关注位置、语义、指代或句法关系，最后拼接形成更丰富的表示。",
    points: ["每个头拥有独立的投影参数", "多个头可学习互补关系", "拼接后再经过线性层融合信息"],
    question: "增加注意力头数量一定会提升效果吗？还要考虑哪些代价？",
    ocr: "MultiHead(Q,K,V) = Concat(head₁ … headₕ)Wo",
  },
] as const;

const STORAGE_KEY = "snapnote.tasks.v1";
const VIDEO_KEY = "__snapnoteVideoUrls";

function seededTasks(): SnapTask[] {
  const now = Date.now();
  return [
    { id: "demo-transformer", title: "Transformer 核心原理与注意力机制", filename: "week-04-transformer.mp4", fileSize: 486000000, duration: 447, status: "completed", progress: 100, stageIndex: STAGES.length - 1, asrProvider: "mimo", noteStyle: "classroom", frameCount: 12, createdAt: new Date(now - 86400000).toISOString(), completedAt: new Date(now - 85800000).toISOString() },
    { id: "demo-product", title: "产品增长实验复盘会", filename: "growth-review.mov", fileSize: 238000000, duration: 2154, status: "completed", progress: 100, stageIndex: STAGES.length - 1, asrProvider: "whisper", noteStyle: "meeting", frameCount: 18, createdAt: new Date(now - 172800000).toISOString(), completedAt: new Date(now - 171600000).toISOString() },
  ];
}

export function getTasks(): SnapTask[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) {
      return (JSON.parse(raw) as SnapTask[]).map((task) => (
        task.status === "completed"
          ? { ...task, progress: 100, stageIndex: STAGES.length - 1 }
          : task
      ));
    }
  } catch { /* use seeded tasks */ }
  const seeded = seededTasks();
  localStorage.setItem(STORAGE_KEY, JSON.stringify(seeded));
  return seeded;
}

export function getTask(id: string): SnapTask | undefined {
  return getTasks().find((task) => task.id === id);
}

export function saveTask(task: SnapTask) {
  const tasks = getTasks();
  const index = tasks.findIndex((item) => item.id === task.id);
  if (index >= 0) tasks[index] = task; else tasks.unshift(task);
  localStorage.setItem(STORAGE_KEY, JSON.stringify(tasks));
}

export function createLocalTask(file: File, asrProvider: SnapTask["asrProvider"], noteStyle: SnapTask["noteStyle"]): SnapTask {
  const id = typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : `task-${Date.now()}`;
  const title = file.name.replace(/\.[^.]+$/, "").replace(/[-_]+/g, " ");
  const task: SnapTask = { id, title, filename: file.name, fileSize: file.size, duration: 447, status: "processing", progress: 4, stageIndex: 0, asrProvider, noteStyle, frameCount: 0, createdAt: new Date().toISOString() };
  saveTask(task);
  return task;
}

export function saveVideoUrl(id: string, url: string) {
  if (typeof window === "undefined") return;
  const target = window as typeof window & { [VIDEO_KEY]?: Record<string, string> };
  target[VIDEO_KEY] = { ...(target[VIDEO_KEY] || {}), [id]: url };
}

export function getVideoUrl(id: string) {
  if (typeof window === "undefined") return "";
  const target = window as typeof window & { [VIDEO_KEY]?: Record<string, string> };
  return target[VIDEO_KEY]?.[id] || "";
}

export function formatBytes(value: number) {
  if (!value) return "0 MB";
  if (value >= 1024 ** 3) return `${(value / 1024 ** 3).toFixed(2)} GB`;
  return `${(value / 1024 ** 2).toFixed(1)} MB`;
}

export function formatDuration(seconds: number) {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
}

export function exportMarkdown(task: SnapTask) {
  const content = `# ${task.title}\n\n> 由 SnapNote 生成 · ${formatDuration(task.duration)}\n\n## 总体摘要\n\n本节课程系统介绍了注意力机制的核心动机、Query/Key/Value 的分工、缩放点积注意力，以及多头注意力的表示能力。\n\n${SAMPLE_NOTES.map((note, index) => `## ${index + 1}. ${note.title}\n\n**时间锚点：${formatDuration(note.time)}**\n\n${note.summary}\n\n### 关键知识点\n${note.points.map((point) => `- ${point}`).join("\n")}\n\n### 复习问题\n${note.question}`).join("\n\n")}\n`;
  const blob = new Blob([content], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `${task.title || "SnapNote"}.md`;
  anchor.click();
  URL.revokeObjectURL(url);
}
