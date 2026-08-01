# SnapNote

SnapNote 是一个面向课程录屏、培训视频与 PPT 会议的 AI 图文笔记 Demo。上传视频后，它会提取关键画面与语音转写，按时间线自动对齐，并生成可回看、可搜索、可复习、可导出的结构化笔记。

## 已实现能力

- MP4、MOV、WebM 拖拽上传、格式校验、本地预览与上传进度
- MIMO-ASR / Whisper、课堂笔记 / 会议笔记选项
- 11 阶段处理时间线、实时事件、刷新恢复、失败重试
- ffprobe 元信息解析与 ffmpeg 音频/关键帧提取
- 可选 faster-whisper、PaddleOCR 与 DeepSeek 增强
- pHash 关键帧去重与按时间窗口的图文对齐
- 吸顶视频播放器、章节目录、当前笔记高亮与时间锚点跳转
- OCR 折叠展示、复习问题、原始转写与 Markdown 导出
- SQLite 任务持久化、SSE 进度流、历史任务和任务删除 API
- 无后端时可直接体验的浏览器演示模式
- 响应式桌面/移动布局及 Open Graph 分享封面

## 项目结构

```text
SnapNote/
├─ frontend/                 # Vinext / React 19 / TypeScript / Tailwind CSS 4
│  ├─ app/
│  │  ├─ page.tsx            # 上传首页与最近任务
│  │  ├─ tasks/[id]/         # 结果详情页
│  │  └─ tasks/[id]/processing/ # 处理进度页
│  └─ public/og.png          # 分享预览图
├─ backend/                  # FastAPI / SQLAlchemy async / SQLite / SSE
│  └─ app/
│     ├─ main.py             # API、上传、导出、视频服务
│     ├─ pipeline.py         # 多模态处理流水线
│     ├─ database.py         # 任务与阶段数据模型
│     └─ sse_manager.py      # 实时事件管理
├─ storage/                  # 运行时视频、帧、音频与数据库（不提交）
└─ start.ps1                 # Windows 一键启动
```

## 快速开始

需要 Windows、Conda、Node.js 22.13+ 与 ffmpeg。执行：

```powershell
.\start.ps1
```

首次运行会创建 `snapnote` Python 3.10 环境并安装依赖。启动后打开：

- Web：`http://localhost:3000`
- API 文档：`http://localhost:8001/docs`

只想查看界面时，也可以仅启动前端；未配置 `NEXT_PUBLIC_API_BASE_URL` 时会自动使用完整的浏览器演示流程：

```powershell
cd frontend
npm.cmd install
npm.cmd run dev
```

## AI Provider 配置

首次启动会从 `backend/.env.example` 创建 `backend/.env`。默认不开启本地 Whisper，未配置密钥时流水线仍会完成关键帧提取和可演示的结构化结果。

常用配置：

```env
ENABLE_LOCAL_WHISPER=1
WHISPER_MODEL_SIZE=small
WHISPER_LANGUAGE=zh
DEEPSEEK_API_KEY=
MIMO_API_KEY=
```

如需本地 Whisper 或 PaddleOCR，请取消 `backend/requirements.txt` 底部对应依赖的注释后安装。DeepSeek 未配置或调用失败时会使用确定性的本地笔记生成降级，不会丢失已完成的转写和关键帧结果。

## 主要 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/snapnote/tasks` | 上传视频并创建任务 |
| GET | `/api/snapnote/tasks` | 历史任务列表 |
| GET | `/api/snapnote/tasks/{id}` | 任务、关键帧、转写和笔记详情 |
| GET | `/api/snapnote/tasks/{id}/stream` | SSE 处理进度 |
| POST | `/api/snapnote/tasks/{id}/retry` | 重试任务 |
| GET | `/api/snapnote/tasks/{id}/export/markdown` | 导出 Markdown |
| DELETE | `/api/snapnote/tasks/{id}` | 删除任务及文件 |

## 验证

```powershell
cd frontend
npm.cmd test

cd ..\backend
conda run -n snapnote python -m compileall -q app
```

当前版本以 PPT/录屏型视频为优先输入。手持拍摄、白板手写、多机位频繁转场等复杂场景不属于 Demo 的首要优化范围。
