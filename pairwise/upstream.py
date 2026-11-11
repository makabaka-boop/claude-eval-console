"""Pause Pair-wise Claude work during a confirmed auto_model/urm outage."""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import uuid

from . import models as m


STATE_KEY = "pairwise_claude_upstream_state"
REASON_KEY = "pairwise_claude_upstream_reason"
PAUSED_AT_KEY = "pairwise_claude_upstream_paused_at"
NEXT_PROBE_KEY = "pairwise_claude_upstream_next_probe_at"
LAST_PROBE_KEY = "pairwise_claude_upstream_last_probe_at"
LAST_CONFIRM_PROBE_KEY = "pairwise_claude_upstream_last_confirm_probe_epoch"
PRIORITY_PAIRS_KEY = "pairwise_claude_priority_pairs"
PROBE_INTERVAL_SECONDS = 30 * 60
CONFIRM_PROBE_COOLDOWN_SECONDS = 5 * 60
PROBE_TIMEOUT_SECONDS = 120
POLL_SECONDS = 5
WAKE = threading.Event()
PROBE_LOCK = threading.Lock()
START_LOCK = threading.Lock()
STARTED = False


def initialize(h):
    defaults = {
        STATE_KEY: "ready", REASON_KEY: "", PAUSED_AT_KEY: "",
        NEXT_PROBE_KEY: "", LAST_PROBE_KEY: "", LAST_CONFIRM_PROBE_KEY: "0",
        PRIORITY_PAIRS_KEY: "[]",
    }
    values = h.settings_values(tuple(defaults))
    missing = {key: value for key, value in defaults.items() if key not in values}
    if missing:
        h.write_settings(missing)


def is_paused(h):
    return h.settings_values((STATE_KEY,)).get(STATE_KEY, "ready") == "paused"


def clear_legacy_pause(h):
    """Discard the old global pause state without restarting past attempts."""
    initialize(h)
    h.write_settings({STATE_KEY: "ready", REASON_KEY: "", PAUSED_AT_KEY: "",
                      NEXT_PROBE_KEY: "", PRIORITY_PAIRS_KEY: "[]"})
    WAKE.set()


def status(h):
    initialize(h)
    values = h.settings_values((STATE_KEY, REASON_KEY, PAUSED_AT_KEY,
                                NEXT_PROBE_KEY, LAST_PROBE_KEY))
    try:
        next_probe = int(values.get(NEXT_PROBE_KEY) or 0)
    except ValueError:
        next_probe = 0
    with h.db_connection() as db:
        paused_attempts = db.execute(
            "SELECT COUNT(*) FROM pairwise_attempts WHERE status='upstream_paused'"
        ).fetchone()[0]
    return {
        "paused": values.get(STATE_KEY) == "paused",
        "model": m.AUTO_MODEL,
        "reason": values.get(REASON_KEY, ""),
        "paused_at": values.get(PAUSED_AT_KEY, ""),
        "last_probe_at": values.get(LAST_PROBE_KEY, ""),
        "next_probe_at": next_probe,
        "next_probe_in_seconds": max(0, next_probe - int(time.time())) if next_probe else 0,
        "paused_attempts": paused_attempts,
    }


def candidate_error(detail):
    message = str(detail or "")
    match = re.search(r"API Error:\s*(\d{3})", message, re.I)
    if match and (int(match.group(1)) in {408, 429}
                  or 500 <= int(match.group(1)) <= 599):
        return True
    lowered = message.casefold()
    return any(marker in lowered for marker in (
        "unknown_certificate_verification_error", "unable to connect to api",
        "connection to api failed", "upstream connect error",
    ))


