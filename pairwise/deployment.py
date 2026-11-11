"""Persistent handover gate for updating the console around live A/B sessions."""
from __future__ import annotations

import time
from typing import Optional


DRAINING_KEY = "pairwise_deployment_draining"
REQUESTED_AT_KEY = "pairwise_deployment_requested_at"
CRITICAL_ATTEMPT_STATES = ("starting", "recovering", "archiving")


def initialize(h) -> None:
    values = h.settings_values((DRAINING_KEY, REQUESTED_AT_KEY))
    missing = {}
    if DRAINING_KEY not in values:
        missing[DRAINING_KEY] = "0"
    if REQUESTED_AT_KEY not in values:
        missing[REQUESTED_AT_KEY] = ""
    if missing:
        h.write_settings(missing)


def is_draining(h) -> bool:
    initialize(h)
    return h.settings_values((DRAINING_KEY,)).get(DRAINING_KEY, "0") == "1"


def configure(h, draining: bool):
    if not isinstance(draining, bool):
        raise h.WorkflowError("交接模式必须是布尔值")
    initialize(h)
    h.write_settings({
        DRAINING_KEY: "1" if draining else "0",
        REQUESTED_AT_KEY: h.now_text() if draining else "",
    })
    # Wake workers so they promptly observe either entering or leaving the gate.
    try:
        from . import recording
        recording._WAKE.set()
    except Exception:
        pass
    return status(h)


def wait_until_accepting(h, run_id: Optional[str] = None, detail: str = "服务更新交接中，保持现场"):
    """Hold a safe transition without stopping or writing to the Claude session."""
    announced = False
    while is_draining(h):
        h.ensure_job_active()
        if run_id and not announced:
            h.update_run(run_id, status_detail=detail)
            announced = True
        time.sleep(0.5)


def status(h):
    initialize(h)
    values = h.settings_values((DRAINING_KEY, REQUESTED_AT_KEY))
    with h.db_connection() as db:
        attempts = {
            str(row["status"]): int(row["count"])
            for row in db.execute(
                "SELECT status,COUNT(*) count FROM pairwise_attempts "
                "WHERE status IN ('queued','starting','running','recovering','archiving') GROUP BY status"
            )
        }
        draft_jobs = db.execute(
            "SELECT COUNT(*) FROM pairwise_drafts WHERE status IN ('queued','running')"
        ).fetchone()[0]
        draft_jobs += db.execute(
            "SELECT COUNT(*) FROM pairwise_zero_one_batches WHERE status IN ('queued','running')"
        ).fetchone()[0]
        draft_jobs += db.execute(
            "SELECT COUNT(*) FROM pairwise_iteration_batches WHERE status IN ('queued','running')"
        ).fetchone()[0]
        draft_jobs += db.execute(
            "SELECT COUNT(*) FROM pairwise_bug_batches WHERE status IN ('queued','running')"
        ).fetchone()[0]
        review_jobs = db.execute(
            "SELECT COUNT(*) FROM pairwise_tasks WHERE difficulty_status='running'"
        ).fetchone()[0]
    from . import recording
    recording_state = recording.queue_status(h)
    blockers = {
        "critical_attempts": sum(attempts.get(name, 0) for name in CRITICAL_ATTEMPT_STATES),
        "recording_active": int(recording_state["active"]),
        "codex_active": int(getattr(h, "CODEX_SLOT_ACTIVE", 0)),
        "draft_jobs": int(draft_jobs),
        "review_jobs": int(review_jobs),
    }
    draining = values.get(DRAINING_KEY, "0") == "1"
    return {
        "draining": draining,
        "requested_at": values.get(REQUESTED_AT_KEY, ""),
        "restart_safe": draining and not any(blockers.values()),
        "blockers": blockers,
        "preserved": {
            "running": attempts.get("running", 0),
            "queued": attempts.get("queued", 0),
            "recording_queued": int(recording_state["queued"]),
        },
    }
