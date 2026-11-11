"""Continuous Pair creation with eventual balance, never a per-step ratio gate."""
import json
import re
import sqlite3
import threading
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path

from . import deployment, history, models as m, workflow, notebook, sources, candidate_bank, legacy_baselines, seeded_bug, natural_bug, bug_runtime, bug_generation_v2, bug_references
from .drafts import GUIDANCE


DEFAULT_DIRECTORY = "zzzz-gsb"
ROOT_DEPTH_GUIDANCE = (
    "\n自动补题的 0-1 应是可运行、可操作且能支撑后续真实迭代的项目。"
    "围绕一个明确的用户目标形成可持续使用、可直接验收的完整用户闭环；"
    "一条足够深的工作流即可，不强制拆成两个功能模块。只取困难下沿，不接受地狱级候选；"
    "实现模块最多三个、运行组件最多两个、核心外辅助机制最多一项，复杂状态机制与自定义算法必须二选一；"
    "主体实现与针对性验收按三十至四十五分钟设计，预计超过五十分钟必须收窄或换方向；"
    "手写源码、样式、数据库脚本与测试合计不得超过 2500 行，依赖锁文件和自动生成文件不计；超出任一上限就换方向。"
    "优先有完整纵向主流程、真实的状态生命周期和可独立观察的正常与失败结果；"
    "不要反复生成仅粘贴 JSON、执行一次计算、展示一次答案的窄流程。"
    "难度仍须来自最小正确实现的设计取舍与可击穿错误方案的验收，"
    "不能把两个页面、两个接口或输入与展示拆开凑模块，也不能叠加无关子系统。"
)
POLL_SECONDS = 3
JOB_KEY = "pairwise-auto-refill"
PREFILL_JOB_KEY = "pairwise-auto-prefill"
PREFILL2_JOB_KEY = "pairwise-auto-prefill:2"
PREFILL3_JOB_KEY = "pairwise-auto-prefill:3"
# Keep three independent questions in flight. Feature and Bug use one model
# route; 0-1 can race two independent routes inside its own prefill lane.
# route; the dispatch coordinator only claims reviewed drafts and therefore
# does not open a fourth generation route.
PREFILL_LANE_COUNT = 3
BALANCED_TASK_TYPES = ("0-1 代码生成", "Feature 迭代", "Bug 修复")
BUG_SOURCE_TARGET_SECONDS = 25 * 60
BUG_SOURCE_BUDGET_SECONDS = 45 * 60
BUG_SOURCE_ROTATION_GUARD_SECONDS = 60
BUG_V2_ACTIVE_TARGET_SECONDS = 45 * 60
BUG_V2_ACTIVE_BUDGET_SECONDS = 60 * 60
BUG_V2_WALL_BUDGET_SECONDS = 75 * 60
BUG_GENERATION_FLOWS = ("bug_generation_v1", "bug_generation_v2")
BUG_LINEAGE_MODE = "evolved"
FEATURE_SOURCE_BUDGET_SECONDS = 10 * 60
TYPE_FAILURE_LIMIT = 2
TYPE_RETRY_SECONDS = 10 * 60
TASK_TYPE_SLUGS = {
    "Feature 迭代": "feature",
    "Bug 修复": "bugfix",
}
KEYS = (
    "pairwise_auto_enabled",
    "pairwise_auto_task_type_mode",
    "pairwise_auto_bug_lineage_mode",
    "pairwise_auto_created",
    "pairwise_auto_project_directory",
    "pairwise_auto_start_at",
    "pairwise_auto_delay_hours",
    "pairwise_auto_detail",
    "pairwise_auto_error",
    "pairwise_auto_batch",
    "pairwise_auto_failures",
    "pairwise_auto_blocked_sources",
    "pairwise_auto_status",
    "pairwise_auto_next_type",
    "pairwise_auto_stage",
    "pairwise_auto_stage_started_at",
    "pairwise_auto_generation_started_at",
    "pairwise_auto_rewrite_count",
    "pairwise_auto_last_rejection",
    "pairwise_auto_rejection_history",
    "pairwise_auto_dispatch_reason",
    "pairwise_auto_pending_id",
    "pairwise_auto_pending_title",
    "pairwise_bug_generation_flow",
)

LOCK = threading.RLock()
PREFILL_PLAN_LOCK = threading.Lock()
GENERATION_FAILURE_LOCK = threading.Lock()
WAKE = threading.Event()
PREFILL_WAKE = threading.Event()
ACTIVE = False
PREFILL_ACTIVE = False
PREFILL_STAGE = ""
PREFILL_STARTED_AT = 0.0
PREFILL_STAGE_STARTED_AT = 0.0
PREFILL_CREATED_AT = ""
PREFILL_PENDING_ID = ""
PREFILL_TASK_TYPE = ""
PREFILL_TITLE = ""
PREFILL_NEXT_AT = 0.0
RETRY_NEXT_AT = 0.0
PREFILL_ERROR = ""
PREFILL2_WAKE = threading.Event()
PREFILL2_ACTIVE = False
PREFILL2_STAGE = ""
PREFILL2_STARTED_AT = 0.0
PREFILL2_STAGE_STARTED_AT = 0.0
PREFILL2_CREATED_AT = ""
PREFILL2_PENDING_ID = ""
PREFILL2_TASK_TYPE = ""
PREFILL2_TITLE = ""
PREFILL2_NEXT_AT = 0.0
PREFILL2_ERROR = ""
PREFILL3_WAKE = threading.Event()
PREFILL3_ACTIVE = False
PREFILL3_STAGE = ""
PREFILL3_STARTED_AT = 0.0
PREFILL3_STAGE_STARTED_AT = 0.0
PREFILL3_CREATED_AT = ""
PREFILL3_PENDING_ID = ""
PREFILL3_TASK_TYPE = ""
PREFILL3_TITLE = ""
PREFILL3_NEXT_AT = 0.0
PREFILL3_ERROR = ""
STARTED = False


def _prefill_snapshot(lane):
    if lane == 1:
        return {
            "active": PREFILL_ACTIVE, "stage": PREFILL_STAGE,
            "started_at": PREFILL_STARTED_AT,
            "stage_started_at": PREFILL_STAGE_STARTED_AT,
            "created_at": PREFILL_CREATED_AT, "pending_id": PREFILL_PENDING_ID,
            "task_type": PREFILL_TASK_TYPE, "title": PREFILL_TITLE,
            "next_at": PREFILL_NEXT_AT, "error": PREFILL_ERROR,
        }
    if lane == 2:
        return {
            "active": PREFILL2_ACTIVE, "stage": PREFILL2_STAGE,
            "started_at": PREFILL2_STARTED_AT,
            "stage_started_at": PREFILL2_STAGE_STARTED_AT,
            "created_at": PREFILL2_CREATED_AT, "pending_id": PREFILL2_PENDING_ID,
            "task_type": PREFILL2_TASK_TYPE, "title": PREFILL2_TITLE,
            "next_at": PREFILL2_NEXT_AT, "error": PREFILL2_ERROR,
        }
    if lane == 3:
        return {
            "active": PREFILL3_ACTIVE, "stage": PREFILL3_STAGE,
            "started_at": PREFILL3_STARTED_AT,
            "stage_started_at": PREFILL3_STAGE_STARTED_AT,
            "created_at": PREFILL3_CREATED_AT, "pending_id": PREFILL3_PENDING_ID,
            "task_type": PREFILL3_TASK_TYPE, "title": PREFILL3_TITLE,
            "next_at": PREFILL3_NEXT_AT, "error": PREFILL3_ERROR,
        }
    raise ValueError("后台备题通道编号无效")


def _update_prefill(lane, **changes):
    global PREFILL_ACTIVE, PREFILL_STAGE, PREFILL_STARTED_AT, PREFILL_STAGE_STARTED_AT
    global PREFILL_CREATED_AT, PREFILL_PENDING_ID, PREFILL_TASK_TYPE, PREFILL_TITLE
    global PREFILL_NEXT_AT, PREFILL_ERROR
    global PREFILL2_ACTIVE, PREFILL2_STAGE, PREFILL2_STARTED_AT, PREFILL2_STAGE_STARTED_AT
    global PREFILL2_CREATED_AT, PREFILL2_PENDING_ID, PREFILL2_TASK_TYPE, PREFILL2_TITLE
    global PREFILL2_NEXT_AT, PREFILL2_ERROR
    global PREFILL3_ACTIVE, PREFILL3_STAGE, PREFILL3_STARTED_AT, PREFILL3_STAGE_STARTED_AT
    global PREFILL3_CREATED_AT, PREFILL3_PENDING_ID, PREFILL3_TASK_TYPE, PREFILL3_TITLE
    global PREFILL3_NEXT_AT, PREFILL3_ERROR
    names = {
        "active": "ACTIVE", "stage": "STAGE", "started_at": "STARTED_AT",
        "stage_started_at": "STAGE_STARTED_AT", "created_at": "CREATED_AT",
        "pending_id": "PENDING_ID", "task_type": "TASK_TYPE", "title": "TITLE",
        "next_at": "NEXT_AT", "error": "ERROR",
    }
    prefix = {1: "PREFILL", 2: "PREFILL2", 3: "PREFILL3"}.get(lane)
    if prefix is None:
        raise ValueError("后台备题通道编号无效")
    for key, value in changes.items():
        if key not in names:
            raise ValueError(f"未知后台备题状态：{key}")
        globals()[f"{prefix}_{names[key]}"] = value


def _prefill_job_key(lane):
    keys = {1: PREFILL_JOB_KEY, 2: PREFILL2_JOB_KEY, 3: PREFILL3_JOB_KEY}
    if lane not in keys:
        raise ValueError("后台备题通道编号无效")
    return keys[lane]


def _prefill_wake(lane):
    wakes = {1: PREFILL_WAKE, 2: PREFILL2_WAKE, 3: PREFILL3_WAKE}
    if lane not in wakes:
        raise ValueError("后台备题通道编号无效")
    return wakes[lane]


def initialize(h):
    notebook.initialize(h)
    candidate_bank.initialize(h)
    bug_references.initialize(h)
    _, directory = h.resolve_project_directory(DEFAULT_DIRECTORY)
    directory.mkdir(parents=True, exist_ok=True)
    history.ensure(h)
    defaults = {
        "pairwise_auto_enabled": "0",
        "pairwise_auto_task_type_mode": "balanced",
        "pairwise_auto_bug_lineage_mode": BUG_LINEAGE_MODE,
        "pairwise_auto_created": "0",
        "pairwise_auto_project_directory": DEFAULT_DIRECTORY,
        "pairwise_auto_start_at": "",
        "pairwise_auto_delay_hours": "0",
        "pairwise_auto_detail": "自动补题已关闭",
        "pairwise_auto_error": "",
        "pairwise_auto_batch": "",
        "pairwise_auto_failures": "0",
        "pairwise_auto_blocked_sources": "{}",
        "pairwise_auto_status": "",
        "pairwise_auto_next_type": "",
        "pairwise_auto_stage": "",
        "pairwise_auto_stage_started_at": "",
        "pairwise_auto_generation_started_at": "",
        "pairwise_auto_rewrite_count": "0",
        "pairwise_auto_last_rejection": "",
        "pairwise_auto_rejection_history": "[]",
        "pairwise_auto_dispatch_reason": "",
        "pairwise_auto_pending_id": "",
        "pairwise_auto_pending_title": "",
        # Existing installations opt in explicitly so an in-flight V1 source
        # can finish under the policy snapshot it started with. Production is
        # switched to V2 after the safe reload.
        "pairwise_bug_generation_flow": "bug_generation_v1",
        "pairwise_blueprint_cursor": "0",
    }
    now = h.now_text()
    with h.db_connection() as db:
        _initialize_health(db)
        # Difficulty/quality rejection is a direction-selection signal, not a
        # transient outage. Remove durable Bug backoffs created by older code.
        db.execute("""UPDATE pairwise_generation_health
            SET failures=0,retry_at=0
            WHERE task_type='Bug 修复' AND error_kind='quality'
              AND (failures<>0 OR retry_at<>0)""")
        db.executemany(
            "INSERT OR IGNORE INTO settings(key,value,updated_at) VALUES (?,?,?)",
            [(key, value, now) for key, value in defaults.items()],
        )
        # Bug generation always follows the newest completed product in a
        # lineage.  Keep the persisted compatibility key normalized so older
        # pages cannot restore the removed "one Bug per lineage" policy.
        db.execute(
            "UPDATE settings SET value=?,updated_at=? WHERE key=? AND value<>?",
            (BUG_LINEAGE_MODE, now, "pairwise_auto_bug_lineage_mode", BUG_LINEAGE_MODE),
        )


def _values(h):
    initialize(h)
    values = h.settings_values(KEYS)
    return {key: values.get(key, "") for key in KEYS}


def _integer(value, fallback):
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def allowed_task_types(h, values=None):
    values = _values(h) if values is None else values
    return (("Bug 修复",) if values.get("pairwise_auto_task_type_mode") == "bug_only"
            else BALANCED_TASK_TYPES)


def _bug_generation_flow(h, values=None):
    values = _values(h) if values is None else values
    flow = values.get("pairwise_bug_generation_flow") or "bug_generation_v1"
    return flow if flow in BUG_GENERATION_FLOWS else "bug_generation_v1"


def _ready_count(h, values=None):
    if allowed_task_types(h, values) == BALANCED_TASK_TYPES:
        return candidate_bank.ready_count(h)
    return candidate_bank.ready_task_counts(h).get("Bug 修复", 0)


def _candidate_capacity(h, values):
    limits = {"max_ready": candidate_bank.READY_LIMIT}
    if allowed_task_types(h, values) != BALANCED_TASK_TYPES:
        limits["capacity_task_type"] = "Bug 修复"
    return limits


