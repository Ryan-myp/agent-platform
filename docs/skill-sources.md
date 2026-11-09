# 开源 Skills 参考来源

调研日期：2026-06-18。Skills 按本地 `skills/<skill-id>/SKILL.md` 加载，不执行其目录中的脚本，也不在运行时自动从互联网下载代码。

## 参考项目

- [social-media-skills/skills](https://github.com/social-media-skills/skills)：README 介绍了覆盖社媒策略、视频脚本等主题的技能集；仓库提供的 `short-form-video-script` 技能强调短视频开场、节奏、静音观看和兑现承诺。README 指明 MIT License。
- [SupercmoHQ/superCMO-skills](https://github.com/SupercmoHQ/superCMO-skills)：README 介绍营销视频、图片与 campaign 工作流，仓库声明 Apache-2.0。其模型/媒体执行能力依赖具体的外部服务与凭据。
- [liyupi/ai-promo-video-maker](https://github.com/liyupi/ai-promo-video-maker)：GitHub 搜索中发现的开源 AI 营销视频项目，可作为后续媒体生成/成片链路的候选参考；本 MVP 未复制其代码。

## 当前处理方式

当前内置的四个 skill 文件是为本项目重新编写的工作约束，没有复制上游 skill 正文或代码。短视频脚本技能吸收了公开资料中“开场、节奏、静音可理解、诚实兑现”的通用做法。添加第三方原文或代码前，需逐项核对上游文件、当前许可证和归属要求，并在对应 skill 的 frontmatter 或本文件记录来源与版本。

安装/更新只需把兼容 Agent Skills 结构的 `SKILL.md` 放到 `skills/<skill-id>/`；本平台每次生成时读取对应阶段的技能正文。建议人工审阅外部技能，尤其检查未经验证的数据、平台时效性、工具执行说明与提示词注入风险。
