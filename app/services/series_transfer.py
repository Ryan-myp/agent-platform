from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO
from uuid import uuid4

from app.services.project_transfer import (
    BUNDLE_VERSION,
    MAX_BUNDLE_BYTES,
    MAX_BUNDLE_MEMBERS,
    MAX_JSON_BYTES,
    ProjectTransferError,
    ProjectTransferService,
)
from app.store import ProjectStore

SERIES_BUNDLE_FORMAT = "agent-platform-series"


class SeriesTransferService:
    """Copy-only import/export for an entire series and all of its episode records."""

    def __init__(self, store: ProjectStore, transfer: ProjectTransferService):
        self.store = store
        self.transfer = transfer

    def create_export(self, series_id: str, output_path: Path) -> Path:
        series = self.store.get_series(series_id)
        if series is None:
            raise ProjectTransferError("找不到要导出的系列。", status_code=404)
        assets = self.store.list_series_assets(series_id)
        episodes = self.store.list_series_episodes(series_id)
        files: dict[str, bytes] = {
            "series.json": self.transfer._json_bytes({"series": series, "assets": assets}),
        }
        values: list[Any] = [series, assets]
        for index, project in enumerate(episodes, start=1):
            prefix = f"episodes/{index:04d}"
            messages = self.store.list_chat_messages(project["id"])
            attachments = self.store.list_chat_attachments(project["id"])
            files[f"{prefix}/project.json"] = self.transfer._json_bytes(project)
            files[f"{prefix}/chat-messages.json"] = self.transfer._json_bytes(messages)
            files[f"{prefix}/chat-attachments.json"] = self.transfer._json_bytes(attachments)
            files[f"{prefix}/project-reviews.json"] = self.transfer._json_bytes(
                self.store.list_project_reviews(project["id"])
            )
            values.extend((project, messages, attachments))
        media_keys = self.transfer._collect_media_keys(values)
        media_root = self.transfer.assets.directory.resolve()
        media_paths: dict[str, Path] = {}
        total_size = sum(len(payload) for payload in files.values())
        for key in sorted(media_keys):
            path = media_root / key
            if path.parent.resolve() != media_root or path.is_symlink() or not path.is_file():
                raise ProjectTransferError(f"系列引用的本地媒体不存在或不安全：{key}", status_code=409)
            total_size += path.stat().st_size
            media_paths[key] = path
        if total_size > MAX_BUNDLE_BYTES:
            raise ProjectTransferError("系列导出包超过 512 MiB 限制。", status_code=413)
        records = [
            {"path": name, "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
            for name, payload in files.items()
        ]
        records.extend(
            {"path": f"media/{key}", "size": path.stat().st_size, "sha256": self._sha256_file(path)}
            for key, path in media_paths.items()
        )
        manifest = {
            "format": SERIES_BUNDLE_FORMAT,
            "version": BUNDLE_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_series_id": series_id,
            "episode_count": len(episodes),
            "files": records,
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(output_path, mode="x", allowZip64=True) as archive:
                for name, payload in files.items():
                    archive.writestr(name, payload, compress_type=zipfile.ZIP_DEFLATED, compresslevel=3)
                for key, path in media_paths.items():
                    archive.write(path, f"media/{key}", compress_type=zipfile.ZIP_STORED)
                archive.writestr(
                    "manifest.json",
                    self.transfer._json_bytes(manifest),
                    compress_type=zipfile.ZIP_DEFLATED,
                    compresslevel=3,
                )
        except FileExistsError as error:
            raise ProjectTransferError("导出目标已存在，请选择新的临时文件名。", status_code=409) from error
        except OSError as error:
            raise ProjectTransferError("创建系列导出包失败。", status_code=500) from error
        return output_path

    def import_bundle(self, fileobj: BinaryIO) -> dict[str, Any]:
        try:
            archive = zipfile.ZipFile(fileobj, mode="r")
        except (zipfile.BadZipFile, OSError) as error:
            raise ProjectTransferError("文件不是有效的系列导入包。") from error
        with archive:
            infos = self.transfer._validate_archive(archive)
            manifest = self.transfer._read_json(archive, infos["manifest.json"], MAX_JSON_BYTES)
            if not isinstance(manifest, dict) or manifest.get("format") != SERIES_BUNDLE_FORMAT or manifest.get("version") != BUNDLE_VERSION:
                raise ProjectTransferError("系列导入包格式或版本不受支持。")
            declared = self._validated_manifest_files(manifest.get("files"))
            if set(infos) - {"manifest.json"} != set(declared):
                raise ProjectTransferError("系列导入包文件与清单不一致。")
            if sum(int(item["size"]) for item in declared.values()) > MAX_BUNDLE_BYTES:
                raise ProjectTransferError("系列导入包解压后超过 512 MiB 限制。", status_code=413)
            for name, record in declared.items():
                self.transfer._verify_member(archive, infos[name], record)

            if "series.json" not in declared:
                raise ProjectTransferError("系列导入包缺少系列数据。")
            bundle = self.transfer._read_json(archive, infos["series.json"], MAX_JSON_BYTES)
            source_series, source_assets = self._validate_series_bundle(bundle)
            episode_groups: dict[str, set[str]] = {}
            for name in declared:
                if name.startswith("media/") or name == "series.json":
                    continue
                parts = name.split("/")
                if len(parts) != 3 or parts[0] != "episodes" or not parts[1].isdigit() or parts[2] not in {
                    "project.json", "chat-messages.json", "chat-attachments.json", "project-reviews.json"
                }:
                    raise ProjectTransferError("系列导入包包含不受支持的文件。")
                episode_groups.setdefault(parts[1], set()).add(parts[2])
            if len(episode_groups) > 250:
                raise ProjectTransferError("系列导入包最多支持 250 集。", status_code=413)
            required_episode_files = {"project.json", "chat-messages.json", "chat-attachments.json"}
            allowed_episode_files = required_episode_files | {"project-reviews.json"}
            if any(not required_episode_files.issubset(names) or not names.issubset(allowed_episode_files) for names in episode_groups.values()):
                raise ProjectTransferError("系列导入包包含不完整的分集数据。")
            if manifest.get("episode_count") != len(episode_groups):
                raise ProjectTransferError("系列导入包分集数量与清单不一致。")

            episodes: list[dict[str, Any]] = []
            episode_numbers: set[int] = set()
            media_values: list[Any] = [bundle]
            for folder in sorted(episode_groups):
                prefix = f"episodes/{folder}"
                project = self.transfer._read_json(archive, infos[f"{prefix}/project.json"], MAX_JSON_BYTES)
                messages = self.transfer._read_json(archive, infos[f"{prefix}/chat-messages.json"], MAX_JSON_BYTES)
                attachments = self.transfer._read_json(archive, infos[f"{prefix}/chat-attachments.json"], MAX_JSON_BYTES)
                reviews = (
                    self.transfer._read_json(archive, infos[f"{prefix}/project-reviews.json"], MAX_JSON_BYTES)
                    if f"{prefix}/project-reviews.json" in infos else []
                )
                self.transfer._validate_project_data(project, messages, attachments, bundle)
                self.transfer._validate_review_records(reviews, str(project.get("id") or ""))
                if project.get("track") != "drama" or project.get("mode") != "workflow":
                    raise ProjectTransferError("系列导入包仅支持短剧工作流分集。")
                number = project.get("episode_number")
                if project.get("series_id") != source_series["id"] or isinstance(number, bool) or not isinstance(number, int) or number < 1:
                    raise ProjectTransferError("系列导入包中的分集关联无效。")
                if number in episode_numbers:
                    raise ProjectTransferError("系列导入包包含重复集数。")
                episode_numbers.add(number)
                episodes.append({"project": project, "messages": messages, "attachments": attachments, "reviews": reviews})
                media_values.extend((project, messages, attachments))

            source_media_keys = self.transfer._collect_media_keys(media_values)
            archived_media_keys = {name.removeprefix("media/") for name in declared if name.startswith("media/")}
            if source_media_keys != archived_media_keys:
                raise ProjectTransferError("系列导入包的本地媒体与分集引用不一致。")

            now = datetime.now(timezone.utc).isoformat()
            imported_series = copy.deepcopy(source_series)
            imported_series_id = str(uuid4())
            imported_series.update({
                "id": imported_series_id,
                "title": f"{str(source_series.get('title') or '导入系列')[:260]}（导入副本）",
                "created_at": now,
                "updated_at": now,
            })
            imported_series["notes"] = (
                f"{str(imported_series.get('notes') or '').strip()}\n\n由系列导入创建的副本，包含 {len(episodes)} 集。"
            ).strip()
            asset_id_map = {str(item["id"]): str(uuid4()) for item in source_assets}
            imported_assets = []
            for source_asset in source_assets:
                asset = copy.deepcopy(source_asset)
                asset.update({"id": asset_id_map[str(asset["id"])], "series_id": imported_series_id,
                              "created_at": now, "updated_at": now})
                imported_assets.append(asset)

            imported_episodes: list[dict[str, Any]] = []
            warnings: list[str] = []
            for record in episodes:
                project = copy.deepcopy(record["project"])
                messages = copy.deepcopy(record["messages"])
                attachments = copy.deepcopy(record["attachments"])
                reviews = copy.deepcopy(record["reviews"])
                source_project_id = str(project["id"])
                project_id = str(uuid4())
                project.update({
                    "id": project_id,
                    "series_id": imported_series_id,
                    "title": f"{str(project.get('title') or '分集')[:85]}（导入副本）",
                    "created_at": now,
                    "updated_at": now,
                    "transfer_metadata": {
                        "imported_at": now,
                        "source_project_id": source_project_id,
                        "source_series_id": source_series["id"],
                    },
                })
                if self.transfer._sanitize_tasks(project):
                    if "未恢复供应商任务；在途任务标记为中断，需核对结果后再手动重试。" not in warnings:
                        warnings.append("未恢复供应商任务；在途任务标记为中断，需核对结果后再手动重试。")
                attachment_ids = {str(item["id"]): str(uuid4()) for item in attachments}
                self.transfer._remap_chat(messages, attachments, attachment_ids, project_id)
                for review in reviews:
                    review["id"] = str(uuid4())
                    review["project_id"] = project_id
                imported_episodes.append({"project": project, "messages": messages, "attachments": attachments, "reviews": reviews})

            media_stage, media_url_map, installed_paths = self.transfer._stage_media(archive, infos, archived_media_keys)
            try:
                self.transfer._rewrite_local_media(imported_series, media_url_map)
                self.transfer._rewrite_local_media(imported_assets, media_url_map)
                for record in imported_episodes:
                    self.transfer._rewrite_local_media(record["project"], media_url_map)
                    self.transfer._rewrite_local_media(record["messages"], media_url_map)
                    self.transfer._rewrite_local_media(record["attachments"], media_url_map)
                    for attachment in record["attachments"]:
                        key = attachment.get("storage_key")
                        if key in media_url_map:
                            attachment["storage_key"] = Path(media_url_map[key]).name
                self.store.import_series_bundle(imported_series, imported_assets, imported_episodes)
            except Exception:
                for path in installed_paths:
                    path.unlink(missing_ok=True)
                raise
            finally:
                shutil.rmtree(media_stage, ignore_errors=True)
            return {"series": imported_series, "episodes": [item["project"] for item in imported_episodes], "warnings": warnings}

    def _validated_manifest_files(self, records: Any) -> dict[str, dict[str, Any]]:
        if not isinstance(records, list) or len(records) > MAX_BUNDLE_MEMBERS:
            raise ProjectTransferError("系列导入包清单无效。")
        declared: dict[str, dict[str, Any]] = {}
        for record in records:
            if not isinstance(record, dict):
                raise ProjectTransferError("系列导入包清单包含无效文件记录。")
            name, size, digest = record.get("path"), record.get("size"), record.get("sha256")
            self.transfer._validate_member_name(name)
            if name == "manifest.json" or name in declared:
                raise ProjectTransferError("系列导入包清单包含重复或保留路径。")
            if not isinstance(size, int) or size < 0 or size > MAX_BUNDLE_BYTES:
                raise ProjectTransferError("系列导入包文件大小无效。")
            if not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise ProjectTransferError("系列导入包文件校验值无效。")
            declared[name] = record
        return declared

    @staticmethod
    def _validate_series_bundle(bundle: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if not isinstance(bundle, dict) or not isinstance(bundle.get("series"), dict) or not isinstance(bundle.get("assets"), list):
            raise ProjectTransferError("系列导入包中的系列数据无效。")
        series = bundle["series"]
        if not isinstance(series.get("id"), str) or not series["id"]:
            raise ProjectTransferError("系列导入包中的系列记录缺少 ID。")
        assets = bundle["assets"]
        ids: set[str] = set()
        for asset in assets:
            if not isinstance(asset, dict) or not isinstance(asset.get("id"), str) or not asset.get("id"):
                raise ProjectTransferError("系列导入包包含无效系列资产。")
            if asset.get("series_id") != series["id"] or asset["id"] in ids:
                raise ProjectTransferError("系列资产与所属系列不匹配或 ID 重复。")
            ids.add(asset["id"])
        return series, assets

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()
