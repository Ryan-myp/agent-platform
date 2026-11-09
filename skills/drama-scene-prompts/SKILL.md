---
name: drama-scene-prompts
description: 将确认的短剧 / 漫剧分镜和视觉设定转成平台可校验的逐镜图像、视频任务 JSON。
stages: scenes
tracks: drama
references: motion-recipe,production-prompt-grammar
license: MIT
---

# 短剧 / 漫剧逐镜任务

此阶段只输出一个 JSON 对象，不要 Markdown 代码围栏、注释或额外说明。严格遵循平台 `scenes` 输出结构：

```json
{"shots":[{"shot_id":"S01","duration_seconds":5,"aspect_ratio":"9:16","purpose":"","visual_description":"","voiceover":"","screen_text":"","transition":"","continuity":"","image_prompt":"English image prompt","video_prompt":"English video motion prompt","negative_prompt":""}],"edit_plan":{"sequence":["S01"],"pacing":"","transitions":"","music_direction":"","sound_design":"","subtitle_direction":"","notes":""}}
```

- 只使用已确认的脚本、视觉设定和分镜；`shot_id` 与分镜编号逐一对应，不能新增、遗漏或重复镜头。
- 每镜时长必须是 4–12 的整数秒；画幅使用已确认设定，缺失时用 brief 的平台方向并标明假设。
- `purpose`、`visual_description`、`voiceover`、`screen_text`、`transition`、`continuity` 使用中文。`image_prompt` 和 `video_prompt` 使用清楚、可执行的英文；不要依赖“cinematic”“beautiful”等空泛词代替主体、动作、构图、机位、光线与连续性描述。
- 图片提示词描述本镜可审阅的静态关键帧；视频提示词只描述本镜连续动作，从已确认起点走到终点，避免无关动作、瞬移、多场景拼接和无法生成的精细动作。
- 跨镜角色、造型、场景、道具和状态应沿用视觉设定中的锚点；提示词不得补造标识、可读文字、剧情事实或不存在的参考图。
- `edit_plan` 仅描述镜头次序、节奏、转场、字幕和声音方向。明确这只是剪辑方案；本平台不自动拼接 MP4，也不能声称已生成素材。
- 仅当真实媒体 Provider 成功返回后，才能称镜头素材已生成；本阶段没有调用媒体服务。
