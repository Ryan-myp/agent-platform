from __future__ import annotations

STAGES: tuple[dict[str, str], ...] = (
    {"id": "script", "label": "脚本", "purpose": "把创作目标转成可审阅、可制作的脚本"},
    {"id": "prompt", "label": "视觉设定", "purpose": "确认品牌、角色、场景与整体视觉锚点"},
    {"id": "storyboard", "label": "分镜", "purpose": "把脚本拆成有节奏且保持连续性的镜头"},
    {"id": "scenes", "label": "镜头任务", "purpose": "逐镜审阅图片提示词、视频提示词与 AI 剪辑方案"},
    {"id": "video", "label": "视频片段", "purpose": "按镜头调用模型生成关键帧和视频片段，不在本地拼接"},
)

STAGE_IDS = tuple(stage["id"] for stage in STAGES)

TRACK_LABELS = {
    "marketing": "品牌营销视频",
    "drama": "短剧 / 漫剧",
}

TRACK_INSTRUCTIONS = {
    "marketing": "遵守品牌和营销内容创作要求。",
    "drama": (
        "按短剧 / 漫剧路线创作：优先让人物目标、阻力、选择、可见行动和后果推动情节；"
        "不得把脚本改写成产品广告，不强加品牌卖点或 CTA。保持人物、造型、场景、道具和状态连续。"
    ),
}

INSTRUCTIONS = {
    "script": "写一份中文短视频脚本。包含：核心卖点、3秒内的开场钩子、逐拍内容（画面/口播/屏幕文字/时长）、兑现钩子的结尾和明确 CTA。不要编造产品功效、数据或用户证言；根据目标平台和时长调整节奏。",
    "prompt": "整理可复用的项目视觉设定（visual bible），包括产品外观与不可变事实、角色/服装、场景、道具、色彩与光线、镜头质感、画幅及禁止项。描述可被逐镜提示词引用的连续性锚点；不确定的外观细节需标记待确认，不得臆造品牌标识、产品功能或真实证据。",
    "storyboard": "将已确认脚本拆成逐镜分镜。每镜给出稳定镜头编号、4–12 秒建议时长、景别/机位、主体动作、口播、屏幕文字、转场、视觉连续性约束。镜头数量和时长要接近 brief 目标；视频模型单镜限 4–12 秒，长视频应拆成多个独立镜头任务，不要假设会自动拼接。",
    "scenes": "根据已确认分镜和视觉设定，为每镜生成可独立调用图像/视频模型的任务，并生成 AI 剪辑方案。只输出一个 JSON 对象，不要 Markdown 代码围栏或额外说明。结构：{\"shots\":[{\"shot_id\":\"S01\",\"duration_seconds\":5,\"aspect_ratio\":\"9:16\",\"purpose\":\"\",\"visual_description\":\"\",\"voiceover\":\"\",\"screen_text\":\"\",\"transition\":\"\",\"continuity\":\"\",\"image_prompt\":\"英文图片提示词\",\"video_prompt\":\"英文视频动作提示词\",\"negative_prompt\":\"\"}],\"edit_plan\":{\"sequence\":[\"S01\"],\"pacing\":\"\",\"transitions\":\"\",\"music_direction\":\"\",\"sound_design\":\"\",\"subtitle_direction\":\"\",\"notes\":\"\"}}。每个镜头时长必须为整数且 4–12 秒；image_prompt 与 video_prompt 必须具体可执行；edit_plan 仅描述镜头顺序、字幕、声音与节奏，不声称已经完成拼接或生成素材。",
}

DRAMA_INSTRUCTIONS = {
    "script": (
        "写一份可表演的中文短剧 / 漫剧脚本。围绕人物目标、阻力、选择、可见行动和后果组织场景；"
        "建立开场钩子与清楚的因果转折，写出必要对白、动作、声音和场景退出状态。"
        "不要虚构用户未提供的事实，不强加产品卖点、广告口播或 CTA。"
    ),
    "prompt": (
        "根据已确认剧本整理可复用的短剧 / 漫剧视觉设定：人物身份与造型变体、地点锚点、道具和状态、"
        "色彩 / 光线 / 媒介风格，以及有生效范围的跨镜连续性锁。镜头瞬态留给分镜；不确定的视觉事实标为待确认。"
    ),
    "storyboard": (
        "将已确认脚本拆成逐镜分镜，沿用稳定 S01、S02…编号。每镜说明剧情职责、来源、4–12 秒整数时长、"
        "景别 / 机位、主体动作、起点和终点、声音及连续性。每镜聚焦一个主要状态变化，时长合计尽量接近 brief；"
        "长内容拆成独立镜头任务，不假设会自动拼接。"
    ),
    "scenes": INSTRUCTIONS["scenes"],
}

TRACK_STAGE_INSTRUCTIONS = {
    "marketing": INSTRUCTIONS,
    "drama": DRAMA_INSTRUCTIONS,
}
