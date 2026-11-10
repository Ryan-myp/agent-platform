from __future__ import annotations

import base64
import json
from collections import OrderedDict
import mimetypes
import re
import subprocess
import tempfile
import zipfile
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from app.providers.media import AssetStore
from app.store import ProjectStore


MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_ATTACHMENTS_PER_MESSAGE = 8
MAX_TOTAL_ATTACHMENT_BYTES = 40 * 1024 * 1024
MAX_EXTRACTED_TEXT_CHARS = 24000
MAX_VIDEO_SECONDS = 180
MAX_VIDEO_FRAMES = 6
MAX_VIDEO_FRAME_CACHE_BYTES = 24 * 1024 * 1024

_IMAGE_TYPES = {
    ".jpg": ("image/jpeg", b"\xff\xd8\xff"),
    ".jpeg": ("image/jpeg", b"\xff\xd8\xff"),
    ".png": ("image/png", b"\x89PNG\r\n\x1a\n"),
    ".webp": ("image/webp", b"RIFF"),
    ".gif": ("image/gif", b"GIF8"),
}
_VIDEO_TYPES = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
}
_TEXT_TYPES = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".json": "application/json",
    ".yaml": "text/yaml",
    ".yml": "text/yaml",
    ".xml": "application/xml",
    ".html": "text/html",
    ".log": "text/plain",
}
_FILE_TYPES = {".pdf": "application/pdf", ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}


class ChatAttachmentError(ValueError):
    pass


