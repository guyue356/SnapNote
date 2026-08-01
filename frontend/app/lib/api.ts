import type { SnapTask } from "./demo";

export const API_BASE = (process.env.NEXT_PUBLIC_API_BASE_URL || "").replace(/\/$/, "");
export const hasBackend = Boolean(API_BASE);

export type BackendNote = {
  id: string;
  timestamp: number;
  end_time: number;
  title: string;
  summary: string;
  key_points: string[];
  review_questions: string[];
  ocr_text: string;
  image_url: string;
  confidence: number;
};

export type BackendTask = {
  id: string;
  filename: string;
  title: string;
  duration: number;
  status: "queued" | "processing" | "completed" | "failed";
  current_stage: string;
  progress: number;
  asr_provider: "mimo" | "whisper";
  note_style: "classroom" | "meeting";
  error_message?: string;
  frame_count: number;
  frames: Array<{ timestamp: number; image_url: string }>;
  transcript_segments: Array<{ start: number; end: number; text: string }>;
  note_blocks: BackendNote[];
  final_markdown: string;
  video_url: string;
  created_at: string;
};

export function toLocalTask(task: BackendTask): SnapTask {
  return {
    id: task.id,
    title: task.title || task.filename.replace(/\.[^.]+$/, ""),
    filename: task.filename,
    fileSize: 0,
    duration: task.duration,
    status: task.status === "completed" ? "completed" : task.status === "failed" ? "failed" : "processing",
    progress: task.progress,
    stageIndex: Math.min(10, Math.floor(task.progress / 9.1)),
    asrProvider: task.asr_provider,
    noteStyle: task.note_style,
    frameCount: task.frame_count,
    createdAt: task.created_at,
  };
}

export function createBackendTask(file: File, asr: string, style: string, onProgress: (value: number) => void): Promise<{ task_id: string }> {
  return new Promise((resolve, reject) => {
    const data = new FormData();
    data.append("video", file);
    data.append("asr_provider", asr);
    data.append("note_style", style);
    const request = new XMLHttpRequest();
    request.open("POST", `${API_BASE}/api/snapnote/tasks`);
    request.upload.onprogress = (event) => event.lengthComputable && onProgress(Math.round(event.loaded / event.total * 100));
    request.onerror = () => reject(new Error("无法连接处理服务，请确认后端已经启动。"));
    request.onload = () => {
      if (request.status >= 200 && request.status < 300) resolve(JSON.parse(request.responseText));
      else reject(new Error(JSON.parse(request.responseText || "{}").detail || "视频上传失败"));
    };
    request.send(data);
  });
}

export async function fetchBackendTask(id: string): Promise<BackendTask> {
  const response = await fetch(`${API_BASE}/api/snapnote/tasks/${id}`, { cache: "no-store" });
  if (!response.ok) throw new Error("任务不存在或暂时无法读取");
  return response.json();
}

export async function fetchBackendTasks(): Promise<BackendTask[]> {
  const response = await fetch(`${API_BASE}/api/snapnote/tasks`, { cache: "no-store" });
  if (!response.ok) throw new Error("无法读取任务列表");
  return response.json();
}

export function backendAsset(path: string) {
  if (!path) return "";
  return path.startsWith("http") ? path : `${API_BASE}${path}`;
}
