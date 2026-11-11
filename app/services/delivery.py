from __future__ import annotations

import copy
import hashlib
import html
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from app.providers.media import AssetStore
from app.services.project_transfer import MAX_BUNDLE_BYTES, MAX_BUNDLE_MEMBERS, MAX_JSON_BYTES
from app.services.reviews import ReviewService
from app.services.storyboard_canvas import StoryboardCanvasError, StoryboardCanvasService
from app.services.video_assembly import VideoAssemblyError, VideoAssemblyService
from app.stages import STAGES
from app.store import ProjectStore


DELIVERY_FORMAT = "agent-platform-production-delivery"
DELIVERY_VERSION = 1
_STAGE_FILES = {
    "script": ("01-script.md", "脚本"),
    "prompt": ("02-visual-guide.md", "视觉设定"),
    "storyboard": ("03-storyboard.md", "分镜"),
}


class DeliveryPackageError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


class DeliveryService:
    """Create a human-readable, local-only production handoff ZIP."""

    def __init__(self, store: ProjectStore, assets: AssetStore):
        self.store = store
        self.assets = assets

    def create_package(self, project_id: str, output_path: Path) -> Path:
        project = self.store.get(project_id)
        if project is None:
            raise DeliveryPackageError("找不到要打包的创作项目。", status_code=404)
        raw_canvas = project.get("storyboard_canvas", {})
        if "storyboard_canvas" in project and project.get("mode") != "workflow":
            raise DeliveryPackageError("手工分镜画布仅适用于创作流程项目。")
        try:
            canvas = StoryboardCanvasService.validate_record(raw_canvas)
        except StoryboardCanvasError as error:
            raise DeliveryPackageError("项目中的手工分镜画布无效。") from error
        canvas_cards = canvas["cards"]
        workflow = project.get("workflow") if isinstance(project.get("workflow"), dict) else {}
        review_records = ReviewService(self.store).decorate(project).get("review_records", [])
        files: dict[str, bytes] = {}

        stage_report = []
        for stage in STAGES:
            stage_id = stage["id"]
            item = workflow.get(stage_id) if isinstance(workflow.get(stage_id), dict) else {}
            content = item.get("content")
            stage_report.append({
                "id": stage_id,
                "label": stage["label"],
                "status": str(item.get("status") or "pending"),
                "included": isinstance(content, str) and bool(content.strip()),
            })
            if stage_id in _STAGE_FILES:
                filename, label = _STAGE_FILES[stage_id]
                text = content.strip() if isinstance(content, str) and content.strip() else "当前步骤尚无内容。"
                header = f"# {label}\n\n> 当前状态：{self._status_label(item.get('status'))}\n\n"
                files[filename] = (header + text + "\n").encode("utf-8")

        scene_item = workflow.get("scenes") if isinstance(workflow.get("scenes"), dict) else {}
        scene_content = scene_item.get("content")
        if isinstance(scene_content, str) and scene_content.strip():
            try:
                decoded = json.loads(scene_content)
            except (json.JSONDecodeError, TypeError):
                files["04-shot-plan.md"] = ("# 镜头任务\n\n" + scene_content.strip() + "\n").encode("utf-8")
            else:
                files["04-shot-plan.json"] = self._json_bytes(decoded)
        else:
            files["04-shot-plan.md"] = "# 镜头任务\n\n当前步骤尚无内容。\n".encode("utf-8")

        video = workflow.get("video") if isinstance(workflow.get("video"), dict) else {}
        video_shots = video.get("shots") if isinstance(video.get("shots"), list) else []
        final_video = video.get("final_video") if isinstance(video.get("final_video"), dict) else {}
        try:
            final_is_current = (
                final_video.get("status") == "ready"
                and final_video.get("source_signature") == VideoAssemblyService._signature(project)
            )
        except VideoAssemblyError:
            final_is_current = False
        final_integrity_failed = False
        if final_is_current:
            final_key = self._local_media_key(final_video.get("asset_url", ""))
            root = self.assets.directory.resolve()
            final_path = (root / final_key) if final_key else None
            try:
                final_is_current = bool(
                    final_path
                    and not final_path.is_symlink()
                    and final_path.parent.resolve() == root
                    and final_path.is_file()
                    and isinstance(final_video.get("size_bytes"), int)
                    and not isinstance(final_video.get("size_bytes"), bool)
                    and final_path.stat().st_size == final_video["size_bytes"]
                    and isinstance(final_video.get("sha256"), str)
                    and self._sha256_file(final_path) == final_video["sha256"]
                )
            except OSError:
                final_is_current = False
            final_integrity_failed = not final_is_current
        delivery_project = copy.deepcopy(project)
        delivery_video = ((delivery_project.get("workflow") or {}).get("video") or {})
        delivery_video.pop("final_video_versions", None)
        if not final_is_current and isinstance(delivery_video.get("final_video"), dict):
            delivery_video["final_video"].pop("asset_url", None)
        media_paths, media_report = self._collect_media(delivery_project)
        final_video_status = str(final_video.get("status") or "not_assembled")
        if final_video_status == "ready" and not final_is_current:
            final_video_status = "stale"
        final_video_stale_reason = None
        if final_video_status == "stale":
            final_video_stale_reason = (
                "合成 MP4 文件校验失败，请重新合成。"
                if final_integrity_failed else "镜头片段或剪辑顺序已更改，请重新合成。"
            )
        production_report = {
            "format": DELIVERY_FORMAT,
            "version": DELIVERY_VERSION,
            "project": {
                "title": str(project.get("title") or "未命名项目"),
                "track": str(project.get("track") or "marketing"),
                "episode_number": project.get("episode_number"),
                "created_at": project.get("created_at"),
                "updated_at": project.get("updated_at"),
                "brief": self._safe_brief(project.get("brief")),
            },
            "stages": stage_report,
            "shots": [self._shot_report(shot, media_paths) for shot in video_shots if isinstance(shot, dict)],
            "media": media_report,
            "final_video_assembled": final_is_current,
            "final_video": {
                "status": final_video_status,
                "stale_reason": final_video_stale_reason,
                "included": self._is_packaged(final_video.get("asset_url"), media_paths) if final_is_current else False,
                "size_bytes": final_video.get("size_bytes") if final_is_current else None,
                "duration_seconds": final_video.get("duration_seconds") if final_is_current else None,
                "sha256": final_video.get("sha256") if final_is_current else None,
            },
            "review_count": len(review_records),
            "current_review_count": sum(bool(item.get("is_current")) for item in review_records),
            "notes": [
                "此交付包包含创作材料和可找到的本地媒体，不代表内容已通过人工验收。",
                "如果本地合成视频仍匹配当前镜头与顺序，将以 MP4 一并交付；合成成片仍需人工审核。",
                "非本地供应商媒体链接不会被下载或写入包内，请在原工作台核对可用性。",
            ],
        }
        files["manual-storyboard.md"] = self._manual_storyboard_markdown(project, canvas_cards).encode("utf-8")
        production_report["manual_storyboard"] = {
            "included": bool(canvas_cards),
            "shot_count": len(canvas_cards),
            "duration_seconds": sum(card["duration_seconds"] for card in canvas_cards),
            "updated_at": canvas["updated_at"],
        }
        files["production-report.json"] = self._json_bytes(production_report)
        files["review-history.json"] = self._json_bytes(review_records)
        files["05-review-history.md"] = self._review_markdown(review_records).encode("utf-8")
        files["README.md"] = self._readme(project, stage_report, video_shots, media_report, len(review_records)).encode("utf-8")

        if len(files) + len(media_paths) > MAX_BUNDLE_MEMBERS:
            raise DeliveryPackageError("交付包文件数量超过限制。", status_code=413)
        total_size = sum(len(payload) for payload in files.values()) + sum(path.stat().st_size for path in media_paths.values())
        if total_size > MAX_BUNDLE_BYTES:
            raise DeliveryPackageError("交付包超过 512 MiB 限制。", status_code=413)

        records = [
            {"path": name, "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
            for name, payload in sorted(files.items())
        ]
        records.extend(
            {"path": name, "size": path.stat().st_size, "sha256": self._sha256_file(path)}
            for name, path in sorted(media_paths.items())
        )
        manifest_bytes = self._json_bytes({
            "format": DELIVERY_FORMAT,
            "version": DELIVERY_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "project_id": project_id,
            "files": records,
        })
        if total_size + len(manifest_bytes) > MAX_BUNDLE_BYTES:
            raise DeliveryPackageError("交付包超过 512 MiB 限制。", status_code=413)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(output_path, mode="x", allowZip64=True) as archive:
                for name, payload in sorted(files.items()):
                    archive.writestr(name, payload, compress_type=zipfile.ZIP_DEFLATED, compresslevel=3)
                for name, path in sorted(media_paths.items()):
                    archive.write(path, name, compress_type=zipfile.ZIP_STORED)
                archive.writestr("manifest.json", manifest_bytes, compress_type=zipfile.ZIP_DEFLATED, compresslevel=3)
        except FileExistsError as error:
            raise DeliveryPackageError("交付包目标已存在，请选择新的临时文件名。", status_code=409) from error
        except OSError as error:
            raise DeliveryPackageError("创建交付包失败。", status_code=500) from error
        return output_path

    def _collect_media(self, project: dict[str, Any]) -> tuple[dict[str, Path], list[dict[str, Any]]]:
        references: dict[str, list[str]] = {}
        external: list[dict[str, str]] = []

        def visit(value: Any, location: str = "project") -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    child = f"{location}.{key}"
                    if key == "asset_url" and isinstance(item, str) and item.strip():
                        media_key = self._local_media_key(item)
                        if media_key:
                            references.setdefault(media_key, []).append(child)
                        elif urlsplit(item).scheme in {"http", "https"}:
                            external.append({"location": child, "included": "false", "reason": "remote_url_not_downloaded"})
                    elif key == "storage_key" and isinstance(item, str) and item.strip():
                        references.setdefault(self._validate_media_key(item), []).append(child)
                    visit(item, child)
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    visit(item, f"{location}[{index}]")

        visit(project)
        root = self.assets.directory.resolve()
        media_paths: dict[str, Path] = {}
        report: list[dict[str, Any]] = []
        for key, locations in sorted(references.items()):
            path = root / key
            if path.is_symlink() or path.parent.resolve() != root or not path.is_file():
                raise DeliveryPackageError(f"项目引用的本地媒体不存在或不安全：{key}", status_code=409)
            size = path.stat().st_size
            if size > MAX_BUNDLE_BYTES:
                raise DeliveryPackageError("单个媒体文件超过交付包限制。", status_code=413)
            name = f"media/{key}"
            media_paths[name] = path
            report.append({"file": name, "size": size, "referenced_at": locations})
        report.extend(external)
        return media_paths, report

    def _local_media_key(self, value: str) -> str | None:
        parsed = urlsplit(value)
        if parsed.scheme or parsed.netloc:
            return None
        path = unquote(value.split("?", 1)[0].split("#", 1)[0])
        prefix = self.assets.public_prefix.rstrip("/") + "/"
        if not path.startswith(prefix):
            return None
        return self._validate_media_key(path[len(prefix):])

    @staticmethod
    def _validate_media_key(key: str) -> str:
        if not key or Path(key).name != key or "/" in key or "\\" in key or key in {".", ".."}:
            raise DeliveryPackageError("项目中包含无效的本地媒体路径。")
        return key

    def _shot_report(self, shot: dict[str, Any], media_paths: dict[str, Path]) -> dict[str, Any]:
        image = shot.get("image") if isinstance(shot.get("image"), dict) else {}
        video = shot.get("video") if isinstance(shot.get("video"), dict) else {}
        return {
            "shot_id": str(shot.get("shot_id") or ""),
            "duration_seconds": shot.get("duration_seconds"),
            "image_status": str(image.get("status") or "pending"),
            "image_included": self._is_packaged(image.get("asset_url"), media_paths),
            "video_status": str(video.get("status") or "pending"),
            "video_included": self._is_packaged(video.get("asset_url"), media_paths),
        }

    def _is_packaged(self, asset_url: Any, media_paths: dict[str, Path]) -> bool:
        if not isinstance(asset_url, str) or not asset_url.strip():
            return False
        key = self._local_media_key(asset_url)
        return bool(key and f"media/{key}" in media_paths)

    @staticmethod
    def _safe_brief(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        allowed = ("business", "product", "audience", "platform", "style", "duration_seconds")
        result = {}
        for key in allowed:
            item = value.get(key)
            if isinstance(item, str) or isinstance(item, (int, float)) and not isinstance(item, bool):
                result[key] = item
        return result

    @staticmethod
    def _status_label(status: Any) -> str:
        return {
            "approved": "已确认",
            "ready": "待确认草稿",
            "stale": "内容已过期，需复核",
            "generating": "生成中",
            "pending": "尚未生成",
            "partial": "部分完成",
            "failed": "失败",
        }.get(str(status or "pending"), str(status))

    @staticmethod
    def _markdown_text(value: Any) -> str:
        return str(value or "未设置").replace("\r", " ").replace("\n", " ").replace("|", "\\|")

    @staticmethod
    def _escape_manual_markdown(value: Any) -> str:
        text = html.escape(str(value or "").replace("\r\n", "\n").replace("\r", "\n"), quote=False)
        return text.translate(str.maketrans({
            "\\": r"\\", "`": r"\`", "*": r"\*", "_": r"\_", "{": r"\{", "}": r"\}",
            "[": r"\[", "]": r"\]", "(": r"\(", ")": r"\)", "#": r"\#", "+": r"\+",
            "-": r"\-", ".": r"\.", "!": r"\!", "|": r"\|", "~": r"\~",
        }))

    def _manual_storyboard_markdown(self, project: dict[str, Any], cards: list[dict[str, Any]]) -> str:
        title = self._escape_manual_markdown(project.get("title") or "未命名项目")
        if not cards:
            return f"# {title} · 手工分镜\n\n尚未创建手工分镜卡片。\n"
        duration = sum(card["duration_seconds"] for card in cards)
        lines = [f"# {title} · 手工分镜", "", f"> {len(cards)} 个镜头 · 预计 {duration} 秒 · 手工编排，不调用模型", ""]
        labels = [
            ("叙事目的", "purpose"), ("画面描述", "visual_description"), ("镜头 / 运镜", "camera"),
            ("对白 / 旁白", "voiceover"), ("屏幕文字", "screen_text"), ("转场", "transition"),
            ("连续性备注", "continuity"),
        ]
        for index, card in enumerate(cards, start=1):
            shot_id = self._escape_manual_markdown(card["shot_id"] or f"S{index:02d}")
            shot_title = self._escape_manual_markdown(card["title"] or "未命名镜头")
            lines.extend([f"## {index:02d} · {shot_id} · {shot_title}", "", f"- 时长：{card['duration_seconds']} 秒"])
            for label, key in labels:
                value = self._escape_manual_markdown(card.get(key, "").strip())
                if value:
                    lines.extend([f"- {label}：", ""])
                    lines.extend(f"> {line}" if line else ">" for line in value.split("\n"))
                    lines.append("")
            lines.append("")
        return "\n".join(lines)

    def _review_markdown(self, records: list[dict[str, Any]]) -> str:
        if not records:
            return "# 审阅历史\n\n尚无审阅记录。\n"
        decisions = {"approved": "通过", "revision_requested": "需修改", "comment": "意见"}
        categories = {"general": "综合", "story": "故事", "continuity": "连续性", "visual": "画面", "audio": "声音", "editing": "剪辑", "subtitle": "字幕", "technical": "技术", "other": "其他"}
        lines = ["# 审阅历史", "", "审阅记录按提交时间倒序排列；“历史版本”表示意见针对的内容或媒体已更新。", ""]
        for index, record in enumerate(records, start=1):
            current = "当前版本" if record.get("is_current") else "历史版本"
            lines.extend([
                f"## 审阅 {index}", "",
                f"- 目标：{self._markdown_text(record.get('target_label'))}",
                f"- 结论：{decisions.get(str(record.get('decision')), '意见')} · {categories.get(str(record.get('category')), '综合')}",
                f"- 版本状态：{current}",
                f"- 时间：{self._markdown_text(record.get('created_at'))}",
            ])
            if record.get("timecode"):
                lines.append(f"- 时间码：{self._markdown_text(record.get('timecode'))}")
            note = self._markdown_text(record.get("note"))
            if note:
                lines.extend(["", "意见：", "", note])
            lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _readme(project: dict[str, Any], stages: list[dict[str, Any]], shots: list[Any], media: list[dict[str, Any]], review_count: int) -> str:
        title = DeliveryService._markdown_text(project.get("title") or "未命名项目")
        brief = project.get("brief") if isinstance(project.get("brief"), dict) else {}
        track = "短剧 / 漫剧" if project.get("track") == "drama" else "品牌营销视频"
        episode = project.get("episode_number")
        episode_label = f" · 第 {episode} 集" if isinstance(episode, int) and not isinstance(episode, bool) else ""
        stage_rows = "\n".join(
            f"| {item['label']} | {DeliveryService._status_label(item['status'])} | {'有内容' if item['included'] else '无内容'} |"
            for item in stages
        )
        videos = sum(
            bool((shot.get("video") or {}).get("asset_url"))
            for shot in shots if isinstance(shot, dict) and isinstance(shot.get("video"), dict)
        )
        local_media = sum(bool(item.get("file")) for item in media)
        external_media = sum(item.get("reason") == "remote_url_not_downloaded" for item in media)
        return (
            f"# {title}\n\n"
            f"**类型：** {track}{episode_label}  \n"
            f"**目标时长：** {DeliveryService._markdown_text(brief.get('duration_seconds'))} 秒  \n"
            f"**平台：** {DeliveryService._markdown_text(brief.get('platform'))}\n\n"
            "## 交付内容\n\n"
            "- `01-script.md`：脚本（如果尚未生成，会标明无内容）\n"
            "- `02-visual-guide.md`：视觉设定\n"
            "- `03-storyboard.md`：分镜\n"
            "- `04-shot-plan.json` 或 `04-shot-plan.md`：逐镜任务与剪辑方案\n"
            "- `manual-storyboard.md`：无需模型的手工分镜卡片（如已创建）\n"
            "- `production-report.json`：机器可读的制作状态摘要\n"
            "- `05-review-history.md` / `review-history.json`：绑定内容版本的审阅历史\n"
            "- `media/`：工作台本地保存且被项目引用的图片 / 视频；如有当前有效的本地合成视频，也会包含最终 MP4\n\n"
            "## 步骤状态\n\n"
            "| 步骤 | 状态 | 内容 |\n|---|---|---|\n"
            f"{stage_rows}\n\n"
            f"已记录视频片段：{videos} 个；审阅记录：{review_count} 条；打包的本地媒体文件：{local_media} 个；未下载的远程媒体引用：{external_media} 个。\n\n"
            "## 验收提示\n\n"
            "本包是创作交接材料，不等同于已验收成片。请人工确认所有脚本与镜头；本地合成 MP4 也需人工审核。"
            "远程供应商媒体不会被下载，且原链接可能过期；请在工作台核对。项目副本导入 / 备份请使用“导出项目副本”，不要把本交付包当作可恢复备份。\n"
        )

    @staticmethod
    def _json_bytes(value: Any) -> bytes:
        try:
            data = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise DeliveryPackageError("交付包中包含无法序列化的数据。") from error
        if len(data) > MAX_JSON_BYTES:
            raise DeliveryPackageError("交付包中的 JSON 文件超过 32 MiB 限制。", status_code=413)
        return data

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()
