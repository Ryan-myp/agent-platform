from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from app.providers.media import AssetStore
from app.services.storyboard_canvas import StoryboardCanvasError, StoryboardCanvasService
from app.store import ProjectStore

BUNDLE_FORMAT = "agent-platform-project"
BUNDLE_VERSION = 1
MAX_BUNDLE_BYTES = 512 * 1024 * 1024
MAX_JSON_BYTES = 32 * 1024 * 1024
MAX_BUNDLE_MEMBERS = 4096
ACTIVE_TASK_STATUSES = {"queued", "submitting", "polling", "generating", "processing", "in_progress", "interrupted", "stalled", "timed_out"}
TASK_KEYS = {"job_id", "task_id", "provider_task_id"}
_HASH_PATTERN = re.compile(r"^[a-f0-9]{64}$")


class ProjectTransferError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


class ProjectTransferService:
    def __init__(self, store: ProjectStore, assets: AssetStore):
        self.store = store
        self.assets = assets

    def create_export(self, project_id: str, output_path: Path) -> Path:
        project = self.store.get(project_id)
        if project is None:
            raise ProjectTransferError("找不到要导出的项目。", status_code=404)
        messages = self.store.list_chat_messages(project_id)
        attachments = self.store.list_chat_attachments(project_id)
        reviews = self.store.list_project_reviews(project_id)
        files: dict[str, bytes] = {
            "project.json": self._json_bytes(project),
            "chat-messages.json": self._json_bytes(messages),
            "chat-attachments.json": self._json_bytes(attachments),
            "project-reviews.json": self._json_bytes(reviews),
        }
        series_snapshot: dict[str, Any] | None = None
        series_id = project.get("series_id")
        if series_id:
            series = self.store.get_series(str(series_id))
            if series:
                series_snapshot = {
                    "series": series,
                    "assets": self.store.list_series_assets(str(series_id)),
                }
                files["series.json"] = self._json_bytes(series_snapshot)

        media_keys = self._collect_media_keys([project, messages, attachments, series_snapshot])
        media_paths: dict[str, Path] = {}
        media_root = self.assets.directory.resolve()
        total_bytes = sum(len(payload) for payload in files.values())
        for key in sorted(media_keys):
            path = media_root / key
            if path.parent.resolve() != media_root or path.is_symlink() or not path.is_file():
                raise ProjectTransferError(f"项目引用的本地媒体不存在或不安全：{key}", status_code=409)
            size = path.stat().st_size
            total_bytes += size
            media_paths[key] = path
        if total_bytes > MAX_BUNDLE_BYTES:
            raise ProjectTransferError("项目导出包超过 512 MiB 限制。", status_code=413)

        manifest_files = []
        for name, payload in files.items():
            manifest_files.append({"path": name, "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()})
        for key, path in media_paths.items():
            manifest_files.append({"path": f"media/{key}", "size": path.stat().st_size, "sha256": self._sha256_file(path)})
        manifest = {
            "format": BUNDLE_FORMAT,
            "version": BUNDLE_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_project_id": project_id,
            "files": manifest_files,
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(output_path, mode="x", allowZip64=True) as archive:
                for name, payload in files.items():
                    archive.writestr(name, payload, compress_type=zipfile.ZIP_DEFLATED, compresslevel=3)
                for key, path in media_paths.items():
                    archive.write(path, f"media/{key}", compress_type=zipfile.ZIP_STORED)
                archive.writestr("manifest.json", self._json_bytes(manifest), compress_type=zipfile.ZIP_DEFLATED, compresslevel=3)
        except FileExistsError as error:
            raise ProjectTransferError("导出目标已存在，请选择新的临时文件名。", status_code=409) from error
        except OSError as error:
            raise ProjectTransferError("创建项目导出包失败。", status_code=500) from error
        return output_path

    def import_bundle(self, fileobj: BinaryIO) -> dict[str, Any]:
        try:
            archive = zipfile.ZipFile(fileobj, mode="r")
        except (zipfile.BadZipFile, OSError) as error:
            raise ProjectTransferError("文件不是有效的项目导入包。") from error

        with archive:
            infos = self._validate_archive(archive)
            manifest = self._read_json(archive, infos["manifest.json"], MAX_JSON_BYTES)
            if not isinstance(manifest, dict) or manifest.get("format") != BUNDLE_FORMAT or manifest.get("version") != BUNDLE_VERSION:
                raise ProjectTransferError("项目导入包格式或版本不受支持。")
            records = manifest.get("files")
            if not isinstance(records, list) or len(records) > MAX_BUNDLE_MEMBERS:
                raise ProjectTransferError("项目导入包清单无效。")
            declared: dict[str, dict[str, Any]] = {}
            for record in records:
                if not isinstance(record, dict):
                    raise ProjectTransferError("项目导入包清单包含无效文件记录。")
                name = record.get("path")
                size = record.get("size")
                digest = record.get("sha256")
                self._validate_member_name(name)
                if name == "manifest.json" or name in declared:
                    raise ProjectTransferError("项目导入包清单包含重复或保留路径。")
                if not isinstance(size, int) or size < 0 or size > MAX_BUNDLE_BYTES:
                    raise ProjectTransferError("项目导入包文件大小无效。")
                if not isinstance(digest, str) or not _HASH_PATTERN.fullmatch(digest):
                    raise ProjectTransferError("项目导入包文件校验值无效。")
                declared[name] = record
            archive_names = set(infos) - {"manifest.json"}
            if archive_names != set(declared):
                raise ProjectTransferError("项目导入包文件与清单不一致。")
            if sum(int(item["size"]) for item in declared.values()) > MAX_BUNDLE_BYTES:
                raise ProjectTransferError("项目导入包解压后超过 512 MiB 限制。", status_code=413)

            for name, record in declared.items():
                self._verify_member(archive, infos[name], record)
            required = {"project.json", "chat-messages.json", "chat-attachments.json"}
            if not required.issubset(declared):
                raise ProjectTransferError("项目导入包缺少必需数据。")
            project = self._read_json(archive, infos["project.json"], MAX_JSON_BYTES)
            messages = self._read_json(archive, infos["chat-messages.json"], MAX_JSON_BYTES)
            attachments = self._read_json(archive, infos["chat-attachments.json"], MAX_JSON_BYTES)
            series_bundle = self._read_json(archive, infos["series.json"], MAX_JSON_BYTES) if "series.json" in infos else None
            self._validate_project_data(project, messages, attachments, series_bundle)
            reviews = (
                self._read_json(archive, infos["project-reviews.json"], MAX_JSON_BYTES)
                if "project-reviews.json" in infos else []
            )
            self._validate_review_records(reviews, str(project["id"]))

            source_media_keys = self._collect_media_keys([project, messages, attachments, series_bundle])
            archived_media_keys = {name.removeprefix("media/") for name in declared if name.startswith("media/")}
            supported_names = required | {"series.json", "project-reviews.json"}
            if any(not name.startswith("media/") and name not in supported_names for name in declared):
                raise ProjectTransferError("项目导入包包含不受支持的文件。")
            if source_media_keys != archived_media_keys:
                raise ProjectTransferError("项目导入包的本地媒体与项目引用不一致。")

            source_project_id = str(project["id"])
            imported_project = copy.deepcopy(project)
            imported_messages = copy.deepcopy(messages)
            imported_attachments = copy.deepcopy(attachments)
            imported_reviews = copy.deepcopy(reviews)
            imported_series: dict[str, Any] | None = None
            imported_series_assets: list[dict[str, Any]] = []
            warnings: list[str] = []
            if series_bundle:
                imported_series, imported_series_assets = self._remap_series(series_bundle, source_project_id)
                imported_project["series_id"] = imported_series["id"]
                warnings.append("系列已作为副本导入；此项目包不包含同系列的其他分集。")
            elif imported_project.get("series_id"):
                imported_project["series_id"] = None
                imported_project["episode_number"] = None
                warnings.append("原系列信息未包含在导入包中，已解除系列关联。")

            now = datetime.now(timezone.utc).isoformat()
            new_project_id = str(uuid4())
            imported_project["id"] = new_project_id
            imported_project["title"] = f"{str(imported_project.get('title') or '导入项目')[:280]}（导入副本）"
            imported_project["created_at"] = now
            imported_project["updated_at"] = now
            imported_project["archived"] = False
            imported_project["transfer_metadata"] = {
                "imported_at": now,
                "source_project_id": source_project_id,
                "source_created_at": project.get("created_at"),
            }
            has_unresumable_tasks = self._sanitize_tasks(imported_project)
            if has_unresumable_tasks:
                warnings.append("未恢复供应商任务；在途任务标记为中断，需核对结果后再手动重试。")

            attachment_id_map = {str(item["id"]): str(uuid4()) for item in imported_attachments}
            media_stage, media_url_map, installed_paths = self._stage_media(archive, infos, archived_media_keys)
            try:
                self._rewrite_local_media(imported_project, media_url_map)
                self._rewrite_local_media(imported_messages, media_url_map)
                self._rewrite_local_media(imported_attachments, media_url_map)
                self._remap_chat(imported_messages, imported_attachments, attachment_id_map, new_project_id)
                for review in imported_reviews:
                    review["id"] = str(uuid4())
                    review["project_id"] = new_project_id
                self._rewrite_local_media(imported_series, media_url_map)
                self._rewrite_local_media(imported_series_assets, media_url_map)

                for attachment in imported_attachments:
                    storage_key = attachment.get("storage_key")
                    if storage_key in media_url_map:
                        attachment["storage_key"] = Path(urlsplit(media_url_map[storage_key]).path).name
                self.store.import_project_bundle(
                    imported_project,
                    messages=imported_messages,
                    attachments=imported_attachments,
                    series=imported_series,
                    series_assets=imported_series_assets,
                    reviews=imported_reviews,
                )
            except Exception:
                for path in installed_paths:
                    path.unlink(missing_ok=True)
                raise
            finally:
                shutil.rmtree(media_stage, ignore_errors=True)
            return {"project": imported_project, "warnings": warnings}

    def _validate_archive(self, archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
        infos: dict[str, zipfile.ZipInfo] = {}
        total_size = 0
        all_infos = archive.infolist()
        if not all_infos or len(all_infos) > MAX_BUNDLE_MEMBERS + 1:
            raise ProjectTransferError("项目导入包文件数量无效。")
        for info in all_infos:
            self._validate_member_name(info.filename)
            if info.filename in infos:
                raise ProjectTransferError("项目导入包包含重复文件路径。")
            if info.is_dir() or info.flag_bits & 0x1:
                raise ProjectTransferError("项目导入包包含不支持的目录或加密文件。")
            mode = (info.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(mode):
                raise ProjectTransferError("项目导入包不能包含符号链接。")
            if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                raise ProjectTransferError("项目导入包使用了不支持的压缩方式。")
            if info.file_size < 0 or info.file_size > MAX_BUNDLE_BYTES:
                raise ProjectTransferError("项目导入包中的文件超过大小限制。", status_code=413)
            total_size += info.file_size
            if total_size > MAX_BUNDLE_BYTES:
                raise ProjectTransferError("项目导入包解压后超过 512 MiB 限制。", status_code=413)
            infos[info.filename] = info
        if "manifest.json" not in infos:
            raise ProjectTransferError("项目导入包缺少清单文件。")
        return infos

    @staticmethod
    def _validate_member_name(name: Any) -> None:
        if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
            raise ProjectTransferError("项目导入包包含无效文件路径。")
        path = PurePosixPath(name)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts) or str(path) != name:
            raise ProjectTransferError("项目导入包包含不安全的文件路径。")
        if name.startswith("media/"):
            key = name.removeprefix("media/")
            if not key or "/" in key or Path(key).name != key:
                raise ProjectTransferError("项目导入包包含无效媒体路径。")

    @staticmethod
    def _read_json(archive: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int) -> Any:
        if info.file_size > limit:
            raise ProjectTransferError("项目导入包中的 JSON 文件过大。", status_code=413)
        try:
            with archive.open(info, "r") as source:
                data = source.read(limit + 1)
            if len(data) > limit or len(data) != info.file_size:
                raise ProjectTransferError("项目导入包中的 JSON 文件大小无效。")
            return json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, zipfile.BadZipFile, RuntimeError) as error:
            raise ProjectTransferError("项目导入包中的 JSON 文件无法读取。") from error

    @staticmethod
    def _verify_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo, record: dict[str, Any]) -> None:
        digest = hashlib.sha256()
        count = 0
        try:
            with archive.open(info, "r") as source:
                while chunk := source.read(1024 * 1024):
                    count += len(chunk)
                    if count > int(record["size"]) or count > MAX_BUNDLE_BYTES:
                        raise ProjectTransferError("项目导入包成员解压大小异常。", status_code=413)
                    digest.update(chunk)
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as error:
            raise ProjectTransferError("项目导入包包含损坏的文件。") from error
        if count != record["size"] or digest.hexdigest() != record["sha256"]:
            raise ProjectTransferError("项目导入包文件校验失败。")

    @staticmethod
    def _validate_review_records(records: Any, source_project_id: str) -> None:
        if not isinstance(records, list) or len(records) > 100_000:
            raise ProjectTransferError("项目导入包中的审阅记录无效。")
        seen: set[str] = set()
        target_types = {"script", "storyboard", "clip", "final_video"}
        decisions = {"approved", "revision_requested", "comment"}
        categories = {"general", "story", "continuity", "visual", "audio", "editing", "subtitle", "technical", "other"}
        for record in records:
            if not isinstance(record, dict):
                raise ProjectTransferError("项目导入包包含无效审阅记录。")
            record_id = record.get("id")
            revision = record.get("target_revision")
            target_type = record.get("target_type")
            target_id = record.get("target_id", "")
            decision = record.get("decision")
            note = record.get("note", "")
            created_at = record.get("created_at")
            timecode = record.get("timecode")
            valid_timecode = timecode is None
            if isinstance(timecode, str) and re.fullmatch(r"(\d{1,3}):([0-5]\d)", timecode):
                minutes, seconds = (int(part) for part in timecode.split(":"))
                valid_timecode = minutes * 60 + seconds <= 1800
            valid_timestamp = False
            if isinstance(created_at, str) and len(created_at) <= 64:
                try:
                    valid_timestamp = datetime.fromisoformat(created_at.replace("Z", "+00:00")).tzinfo is not None
                except ValueError:
                    pass
            if (
                not isinstance(record_id, str) or not record_id or len(record_id) > 80 or record_id in seen
                or record.get("project_id") != source_project_id
                or not isinstance(target_type, str) or target_type not in target_types
                or not isinstance(decision, str) or decision not in decisions
                or not isinstance(record.get("category"), str) or record.get("category") not in categories
                or not valid_timestamp
                or not isinstance(record.get("target_label"), str) or len(record.get("target_label", "")) > 200
                or not isinstance(target_id, str) or len(target_id) > 80
                or (target_type == "clip") != bool(target_id)
                or not isinstance(note, str) or len(note) > 2000
                or (decision != "approved" and not note.strip())
                or not valid_timecode
                or (timecode is not None and target_type not in {"clip", "final_video"})
                or not isinstance(revision, str) or not _HASH_PATTERN.fullmatch(revision)
            ):
                raise ProjectTransferError("项目导入包包含无效审阅记录。")
            seen.add(record_id)

    @staticmethod
    def _validate_project_data(project: Any, messages: Any, attachments: Any, series_bundle: Any) -> None:
        if not isinstance(project, dict) or not isinstance(project.get("id"), str) or not project["id"]:
            raise ProjectTransferError("导入包中的项目记录无效。")
        if project.get("mode") not in {"chat", "workflow"}:
            raise ProjectTransferError("导入包中的项目类型不受支持。")
        if "storyboard_canvas" in project:
            if project.get("mode") != "workflow":
                raise ProjectTransferError("手工分镜画布仅适用于创作流程项目。")
            try:
                project["storyboard_canvas"] = StoryboardCanvasService.validate_record(project["storyboard_canvas"])
            except StoryboardCanvasError as error:
                raise ProjectTransferError("导入包中的手工分镜画布无效。") from error
        if not isinstance(messages, list) or not isinstance(attachments, list):
            raise ProjectTransferError("导入包中的聊天数据无效。")
        if any(not isinstance(item, dict) or not isinstance(item.get("id"), str) or not isinstance(item.get("created_at"), str) for item in messages):
            raise ProjectTransferError("导入包包含无效聊天记录。")
        if any(not isinstance(item, dict) or not isinstance(item.get("id"), str) or not isinstance(item.get("created_at"), str) for item in attachments):
            raise ProjectTransferError("导入包包含无效附件记录。")
        attachment_ids = [str(item["id"]) for item in attachments]
        if len(set(attachment_ids)) != len(attachment_ids):
            raise ProjectTransferError("导入包包含重复的附件 ID。")
        known_attachment_ids = set(attachment_ids)
        for message in messages:
            summaries = message.get("attachments", [])
            if not isinstance(summaries, list):
                raise ProjectTransferError("导入包包含无效聊天附件引用。")
            if any(isinstance(item, dict) and item.get("id") and str(item["id"]) not in known_attachment_ids for item in summaries):
                raise ProjectTransferError("导入包引用了缺失的聊天附件。")
        if series_bundle is not None:
            if not isinstance(series_bundle, dict) or not isinstance(series_bundle.get("series"), dict) or not isinstance(series_bundle.get("assets"), list):
                raise ProjectTransferError("导入包中的系列数据无效。")
            if not project.get("series_id") or series_bundle["series"].get("id") != project.get("series_id"):
                raise ProjectTransferError("导入包中的系列与项目关联不匹配。")
            if any(not isinstance(item, dict) for item in series_bundle["assets"]):
                raise ProjectTransferError("导入包包含无效系列资产。")

    def _remap_series(self, bundle: dict[str, Any], source_project_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        original = copy.deepcopy(bundle["series"])
        if not original.get("id"):
            raise ProjectTransferError("导入包中的系列记录缺少 ID。")
        new_id = str(uuid4())
        now = datetime.now(timezone.utc).isoformat()
        original["id"] = new_id
        original["title"] = f"{str(original.get('title') or '导入系列')[:260]}（导入副本）"
        original["created_at"] = now
        original["updated_at"] = now
        note = str(original.get("notes") or "").strip()
        import_note = "单项目导入副本；未包含原系列的其他分集。"
        original["notes"] = f"{note}\n\n{import_note}".strip()
        assets = []
        for source_asset in bundle["assets"]:
            asset = copy.deepcopy(source_asset)
            if asset.get("series_id") != bundle["series"].get("id"):
                raise ProjectTransferError("系列资产与所属系列不匹配。")
            asset["id"] = str(uuid4())
            asset["series_id"] = new_id
            asset["created_at"] = now
            asset["updated_at"] = now
            assets.append(asset)
        return original, assets

    @staticmethod
    def _sanitize_tasks(value: Any) -> bool:
        found = False
        if isinstance(value, dict):
            status = str(value.get("status") or "").lower()
            has_task_id = any(value.get(key) for key in TASK_KEYS)
            if status in ACTIVE_TASK_STATUSES or has_task_id:
                found = True
                if status in ACTIVE_TASK_STATUSES or has_task_id and status not in {"ready", "succeeded", "completed", "failed", "cancelled"}:
                    value["status"] = "interrupted"
                    value["error"] = "导入时未恢复供应商任务；请核对远端状态后再手动重试。"
                for key in TASK_KEYS:
                    value.pop(key, None)
                value.pop("provider_status", None)
            for item in value.values():
                found = ProjectTransferService._sanitize_tasks(item) or found
        elif isinstance(value, list):
            for item in value:
                found = ProjectTransferService._sanitize_tasks(item) or found
        return found

    def _stage_media(
        self,
        archive: zipfile.ZipFile,
        infos: dict[str, zipfile.ZipInfo],
        media_keys: set[str],
    ) -> tuple[Path, dict[str, str], list[Path]]:
        self.assets.directory.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".project-import-", dir=self.assets.directory))
        url_map: dict[str, str] = {}
        staged_files: dict[str, Path] = {}
        try:
            for key in sorted(media_keys):
                suffix = Path(key).suffix
                filename = f"{uuid4().hex}{suffix}"
                archive_name = f"media/{key}"
                target = stage / filename
                with archive.open(infos[archive_name], "r") as source, target.open("xb") as destination:
                    shutil.copyfileobj(source, destination, length=1024 * 1024)
                staged_files[key] = target
                url_map[key] = f"{self.assets.public_prefix}/{filename}"
        except Exception:
            shutil.rmtree(stage, ignore_errors=True)
            raise

        installed: list[Path] = []
        try:
            for key, staged in staged_files.items():
                destination = self.assets.directory / Path(url_map[key]).name
                if destination.exists() or destination.is_symlink():
                    raise ProjectTransferError("媒体文件名冲突，请重新导入项目包。", status_code=409)
                os.link(staged, destination)
                installed.append(destination)
        except Exception:
            for path in installed:
                path.unlink(missing_ok=True)
            shutil.rmtree(stage, ignore_errors=True)
            raise
        return stage, url_map, installed

    def _collect_media_keys(self, values: list[Any]) -> set[str]:
        keys: set[str] = set()
        prefix = self.assets.public_prefix.rstrip("/") + "/"

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                storage_key = value.get("storage_key")
                if isinstance(storage_key, str) and storage_key:
                    keys.add(self._validate_storage_key(storage_key))
                for item in value.values():
                    visit(item)
            elif isinstance(value, list):
                for item in value:
                    visit(item)
            elif isinstance(value, str):
                parsed = urlsplit(value)
                path = unquote(parsed.path if parsed.scheme or parsed.netloc else value.split("?", 1)[0].split("#", 1)[0])
                if path.startswith(prefix):
                    key = path[len(prefix):]
                    keys.add(self._validate_storage_key(key))

        for item in values:
            visit(item)
        return keys

    @staticmethod
    def _validate_storage_key(key: str) -> str:
        if not key or Path(key).name != key or "/" in key or "\\" in key or key in {".", ".."}:
            raise ProjectTransferError("项目中包含无效的本地媒体路径。")
        return key

    def _rewrite_local_media(self, value: Any, url_map: dict[str, str]) -> None:
        if isinstance(value, dict):
            for key, item in list(value.items()):
                value[key] = self._rewrite_local_media_value(item, url_map)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                value[index] = self._rewrite_local_media_value(item, url_map)

    def _rewrite_local_media_value(self, value: Any, url_map: dict[str, str]) -> Any:
        if isinstance(value, dict):
            self._rewrite_local_media(value, url_map)
            return value
        if isinstance(value, list):
            self._rewrite_local_media(value, url_map)
            return value
        if isinstance(value, str):
            prefix = self.assets.public_prefix.rstrip("/") + "/"
            parsed = urlsplit(value)
            path = unquote(parsed.path if parsed.scheme or parsed.netloc else value.split("?", 1)[0].split("#", 1)[0])
            if path.startswith(prefix):
                key = path[len(prefix):]
                if key in url_map:
                    return f"{url_map[key]}{('?'+parsed.query) if parsed.query else ''}{('#'+parsed.fragment) if parsed.fragment else ''}"
        return value

    @staticmethod
    def _remap_chat(
        messages: list[dict[str, Any]],
        attachments: list[dict[str, Any]],
        attachment_id_map: dict[str, str],
        project_id: str,
    ) -> None:
        for message in messages:
            message["id"] = str(uuid4())
            message["project_id"] = project_id
            for summary in message.get("attachments", []):
                if isinstance(summary, dict) and str(summary.get("id", "")) in attachment_id_map:
                    summary["id"] = attachment_id_map[str(summary["id"])]
        for attachment in attachments:
            attachment["id"] = attachment_id_map[str(attachment["id"])]
            attachment["project_id"] = project_id

    @staticmethod
    def _json_bytes(value: Any) -> bytes:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(payload) > MAX_JSON_BYTES:
            raise ProjectTransferError("项目数据超过单个 JSON 文件的 32 MiB 限制。", status_code=413)
        return payload

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()
