from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from app.store import ProjectStore

if TYPE_CHECKING:
    from app.services.workflow import WorkflowService


logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class VideoJobWorker:
    """Single-process worker that resumes durable video submission and polling jobs."""

    def __init__(self, workflow: WorkflowService, store: ProjectStore):
        self.workflow = workflow
        self.store = store
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()

    def start(self) -> None:
        with self._start_lock:
            if self._thread and self._thread.is_alive():
                return
            self._recover_interrupted_submissions()
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="video-job-worker",
                daemon=True,
            )
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout)

    def wake(self) -> None:
        self._wake.set()

    def process_once(self) -> bool:
        """Process one queued submission or one due provider status poll."""
        job = self.store.claim_next_video_job()
        if job is not None:
            try:
                self.workflow._submit_video_job(job)
            except Exception:
                logger.exception("Unexpected video job submission error")
                self.workflow._mark_video_job_interrupted(
                    job,
                    "后台任务处理时发生异常，供应商是否已受理未知。请先核对供应商任务，再决定是否重试。",
                )
            return True

        job = self.store.next_polling_video_job(_now_iso())
        if job is None:
            return False
        try:
            self.workflow._poll_video_job(job)
        except Exception:
            logger.exception("Unexpected video job polling error")
            self.workflow._defer_video_job_poll(job, "后台查询暂时失败，将稍后自动重试。")
        return True

    def _recover_interrupted_submissions(self) -> None:
        for job in self.store.list_video_jobs({"submitting"}):
            self.workflow._mark_video_job_interrupted(
                job,
                "服务在提交视频任务期间停止，供应商是否已受理未知。请先核对供应商任务，再决定是否重试。",
            )

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                processed = self.process_once()
            except Exception:
                logger.exception("Video job worker cycle failed")
                processed = False
            if not processed:
                self._wake.wait(timeout=0.5)
                self._wake.clear()
