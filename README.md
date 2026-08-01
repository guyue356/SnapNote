<div align="center">

<img src="./docs/images/snapnote-cover.png" alt="SnapNote：把视频变成可复习的图文笔记" width="100%" />

# SnapNote

> 自动把课程与会议视频整理成带关键画面、时间锚点和复习问题的结构化图文笔记。

[![License: MIT](https://img.shields.io/badge/License-MIT-7147e8?style=flat-square)](./LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10+-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![Node.js](https://img.shields.io/badge/Node.js-22.13+-339933?style=flat-square&logo=nodedotjs&logoColor=white)](https://nodejs.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688?style=flat-square&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![React](https://img.shields.io/badge/React-19-61DAFB?style=flat-square&logo=react&logoColor=111)](https://react.dev/)

[在线体验](https://snapnote-visual-notes.yuegu827.chatgpt.site) · [产品需求文档](./SnapNote_Web_Demo_PRD_v1.md) · [API 文档](#api-接口)

</div>

---

## 项目简介

课堂录屏、技术培训和 PPT 型会议通常包含两条同等重要的信息线：讲解者说了什么，以及屏幕上正在展示什么。纯语音转写只能保留前者，用户复习时仍需在视频、截图和文字之间反复切换。

SnapNote 将视频中的关键画面、带时间戳的语音片段和结构化总结绑定在同一条时间线上。用户可以按 PPT 页面浏览摘要与知识点，点击画面或时间标签直接回到视频原位置，并将结果导出为 Markdown。项目面向学生、研究人员、职场学习者与内容整理者，当前优先优化录屏、在线课程、培训和稳定机位拍摄的 PPT 视频。

> [!IMPORTANT]
> 在线站点使用无需后端即可运行的浏览器演示模式，适合体验完整交互。真实视频解析、ffmpeg 抽帧、本地 Whisper、OCR 和 DeepSeek 增强需要在本地启动 FastAPI 后端。

---

## 核心能力

### 1. 关键画面与讲解自动绑定

后端使用 ffprobe 读取视频元信息，使用 ffmpeg 标准化音频并按时间间隔提取候选帧，再通过 pHash 去除高度相似页面。关键画面最终与相邻转写片段按时间窗口对齐，避免笔记退化为脱离视觉上下文的纯文本。

### 2. 可回看的视频图文笔记

结果页采用吸顶播放器与时间线笔记布局。点击关键帧、章节目录或时间标签即可修改播放器时间；播放过程中，当前时间所属的笔记块会自动高亮。

### 3. 可解释的长任务进度

处理过程被拆分为上传、解析、音频提取、转写、抽帧、去重、OCR、对齐、摘要和完整笔记生成等阶段。后端通过 SSE 推送事件并保留近期事件历史，前端在刷新后仍可恢复任务状态。

### 4. 可选 AI Provider 与确定性降级

本地可选启用 `faster-whisper` 和 PaddleOCR；配置 DeepSeek 后可对逐页笔记执行结构化增强。未安装重型模型或 LLM 调用失败时，流水线会保留已有产物并使用本地规则生成可展示结果。

### 5. 双运行模式

- **浏览器演示模式**：不设置后端地址，任务、进度和示例笔记保存在 `localStorage`，便于直接部署和体验。
- **真实处理模式**：设置 `NEXT_PUBLIC_API_BASE_URL`，前端改用 FastAPI、SQLite、本地文件与 SSE 处理真实视频。

### 6. Markdown 导出与历史任务

任务完成后可导出包含摘要、章节、时间锚点、关键画面、知识点和复习问题的 Markdown。SQLite 保存任务状态、阶段结果、转写、画面和笔记 JSON，首页提供历史任务入口。

---

## 效果展示

### 在线 Demo

访问 **[SnapNote 在线演示](https://snapnote-visual-notes.yuegu827.chatgpt.site)**，无需配置后端即可体验：

1. 拖入 MP4、MOV 或 WebM 视频；
2. 选择识别方案与笔记类型；
3. 查看 11 阶段处理时间线；
4. 在结果页体验视频跳转、章节导航、OCR 折叠和 Markdown 导出。

### 输入与输出

| 输入 | 输出 |
| --- | --- |
| PPT 录屏、在线课程、培训或屏幕共享视频 | 视频标题、时长与生成信息 |
| MP4 / MOV / WebM | 总体摘要与章节目录 |
| 中文优先，兼容 Whisper 支持的其他语言 | 按关键画面组织的逐页笔记 |
| 建议 60 分钟以内、PPT 占据主要画面 | 时间锚点、OCR、知识点与复习问题 |
| 至少包含一路可识别音频 | 原始转写与 Markdown 文件 |

---

## 应用场景

- **学生与研究生**：整理课程录屏、公开课和学术讲座，复习老师对公式、代码和图表的补充说明。
- **职场学习者**：把培训、技术分享和行业会议转为可检索、可回看的结构化资料。
- **产品与项目团队**：从 PPT 会议中提取页面结论、讲解片段和后续复习问题。
- **内容整理者**：将长视频转换为文章、课程纪要或知识库素材的初稿。

当前 Demo 不优先处理手持镜头、白板手写、多机位频繁转场、PPT 占比过小或需要复杂说话人分离的内容。

---

## 安装部署

### 环境要求

| 工具 | 版本 / 要求 | 用途 |
| --- | --- | --- |
| Windows PowerShell | 5.1+ | 运行一键启动脚本 |
| Conda | Miniconda 或 Anaconda | 创建 Python 3.10 环境 |
| Python | 3.10+ | FastAPI 与处理流水线 |
| Node.js | 22.13+ | Vinext 前端构建与开发 |
| ffmpeg / ffprobe | 可从命令行调用，或配置绝对路径 | 视频解析、音频和关键帧提取 |

项目目前没有提供 Dockerfile 或 Compose 配置；Windows 一键脚本是已验证的首选启动方式。

### 方式一：让 AI 编码助手安装

将下面的提示词交给 Codex、Claude Code 或其他能够操作终端的编码助手：

```text
请帮我在 Windows 上安装并启动 SnapNote：
1. 克隆仓库：https://github.com/guyue356/SnapNote.git
2. 检查 Conda、Node.js 22.13+ 和 ffmpeg
3. 在仓库根目录运行 .\start.ps1
4. 如果需要真实 ASR，请按 README 配置 backend/.env
5. 验证 http://localhost:3000 和 http://localhost:8001/api/health
请保留现有配置，不要提交任何 API Key。
```

### 方式二：Windows 一键启动

```powershell
git clone https://github.com/guyue356/SnapNote.git
cd SnapNote
.\start.ps1
```

脚本会自动：

- 创建名为 `snapnote` 的 Python 3.10 Conda 环境；
- 安装前后端依赖；
- 从示例创建本地环境配置；
- 在 `http://localhost:3000` 启动 Web；
- 在 `http://localhost:8001` 启动 API。

依赖已经安装时，可以减少检查步骤：

```powershell
.\start.ps1 -SkipInstall
```

### 方式三：手动安装

后端：

```powershell
conda create -n snapnote python=3.10 -y
conda activate snapnote
cd backend
pip install -r requirements.txt
Copy-Item .env.example .env
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8001
```

前端需要在另一个终端中启动：

```powershell
cd frontend
npm.cmd install
Copy-Item .env.example .env.local
# 将 .env.local 中的地址设置为：
# NEXT_PUBLIC_API_BASE_URL=http://localhost:8001
npm.cmd run dev
```

如果只想体验界面，将 `NEXT_PUBLIC_API_BASE_URL` 留空即可进入浏览器演示模式。

---

## 快速开始

安装完成后打开 `http://localhost:3000`：

1. 上传一个 PPT 或录屏视频；
2. 选择识别方案和“课堂笔记 / 会议笔记”；
3. 点击“开始生成图文笔记”；
4. 等待任务完成后，从画面或时间标签跳回视频；
5. 点击“导出 Markdown”保存结果。

检查后端是否正常：

```powershell
Invoke-RestMethod http://localhost:8001/api/health
```

预期响应：

```json
{
  "status": "ok",
  "service": "snapnote"
}
```

---

## 使用说明

### 浏览器演示模式

当 `NEXT_PUBLIC_API_BASE_URL` 为空时，前端使用预置的 Transformer 课程与产品复盘示例。上传的文件仅用于当前浏览器预览；任务元信息保存在 `localStorage`，刷新后仍可看到任务，但浏览器重启后原视频对象地址可能失效。

### 真实处理模式

真实模式通过 XHR 上传文件并显示实际上传百分比。任务创建后，处理页订阅 `/stream` 的命名 SSE 事件；收到进度后重新获取任务详情。处理完成后自动进入结果页，并使用后端提供的视频、关键帧、转写和笔记内容。

### 上传限制

- 文件格式：`.mp4`、`.mov`、`.webm`
- 默认最大文件：2048 MB
- 默认最大处理时长：3600 秒
- 推荐内容：录屏、在线课程、PPT 画面稳定的视频

---

## 系统架构

```mermaid
flowchart TD
    User["用户"] --> Web["Vinext Web 应用"]
    Web -->|"演示模式"| Local["localStorage 与示例任务"]
    Web -->|"HTTP 与 SSE"| API["FastAPI 服务"]

    API --> DB[("SQLite")]
    API --> Files["本地任务文件"]
    API --> Pipeline["多模态处理流水线"]

    Pipeline --> Media["ffprobe 与 ffmpeg"]
    Pipeline --> ASR["faster-whisper 可选"]
    Pipeline --> OCR["PaddleOCR 可选"]
    Pipeline --> LLM["DeepSeek 可选"]

    Media --> Align["图文时间对齐"]
    ASR --> Align
    OCR --> Align
    Align --> Notes["逐页笔记与 Markdown"]
    Notes --> DB
    Notes --> Web
```

| 模块 | 职责 |
| --- | --- |
| Vinext Web | 上传、任务列表、实时进度、播放器与笔记联动、导出入口 |
| FastAPI | 文件校验、任务 API、SSE、视频与 Markdown 文件响应 |
| Pipeline | 视频解析、音频、转写、抽帧、去重、OCR、对齐和笔记生成 |
| SQLAlchemy async | 保存任务状态、阶段历史和最终产物索引 |
| 本地文件系统 | 按 UUID 隔离存储原视频、音频、关键帧和导出文件 |
| Provider 层 | 按环境能力启用 Whisper、PaddleOCR 和 DeepSeek，失败时降级 |

---

## 核心工作流程

```mermaid
flowchart LR
    Upload["上传视频"] --> Probe["解析元信息"]
    Probe --> Audio["提取音频"]
    Audio --> ASR["带时间戳转写"]
    Probe --> Frames["按时间提取画面"]
    Frames --> Dedup["pHash 去重"]
    Dedup --> OCR["OCR 识别"]
    ASR --> Align["时间窗口对齐"]
    OCR --> Align
    Align --> Blocks["逐页笔记块"]
    Blocks --> Enhance["LLM 可选增强"]
    Enhance --> Markdown["Markdown 输出"]
```

后端按以下阶段写入进度并发送 SSE：

| 阶段 | 默认进度 | 主要产物 |
| --- | ---: | --- |
| `upload_complete` | 3% | 原始视频与任务记录 |
| `probing_video` | 8% | 时长、分辨率、编码信息 |
| `extracting_audio` | 16% | 16kHz 单声道 WAV |
| `transcribing` | 32% | 带开始/结束时间的转写片段 |
| `detecting_frames` | 45% | 候选关键画面 |
| `selecting_frames` | 55% | 稳定画面集合 |
| `deduplicating_frames` | 62% | pHash 去重后的关键帧 |
| `running_ocr` | 72% | 页面 OCR 文字 |
| `aligning` | 82% | 画面与讲解的时间对齐结果 |
| `generating_blocks` | 91% | 标题、摘要、知识点和问题 |
| `generating_note` | 97% | 完整 Markdown |
| `complete` | 100% | 可浏览与导出的任务结果 |

---

## AI 工作流程

SnapNote 当前采用确定性的流水线编排，不是多 Agent 系统，也没有 RAG 或向量数据库。这样的设计减少了 Demo 阶段的基础设施依赖，让每个处理阶段都可以独立记录、诊断和降级。

### ASR

- 设置 `ENABLE_LOCAL_WHISPER=1` 后，后端加载 `faster-whisper`，以 CPU `int8` 模式生成带时间戳的片段。
- `WHISPER_MODEL_SIZE` 和 `WHISPER_LANGUAGE` 控制模型与语言。
- 未启用 Whisper 或音频不可用时，当前版本使用演示转写保证产品闭环可展示。
- 前端已提供 MIMO-ASR 选项，`MIMO_API_KEY` 也预留在配置中，但当前代码尚未实现真实 MIMO 请求适配器。

### OCR

安装 PaddleOCR 后，流水线会按关键帧提取中英文页面文字。OCR 属于可选增强：导入失败或单帧识别异常不会中断整个任务，笔记仍可由时间窗口与转写生成。

### LLM 结构化增强

配置 `DEEPSEEK_API_KEY` 后，系统将已对齐的笔记块注入请求，要求模型输出 JSON，并保留 `id`、时间、图片、OCR 和置信度字段。模型负责改写标题、摘要、知识点和复习问题；调用失败时直接保留本地规则生成的块。

```mermaid
flowchart LR
    Material["对齐后的图文材料"] --> Prompt["结构化指令与 JSON 上下文"]
    Prompt --> DeepSeek["DeepSeek Chat Completions"]
    DeepSeek --> Parse["JSON 解析"]
    Parse -->|"成功"| Enhanced["增强笔记块"]
    Parse -->|"失败"| Fallback["保留本地笔记块"]
    Enhanced --> Export["Markdown 生成"]
    Fallback --> Export
```

### 当前算法边界

当前关键帧实现是“按配置间隔抽帧 + pHash 去重”，尚未接入 PRD 中规划的 PySceneDetect、自适应场景变化检测、运动稳定性评分和亮度/清晰度综合择优。README 将这部分列入 Roadmap，而不作为已实现能力描述。

---

## 技术栈

| 层级 | 技术 | 用途 | 选型理由 |
| --- | --- | --- | --- |
| 前端 | Vinext 0.0.50、React 19、TypeScript 5.9 | App Router 页面与交互 | 保留 Next 风格开发体验并输出 Cloudflare Worker 兼容构建 |
| 样式 | Tailwind CSS 4 + 项目级 CSS | 响应式视觉系统 | 轻量、可维护，适合快速构建产品 Demo |
| 后端 | FastAPI、Uvicorn | 异步 API 与自动文档 | 上传、SSE 和文件响应实现直接 |
| ORM | SQLAlchemy 2 async、aiosqlite | 任务与阶段结果持久化 | 单机 Demo 无需独立数据库服务 |
| 实时通信 | SSE / `sse-starlette` | 处理阶段与心跳推送 | 单向进度流比 WebSocket 更简单 |
| 媒体处理 | ffmpeg、ffprobe | 元信息、音频与关键帧 | 格式支持成熟，命令行集成稳定 |
| ASR | faster-whisper 可选 | 带时间戳语音识别 | CPU `int8` 可在本地运行 |
| 图像 | Pillow、ImageHash、PaddleOCR 可选 | pHash 去重与页面文字识别 | 适合 PPT 型关键帧处理 |
| LLM | DeepSeek Chat Completions 可选 | 标题、摘要、知识点和问题增强 | 支持结构化 JSON 输出与本地降级 |
| 部署 | Cloudflare Workers / Sites | 公开前端演示 | Worker 兼容 ESM 与边缘分发 |

---

## API 接口

启动后可访问 `http://localhost:8001/docs` 查看 OpenAPI 文档。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/health` | 服务健康检查 |
| `POST` | `/api/snapnote/tasks` | 上传视频并创建后台任务 |
| `GET` | `/api/snapnote/tasks` | 获取历史任务 |
| `GET` | `/api/snapnote/tasks/{task_id}` | 获取任务、画面、转写和笔记详情 |
| `GET` | `/api/snapnote/tasks/{task_id}/stream` | 订阅 SSE 进度和心跳 |
| `POST` | `/api/snapnote/tasks/{task_id}/retry` | 清理阶段产物并重试 |
| `GET` | `/api/snapnote/tasks/{task_id}/export/markdown` | 下载 Markdown |
| `GET` | `/api/snapnote/tasks/{task_id}/video` | 获取原视频 |
| `DELETE` | `/api/snapnote/tasks/{task_id}` | 删除数据库记录和任务目录 |

上传示例：

```bash
curl -X POST "http://localhost:8001/api/snapnote/tasks" \
  -F "video=@lecture.mp4" \
  -F "asr_provider=whisper" \
  -F "note_style=classroom"
```

响应示例：

```json
{
  "task_id": "b93346f8-1ad6-4450-827b-4e977ca3ad37",
  "status": "queued"
}
```

---

## 数据模型

### `snap_tasks`

| 字段组 | 字段 | 说明 |
| --- | --- | --- |
| 标识与文件 | `id`、`filename`、`video_path`、`audio_path` | UUID 任务与本地文件位置 |
| 状态 | `status`、`current_stage`、`progress`、`error_message` | 生命周期、阶段和错误信息 |
| 参数 | `asr_provider`、`note_style` | 识别方案与笔记类型 |
| 产物 | `frames_json`、`transcripts_json`、`notes_json`、`final_markdown` | Demo 阶段以 JSON/文本保存的结果 |
| 时间 | `created_at`、`updated_at` | UTC 创建与更新时间 |

### `snap_stage_results`

记录每个阶段的名称、状态、JSON 结果、错误信息、开始时间与完成时间，便于诊断和后续实现单阶段重试。

---

## 项目结构

```text
SnapNote/
├── backend/
│   ├── app/
│   │   ├── main.py             # FastAPI 路由、上传、SSE、导出与删除
│   │   ├── pipeline.py         # 媒体、ASR、OCR、对齐与笔记流水线
│   │   ├── database.py         # SQLAlchemy 异步模型与数据库初始化
│   │   ├── config.py           # 环境变量、存储和 ffmpeg 探测
│   │   ├── schemas.py          # API 请求与响应 Schema
│   │   └── sse_manager.py      # 订阅者队列与近期事件历史
│   ├── .env.example            # 后端配置模板
│   └── requirements.txt        # Python 依赖
├── frontend/
│   ├── app/
│   │   ├── page.tsx            # 上传首页与最近任务
│   │   ├── tasks/[id]/page.tsx # 视频与图文笔记结果页
│   │   ├── tasks/[id]/processing/page.tsx
│   │   │                         # 处理时间线与实时事件
│   │   ├── components/          # 品牌导航与画面预览组件
│   │   └── lib/                 # 后端客户端与本地演示数据
│   ├── tests/                   # Worker 服务端渲染测试
│   ├── worker/                  # Cloudflare Worker 入口
│   ├── public/                  # 图标与 Open Graph 资源
│   └── .openai/hosting.json     # Sites 项目标识和逻辑绑定
├── docs/images/                 # README 主视觉
├── storage/                     # SQLite 与任务文件，运行时生成且不提交
├── SnapNote_Web_Demo_PRD_v1.md  # Web Demo 产品需求
├── start.ps1                    # Windows 一键启动
└── README.md
```

---

## 配置说明

### 后端 `backend/.env`

| 配置项 | 必需 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `FRONTEND_ORIGIN` | 否 | `http://localhost:3000` | 允许跨域访问的前端地址 |
| `STORAGE_ROOT` | 否 | 项目内 `storage` | 数据库和任务目录 |
| `DATABASE_URL` | 否 | SQLite async URL | 可覆盖数据库连接 |
| `MAX_UPLOAD_SIZE_MB` | 否 | `2048` | 上传大小上限 |
| `MAX_VIDEO_DURATION_SECONDS` | 否 | `3600` | 最大处理时长 |
| `ENABLE_LOCAL_WHISPER` | 否 | `0` | 是否启用本地真实 ASR |
| `WHISPER_MODEL_SIZE` | 否 | `small` | faster-whisper 模型规格 |
| `WHISPER_LANGUAGE` | 否 | `zh` | 识别语言；留空可自动检测 |
| `MIMO_API_KEY` | 否 | 空 | 预留配置，当前尚未接入真实请求 |
| `DEEPSEEK_API_KEY` | 否 | 空 | 配置后启用 LLM 笔记增强 |
| `DEEPSEEK_MODEL` | 否 | `deepseek-chat` | DeepSeek 模型名 |
| `DEEPSEEK_BASE_URL` | 否 | `https://api.deepseek.com/v1` | OpenAI 兼容接口地址 |
| `FRAME_FALLBACK_INTERVAL_SECONDS` | 否 | `60` | 兜底抽帧间隔 |
| `FRAME_MAX_COUNT` | 否 | `12` | 单任务最大关键帧数 |
| `FFMPEG_BIN` / `FFPROBE_BIN` | 否 | 自动查找 | 媒体工具绝对路径 |

### 前端 `frontend/.env.local`

| 配置项 | 必需 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `NEXT_PUBLIC_API_BASE_URL` | 否 | 空 | 空值使用浏览器演示模式；真实模式填写 `http://localhost:8001` |

> [!CAUTION]
> API Key 只应保存在 `backend/.env`，不要提交到 Git，也不要写入任何 `NEXT_PUBLIC_*` 变量。

---

## 性能与扩展性

- **主要瓶颈**：本地 Whisper、PaddleOCR 和逐帧 ffmpeg 子进程会消耗 CPU、内存与磁盘 I/O。
- **当前并发模型**：任务通过 FastAPI `BackgroundTasks` 在应用进程内执行，适合单机 Demo，不适合高并发生产环境。
- **抽帧控制**：按视频时长动态计算间隔，并由 `FRAME_MAX_COUNT` 控制图片数量，避免全帧率扫描。
- **可扩展方向**：将任务编排迁移到独立队列，将 SQLite 升级为 PostgreSQL，将文件迁移到对象存储，并为 ASR/OCR 设置独立工作池。
- **前端性能**：结果页优先加载关键帧缩略图；在线演示构建为 Cloudflare Worker 兼容 ESM。

---

## 安全设计

- 上传文件名经过 `Path.name` 与字符白名单清理，不直接作为目录路径。
- 每个任务使用独立 UUID 目录，删除时校验解析后的目标必须位于任务根目录下。
- 文件大小在流式写入过程中累计校验，超限时删除已创建的任务目录。
- API Key 只从后端环境变量读取，不返回给浏览器。
- CORS 默认仅允许本地前端地址；部署真实后端时应改为实际可信域名。
- 当前 Demo 没有用户登录和任务级权限隔离，不应直接作为多租户公开文件服务。

---

## 验证与测试

前端检查：

```powershell
cd frontend
npm.cmd run lint
npm.cmd test
```

`npm test` 会先完成 Vinext 生产构建，再验证首页服务端渲染、产品文案和 Open Graph 元数据。

后端检查：

```powershell
cd backend
conda run -n snapnote python -m compileall -q app
conda run -n snapnote python -c "from app.main import app; print(app.title)"
```

---

## 项目亮点

1. **产品创新**：以“关键画面 + 对应讲解 + 时间锚点”为核心信息单元，而不是把视频简单转换为长转写文本。
2. **工程降级**：ASR、OCR 和 LLM 都允许缺失或失败，已经完成的媒体与对齐结果不会因此丢失。
3. **可诊断流水线**：阶段状态、进度、摘要与 JSON 产物均可记录，便于调参和演进到单阶段重试。
4. **双模式交付**：同一套前端既可作为无需服务端的产品 Demo，也能连接本地多模态后端处理真实视频。
5. **边缘友好前端**：Vinext 输出 Cloudflare Worker 兼容构建，同时保留 React App Router 的开发方式。

---

## Roadmap

- [x] 视频拖拽上传、进度与历史任务
- [x] ffprobe 元信息和 ffmpeg 音频/关键帧提取
- [x] 可选 faster-whisper、PaddleOCR 与 DeepSeek 增强
- [x] pHash 去重、时间窗口对齐和 Markdown 导出
- [x] 视频播放器、章节与笔记时间锚点联动
- [x] 公开在线前端演示
- [ ] 实现真实 MIMO-ASR Provider
- [ ] 接入 PySceneDetect 与自适应场景变化检测
- [ ] 增加清晰度、亮度、运动稳定性综合评分
- [ ] 支持单帧删除、重复页面合并和标题编辑
- [ ] 支持单阶段 OCR / ASR / 笔记重新执行
- [ ] 引入独立任务队列、对象存储和 PostgreSQL
- [ ] 增加用户系统、任务权限与分享控制

---

## 贡献指南

欢迎通过 Issue 和 Pull Request 改进 SnapNote。

1. Fork 本仓库并从 `main` 创建分支；
2. 分支建议使用 `feature/xxx`、`fix/xxx` 或 `docs/xxx`；
3. 提交信息遵循 Conventional Commits，例如 `feat: add scene detector`；
4. 前端改动请运行 `npm.cmd run lint` 与 `npm.cmd test`；
5. 后端改动至少执行模块导入与相关 API 检查；
6. PR 中说明测试视频类型、环境、预期结果及兼容性影响。

涉及抽帧、去重或对齐算法时，建议附上人工标注的页面时间点与前后对比，避免只针对单个样例调参。

---

## FAQ

### 为什么在线 Demo 上传后没有真正调用 Whisper？

公开站点默认是浏览器演示模式，目的是无需上传服务即可体验完整产品交互。请按安装章节启动本地后端，并配置 `NEXT_PUBLIC_API_BASE_URL=http://localhost:8001`。

### 为什么启用了真实后端，转写仍显示为演示内容？

默认 `ENABLE_LOCAL_WHISPER=0`，并且 `faster-whisper` 属于可选重型依赖。安装该依赖、将开关设为 `1`，然后重启后端。

### 选择 MIMO-ASR 会调用 MIMO 服务吗？

当前不会。UI 和配置已经为 Provider 切换预留接口，但真实 MIMO 请求适配器仍在 Roadmap 中。需要真实转写时请先使用本地 Whisper。

### PaddleOCR 安装失败会导致任务失败吗？

不会。OCR 是可选阶段；无法导入或识别失败时会保留关键帧，并继续使用转写和时间窗口生成笔记。

### ffmpeg 不在 PATH 中怎么办？

在 `backend/.env` 中填写 `FFMPEG_BIN` 和 `FFPROBE_BIN` 的绝对路径。代码也会尝试查找项目相邻目录中的 `ffmpeg/bin/*.exe`。

### 任务文件保存在哪里？

默认位于仓库根目录的 `storage/tasks/{task_id}`，SQLite 位于 `storage/app.db`。这些运行时文件已被 `.gitignore` 排除。

---

## License

本项目基于 [MIT License](./LICENSE) 开源。你可以使用、复制、修改、合并、发布和分发本项目，但需要保留原始版权与许可声明。

---

<div align="center">

如果 SnapNote 对你有帮助，欢迎提交 Issue、贡献代码或为项目点亮 Star。

</div>
