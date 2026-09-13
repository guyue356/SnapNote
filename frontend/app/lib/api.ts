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
  content_summary?: string;
  visual_summary?: string;
  video_title?: string;
  title_confidence?: number;
  content_type?: string;
  target_audience?: string;
  hook?: string;
  visual_style?: string;
  editing_style?: string;
  viral_elements?: string[];
  recurring_patterns?: string[];
  recommendations?: string[];
  structure?: Array<{ title?: string; stage?: string; stage_role?: string; start_time: number; end_time: number; summary?: string; description?: string }>;
  narrative_structure?: Array<{ title?: string; stage?: string; stage_role?: string; start_time: number; end_time: number; summary?: string; description?: string }>;
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
  title: string | null;
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
  matched_field: "asset_title" | "chapter_title" | "content_title" | "text" | "semantic";
  source_status: "ready" | "degraded";
};

export type KnowledgeSearchResponse = {
  query: string;
  scope: { asset_ids: string[] | null; owner_scope: string };
  results: KnowledgeSearchHit[];
  total: number;
  available_assets: number;
  degraded_search: boolean;
  retrieval_mode: "hybrid" | "keyword";
  semantic_status: "disabled" | "indexing" | "partial" | "model_error" | "ready";
  semantic_error: string | null;
  semantic_index: {
    indexed_chunks: number;
    total_chunks: number;
    coverage: number;
  };
  elapsed_ms: number;
};

export type AssistantScope = {
  asset_ids: string[] | null;
  content_types: KnowledgeContentType[] | null;
  created_after: string | null;
  created_before: string | null;
};

export type AssistantConversation = {
  id: string;
  owner_scope: string;
  title: string;
  status: "active" | "archived";
  default_scope: AssistantScope;
  scope_label: string;
  created_at: string;
  updated_at: string;
};

export type AssistantCitation = {
  ordinal: number;
  asset_id: string;
  task_id: string;
  asset_version_id: string;
  chunk_id: string;
  chapter_id: string | null;
  asset_title: string;
  content_type?: KnowledgeContentType;
  chapter_title?: string | null;
  start_time: number | null;
  end_time: number | null;
  text: string;
  keyframe: { id?: string; relative_uri?: string; timestamp?: number; availability: string } | null;
  availability: "available" | "unavailable";
};

export type AssistantAssetResult = {
  asset_id: string;
  task_id: string;
  title: string;
  status: "ready" | "degraded";
  updated_at: string;
};

export type AssistantMessage = {
  id: string;
  role: "user" | "assistant" | "system";
  intent: "asset_query" | "knowledge_qa" | "unsupported_action";
  status: "pending" | "retrieving" | "streaming" | "completed" | "failed" | "cancelled";
  content: string;
  scope_snapshot: AssistantScope;
  retrieval: Record<string, unknown>;
  error_code?: string | null;
  created_at: string;
  citations: AssistantCitation[];
  asset_results?: { items: AssistantAssetResult[]; total: number };
};

export type AssistantSseEvent = { event: string; data: Record<string, unknown> };

function assistantError(response: Response, fallback: string) {
  return response.json().catch(() => ({})).then((payload) => {
    const detail = payload.detail;
    return new Error(typeof detail === "string" ? detail : typeof detail?.summary === "string" ? detail.summary : fallback);
  });
}

export async function createAssistantConversation(defaultScope?: Partial<AssistantScope>): Promise<AssistantConversation> {
  const response = await fetch(`${API_BASE}/api/assistant/conversations`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ owner_scope: "local", default_scope: { asset_ids: null, content_types: null, created_after: null, created_before: null, ...defaultScope } }),
  });
  if (!response.ok) throw await assistantError(response, "无法创建助手会话");
  return response.json();
}