def _prefill_planned_counts(values, counts, ready, active_types):
    """Reserve one lane per task type at the start of a balanced batch."""
    created = max(0, _integer(values.get("pairwise_auto_created"), 0))
    fresh_balanced_batch = (
        created == 0
        and values.get("pairwise_auto_task_type_mode") != "bug_only"
    )
    planned = {
        kind: (0 if fresh_balanced_batch else int(counts.get(kind, 0)))
        + int(ready.get(kind, 0))
        for kind in BALANCED_TASK_TYPES
    }
    for kind in active_types:
        if kind in planned:
            planned[kind] += 1
    return planned


def directory_choices(h):
    initialize(h)
    choices = []
    for value in (DEFAULT_DIRECTORY, *h.available_project_directories()):
        try:
            path = h.resolve_project_directory(value)[1]
        except h.WorkflowError:
            continue
        text = str(path)
        if text not in choices:
            choices.append(text)
    return choices


def configuration(h):
    values = _values(h)
    created = max(0, _integer(values["pairwise_auto_created"], 0))
    start_epoch = _integer(values["pairwise_auto_start_at"], 0) or None
    directory_value = values["pairwise_auto_project_directory"] or DEFAULT_DIRECTORY
    _, path = h.resolve_project_directory(directory_value)
    with LOCK:
        active = ACTIVE
        prefill_states = [_prefill_snapshot(lane) | {"lane": lane}
                          for lane in range(1, PREFILL_LANE_COUNT + 1)]
    now = int(time.time())
    delay_hours = max(0, _integer(values["pairwise_auto_delay_hours"], 0))
    if start_epoch and start_epoch > now and not delay_hours:
        delay_hours = max(1, (start_epoch - now + 3599) // 3600)
    counts, qualified, excluded = daily_task_type_counts(h)
    status = values["pairwise_auto_status"] or "ready"
    if not values["pairwise_auto_enabled"] == "1":
        status = "needs_attention" if values["pairwise_auto_error"] else "disabled"
    elif start_epoch and start_epoch > now:
        status = "scheduled"
    elif status == "generating" and not active:
        status = "ready"
    task_types = allowed_task_types(h, values)
    next_type = values["pairwise_auto_next_type"]
    if next_type not in task_types:
        next_type = min(task_types, key=lambda kind: (counts[kind], BALANCED_TASK_TYPES.index(kind)))
    if status in {"needs_attention", "waiting_baseline"}:
        next_type = ""
    recovery = None
    if status == "needs_attention" and not active:
        kind, source, unavailable = next_available_task(h, counts, _blocked_sources(values))
        if kind:
            number = str(source["pair"].get("display_number") or "") if source else ""
            title = str(source["pair"]["title"]) if source else ""
            recovery = {
                "task_type": kind, "source_id": source["pair"]["id"] if source else "",
                "source_title": title, "source_number": number,
                "detail": (f"当前已有可用基线 {number} {title}，继续后优先尝试 {kind}。"
                           if source else "当前没有可用的迭代基线，继续后换方向生成新的 0-1 题。"),
            }
    generating = active and status == "generating"
    started_at = _integer(values["pairwise_auto_generation_started_at"], 0)
    active_prefills = []
    for state in prefill_states:
        if not state["active"]:
            continue
        active_prefills.append({
            "lane": state["lane"], "pending_id": state["pending_id"],
            "title": state["title"], "task_type": state["task_type"],
            "stage": state["stage"], "created_at": state["created_at"],
            "elapsed_seconds": int(time.monotonic() - state["started_at"]),
            "stage_elapsed_seconds": (
                int(time.monotonic() - state["stage_started_at"])
                if state["stage_started_at"] else 0
            ),
        })
    primary_prefill = active_prefills[0] if active_prefills else {}
    prefill_error = "；".join(dict.fromkeys(
        str(state["error"]) for state in prefill_states if state["error"]
    ))

    def elapsed(key):
        started = _integer(values[key], 0)
        return max(0, now - started) if generating and started else 0

    return {
        "enabled": values["pairwise_auto_enabled"] == "1",
        "task_type_mode": values["pairwise_auto_task_type_mode"] or "balanced",
        "bug_lineage_mode": BUG_LINEAGE_MODE,
        "bug_lineage_reuse": True,
        "task_types": list(task_types),
        "created_count": created,
        "project_directory": str(path),
        "start_after_hours": delay_hours,
        "scheduled_for": datetime.fromtimestamp(start_epoch).astimezone().isoformat() if start_epoch else "",
        "scheduled": bool(start_epoch and start_epoch > now),
        "generating": generating,
        "status": status,
        "needs_attention": status == "needs_attention",
        "detail": values["pairwise_auto_detail"] or "自动补题已关闭",
        "error": values["pairwise_auto_error"],
        "history_file": str(h.PAIRWISE_HISTORY_PATH),
        "task_type_counts": counts,
        "daily_qualified_counts": qualified,
        "daily_downgraded_counts": excluded,
        "daily_count_date": h.now_text()[:10],
        "next_task_type": next_type,
        "task_mix_policy": "daily_balance" if task_types == BALANCED_TASK_TYPES else "bug_only",
        "task_mix_max_difference": None,
        "generation_race_lanes": 1,
        "candidate_target": candidate_bank.READY_LIMIT,
        "prefill_parallel": PREFILL_LANE_COUNT,
        "prefill_lanes": active_prefills,
        "prefill_generating": bool(active_prefills),
        "prefill_stage": primary_prefill.get("stage", ""),
        "prefill_elapsed_seconds": primary_prefill.get("elapsed_seconds", 0),
        "prefill_stage_elapsed_seconds": primary_prefill.get("stage_elapsed_seconds", 0),
        "prefill_pending_id": primary_prefill.get("pending_id", ""),
        "prefill_task_type": primary_prefill.get("task_type", ""),
        "prefill_title": primary_prefill.get("title", ""),
        "prefill_created_at": primary_prefill.get("created_at", ""),
        "prefill_error": prefill_error,
        "bug_source_budget_seconds": (BUG_V2_WALL_BUDGET_SECONDS
                                      if _bug_generation_flow(h, values) == bug_generation_v2.FLOW
                                      else BUG_SOURCE_BUDGET_SECONDS),
        "bug_source_target_seconds": (BUG_V2_ACTIVE_TARGET_SECONDS
                                      if _bug_generation_flow(h, values) == bug_generation_v2.FLOW
                                      else BUG_SOURCE_TARGET_SECONDS),
        "bug_active_budget_seconds": (BUG_V2_ACTIVE_BUDGET_SECONDS
                                      if _bug_generation_flow(h, values) == bug_generation_v2.FLOW
                                      else BUG_SOURCE_BUDGET_SECONDS),
        "bug_generation_strategy": "existing_first_then_seed",
        "bug_generation_flow": _bug_generation_flow(h, values),
        "bug_verification_mode": "static_first",
        "feature_source_budget_seconds": FEATURE_SOURCE_BUDGET_SECONDS,
        "candidate_ready": _ready_count(h, values),
        "blocked_source_count": len(source_cooldowns(h)) + len(source_exhaustions(h)),
        "source_cooldowns": source_cooldowns(h),
        "source_exhaustions": source_exhaustions(h),
        "stage": values["pairwise_auto_stage"] if generating else "",
        "stage_elapsed_seconds": elapsed("pairwise_auto_stage_started_at"),
        "generation_elapsed_seconds": elapsed("pairwise_auto_generation_started_at"),
        "rewrite_count": _integer(values["pairwise_auto_rewrite_count"], 0),
        "last_rejection": values["pairwise_auto_last_rejection"],
        # This describes the last dispatch, not the currently recomputed
        # recovery plan. A paused run may retain an obsolete root-wide block.
        "dispatch_reason": "" if status == "needs_attention" else values["pairwise_auto_dispatch_reason"],
        "recovery": recovery,
        "pending_id": values["pairwise_auto_pending_id"] if generating else "",
        "pending_title": values["pairwise_auto_pending_title"] if generating else "",
        "generation_started_at": (
            datetime.fromtimestamp(started_at).astimezone().isoformat()
            if generating and started_at else ""
        ),
    }


def _parse_start_after_hours(h, value):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 168:
        raise h.WorkflowError("预约开始必须是 0 至 168 小时的整数")
    return value


def configure(h, payload):
    global RETRY_NEXT_AT
    enabled = payload.get("enabled")
    if not isinstance(enabled, bool):
        raise h.WorkflowError("自动补题开关必须是布尔值")
    task_type_mode = payload.get("task_type_mode", _values(h)["pairwise_auto_task_type_mode"] or "balanced")
    if task_type_mode not in ("balanced", "bug_only"):
        raise h.WorkflowError("补题类型必须是 balanced 或 bug_only")
    bug_generation_flow = payload.get(
        "bug_generation_flow", _values(h).get("pairwise_bug_generation_flow") or "bug_generation_v1")
    if bug_generation_flow not in BUG_GENERATION_FLOWS:
        raise h.WorkflowError("Bug 生成流程必须是 bug_generation_v1 或 bug_generation_v2")
    group, directory = h.resolve_project_directory(
        str(payload.get("project_directory") or DEFAULT_DIRECTORY)
    )
    if directory.exists() and not directory.is_dir():
        raise h.WorkflowError("所选保存路径不是目录")
    directory.mkdir(parents=True, exist_ok=True)
    delay_hours = 0
    start_at = None
    if enabled:
        delay_hours = _parse_start_after_hours(h, payload.get("start_after_hours", 0))
        start_at = int(time.time()) + delay_hours * 60 * 60 if delay_hours else None
    batch = uuid.uuid4().hex[:12] if enabled else ""
    if enabled and start_at:
        when = datetime.fromtimestamp(start_at).astimezone().strftime("%m-%d %H:%M")
        detail = f"已预约 {delay_hours} 小时后（{when}）开始；开启后持续补题，直到手动关闭"
    elif enabled and task_type_mode == "bug_only":
        detail = ("自动补题已开启；仅生成 Bug 修复，暂无合格来源或来源冷却时等待，"
                  "不补 0-1 或 Feature；同一血缘只使用最新归档代码，具体 Pair 产物仍只用一次")
    elif enabled:
        detail = "自动补题已开启；只按当天创建的有效或待复评题目调节 1:1:1，缺少合格基线时先补 0-1"
    else:
        detail = "自动补题已关闭；已启动的 A/B 仍会继续运行"
    with LOCK:
        for lane in range(1, PREFILL_LANE_COUNT + 1):
            _update_prefill(lane, error="", next_at=0.0)
        RETRY_NEXT_AT = 0.0
        h.write_settings(
            {
                "pairwise_auto_enabled": "1" if enabled else "0",
                "pairwise_auto_task_type_mode": task_type_mode,
                "pairwise_auto_bug_lineage_mode": BUG_LINEAGE_MODE,
                "pairwise_auto_created": "0" if enabled else _values(h)["pairwise_auto_created"],
                "pairwise_auto_project_directory": group,
                "pairwise_auto_start_at": str(start_at or ""),
                "pairwise_auto_delay_hours": str(delay_hours if enabled else 0),
                "pairwise_auto_detail": detail,
                "pairwise_auto_error": "",
                "pairwise_auto_batch": batch,
                "pairwise_auto_failures": "0",
                "pairwise_auto_status": "scheduled" if start_at else "ready" if enabled else "disabled",
                "pairwise_auto_next_type": "",
                "pairwise_auto_stage": "",
                "pairwise_auto_stage_started_at": "",
                "pairwise_auto_generation_started_at": "",
                "pairwise_auto_rewrite_count": "0",
                "pairwise_auto_dispatch_reason": "",
                "pairwise_auto_pending_id": "",
                "pairwise_auto_pending_title": "",
                "pairwise_bug_generation_flow": bug_generation_flow,
            }
        )
    if not enabled:
        h.cancel_background_job(JOB_KEY)
        for lane in range(1, PREFILL_LANE_COUNT + 1):
            h.cancel_background_job(_prefill_job_key(lane))
    else:
        h.clear_job_cancellation(JOB_KEY)
        for lane in range(1, PREFILL_LANE_COUNT + 1):
            h.clear_job_cancellation(_prefill_job_key(lane))
    WAKE.set()
    for lane in range(1, PREFILL_LANE_COUNT + 1):
        _prefill_wake(lane).set()
    return configuration(h)


def _current(h, batch):
    values = _values(h)
    return values["pairwise_auto_enabled"] == "1" and values["pairwise_auto_batch"] == batch


def _progress(h, batch, task_type, text, rejection=""):
    if not _current(h, batch):
        return
    values = _values(h)
    number = max(1, _integer(values["pairwise_auto_created"], 0) + 1)
    updates = {
        "pairwise_auto_detail": f"正在自动生成第 {number} 道 {task_type}：{text}",
        "pairwise_auto_error": "",
        "pairwise_auto_status": "generating",
        "pairwise_auto_next_type": task_type,
        "pairwise_auto_stage": text,
    }
    now = str(int(time.time()))
    if text != values["pairwise_auto_stage"]:
        updates["pairwise_auto_stage_started_at"] = now
    if not values["pairwise_auto_generation_started_at"]:
        updates["pairwise_auto_generation_started_at"] = now
    if rejection:
        updates["pairwise_auto_last_rejection"] = str(rejection)[:1200]
    if "规则 C 未通过" in text:
        # The visible race summary starts with "2 路竞速", and can contain
        # both lanes; count lanes currently retrying, not poll repetitions.
        updates["pairwise_auto_rewrite_count"] = str(max(
            _integer(values["pairwise_auto_rewrite_count"], 0),
            text.count("规则 C 未通过"),
        ))
    h.write_settings(updates)


def _blocked_sources(values):
    try:
        parsed = json.loads(values.get("pairwise_auto_blocked_sources") or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _source_key(h, source, target_task_type):
    return f"{target_task_type}:{source['pair']['id']}"


def source_exhaustions(h):
    """Sources fully screened by the current Bug-generation policy."""
    with h.db_connection() as db:
        rows = db.execute("""SELECT source_pair_id,task_type,state,reason,updated_at
            FROM pairwise_source_locks WHERE state=? ORDER BY updated_at""",
                          (sources.exhausted_state(),)).fetchall()
    return [dict(row) for row in rows]


def _timeout_type_backoff(h, task_type):
    """Do not spend every generation lane on a type with repeated source timeouts."""
    stalled = [row for row in source_cooldowns(h)
               if row["task_type"] == task_type
               and ("超时" in str(row["reason"]) or
                    re.search(r"超过\s*\d+\s*分钟", str(row["reason"])))]
    if len({row["source_pair_id"] for row in stalled}) < 3:
        return None
    return min(row["cooldown_until"] for row in stalled)


def _initialize_health(db):
    db.execute("""CREATE TABLE IF NOT EXISTS pairwise_generation_health (
        task_type TEXT PRIMARY KEY, failures INTEGER NOT NULL DEFAULT 0,
        retry_at REAL NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL)""")
    if 'error_kind' not in {row[1] for row in db.execute('PRAGMA table_info(pairwise_generation_health)')}:
        try:
            db.execute("ALTER TABLE pairwise_generation_health ADD COLUMN error_kind TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError as exc:
            # Two generation lanes may initialize a freshly restored database
            # at the same time. The second ALTER is already satisfied.
            if 'duplicate column name' not in str(exc).casefold():
                raise


def _type_backoff(h, task_type):
    with h.db_connection() as db:
        _initialize_health(db)
        row = db.execute("SELECT * FROM pairwise_generation_health WHERE task_type=?",
                         (task_type,)).fetchone()
    if row and task_type == 'Bug 修复' and row['error_kind'] == 'quality':
        return None
    retry_at = row['retry_at'] if row else 0
    if (row and task_type == 'Feature 迭代' and row['error_kind'] == 'quality'
            and sources.transient_cooldown_reason(row['reason'])):
        # Older builds counted Feature stage timeouts as quality failures and
        # could pause the whole task type for ten minutes. Treat those rows as
        # the short technical retry they should have been across a restart.
        try:
            retry_at = min(
                retry_at,
                datetime.strptime(row['updated_at'], '%Y-%m-%d %H:%M:%S %z').timestamp()
                + bug_runtime.RETRY_SECONDS,
            )
        except ValueError:
            pass
    if (row and task_type == 'Bug 修复' and not row['error_kind']
            and bug_runtime.legacy_timeout_reason(row['reason'])):
        try:
            retry_at = min(retry_at, datetime.strptime(row['updated_at'], '%Y-%m-%d %H:%M:%S %z').timestamp()
                           + bug_runtime.RETRY_SECONDS)
        except ValueError:
            pass
    return retry_at if retry_at > time.time() else None


def _record_generation_outcome(h, task_type, error=None):
    """Share progress across dispatch/prefill and restarts, without changing quotas."""
    if task_type not in {'Feature 迭代', 'Bug 修复'}:
        return
    if isinstance(error, (h.JobCancelled, h.UpstreamCapacityError)):
        return
    with h.db_connection() as db:
        _initialize_health(db)
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM pairwise_generation_health WHERE task_type=?',
                         (task_type,)).fetchone()
        legacy_timeout = bool(row and task_type == 'Bug 修复' and not row['error_kind']
                              and bug_runtime.legacy_timeout_reason(row['reason']))
        previous_failures = int(row['failures']) if row and not legacy_timeout else 0
        previous_retry = float(row['retry_at']) if row and not legacy_timeout else 0
        technical = bool(error) and _transient_generation_failure(h, error)
        quality_without_backoff = task_type == 'Bug 修复' and error and not technical
        failures = (previous_failures + (0 if technical else 1)) if error else 0
        retry_at = previous_retry if error else 0
        kind = 'quality' if error else ''
        if quality_without_backoff:
            # The rejected direction is already persisted per source. Rotate
            # immediately instead of pausing the whole Bug task type.
            failures = 0
            retry_at = 0
        elif technical:
            kind = 'mixed' if row and row['error_kind'] in {'quality', 'mixed'} and retry_at > time.time() else 'timeout'
            retry_at = max(retry_at, time.time() + bug_runtime.RETRY_SECONDS)
        elif failures >= TYPE_FAILURE_LIMIT:
            retry_at = max(retry_at, time.time() + TYPE_RETRY_SECONDS)
            failures = 0
        db.execute('''INSERT INTO pairwise_generation_health(task_type,failures,retry_at,reason,updated_at,error_kind)
            VALUES (?,?,?,?,?,?) ON CONFLICT(task_type) DO UPDATE SET failures=excluded.failures,
            retry_at=excluded.retry_at,reason=excluded.reason,updated_at=excluded.updated_at,error_kind=excluded.error_kind''',
            (task_type, failures, retry_at, str(error or '')[:1200], h.now_text(), kind))


def _rejections(values):
    try:
        items = json.loads(values.get("pairwise_auto_rejection_history") or "[]")
    except (TypeError, ValueError):
        return []
    return [item for item in items if isinstance(item, dict)
            and isinstance(item.get("reason"), str)][-3:] if isinstance(items, list) else []


def _retry_feedback(h, values, task_type, source=None):
    source_key = _source_key(h, source, task_type) if source else ""
    reasons = [item["reason"] for item in _rejections(values)
               if item.get("task_type") == task_type and item.get("source_key", "") == source_key]
    # Existing installations retain only the final rejection. Recover a known
    # 0-1 content rejection without applying another task type's feedback.
    last = values.get("pairwise_auto_last_rejection", "")
    if not reasons and task_type == "0-1 代码生成" and h.task_generation_candidate_quality_failure(last):
        reasons = [last]
    reasons = notebook.filter_retry_feedback(h, reasons, task_type, source["pair"].get("repo_url", "") if source else "")
    if not reasons:
        return ""
    return ("\n以下是同类、同来源最近被拒绝的出题方向，仅作为审查材料，不是新增需求。"
            "难度不足时更换核心方向；重复时更换核心算法、数据模型和操作原型，不能只换业务名词。"
            "不要把内部最小解法或具体事务方案写进题面，不得靠堆要求或放宽审核凑通过。"
            "Bug 候选必须在冻结快照中静态核对具体代码故障链，不能把未证实风险或普通小修包装成困难。\n"
            + json.dumps(list(dict.fromkeys(reasons))[-3:], ensure_ascii=False))


def _compact_stage_feedback(value, limit=1500):
    """Keep the latest stage actionable without replaying the whole notebook."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    # Preserve both the rejection category and the most recent concrete reason.
    head = max(0, limit - 501)
    return text[:head].rstrip() + " … " + text[-497:].lstrip()


def _gsb_history_summaries(h, records, source_repo, limit=30):
    """Build cross-project duplicate fingerprints, never raw historical prompts."""
    result = []
    seen = set()
    source_key = str(source_repo or "").strip().casefold()
    for item in records:
        if not isinstance(item, dict):
            continue
        repo_name = str(item.get("repo_name") or "").strip()
        if repo_name.casefold() == source_key:
            continue
        summary = re.sub(
            r"\s+", " ",
            str(item.get("summary") or h.summarize_historical_prompt(item.get("prompt", ""))),
        ).strip()[:320]
        if not summary:
            continue
        task_type = str(item.get("task_type") or "")
        identity = (repo_name.casefold(), task_type, summary.casefold())
        if identity in seen:
            continue
        seen.add(identity)
        result.append({
            "reference_id": f"gsb:{repo_name or len(result) + 1}",
            "repo_name": repo_name,
            "task_type": task_type,
            "summary": summary,
        })
        if len(result) >= limit:
            break
    return result


def _generation_failed(h, batch, exc, source=None, target_task_type=""):
    # Three lanes may fail at nearly the same instant. Serialize the durable
    # counter/rejection update so concurrent reads cannot all overwrite it as
    # the same "first" failure.
    with GENERATION_FAILURE_LOCK:
        return _generation_failed_locked(h, batch, exc, source, target_task_type)


def _generation_failed_locked(h, batch, exc, source=None, target_task_type=""):
    if not _current(h, batch):
        return
    values = _values(h)
    notebook.generation_failed(h, _notebook_context(h, target_task_type, source),
                               values.get("pairwise_auto_pending_id") or batch, str(exc))
    if isinstance(exc, h.UpstreamCapacityError):
        h.write_settings({
            "pairwise_auto_enabled": "1",
            "pairwise_auto_status": "waiting_capacity",
            "pairwise_auto_detail": "上游容量不足，60 秒后重试；不计入题面或来源失败",
            "pairwise_auto_error": str(exc)[:1200],
            "pairwise_auto_next_type": "",
        })
        return
    rejections = _rejections(values)
    rejections.append({"task_type": target_task_type,
                       "source_key": _source_key(h, source, target_task_type) if source else "",
                       "reason": str(exc)[:1200], "recorded_at": h.now_text()})
    saved_rejections = json.dumps(rejections[-3:], ensure_ascii=False)
    if not source and h.task_generation_candidate_quality_failure(str(exc)):
        # A completed generation followed by a content rejection proves that
        # the generation infrastructure is healthy. Keep the rejection as
        # retry guidance, but do not let normal candidate selection trip the
        # global three-technical-failures circuit breaker.
        h.write_settings(
            {
                "pairwise_auto_enabled": "1",
                "pairwise_auto_failures": "0",
                "pairwise_auto_status": "ready",
                "pairwise_auto_next_type": "",
                "pairwise_auto_detail": (
                    "候选质量复核未通过，已保留失败依据；正在换方向继续生成"
                ),
                "pairwise_auto_error": str(exc)[:1200],
                "pairwise_auto_last_rejection": str(exc)[:1200],
                "pairwise_auto_rejection_history": saved_rejections,
            }
        )
        return
    if source:
        lock = sources.get_lock(h, source['pair']['id'], target_task_type)
        cooling = bool(lock and lock['cooldown_until'] > time.time())
        exhausted = bool(lock and lock['state'] == sources.exhausted_state())
        detail = (
            '该冻结源码在当前生成策略下已完成困难 Bug 筛查；策略升级后可自动重新检查'
            if exhausted else
            f"来源冷却至 {datetime.fromtimestamp(lock['cooldown_until']).astimezone():%m-%d %H:%M}，原因：{str(exc)[:200]}"
            if cooling else '后续换真实方向或其他来源，不永久排除根项目'
        )
        h.write_settings(
            {
                "pairwise_auto_enabled": "1",
                "pairwise_auto_failures": "0",
                "pairwise_auto_status": "ready",
                "pairwise_auto_next_type": "",
                "pairwise_auto_detail": (
                    f"{source['pair']['title']} 本次生成未通过，已保留失败依据；{detail}"
                ),
                "pairwise_auto_error": str(exc)[:1200],
                "pairwise_auto_last_rejection": str(exc)[:1200],
                "pairwise_auto_rejection_history": saved_rejections,
            }
        )
        return
    failures = _integer(values["pairwise_auto_failures"], 0) + 1
    stopped = failures >= 3
    h.write_settings(
        {
            "pairwise_auto_enabled": "0" if stopped else "1",
            "pairwise_auto_failures": str(failures),
            "pairwise_auto_status": "needs_attention" if stopped else "retrying",
            "pairwise_auto_next_type": "",
            "pairwise_auto_detail": (
                "自动补题连续失败 3 次，已暂停"
                if stopped
                else f"第 {failures}/3 次生成失败，将自动再试"
            ),
            "pairwise_auto_error": str(exc)[:1200],
            "pairwise_auto_last_rejection": str(exc)[:1200],
            "pairwise_auto_rejection_history": saved_rejections,
        }
    )


def _source_failure_policy(h, exc):
    """Only substantive direction failures spend the source's retry allowance."""
    if _transient_generation_failure(h, exc):
        return False, False
    reason = str(exc)
    # A source-wide search with no qualifying direction is already exhausted
    # under the current generation policy. Retrying the same frozen snapshot
    # adds no evidence; a later policy version automatically makes it eligible.
    if sources.source_exhaustion_reason(reason):
        return True, False
    # A substantive rejection spends a source attempt so source ordering moves
    # to a fresher baseline. It does not cool the source: if no alternative is
    # available, the next poll may seed a different, non-rejected Bug direction.
    return True, False


def task_type_counts(h):
    counts = {task_type: 0 for task_type in BALANCED_TASK_TYPES}
    with h.db_connection() as db:
        rows = db.execute(
            """SELECT task_type,COUNT(*) AS total FROM pairwise_tasks
               WHERE deleted_at IS NULL
                 AND task_type IN ('0-1 代码生成','Feature 迭代','Bug 修复')
               GROUP BY task_type"""
        ).fetchall()
    for row in rows:
        counts[str(row["task_type"])] = int(row["total"])
    return counts


def daily_task_type_counts(h):
    """Created-today mix; pending reviews reserve a slot, proven easy work does not."""
    counts = dict.fromkeys(BALANCED_TASK_TYPES, 0)
    qualified = counts.copy()
    downgraded = counts.copy()
    today = h.now_text()[:10]
    with h.db_connection() as db:
        rows = db.execute("""SELECT task_type,difficulty_status,difficulty_review
            FROM pairwise_tasks WHERE deleted_at IS NULL AND substr(created_at,1,10)=?
              AND task_type IN ('0-1 代码生成','Feature 迭代','Bug 修复')""", (today,)).fetchall()
    for row in rows:
        kind = str(row['task_type'])
        try:
            review = json.loads(row['difficulty_review'] or '{}')
        except (TypeError, ValueError):
            review = {}
        level = review.get('level') if isinstance(review, dict) else None
        accepted = {'困难', '地狱'}
        if row['difficulty_status'] == 'complete' and level not in accepted:
            downgraded[kind] += 1
        else:
            counts[kind] += 1
            if row['difficulty_status'] == 'complete' and level in accepted:
                qualified[kind] += 1
    return counts, qualified, downgraded


def balanced_task_type(counts):
    return min(
        BALANCED_TASK_TYPES,
        key=lambda task_type: (
            int(counts.get(task_type, 0)),
            BALANCED_TASK_TYPES.index(task_type),
        ),
    )


def next_available_task(
        h, counts, blocked_sources, *, ready_mode="any", excluded_task_types=()):
    """Repay deficits when possible; bootstrap new roots when no source fits.

    Recompute on every dispatch so a newly archived baseline immediately gets
    priority over another leading 0-1 task. No maximum count difference applies.
    """
    unavailable = []
    ready_counts = candidate_bank.ready_task_counts(h) if ready_mode == "only" else {}
    task_types = allowed_task_types(h)
    retry_action = "等待 Bug 冷却结束" if task_types == ("Bug 修复",) else "先补其他题型"
    ordered = sorted(task_types, key=lambda kind: (
        int(counts.get(kind, 0)), BALANCED_TASK_TYPES.index(kind)
    ))
    excluded = set(excluded_task_types)
    for kind in ordered:
        if kind in excluded:
            continue
        if ready_mode == "only" and not ready_counts.get(kind, 0):
            continue
        if ready_mode != "only":
            retry_at = _type_backoff(h, kind)
            if retry_at:
                unavailable.append(
                    f"{kind}：连续出题未产出合格候选，暂缓到 "
                    f"{datetime.fromtimestamp(retry_at).astimezone():%m-%d %H:%M}；{retry_action}"
                )
                continue
            retry_at = _timeout_type_backoff(h, kind)
            if retry_at:
                unavailable.append(
                    f"{kind}：连续多个来源出题超时，暂缓到 "
                    f"{datetime.fromtimestamp(retry_at).astimezone():%m-%d %H:%M}；{retry_action}"
                )
                continue
        if kind == "0-1 代码生成":
            return kind, None, unavailable
        source = (continuation_source(h, kind, blocked_sources)
                  if ready_mode == "any" else
                  continuation_source(h, kind, blocked_sources, ready_mode=ready_mode))
        if source:
            return kind, source, unavailable
        if ready_mode == "only":
            continue
        cooling = [r for r in source_cooldowns(h) if r['task_type'] == kind]
        exhausted = [r for r in source_exhaustions(h) if r['task_type'] == kind]
        reason = (f"{len(cooling)} 个来源冷却中，最早于 "
                  f"{datetime.fromtimestamp(min(r['cooldown_until'] for r in cooling)).astimezone():%m-%d %H:%M}"
                  " 重新检查；其他来源暂无合格产物"
                  if cooling else
                  f"{len(exhausted)} 个来源已在当前策略完成筛查；其他来源暂无合格产物"
                  if exhausted else "没有可用且未重复使用的单侧归档基线")
        if (not cooling and not exhausted and kind == "Bug 修复"
                and sources.bug_lineage_reuse_enabled(h)):
            reason = "没有可用的最新归档产物；同一具体 Pair 产物不会重复使用，也不会回退旧代码"
        unavailable.append(f"{kind}：{reason}")
    return "", None, unavailable


def _mix_text(counts):
    return " / ".join(
        f"{task_type.replace(' 代码生成', '')} {int(counts.get(task_type, 0))}"
        for task_type in BALANCED_TASK_TYPES
    )


def _active_attempt_count(h):
    """Count running and queued Pair attempts so continuous refill cannot pile up."""
    with h.db_connection() as db:
        row = db.execute(
            """SELECT COUNT(*) FROM pairwise_attempts
               WHERE status IN ('queued','starting','running','recovering','archiving')"""
        ).fetchone()
    return int(row[0])


def _queued_attempt_count(h):
    """Bound the Claude backlog without coupling prompt generation to live slots."""
    with h.db_connection() as db:
        row = db.execute(
            "SELECT COUNT(*) FROM pairwise_attempts WHERE status='queued'"
        ).fetchone()
    return int(row[0])


def _verification_passed(attempt):
    checks = attempt.get("verification") or []
    return all(int(item.get("exit_code", 1)) == 0 for item in checks)


def _preferred_sides(pair, attempts):
    verdict = str(pair.get("gsb") or "") if pair.get("human_confirmed") else ""
    if not verdict and pair.get("reference_status") == "complete":
        verdict = str(pair.get("reference_gsb") or "")
    ordered = sorted(attempts, key=sources.completion_key)
    winner = {"A 更好": "A", "B 更好": "B"}.get(verdict)
    return sorted(ordered, key=lambda a: a['side'] != winner) if winner else ordered


def continuation_source(h, target_task_type, blocked_sources=None, *, ready_mode="any"):
    if target_task_type not in {"Feature 迭代", "Bug 修复"}:
        return None
    lineage_reuse = sources.bug_lineage_reuse_enabled(h)
    if target_task_type == "Bug 修复":
        # Prefer untouched, completed 0-1 products from the old zzzz archive.
        # They provide a much wider fault surface than repeatedly deriving a
        # Bug task from an already narrowed Bug descendant.  The picker still
        # requires a clean local Git snapshot, successful historical Docker
        # verification and an unconsumed source lock.
        legacy_source = legacy_baselines.pick(h, ready_mode=ready_mode)
        if legacy_source:
            return legacy_source
    # Fall back to Pair-wise products only after eligible zzzz roots. Both
    # selectors enforce the same terminal Bug rule at the project-root level.
    with h.db_connection() as db:
        source_type_filter = (
            "AND task_type<>'Bug 修复'"
            if target_task_type == 'Bug 修复' and not lineage_reuse else ''
        )
        pair_ids = [
            str(row["id"])
            for row in db.execute(
                f"""SELECT id FROM pairwise_tasks
                   WHERE deleted_at IS NULL AND repo_url<>'' {source_type_filter}
                   ORDER BY created_at,id"""
            ).fetchall()
        ]
    pair_records = [m.task(h, pair_id) for pair_id in pair_ids]
    roots = {}
    newest_non_bug = {}
    for pair in pair_records:
        lineage = _lineage(h, pair)
        root_id = str(lineage[0]["id"] if lineage else pair["id"])
        roots[str(pair["id"])] = root_id
        key = (int(pair.get("sequence_number") or 0),
               str(pair.get("created_at") or ""), str(pair["id"]))
        if key > newest_non_bug.get(root_id, ((0, "", ""), ""))[0]:
            newest_non_bug[root_id] = (key, str(pair["id"]))
    candidates = []
    for pair in pair_records:
        pair_id = str(pair["id"])
        if (target_task_type == 'Bug 修复' and pair['task_type'] == 'Bug 修复'
                and not lineage_reuse):
            continue
        root_id = roots[pair_id]
        if (target_task_type == 'Bug 修复' and lineage_reuse
                and newest_non_bug[root_id][1] != pair_id):
            continue
        if sources.lineage_unavailable(h, root_id, target_task_type):
            continue
        source_key = f"{target_task_type}:{pair_id}"
        lock = sources.get_lock(h, pair_id, target_task_type)
        if sources.unavailable(lock):
            continue
        with h.db_connection() as db:
            if sources.consumed(db, pair_id, target_task_type):
                continue
            usage = db.execute('SELECT COUNT(*) FROM pairwise_tasks WHERE source_pair_id=?', (pair_id,)).fetchone()[0]
            rejected_directions = db.execute('SELECT COUNT(*) FROM pairwise_source_directions WHERE source_pair_id=? AND task_type=?',
                                             (pair_id, target_task_type)).fetchone()[0]
            root_stats = db.execute("""SELECT COUNT(*) AS source_count,
                    COALESCE(SUM(failure_count),0) AS failures,
                    COALESCE(MAX(updated_at),'') AS updated_at
                FROM pairwise_source_locks
                WHERE source_root_pair_id=? AND task_type=?""",
                (root_id, target_task_type)).fetchone()
            root_rejections = db.execute("""SELECT COUNT(*) FROM pairwise_source_directions AS direction
                JOIN pairwise_source_locks AS lock
                  ON lock.source_pair_id=direction.source_pair_id
                 AND lock.task_type=direction.task_type
                WHERE lock.source_root_pair_id=? AND direction.task_type=?""",
                (root_id, target_task_type)).fetchone()[0]
        attempts = m.attempts(h, pair_id)
        choices = ([a for a in attempts if a['id'] == lock['source_attempt_id']
                    and a['artifact_sha'] == lock['source_artifact_sha']] if lock
                   else [m.latest(attempts, side) for side in ('A', 'B')])
        eligible = [a for a in choices if a and not sources.eligibility_issues(h, pair, a)]
        if not eligible:
            continue
        attempt = _preferred_sides(pair, eligible)[0]
        source = {'pair': pair, 'attempt': attempt, 'side': attempt['side'], 'root_pair_id': root_id,
                  'source_key': source_key, 'snapshot_url': f"{pair['repo_url']}/commit/{attempt['artifact_sha']}"}
        if ready_mode != "any":
            buffered = candidate_bank.has_ready(h, target_task_type, source)
            if ready_mode == "only" and not buffered:
                continue
            if ready_mode == "exclude" and buffered:
                continue
        priority = (rejected_directions, (lock or {}).get('failure_count', 0), usage,
                    str(pair.get('created_at') or ''), pair_id)
        if target_task_type == 'Bug 修复':
            # Rotate by the root lineage, not by a newly-created descendant id.
            # A root that was already attempted cannot masquerade as untouched
            # merely because a newer Feature snapshot exists.
            priority = (str(root_stats['updated_at']), int(root_rejections),
                        int(root_stats['failures']), int(root_stats['source_count'])) + priority
        recently_failed = False
        if (target_task_type == 'Bug 修复' and ready_mode != "only"
                and int(root_stats['failures']) > 0):
            try:
                updated = datetime.strptime(root_stats['updated_at'], '%Y-%m-%d %H:%M:%S %z').timestamp()
                recently_failed = time.time() - updated < BUG_SOURCE_ROTATION_GUARD_SECONDS
            except (TypeError, ValueError):
                pass
        candidates.append((priority, source, recently_failed))
    if target_task_type == 'Bug 修复' and ready_mode != "only":
        # This is a scheduler rotation guard, not a quality cooldown: the lock
        # remains ready and is not counted as blocked. Prefer another source
        # while generating, but never hide an already-reviewed buffered draft
        # from the dispatch-only coordinator.
        candidates = [item for item in candidates if not item[2]]
    return min(candidates, key=lambda item: item[0])[1] if candidates else None


def _parent_pair(h, pair):
    parent_id = str(pair.get("source_pair_id") or "")
    if parent_id:
        # legacy:* is a frozen external baseline, not a pairwise_tasks row.
        # The current Pair is therefore the local lineage boundary.
        if parent_id.startswith('legacy:'):
            return None
        try:
            return m.task(h, parent_id)
        except h.WorkflowError:
            return None
    repo_url = str(pair.get("source_repo_url") or "")
    source_sha = str(pair.get("source_base_sha") or "")
    if not repo_url or not source_sha:
        return None
    with h.db_connection() as db:
        row = db.execute(
            """SELECT p.id FROM pairwise_tasks AS p
               JOIN pairwise_attempts AS a ON a.pair_id=p.id
               WHERE p.repo_url=? AND a.artifact_sha=?
               ORDER BY p.created_at DESC LIMIT 1""",
            (repo_url, source_sha),
        ).fetchone()
    return m.task(h, str(row["id"])) if row else None


def _lineage(h, pair):
    result = []
    seen = set()
    current = pair
    while current and current["id"] not in seen:
        seen.add(current["id"])
        result.append(current)
        current = _parent_pair(h, current)
    result.reverse()
    return result


def _continuation_context(h, source, repo_path):
    pair = source["pair"]
    history_rows = []
    prior_bug_directions = sources.prior_bug_directions(h, source)
    legacy_lineage = source.get("legacy_lineage")
    if isinstance(legacy_lineage, list) and legacy_lineage:
        lineage_items = [
            {"id": item.get("id"), "task_type": item.get("task_type"),
             "prompt": item.get("prompt"), "phase": item.get("phase")}
            for item in legacy_lineage if isinstance(item, dict)
        ]
    else:
        lineage_items = _lineage(h, pair)
    for index, item in enumerate(lineage_items):
        history_rows.append({
            "sequence": index,
            "run_id": str(item["id"]),
            "task_type": str(item["task_type"]),
            "prompt": str(item["prompt"])[:12000],
            "phase": str(item.get("phase") or "complete"),
            "counts_toward_quota": True,
            "outcome": "root" if index == 0 else "succeeded",
            "expansion_axis": "",
            "modules": [],
            "engineering_core": "",
            "complex_dimensions": [],
            "main_user_flow": "",
            "api_or_actions": [],
            "new_state_sets": [],
        })
    followups = history_rows[1:]
    tracked = h.run_command(["git", "ls-files"], cwd=repo_path).stdout.splitlines()
    readme = ""
    for name in ("README.md", "README", "docs/README.md"):
        path = repo_path / name
        if path.is_file():
            readme = path.read_text(encoding="utf-8")[:30000]
            break
    return {
        "repo_path": str(repo_path),
        "_pairwise_mode": True,
        "repo_name": str(pair["repo_name"]),
        "project_category": str(pair["project_category"]),
        "language_framework": str(pair["language_framework"]),
        "current_commit": str(source["attempt"]["artifact_sha"]),
        "original_or_current_prompt": str(pair["prompt"])[:12000],
        "iteration_history": history_rows,
        "same_repository_history": [
            {
                "run_id": row["run_id"],
                "turn_number": 1,
                "task_type": row["task_type"],
                "prompt": row["prompt"],
                "remote_submission_id": "",
                "reference_id": f"pair:{row['run_id']}",
                "submission_state": "local_pairwise",
                "run_phase": "complete",
            }
            for row in history_rows
        ],
        "prior_bug_directions": prior_bug_directions,
        "iteration_policy": {
            "iteration_count": len(followups),
            "new_module_count": sum(
                row["task_type"] == "0-1 代码生成" for row in followups
            ),
            "feature_count": sum(
                row["task_type"] == "Feature 迭代" for row in followups
            ),
            "last_iteration_task_type": (
                str(followups[-1]["task_type"]) if followups else ""
            ),
            "maximum_iterations": h.AUTO_REFILL_MAX_ITERATIONS_PER_ROOT,
            "maximum_new_modules": h.MAX_NEW_MODULE_ITERATIONS_PER_ROOT,
        },
        "tracked_files": [item.strip() for item in tracked if item.strip()][:300],
        "readme": readme,
    }


def _generate_continuation(h, source, target_task_type, progress, notebook_feedback=""):
    progress("正在准备已归档产物快照")
    with tempfile.TemporaryDirectory(prefix="pairwise-followup-") as directory:
        repo_path = Path(directory) / "source"
        repo_path.mkdir()
        h.clone_repository_snapshot(
            str(source["pair"]["repo_url"]),
            repo_path,
            str(source["attempt"]["artifact_sha"]),
        )
        sha, tree = source['attempt']['artifact_sha'], source['attempt']['artifact_tree']
        actual = h.run_command(['git', 'show', '-s', '--format=%T', sha], cwd=repo_path).stdout.strip()
        if actual != tree:
            raise h.WorkflowError('临时 main 与锁定来源 Git tree 不一致')
        h.run_command(['git', 'checkout', '-B', 'main', sha], cwd=repo_path)
        context = _continuation_context(h, source, repo_path)
        gsb_history = _gsb_history_summaries(
            h,
            history.records(h, limit=30) + candidate_bank.ready_records(h),
            source["pair"]["repo_name"],
        )
        context["gsb_history"] = gsb_history
        if target_task_type == "Bug 修复":
            context["bug_prompt_references"] = bug_references.relevant(h, context, limit=3)
        excluded = sources.rejected_directions(h, source, target_task_type)
        historical_warnings = sources.historical_direction_warnings(h, source, target_task_type)
        feedback = _retry_feedback(h, _values(h), target_task_type, source) + notebook_feedback
        if target_task_type == 'Bug 修复':
            context['_bug_verification_mode'] = 'static_first'
            context['_bug_generation_flow'] = _bug_generation_flow(h)
            # These records already travel as structured seed_source fields.
            # Do not replay them or the static policy in every later stage.
            feedback = '核验模式：static_first。' + _compact_stage_feedback(feedback)
        else:
            if excluded:
                feedback += '\n以下缺陷或功能方向已被拒绝，只排除这些方向，不排除整个项目；不能换措辞绕过：\n' + json.dumps(excluded, ensure_ascii=False)
            if historical_warnings:
                feedback += ('\n以下是旧口径下的方向意见，当前尚未重新确认，不是禁止方向。'
                             '不能只因为旧理由写着已排除就放弃；须与真实历史任务重新比较具体故障链。\n'
                             + json.dumps(historical_warnings, ensure_ascii=False)[:6000])
            feedback += '\n只读检查临时 main；复现脚本、依赖、构建物放在隔离临时目录，不能修复或改写基线文件。'
        if feedback:
            context["_concept_retry_feedback"] = feedback
        # General execution/lesson feedback is not evidence of a Rule C rejection.
        # Pair-wise Feature may choose a real extension or a complete new module.
        seed_snapshot = None
        current_direction = {}

        def guard(concept):
            identity = sources.direction_identity(concept)
            if not identity:
                raise h.WorkflowError('概念卡缺少有代码位置支撑的方向身份，不按题面猜测指纹')
            cited = set()
            for location in concept['direction_identity']['code_locations']:
                relative = str(location).split(':', 1)[0].strip()
                path = (repo_path / relative).resolve()
                if not path.is_relative_to(repo_path.resolve()) or not path.is_file():
                    raise h.WorkflowError('方向指纹引用了不存在或越界的代码位置')
                cited.add(path.relative_to(repo_path.resolve()).as_posix())
            changed_files = set(seed_snapshot.get('changed_files') or []) if seed_snapshot else set()
            if changed_files and not cited.intersection(changed_files):
                raise h.WorkflowError('概念卡偏离预置缺陷改动的源文件，不能另选历史缺陷方向')
            current_direction.clear()
            if any(item['identity'] == identity for item in sources.rejected_directions(h, source, target_task_type)):
                raise h.SameRepositoryOverlapError('规则 C：该来源的相同缺陷或功能方向已被拒绝，请更换真实方向')
            claim = getattr(h.CODEX_JOB_CONTEXT, 'feature_direction_claim', None)
            if target_task_type == 'Feature 迭代' and callable(claim) and not claim(concept, identity):
                raise h.SameRepositoryOverlapError('另一条竞速路线正在生成同一功能方向，请选择不同的核心操作')
            current_direction.update(concept)

        def generate(active_context, active_feedback, expected_tree):
            previous_observer = getattr(h.CODEX_JOB_CONTEXT, 'notebook_observer', None)
            previous_guard = getattr(h.CODEX_JOB_CONTEXT, 'direction_guard', None)

            def observe(stage, candidate, reason='', review=None, outcome='rejected'):
                duplicate_rejection = '查重' in stage or stage == '第 3 条预检'
                difficulty_rejection = (
                    target_task_type == 'Bug 修复' and stage == '迭代预审'
                    and isinstance(review, dict) and h.difficulty_policy.rejected_direction(review)
                )
                if outcome == 'rejected' and (duplicate_rejection or difficulty_rejection):
                    rejection = reason or ('开发者视角难度盲审：最小正确方案未达到困难'
                                           if difficulty_rejection else '方向复核未通过')
                    sources.reject_direction(h, source, target_task_type,
                                             current_direction or candidate, rejection)
                if callable(previous_observer):
                    previous_observer(stage, candidate, reason, review, outcome)

            h.CODEX_JOB_CONTEXT.notebook_observer = observe
            h.CODEX_JOB_CONTEXT.direction_guard = guard
            try:
                try:
                    result = h._generate_iteration_candidate_for_context(
                        active_context, target_task_type, active_feedback)
                except getattr(h, 'SameRepositoryOverlapError') as exc:
                    sources.reject_direction(h, source, target_task_type, current_direction, str(exc))
                    if target_task_type == 'Bug 修复' and (
                        active_context.get('_seeded_defect') or active_context.get('_existing_defect')
                    ):
                        raise  # This frozen defect cannot be redirected by rewriting its concept.
                    stage = ('规则 C 未通过，正在重新查找不同的真实缺陷（1/1）'
                             if target_task_type == 'Bug 修复'
                             else '规则 C 未通过，正在重新选择不同功能方向（1/1）')
                    progress(stage, rejection=str(exc))
                    retry_context = dict(active_context)
                    retry_context['_concept_retry_feedback'] = active_feedback + '\n' + str(exc)
                    retry_context['_concept_overlap_feedback'] = str(exc)
                    result = h._generate_iteration_candidate_for_context(
                        retry_context, target_task_type, retry_context['_concept_retry_feedback'])
                if h.run_command(['git', 'rev-parse', 'HEAD^{tree}'], cwd=repo_path).stdout.strip() != expected_tree:
                    raise h.WorkflowError('出题过程中临时 main 的 Git tree 被改变，拒绝创建子题')
                if h.run_command(['git', '--no-optional-locks', 'status', '--porcelain', '--untracked-files=all'],
                                 cwd=repo_path).stdout.strip():
                    raise h.WorkflowError('出题或复现修改了基线工作区；不得把修改后的代码用作新题起点')
                return result
            except h.SameRepositoryOverlapError as exc:
                sources.reject_direction(h, source, target_task_type, current_direction, str(exc))
                raise
            finally:
                h.CODEX_JOB_CONTEXT.notebook_observer = previous_observer
                h.CODEX_JOB_CONTEXT.direction_guard = previous_guard

        if target_task_type != 'Bug 修复':
            progress("正在读取代码并生成困难级题面")
            return generate(context, feedback, tree)

        current_direction.clear()
        rejected_now = sources.rejected_directions(h, source, target_task_type)
        natural_history = json.dumps({
            "same_repository": context.get("same_repository_history") or [],
            "prior_bug_directions": context.get("prior_bug_directions") or [],
            "gsb": gsb_history,
            "rejected_directions": rejected_now,
        }, ensure_ascii=False)
        natural = natural_bug.discover_verified(
            h, source, repo_path, natural_history, progress=progress, lanes=2)
        if natural:
            progress(
                f"现有 Bug 路线 {natural['lane']} 已通过独立复核，正在生成并审核题面")
            existing_context = dict(context)
            existing_context['_existing_defect'] = natural['discovery']
            existing_context.pop('_seeded_defect', None)
            try:
                result = generate(existing_context, feedback, tree)
                result.update(
                    bug_generation_strategy='existing_first',
                    bug_verification_mode='static',
                    runtime_verified=False,
                    existing_bug_discovery=natural['discovery'],
                    existing_bug_reverification=natural['reverification'],
                )
                return result
            except (h.JobCancelled, h.UpstreamCapacityError):
                raise
            except Exception as exc:
                if bug_runtime.is_timeout(exc) or getattr(exc, 'source_environment_failure', False):
                    raise
                progress("现有 Bug 已找到但完整题面未通过，转入埋 Bug 兜底", rejection=str(exc))
        else:
            progress("两条现有 Bug 查找路线均未通过，转入埋 Bug 兜底")
        seed_source = {**source, "seed_history": context["same_repository_history"],
                       "prior_bug_directions": context.get("prior_bug_directions") or [],
                       "gsb_history": gsb_history, "rejected_directions": rejected_now,
                       "bug_prompt_references": context.get("bug_prompt_references") or [],
                       "historical_direction_warnings": historical_warnings,
                       "bug_verification_mode": "static_first",
                       "bug_generation_flow": context['_bug_generation_flow'],
                       "generation_feedback": feedback}
        if context['_bug_generation_flow'] == bug_generation_v2.FLOW:
            result = bug_generation_v2.generate(
                h, source, repo_path, context, seed_source, sha, tree, progress)
            result['bug_generation_strategy'] = 'existing_first_fallback_seed'
            return result
        designs = seeded_bug.design_candidates(h, seed_source, repo_path, progress)
        base_context = dict(context)
        failures = []
        runtime = getattr(h.CODEX_JOB_CONTEXT, 'bug_generation_runtime', None)
        for design_index, design in enumerate(designs[:2]):
            candidate_name = design.get('candidate_id') or chr(ord('A') + design_index)
            if runtime is not None:
                runtime.set_candidate(candidate_name)
            if design_index:
                progress(f'候选 A 未通过，正在从干净源码植入备用候选 {candidate_name}')
                h.run_command(['git', 'checkout', '--detach', sha], cwd=repo_path)
                if h.run_command(['git', 'status', '--porcelain', '--untracked-files=all'],
                                 cwd=repo_path).stdout.strip():
                    raise h.WorkflowError('切换备用候选前源码未恢复为干净快照')
            context = dict(base_context)
            current_direction.clear()
            seed_snapshot = None
            try:
                seed_snapshot = seeded_bug.seed(
                    h, seed_source, repo_path, progress, design=design)
                candidate_tree = seed_snapshot['tree']
                description = seed_snapshot.get('description') or {}
                if description:
                    context['_seeded_defect'] = {
                        key: str(description.get(key) or '') for key in
                        ('affected_flow', 'observable_failure', 'expected_behavior')
                    }
                    context['_seeded_defect']['problems'] = list(description.get('problems') or [])
                    context['_seeded_defect']['changed_files'] = list(
                        seed_snapshot.get('changed_files') or [])
                context['current_commit'] = seed_snapshot['sha']
                progress(f"候选 {candidate_name} 已冻结，正在生成并盲审困难级题面")
                result = generate(context, feedback, candidate_tree)
                result['seed_snapshot'] = seeded_bug.bundle(h, repo_path, seed_snapshot)
                result['bug_generation_strategy'] = 'existing_first_fallback_seed'
                result['bug_verification_mode'] = 'static_first'
                result['runtime_verified'] = False
                return result
            except (h.JobCancelled, h.UpstreamCapacityError):
                raise
            except Exception as exc:
                if (bug_runtime.is_timeout(exc) or getattr(exc, 'source_environment_failure', False)
                        or getattr(exc, 'prompt_only_failure', False)):
                    raise
                failures.append(f'候选 {candidate_name}：{exc}')
                identity = {'direction_identity': {
                    'mechanism': str(design['candidate'].get('proposed_change') or ''),
                    'invariant': str(design['candidate'].get('original_invariants') or ''),
                    'code_locations': list(design['candidate'].get('code_locations') or []),
                }}
                rejection_candidate = current_direction or identity
                rejection_identity = sources.direction_identity(rejection_candidate)
                already_recorded = any(
                    item['identity'] == rejection_identity
                    for item in sources.rejected_directions(h, source, target_task_type)
                )
                if rejection_identity and not already_recorded:
                    sources.reject_direction(
                        h, source, target_task_type, rejection_candidate, str(exc))
                if runtime is not None:
                    runtime.checkpoint(h, 'candidate-rejected', {
                        'candidate_id': candidate_name, 'reason': str(exc),
                        'design': design['candidate'],
                    }, repo_path)
                if design_index + 1 >= len(designs[:2]):
                    if len(designs[:2]) == 1:
                        raise
                    break
        error = h.WorkflowError('本来源两个候选均未通过，已保存依据并立即轮换源码：'
                                + '；'.join(failures)[-5000:])
        error.bug_candidates_exhausted = True
        raise error


def _generate_continuation_race(
    h, source, target_task_type, progress, notebook_feedback="", lanes=2
):
    """Race independent follow-up candidates and keep only the first success."""
    parent_key = h.current_job_key()
    started = time.monotonic()
    if target_task_type == "Bug 修复":
        lanes = 1
        progress("优先并行查找两组现有 Bug；均未通过时再串行植入排名最高的两个候选")
    bug_flow = _bug_generation_flow(h) if target_task_type == "Bug 修复" else ""
    baseline_rejections = len(sources.rejected_directions(h, source, target_task_type))
    token = uuid.uuid4().hex[:8]
    lane_keys = [
        f"{parent_key or JOB_KEY}:race:{token}:lane:{number}"
        for number in range(1, lanes + 1)
    ]
    lock = threading.Lock()
    finished = threading.Event()
    winner = []
    failures = []
    stages = {number: "等待启动" for number in range(1, lanes + 1)}
    claimed_feature_directions = set()
    lane_runtimes = {}
    observer_present = hasattr(h.CODEX_JOB_CONTEXT, "notebook_observer")
    observer = getattr(h.CODEX_JOB_CONTEXT, "notebook_observer", None)

    def claim_feature_direction(concept, identity):
        focus = (
            re.sub(r'\s+', '', str(concept.get('business_object') or '')).casefold(),
            re.sub(r'\s+', '', str(concept.get('primary_operation') or '')).casefold(),
        )
        keys = {json.dumps(identity, sort_keys=True, ensure_ascii=False), focus}
        with lock:
            if claimed_feature_directions.intersection(keys):
                return False
            claimed_feature_directions.update(keys)
        return True

    def lane_progress(number, detail, rejection=""):
        with lock:
            stages[number] = detail
            summary = " · ".join(
                f"路线 {lane}: {stages[lane]}" for lane in range(1, lanes + 1)
            )
        progress((f"单路生成｜{detail}" if lanes == 1
                  else f"{lanes} 路竞速｜{summary}"), rejection)

    def run_lane(number):
        key = lane_keys[number - 1]
        previous_key = h.current_job_key()
        previous_observer = getattr(h.CODEX_JOB_CONTEXT, "notebook_observer", None)
        previous_progress = getattr(h.CODEX_JOB_CONTEXT, "progress", None)
        previous_claim = getattr(h.CODEX_JOB_CONTEXT, "feature_direction_claim", None)
        previous_runtime = getattr(h.CODEX_JOB_CONTEXT, 'bug_generation_runtime', None)
        h.clear_job_cancellation(key)
        h.CODEX_JOB_CONTEXT.key = key
        if target_task_type == 'Feature 迭代':
            h.CODEX_JOB_CONTEXT.feature_direction_claim = claim_feature_direction
        if target_task_type == 'Bug 修复':
            if bug_flow == bug_generation_v2.FLOW:
                runtime = bug_runtime.Runtime(
                    started + BUG_V2_WALL_BUDGET_SECONDS, source,
                    active_budget_seconds=BUG_V2_ACTIVE_BUDGET_SECONDS,
                    active_soft_seconds=BUG_V2_ACTIVE_TARGET_SECONDS)
            else:
                runtime = bug_runtime.Runtime(
                    started + BUG_SOURCE_BUDGET_SECONDS, source,
                    soft_deadline=started + BUG_SOURCE_TARGET_SECONDS)
            h.CODEX_JOB_CONTEXT.bug_generation_runtime = runtime
            with lock:
                lane_runtimes[number] = runtime
        h.CODEX_JOB_CONTEXT.progress = (
            lambda detail, rejection="": lane_progress(number, detail, rejection)
        )
        if observer_present:
            h.CODEX_JOB_CONTEXT.notebook_observer = observer
        try:
            feedback = notebook_feedback.strip() if lanes == 1 else (
                f"{notebook_feedback}\n这是并行竞速路线 {number}/{lanes}；"
                "必须独立选择核心方向，不得假设其他路线的候选内容。"
            ).strip()
            result = _generate_continuation(
                h,
                source,
                target_task_type,
                lambda detail, rejection="": lane_progress(number, detail, rejection),
                feedback,
            )
            _validate_continuation_draft(h, result, target_task_type)
            won = False
            with lock:
                if not winner:
                    winner.append((number, result))
                    stages[number] = "已通过"
                    won = True
            lane_progress(number, "已通过" if won else "已完成，等待先通过的路线")
            if won:
                finished.set()
                for other_key in lane_keys:
                    if other_key != key:
                        h.cancel_background_job(other_key)
        except h.JobCancelled:
            with lock:
                stages[number] = "已停止"
            lane_progress(number, "已停止")
        except Exception as exc:
            with lock:
                failures.append((number, exc))
                stages[number] = "未通过"
                if len(failures) == lanes:
                    finished.set()
            lane_progress(number, "未通过", str(exc))
        finally:
            h.CODEX_JOB_CONTEXT.key = previous_key
            h.CODEX_JOB_CONTEXT.progress = previous_progress
            if previous_runtime is None:
                if hasattr(h.CODEX_JOB_CONTEXT, 'bug_generation_runtime'):
                    delattr(h.CODEX_JOB_CONTEXT, 'bug_generation_runtime')
            else:
                h.CODEX_JOB_CONTEXT.bug_generation_runtime = previous_runtime
            if previous_claim is not None:
                h.CODEX_JOB_CONTEXT.feature_direction_claim = previous_claim
            elif hasattr(h.CODEX_JOB_CONTEXT, "feature_direction_claim"):
                delattr(h.CODEX_JOB_CONTEXT, "feature_direction_claim")
            if observer_present:
                h.CODEX_JOB_CONTEXT.notebook_observer = previous_observer
            elif hasattr(h.CODEX_JOB_CONTEXT, "notebook_observer"):
                delattr(h.CODEX_JOB_CONTEXT, "notebook_observer")

    threads = []
    for number in range(1, lanes + 1):
        thread = threading.Thread(
            target=run_lane,
            args=(number,),
            name=f"continuation-race-{token}-{number}",
            daemon=True,
        )
        threads.append(thread)
        thread.start()
    try:
        last_direction_check = 0.0
        soft_target_reported = False
        while not finished.wait(0.25):
            h.ensure_job_active(parent_key)
            now = time.monotonic()
            if target_task_type in {"Bug 修复", "Feature 迭代"} and now - last_direction_check >= 1:
                last_direction_check = now
                rejected = sources.rejected_directions(h, source, target_task_type)
                new_rejections = len(rejected) - baseline_rejections
                if new_rejections >= sources.DIRECTION_LIMIT:
                    raise h.WorkflowError(
                        f"本轮已有 {new_rejections} 个不同{'缺陷' if target_task_type == 'Bug 修复' else '功能'}方向命中规则 C，提前换来源"
                    )
            if target_task_type == "Bug 修复":
                runtime = next(iter(lane_runtimes.values()), None)
                active_seconds = runtime.active_elapsed() if runtime is not None else now - started
                if bug_flow == bug_generation_v2.FLOW:
                    if active_seconds >= BUG_V2_ACTIVE_TARGET_SECONDS and not soft_target_reported:
                        soft_target_reported = True
                        progress("本来源已超过 45 分钟活动软目标；排队不计入活动时间，继续当前检查点")
                    if active_seconds >= BUG_V2_ACTIVE_BUDGET_SECONDS:
                        raise h.GenerationTimeout(
                            "本来源 V2 Bug 题生成达到 60 分钟活动上限，已保存记录并换来源")
                    if now - started >= BUG_V2_WALL_BUDGET_SECONDS:
                        raise h.GenerationTimeout(
                            "本来源 V2 Bug 题生成达到 75 分钟墙钟安全上限，已保存记录并换来源")
                else:
                    if now - started >= BUG_SOURCE_TARGET_SECONDS and not soft_target_reported:
                        soft_target_reported = True
                        progress("本来源已超过 25 分钟软目标，继续当前检查点，最迟 45 分钟换来源")
                    if now - started >= BUG_SOURCE_BUDGET_SECONDS:
                        raise h.GenerationTimeout(
                            "本来源 Bug 题生成达到 45 分钟绝对上限，已保存记录并换来源")
            if (target_task_type == "Feature 迭代"
                    and now - started >= FEATURE_SOURCE_BUDGET_SECONDS):
                raise h.GenerationTimeout(
                    "本来源 Feature 题生成达到 10 分钟墙钟硬上限，已停止并换来源"
                )
            if not any(thread.is_alive() for thread in threads):
                break
    except Exception:
        for key in lane_keys:
            h.cancel_background_job(key)
        raise
    finally:
        if winner:
            for key in lane_keys:
                h.cancel_background_job(key)
        for thread in threads:
            thread.join(timeout=6)
    if winner:
        number, result = winner[0]
        progress(f"路线 {number} 已通过，其他路线已停止")
        return result
    detail = "；".join(
        f"路线 {number}：{exc}" for number, exc in sorted(failures)
    ) or "所有竞速路线均未返回结果"
    errors = [exc for _, exc in failures]
    if errors and all(isinstance(exc, h.SameRepositoryOverlapError) for exc in errors):
        raise h.SameRepositoryOverlapError(detail[-5000:])
    capacity = next((exc for exc in errors if isinstance(exc, h.UpstreamCapacityError)), None)
    if capacity is not None:
        raise capacity
    error = h.WorkflowError(
        ("单路题面生成未通过：" if lanes == 1 else f"{lanes} 路题面竞速均未通过：")
        + detail[-5000:]
    )
    if errors and all(bug_runtime.is_timeout(exc) for exc in errors):
        error.generation_timeout = True
    if errors and all(getattr(exc, 'source_environment_failure', False) for exc in errors):
        error.source_environment_failure = True
    raise error


def _payload(h, draft, directory):
    difficulty = str(draft.get("task_difficulty") or "")
    if difficulty != "困难":
        raise ValueError("0-1 自动题只接受困难下沿，不接受地狱级候选")
    errors = h.difficulty_review_errors({"difficulty_review": draft.get("difficulty_review")})
    if h.reviewed_task_difficulty(draft) != difficulty:
        errors.append("难度标签与独立复核结论不一致")
    if errors:
        raise ValueError("自动题面未通过困难度复核：" + "；".join(errors))
    repo_name = str(draft.get("repo_name") or "").strip()
    return {
        "title": str(draft.get("title") or repo_name),
        "repo_name": repo_name,
        "prompt": str(draft.get("first_prompt") or ""),
        "task_type": "0-1 代码生成",
        "project_category": str(draft.get("category") or ""),
        "difficulty": difficulty,
        "difficulty_preflight": {"review": draft["difficulty_review"],
                                 "policy_version": h.difficulty_policy.VERSION,
                                 "generation_policy_version": h.generation_policy.VERSION,
                                 "handwritten_line_limit": h.ZERO_ONE_HANDWRITTEN_LINE_LIMIT,
                                 "review_model": h.REVIEW_MODEL, "reviewed_at": h.now_text(),
                                 "analysis": draft.get("complexity_analysis") or {}},
        "language_framework": str(draft.get("language_framework") or ""),
        "snapshot_url": "",
        "verification_commands": draft.get("verification_commands") or [],
        "reproducibility": "已容器化，可一键起环境",
        "project_directory": directory,
        "topic_confirmed": True,
        "origin": "auto",
        "legacy_source_run_id": str(draft.get("legacy_source_run_id") or ""),
    }


def _continuation_payload(h, draft, source, target_task_type, directory, project_number):
    difficulty, prompt = _validate_continuation_draft(h, draft, target_task_type)
    parent = source["pair"]
    seed_snapshot = draft.get("seed_snapshot") if target_task_type == "Bug 修复" else None
    if target_task_type == "Bug 修复" and not seed_snapshot and draft.get("evidence_source_commit") != source["attempt"]["artifact_sha"]:
        raise ValueError("原产物 Bug 证据与冻结来源提交不一致")
    if seed_snapshot:
        repo_name = h.unique_repo_name(f"repair-case-{project_number:04d}-{uuid.uuid4().hex[:6]}")
    else:
        stem = re.sub(r"[^A-Za-z0-9._-]+", "-", str(parent["repo_name"]))[:68].strip("-.")
        slug = TASK_TYPE_SLUGS[target_task_type]
        repo_name = h.unique_repo_name(f"{stem}-{slug}-{project_number:04d}"[:100])
    return {
        "title": f"{parent['title']} · {target_task_type}",
        "repo_name": repo_name,
        "prompt": prompt,
        "task_type": target_task_type,
        "project_category": str(parent["project_category"]),
        "difficulty": difficulty,
        "difficulty_preflight": {"review": draft["difficulty_review"],
                                 **({"difficulty_admission": draft["difficulty_admission"]}
                                    if isinstance(draft.get("difficulty_admission"), dict) else {}),
                                 **({"bug_generation_strategy": draft.get('bug_generation_strategy', 'seeded' if seed_snapshot else 'existing'),
                                     "bug_verification_mode": draft.get('bug_verification_mode', 'runtime'),
                                     "runtime_verified": draft.get('runtime_verified', True)}
                                    if target_task_type == 'Bug 修复' else {}),
                                 "policy_version": h.difficulty_policy.VERSION,
                                 "review_model": h.REVIEW_MODEL, "reviewed_at": h.now_text(),
                                 "analysis": draft.get("complexity_analysis") or {},
                                 **({"seed_snapshot": seed_snapshot} if seed_snapshot else {}),
                                 **({"existing_bug_evidence": {
                                     "source_commit": draft["evidence_source_commit"],
                                     "verified_at": draft["evidence_verified_at"],
                                     "verification_mode": draft.get('bug_verification_mode', 'runtime'),
                                     "runtime_verified": draft.get('runtime_verified', True),
                                     "discovery": draft.get("existing_bug_discovery") or {},
                                     "lane_reverification": draft.get("existing_bug_reverification") or {},
                                     "confirmed_bugs": draft["confirmed_bugs"],
                                     "independent_review": draft["independent_review"],
                                 }} if target_task_type == "Bug 修复" and not seed_snapshot else {})},
        "language_framework": str(parent["language_framework"]),
        "snapshot_url": "" if seed_snapshot else source["snapshot_url"],
        "source_pair_id": parent['id'],
        "source_attempt_id": source['attempt']['id'],
        "source_side": source['side'],
        "source_artifact_sha": source['attempt']['artifact_sha'],
        "source_artifact_tree": source['attempt']['artifact_tree'],
        "source_reservation_token": source.get('reservation_token', ''),
        "legacy_baseline_run_id": source.get('legacy_run_id', ''),
        "verification_commands": parent.get("verification_commands") or [],
        "reproducibility": "已容器化，可一键起环境",
        "project_directory": directory,
        "topic_confirmed": True,
        "origin": "auto",
    }


def _notebook_context(h, task_type, source=None, project_number=None):
    pair = source["pair"] if source else {}
    return {"task_type": task_type, "project_category": pair.get("project_category") or
            (h.category_for_project_number(project_number) if project_number else ""),
            "source_pair_id": pair.get("id", ""), "source_repo": pair.get("repo_url", ""),
            "source_sha": source["attempt"].get("artifact_sha", "") if source and source.get("attempt") else ""}


def run_once(h):
    global ACTIVE, RETRY_NEXT_AT
    from . import upstream
    if upstream.is_paused(h):
        return False
    if deployment.is_draining(h):
        return False
    values = _values(h)
    if values["pairwise_auto_enabled"] != "1":
        return False
    if time.monotonic() < RETRY_NEXT_AT:
        return False
    start_at = _integer(values["pairwise_auto_start_at"], 0)
    if start_at and start_at > int(time.time()):
        return False
    created = max(0, _integer(values["pairwise_auto_created"], 0))
    if start_at:
        h.write_settings(
            {"pairwise_auto_start_at": "", "pairwise_auto_delay_hours": "0"}
        )
    occupied_attempts = _active_attempt_count(h)
    max_parallel = h.configured_max_parallel_runs()
    if occupied_attempts >= max_parallel:
        h.write_settings(
            {
                "pairwise_auto_detail": (
                    f"Claude 名额已满；3 条独立出题通道继续准备候选，"
                    f"当前待选库 {_ready_count(h, values)}/{candidate_bank.READY_LIMIT} 道"
                ),
                "pairwise_auto_error": "",
                "pairwise_auto_status": "waiting_capacity",
                "pairwise_auto_next_type": "",
            }
        )
        return False
    with LOCK:
        if ACTIVE:
            return False
        ACTIVE = True
    batch = values["pairwise_auto_batch"]
    directory = values["pairwise_auto_project_directory"] or DEFAULT_DIRECTORY
    previous = h.current_job_key()
    previous_progress = getattr(h.CODEX_JOB_CONTEXT, "progress", None)
    source = None
    lease_stop = threading.Event()
    lease_thread = None
    target_task_type = ""
    banked = None
    draft = None
    h.CODEX_JOB_CONTEXT.key = JOB_KEY
    h.clear_job_cancellation(JOB_KEY)
    try:
        counts, _, _ = daily_task_type_counts(h)
        blocked_sources = _blocked_sources(values)
        target_task_type, source, unavailable = next_available_task(
            h, counts, blocked_sources, ready_mode="only"
        )
        if source:
            source = sources.claim(h, source, target_task_type)
            if not source:
                return False  # Another scheduler won; no candidate or remote repo created.

            def heartbeat():
                while not lease_stop.wait(30):
                    try:
                        if not sources.renew(h, source):
                            return
                    except Exception as exc:
                        h.log_workflow_exception('-', 'pairwise-source-lease', exc)
                        return  # Fencing will reject a late result after lease expiry.

            lease_thread = threading.Thread(target=heartbeat, name='pairwise-source-lease', daemon=True)
            lease_thread.start()
        dispatch_reason = (
            "仅补 Bug 修复；无合格来源时等待，不生成其他题型。"
            if allowed_task_types(h, values) == ("Bug 修复",) else
            "；".join(unavailable) + "。先补 0-1 建立新基线，之后优先补齐缺额。"
            if unavailable and target_task_type == "0-1 代码生成" else
            "；".join(unavailable) + f"。本次先补可推进的 {target_task_type}。"
            if unavailable and target_task_type else
            "按今日创建的有效或待复评题目调节 1:1:1；不强求每一步相等。"
        )
        if not target_task_type:
            # Generation belongs exclusively to the three prefill lanes.  The
            # main coordinator stays a lightweight dispatcher and never opens
            # a fourth model route when the reviewed bank is empty.
            return False
        pending_id = uuid.uuid4().hex[:12]
        banked = candidate_bank.claim(h, target_task_type, pending_id, source)
        if not banked:
            return False
        overlap_reason = candidate_bank.overlap(h, banked["prompt"])
        if overlap_reason:
            candidate_bank.release(h, banked["id"], overlap_reason, stale=True)
            banked = None
            raise h.WorkflowError(overlap_reason)
        project_number = int(banked["project_number"])
        h.write_settings({
            "pairwise_auto_generation_started_at": "",
            "pairwise_auto_stage_started_at": "",
            "pairwise_auto_stage": "",
            "pairwise_auto_rewrite_count": "0",
            "pairwise_auto_dispatch_reason": dispatch_reason,
            "pairwise_auto_pending_id": pending_id,
            "pairwise_auto_pending_title": (
                f"{source['pair']['title']} · {target_task_type}"
                if source else "新的 0-1 项目 · 题面生成中"
            ),
        })
        progress = lambda text, rejection="": _progress(h, batch, target_task_type, text, rejection)
        h.CODEX_JOB_CONTEXT.progress = progress
        progress("正在领取三通道已审核题面")
        context = _notebook_context(h, target_task_type, source, project_number)
        search_text = source["pair"]["prompt"] if source else values.get("pairwise_auto_last_rejection", "")
        with notebook.generation(h, context, pending_id, search_text) as lessons:
            draft = banked["draft"]
            progress("已领用本地待选题面，正在做创建前查重")
            if target_task_type == "0-1 代码生成":
                pair_payload = _payload(h, draft, directory)
            else:
                pair_payload = _continuation_payload(
                    h,
                    draft,
                    source,
                    target_task_type,
                    directory,
                    project_number,
                )
        h.ensure_job_active(JOB_KEY)
        if not _current(h, batch):
            return False
        if source:
            if source.get('legacy_run_id'):
                current_source = legacy_baselines.by_id(
                    h, source['legacy_run_id'],
                    expected_sha=source['attempt']['artifact_sha'],
                    expected_tree=source['attempt']['artifact_tree'])
                if not current_source:
                    raise h.WorkflowError('生成期间 zzzz 旧项目基线发生变化，不创建子题')
            else:
                current_attempt = m.attempt(h, source['attempt']['id'])
                if (current_attempt['artifact_sha'] != source['attempt']['artifact_sha']
                        or sources.eligibility_issues(h, source['pair'], current_attempt)):
                    raise h.WorkflowError('生成期间来源证据发生变化，保留锁定来源，不创建子题')
        overlap_reason = candidate_bank.overlap(h, pair_payload["prompt"])
        if overlap_reason:
            candidate_bank.release(h, banked["id"], overlap_reason, stale=True)
            banked = None
            raise h.WorkflowError(overlap_reason)
        pair = m.create(h, pair_payload, pair_id=pending_id, enforce_prompt_dedup=True)
        candidate_bank.finish(h, banked["id"], pair["id"])
        candidate_bank.mark_legacy_used(h, draft.get("legacy_source_run_id", ""), pair["id"])
        history.append(h, pair)
        notebook.add_event(h, lessons["ids"], "pair_created", {
            "pair_id": pair["id"], "title": pair["title"], "prompt": pair["prompt"],
            "detail": "参考记录后创建了新题，仍待 A/B 完成及实际难度复评。"}, pending_id)
        completed = created + 1
        updated_counts, _, _ = daily_task_type_counts(h)
        h.write_settings(
            {
                "pairwise_auto_created": str(completed),
                "pairwise_auto_failures": "0",
                "pairwise_auto_enabled": "1",
                "pairwise_auto_detail": (
                    f"已创建并启动第 {completed} 道 {target_task_type}；"
                    f"今日配比：{_mix_text(updated_counts)}；将持续补题直到手动关闭"
                ),
                "pairwise_auto_error": "",
                "pairwise_auto_status": "ready",
                "pairwise_auto_next_type": "",
            }
        )
        try:
            workflow.start(h, pair["id"])
        except Exception as exc:
            h.write_settings(
                {
                    "pairwise_auto_enabled": "0",
                    "pairwise_auto_detail": "题目已创建，但 A/B 自动启动失败，请在任务列表手动启动",
                    "pairwise_auto_error": str(exc)[:1200],
                    "pairwise_auto_status": "needs_attention",
                    "pairwise_auto_next_type": "",
                }
            )
        return True
    except h.JobCancelled:
        return False
    except Exception as exc:
        candidate_bank.release(h, banked["id"] if banked else "", str(exc),
                               stale=isinstance(exc, ValueError))
        failed, cooldown = _source_failure_policy(h, exc)
        if not failed:
            RETRY_NEXT_AT = time.monotonic() + 60
        sources.release(h, source, str(exc), failed=failed, cooldown_after_attempt=cooldown)
        if _current(h, batch):
            _record_generation_outcome(h, target_task_type, exc)
        _generation_failed(h, batch, exc, source, target_task_type)
        h.log_workflow_exception("-", "pairwise-auto-refill", exc)
        return False
    finally:
        candidate_bank.release_legacy_review(h, draft.get("legacy_source_run_id", "") if draft else "")
        candidate_bank.release(h, banked["id"] if banked else "")
        lease_stop.set()
        if lease_thread:
            lease_thread.join(timeout=2)
        sources.release(h, source)
        h.CODEX_JOB_CONTEXT.key = previous
        h.CODEX_JOB_CONTEXT.progress = previous_progress
        with LOCK:
            ACTIVE = False
        # The coordinator polls after POLL_SECONDS. Waking itself here causes
        # a no-source result to spin continuously and flicker as "generating".


def _prefill_once(h, lane=1):
    """Keep reviewed drafts in stock without occupying the Pair dispatch lane."""
    if lane not in range(1, PREFILL_LANE_COUNT + 1):
        raise ValueError("后台备题通道编号无效")
    job_key = _prefill_job_key(lane)
    if deployment.is_draining(h):
        return False
    values = _values(h)
    if values["pairwise_auto_enabled"] != "1":
        return False
    start_at = _integer(values["pairwise_auto_start_at"], 0)
    with LOCK:
        lane_state = _prefill_snapshot(lane)
    if (start_at and start_at > int(time.time())) or time.monotonic() < lane_state["next_at"]:
        return False
    if _ready_count(h, values) >= candidate_bank.READY_LIMIT:
        return False
    with LOCK:
        if _prefill_snapshot(lane)["active"]:
            return False
        started_at = time.monotonic()
        _update_prefill(
            lane, active=True, stage="正在选择备题方向", started_at=started_at,
            stage_started_at=started_at, created_at=datetime.now().astimezone().isoformat(),
            pending_id=uuid.uuid4().hex[:12], task_type="",
            title=f"后台备题 {lane} · 方向选择中", error="",
        )
    batch = values["pairwise_auto_batch"]
    directory = values["pairwise_auto_project_directory"] or DEFAULT_DIRECTORY
    previous_key = h.current_job_key()
    previous_progress = getattr(h.CODEX_JOB_CONTEXT, "progress", None)
    source = None
    lease_stop = threading.Event()
    lease_thread = None
    draft = None
    task_type = ""
    h.CODEX_JOB_CONTEXT.key = job_key
    h.clear_job_cancellation(job_key)
    try:
        # Serialize only direction reservation, not model work. This lets all
        # three lanes run concurrently without choosing the same unreserved
        # type/source from the same instantaneous snapshot.
        with PREFILL_PLAN_LOCK:
            counts, _, _ = daily_task_type_counts(h)
            ready = candidate_bank.ready_task_counts(h)
            with LOCK:
                active_types = [
                    _prefill_snapshot(other)["task_type"]
                    for other in range(1, PREFILL_LANE_COUNT + 1)
                    if other != lane and _prefill_snapshot(other)["active"]
                ]
            planned = _prefill_planned_counts(values, counts, ready, active_types)
            # In balanced mode each live prefill lane owns a different task
            # type. If Feature is briefly retrying after a technical timeout,
            # leave its lane available instead of filling it with a second 0-1
            # generation that can keep Feature out indefinitely.
            reserved_types = (
                set(active_types)
                if allowed_task_types(h, values) == BALANCED_TASK_TYPES else set()
            )
            task_type, source, unavailable = next_available_task(
                h, planned, _blocked_sources(values), ready_mode="exclude",
                excluded_task_types=reserved_types,
            )
            if not task_type or not _current(h, batch):
                if (not task_type and _current(h, batch)
                        and allowed_task_types(h, values) == ("Bug 修复",)):
                    h.write_settings({
                        "pairwise_auto_status": "waiting_baseline",
                        "pairwise_auto_next_type": "",
                        "pairwise_auto_error": "",
                        "pairwise_auto_detail": (
                            "仅补 Bug 修复；"
                            + ("；".join(unavailable) if unavailable
                               else "当前没有可用的最新归档产物，等待来源或冷却结束")
                        ),
                    })
                return False
            with LOCK:
                _update_prefill(
                    lane, task_type=task_type,
                    title=(f"{source['pair']['title']} · {task_type}"
                           if source else "新的 0-1 项目"),
                )
        if source:
            source = sources.claim(h, source, task_type)
            if not source:
                return False

            def heartbeat():
                while not lease_stop.wait(30):
                    try:
                        if not sources.renew(h, source):
                            return
                    except Exception as exc:
                        h.log_workflow_exception("-", "pairwise-prefill-source-lease", exc)
                        return

            lease_thread = threading.Thread(
                target=heartbeat, name="pairwise-prefill-source-lease", daemon=True
            )
            lease_thread.start()

        def progress(detail, rejection=""):
            if not _current(h, batch):
                return
            with LOCK:
                stage = f"{task_type}：{detail}"
                current = _prefill_snapshot(lane)
                updates = {"stage": stage}
                if stage != current["stage"]:
                    updates["stage_started_at"] = time.monotonic()
                _update_prefill(lane, **updates)

        project_number = history.next_project_number(h)
        with LOCK:
            pending_id = _prefill_snapshot(lane)["pending_id"]
        context = _notebook_context(h, task_type, source, project_number)
        search_text = source["pair"]["prompt"] if source else values.get("pairwise_auto_last_rejection", "")
        h.CODEX_JOB_CONTEXT.progress = progress
        progress("正在启动单路独立题面生成")
        with notebook.generation(h, context, pending_id, search_text) as lessons:
            if task_type == "0-1 代码生成":
                seen_history = history.records(h) + candidate_bank.ready_records(h)
                draft = candidate_bank.review_next_legacy(h, project_number, seen_history, progress)
                if draft is None:
                    draft = h.generate_task_draft(
                        project_number=project_number,
                        initial_feedback=_retry_feedback(h, values, task_type),
                        base_policy=GUIDANCE + ROOT_DEPTH_GUIDANCE,
                        history_lessons=lessons["feedback"],
                        progress=progress, history_override=seen_history, race_lanes=1,
                    )
            else:
                draft = _generate_continuation_race(
                    h, source, task_type, progress, lessons["feedback"], lanes=1
                )
            h.ensure_job_active(job_key)
            if not _current(h, batch):
                return False
            if task_type == "0-1 代码生成":
                _payload(h, draft, directory)
            else:
                _validate_continuation_draft(h, draft, task_type)
            staged_id = candidate_bank.stage(
                h, task_type, draft, project_number, source,
                **_candidate_capacity(h, values),
            )
            if not staged_id:
                return False
            candidate_bank.mark_legacy_staged(h, draft.get("legacy_source_run_id", ""), staged_id)
            _record_generation_outcome(h, task_type)
            return True
    except h.JobCancelled:
        return False
    except Exception as exc:
        failed, cooldown = _source_failure_policy(h, exc)
        sources.release(h, source, str(exc), failed=failed, cooldown_after_attempt=cooldown)
        h.log_workflow_exception("-", "pairwise-auto-prefill", exc)
        if _current(h, batch):
            _record_generation_outcome(h, task_type, exc)
            # Generation now lives exclusively in the three independent
            # lanes, so failure accounting and retry feedback must be written
            # here rather than by the lightweight dispatch coordinator.
            _generation_failed(h, batch, exc, source, task_type)
            with LOCK:
                _update_prefill(
                    lane, error=str(exc)[:1200],
                    next_at=time.monotonic() + (60 if not failed else 0),
                )
        return False
    finally:
        candidate_bank.release_legacy_review(h, draft.get("legacy_source_run_id", "") if draft else "")
        lease_stop.set()
        if lease_thread:
            lease_thread.join(timeout=2)
        sources.release(h, source)
        h.CODEX_JOB_CONTEXT.key = previous_key
        h.CODEX_JOB_CONTEXT.progress = previous_progress
        with LOCK:
            _update_prefill(
                lane, active=False, pending_id="", task_type="", title="",
                created_at="", stage_started_at=0.0,
            )


def _loop(h):
    while True:
        WAKE.wait(POLL_SECONDS)
        WAKE.clear()
        run_once(h)


def _prefill_loop(h, lane):
    wake = _prefill_wake(lane)
    while True:
        wake.wait(POLL_SECONDS)
        wake.clear()
        try:
            _prefill_once(h, lane)
        except Exception as exc:
            h.log_workflow_exception("-", f"pairwise-prefill-loop-{lane}", exc)


def start_coordinator(h):
    global STARTED
    initialize(h)
    with LOCK:
        if STARTED:
            return
        STARTED = True
    threading.Thread(
        target=_loop,
        args=(h,),
        name="pairwise-auto-refill-coordinator",
        daemon=True,
    ).start()
    for lane in range(1, PREFILL_LANE_COUNT + 1):
        threading.Thread(
            target=_prefill_loop,
            args=(h, lane),
            name=f"pairwise-auto-prefill-{lane}-coordinator",
            daemon=True,
        ).start()
    WAKE.set()
    for lane in range(1, PREFILL_LANE_COUNT + 1):
        _prefill_wake(lane).set()


def source_cooldowns(h):
    with h.db_connection() as db:
        rows = db.execute("""SELECT source_pair_id,task_type,cooldown_until,reason
            FROM pairwise_source_locks WHERE cooldown_until>? ORDER BY cooldown_until""",
                          (time.time(),)).fetchall()
    return [dict(row) for row in rows]


def _validate_continuation_draft(h, draft, target_task_type):
    difficulty = str(draft.get("task_difficulty") or "")
    allowed = {"困难"} if target_task_type == "Bug 修复" else {"困难", "地狱"}
    if difficulty not in allowed:
        raise ValueError("Bug 自动题只接受恰好困难" if target_task_type == "Bug 修复"
                         else "自动迭代题面未通过困难度复核")
    review_payload = {"difficulty_review": draft.get("difficulty_review")}
    errors = (h.bug_difficulty_review_errors(review_payload)
              if target_task_type == "Bug 修复"
              else h.difficulty_review_errors(review_payload))
    if h.reviewed_task_difficulty(draft) != difficulty:
        errors.append("难度标签与独立复核结论不一致")
    if errors:
        raise ValueError("自动迭代题面未通过困难度复核：" + "；".join(errors))
    if str(draft.get("task_type") or "") != target_task_type:
        raise ValueError(f"自动题面类型不是指定的 {target_task_type}")
    prompt = str(draft.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("自动迭代题面为空")
    if target_task_type == "Bug 修复":
        seed_snapshot = draft.get("seed_snapshot")
        if seed_snapshot:
            if (not isinstance(seed_snapshot, dict)
                    or not all(seed_snapshot.get(key) for key in ("bundle_path", "sha", "tree"))
                    or not Path(str(seed_snapshot["bundle_path"])).is_file()):
                raise ValueError("Bug 修复题缺少有效的冻结预置缺陷快照")
            if seed_snapshot.get('verification_mode') in {'static', 'static_first'}:
                review = draft.get('independent_review')
                review_errors = h.bugfix_review_errors(review or {})
                if (not seed_snapshot.get('code_evidence') or not seed_snapshot.get('static_analysis')
                        or not isinstance(review, dict) or review.get('approved') is not True
                        or review_errors):
                    raise ValueError('静态 Bug 修复题缺少代码依据或独立审核，不能直接放行')
                from .bug_difficulty import audit_matches
                if not audit_matches(draft):
                    raise ValueError('静态 Bug 修复题缺少与冻结快照及最终题面一致的开发者视角难度盲审')
        else:
            review = draft.get("independent_review")
            if (not draft.get("evidence_source_commit")
                    or not draft.get("evidence_verified_at")
                    or not isinstance(draft.get("confirmed_bugs"), list)
                    or not draft["confirmed_bugs"]
                    or not isinstance(review, dict)
                    or review.get("approved") is not True
                    or h.bugfix_review_errors(review)):
                raise ValueError("原产物 Bug 修复题缺少独立源码核验与审核证据")
    return difficulty, prompt


def _transient_generation_failure(h, exc):
    if isinstance(exc, h.UpstreamCapacityError) or bug_runtime.is_timeout(exc):
        return True
    if getattr(exc, 'source_environment_failure', False):
        return True
    return sources.transient_cooldown_reason(str(exc))


def _difficulty(h, context, candidate, progress):
    progress("真实性已通过，正在对冻结源码进行开发者视角难度盲审")
    blind = bug_difficulty.review_difficulty(h, context, candidate)
    if _contradictory_medium(blind):
        progress("首轮难度结论与其约束证据矛盾，正在进行一次无提示盲审仲裁")
        second = bug_difficulty.review_difficulty(h, context, candidate)
        if not h.bug_difficulty_review_errors(second):
            blind = second
    return blind


def provisional_admission_errors(h, draft):
    """Validate stored provisional evidence without silently re-labelling it hard."""
    admission = draft.get("difficulty_admission") if isinstance(draft, dict) else None
    if not isinstance(admission, dict) or admission.get("status") != PROVISIONAL_STATUS:
        return ["缺少困难潜力待复评标记"]
    errors = []
    review = draft.get("difficulty_review")
    if not isinstance(review, dict):
        return ["困难潜力题缺少前置难度盲审"]
    errors.extend(h.difficulty_policy.analysis_errors(review, require_hard=False))
    if review.get("level") != "中等":
        errors.append("困难潜力通道只接受前置盲审为中等的题目")
    card = draft.get("design_preflight", {}).get("candidate") if isinstance(draft, dict) else None
    card = card if isinstance(card, dict) else {}
    problems = card.get("problems") if isinstance(card.get("problems"), list) else []
    if review.get("routine_solution_sufficient") is not False and len(problems) < MIN_PROBLEMS:
        errors.append("困难潜力题的完整常规路径已足够")
    if review.get("one_hour_feasible") is not True:
        errors.append("困难潜力题无法在运行预算内闭环")
    if admission.get("preflight_level") != "中等" or admission.get("target_level") != "困难":
        errors.append("困难潜力题的前置等级或目标等级无效")
    if admission.get("final_gate") != PROVISIONAL_FINAL_GATE:
        errors.append("困难潜力题没有绑定执行后难度复评")
    mode = admission.get("admission_mode")
    try:
        attempt_number = int(admission.get("source_attempt_number") or 0)
    except (TypeError, ValueError):
        attempt_number = 0
    if mode == "substantial_patch":
        scope = admission.get("patch_scope") if isinstance(admission.get("patch_scope"), dict) else {}
        try:
            changed_lines = int(scope.get("changed_lines") or 0)
        except (TypeError, ValueError):
            changed_lines = 0
        if attempt_number not in range(1, BUG_STRICT_ATTEMPTS + 1):
            errors.append("大改动替代准入不属于前两轮严格尝试")
        if changed_lines < BUG_SUBSTANTIAL_PATCH_MIN_CHANGED_LINES:
            errors.append("大改动替代准入没有达到真实生产代码修改量")
    elif mode == "potential_fallback":
        if attempt_number <= BUG_STRICT_ATTEMPTS:
            errors.append("困难潜力通道只能在前两轮严格准入失败后开放")
        if (not _nonempty_strings(admission.get("signals"))
                or len(admission["signals"]) < MIN_PROVISIONAL_SIGNALS):
            errors.append("困难潜力题缺少完整的结构性信号")
    else:
        errors.append("困难潜力题缺少有效的分级准入模式")
    independent = draft.get("independent_review")
    if not isinstance(independent, dict) or independent.get("difficulty_admission") != admission:
        errors.append("困难潜力准入记录与独立审核不一致")
    elif _review_state(independent)[0] != "approved":
        errors.append("困难潜力题没有通过真实性、范围与查重审核")
    if not bug_difficulty.audit_matches(draft):
        errors.append("困难潜力题缺少与冻结源码和题面一致的难度盲审")
    return list(dict.fromkeys(errors))


def _review_state(review, *, min_problems=MIN_PROBLEMS):
    bug = review.get("bug_review") if isinstance(review, dict) else None
    if not isinstance(bug, dict):
        return "fault", "真实性审核缺少 Bug 明细"
    try:
        count = int(bug.get("verified_bug_count"))
    except (TypeError, ValueError):
        count = -1
    unverified = list(bug.get("unverified_bugs") or [])
    overlaps = list(bug.get("overlapping_sequences") or [])
    leaks = list(bug.get("solution_leaks") or [])
    styles = list(bug.get("style_issues") or [])
    if not min_problems <= count <= MAX_PROBLEMS or unverified:
        return "fault", "；".join(str(x) for x in unverified) or "源码确认的问题数量不合格"
    if overlaps:
        return "duplicate", "；".join(str(x) for x in overlaps)
    if bug.get("scope_too_large") is not False or bug.get("single_focus") is not True:
        return "scope", "题面没有保持同一项目下的相关修复范围，或组合范围过大"
    if leaks or styles:
        return "rewrite", "；".join(str(x) for x in leaks + styles)
    if review.get("approved") is not True or review.get("task_type") != "Bug 修复":
        reasons = review.get("reasons") or []
        return "fault", "；".join(str(x) for x in reasons) or "真实性审核未批准"
    return "approved", ""


def _contradictory_medium(blind):
    review = blind.get("difficulty_review") if isinstance(blind, dict) else None
    if not isinstance(review, dict) or review.get("level") not in {"简单", "中等"}:
        return False
    return (review.get("routine_solution_sufficient") is False
            and bool(review.get("design_tradeoffs"))
            and bool(review.get("verification_obligations"))
            and bool(review.get("hard_drivers"))
            and bool(review.get("human_work_reasons")))


def _nonempty_strings(value):
    return (isinstance(value, list) and bool(value)
            and all(isinstance(item, str) and item.strip() for item in value))
