# 片场 · 多模态内容创作台

支持品牌营销视频与短剧 / 漫剧两条创作路线的本地内容创作平台。左侧提供「新对话」「创作流程」和「制作记录」入口：普通对话支持持续讨论、图片/视频/文件附件理解，并可生成图片、短视频或语音；涉及媒体生成时，助手会先说明生成内容并等待用户确认。创作流程选择内容类型并填写 brief，再按脚本、视觉设定、分镜、逐镜任务和视频片段逐步确认。工作流草稿可编辑，未经确认不会推进；已确认步骤也可回退修改或重新生成。上游修改后，后续内容与媒体素材会保留并标记为过期，重新确认后可逐步重生成；被覆盖的文本草稿可从历史版本恢复。确认镜头任务后，可逐镜生成/重试关键帧和视频片段，并查看 AI 剪辑方案、素材版本及供应商实际返回的任务/费用信息。制作记录复用项目数据保存脚本、分镜、镜头任务与素材；当前版本不将供应商视频 URL 复制到本地，URL 失效后可能无法回放。当前交付物是逐镜视频片段和剪辑方案，不代表已合并成单个 MP4。

### 对话附件与最近对话

对话输入框支持 JPEG/PNG/WebP/GIF 图片、MP4/MOV/M4V/WebM 视频、TXT/MD/CSV/JSON/YAML/XML/HTML/LOG、PDF 和 DOCX；单个文件最大 20 MB，每条消息最多 8 个且合计不超过 40 MB。文本/PDF/DOCX 在本机抽取文本，视频最多抽取 6 张画面帧、最长分析 180 秒，不分析视频声音。每轮对话最多将最近 8 个附件（合计 40 MB）发送给模型；更早的附件会被省略并提示重新上传。图片及视频画面会作为视觉输入发送给所选文本模型，因此应选择支持视觉输入的模型；附件内容也会发送到配置的模型供应商处理。附件文件保存在本机媒体目录。扫描版 PDF 不做 OCR。

左侧最近对话可重命名，或「从最近对话移除」。移除只隐藏该条目，不删除项目、聊天记录、制作记录、任务或本地媒体；创作项目仍可从「制作记录」打开。

## 当前集成

默认模型目录见 [`config/models.json`](config/models.json)。工作台右上角“模型配置”支持添加、编辑、删除模型 profile，以及为每种模态切换默认模型；模型目录更改会保存到该文件并立即应用。API Key 可直接在配置界面填写并回显；密钥独立保存到数据库同目录下的 `model-credentials.json`，不写入模型目录或 SQLite。文件会尝试设置为仅当前用户可读写（`0600`），并通过临时文件原子替换。服务环境变量仍可作为凭据来源。

| 模态 | 默认配置 | 当前能力 |
|---|---|---|
| 文本 | Agnes 2.5 Flash | OpenAI-compatible Chat Completions、工具调用 |
| 文本 | Claude Sonnet | Anthropic Messages API / Python SDK |
| 图像 | Agnes Image 2.5 Flash | 文生图，支持配置画幅与尺寸 |
| 视频 | Agnes Video 2.5 Flash | 异步创建任务、轮询状态、返回成片 URL |
| 音频 | OpenAI GPT-4o mini TTS / Transcribe | 对话中语音生成；另提供音频转写 API |

模型和接口会更新，以上 ID 以配置目录和供应商当前官方文档为准。Agnes 官方文档：

