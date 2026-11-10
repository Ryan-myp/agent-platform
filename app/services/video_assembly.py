from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse
from uuid import uuid4

from app.providers.media import AssetStore
from app.services.production import ScenePlanError, normalize_scene_plan
from app.store import ProjectStore

MAX_ASSEMBLY_BYTES = 512 * 1024 * 1024
MAX_CLIPS = 24
MAX_TOTAL_SECONDS = 1800
OUTPUT_ASPECT_RATIOS = frozenset({"9:16", "16:9", "1:1", "4:3", "3:4", "2:3", "3:2", "21:9"})
OUTPUT_PRESETS = {
    "compact": {"max_edge": 1280, "crf": "25", "audio_bitrate": "128k"},
    "standard": {"max_edge": 1920, "crf": "20", "audio_bitrate": "160k"},
    "quality": {"max_edge": 2560, "crf": "18", "audio_bitrate": "192k"},
}


class VideoAssemblyError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


class VideoAssemblyService:
    def __init__(
        self,
        store: ProjectStore,
        assets: AssetStore,
        *,
        ffmpeg_path: str | None = None,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        timeout_seconds: int = 900,
    ):
        self.store = store
        self.assets = assets
        self.ffmpeg_path = ffmpeg_path
        self.runner = runner
        self.timeout_seconds = timeout_seconds

    def _find_ffmpeg(self) -> str:
        if self.ffmpeg_path:
            candidate = Path(self.ffmpeg_path)
            if candidate.is_file():
                return str(candidate)
        found = shutil.which("ffmpeg")
        if found:
            return found
        try:
            import imageio_ffmpeg

            candidate = imageio_ffmpeg.get_ffmpeg_exe()
            if candidate and Path(candidate).is_file():
                return candidate
        except (ImportError, RuntimeError, OSError):
            pass
        raise VideoAssemblyError(
            "本机未找到 FFmpeg。请安装 FFmpeg 后重试；本地合成不会调用外部媒体供应商。", 503
        )

    def _get_project(self, project_id: str) -> dict[str, Any]:
        project = self.store.get(project_id)
        if not project:
            raise VideoAssemblyError("找不到这个创作项目。", 404)
        return project

    @staticmethod
    def _shots(project: dict[str, Any]) -> list[dict[str, Any]]:
        if project.get("mode") != "workflow":
            raise VideoAssemblyError("只有创作流程项目可以合成视频。", 409)
        scenes = (project.get("workflow") or {}).get("scenes") or {}
        if scenes.get("status") != "approved":
            raise VideoAssemblyError("请先确认镜头任务，再进行本地合成。", 409)
        shots = ((project.get("workflow") or {}).get("video") or {}).get("shots") or []
        if not shots:
            raise VideoAssemblyError("项目没有逐镜视频任务。", 409)
        if len(shots) > MAX_CLIPS:
            raise VideoAssemblyError(f"一次最多合成 {MAX_CLIPS} 个镜头片段。", 413)
        return shots

    @staticmethod
    def _order(project: dict[str, Any], shots: list[dict[str, Any]]) -> list[str]:
        shot_ids = [str(shot.get("shot_id") or "") for shot in shots]
        video = (project.get("workflow") or {}).get("video") or {}
        candidates = [video.get("render_order"), (video.get("edit_plan") or {}).get("sequence")]
        for candidate in candidates:
            if (
                isinstance(candidate, list)
                and len(candidate) == len(shot_ids)
                and len(set(candidate)) == len(shot_ids)
                and set(candidate) == set(shot_ids)
            ):
                return list(candidate)
        return shot_ids

    @staticmethod
    def _signature(project: dict[str, Any]) -> str:
        shots = VideoAssemblyService._shots(project)
        order = VideoAssemblyService._order(project, shots)
        by_id = {str(shot.get("shot_id")): shot for shot in shots}
        source = []
        for shot_id in order:
            shot = by_id.get(shot_id) or {}
            video = shot.get("video") or {}
            source.append({
                "shot_id": shot_id,
                "asset_url": video.get("asset_url"),
                "asset_status": video.get("status"),
                "asset_stale_reason": video.get("stale_reason"),
                "shot_stale": bool(shot.get("stale")),
                "asset_ids": sorted(str(item) for item in shot.get("asset_ids", [])),
                "video_prompt": shot.get("video_prompt"),
                "duration_seconds": shot.get("duration_seconds"),
                "aspect_ratio": shot.get("aspect_ratio"),
                "voiceover": shot.get("voiceover"),
                "screen_text": shot.get("screen_text"),
            })
        brief = project.get("brief") or {}
        source.append({"output_settings": {
            key: brief.get(key)
            for key in ("output_aspect_ratio", "output_frame_mode", "output_preset")
        }})
        scenes = (project.get("workflow") or {}).get("scenes") or {}
        source.append({"approved_scene_content": scenes.get("content") if scenes.get("status") == "approved" else None})
        import json

        return hashlib.sha256(json.dumps(source, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()

    def decorate(self, project: dict[str, Any]) -> dict[str, Any]:
        video = ((project.get("workflow") or {}).get("video") or {})
        final_video = video.get("final_video")
        if isinstance(final_video, dict) and final_video.get("status") == "ready":
            try:
                if final_video.get("source_signature") != self._signature(project):
                    final_video["status"] = "stale"
                    final_video["stale_reason"] = "镜头片段或剪辑顺序已更改，请重新合成。"
            except VideoAssemblyError:
                final_video["status"] = "stale"
                final_video["stale_reason"] = "当前镜头素材不完整，请重新合成。"
        return project

    def set_order(self, project_id: str, shot_ids: list[str]) -> dict[str, Any]:
        project = self._get_project(project_id)
        shots = self._shots(project)
        current_ids = [str(shot.get("shot_id") or "") for shot in shots]
        if (
            len(shot_ids) != len(current_ids)
            or len(set(shot_ids)) != len(shot_ids)
            or set(shot_ids) != set(current_ids)
        ):
            raise VideoAssemblyError("剪辑顺序必须恰好包含每个镜头编号一次。", 422)
        video = project["workflow"].setdefault("video", {})
        video["render_order"] = list(shot_ids)
        project["updated_at"] = _utc_now()
        self.store.save(project)
        return self.decorate(project)

    def update_settings(
        self,
        project_id: str,
        *,
        output_aspect_ratio: str,
        output_frame_mode: str,
        output_preset: str,
    ) -> dict[str, Any]:
        project = self._get_project(project_id)
        if output_aspect_ratio not in OUTPUT_ASPECT_RATIOS:
            raise VideoAssemblyError("不支持此输出画幅。", 422)
        if output_frame_mode not in {"pad", "crop"}:
            raise VideoAssemblyError("输出填充方式无效。", 422)
        if output_preset not in OUTPUT_PRESETS:
            raise VideoAssemblyError("不支持此合成预设。", 422)
        brief = project.setdefault("brief", {})
        values = {
            "output_aspect_ratio": output_aspect_ratio,
            "output_frame_mode": output_frame_mode,
            "output_preset": output_preset,
        }
        if any(brief.get(key) != value for key, value in values.items()):
            brief.update(values)
            project["updated_at"] = _utc_now()
            project = self.store.save(project)
        return self.decorate(project)

    @staticmethod
    def _subtitle_cues(project: dict[str, Any]) -> list[dict[str, Any]]:
        scenes = (project.get("workflow") or {}).get("scenes") or {}
        if scenes.get("status") != "approved":
            raise VideoAssemblyError("请先确认镜头任务，再生成字幕。", 409)
        content = scenes.get("content")
        if not isinstance(content, str) or not content.strip():
            raise VideoAssemblyError("已确认的镜头任务中没有可用内容。", 422)
        try:
            shots = normalize_scene_plan(content)["shots"]
        except ScenePlanError as error:
            raise VideoAssemblyError(f"无法从已确认的镜头任务生成字幕：{error}", 422) from error
        cues: list[dict[str, Any]] = []
        cursor = 0.0
        for shot in shots:
            duration = int(shot["duration_seconds"])
            text = "\n".join(
                value for value in (
                    str(shot.get("voiceover") or "").replace("\x00", "").strip(),
                    str(shot.get("screen_text") or "").replace("\x00", "").strip(),
                )
                if value
            )
            if text:
                cues.append({"start": cursor, "end": cursor + duration, "text": text})
            cursor += duration
        return cues

    @staticmethod
    def _srt_timestamp(seconds: float) -> str:
        milliseconds = max(0, round(seconds * 1000))
        hours, remainder = divmod(milliseconds, 3_600_000)
        minutes, remainder = divmod(remainder, 60_000)
        whole_seconds, millis = divmod(remainder, 1000)
        return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{millis:03d}"

    @staticmethod
    def _ass_timestamp(seconds: float) -> str:
        centiseconds = max(0, round(seconds * 100))
        hours, remainder = divmod(centiseconds, 360_000)
        minutes, remainder = divmod(remainder, 6_000)
        whole_seconds, fraction = divmod(remainder, 100)
        return f"{hours}:{minutes:02d}:{whole_seconds:02d}.{fraction:02d}"

    @staticmethod
    def _ass_text(text: str) -> str:
        return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}").replace("\r\n", "\n").replace("\r", "\n").replace("\n", r"\N")

    @classmethod
    def _render_srt(cls, cues: list[dict[str, Any]]) -> str:
        return "\n\n".join(
            f"{index}\n{cls._srt_timestamp(cue['start'])} --> {cls._srt_timestamp(cue['end'])}\n{cue['text'].replace(chr(13), '').strip()}"
            for index, cue in enumerate(cues, start=1)
        ) + "\n"

    def generate_subtitles(self, project_id: str, subtitle_format: str) -> str:
        if subtitle_format not in {"srt", "ass"}:
            raise VideoAssemblyError("字幕格式仅支持 SRT 或 ASS。", 404)
        project = self._get_project(project_id)
        cues = self._subtitle_cues(project)
        if not cues:
            raise VideoAssemblyError("镜头任务没有旁白或屏幕文字，无法生成字幕。", 422)
        if subtitle_format == "srt":
            return self._render_srt(cues)
        header = """[Script Info]
ScriptType: v4.00+
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,42,&H00FFFFFF,&H0000FFFF,&H00101010,&H80000000,0,0,0,0,100,100,0,0,1,2,0,2,56,56,48,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
        events = [
            f"Dialogue: 0,{self._ass_timestamp(cue['start'])},{self._ass_timestamp(cue['end'])},Default,,0,0,0,,{self._ass_text(cue['text'])}"
            for cue in cues
        ]
        return header + "\n".join(events) + "\n"

    def _local_media_path(self, asset_url: Any) -> Path:
        if not isinstance(asset_url, str):
            raise VideoAssemblyError("镜头视频没有本地媒体地址。", 422)
        parsed = urlparse(asset_url)
        if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
            raise VideoAssemblyError("只允许引用本地媒体文件，拒绝远程视频地址。", 422)
        prefix = f"{self.assets.public_prefix}/"
        if not parsed.path.startswith(prefix):
            raise VideoAssemblyError("镜头视频必须来自本地媒体存储。", 422)
        name = parsed.path[len(prefix):]
        if not name or Path(name).name != name or name in {".", ".."} or "\\" in name:
            raise VideoAssemblyError("本地视频文件路径无效。", 422)
        root = self.assets.directory.resolve()
        candidate = (root / name).resolve(strict=False)
        try:
            candidate.relative_to(root)
        except ValueError as error:
            raise VideoAssemblyError("本地视频文件路径越界。", 422) from error
        if not candidate.is_file():
            raise VideoAssemblyError("镜头引用的本地视频文件不存在。", 422)
        return candidate

    def _probe(self, ffmpeg: str, path: Path) -> dict[str, Any]:
        try:
            result = self.runner(
                [ffmpeg, "-hide_banner", "-nostdin", "-i", str(path), "-frames:v", "1", "-f", "null", "-"],
                capture_output=True,
                text=True,
                timeout=60,
                stdin=subprocess.DEVNULL,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise VideoAssemblyError("读取镜头视频信息超时。", 422) from error
        except OSError as error:
            raise VideoAssemblyError(f"无法读取本地镜头视频：{error}", 422) from error
        diagnostic = (result.stderr or "")
        video_line = next((line for line in diagnostic.splitlines() if "Video:" in line and "Stream #" in line), "")
        duration_match = re.search(r"Duration:\s*(\d{2}):(\d{2}):(\d{2}(?:\.\d+)?)", diagnostic)
        dimensions = re.search(r"(\d{2,5})x(\d{2,5})", video_line)
        if result.returncode != 0 or not video_line or not duration_match or not dimensions:
            raise VideoAssemblyError("无法解码镜头视频或读取其时长 / 尺寸；请检查片段后重试。", 422)
        hours, minutes, seconds = duration_match.groups()
        duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        width, height = map(int, dimensions.groups())
        if duration <= 0 or width <= 0 or height <= 0:
            raise VideoAssemblyError("镜头视频时长或尺寸无效。", 422)
        return {
            "duration": duration,
            "width": width,
            "height": height,
            "has_audio": any("Audio:" in line and "Stream #" in line for line in diagnostic.splitlines()),
        }

    def assemble(self, project_id: str) -> dict[str, Any]:
        project = self._get_project(project_id)
        expected_updated_at = str(project.get("updated_at") or "")
        source_signature = self._signature(project)
        shots = self._shots(project)
        order = self._order(project, shots)
        by_id = {str(shot.get("shot_id")): shot for shot in shots}
        paths: list[Path] = []
        sizes = 0
        for shot_id in order:
            shot = by_id[shot_id]
            video = shot.get("video") or {}
            if video.get("status") != "ready" or video.get("stale_reason") or shot.get("stale"):
                raise VideoAssemblyError(f"镜头 {shot_id} 的视频片段尚未确认或已过期。", 409)
            path = self._local_media_path(video.get("asset_url"))
            sizes += path.stat().st_size
            if sizes > MAX_ASSEMBLY_BYTES:
                raise VideoAssemblyError("镜头视频总大小超过 512 MiB 限制。", 413)
            paths.append(path)
        ffmpeg = self._find_ffmpeg()
        probes = [self._probe(ffmpeg, path) for path in paths]
        total_duration = sum(item["duration"] for item in probes)
        if total_duration > MAX_TOTAL_SECONDS:
            raise VideoAssemblyError("视频总时长超过本地合成的 30 分钟限制。", 413)
        brief = project.get("brief") or {}
        preset_name = str(brief.get("output_preset") or "standard")
        preset = OUTPUT_PRESETS.get(preset_name, OUTPUT_PRESETS["standard"])
        frame_mode = str(brief.get("output_frame_mode") or "pad")
        if frame_mode not in {"pad", "crop"}:
            frame_mode = "pad"
        aspect_ratio = brief.get("output_aspect_ratio")
        max_edge = int(preset["max_edge"])
        if aspect_ratio in OUTPUT_ASPECT_RATIOS:
            ratio_width, ratio_height = (int(value) for value in aspect_ratio.split(":"))
            ratio_scale = max_edge / max(ratio_width, ratio_height)
            width = max(2, int(ratio_width * ratio_scale) // 2 * 2)
            height = max(2, int(ratio_height * ratio_scale) // 2 * 2)
        else:
            scale = min(1.0, max_edge / max(probes[0]["width"], probes[0]["height"]))
            width = max(2, int(probes[0]["width"] * scale) // 2 * 2)
            height = max(2, int(probes[0]["height"] * scale) // 2 * 2)
            aspect_ratio = f"{width}:{height}"
        filters: list[str] = []
        concat_inputs: list[str] = []
        for index, probe in enumerate(probes):
            duration = f"{probe['duration']:.3f}"
            frame_filter = (
                f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}"
                if frame_mode == "crop"
                else f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black"
            )
            filters.append(
                f"[{index}:v:0]fps=30,{frame_filter},setsar=1,format=yuv420p,"
                f"trim=duration={duration},setpts=PTS-STARTPTS[v{index}]"
            )
            if probe["has_audio"]:
                filters.append(
                    f"[{index}:a:0]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                    f"apad,atrim=duration={duration},asetpts=PTS-STARTPTS[a{index}]"
                )
            else:
                filters.append(
                    f"anullsrc=r=48000:cl=stereo,atrim=duration={duration},asetpts=PTS-STARTPTS[a{index}]"
                )
            concat_inputs.extend((f"[v{index}]", f"[a{index}]"))
        filters.append(f"{''.join(concat_inputs)}concat=n={len(probes)}:v=1:a=1[vout][aout]")
        subtitle_cues = self._subtitle_cues(project)
        with tempfile.TemporaryDirectory(prefix="agent-platform-render-") as temporary:
            output = Path(temporary) / "assembled.mp4"
            if subtitle_cues:
                subtitle_path = Path(temporary) / "subtitles.srt"
                subtitle_path.write_text(self._render_srt(subtitle_cues), encoding="utf-8")
                escaped_subtitle_path = str(subtitle_path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
                filters.append(f"[vout]subtitles=filename='{escaped_subtitle_path}'[vsub]")
            command = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-filter_complex_threads", "1"]
            for path in paths:
                command.extend(("-i", str(path)))
            command.extend((
                "-filter_complex", ";".join(filters), "-map", "[vsub]" if subtitle_cues else "[vout]", "-map", "[aout]",
                "-c:v", "libx264", "-preset", "medium", "-crf", str(preset["crf"]), "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", str(preset["audio_bitrate"]), "-movflags", "+faststart",
                "-fs", str(MAX_ASSEMBLY_BYTES), str(output),
            ))
            try:
                result = self.runner(
                    command, capture_output=True, text=True, timeout=self.timeout_seconds,
                    stdin=subprocess.DEVNULL, check=False,
                )
            except subprocess.TimeoutExpired as error:
                raise VideoAssemblyError("本地视频合成超时；原始镜头片段未被修改。", 504) from error
            except OSError as error:
                raise VideoAssemblyError(f"无法启动 FFmpeg：{error}", 503) from error
            if result.returncode != 0 or not output.is_file():
                detail = (result.stderr or "").strip()[-1200:]
                raise VideoAssemblyError(f"FFmpeg 合成失败；原始镜头片段未被修改。{f' {detail}' if detail else ''}", 502)
            output_size = output.stat().st_size
            if output_size < 32 or output_size > MAX_ASSEMBLY_BYTES:
                raise VideoAssemblyError("合成文件大小无效或超过 512 MiB 限制。", 413)
            with output.open("rb") as source:
                header = source.read(64)
            if b"ftyp" not in header:
                raise VideoAssemblyError("合成结果不是可识别的 MP4 文件。", 502)
            verify = self.runner(
                [ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-i", str(output), "-frames:v", "1", "-f", "null", "-"],
                capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL, check=False,
            )
            if verify.returncode != 0:
                raise VideoAssemblyError("合成结果未通过解码校验；原始镜头片段未被修改。", 502)
            verified = self._probe(ffmpeg, output)
            duration_tolerance = max(1.0, total_duration * 0.001)
            if (
                abs(verified["duration"] - total_duration) > duration_tolerance
                or verified["width"] != width
                or verified["height"] != height
            ):
                raise VideoAssemblyError("合成结果时长或画幅与预期不符，可能不完整；原始镜头片段未被修改。", 502)
            digest = hashlib.sha256()
            with output.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
            current = self._get_project(project_id)
            if (
                str(current.get("updated_at") or "") != expected_updated_at
                or self._signature(current) != source_signature
            ):
                raise VideoAssemblyError("合成期间项目或镜头素材发生变化；未保存过期结果，请重新合成。", 409)
            filename = f"{uuid4().hex}.mp4"
            media_root = self.assets.directory.resolve()
            temporary_target = media_root / f".{filename}.tmp"
            final_target = media_root / filename
            try:
                shutil.copyfile(output, temporary_target)
                os.replace(temporary_target, final_target)
            except OSError:
                temporary_target.unlink(missing_ok=True)
                raise

        current = self._get_project(project_id)
        if (
            str(current.get("updated_at") or "") != expected_updated_at
            or self._signature(current) != source_signature
        ):
            final_target.unlink(missing_ok=True)
            raise VideoAssemblyError("合成期间项目发生变化；未保存过期结果，请重新合成。", 409)
        video = current["workflow"].setdefault("video", {})
        old = video.get("final_video")
        if isinstance(old, dict) and old.get("asset_url"):
            versions = video.setdefault("final_video_versions", [])
            versions.append(old)
            del versions[:-10]
        record = {
            "status": "ready",
            "asset_url": f"{self.assets.public_prefix}/{filename}",
            "order": order,
            "source_signature": source_signature,
            "sha256": digest.hexdigest(),
            "size_bytes": output_size,
            "duration_seconds": round(total_duration, 3),
            "width": width,
            "height": height,
            "output_aspect_ratio": aspect_ratio,
            "frame_mode": frame_mode,
            "preset": preset_name,
            "subtitles_burned": bool(subtitle_cues),
            "subtitle_cue_count": len(subtitle_cues),
            "rendered_at": _utc_now(),
        }
        video["final_video"] = record
        current["updated_at"] = record["rendered_at"]
        try:
            saved = self.store.save_if_updated_at(current, expected_updated_at)
        except Exception:
            final_target.unlink(missing_ok=True)
            raise
        if not saved:
            final_target.unlink(missing_ok=True)
            raise VideoAssemblyError("项目在保存合成结果时发生变化；未覆盖最新内容，请重新合成。", 409)
        return self.decorate(current)


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
