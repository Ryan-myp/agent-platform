from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import threading
from urllib.parse import urlparse
from uuid import uuid4

from app.config import Settings
from app.providers.catalog import ModelProfile
from app.providers.media import (
    AgnesVideoProvider,
    AssetStore,
    ImageProvider,
    MediaProviderError,
    VideoProvider,
    VideoSubmissionUncertainError,
    create_image_provider,
)
from app.providers.text import create_text_provider
from app.stages import STAGE_IDS, STAGES, TRACK_INSTRUCTIONS, TRACK_LABELS, TRACK_STAGE_INSTRUCTIONS
from app.store import ProjectStore, VideoJobConflict
from app.skills.loader import SkillLoader
from app.services.production import (
    ScenePlanError,
    new_shot_record,
    normalize_scene_plan,
    serialize_scene_plan,
)


class WorkflowError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


_BLOCKING_CONTENT_PATTERNS = (
    re.compile(r"(?:暂)?无法(?:直接|继续)?(?:撰写|生成)"),
    re.compile(r"(?:核心字段|关键信息).{0,30}(?:缺失|不足|模糊)"),
    re.compile(r"(?:缺失|不足|模糊).{0,40}无法"),
    re.compile(r"必须补充.{0,30}才能(?:继续|生成)"),
)