def _credential(h):
    for name in (h.DOCKER_API_KEY_ENV, "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
        value = h.os.environ.get(name)
        if value:
            return value
    try:
        settings = json.loads(h.CLAUDE_SETTINGS_PATH.read_text(encoding="utf-8"))
        env = settings.get("env", {}) if isinstance(settings, dict) else {}
        value = env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY")
        if isinstance(value, str) and value:
            return value
    except (OSError, ValueError, AttributeError):
        pass
    raise h.WorkflowError("Claude 探测缺少与 A/B 相同的 API 凭据")


def probe(h):
    """One isolated, bounded, same-image model call; never touch task sessions."""
    try:
        key = _credential(h)
    except h.WorkflowError as exc:
        return False, f"本地配置错误：{exc}"
    name = f"pairwise-upstream-probe-{uuid.uuid4().hex[:12]}"
    command = [
        "docker", "run", "--rm", "--name", name, "-i", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "-e", "apikey", "-e",
        f"ANTHROPIC_MODEL={m.AUTO_MODEL}", h.DOCKER_IMAGE, "print",
    ]
    environment = os.environ.copy()
    environment["apikey"] = key
    try:
        result = subprocess.run(
            command, input="Reply with exactly PONG.\n", text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=PROBE_TIMEOUT_SECONDS, env=environment, start_new_session=True,
        )
        replies = []
        for line in result.stdout.splitlines():
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if item.get("type") == "result":
                replies.append(item)
        if result.returncode == 0 and any(
                not item.get("is_error") and "PONG" in str(item.get("result") or "").upper()
                for item in replies):
            return True, "同模型独立探测已收到有效回复"
        output = "\n".join(part for part in (result.stderr, result.stdout) if part)
        detail = (output or f"探测退出码 {result.returncode}")[-1200:]
        if candidate_error(output):
            return False, f"上游模型探测失败：{detail}"
        return False, f"本地探测未获有效 PONG（不能判定上游故障）：{detail}"
    except subprocess.TimeoutExpired:
        return False, f"上游模型探测超过 {PROBE_TIMEOUT_SECONDS} 秒无有效回复"
    except (OSError, ValueError) as exc:
        return False, f"本地探测失败：{exc}"
    finally:
        environment.pop("apikey", None)
        # The temporary probe is our own exact-name container. A timed-out
        # Docker client must not leave it running or consume later probes.
        try:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True,
                           timeout=15, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass


def wait_until_ready(h, run_id=None):
    announced = ""
    while True:
        h.ensure_job_active()
        paused = is_paused(h)
        priority_wait = not paused and run_id and _priority_wait_required(h, run_id)
        if not paused and not priority_wait:
            break
        detail = ("Claude 上游故障暂停；等待每 30 分钟独立探测" if paused else
                  "优先补齐已有单侧产物的题；当前排队位置保留")
        if run_id and announced != detail:
            h.update_run(run_id, status_detail=detail)
            announced = detail
        WAKE.wait(POLL_SECONDS)
        WAKE.clear()
    h.ensure_job_active()


def _priority_pairs(h):
    try:
        value = json.loads(h.settings_values((PRIORITY_PAIRS_KEY,)).get(PRIORITY_PAIRS_KEY, "[]"))
        return [pair_id for pair_id in value if isinstance(pair_id, str)] if isinstance(value, list) else []
    except (TypeError, ValueError):
        return []


def _unfinished_priority_pairs(h, pair_ids):
    if not pair_ids:
        return []
    placeholders = ",".join("?" for _ in pair_ids)
    with h.db_connection() as db:
        rows = db.execute(f"""SELECT t.id,
            (SELECT status FROM pairwise_attempts a WHERE a.pair_id=t.id AND a.side='A'
             ORDER BY a.number DESC LIMIT 1) AS a_status,
            (SELECT status FROM pairwise_attempts b WHERE b.pair_id=t.id AND b.side='B'
             ORDER BY b.number DESC LIMIT 1) AS b_status
            FROM pairwise_tasks t WHERE t.id IN ({placeholders}) AND t.deleted_at IS NULL""",
            tuple(pair_ids)).fetchall()
    pending = set(m.ACTIVE) | {"upstream_paused"}
    return [row["id"] for row in rows if
            (row["a_status"] == "complete" and row["b_status"] in pending) or
            (row["b_status"] == "complete" and row["a_status"] in pending)]


def _priority_wait_required(h, run_id):
    pair_ids = _priority_pairs(h)
    if not pair_ids:
        return False
    if m.attempt(h, run_id)["pair_id"] in pair_ids:
        return False
    return bool(_unfinished_priority_pairs(h, pair_ids))


def _pairs_with_completed_sibling(h):
    with h.db_connection() as db:
        rows = db.execute("""SELECT t.id,
            (SELECT status FROM pairwise_attempts a WHERE a.pair_id=t.id AND a.side='A'
             ORDER BY a.number DESC LIMIT 1) AS a_status,
            (SELECT status FROM pairwise_attempts b WHERE b.pair_id=t.id AND b.side='B'
             ORDER BY b.number DESC LIMIT 1) AS b_status
            FROM pairwise_tasks t WHERE t.deleted_at IS NULL""").fetchall()
    pending = set(m.ACTIVE) | {"upstream_paused"}
    return [row["id"] for row in rows if
            (row["a_status"] == "complete" and row["b_status"] in pending) or
            (row["b_status"] == "complete" and row["a_status"] in pending)]


def pause_all(h, reason):
    """Freeze queued dispatch and stop all in-flight Claude containers safely."""
    from . import workflow
    with workflow.LOCK:
        if is_paused(h):
            return False
        now = h.now_text()
        h.write_settings({
            STATE_KEY: "paused", REASON_KEY: str(reason)[:1200],
            PAUSED_AT_KEY: now,
            LAST_PROBE_KEY: now,
            NEXT_PROBE_KEY: str(int(time.time()) + PROBE_INTERVAL_SECONDS),
        })
        with h.db_connection() as db:
            rows = db.execute(
                "SELECT id,status FROM pairwise_attempts WHERE status IN ('queued','starting','running')"
            ).fetchall()
        active = []
        for row in rows:
            run_id = row["id"]
            if row["status"] == "queued":
                h.update_run(run_id, status_detail="Claude 上游故障暂停；保留排队位置")
                continue
            m.update_attempt(h, run_id, status="upstream_paused", error=str(reason)[:1200])
            h.cancel_background_job(f"run:{run_id}")
            h.update_run(run_id, phase="upstream_paused",
                         status_detail="Claude 上游故障；正在停止并保存原始现场")
            h.add_event(run_id, f"上游故障已确认，暂停本次 Claude Session：{reason}", "warning")
            active.append(run_id)
    for run_id in active:
        _stop_paused_attempt(h, run_id, reason)
    WAKE.set()
    return True


def _stop_paused_attempt(h, run_id, reason):
    """Also usable after a process crash between marking and stopping."""
    from . import workflow
    attempt = m.attempt(h, run_id)
    if attempt["status"] != "upstream_paused" or h.run_row(run_id)["container_cleaned"]:
        return
    previous = h.current_job_key()
    h.CODEX_JOB_CONTEXT.key = ""
    try:
        try:
            workflow.stop(h, run_id)
            h.update_run(run_id, phase="upstream_paused",
                         status_detail="Claude 上游故障暂停；现场已保存，恢复后新建独立 Session")
        except Exception as exc:
            h.update_run(run_id, phase="upstream_paused",
                         status_detail="Claude 上游故障暂停；现场保留，导出待人工检查")
            h.add_event(run_id, f"停止后轨迹未能安全归档：{exc}", "error")
        finally:
            m.update_attempt(h, run_id, status="upstream_paused",
                             error=f"Claude 上游故障暂停：{reason}"[:1200])
    finally:
        h.CODEX_JOB_CONTEXT.key = previous


def reconcile_paused(h):
    """Stop any container left alive by an interruption of the pause handler."""
    if not is_paused(h):
        return
    from . import workflow
    reason = h.settings_values((REASON_KEY,)).get(REASON_KEY, "上游故障")
    with workflow.LOCK:
        with h.db_connection() as db:
            rows = db.execute("""SELECT id,status FROM pairwise_attempts
                WHERE status IN ('starting','running','upstream_paused')""").fetchall()
        for row in rows:
            if row["status"] != "upstream_paused":
                m.update_attempt(h, row["id"], status="upstream_paused", error=reason[:1200])
                h.cancel_background_job(f"run:{row['id']}")
    for row in rows:
        _stop_paused_attempt(h, row["id"], reason)


def confirm_and_pause(h, run_id, error):
    """One task error is not enough to stop every other healthy Session."""
    if not candidate_error(error):
        return False
    with PROBE_LOCK:
        if is_paused(h):
            return True
        # Several A/B sessions can report the same gateway failure at once.
        # Coalesce that burst into one independent check, including across a
        # service restart, rather than issuing a probe for every failing side.
        values = h.settings_values((LAST_CONFIRM_PROBE_KEY,))
        try:
            last_probe = int(values.get(LAST_CONFIRM_PROBE_KEY) or 0)
        except ValueError:
            last_probe = 0
        now = int(time.time())
        if now - last_probe < CONFIRM_PROBE_COOLDOWN_SECONDS:
            return False
        h.write_settings({LAST_CONFIRM_PROBE_KEY: str(now)})
        healthy, detail = probe(h)
        if healthy or not detail.startswith("上游"):
            return False
        pause_all(h, f"{error}；{detail}")
        return True


def probe_if_due(h, now=None):
    initialize(h)
    if not is_paused(h):
        return False
    current = int(now if now is not None else time.time())
    values = h.settings_values((NEXT_PROBE_KEY,))
    try:
        due = int(values.get(NEXT_PROBE_KEY) or 0)
    except ValueError:
        due = 0
    if current < due:
        return False
    with PROBE_LOCK:
        if not is_paused(h):
            return False
        reconcile_paused(h)
        healthy, detail = probe(h)
        h.write_settings({LAST_PROBE_KEY: h.now_text()})
        if healthy:
            resume_all(h)
        else:
            h.write_settings({REASON_KEY: detail[:1200],
                              NEXT_PROBE_KEY: str(int(time.time()) + PROBE_INTERVAL_SECONDS)})
        return healthy


def resume_all(h):
    """Rerun stopped work in fresh Sessions, then release the original queue."""
    from . import workflow
    with workflow.LOCK:
        if not is_paused(h):
            return []
        priority_pairs = _pairs_with_completed_sibling(h)
        with h.db_connection() as db:
            rows = db.execute(
                "SELECT id FROM pairwise_attempts WHERE status='upstream_paused' ORDER BY created_at,id"
            ).fetchall()
        restarted = []
        for row in rows:
            old = m.attempt(h, row["id"])
            if m.task(h, old["pair_id"]).get("deleted_at"):
                m.update_attempt(h, old["id"], status="stopped")
                h.update_run(old["id"], status_detail="上游已恢复；本题已从列表删除，不自动重建")
                continue
            if not old["container_cleaned"]:
                m.update_attempt(h, old["id"], status="failed")
                h.update_run(old["id"], status_detail="上游已恢复；旧容器轨迹未安全归档，保留现场待人工处理")
                continue
            try:
                fresh = workflow.start(h, old["pair_id"], old["side"])
                restarted.append(fresh["id"])
                h.add_event(old["id"], f"上游恢复，已新建独立 Session {fresh['id']}；未向旧会话追加消息", "info")
            except Exception as exc:
                m.update_attempt(h, old["id"], status="failed")
                h.update_run(old["id"], status_detail=f"上游已恢复；自动重建失败，需人工处理：{exc}")
                h.add_event(old["id"], f"上游恢复后自动重建失败：{exc}", "error")
        h.write_settings({STATE_KEY: "ready", REASON_KEY: "", PAUSED_AT_KEY: "",
                          NEXT_PROBE_KEY: "",
                          PRIORITY_PAIRS_KEY: json.dumps(priority_pairs)})
        with h.db_connection() as db:
            queued = [row["id"] for row in db.execute(
                "SELECT id FROM pairwise_attempts WHERE status='queued' ORDER BY created_at,id"
            )]
        for run_id in queued:
            workflow.enqueue(h, run_id)
    WAKE.set()
    return restarted


def _loop(h):
    try:
        reconcile_paused(h)
    except Exception as exc:
        h.log_workflow_exception("-", "pairwise-claude-upstream-reconcile", exc)
    while True:
        WAKE.wait(POLL_SECONDS)
        WAKE.clear()
        try:
            probe_if_due(h)
        except Exception as exc:
            h.log_workflow_exception("-", "pairwise-claude-upstream-probe", exc)
            if is_paused(h):
                h.write_settings({REASON_KEY: f"探测协调失败：{exc}"[:1200],
                                  NEXT_PROBE_KEY: str(int(time.time()) + PROBE_INTERVAL_SECONDS)})


def start_coordinator(h):
    global STARTED
    initialize(h)
    with START_LOCK:
        if STARTED:
            return
        STARTED = True
    threading.Thread(target=_loop, args=(h,), name="pairwise-claude-upstream",
                     daemon=True).start()
    WAKE.set()