class ChatAttachmentService:
    def __init__(self, store: ProjectStore, assets: AssetStore):
        self.store = store
        self.assets = assets
        self._video_frame_cache: OrderedDict[
            str, tuple[list[tuple[float, bytes]], float, bool]
        ] = OrderedDict()
        self._video_frame_cache_bytes = 0

    @staticmethod
    def _safe_filename(filename: str | None) -> str:
        value = str(filename or "附件").replace("\\", "/").split("/")[-1]
        value = re.sub(r"[\x00-\x1f\x7f]", "", value).strip()
        return value[:160] or "附件"

    def upload(self, project_id: str, filename: str | None, content: bytes) -> dict[str, Any]:
        if not content:
            raise ChatAttachmentError("不能上传空文件。")
        if len(content) > MAX_ATTACHMENT_BYTES:
            raise ChatAttachmentError("单个附件不能超过 20 MB。")
        safe_name = self._safe_filename(filename)
        suffix = Path(safe_name).suffix.lower()
        if suffix not in (_IMAGE_TYPES.keys() | _VIDEO_TYPES.keys() | _TEXT_TYPES.keys() | _FILE_TYPES.keys()):
            raise ChatAttachmentError("暂不支持此文件格式。可上传图片、MP4/MOV/WebM 视频、TXT/MD/CSV/JSON/YAML/XML/HTML/LOG、PDF 或 DOCX。")

        if suffix in _IMAGE_TYPES:
            mime_type, signature = _IMAGE_TYPES[suffix]
            if not content.startswith(signature) or (suffix == ".webp" and content[8:12] != b"WEBP"):
                raise ChatAttachmentError("文件内容与图片格式不匹配。")
            kind = "image"
            extracted_text = ""
        elif suffix in _VIDEO_TYPES:
            if suffix == ".webm":
                valid = content.startswith(b"\x1a\x45\xdf\xa3")
            else:
                valid = len(content) >= 12 and content[4:8] == b"ftyp"
            if not valid:
                raise ChatAttachmentError("文件内容与视频格式不匹配。")
            mime_type = _VIDEO_TYPES[suffix]
            kind = "video"
            extracted_text = ""
        elif suffix in _TEXT_TYPES:
            mime_type = _TEXT_TYPES[suffix]
            if b"\x00" in content[:4096]:
                raise ChatAttachmentError("文本文件编码无法识别。")
            kind = "file"
            extracted_text = content.decode("utf-8-sig", errors="replace")[:MAX_EXTRACTED_TEXT_CHARS]
        else:
            mime_type = _FILE_TYPES[suffix]
            kind = "file"
            extracted_text = self._extract_document(suffix, content)

        asset_url = self.assets.save(content, suffix)
        storage_key = asset_url.rsplit("/", 1)[-1]
        attachment = {
            "id": str(uuid4()),
            "project_id": project_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "filename": safe_name,
            "type": kind,
            "mime_type": mime_type,
            "url": asset_url,
            "size": len(content),
            "storage_key": storage_key,
            "extracted_text": extracted_text,
        }
        try:
            self.store.save_chat_attachment(attachment)
        except Exception:
            self._file_path(storage_key).unlink(missing_ok=True)
            raise
        return self.public_dict(attachment)

    @staticmethod
    def _extract_document(suffix: str, content: bytes) -> str:
        if suffix == ".pdf":
            try:
                from pypdf import PdfReader

                reader = PdfReader(BytesIO(content), strict=False)
                if reader.is_encrypted:
                    raise ChatAttachmentError("暂不支持读取加密 PDF。")
                text = "\n".join((page.extract_text() or "") for page in reader.pages[:30])
                return text[:MAX_EXTRACTED_TEXT_CHARS]
            except ChatAttachmentError:
                raise
            except Exception as error:
                raise ChatAttachmentError("PDF 内容无法读取，请确认文件未损坏。") from error
        if suffix == ".docx":
            try:
                with zipfile.ZipFile(BytesIO(content)) as archive:
                    info = archive.getinfo("word/document.xml")
                    if info.file_size > 8 * 1024 * 1024:
                        raise ChatAttachmentError("DOCX 文档内容过大，无法安全读取。")
                    root = ET.fromstring(archive.read("word/document.xml"))
                namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
                paragraphs = []
                for paragraph in root.findall(".//w:p", namespace):
                    value = "".join(node.text or "" for node in paragraph.findall(".//w:t", namespace))
                    if value:
                        paragraphs.append(value)
                return "\n".join(paragraphs)[:MAX_EXTRACTED_TEXT_CHARS]
            except ChatAttachmentError:
                raise
            except Exception as error:
                raise ChatAttachmentError("DOCX 内容无法读取，请确认文件未损坏。") from error
        return ""

    @staticmethod
    def public_dict(attachment: dict[str, Any]) -> dict[str, Any]:
        return {
            key: attachment[key]
            for key in ("id", "filename", "type", "mime_type", "url", "size")
            if key in attachment
        }

    def discard_upload(self, project_id: str, attachment_id: str) -> None:
        attachment = self.store.delete_chat_attachment(attachment_id, project_id)
        if not attachment:
            return
        storage_key = str(attachment.get("storage_key", ""))
        if Path(storage_key).name != storage_key:
            return
        root = self.assets.directory.resolve()
        path = (root / storage_key).resolve()
        if path.parent == root:
            path.unlink(missing_ok=True)

    def get_for_project(self, project_id: str, attachment_ids: list[str]) -> list[dict[str, Any]]:
        if len(attachment_ids) > MAX_ATTACHMENTS_PER_MESSAGE:
            raise ChatAttachmentError("一条消息最多附加 8 个文件。")
        if len(set(attachment_ids)) != len(attachment_ids):
            raise ChatAttachmentError("附件列表中存在重复文件。")
        attachments = []
        total_size = 0
        for attachment_id in attachment_ids:
            attachment = self.store.get_chat_attachment(attachment_id, project_id)
            if not attachment:
                raise ChatAttachmentError("附件不存在或不属于当前对话，请重新上传。")
            total_size += int(attachment.get("size", 0))
            if total_size > MAX_TOTAL_ATTACHMENT_BYTES:
                raise ChatAttachmentError("单条消息的附件总大小不能超过 40 MB。")
            attachments.append(attachment)
        return attachments

    def _file_path(self, storage_key: str) -> Path:
        if Path(storage_key).name != storage_key:
            raise ChatAttachmentError("附件存储路径无效。")
        root = self.assets.directory.resolve()
        path = (root / storage_key).resolve()
        if path.parent != root or not path.is_file():
            raise ChatAttachmentError("找不到已上传的附件，请重新上传。")
        return path

    def provider_parts(self, attachment: dict[str, Any]) -> list[dict[str, Any]]:
        if attachment["type"] == "file":
            text = attachment.get("extracted_text", "").strip()
            if not text:
                text = "（未能从文件中提取到可读文本）"
            return [{
                "type": "text",
                "text": f"\n[附件：{attachment['filename']}，已提取文本]\n{text}\n[/附件]\n",
            }]
        path = self._file_path(attachment["storage_key"])
        if attachment["type"] == "image":
            return [self._image_part(path.read_bytes(), attachment["mime_type"])]
        frames, duration, truncated = self._cached_video_frames(attachment["id"], path)
        parts: list[dict[str, Any]] = [{
            "type": "text",
            "text": (
                f"\n[视频附件：{attachment['filename']}，时长约 {duration:.1f} 秒。"
                f"以下为按时间抽取的 {len(frames)} 张代表帧；仅分析画面，不含声音。"
                f"{'视频超过 180 秒，仅读取前 180 秒。' if truncated else ''}]\n"
            ),
        }]
        for timestamp, frame in frames:
            parts.append({"type": "text", "text": f"视频画面时间点：{timestamp:.1f} 秒"})
            parts.append(self._image_part(frame, "image/jpeg"))
        return parts

    @staticmethod
    def _image_part(content: bytes, mime_type: str) -> dict[str, Any]:
        encoded = base64.b64encode(content).decode("ascii")
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{mime_type};base64,{encoded}"},
        }

    def _cached_video_frames(
        self,
        attachment_id: str,
        path: Path,
    ) -> tuple[list[tuple[float, bytes]], float, bool]:
        cached = self._video_frame_cache.get(attachment_id)
        if cached is not None:
            self._video_frame_cache.move_to_end(attachment_id)
            return cached
        result = self._video_frames(path)
        result_size = sum(len(frame) for _, frame in result[0])
        if result_size <= MAX_VIDEO_FRAME_CACHE_BYTES:
            while self._video_frame_cache and self._video_frame_cache_bytes + result_size > MAX_VIDEO_FRAME_CACHE_BYTES:
                _, evicted = self._video_frame_cache.popitem(last=False)
                self._video_frame_cache_bytes -= sum(len(frame) for _, frame in evicted[0])
            self._video_frame_cache[attachment_id] = result
            self._video_frame_cache_bytes += result_size
        return result

    @staticmethod
    def _video_frames(path: Path) -> tuple[list[tuple[float, bytes]], float, bool]:
        try:
            import imageio_ffmpeg
        except ImportError as error:
            raise ChatAttachmentError("服务端未安装视频解析组件，暂时无法让 AI 读取视频。") from error

        reader = imageio_ffmpeg.read_frames(str(path), pix_fmt="rgb24")
        try:
            metadata = next(reader)
        except Exception as error:
            raise ChatAttachmentError("视频无法解析，请尝试 MP4、MOV 或 WebM 文件。") from error
        finally:
            reader.close()
        try:
            duration = float(metadata.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0
        if duration <= 0:
            raise ChatAttachmentError("无法识别视频时长。")
        sampled_duration = min(duration, MAX_VIDEO_SECONDS)
        truncated = duration > MAX_VIDEO_SECONDS
        rate = MAX_VIDEO_FRAMES / sampled_duration
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        with tempfile.TemporaryDirectory(prefix="agent-platform-video-") as temporary_directory:
            output_pattern = str(Path(temporary_directory) / "frame-%02d.jpg")
            command = [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(path),
                "-t", str(sampled_duration), "-vf", f"fps={rate:.8f},scale=768:-1",
                "-frames:v", str(MAX_VIDEO_FRAMES), "-q:v", "5", output_pattern,
            ]
            try:
                subprocess.run(command, check=True, capture_output=True, timeout=35)
            except (subprocess.SubprocessError, OSError) as error:
                raise ChatAttachmentError("视频关键帧提取失败，请尝试其他视频文件。") from error
            frame_paths = sorted(Path(temporary_directory).glob("frame-*.jpg"))
            frames: list[tuple[float, bytes]] = []
            for index, frame_path in enumerate(frame_paths[:MAX_VIDEO_FRAMES]):
                timestamp = min(sampled_duration, (index + 0.5) / rate)
                frames.append((timestamp, frame_path.read_bytes()))
        if not frames:
            raise ChatAttachmentError("视频中没有可供 AI 分析的画面。")
        return frames, duration, truncated

    def preview_storage_cleanup(self) -> dict[str, Any]:
        unreferenced_attachments = self.store.list_unreferenced_chat_attachments()
        references = self.store.media_storage_references()
        root = self.assets.directory.resolve()
        files = []
        for path in root.iterdir():
            if path.is_symlink() or not path.is_file():
                continue
            if path.name in references:
                continue
            files.append({"storage_key": path.name, "size": path.stat().st_size})
        files.sort(key=lambda item: item["storage_key"])
        attachments = [
            {
                "id": str(item["id"]),
                "project_id": str(item.get("project_id", "")),
                "filename": str(item.get("filename", "附件")),
                "storage_key": str(item.get("storage_key", "")),
                "size": int(item.get("size", 0)),
            }
            for item in unreferenced_attachments
        ]
        return {
            "attachments": attachments,
            "files": files,
            "total_bytes": sum(item["size"] for item in files) + sum(item["size"] for item in attachments),
        }

    def confirm_storage_cleanup(
        self, *, attachment_ids: list[str], storage_keys: list[str]
    ) -> dict[str, Any]:
        if len(attachment_ids) > 1000 or len(storage_keys) > 1000:
            raise ChatAttachmentError("一次最多清理 1000 个本地项目，请分批操作。")
        if len(set(attachment_ids)) != len(attachment_ids) or len(set(storage_keys)) != len(storage_keys):
            raise ChatAttachmentError("清理列表中存在重复项目，请刷新预览。")
        preview = self.preview_storage_cleanup()
        eligible_attachment_ids = {item["id"] for item in preview["attachments"]}
        if not set(attachment_ids).issubset(eligible_attachment_ids):
            raise ChatAttachmentError("附件引用状态已变化，请刷新清理预览后再确认。")
        eligible_storage_keys = {item["storage_key"] for item in preview["files"]}
        if not set(storage_keys).issubset(eligible_storage_keys):
            raise ChatAttachmentError("媒体引用状态已变化，请刷新清理预览后再确认。")

        removed_attachments = self.store.delete_unreferenced_chat_attachments(
            attachment_ids=set(attachment_ids)
        ) if attachment_ids else []
        keys_to_remove = set(storage_keys)
        keys_to_remove.update(
            str(item.get("storage_key", "")) for item in removed_attachments if item.get("storage_key")
        )
        still_referenced = self.store.media_storage_references()
        removed_files = []
        for storage_key in sorted(keys_to_remove):
            if storage_key in still_referenced or Path(storage_key).name != storage_key:
                continue
            root = self.assets.directory.resolve()
            path = root / storage_key
            if path.parent.resolve() != root or path.is_symlink() or not path.is_file():
                continue
            size = path.stat().st_size
            path.unlink()
            removed_files.append({"storage_key": storage_key, "size": size})
        return {
            "removed_attachments": len(removed_attachments),
            "removed_files": removed_files,
            "removed_bytes": sum(item["size"] for item in removed_files),
        }

    def provider_attachments(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        result = []
        for summary in message.get("attachments", []):
            attachment_id = summary.get("id")
            if not attachment_id:
                continue
            attachment = self.store.get_chat_attachment(attachment_id, message.get("project_id", ""))
            if attachment:
                result.append(attachment)
        return result
