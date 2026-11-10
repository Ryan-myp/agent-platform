from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from app.services.backup import (
    DEFAULT_MAX_RESTORE_BYTES,
    BackupError,
    create_backup_archive,
    prune_backup_archives,
    restore_backup_archive,
    verify_backup_archive,
)


def _default_database_path() -> Path:
    return Path(os.getenv("DATABASE_PATH", "data/agent-platform.db"))


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("必须是正整数。") from error
    if parsed < 1:
        raise argparse.ArgumentTypeError("必须是正整数。")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.backup",
        description="导出或恢复片场创作台的 SQLite 数据库与本地媒体。",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="创建 ZIP 备份（不包含模型凭据）")
    create.add_argument("--database", type=Path, default=_default_database_path())
    create.add_argument("--media", type=Path, help="媒体目录；默认是数据库同级的 media/")
    create.add_argument("--directory", type=Path, default=Path("backups"), help="省略 --output 时的备份目录")
    create.add_argument("--output", type=Path, help="输出文件；默认使用带 UTC 时间戳的文件名")
    create.add_argument("--keep", type=_positive_int, help="成功备份后只保留最新 N 个时间戳备份")

    restore = subparsers.add_parser("restore", help="恢复到一个新目录，不覆盖现有数据")
    restore.add_argument("archive", type=Path, help="create 子命令生成的 ZIP 文件")
    restore.add_argument("--target", type=Path, required=True, help="尚不存在的恢复目标目录")
    restore.add_argument(
        "--max-restore-gb",
        type=_positive_int,
        default=max(1, DEFAULT_MAX_RESTORE_BYTES // (1024**3)),
        help="解压总大小上限（GiB）",
    )

    verify = subparsers.add_parser("verify", help="校验 ZIP、SHA-256 和 SQLite 完整性，不恢复到应用目录")
    verify.add_argument("archive", type=Path, help="待校验的 ZIP 备份")
    verify.add_argument(
        "--max-restore-gb",
        type=_positive_int,
        default=max(1, DEFAULT_MAX_RESTORE_BYTES // (1024**3)),
        help="临时校验目录的解压大小上限（GiB）",
    )

    prune = subparsers.add_parser("prune", help="预览或清理旧的时间戳备份")
    prune.add_argument("--directory", type=Path, default=Path("backups"))
    prune.add_argument("--keep", type=_positive_int, default=7, help="保留最新备份数（默认 7）")
    prune.add_argument("--confirm", action="store_true", help="确认删除预览中的旧备份；默认只预览")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            if args.keep is not None and args.output is not None:
                parser.error("--keep 仅适用于自动生成的时间戳备份；请省略 --output。")
            output = args.output or (
                args.directory
                / f"agent-platform-backup-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.zip"
            )
            media_directory = args.media or args.database.parent / "media"
            result = create_backup_archive(args.database, media_directory, output)
            if args.keep is not None:
                result["retention"] = prune_backup_archives(output.parent, keep=args.keep, dry_run=False)
        elif args.command == "restore":
            result = restore_backup_archive(
                args.archive,
                args.target,
                max_total_bytes=args.max_restore_gb * 1024**3,
            )
        elif args.command == "verify":
            result = verify_backup_archive(
                args.archive,
                max_total_bytes=args.max_restore_gb * 1024**3,
            )
        else:
            result = prune_backup_archives(args.directory, keep=args.keep, dry_run=not args.confirm)
    except BackupError as error:
        print(f"备份操作失败：{error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
