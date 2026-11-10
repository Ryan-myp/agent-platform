from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


BACKUP_FORMAT = "agent-platform-backup"
BACKUP_FORMAT_VERSION = 1
DATABASE_FILENAME = "agent-platform.db"
MANIFEST_FILENAME = "manifest.json"
MAX_ARCHIVE_ENTRIES = 100_000
MAX_MANIFEST_BYTES = 1_000_000
DEFAULT_MAX_RESTORE_BYTES = 20 * 1024**3
TIMESTAMPED_BACKUP_NAME = re.compile(r"^agent-platform-backup-\d{8}T\d{12}Z\.zip$")


class BackupError(RuntimeError):
    """The archive could not be safely created or restored."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_archive_name(name: str) -> str:
    if not name or "\x00" in name or "\\" in name or ":" in name:
        raise BackupError("备份中包含不安全的文件路径。")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise BackupError("备份中包含不安全的文件路径。")
    if path.as_posix() != name or len(name) > 1024:
        raise BackupError("备份中包含无效的文件路径。")
    return name


def _zip_member(archive: zipfile.ZipFile, path: Path, archive_name: str) -> dict[str, Any]:
    archive_name = _safe_archive_name(archive_name)
    info = zipfile.ZipInfo(archive_name, date_time=datetime.now().timetuple()[:6])
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (stat.S_IFREG | 0o600) << 16
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source, archive.open(info, "w") as destination:
        while chunk := source.read(1024 * 1024):
            destination.write(chunk)
            digest.update(chunk)
            size += len(chunk)
    return {"path": archive_name, "size": size, "sha256": digest.hexdigest()}


def _media_files(media_directory: Path) -> list[tuple[str, Path]]:
    if not media_directory.exists():
        return []
    if media_directory.is_symlink() or not media_directory.is_dir():
        raise BackupError("媒体目录必须是普通目录，不能是符号链接。")

    media_root = media_directory.resolve()
    result: list[tuple[str, Path]] = []
    def on_walk_error(error: OSError) -> None:
        raise BackupError(f"无法读取媒体目录：{error}") from error

    for root, directories, filenames in os.walk(
        media_root, topdown=True, onerror=on_walk_error, followlinks=False
    ):
        root_path = Path(root)
        directories[:] = sorted(
            name for name in directories
            if not (root_path / name).is_symlink()
            and (root_path / name).resolve().is_relative_to(media_root)
        )
        for name in sorted(filenames):
            path = root_path / name
            try:
                file_stat = path.lstat()
            except OSError as error:
                raise BackupError(f"无法读取媒体文件：{path.name}") from error
            if not stat.S_ISREG(file_stat.st_mode):
                continue
            relative = path.relative_to(media_root).as_posix()
            result.append((f"media/{relative}", path))
    return result


def create_backup_archive(
    database_path: Path,
    media_directory: Path,
    output_path: Path,
) -> dict[str, Any]:
    database_path = Path(database_path)
    media_directory = Path(media_directory)
    output_path = Path(output_path)
    if not database_path.is_file() or database_path.is_symlink():
        raise BackupError("数据库文件不存在，或不是普通文件。")
    if output_path.exists() or output_path.is_symlink():
        raise BackupError("备份文件已存在；请指定新的输出路径，避免覆盖已有备份。")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_output = output_path.resolve()
    resolved_database = database_path.resolve()
    if resolved_output == resolved_database:
        raise BackupError("备份输出不能覆盖数据库文件。")
    if media_directory.exists():
        resolved_media = media_directory.resolve()
        if resolved_output == resolved_media or resolved_media in resolved_output.parents:
            raise BackupError("备份输出不能放在媒体目录内。")

    temporary_directory = Path(tempfile.mkdtemp(prefix=".agent-platform-backup-", dir=output_path.parent))
    temporary_archive = temporary_directory / "backup.zip"
    snapshot_path = temporary_directory / DATABASE_FILENAME
    try:
        try:
            source = sqlite3.connect(str(database_path))
            snapshot: sqlite3.Connection | None = None
            try:
                snapshot = sqlite3.connect(str(snapshot_path))
                source.backup(snapshot)
                integrity = snapshot.execute("PRAGMA integrity_check").fetchone()
                if not integrity or integrity[0] != "ok":
                    raise BackupError("数据库快照完整性检查失败。")
            finally:
                if snapshot is not None:
                    snapshot.close()
                source.close()
        except sqlite3.Error as error:
            raise BackupError(f"无法创建一致的 SQLite 数据库快照：{error}") from error

        files: list[dict[str, Any]] = []
        with zipfile.ZipFile(
            temporary_archive,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
            allowZip64=True,
        ) as archive:
            files.append(_zip_member(archive, snapshot_path, DATABASE_FILENAME))
            for archive_name, media_path in _media_files(media_directory):
                files.append(_zip_member(archive, media_path, archive_name))
            manifest = {
                "format": BACKUP_FORMAT,
                "format_version": BACKUP_FORMAT_VERSION,
                "created_at": _utc_now(),
                "database": DATABASE_FILENAME,
                "files": files,
                "credentials_included": False,
            }
            archive.writestr(
                MANIFEST_FILENAME,
                json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8"),
            )
        os.chmod(temporary_archive, 0o600)
        try:
            os.link(temporary_archive, output_path)
        except FileExistsError as error:
            raise BackupError("备份文件已存在；请指定新的输出路径，避免覆盖已有备份。") from error
        temporary_archive.unlink()
        return {
            "path": str(output_path),
            "file_count": len(files),
            "total_bytes": sum(item["size"] for item in files),
            "manifest": manifest,
        }
    except BackupError:
        raise
    except (OSError, sqlite3.Error, zipfile.BadZipFile) as error:
        raise BackupError(f"创建备份失败：{error}") from error
    finally:
        shutil.rmtree(temporary_directory, ignore_errors=True)


def _read_manifest(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> dict[str, Any]:
    if info.file_size > MAX_MANIFEST_BYTES:
        raise BackupError("备份清单过大。")
    try:
        with archive.open(info, "r") as source:
            raw_manifest = source.read(MAX_MANIFEST_BYTES + 1)
        if len(raw_manifest) > MAX_MANIFEST_BYTES:
            raise BackupError("备份清单过大。")
        manifest = json.loads(raw_manifest.decode("utf-8"))
    except BackupError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, OSError, RuntimeError, zipfile.BadZipFile) as error:
        raise BackupError("备份清单无效。") from error
    if not isinstance(manifest, dict):
        raise BackupError("备份清单格式无效。")
    if manifest.get("format") != BACKUP_FORMAT or manifest.get("format_version") != BACKUP_FORMAT_VERSION:
        raise BackupError("不支持此备份格式或版本。")
    if manifest.get("database") != DATABASE_FILENAME or not isinstance(manifest.get("files"), list):
        raise BackupError("备份清单缺少数据库或文件列表。")
    return manifest


def _validate_archive_entries(
    archive: zipfile.ZipFile,
    *,
    max_total_bytes: int,
) -> tuple[dict[str, zipfile.ZipInfo], dict[str, dict[str, Any]], dict[str, Any]]:
    infos = archive.infolist()
    if not infos or len(infos) > MAX_ARCHIVE_ENTRIES:
        raise BackupError("备份文件数量无效或超过限制。")

    by_name: dict[str, zipfile.ZipInfo] = {}
    for info in infos:
        name = _safe_archive_name(info.filename)
        if name in by_name or info.is_dir():
            raise BackupError("备份中包含重复路径或目录条目。")
        mode = (info.external_attr >> 16) & 0o170000
        if mode == stat.S_IFLNK or (mode not in {0, stat.S_IFREG}):
            raise BackupError("备份中不允许包含符号链接或特殊文件。")
        if info.flag_bits & 0x1:
            raise BackupError("不支持加密的备份文件。")
        by_name[name] = info

    manifest_info = by_name.get(MANIFEST_FILENAME)
    if manifest_info is None:
        raise BackupError("备份缺少 manifest.json。")
    manifest = _read_manifest(archive, manifest_info)

    records: dict[str, dict[str, Any]] = {}
    for item in manifest["files"]:
        if not isinstance(item, dict):
            raise BackupError("备份清单中的文件记录无效。")
        raw_name = item.get("path")
        if not isinstance(raw_name, str):
            raise BackupError("备份清单中的文件路径无效。")
        name = _safe_archive_name(raw_name)
        size = item.get("size")
        digest = item.get("sha256")
        if name == MANIFEST_FILENAME or name in records:
            raise BackupError("备份清单包含重复或保留路径。")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise BackupError("备份清单中的文件大小无效。")
        if not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest.lower()):
            raise BackupError("备份清单中的校验值无效。")
        records[name] = {"size": size, "sha256": digest.lower()}

    if DATABASE_FILENAME not in records:
        raise BackupError("备份中缺少数据库文件。")
    if set(by_name) != set(records) | {MANIFEST_FILENAME}:
        raise BackupError("备份内容与清单不一致。")

    declared_total = 0
    for name, record in records.items():
        info = by_name[name]
        if info.file_size != record["size"]:
            raise BackupError("备份文件大小与清单不一致。")
        if name != DATABASE_FILENAME and not name.startswith("media/"):
            raise BackupError("备份包含不支持的文件路径。")
        declared_total += record["size"]
        if declared_total > max_total_bytes:
            raise BackupError("备份解压后的总大小超过限制。")
    return by_name, records, manifest


def _extract_verified(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    target: Path,
    record: dict[str, Any],
    *,
    max_remaining_bytes: int,
) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    try:
        with archive.open(info, "r") as source, target.open("xb") as destination:
            os.chmod(target, 0o600)
            while chunk := source.read(1024 * 1024):
                size += len(chunk)
                if size > record["size"] or size > max_remaining_bytes:
                    raise BackupError("备份解压内容超过清单或大小限制。")
                digest.update(chunk)
                destination.write(chunk)
    except (OSError, RuntimeError, zipfile.BadZipFile) as error:
        raise BackupError(f"无法安全解压备份文件：{info.filename}") from error
    if size != record["size"] or digest.hexdigest() != record["sha256"]:
        raise BackupError(f"备份校验失败：{info.filename}")
    return size


def restore_backup_archive(
    archive_path: Path,
    target_directory: Path,
    *,
    max_total_bytes: int = DEFAULT_MAX_RESTORE_BYTES,
) -> dict[str, Any]:
    archive_path = Path(archive_path)
    target_directory = Path(target_directory)
    if not archive_path.is_file() or archive_path.is_symlink():
        raise BackupError("备份文件不存在，或不是普通文件。")
    if target_directory.exists() or target_directory.is_symlink():
        raise BackupError("恢复目标必须是一个尚不存在的新目录；不会覆盖现有数据。")
    if max_total_bytes <= 0:
        raise BackupError("解压大小限制必须大于 0。")

    target_directory.parent.mkdir(parents=True, exist_ok=True)
    target_directory = target_directory.parent.resolve() / target_directory.name
    staging = Path(tempfile.mkdtemp(prefix=f".{target_directory.name}.restore-", dir=target_directory.parent))
    try:
        try:
            with zipfile.ZipFile(archive_path, "r") as archive:
                by_name, records, manifest = _validate_archive_entries(
                    archive, max_total_bytes=max_total_bytes
                )
                extracted = 0
                for name, record in records.items():
                    info = by_name[name]
                    extracted += _extract_verified(
                        archive,
                        info,
                        staging.joinpath(*PurePosixPath(name).parts),
                        record,
                        max_remaining_bytes=max_total_bytes - extracted,
                    )
                database = staging / DATABASE_FILENAME
                if not database.is_file():
                    raise BackupError("恢复后的数据库文件缺失。")
                (staging / "media").mkdir(exist_ok=True)
                try:
                    connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
                    try:
                        integrity = connection.execute("PRAGMA integrity_check").fetchone()
                    finally:
                        connection.close()
                except sqlite3.Error as error:
                    raise BackupError("恢复后的 SQLite 数据库无法读取。") from error
                if not integrity or integrity[0] != "ok":
                    raise BackupError("恢复后的数据库完整性检查失败。")
        except zipfile.BadZipFile as error:
            raise BackupError("备份不是有效的 ZIP 文件。") from error

        if target_directory.exists():
            raise BackupError("恢复目标在操作期间已创建；为避免覆盖，恢复已取消。")
        os.rename(staging, target_directory)
        return {
            "target": str(target_directory),
            "database": str(target_directory / DATABASE_FILENAME),
            "media": str(target_directory / "media"),
            "file_count": len(records),
            "total_bytes": sum(item["size"] for item in records.values()),
            "created_at": manifest.get("created_at"),
        }
    except BackupError:
        raise
    except OSError as error:
        raise BackupError(f"恢复备份失败：{error}") from error
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def verify_backup_archive(
    archive_path: Path,
    *,
    max_total_bytes: int = DEFAULT_MAX_RESTORE_BYTES,
) -> dict[str, Any]:
    """Validate every archive member and its SQLite snapshot without touching app data."""
    with tempfile.TemporaryDirectory(prefix="agent-platform-backup-verify-") as temporary_directory:
        result = restore_backup_archive(
            archive_path,
            Path(temporary_directory) / "verified",
            max_total_bytes=max_total_bytes,
        )
    return {
        "valid": True,
        "file_count": result["file_count"],
        "total_bytes": result["total_bytes"],
        "created_at": result["created_at"],
    }


def prune_backup_archives(
    directory: Path,
    *,
    keep: int,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Preview or remove old timestamped backups; unrelated ZIP files are ignored."""
    directory = Path(directory)
    if keep < 1:
        raise BackupError("至少需要保留 1 个备份。")
    if directory.is_symlink() or not directory.is_dir():
        raise BackupError("备份目录不存在，或不是普通目录。")

    candidates: list[Path] = []
    for path in directory.iterdir():
        if not TIMESTAMPED_BACKUP_NAME.fullmatch(path.name) or path.is_symlink():
            continue
        try:
            if stat.S_ISREG(path.lstat().st_mode):
                candidates.append(path)
        except OSError as error:
            raise BackupError(f"无法检查备份文件：{path.name}") from error

    candidates.sort(key=lambda path: path.name, reverse=True)
    old_backups = candidates[keep:]
    removed: list[str] = []
    if not dry_run:
        for path in old_backups:
            try:
                if path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode):
                    continue
                path.unlink()
                removed.append(str(path))
            except OSError as error:
                raise BackupError(f"无法删除过期备份：{path.name}") from error

    return {
        "directory": str(directory),
        "keep": keep,
        "kept": [str(path) for path in candidates[:keep]],
        "would_remove": [str(path) for path in old_backups],
        "removed": removed,
        "dry_run": dry_run,
    }
