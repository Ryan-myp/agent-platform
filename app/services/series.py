from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from app.store import ProjectStore


class SeriesError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SeriesService:
    def __init__(self, store: ProjectStore):
        self.store = store

    def list_series(self) -> list[dict]:
        items = []
        for series in self.store.list_series():
            assets = self.store.list_series_assets(series["id"])
            episodes = self.store.list_series_episodes(series["id"])
            items.append({
                **series,
                "asset_count": sum(not asset.get("archived", False) for asset in assets),
                "episode_count": len(episodes),
            })
        return items

    def get_series(self, series_id: str) -> dict:
        series = self.store.get_series(series_id)
        if not series:
            raise SeriesError("找不到这个系列。", 404)
        return {
            **series,
            "assets": self.store.list_series_assets(series_id),
            "episodes": self.store.list_series_episodes(series_id),
        }

    def create_series(self, data: dict) -> dict:
        title = str(data.get("title", "")).strip()
        if not title:
            raise SeriesError("系列名称不能为空。", 422)
        now = _now()
        series = {
            "id": str(uuid4()),
            "title": title,
            **{
                field: str(data.get(field, "")).strip()
                for field in (
                    "logline", "genre", "world_setting", "central_theme", "season_arc",
                    "character_arcs", "ending", "tone", "content_boundaries", "visual_style", "notes",
                )
            },
            "continuity_entries": [],
            "archived": False,
            "created_at": now,
            "updated_at": now,
        }
        return self.store.save_series(series)

    def update_series(self, series_id: str, updates: dict) -> dict:
        series = self.store.get_series(series_id)
        if not series:
            raise SeriesError("找不到这个系列。", 404)
        for field in (
            "title", "logline", "genre", "world_setting", "central_theme", "season_arc",
            "character_arcs", "ending", "tone", "content_boundaries", "visual_style", "notes",
        ):
            if field in updates:
                value = str(updates[field]).strip()
                if field == "title" and not value:
                    raise SeriesError("系列名称不能为空。", 422)
                series[field] = value
        if "archived" in updates:
            series["archived"] = bool(updates["archived"])
        series["updated_at"] = _now()
        return self.store.save_series(series)

    def create_continuity_entry(self, series_id: str, data: dict) -> dict:
        series = self.store.get_series(series_id)
        if not series:
            raise SeriesError("找不到这个系列。", 404)
        if series.get("archived"):
            raise SeriesError("已归档的系列不能新增连续性记录。", 409)
        entries = series.setdefault("continuity_entries", [])
        if len(entries) >= 200:
            raise SeriesError("每个系列最多保留 200 条连续性记录。", 413)
        summary = str(data.get("summary", "")).strip()
        if not summary:
            raise SeriesError("连续性记录不能为空。", 422)

        source_project_id = data.get("source_project_id")
        source_stage = data.get("source_stage")
        source_excerpt = str(data.get("source_excerpt") or "").strip()
        if source_project_id:
            if not source_stage or not source_excerpt:
                raise SeriesError("引用分集来源时，必须选择已确认步骤并填写原文节选。", 422)
            project = self.store.get(str(source_project_id))
            if not project or project.get("series_id") != series_id:
                raise SeriesError("引用的分集不属于当前系列。", 422)
            stage_item = (project.get("workflow") or {}).get(source_stage) or {}
            content = stage_item.get("content")
            if stage_item.get("status") != "approved" or not isinstance(content, str) or not content.strip():
                raise SeriesError("连续性来源必须是已确认的分集步骤。", 409)
            if source_excerpt not in content:
                raise SeriesError("原文节选必须能在所选已确认步骤中找到。", 422)
            source = {
                "type": "episode",
                "project_id": project["id"],
                "episode_number": project.get("episode_number"),
                "episode_title": str(project.get("title") or "")[:120],
                "stage": source_stage,
                "excerpt": source_excerpt,
            }
        else:
            if source_stage or source_excerpt:
                raise SeriesError("请先选择来源分集，或清空来源步骤和原文节选。", 422)
            source = {"type": "manual"}

        now = _now()
        entry = {
            "id": str(uuid4()),
            "kind": data["kind"],
            "summary": summary,
            "status": data.get("status", "confirmed"),
            "source": source,
            "versions": [],
            "created_at": now,
            "updated_at": now,
        }
        entries.append(entry)
        series["updated_at"] = now
        self.store.save_series(series)
        return entry

    def update_continuity_entry(self, series_id: str, entry_id: str, updates: dict) -> dict:
        series = self.store.get_series(series_id)
        if not series:
            raise SeriesError("找不到这个系列。", 404)
        entries = series.setdefault("continuity_entries", [])
        entry = next((item for item in entries if item.get("id") == entry_id), None)
        if entry is None:
            raise SeriesError("找不到这条连续性记录。", 404)
        changed = any(
            key in updates and str(updates[key]).strip() != str(entry.get(key) or "")
            for key in ("summary", "status")
        )
        if changed:
            versions = entry.setdefault("versions", [])
            versions.append({
                "summary": entry.get("summary", ""),
                "status": entry.get("status", "confirmed"),
                "archived_at": _now(),
            })
            del versions[:-12]
        if "summary" in updates:
            summary = str(updates["summary"]).strip()
            if not summary:
                raise SeriesError("连续性记录不能为空。", 422)
            entry["summary"] = summary
        if "status" in updates:
            entry["status"] = updates["status"]
        if changed:
            entry["updated_at"] = _now()
            series["updated_at"] = entry["updated_at"]
            self.store.save_series(series)
        return entry

    def create_asset(self, series_id: str, data: dict) -> dict:
        series = self.store.get_series(series_id)
        if not series:
            raise SeriesError("找不到这个系列。", 404)
        if series.get("archived"):
            raise SeriesError("已归档的系列不能新增资产。", 409)
        name = str(data.get("name", "")).strip()
        if not name:
            raise SeriesError("资产名称不能为空。", 422)
        now = _now()
        asset = {
            "id": str(uuid4()),
            "series_id": series_id,
            "kind": data["kind"],
            "name": name,
            "description": str(data.get("description", "")).strip(),
            "visual_prompt": str(data.get("visual_prompt", "")).strip(),
            "continuity_notes": str(data.get("continuity_notes", "")).strip(),
            "media": [],
            "archived": False,
            "created_at": now,
            "updated_at": now,
        }
        return self.store.save_series_asset(asset)

    def update_asset(self, series_id: str, asset_id: str, updates: dict) -> dict:
        asset = self.store.get_series_asset(series_id, asset_id)
        if not asset:
            raise SeriesError("找不到这个系列资产。", 404)
        for field in ("kind", "name", "description", "visual_prompt", "continuity_notes"):
            if field in updates:
                value = str(updates[field]).strip()
                if field == "name" and not value:
                    raise SeriesError("资产名称不能为空。", 422)
                asset[field] = value
        if "archived" in updates:
            asset["archived"] = bool(updates["archived"])
        asset["updated_at"] = _now()
        return self.store.save_series_asset(asset)

    def add_asset_media(self, series_id: str, asset_id: str, items: list[dict]) -> dict:
        series = self.store.get_series(series_id)
        if not series:
            raise SeriesError("找不到这个系列。", 404)
        asset = self.store.get_series_asset(series_id, asset_id)
        if not asset:
            raise SeriesError("找不到这个系列资产。", 404)
        if series.get("archived") or asset.get("archived"):
            raise SeriesError("已归档的系列或资产不能添加媒体。", 409)
        if not items:
            raise SeriesError("请选择至少一个本地图片文件。", 422)
        current = asset.setdefault("media", [])
        if len(current) + len(items) > 8:
            raise SeriesError("每个系列资产最多关联 8 张本地参考图。", 413)
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("asset_url"), str) or not item["asset_url"].startswith("/media/"):
                raise SeriesError("本地参考图记录无效。", 422)
            current.append({
                "id": str(uuid4()),
                "asset_url": item["asset_url"],
                "filename": str(item.get("filename") or "reference")[:180],
                "mime_type": str(item.get("mime_type") or "image/png"),
                "size": int(item.get("size") or 0),
                "sha256": str(item.get("sha256") or ""),
                "created_at": _now(),
            })
        asset["updated_at"] = _now()
        series["updated_at"] = asset["updated_at"]
        self.store.save_series(series)
        return self.store.save_series_asset(asset)
