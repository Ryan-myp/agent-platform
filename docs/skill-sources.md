# 开源 Skills 参考来源

调研日期：2026-11-09。平台按阶段读取 `skills/<skill-id>/SKILL.md`，仅内联技能 frontmatter 中明确列出的 Markdown `references`；不执行技能目录中的代码，也不在运行时自动从互联网下载代码。

## 候选评估

- [coreyhaines31/marketingskills — video](https://github.com/coreyhaines31/marketingskills/tree/main/skills/video)：调研时 skills.sh 页面显示约 93.9K 次安装；上游仓库采用 MIT License。技能涵盖 AI 视频提示词、制作流程、产品演示和剪辑思路，适合作为本平台分镜与逐镜素材提示的补充。
- [social-media-skills/skills — short-form-video-script](https://github.com/social-media-skills/skills/tree/main/skills/short-form-video-script)：调研时 skills.sh 页面显示约 1.6K 次安装；仓库采用 MIT License。它更集中于短视频脚本和 WATCH 框架，且其工作流会引用其他社媒技能；当前项目已有短视频脚本技能，因此本次优先安装覆盖面更广的视频制作技能。
- [SupercmoHQ/superCMO-skills](https://github.com/SupercmoHQ/superCMO-skills)：README 介绍营销视频、图片与 campaign 工作流，仓库声明 Apache-2.0；其媒体执行能力依赖具体外部服务和凭据。
- [liyupi/ai-promo-video-maker](https://github.com/liyupi/ai-promo-video-maker)：可作为后续媒体生成/成片链路的候选参考；本 MVP 未复制其代码。

安装量是调研时第三方目录页面显示的近似值，不代表当前实时数据或质量保证。

## 已安装的上游技能

- 来源：`coreyhaines31/marketingskills/skills/video/SKILL.md`，上游版本 `2.4.0`。
- 固定来源提交：[`96798cf38736ecdba015a1d2b0521b0c8f8f41dd`](https://github.com/coreyhaines31/marketingskills/commit/96798cf38736ecdba015a1d2b0521b0c8f8f41dd)，提交日期 2026-11-06。
- 项目路径：[`skills/video/`](../skills/video/)。包含上游技能正文、Markdown 参考资料和上游 `LICENSE`；没有复制或执行上游脚本。
- 上游技能适用于 `prompt`、`storyboard`、`scenes` 阶段。本地增加了平台兼容约束，并通过 `references: ai-video-prompting` 让 `SkillLoader` 仅在这些阶段内联视频提示词参考。其他 Markdown 参考资料保留供人工查阅，不自动塞入模型上下文。
- 上游关于模型能力、工具可用性及价格的描述可能随时间变化；本平台以已配置 Provider 的实际能力为准。当前应用仍只交付逐镜片段与 AI 剪辑方案，不自动合并 MP4。
- MIT 版权和许可声明保存在 [`skills/video/LICENSE`](../skills/video/LICENSE)。

## 短剧 / 漫剧 Skills（选择性适配）

- 来源仓库：[`zenstory-ai/drama-skills`](https://github.com/zenstory-ai/drama-skills)，审阅时固定提交 [`3d813c30871b4db5f2387d83bdc3a49db8ce7b2b`](https://github.com/zenstory-ai/drama-skills/commit/3d813c30871b4db5f2387d83bdc3a49db8ce7b2b)，上游仓库声明 MIT License。
- 适配目录：`skills/drama-script/`、`skills/drama-visual/`、`skills/drama-storyboard/`、`skills/drama-scene-prompts/`。按 `tracks: drama` 与工作流阶段隔离；普通营销项目不会加载这些指令，短剧 / 漫剧项目也不会加载营销 Skills。
- 只选取并改写剧本、视觉连续性、分镜和逐镜动作提示方法，按需内联少量 Markdown 参考资料；保留上游 MIT 许可声明。没有复制或运行上游 Python 工具、生产脚本、剪辑工具或 Provider adapter，也不调用上游供应商。
- 上游原工作流面向仓库内多份 Markdown 文件和外部 adapter。本平台将输出适配到现有阶段草稿与 `scenes` 的结构化 `shots` JSON；媒体生成仍经平台现有 Provider，并由用户明确触发。本阶段不自动合并 MP4。
- 这次接入是对已审阅上游的选择性平台适配，不代表恢复或确认用户早先提过但当前上下文缺失的其他候选链接。

## 项目自编技能与维护方式

`brand-brief`、`model-first-video`、`scene-image-prompts`、`short-form-video`、`storyboard` 是为本项目编写的本地技能。新增或更新第三方技能时，应复核上游内容、许可证、固定版本和时效性；保留要求的版权声明，并记录本地适配。所有技能都只作为生成提示上下文，不执行其中代码。普通聊天目前不自动加载这些工作流技能。
