# 架构说明

## 目标

支持品牌营销视频与短剧 / 漫剧两种结构化创作路线，同时提供无需 brief 的普通聊天。左侧“新对话”和“创作流程”是两个独立入口：前者用于自由讨论和按需媒体生成，后者先选择路线，再按阶段生成、审阅并确认脚本、视觉设定、分镜和逐镜任务。两条路线共享项目状态机、Provider 和媒体生产能力，提示词与 Skills 按创作类型隔离。

## 模块

- `app/services/workflow.py`：项目与阶段状态机；草稿可编辑，只有确认当前步骤后才进入下一步。明确标注信息不足或无法继续的草稿不能确认，下游生成会预检已确认内容，并在发现阻塞说明时停止且不调用模型。创建时按 `mode=chat|workflow` 区分普通会话和结构化流程。
- `app/services/chat.py`：会话上下文整理和工具调用循环。普通对话无预设 brief，工作流对话包含项目 brief 和已确认阶段；对话保存在 SQLite，媒体工具仅在用户明确提出生成要求时提供给文本模型。
- `app/stages.py`：`script → prompt → storyboard → scenes → video` 的阶段顺序、公共格式要求和按创作类型区分的提示词要求。
- `config/models.json`、`app/providers/catalog.py`：按 text/image/video/audio 声明模型 ID、厂商适配器、端点、参数、兼容用 API Key 环境变量名和模态默认值；`PUT /api/config/models` 校验、原子持久化并热更新模型目录，公共配置接口不返回 Key。
- `app/providers/credentials.py`：将 API Key 独立保存到数据库同目录的 `model-credentials.json`，使用临时文件原子替换并尽量设置 `0600` 权限；凭据不进入模型目录或项目数据库。
- `app/providers/text.py`：统一文本 Provider 协议、OpenAI-compatible Chat Completions / 工具调用适配器及 Anthropic Claude SDK 适配器。
- `app/providers/media.py`：图像、视频、音频 Provider 和本地媒体资源保存。当前包括 Agnes 图像、Agnes 视频异步创建/轮询以及 OpenAI-compatible 语音/转写适配器。
- `app/skills/loader.py`：扫描本地 `skills/<skill-id>/SKILL.md`，按创作类型与阶段加载 Markdown，并仅内联 frontmatter 显式列出的同目录参考文件；缺少 `tracks` 的旧 Skills 仅按营销路线加载；不执行 Skill 文件代码。
- `app/store.py`：SQLite 项目和聊天消息持久化。后续可替换为 Postgres 实现。
- `app/static/`：无构建步骤的工作台 UI，含独立的“新对话”和“创作流程”入口、原生 `<dialog>` 模型配置管理、逐步确认界面。聊天发送采用乐观用户消息和 SSE 文本增量更新；助手回复和工作流只读内容安全渲染 Markdown，用户消息保持纯文本。

## 对话和媒体流程

1. 新对话首次发送消息前由前端调用 `POST /api/chat/sessions` 创建 `mode=chat` 会话；它不要求 brief，首条用户消息会成为会话标题。创作流程由 `POST /api/projects` 创建 `mode=workflow` 项目。随后两种模式都通过 `POST /api/projects/{id}/chat/stream` 发送消息：用户消息先在界面显示，服务通过 SSE 返回状态和文本增量；普通聊天使用不含 brief 的上下文，工作流会话使用项目 brief 与已确认阶段内容。兼容的 `POST /api/projects/{id}/chat` JSON 接口仍保留。
2. 文本模型可调用图片、视频、语音生成工具；工具结果以附件形式保存并显示在对话里。没有 Provider 成功结果时，不得声称媒体已生成。
3. 图像和音频请求当前在聊天 HTTP 请求中同步完成。Agnes 视频接口会创建供应商异步任务并轮询，但轮询仍在同一个 HTTP 请求中；尚未接入独立任务队列或前端任务状态轮询。
4. Agnes Video 2.5 Flash 单次支持 4–12 秒；当前没有多段视频拼接。超出单次范围的项目会收到错误，不会被伪装成完整成片。
5. `scenes` 阶段目前产出逐镜图片提示词，不自动生成场景图片。需要扩展时，可增加 scene-level image job，并将确认过的画面作为视频参考输入。

## 模式与凭据

普通会话和工作流共享现有项目存储，通过项目 payload 的 `mode` 区分，因此不需要新增 SQLite 表；新对话采用延迟创建，打开空白聊天不会提前写入项目。API Key 可在本机模型配置 UI 填写并显示，单独保存在数据库同目录下的 `model-credentials.json`；保存时尽量设置 `0600` 权限，模型目录/密钥更新与密钥读取 API 仅接受 loopback 请求，读取接口禁用缓存。环境变量仍作为兼容凭据来源。模型目录修改会原子写入 JSON 并立即更新工作流和聊天服务；密钥不会进入模型目录、项目数据库或公共配置接口；已有项目正在使用的文本模型不可删除。应用不包含运行时演示模式；真实模式缺少 Key 时返回配置错误，不回退到伪造结果。自动化测试通过注入隔离的伪 Provider，不会访问线上服务。

## 扩展方向

1. 对接新文本、图像、视频或音频供应商：实现相应 Provider 并在工厂注册，再通过工作台或 `config/models.json` 添加 profile。
2. 媒体任务独立化：加入后台队列、任务状态/取消/重试接口和前端轮询，避免视频轮询占用聊天请求。
3. 为短剧 / 漫剧路线完善资产、漫剧画风和剧集 / 分集管理；将结构化镜头任务逐镜生成图片并支持素材替换、版本和引用关系。
4. 用明确的剪辑/音轨工作流组合多段视频，处理时长、转场、字幕和输出编码。
5. 生产化前补充身份认证、租户隔离、对象存储、审计、额度与成本控制、上传限制和并发保护。
