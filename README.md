# 片场 · 多模态内容创作台

支持品牌营销视频与短剧 / 漫剧两条创作路线的本地内容创作平台。左侧提供「新对话」「创作流程」和「制作记录」入口：普通对话支持持续讨论、图片/视频/文件附件理解，并可生成图片、短视频或语音；涉及媒体生成时，助手会先说明生成内容并等待用户确认。创作流程选择内容类型并填写 brief，再按脚本、视觉设定、分镜、逐镜任务和视频片段逐步确认。工作流草稿可编辑，未经确认不会推进；已确认步骤也可回退修改或重新生成。上游修改后，后续内容与媒体素材会保留并标记为过期，重新确认后可逐步重生成；被覆盖的文本草稿可从历史版本恢复。确认镜头任务后，可逐镜生成/重试关键帧和视频片段，并查看 AI 剪辑方案、素材版本及供应商实际返回的任务/费用信息。制作记录复用项目数据保存脚本、分镜、镜头任务与素材，并可下载面向团队交接的人读制作交付包。长篇作品可维护故事圣经并逐章确认；短剧分集可先写并确认可选大纲，镜头可关联系列资产作为文字连续性参考。已确认场景中的就绪本地视频片段可按剪辑顺序使用 FFmpeg 合成为单个 MP4；供应商托管的视频 URL 不复制到本地，URL 失效后可能无法回放。当前有效的最终 MP4 可随交付包提供，但仍需人工审核。

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

注意：Agnes Video 2.5 Flash 单次视频时长为 4–12 秒；工作流将长内容拆成多个逐镜任务，每镜独立生成和重试。平台不会调用视频供应商拼接多个任务；已确认场景中的就绪本地片段可由本机 FFmpeg 合成，AI 剪辑方案本身仍不是成片。视频 URL 仍由供应商托管，若 URL 失效，当前记录页无法回放。镜头任务校验时长、画幅和提示词，并按素材版本保存供应商实际返回的数据；若供应商不返回费用，界面会显示未知，不作估算。Agnes 没有在当前配置中假定独立音频生成模型；语音生成/转写示例用 OpenAI-compatible 音频适配器。

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
- `GET/POST /api/series`、`GET/PUT /api/series/{series_id}`：剧集系列列表、创建与更新
- `POST /api/series/{series_id}/assets`、`PUT /api/series/{series_id}/assets/{asset_id}`：创建或更新人物、场景与道具资产
- `POST /api/series/{series_id}/assets/{asset_id}/media`：在本机同源工作台上传 PNG / JPEG / WebP 参考图（单张最大 20 MiB，每项最多 8 张）
- `POST /api/series/{series_id}/continuity`、`PUT /api/series/{series_id}/continuity/{entry_id}`：新增或修改带来源记录的连续性台账条目
- `GET /api/series/{series_id}/export`、`POST /api/series/import`：在本机同源工作台导出或导入完整系列副本（系列、资产、分集、聊天与引用媒体）
- `GET /api/projects/{id}/export`、`POST /api/projects/import`：在本机同源工作台导出或导入单项目副本（不覆盖已有项目）
- `GET /api/projects/{id}/delivery`：从本机同源工作台下载人读制作交付包（不用于恢复或导入）
- `GET/POST /api/novels`、`GET/PUT /api/novels/{novel_id}`：管理长篇作品和故事圣经
- `POST /api/novels/{novel_id}/chapters`、`PUT /api/novels/{novel_id}/chapters/{chapter_id}`：创建和修改章节；另有 `.../generate` 与 `.../approve` 操作
- `PUT /api/projects/{id}/episode-outline`、`POST /api/projects/{id}/episode-outline/generate`：保存或生成分集大纲
- `PUT /api/projects/{id}/shots/{shot_id}/assets`、`PUT /api/projects/{id}/render-order`：设置镜头资产关联与最终合成顺序
- `PUT /api/projects/{id}/assembly-settings`：设置合成画幅、补边 / 裁切模式和文件预设
- `GET /api/projects/{id}/subtitles.srt`、`GET /api/projects/{id}/subtitles.ass`：从已确认的镜头旁白和屏幕文字下载字幕
- `POST /api/projects/{id}/assemble`：仅在本机合成已确认场景中的就绪本地视频片段，并按需烧录字幕
- `PUT /api/projects/{id}/title`：重命名对话或项目
- `POST /api/projects/{id}/archive`：从最近对话移除；保留项目及其记录和媒体
- `POST /api/chat/sessions`：创建无需 brief 的普通对话会话（首次发送消息后更新会话标题）
- `GET /api/projects/{id}/chat`、`POST /api/projects/{id}/chat`：兼容读取/发送持续对话消息
- `GET /api/projects/{id}/chat/history?limit=10&before={message_id}`：按游标分页读取记录；工作台初次只加载最近 10 条，需要时再向上加载更早消息
- `POST /api/projects/{id}/chat/attachments`：上传对话附件，返回可关联到消息的附件 ID
- `POST /api/projects/{id}/chat/stream`：发送文字和附件 ID，并通过 SSE 流式返回生成状态、文本增量和完成事件
- `POST /api/projects/{id}/generate/{stage}`：生成当前阶段草稿（逐镜模式的视频阶段不支持整段生成）
- `PUT /api/projects/{id}/content/{stage}`、`POST /api/projects/{id}/approve/{stage}`：修改和确认当前草稿
- `GET /api/projects/{id}/content/{stage}/revisions`、`POST /api/projects/{id}/content/{stage}/revisions/{revision_id}/restore`：列出并恢复历史草稿；恢复时归档当前内容并将下游标为过期
- `POST /api/projects/{id}/quality/{stage}`：运行确定性启发式检查，返回评分和问题提示（不是内容质量保证）
- `POST /api/projects/{id}/rollback/{stage}`：回退到已确认的前序步骤；保留后续产物并标记为过期
- `POST /api/projects/{id}/shots/{shot_id}/image`、`POST /api/projects/{id}/shots/{shot_id}/video`：按镜头生成/重试关键帧或视频片段；持久化视频任务入队后返回 HTTP 202，使用 `GET /api/projects/{id}` 查看状态
- `POST /api/projects/{id}/shots/{shot_id}/video/cancel`：只取消仍在本地队列、尚未提交供应商的任务
- `POST /api/projects/{id}/shots/{shot_id}/video/resume`：恢复超时或暂停任务的状态查询，不会重新提交生成
- `GET /api/video-jobs`：查看本机需关注的视频队列/失败/中断任务和 Worker 状态
- `GET /api/system/status`：查看应用/API 契约版本、功能标记和后台 Worker 状态
- `GET /api/usage`：查看本机保存的调用、token 和供应商已报告费用
- `GET /api/backup/export`：从本机同源工作台下载数据库与本地媒体备份
- `GET /api/storage/cleanup/preview`、`POST /api/storage/cleanup`：预览并在明确确认后清理未引用的附件和媒体
- `POST /api/audio/transcriptions`：multipart 音频转写（默认使用配置的转写 profile）
- `GET /health`：健康检查