- [Agnes 2.5 Flash](https://wiki.agnes-ai.com/en/docs/agnes-25-flash)
- [Agnes Image 2.5 Flash](https://wiki.agnes-ai.com/en/docs/agnes-image-25-flash)
- [Agnes Video 2.5 Flash](https://wiki.agnes-ai.com/en/docs/agnes-video-25-flash)

注意：Agnes Video 2.5 Flash 单次视频时长为 4–12 秒；工作流将长内容拆成多个逐镜任务，每镜独立生成和重试。本版本暂未实现多段视频拼接，也不把 AI 剪辑方案描述成已生成成片。视频 URL 仍由供应商托管，若 URL 失效，当前记录页无法回放。镜头任务校验时长、画幅和提示词，并按素材版本保存供应商实际返回的数据；若供应商不返回费用，界面会显示未知，不作估算。Agnes 没有在当前配置中假定独立音频生成模型；语音生成/转写示例用 OpenAI-compatible 音频适配器。

## 启动

需要 Python 3.10+；推荐 Python 3.12。

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
cp .env.example .env
# 编辑 .env，填入你有权使用的供应商 API Key
set -a; source .env; set +a
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

打开 <http://127.0.0.1:8000>。可以在启动前设置环境变量，也可以启动后在右上角“模型配置”中填写 API Key。没有凭据时服务仍可启动和查看配置，但生成请求会返回明确的未配置提示，不会静默切换到伪造结果。应用只调用真实 Provider，不含演示模式；自动化测试使用隔离的伪 Provider，不会访问线上服务。

### Agnes 首次接入

可以在 `.env` 中设置（也可启动应用后在“模型配置”里粘贴）：

```env
AGNES_API_KEY=你的Agnes_API_Key
```

模型目录已配置 Agnes 2.5 Flash 文本、图像、视频三个 profile。启动后打开右上角“模型配置”核对凭据状态，再从左侧“新对话”开始，例如输入“生成一张竖版咖啡产品广告图”或“生成一个 5 秒、9:16 的咖啡产品短视频”。要逐阶段产出脚本和分镜，请选择“创作流程”。生成真实媒体可能消耗账户额度；提交前请核对供应商价格、试用资格和账户限制。

## 自定义供应商和模型

1. 在工作台“模型配置”中添加/编辑 profile，设置 `id`、`modality`、`adapter`、`model_id`、`base_url` 和可选 `params`；API Key 可直接粘贴，环境变量兼容仍可用。模型目录中的 `api_key_env` 仅用于旧配置及环境变量回退。
2. 界面保存会立即应用模型目录和凭据；直接编辑 `config/models.json` 后需重启服务。Key 保存在独立本机凭据文件，不写入模型目录或数据库；模型目录/密钥更新 API 与凭据读取 API 仅接受 loopback 请求，读取接口返回 `no-store`。
3. 在界面中设置每种模态的默认模型。已被项目使用的文本模型不能删除或改成其他模态。`GET /api/config` 只返回凭据状态，不返回密钥值。Base URL 仅允许 HTTPS；本机服务可使用 HTTP。

`openai_compatible` 适配器用于兼容 Chat Completions 和工具调用的文本服务；`anthropic` 使用 Anthropic SDK；图像支持 `agnes_image` / `openai_images`；视频支持 `agnes_video`；音频支持 `openai_audio`。新厂商若协议不同，新增适配器实现并在工厂中注册即可，不需要重写工作流或聊天界面。

## API

- `GET /api/config`：可用模型、模态、默认 profile、参数和凭据状态（不含密钥）
- `PUT /api/config/models`：仅 loopback 可调用；校验、持久化并热更新模型目录，API Key（若提交）单独写入本机凭据文件
- `GET /api/config/models/{profile_id}/credential`：仅 loopback 可读，供本机配置编辑器回显 API Key
- `GET /api/projects`、`POST /api/projects`：项目列表和创作流程项目创建
- `PUT /api/projects/{id}/title`：重命名对话或项目
- `POST /api/projects/{id}/archive`：从最近对话移除；保留项目及其记录和媒体
- `POST /api/chat/sessions`：创建无需 brief 的普通对话会话（首次发送消息后更新会话标题）
- `GET /api/projects/{id}/chat`、`POST /api/projects/{id}/chat`：兼容读取/发送持续对话消息
- `GET /api/projects/{id}/chat/history?limit=10&before={message_id}`：按游标分页读取记录；工作台初次只加载最近 10 条，需要时再向上加载更早消息
- `POST /api/projects/{id}/chat/attachments`：上传对话附件，返回可关联到消息的附件 ID
- `POST /api/projects/{id}/chat/stream`：发送文字和附件 ID，并通过 SSE 流式返回生成状态、文本增量和完成事件
- `POST /api/projects/{id}/generate/{stage}`：生成当前阶段草稿（逐镜模式的视频阶段不支持整段生成）
- `PUT /api/projects/{id}/content/{stage}`、`POST /api/projects/{id}/approve/{stage}`：修改和确认当前草稿
- `POST /api/projects/{id}/rollback/{stage}`：回退到已确认的前序步骤；保留后续产物并标记为过期
- `POST /api/projects/{id}/shots/{shot_id}/image`、`POST /api/projects/{id}/shots/{shot_id}/video`：按镜头生成/重试关键帧或视频片段；持久化视频任务入队后返回 HTTP 202，使用 `GET /api/projects/{id}` 查看状态
- `POST /api/projects/{id}/shots/{shot_id}/video/cancel`：只取消仍在本地队列、尚未提交供应商的任务
- `POST /api/projects/{id}/shots/{shot_id}/video/resume`：恢复超时或暂停任务的状态查询，不会重新提交生成
- `GET /api/usage`：查看本机保存的调用、token 和供应商已报告费用
- `GET /api/storage/cleanup/preview`、`POST /api/storage/cleanup`：预览并在明确确认后清理未引用的附件和媒体
- `POST /api/audio/transcriptions`：multipart 音频转写（默认使用配置的转写 profile）
- `GET /health`：健康检查

OpenAPI 页面：<http://127.0.0.1:8000/docs>。

## 用量、限额与存储清理

`MEDIA_GENERATION_DAILY_LIMIT` 控制本地 UTC 日的媒体生成调用上限，默认 `0` 表示不限；限制和统计基于本地 SQLite 记录及当前单进程服务，不是供应商账单或多实例并发配额。`VIDEO_JOB_TIMEOUT_SECONDS` 默认 `300` 秒，最小 `60` 秒。可在 `.env` 中设置后重启服务。

视频任务超时或连续查询失败后会保留供应商任务 ID；“恢复查询”只继续轮询，不会再次创建任务。任务尚在本地队列且未提交时才可取消；本地取消不等于供应商取消。若提交结果不确定，先到供应商侧核对任务，再明确确认是否重试。用量界面只汇总供应商明确返回的费用，未返回的费用保持未知，不做估算。

存储清理先展示候选附件和媒体文件，只有明确确认后才删除；删除前会再次检查引用关系，仍被项目或聊天使用的文件会保留。

## Skills

在 `skills/<skill-id>/SKILL.md` 新增或修改 Markdown 即可；按创作类型与阶段加载，不执行文件中的代码。frontmatter 的 `tracks` 限定适用路线，`references` 显式内联同目录下的 Markdown 参考资料；缺少 `tracks` 的旧技能默认仅适用于营销路线。短剧 / 漫剧 Skills 是从经过审阅的上游 Markdown 中选择性适配，不调用上游脚本或媒体 adapter。第三方技能的来源、版本、适配和许可证记录见 [`docs/skill-sources.md`](docs/skill-sources.md)。

## 验证

```bash
.venv/bin/python -m pytest -q
node --check app/static/app.js
```

自动化测试覆盖阶段确认、无凭据时不伪造结果、聊天附件传递与媒体生成二次确认、最近对话重命名/移除，以及视频任务提交/轮询/恢复。配置显示“已就绪”仅表示本地存在凭据，不代表供应商鉴权或生成成功。Agnes 视频服务此前返回过队列满（HTTP 503、`video_queue_full`），未返回可轮询的供应商任务 ID，因此实际任务完成流程尚未验证。真实媒体请求可能产生费用；自动化测试注入模拟 Provider，不调用真实供应商。