def _has_blocking_content(content: str) -> bool:
    return any(pattern.search(content) for pattern in _BLOCKING_CONTENT_PATTERNS)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class WorkflowService:
    def __init__(
        self,
        store: ProjectStore,
        settings: Settings,
        skills: SkillLoader,
        video_provider: VideoProvider | None = None,
        image_provider: ImageProvider | None = None,
        assets: AssetStore | None = None,
    ):
        self.store = store
        self.settings = settings
        self.skills = skills
        self.video_provider_override = video_provider
        self.image_provider_override = image_provider
        self.assets = assets or AssetStore(settings.database_path.parent / "media")
        self._media_jobs_lock = threading.Lock()
        self._active_media_jobs: set[tuple[str, str, str]] = set()
        self.video_job_worker = None

    def create_project(self, data: dict) -> dict:
        mode = data.pop("mode", "workflow")
        title = data.pop("title", None)
        track = data.pop("track", "marketing")
        if track not in TRACK_LABELS:
            raise WorkflowError("未知的创作类型。", 422)
        model = data.pop("model", None) or self.settings.model_catalog.defaults["text"]
        self._validate_model(model)
        if mode not in {"chat", "workflow"}:
            raise WorkflowError("未知的项目模式。", 422)
        brief = {} if mode == "chat" else data
        created = now_iso()
        project = {
            "id": str(uuid4()),
            "title": ((title or "").strip() or "新对话") if mode == "chat" else data["product"].strip() or ("短剧 / 漫剧创作" if track == "drama" else "营销视频创作"),
            "mode": mode,
            "track": track,
            "brief": brief,
            "model": model,
            "active_stage": STAGE_IDS[0],
            "workflow": {
                stage_id: {
                    "status": "pending",
                    "content": None,
                    "updated_at": None,
                    **({"asset_url": None, "shots": [], "edit_plan": {}} if stage_id == "video" else {}),
                }
                for stage_id in STAGE_IDS
            },
            "created_at": created,
            "updated_at": created,
        }
        return self.store.save(project)

    def get_project(self, project_id: str) -> dict:
        project = self.store.get(project_id)
        if not project:
            raise WorkflowError("找不到这个创作项目。", 404)
        return project

    def list_projects(self) -> list[dict]:
        return self.store.list()

    def set_model(self, project_id: str, model: str) -> dict:
        self._validate_model(model)
        project = self.get_project(project_id)
        project["model"] = model
        project["updated_at"] = now_iso()
        return self.store.save(project)

    def _validate_model(self, model: str) -> ModelProfile:
        try:
            return self.settings.model_catalog.resolve(model, "text")
        except ValueError as exc:
            raise WorkflowError(str(exc), 422) from exc

    def _archive_stage_content(self, item: dict, reason: str) -> None:
        content = item.get("content")
        if not content:
            return
        revisions = item.setdefault("revisions", [])
        if revisions and revisions[-1].get("content") == content:
            return
        revisions.append({
            "revision_id": str(uuid4()),
            "content": content,
            "status": item.get("status"),
            "updated_at": item.get("updated_at"),
            "archived_at": now_iso(),
            "reason": reason,
        })
        del revisions[:-8]

    @staticmethod
    def _stage_has_data(item: dict) -> bool:
        return bool(item.get("content") or item.get("asset_url") or item.get("shots"))

    def _mark_downstream_stale(self, project: dict, stage: str, reason: str) -> None:
        start = STAGE_IDS.index(stage) + 1
        for stage_id in STAGE_IDS[start:]:
            item = project["workflow"].setdefault(stage_id, {})
            if self._stage_has_data(item):
                item["status"] = "stale"
                item["stale_reason"] = reason
                item["stale_since"] = now_iso()
            else:
                item["status"] = "pending"
            if stage_id != "video":
                continue
            for shot in item.get("shots", []):
                shot["stale"] = True
                for kind in ("image", "video"):
                    asset = shot.setdefault(kind, {})
                    if asset.get("status") != "stale":
                        asset["stale_status"] = asset.get("status", "pending")
                        asset["status"] = "stale"
                    asset["stale_reason"] = reason

    def _rollback_project(self, project: dict, stage: str) -> None:
        item = project["workflow"][stage]
        item["status"] = "ready"
        item.pop("stale_reason", None)
        item.pop("stale_since", None)
        item["updated_at"] = now_iso()
        project["active_stage"] = stage
        self._mark_downstream_stale(
            project,
            stage,
            f"“{next(step['label'] for step in STAGES if step['id'] == stage)}”已回退或修改，请重新审阅后续内容。",
        )
        project["updated_at"] = now_iso()

    def rollback_to_stage(self, project_id: str, stage: str) -> dict:
        if stage not in STAGE_IDS:
            raise WorkflowError("未知的创作步骤。", 404)
        project = self.get_project(project_id)
        active_index = STAGE_IDS.index(project["active_stage"])
        if STAGE_IDS.index(stage) >= active_index:
            raise WorkflowError("只能回退到已确认的前序步骤。", 409)
        item = project["workflow"].get(stage, {})
        if item.get("status") not in {"approved", "ready", "stale"} or not item.get("content"):
            raise WorkflowError("只能回退到已有内容的前序步骤。", 409)
        self._rollback_project(project, stage)
        return self.store.save(project)

    def _reconcile_video_shots(self, video: dict, shot_specs: list[dict], edit_plan: dict) -> None:
        old_shots = video.get("shots", [])
        old_by_id = {shot.get("shot_id"): shot for shot in old_shots}
        new_shots = []
        retained_ids = set()
        for spec in shot_specs:
            shot = new_shot_record(spec)
            previous = old_by_id.get(spec["shot_id"])
            if previous is not None:
                retained_ids.add(spec["shot_id"])
                changed = any(previous.get(key) != spec.get(key) for key in spec)
                for kind in ("image", "video"):
                    asset = deepcopy(previous.get(kind) or shot[kind])
                    if changed and (asset.get("asset_url") or asset.get("versions")):
                        if asset.get("status") != "stale":
                            asset["stale_status"] = asset.get("status", "ready")
                        asset["status"] = "stale"
                        asset["stale_reason"] = "镜头任务已修改，需要重新生成此素材。"
                    elif not changed and asset.get("status") == "stale":
                        asset["status"] = asset.pop(
                            "stale_status", "ready" if asset.get("asset_url") else "pending"
                        )
                        asset.pop("stale_reason", None)
                    shot[kind] = asset
                if changed and any(shot[kind].get("status") == "stale" for kind in ("image", "video")):
                    shot["stale"] = True
                else:
                    shot.pop("stale", None)
            new_shots.append(shot)

        retired = [deepcopy(shot) for shot in old_shots if shot.get("shot_id") not in retained_ids]
        if retired:
            history = video.setdefault("retired_shots", [])
            history.append({"archived_at": now_iso(), "shots": retired})
            del history[:-10]
        if video.get("asset_url"):
            legacy = video.setdefault("legacy_versions", [])
            legacy.append({"asset_url": video["asset_url"], "archived_at": now_iso()})
            del legacy[:-10]
        video["asset_url"] = None
        video["shots"] = new_shots
        video["edit_plan"] = deepcopy(edit_plan)
        has_stale_assets = any(
            shot.get(kind, {}).get("status") == "stale"
            for shot in new_shots
            for kind in ("image", "video")
        )
        video.pop("stale_reason", None)
        if has_stale_assets:
            video["status"] = "stale"
        else:
            video_statuses = [shot.get("video", {}).get("status") for shot in new_shots]
            if video_statuses and all(status == "ready" for status in video_statuses):
                video["status"] = "ready"
            elif any(status in {"ready", "generating"} for status in video_statuses):
                video["status"] = "partial"
            else:
                video["status"] = "pending"

    def _blocking_upstream_stages(self, project: dict, stage: str) -> list[str]:
        target_index = STAGE_IDS.index(stage)
        return [
            stage_id
            for stage_id in STAGE_IDS[:target_index]
            if _has_blocking_content(project.get("workflow", {}).get(stage_id, {}).get("content") or "")
        ]

    def generate(self, project_id: str, stage: str, model: str | None = None) -> dict:
        if stage not in STAGE_IDS:
            raise WorkflowError("未知的创作步骤。", 404)
        project = self.get_project(project_id)
        if project["active_stage"] != stage:
            raise WorkflowError("请先完成并确认前一个步骤，再继续生成。", 409)
        blocked_stages = self._blocking_upstream_stages(project, stage)
        if blocked_stages:
            labels = {
                item["id"]: item["label"]
                for item in STAGES
            }
            blocked_labels = "、".join(labels.get(stage_id, stage_id) for stage_id in blocked_stages)
            raise WorkflowError(
                f"无法生成“{labels.get(stage, stage)}”：已确认的{blocked_labels}仍包含信息不足或无法继续等阻塞说明。"
                "请回退到最早的阻塞步骤，补充真实项目 brief 或编辑草稿后重新确认；本次未调用模型。",
                409,
            )
        if stage == "video":
            video_workflow = project.get("workflow", {}).get("video", {})
            if video_workflow.get("shots"):
                raise WorkflowError("当前项目已启用逐镜生成；请在镜头卡片中单独生成或重试视频片段。", 409)
            provider = self._get_video_provider()
            try:
                result = provider.generate_video(project=project)
            except (MediaProviderError, RuntimeError) as exc:
                raise WorkflowError(str(exc), 502) from exc
            asset_url = result.get("asset_url")
            if not asset_url:
                raise WorkflowError("视频服务未返回成片地址。", 502)
            project["workflow"][stage] = {
                "status": "ready",
                "content": result.get("summary", "成片已生成"),
                "asset_url": asset_url,
                "task_id": result.get("task_id"),
                "cost": result.get("cost") if isinstance(result.get("cost"), (str, int, float)) else None,
                "updated_at": now_iso(),
            }
            project["updated_at"] = now_iso()
            return self.store.save(project)

        selected_model = model or project["model"]
        profile = self._validate_model(selected_model)
        api_key = self.settings.api_key_for(profile)
        if not self.settings.profile_is_configured(profile):
            raise WorkflowError(
                f"模型 {profile.name} 未配置 API Key，请在模型配置中填写。", 503
            )
        try:
            provider = create_text_provider(profile, api_key)
        except RuntimeError as exc:
            raise WorkflowError(str(exc), 503) from exc
        previous = {
            stage_id: project["workflow"][stage_id]["content"]
            for stage_id in STAGE_IDS
            if STAGE_IDS.index(stage_id) < STAGE_IDS.index(stage)
            and project["workflow"][stage_id]["content"]
        }
        track = project.get("track", "marketing")
        skill_context = self.skills.for_stage(stage, track)
        track_instruction = TRACK_INSTRUCTIONS.get(track, TRACK_INSTRUCTIONS["marketing"])
        stage_instruction = TRACK_STAGE_INSTRUCTIONS.get(track, TRACK_STAGE_INSTRUCTIONS["marketing"])[stage]
        system = (
            f"你是{TRACK_LABELS.get(track, TRACK_LABELS['marketing'])}创作工作台中的专业内容策划。"
            "遵守用户提供的事实，不捏造数据、效果、证言或产品属性；输出可直接审阅的中文内容。\n\n"
            f"当前创作类型：{TRACK_LABELS.get(track, TRACK_LABELS['marketing'])}\n"
            f"类型要求：{track_instruction}\n当前步骤：{stage}\n要求：{stage_instruction}\n\n"
            f"适用的本地创作技能：\n{skill_context or '无'}"
        )
        brief = project["brief"]
        if track == "drama":
            brief_context = {
                "故事背景 / 核心设定": brief.get("business", ""),
                "作品 / 项目名": brief.get("product", ""),
                "目标观众 / 题材偏好": brief.get("audience", ""),
                "发布平台 / 画幅方向": brief.get("platform", ""),
                "单集目标时长（秒）": brief.get("duration_seconds"),
                "视觉风格 / 媒介": brief.get("style", ""),
            }
            brief_label = "短剧 / 漫剧创作 brief"
        else:
            brief_context = brief
            brief_label = "品牌营销 brief"
        user = (
            f"stage={stage}\n"
            f"{brief_label}：{brief_context}\n"
            f"已确认的前序内容：{previous or '无'}\n"
            "请根据这些信息完成当前步骤。若 brief 缺少必要信息，请在结果中标出待确认项，不要自行杜撰。"
        )
        try:
            content = provider.generate(system=system, user=user)
            scene_plan = normalize_scene_plan(content) if stage == "scenes" else None
            if scene_plan is not None:
                content = serialize_scene_plan(scene_plan)
        except ScenePlanError as exc:
            raise WorkflowError(str(exc), 422) from exc
        except Exception as exc:
            raise WorkflowError(f"文本模型调用失败：{exc}", 502) from exc
        project["model"] = selected_model
        previous_item = project["workflow"][stage]
        self._archive_stage_content(previous_item, "重新生成前的草稿")
        project["workflow"][stage] = {
            "status": "ready",
            "content": content,
            "updated_at": now_iso(),
            **({"revisions": previous_item["revisions"]} if previous_item.get("revisions") else {}),
            **({"shots": scene_plan["shots"], "edit_plan": scene_plan["edit_plan"]} if scene_plan else {}),
        }
        project["updated_at"] = now_iso()
        return self.store.save(project)

    def _get_image_provider(self) -> ImageProvider:
        if self.image_provider_override is not None:
            return self.image_provider_override
        profile_id = self.settings.model_catalog.defaults.get("image")
        if not profile_id:
            raise WorkflowError("尚未配置默认图像模型，请编辑 config/models.json。", 503)
        profile = self.settings.model_catalog.resolve(profile_id, "image")
        if not self.settings.profile_is_configured(profile):
            raise WorkflowError(
                f"图像模型 {profile.name} 未配置 API Key，请在模型配置中填写。", 503
            )
        try:
            return create_image_provider(profile, self.settings.api_key_for(profile), self.assets)
        except MediaProviderError as exc:
            raise WorkflowError(str(exc), 501) from exc

    def _get_video_provider(self) -> VideoProvider:
        if self.video_provider_override is not None:
            if not self.video_provider_override.ready:
                raise WorkflowError("视频生成服务尚未配置。", 503)
            return self.video_provider_override
        profile_id = self.settings.model_catalog.defaults.get("video")
        if not profile_id:
            raise WorkflowError("尚未配置默认视频模型，请编辑 config/models.json。", 503)
        profile = self.settings.model_catalog.resolve(profile_id, "video")
        if not self.settings.profile_is_configured(profile):
            raise WorkflowError(
                f"视频模型 {profile.name} 未配置 API Key，请在模型配置中填写。", 503
            )
        if profile.adapter != "agnes_video":
            raise WorkflowError(f"视频适配器尚未实现：{profile.adapter}", 501)
        return AgnesVideoProvider(profile, self.settings.api_key_for(profile))

    def _get_shot(self, project: dict, shot_id: str) -> dict:
        if project.get("active_stage") != "video" or project.get("workflow", {}).get("scenes", {}).get("status") != "approved":
            raise WorkflowError("请先确认场景任务，再生成镜头素材。", 409)
        video = project.setdefault("workflow", {}).setdefault("video", {})
        shot = next((item for item in video.get("shots", []) if item.get("shot_id") == shot_id), None)
        if shot is None:
            raise WorkflowError("找不到这个镜头任务；请先确认结构化场景任务。", 404)
        return shot

    def _generate_shot_asset(self, project_id: str, shot_id: str, kind: str) -> dict:
        if kind not in {"image", "video"}:
            raise WorkflowError("不支持的镜头素材类型。", 404)
        project = self.get_project(project_id)
        shot = self._get_shot(project, shot_id)
        provider = self._get_image_provider() if kind == "image" else self._get_video_provider()
        profile_id = self.settings.model_catalog.defaults.get(kind)
        job_key = (project_id, shot_id, kind)
        with self._media_jobs_lock:
            if job_key in self._active_media_jobs:
                raise WorkflowError("这个镜头正在生成，请稍候。", 409)
            self._active_media_jobs.add(job_key)
        asset = shot.setdefault(kind, {})
        asset.update({
            "status": "generating",
            "model": profile_id or "injected-provider",
            "requested_at": now_iso(),
            "completed_at": None,
            "task_id": None,
            "cost": None,
            "error": None,
        })
        project["updated_at"] = now_iso()
        try:
            self.store.save(project)
            if kind == "image":
                prompt = shot["image_prompt"]
                if shot.get("negative_prompt"):
                    prompt += f"\n\n避免：{shot['negative_prompt']}"
                result = provider.generate_image(prompt=prompt, aspect_ratio=shot["aspect_ratio"])
            else:
                image_asset = shot.get("image") or {}
                image_url = image_asset.get("asset_url") or "" if image_asset.get("status") == "ready" else ""
                parsed_image_url = urlparse(image_url)
                references = [image_url] if parsed_image_url.scheme in {"http", "https"} and parsed_image_url.hostname else []
                shot_project = {
                    **project,
                    "brief": {**project.get("brief", {}), "duration_seconds": shot["duration_seconds"]},
                    "video_prompt": shot["video_prompt"],
                    "aspect_ratio": shot["aspect_ratio"],
                    "reference_images": references,
                }
                result = provider.generate_video(project=shot_project)
            asset_url = result.get("asset_url")
            if not asset_url:
                raise MediaProviderError(f"{kind}服务未返回素材地址。")
            completed_at = now_iso()
            cost = result.get("cost") if isinstance(result.get("cost"), (str, int, float)) else None
            task_id = result.get("task_id")
            version = {
                "version": len(asset.get("versions", [])) + 1,
                "asset_url": asset_url,
                "model": asset.get("model"),
                "requested_at": asset.get("requested_at"),
                "completed_at": completed_at,
                "task_id": task_id,
                "cost": cost,
            }
            asset.setdefault("versions", []).append(version)
            asset.update({
                "status": "ready",
                "asset_url": asset_url,
                "completed_at": completed_at,
                "task_id": task_id,
                "cost": cost,
                "error": None,
            })
            asset.pop("stale_status", None)
            asset.pop("stale_reason", None)
        except Exception as exc:
            asset.update({"status": "failed", "completed_at": now_iso(), "error": str(exc)[:1000]})
            project["updated_at"] = now_iso()
            self.store.save(project)
            if isinstance(exc, WorkflowError):
                raise
            label = "图像" if kind == "image" else "视频"
            raise WorkflowError(f"镜头 {shot_id} 的{label}生成失败：{exc}", 502) from exc
        finally:
            with self._media_jobs_lock:
                self._active_media_jobs.discard(job_key)

        video = project["workflow"].setdefault("video", {})
        for shot in video.get("shots", []):
            shot["stale"] = any(
                (shot.get(kind) or {}).get("status") == "stale"
                for kind in ("image", "video")
            )
        asset_statuses = [
            (shot.get(kind) or {}).get("status")
            for shot in video.get("shots", [])
            for kind in ("image", "video")
        ]
        statuses = [(item.get("video") or {}).get("status") for item in video.get("shots", [])]
        if "stale" in asset_statuses:
            video["status"] = "stale"
        elif statuses and all(status == "ready" for status in statuses):
            video["status"] = "ready"
        elif any(status in {"ready", "generating"} for status in statuses):
            video["status"] = "partial"
        else:
            video["status"] = "pending"
        project["updated_at"] = now_iso()
        return self.store.save(project)

    def generate_shot_image(self, project_id: str, shot_id: str) -> dict:
        return self._generate_shot_asset(project_id, shot_id, "image")

    def _video_job_request(self, project: dict, shot: dict) -> dict:
        image_asset = shot.get("image") or {}
        image_url = image_asset.get("asset_url") or "" if image_asset.get("status") == "ready" else ""
        parsed_image_url = urlparse(image_url)
        references = [image_url] if parsed_image_url.scheme in {"http", "https"} and parsed_image_url.hostname else []
        return {
            "brief": {**project.get("brief", {}), "duration_seconds": shot["duration_seconds"]},
            "video_prompt": shot["video_prompt"],
            "aspect_ratio": shot["aspect_ratio"],
            "reference_images": references,
        }

    @staticmethod
    def _video_request_signature(request: dict) -> str:
        canonical = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _refresh_video_stage_status(self, project: dict) -> None:
        video = project.get("workflow", {}).setdefault("video", {})
        shots = video.get("shots", [])
        for shot in shots:
            shot["stale"] = any(
                (shot.get(kind) or {}).get("status") == "stale"
                for kind in ("image", "video")
            )
        statuses = [(shot.get("video") or {}).get("status", "pending") for shot in shots]
        asset_statuses = [
            (shot.get(kind) or {}).get("status", "pending")
            for shot in shots
            for kind in ("image", "video")
        ]
        if "stale" in asset_statuses:
            video["status"] = "stale"
        elif statuses and all(status == "ready" for status in statuses):
            video["status"] = "ready"
        elif any(status != "pending" for status in statuses):
            video["status"] = "partial"
        else:
            video["status"] = "pending"

    def _update_video_job(
        self,
        job: dict,
        updates: dict,
        *,
        asset_updates: dict | None = None,
    ) -> dict:
        project = self.store.get(job["project_id"])
        should_save_project = False
        if project is not None and asset_updates is not None:
            video = project.get("workflow", {}).get("video", {})
            shot = next(
                (item for item in video.get("shots", []) if item.get("shot_id") == job["shot_id"]),
                None,
            )
            asset = (shot or {}).get("video") or {}
            if shot is not None and asset.get("job_id") == job["id"]:
                asset.update(asset_updates)
                self._refresh_video_stage_status(project)
                project["updated_at"] = now_iso()
                should_save_project = True
        return self.store.update_video_job(
            job["id"], updates, project=project if should_save_project else None
        )

    def _fail_video_job(self, job: dict, message: str) -> None:
        error = str(message)[:1000]
        self._update_video_job(
            job,
            {"status": "failed", "error": error, "next_poll_at": None},
            asset_updates={"status": "failed", "completed_at": now_iso(), "error": error},
        )

    def _mark_video_job_interrupted(self, job: dict, message: str) -> None:
        error = str(message)[:1000]
        self._update_video_job(
            job,
            {"status": "interrupted", "error": error, "next_poll_at": None},
            asset_updates={
                "status": "interrupted",
                "completed_at": now_iso(),
                "error": error,
            },
        )

    def _defer_video_job_poll(self, job: dict, message: str) -> None:
        failures = int(job.get("poll_failures", 0)) + 1
        delay_seconds = min(5 * (2 ** min(failures - 1, 4)), 60)
        next_poll = (datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)).isoformat()
        self._update_video_job(
            job,
            {
                "status": "polling",
                "poll_failures": failures,
                "last_poll_error": str(message)[:1000],
                "next_poll_at": next_poll,
            },
            asset_updates={"provider_status": "查询暂时失败，稍后自动重试"},
        )

    def _submit_video_job(self, job: dict) -> None:
        try:
            provider = self._get_video_provider()
        except WorkflowError as exc:
            self._fail_video_job(job, str(exc))
            return
        try:
            task = provider.submit_video(project=job["request"])
        except VideoSubmissionUncertainError as exc:
            self._mark_video_job_interrupted(job, str(exc))
            return
        except MediaProviderError as exc:
            self._fail_video_job(job, str(exc))
            return
        except Exception:
            self._mark_video_job_interrupted(
                job,
                "视频任务提交时发生异常，供应商是否已受理未知。请先核对供应商任务，再决定是否重试。",
            )
            return
        provider_task_id = task.get("provider_task_id") if isinstance(task, dict) else None
        if not provider_task_id:
            self._mark_video_job_interrupted(
                job,
                "供应商响应中没有可用于查询的任务 ID，任务是否已受理未知。请核对供应商任务后再决定是否重试。",
            )
            return
        task_id = str(task.get("task_id") or provider_task_id)
        next_poll = (datetime.now(timezone.utc) + timedelta(seconds=3)).isoformat()
        self._update_video_job(
            job,
            {
                "status": "polling",
                "provider_task_id": str(provider_task_id),
                "task_id": task_id,
                "provider_status": "submitted",
                "poll_failures": 0,
                "next_poll_at": next_poll,
                "error": None,
            },
            asset_updates={
                "status": "generating",
                "task_id": task_id,
                "provider_status": "已提交，等待供应商处理",
                "error": None,
            },
        )

    def _poll_video_job(self, job: dict) -> None:
        provider_task_id = job.get("provider_task_id")
        if not provider_task_id:
            self._mark_video_job_interrupted(
                job,
                "本地记录缺少供应商任务 ID，无法安全恢复查询。请核对供应商任务后再决定是否重试。",
            )
            return
        try:
            provider = self._get_video_provider()
            result = provider.poll_video(provider_task_id=provider_task_id)
        except Exception as exc:
            self._defer_video_job_poll(job, str(exc) or "查询视频任务暂时失败。")
            return
        status = result.get("status") if isinstance(result, dict) else None
        if status == "completed" and result.get("asset_url"):
            self._complete_video_job(job, result)
            return
        if status == "failed":
            self._fail_video_job(job, result.get("error") or "供应商报告视频任务失败。")
            return
        now = datetime.now(timezone.utc)
        delay = 5 if status == "queued" else 10
        self._update_video_job(
            job,
            {
                "status": "polling",
                "provider_status": str(result.get("provider_status") or status or "processing")[:100],
                "poll_failures": 0,
                "last_poll_error": None,
                "next_poll_at": (now + timedelta(seconds=delay)).isoformat(),
            },
            asset_updates={
                "status": "generating",
                "provider_status": str(result.get("provider_status") or status or "processing")[:100],
            },
        )

    def _complete_video_job(self, job: dict, result: dict) -> None:
        project = self.store.get(job["project_id"])
        asset_url = str(result["asset_url"])
        completed_at = now_iso()
        project_to_save = None
        if project is not None:
            video = project.get("workflow", {}).get("video", {})
            shot = next(
                (item for item in video.get("shots", []) if item.get("shot_id") == job["shot_id"]),
                None,
            )
            asset = (shot or {}).get("video") or {}
            if shot is not None and asset.get("job_id") == job["id"]:
                current_request = self._video_job_request(project, shot)
                stale = (
                    self._video_request_signature(current_request) != job.get("input_signature")
                    or asset.get("status") == "stale"
                    or shot.get("stale", False)
                )
                task_id = job.get("task_id") or job.get("provider_task_id")
                version = {
                    "version": len(asset.get("versions", [])) + 1,
                    "asset_url": asset_url,
                    "model": job.get("model"),
                    "requested_at": asset.get("requested_at"),
                    "completed_at": completed_at,
                    "task_id": task_id,
                    "job_id": job["id"],
                    "cost": None,
                    "stale": stale,
                }
                asset.setdefault("versions", []).append(version)
                asset.update({
                    "asset_url": asset_url,
                    "completed_at": completed_at,
                    "task_id": task_id,
                    "cost": None,
                    "error": None,
                    "provider_status": "completed",
                    "status": "stale" if stale else "ready",
                })
                if stale:
                    asset["stale_reason"] = "生成期间镜头任务或参考素材已修改；此片段已保留为过期版本。"
                else:
                    asset.pop("stale_status", None)
                    asset.pop("stale_reason", None)
                self._refresh_video_stage_status(project)
                project["updated_at"] = completed_at
                project_to_save = project
        self.store.update_video_job(
            job["id"],
            {
                "status": "completed",
                "provider_status": "completed",
                "result_url": asset_url,
                "completed_at": completed_at,
                "next_poll_at": None,
                "last_poll_error": None,
            },
            project=project_to_save,
        )

    def generate_shot_video(
        self, project_id: str, shot_id: str, *, confirm_uncertain: bool = False
    ) -> dict:
        project = self.get_project(project_id)
        shot = self._get_shot(project, shot_id)
        provider = self._get_video_provider()
        supports_durable_tasks = callable(getattr(provider, "submit_video", None)) and callable(
            getattr(provider, "poll_video", None)
        )
        if not supports_durable_tasks or self.video_job_worker is None:
            return self._generate_shot_asset(project_id, shot_id, "video")

        latest = self.store.latest_video_job(project_id, shot_id)
        if latest and latest.get("status") in {"queued", "submitting", "polling"}:
            raise WorkflowError("这个镜头已有正在处理的视频任务，请等待当前任务完成。", 409)
        if latest and latest.get("status") == "interrupted":
            if not confirm_uncertain:
                raise WorkflowError(
                    "上一次提交结果未知。请先核对供应商任务；确认没有仍在处理的任务后，再选择“核对后重试”。",
                    409,
                )
            self.store.update_video_job(
                latest["id"],
                {"status": "abandoned", "resolved_at": now_iso()},
            )

        request = self._video_job_request(project, shot)
        created = now_iso()
        job_id = str(uuid4())
        profile_id = self.settings.model_catalog.defaults.get("video") or "injected-provider"
        job = {
            "id": job_id,
            "project_id": project_id,
            "shot_id": shot_id,
            "status": "queued",
            "model": profile_id,
            "request": request,
            "input_signature": self._video_request_signature(request),
            "provider_task_id": None,
            "task_id": None,
            "provider_status": "queued",
            "error": None,
            "poll_failures": 0,
            "created_at": created,
            "updated_at": created,
            "next_poll_at": None,
        }
        asset = shot.setdefault("video", {})
        asset.update({
            "status": "queued",
            "job_id": job_id,
            "model": profile_id,
            "requested_at": created,
            "completed_at": None,
            "task_id": None,
            "provider_status": "等待后台提交",
            "cost": None,
            "error": None,
        })
        asset.pop("stale_status", None)
        asset.pop("stale_reason", None)
        self._refresh_video_stage_status(project)
        project["updated_at"] = created
        try:
            self.store.enqueue_video_job(project, job)
        except VideoJobConflict as exc:
            raise WorkflowError(str(exc), 409) from exc
        self.video_job_worker.wake()
        return project

    def update_content(self, project_id: str, stage: str, content: str) -> dict:
        if stage not in STAGE_IDS:
            raise WorkflowError("未知的创作步骤。", 404)
        if not content.strip():
            raise WorkflowError("内容不能为空。", 422)
        project = self.get_project(project_id)
        if project["active_stage"] != stage:
            raise WorkflowError("只能修改当前步骤的草稿；请先回退到该步骤。", 409)
        item = project["workflow"][stage]
        if item["status"] not in {"ready", "stale"}:
            raise WorkflowError("请先生成当前步骤的草稿，再修改内容。", 409)
        scene_plan = None
        if stage == "scenes":
            try:
                scene_plan = normalize_scene_plan(content)
            except ScenePlanError as exc:
                raise WorkflowError(str(exc), 422) from exc
            content = serialize_scene_plan(scene_plan)
        changed = item.get("content") != content
        if changed:
            self._archive_stage_content(item, "人工修改前的草稿")
        if scene_plan is not None:
            item["shots"] = scene_plan["shots"]
            item["edit_plan"] = scene_plan["edit_plan"]
        item["content"] = content
        item["status"] = "ready"
        item.pop("stale_reason", None)
        item.pop("stale_since", None)
        item["updated_at"] = now_iso()
        if changed:
            self._mark_downstream_stale(project, stage, f"“{next(step['label'] for step in STAGES if step['id'] == stage)}”内容已修改，请重新审阅后续内容。")
        project["updated_at"] = now_iso()
        return self.store.save(project)

    def approve(self, project_id: str, stage: str) -> dict:
        if stage not in STAGE_IDS:
            raise WorkflowError("未知的创作步骤。", 404)
        project = self.get_project(project_id)
        if project["active_stage"] != stage:
            raise WorkflowError("只能确认当前步骤。", 409)
        item = project["workflow"][stage]
        if item["status"] != "ready" or not item["content"]:
            raise WorkflowError("请先生成或填写本步骤内容，再确认。", 409)
        if stage != "scenes" and _has_blocking_content(item["content"]):
            raise WorkflowError(
                "这份草稿明确包含信息不足或无法继续等阻塞说明，不能确认。请补充事实或编辑草稿后再继续。",
                409,
            )
        if stage == "scenes":
            try:
                scene_plan = normalize_scene_plan(item["content"])
            except ScenePlanError as exc:
                raise WorkflowError(str(exc), 422) from exc
            item["shots"] = scene_plan["shots"]
            item["edit_plan"] = scene_plan["edit_plan"]
            video = project["workflow"].setdefault("video", {})
            self._reconcile_video_shots(video, scene_plan["shots"], scene_plan["edit_plan"])
        item["status"] = "approved"
        item.pop("stale_reason", None)
        item.pop("stale_since", None)
        next_index = STAGE_IDS.index(stage) + 1
        if next_index < len(STAGE_IDS):
            project["active_stage"] = STAGE_IDS[next_index]
        project["updated_at"] = now_iso()
        return self.store.save(project)

    def config_view(self) -> dict:
        profiles = [
            profile.public_dict(self.settings.api_keys, self.settings.model_credentials)
            for profile in self.settings.model_catalog.list()
        ]
        text_models = [profile["id"] for profile in profiles if profile["modality"] == "text"]
        configured_modalities = {
            modality: any(
                item["modality"] == modality and item["configured"]
                for item in profiles
            )
            for modality in ("text", "image", "video", "audio")
        }
        mode = "connected" if any(
            self.settings.profile_is_configured(profile)
            for profile in self.settings.model_catalog.list()
        ) else "unconfigured"
        video_profile_id = self.settings.model_catalog.defaults.get("video")
        image_profile_id = self.settings.model_catalog.defaults.get("image")
        video_profile = self.settings.model_catalog.profiles.get(video_profile_id or "")
        image_profile = self.settings.model_catalog.profiles.get(image_profile_id or "")
        video_default_ready = bool(video_profile and self.settings.profile_is_configured(video_profile))
        image_default_ready = bool(image_profile and self.settings.profile_is_configured(image_profile))
        injected_ready = bool(self.video_provider_override and self.video_provider_override.ready)
        injected_image_ready = self.image_provider_override is not None
        media_note = "图片、视频和语音生成依赖各自已配置的模型与 API Key；只会在你明确提出时调用。"
        return {
            "provider_mode": mode,
            "models": text_models,
            "model_profiles": profiles,
            "defaults": self.settings.model_catalog.defaults,
            "default_model": self.settings.model_catalog.defaults.get("text", ""),
            "stages": list(STAGES),
            "configured_modalities": configured_modalities,
            "media_provider_ready": injected_ready or video_default_ready,
            "video_provider_ready": injected_ready or video_default_ready,
            "image_provider_ready": injected_image_ready or image_default_ready,
            "media_provider_note": media_note,
        }