OpenAPI 页面：<http://127.0.0.1:8000/docs>。

## 剧集故事圣经与连续性台账

左侧“剧集资产”可维护系列梗概、题材、世界设定、核心主题、季 / 主线弧光、主要人物弧光、计划结局、叙事基调、内容边界、视觉基调和备注，以及人物、场景和道具设定。系列与资产采用归档而非永久删除；已归档系列仍可查看已有分集，但不能继续新增分集。只有短剧 / 漫剧创作流程可以关联系列；创建分集时可指定集数，不指定则由 SQLite 事务原子分配该系列的下一个集数，并拒绝重复编号。

连续性台账支持既定事实、已发生事件、关系变化、线索、未解线索和创作约束，并可标为已确认、待解决、已解决或已归档。台账可手动记录，也可引用分集中的已确认步骤；引用分集时，所选原文节选必须与该已确认步骤内容逐字匹配。修改记录会保留最多 12 个历史版本。系列页面可查看来源和修改历史，便于团队审阅而不是把模型生成结果直接当作既定事实。

生成时，系列设定和最多 20 项未归档资产作为有界参考上下文传入工作流。连续性上下文最多包含 30 条、总计 8,000 字符：已确认条目作为故事事实，待解决条目作为待保留线索；已解决或归档条目不会继续传入，来源于当前或未来分集的条目也会排除。前序分集脚本只会选取编号最近的两集，且仅在脚本已确认后纳入（最多 4,000 字符）；未确认草稿不会泄漏到后续分集。系列设定、用户 brief 和剧本都作为参考资料，不覆盖系统规则或充当操作指令。系列资产可附加最多 8 张本地 PNG / JPEG / WebP 参考图（单张最大 20 MiB）；图片只保存在本机、随系列副本导出，不会自动上传到模型供应商。`GET /api/system/status` 的 API 契约版本 5 会报告字幕、合成预设、系列参考图和质量检查等功能标记。

## 长篇、分集大纲与本地成片