export async function fetchAssistantConversations(status: "active" | "archived" = "active"): Promise<AssistantConversation[]> {
  const params = new URLSearchParams({ owner_scope: "local", status });
  const response = await fetch(`${API_BASE}/api/assistant/conversations?${params}`, { cache: "no-store" });
  if (!response.ok) throw await assistantError(response, "无法读取助手会话");
  return response.json();
}

async function updateAssistantConversationStatus(id: string, action: "archive" | "restore"): Promise<AssistantConversation> {
  const response = await fetch(`${API_BASE}/api/assistant/conversations/${id}/${action}`, { method: "POST" });
  if (!response.ok) throw await assistantError(response, action === "archive" ? "无法归档会话" : "无法恢复会话");
  return response.json();
}

export function archiveAssistantConversation(id: string) {
  return updateAssistantConversationStatus(id, "archive");
}

export function restoreAssistantConversation(id: string) {
  return updateAssistantConversationStatus(id, "restore");
}

export async function deleteAssistantConversation(id: string): Promise<void> {
  const response = await fetch(`${API_BASE}/api/assistant/conversations/${id}`, { method: "DELETE" });
  if (!response.ok) throw await assistantError(response, "无法永久删除会话");
}

export async function fetchAssistantMessages(id: string): Promise<{ conversation: AssistantConversation; messages: AssistantMessage[] }> {
  const response = await fetch(`${API_BASE}/api/assistant/conversations/${id}/messages`, { cache: "no-store" });
  if (!response.ok) throw await assistantError(response, "无法读取会话内容");
  return response.json();
}

export async function updateAssistantScope(id: string, scope: AssistantScope): Promise<AssistantConversation> {
  const response = await fetch(`${API_BASE}/api/assistant/conversations/${id}/scope`, {
    method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ scope }),
  });
  if (!response.ok) throw await assistantError(response, "无法更新知识范围");
  return response.json();
}

export async function fetchAssistantScopeAssets(scope: AssistantScope): Promise<{ total: number; items: AssistantAssetResult[]; scope_label: string }> {
  const params = new URLSearchParams({ owner_scope: "local" });
  if (scope.asset_ids) params.set("asset_ids", scope.asset_ids.join(","));
  if (scope.content_types) params.set("content_types", scope.content_types.join(","));
  if (scope.created_after) params.set("created_after", scope.created_after);
  if (scope.created_before) params.set("created_before", scope.created_before);
  const response = await fetch(`${API_BASE}/api/assistant/scope/assets?${params}`, { cache: "no-store" });
  if (!response.ok) throw await assistantError(response, "无法读取知识范围");
  return response.json();
}

export async function streamAssistantMessage(
  id: string,
  payload: { content: string; client_request_id: string; scope_override?: AssistantScope },
  onEvent: (event: AssistantSseEvent) => void,
  signal?: AbortSignal,
) {
  const response = await fetch(`${API_BASE}/api/assistant/conversations/${id}/messages`, {
    method: "POST", headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify(payload), signal,
  });
  if (!response.ok) throw await assistantError(response, "助手暂时无法回答");
  if (!response.body) throw new Error("助手连接没有返回内容");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const consume = (chunk: string) => {
    buffer += chunk;
    const parts = buffer.split(/\r?\n\r?\n/);
    buffer = parts.pop() || "";
    for (const part of parts) {
      const event = part.match(/^event:\s*(.+)$/m)?.[1] || "message";
      const data = part.match(/^data:\s*(.+)$/m)?.[1];
      if (!data) continue;
      try { onEvent({ event, data: JSON.parse(data) }); } catch { /* ignore malformed heartbeat */ }
    }
  };
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    consume(decoder.decode(value, { stream: true }));
  }
  consume(decoder.decode());
}

export async function cancelAssistantMessage(id: string) {
  await fetch(`${API_BASE}/api/assistant/messages/${id}/cancel`, { method: "POST" });
}

export type BackendTask = {
  id: string;
  filename: string;
  title: string;
  generated_title?: string | null;
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
