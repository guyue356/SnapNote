import type { ProcessingBranchState, SnapTask } from "./demo";

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
  visual_analysis?: Record<string, unknown>;
  clip_analysis?: Record<string, unknown>;
};

export type BackendVisualAnalysis = {
  summary?: string;
  content_type?: string;
  target_audience?: string;
  hook?: string;
  visual_style?: string;
  editing_style?: string;
  viral_elements?: string[];
  recurring_patterns?: string[];
  recommendations?: string[];
  structure?: Array<{ stage: string; start_time: number; end_time: number; description: string }>;
  narrative_structure?: Array<{ stage: string; start_time: number; end_time: number; description: string }>;
  storyboard?: Array<{ start_time: number; end_time: number; shot: string; purpose: string }>;
  provider?: string;
};

export type KnowledgeAssetStatus = {
  asset_id: string | null;
  status: "not_built" | "building" | "ready" | "degraded" | "failed" | "deleting";
  current_version_id: string | null;
  missing_items: string[];
  error_summary?: string | null;
  updated_at?: string;
};

export type KnowledgeContentType = "video_summary" | "chapter_summary" | "transcript" | "note";

export type KnowledgeSearchHit = {
  chunk_id: string;
  asset_id: string;
  task_id: string;
  asset_version_id: string;
  asset_title: string;
  content_type: KnowledgeContentType;
  chapter_id: string | null;
  chapter_title: string | null;
  text: string;
  start_time: number | null;
  end_time: number | null;
  keyframe: {
    id: string;
    frame_id: string;
    media_type: string;
    relative_uri: string;
    timestamp: number;
    availability: string;
  } | null;
  score: number;
  matched_field: "asset_title" | "chapter_title" | "content_title" | "text";
  source_status: "ready" | "degraded";
};

export type KnowledgeSearchResponse = {
  query: string;
  scope: { asset_ids: string[] | null; owner_scope: string };
  results: KnowledgeSearchHit[];
  total: number;
  available_assets: number;
  degraded_search: boolean;
  elapsed_ms: number;
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
  note_model: "mimo" | "deepseek";
  error_message?: string;
  frame_count: number;
  frames: Array<{ timestamp: number; image_url: string }>;
  transcript_segments: Array<{ start: number; end: number; text: string }>;
  note_blocks: BackendNote[];
  visual_analysis: BackendVisualAnalysis;
  final_markdown: string;
  video_url: string;
  created_at: string;
  processing_state?: Record<string, ProcessingBranchState>;
  knowledge_asset?: KnowledgeAssetStatus | null;
};

export function toLocalTask(task: BackendTask): SnapTask {
  const stageOrder = [
    "upload_complete", "probing_video", "extracting_audio", "transcribing",
    "detecting_frames", "selecting_frames", "deduplicating_frames", "running_ocr",
    "understanding_frames", "understanding_clips", "analyzing_style", "aligning",
    "generating_blocks", "generating_note", "complete",
  ];
  const remoteStage = stageOrder.indexOf(task.current_stage);
  return {
    id: task.id,
    title: task.title || task.filename.replace(/\.[^.]+$/, ""),
    filename: task.filename,
    fileSize: 0,
    duration: task.duration,
    status: task.status === "completed" ? "completed" : task.status === "failed" ? "failed" : "processing",
    progress: task.progress,
    stageIndex: remoteStage >= 0 ? remoteStage : Math.min(stageOrder.length - 1, Math.floor(task.progress / (100 / stageOrder.length))),
    asrProvider: task.asr_provider,
    noteStyle: task.note_style,
    noteModel: task.note_model || "mimo",
    frameCount: task.frame_count,
    createdAt: task.created_at,
    errorMessage: task.error_message,
    processingState: task.processing_state,
    thumbnailUrl: backendAsset(task.frames[0]?.image_url || ""),
  };
}

export function createBackendTask(file: File, asr: string, style: string, noteModel: string, onProgress: (value: number) => void): Promise<{ task_id: string }> {
  return new Promise((resolve, reject) => {
    const data = new FormData();
    data.append("video", file);
    data.append("asr_provider", asr);
    data.append("note_style", style);
    data.append("note_model", noteModel);
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

export async function searchKnowledge(
  query: string,
  contentTypes?: KnowledgeContentType[],
  signal?: AbortSignal,
): Promise<KnowledgeSearchResponse> {
  const response = await fetch(`${API_BASE}/api/knowledge/search`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      query,
      ...(contentTypes?.length ? { content_types: contentTypes } : {}),
      top_k: 12,
      owner_scope: "local",
    }),
    signal,
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    const detail = payload.detail;
    const message = typeof detail === "string"
      ? detail
      : typeof detail?.summary === "string"
        ? detail.summary
        : response.status === 503
          ? "检索功能当前未启用"
          : "知识检索暂时不可用，请稍后重试";
    throw new Error(message);
  }
  return response.json();
}

export async function retryBackendTask(id: string, asrProvider?: "mimo" | "whisper"): Promise<void> {
  const response = await fetch(`${API_BASE}/api/snapnote/tasks/${id}/retry`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(asrProvider ? { asr_provider: asrProvider } : {}),
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(typeof payload.detail === "string" ? payload.detail : "暂时无法重新生成，请稍后重试");
  }
}

export async function deleteBackendTask(id: string): Promise<void> {
  const response = await fetch(`${API_BASE}/api/snapnote/tasks/${id}`, { method: "DELETE" });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(typeof payload.detail === "string" ? payload.detail : "删除清理未完成，请稍后重试");
  }
}

export async function fetchKnowledgeStatus(id: string): Promise<KnowledgeAssetStatus> {
  const response = await fetch(`${API_BASE}/api/snapnote/tasks/${id}/knowledge`, { cache: "no-store" });
  if (!response.ok) throw new Error("知识状态暂不可用，不影响视频浏览");
  return response.json();
}

export async function rebuildKnowledge(id: string): Promise<KnowledgeAssetStatus> {
  const response = await fetch(`${API_BASE}/api/snapnote/tasks/${id}/knowledge/rebuild`, { method: "POST" });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(typeof payload.detail === "string" ? payload.detail : "暂时无法开始重新构建，请稍后重试");
  }
  const payload = await response.json();
  return {
    asset_id: payload.asset_id,
    current_version_id: payload.asset_version_id,
    status: payload.status,
    missing_items: payload.missing_items || [],
    error_summary: null,
  };
}

export function backendAsset(path: string) {
  if (!path) return "";
  return path.startsWith("http") ? path : `${API_BASE}${path}`;
}