“长篇作品”独立维护作品故事圣经和章节。章节生成最多参考编号较小、作者已确认的两章正文；修改已确认章节正文会归档旧版，并把后续已有章节标记为待复核，待复核文本不会自动成为后续生成事实。生成大纲或正文需要用户主动确认调用所选文本模型，可能产生费用；保存、载入历史版本不会自动生成或确认。

短剧工作流可为分集保存大纲草稿；只有明确确认的大纲才进入剧本生成参考。镜头可以链接系列人物、场景或道具资产作为文字参考，不会自动上传资产文件；更改关联会使已有镜头媒体过期。

本地合成只使用场景已确认、视频已就绪且来源为本地媒体的片段，并遵循剪辑顺序。可按项目选择 9:16、16:9、方形等画幅、补边适配或裁切填满，以及紧凑 / 标准 / 高质量预设；FFmpeg 从 PATH 或 `imageio-ffmpeg` 查找。最多 24 段、输入总计 512 MiB、总时长 30 分钟，输出也有大小上限。已确认镜头任务中的旁白和屏幕文字可下载为 SRT / ASS，也可在合成时烧录。该流程不调用媒体供应商，会校验合成结果并防止并发编辑覆盖；交付包仅收录当前有效且通过大小和 SHA-256 校验的最终 MP4。最终视频仍需人工播放审核。

导入单项目副本时会复制系列设定和资产，但不会带入其他分集。系列详情页可导出整个系列 ZIP，也可在系列列表导入为新副本；系列导入会复制所有分集、对话、附件元数据和引用的本地媒体，重新分配记录 ID，不覆盖已有系列，也不包含凭据或用量记录。导入任务不会恢复供应商任务，在途任务会标记为中断。包大小上限为 512 MiB，导入仅允许本机同源请求。

## 草稿修订、质量检查与服务诊断

创作阶段的草稿修改和重新生成会保留历史版本。编辑器中的“载入编辑区”只把旧稿填入编辑器，保存后才替换当前内容；“恢复为当前版本”会先确认、归档现有内容，并将下游阶段标记为过期。质量检查为本地确定性启发式规则，提示占位文本、阻塞语言、重复行、分镜结构及目标时长等问题；短剧脚本还会扫描开场钩子、人物目标、阻碍、风险和转折的常见文字信号，不调用模型。信号未检出不代表缺失，检出也不代表有效，不能替代人工审核。

页面启动时读取 `/api/system/status`，比对 API 契约和所需功能；诊断区显示页面资源版本、服务端契约、实例 ID、启动时间、Python 版本和视频 Worker 状态。若出现旧服务、缺少路由或视频 Worker 未运行，会提示重启服务，并提供重新加载页面按钮。此诊断仅报告本机服务状态，不提供身份认证或公网访问能力。用量对话框还可单独查看需关注的视频任务及其更新时间/错误。

## 用量、限额与存储清理

`MEDIA_GENERATION_DAILY_LIMIT` 控制本地 UTC 日的媒体生成调用上限，默认 `0` 表示不限；限制和统计基于本地 SQLite 记录及当前单进程服务，不是供应商账单或多实例并发配额。`VIDEO_JOB_TIMEOUT_SECONDS` 默认 `300` 秒，最小 `60` 秒。可在 `.env` 中设置后重启服务。

视频任务超时或连续查询失败后会保留供应商任务 ID；“恢复查询”只继续轮询，不会再次创建任务。任务尚在本地队列且未提交时才可取消；本地取消不等于供应商取消。若提交结果不确定，先到供应商侧核对任务，再明确确认是否重试。用量界面只汇总供应商明确返回的费用，未返回的费用保持未知，不做估算。

存储清理先展示候选附件和媒体文件，只有明确确认后才删除；删除前会再次检查引用关系，仍被项目或聊天使用的文件会保留。

## 备份、恢复与导出

可在工作台“用量与存储”中下载完整 ZIP 备份，也可用 CLI 导出 SQLite 数据库快照和本地 `media/` 文件。在线导出接口仅允许 loopback 客户端的同源请求。备份带 SHA-256 清单；不覆盖已有备份文件，也不包含 `model-credentials.json`、`.env` 或外部模型 Key。恢复会校验 ZIP 路径、清单、哈希和 SQLite 完整性，并且只允许写入一个尚不存在的新目录，不覆盖当前数据。

```bash
# 创建带 UTC 时间戳的备份，默认写入 backups/；数据库默认为 DATABASE_PATH 或 data/agent-platform.db
.venv/bin/python -m app.backup create --keep 7

# 只校验备份清单、SHA-256 和 SQLite 完整性，不改动应用数据
.venv/bin/python -m app.backup verify backups/agent-platform-backup-<timestamp>.zip

# 先预览将清理哪些旧备份；只有添加 --confirm 才会删除
.venv/bin/python -m app.backup prune --directory backups --keep 7
.venv/bin/python -m app.backup prune --directory backups --keep 7 --confirm

# 恢复到新目录（默认解压上限 20 GiB）
.venv/bin/python -m app.backup restore backups/agent-platform-backup-<timestamp>.zip --target data-restored

# 需要时，以恢复后的数据库启动服务；媒体目录自动取其同级 media/
DATABASE_PATH=data-restored/agent-platform.db .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

`create --keep N` 会在新备份成功后，仅轮换清理本工具生成的时间戳 ZIP；不带 `--keep` 时不会删除旧备份。`prune` 默认只预览。备份不包含模型凭据和模型目录自定义项；恢复后需重新配置 Key。大型媒体库可通过 `--max-restore-gb` 调整校验/恢复上限。数据库快照与媒体复制不是跨文件系统事务；若需确保二者处于同一时点，建议先停止服务，再使用 CLI 备份。定时频率和备份目录尚未替你设置；可在确定本机存储位置与时间后，将 `create --keep N` 配置到系统任务调度器。

## 项目导入与导出

在制作记录中打开一个项目，或在已有对话页面选择“导出项目副本”，可下载单项目 ZIP；在制作记录页选择“导入项目副本”可导入 ZIP。导入前会显示确认，现有项目不会被覆盖：项目、聊天记录、附件、关联系列资产和被引用的本地媒体都会复制并分配新 ID。系列只作为副本导入当前项目及其系列资产，不包含同系列其他分集；供应商凭据、全局用量记录和可恢复任务不会随包迁移，在途任务会被标记为中断，不会自动重新提交或轮询。

制作记录详情的“下载制作交付包”用于人工/小团队交接，不同于项目副本和完整备份。包内有脚本、视觉设定、分镜、逐镜任务、制作状态摘要、README、清单及项目引用的本地媒体；若存在当前有效且通过文件校验的本地最终 MP4，也会一并收录并记录大小、时长和 SHA-256。远程供应商链接不会下载，任务 ID、聊天记录、用量和凭据不作为交付内容。报告会明确标示最终视频状态；过期或校验失败的视频不会作为当前成片收录。成片仍需人工验收；仅允许 loopback 同源请求。

项目包与完整备份不同，不包含其他项目，也不用于恢复数据库。ZIP 包含创作内容和可能敏感的附件/媒体，只导入可信文件。服务会校验成员路径、大小和 SHA-256，并限制包大小为 512 MiB；导入仅允许从本机同源工作台发起。导出的 PDF、HTML、SVG、文本、JSON 等非媒体文件在 `/media` 下以附件下载，不作为同源可执行页面内联展示。相同源检查是本机使用限制和 CSRF 缓解，不是身份认证。

## Skills

在 `skills/<skill-id>/SKILL.md` 新增或修改 Markdown 即可；按创作类型与阶段加载，不执行文件中的代码。frontmatter 的 `tracks` 限定适用路线，`references` 显式内联同目录下的 Markdown 参考资料；缺少 `tracks` 的旧技能默认仅适用于营销路线。短剧 / 漫剧 Skills 是从经过审阅的上游 Markdown 中选择性适配，不调用上游脚本或媒体 adapter。第三方技能的来源、版本、适配和许可证记录见 [`docs/skill-sources.md`](docs/skill-sources.md)。

本机 API 对带 `Origin` 的写入请求实施同源检查，并拒绝标记为 `cross-site` 的浏览器写入；未带浏览器来源头的本机 CLI/自动化请求仍可用。这是 CSRF 防护，不等同于登录认证或多人权限隔离；服务仍只适合绑定本机回环地址。

## 验证

```bash
.venv/bin/python -m pytest -q
node --check app/static/app.js
```

自动化测试覆盖阶段确认、无凭据时不伪造结果、聊天附件传递与媒体生成二次确认、最近对话重命名/移除、视频任务提交/轮询/恢复/取消与用量限制、备份恢复、项目/系列副本迁移、草稿修订、叙事线索检查、字幕导出与烧录、合成画幅预设和本地资产图片上传。配置显示“已就绪”仅表示本地存在凭据，不代表供应商鉴权或生成成功。Agnes 视频服务此前返回过队列满（HTTP 503、`video_queue_full`），未返回可轮询的供应商任务 ID，因此实际任务完成流程尚未验证。真实媒体请求可能产生费用；自动化测试注入模拟 Provider，不调用真实供应商。
