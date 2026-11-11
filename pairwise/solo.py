"""SOLO-QA GSB export-list and authenticated-helper payloads."""
from __future__ import annotations

import hashlib
import json
import mimetypes
import re
from datetime import datetime, timezone
from pathlib import Path

from . import models as m, date_filters
from . import recording, workflow
from .validation import delivery_description_style_issues, gsb_reason_style_issues, preflight


PAGE_SIZE = 20
MODEL_POLICY_START = "2026-09-29"
MODEL_POLICY_END = "2026-10-01"
SUBMIT_URL = "https://solo2.jzxhnh.com/app/gsb/submit"
DETAIL_URL = "https://solo2.jzxhnh.com/app/gsb/submissions/{remote_id}"
LOCAL_STATES = {
    "not_submitted", "submitting", "qc_pending", "qc_passed", "needs_fix",
    "discarded", "failed", "remote_missing",
}
READINESS_FILTERS = {"ready", "blocked"}
RECORDING_FILTERS = {"processing", "complete", "failed", "partial", "missing"}
SOLO_FILTERS = LOCAL_STATES | {"local_changed"}
REMOTE_STATES = {
    "SUBMITTED": "qc_pending",
    "QC_PASSED": "qc_passed",
    "PENDING_FIX": "needs_fix",
    "DISCARDED": "discarded",
}
def _latest_attempts(h, pair_id):
    items = m.attempts(h, pair_id)
    return {side: m.latest(items, side) for side in ("A", "B")}


def _task_type(value):
    return {
        "0-1 代码生成": "0-1代码生成",
        "Feature 迭代": "feature迭代",
        "Bug 修复": "Bug修复",
    }.get(value, value)


def _operating_system(value):
    text = str(value or "").lower()
    return "Windows" if "windows" in text else "MacOS/Linux"


def _reproducibility(value, *context):
    text = str(value or "").strip()
    choices = ("无外部依赖", "有外部依赖，未容器化", "已容器化，可一键起环境")
    if text in choices:
        return text
    combined = " ".join([text, *(str(item or "") for item in context)]).lower()
    if "未容器化" in combined:
        return "有外部依赖，未容器化"
    if any(word in combined for word in ("docker", "compose", "容器")):
        return "已容器化，可一键起环境"
    if any(word in combined for word in ("无外部依赖", "无需外部依赖", "no external depend")):
        return "无外部依赖"
    return ""


def values(h, pair_id):
    pair = m.task(h, pair_id)
    attempts = _latest_attempts(h, pair_id)
    from .difficulty import assessment, submission_choice
    actual_difficulty, _ = submission_choice(pair, assessment(h, pair, attempts))
    a, b = attempts["A"], attempts["B"]
    config = pair["config"]
    initial = f"{pair['repo_url']}/commit/{pair['base_sha']}"
    a_artifact = f"{pair['repo_url']}/commit/{a['artifact_sha']}" if a else ""
    b_artifact = f"{pair['repo_url']}/commit/{b['artifact_sha']}" if b else ""
    # Include stable field keys and visible labels. The helper maps these values
    # against the live SOLO form schema, so a harmless field-order change does
    # not misroute A evidence into B.
    result = {
        "user_prompt": pair["prompt"], "User Prompt": pair["prompt"],
        "question_type": _task_type(pair["task_type"]), "任务类型": _task_type(pair["task_type"]),
        "difficulty": actual_difficulty, "任务难度": actual_difficulty,
        "languages": pair["language_framework"], "语言/框架": pair["language_framework"],
        "harness": config.get("harness", ""), "Harness": config.get("harness", ""),
        "harness_version": config.get("harness_version", ""), "Harness 版本": config.get("harness_version", ""),
        "os_platform": _operating_system(config.get("operating_system")),
        "操作系统": _operating_system(config.get("operating_system")),
        "env_snapshot": initial, "初始环境快照": initial,
        "a_session_id": a.get("session_id", "") if a else "", "A-SessionID": a.get("session_id", "") if a else "",
        "a_artifact_snapshot": a_artifact, "A-产物快照": a_artifact,
        "b_session_id": b.get("session_id", "") if b else "", "B-SessionID": b.get("session_id", "") if b else "",
        "b_artifact_snapshot": b_artifact, "B-产物快照": b_artifact,
        "a_delivery_score": pair["a_delivery_score"], "A-交付完整性": pair["a_delivery_score"],
        "a_delivery_description": pair["a_delivery_description"],
        "A-交付完整性描述": pair["a_delivery_description"],
        "b_delivery_score": pair["b_delivery_score"], "B-交付完整性": pair["b_delivery_score"],
        "b_delivery_description": pair["b_delivery_description"],
        "B-交付完整性描述": pair["b_delivery_description"],
        "gsb_verdict": pair["gsb"], "GSB 结论": pair["gsb"],
        "gsb_reason": pair["reason"], "GSB 理由": pair["reason"],
        "validity": "有效", "有效性": "有效",
        "remark": pair["notes"], "备注": pair["notes"],
    }
    repro = _reproducibility(
        pair["reproducibility"],
        pair["prompt"],
        pair["language_framework"],
        json.dumps(pair["verification_commands"], ensure_ascii=False),
    )
    if repro:
        result.update({"repro_level": repro, "环境可复现等级": repro})
    return result


def payload_sha256(h, pair_id):
    attempts = _latest_attempts(h, pair_id)
    evidence = {
        side: {
            "trace_sha256": (attempts[side] or {}).get("trace_sha256", ""),
            "recording": _recording_signature(attempts[side]),
        }
        for side in ("A", "B")
    }
    canonical = json.dumps({"values": values(h, pair_id), "evidence": evidence},
                           ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def summary(h, pair_id):
    with h.db_connection() as db:
        row = db.execute(
            "SELECT * FROM pairwise_solo_submissions WHERE pair_id=?", (pair_id,)
        ).fetchone()
    if not row:
        return {"state": "not_submitted", "remote_id": "", "remote_status": "",
                "qc_summary": "", "error": "", "detail_url": ""}
    item = dict(row)
    current_hash = payload_sha256(h, pair_id)
    state = item["state"]
    if item["remote_submission_id"] and item["payload_sha256"] and item["payload_sha256"] != current_hash:
        state = "local_changed"
    remote_id = item["remote_submission_id"]
    return {
        "state": state,
        "remote_id": remote_id,
        "remote_status": item["remote_status"],
        "qc_summary": item["qc_summary"],
        "error": item["error"],
        "submitted_at": item["submitted_at"],
        "last_synced_at": item["last_synced_at"],
        "detail_url": DETAIL_URL.format(remote_id=remote_id) if remote_id else "",
    }


def quick_readiness(h, pair_id):
    pair = m.task(h, pair_id)
    attempts = _latest_attempts(h, pair_id)
    from .difficulty import assessment, submission_choice
    _, issues = submission_choice(pair, assessment(h, pair, attempts))
    for side in ("A", "B"):
        attempt = attempts[side]
        if not attempt or attempt["status"] != "complete":
            issues.append(f"{side} 尚未完成归档")
            continue
        if not attempt["trace_path"] or not Path(attempt["trace_path"]).is_file():
            issues.append(f"{side} 缺少本地轨迹")
        recording_path = Path(attempt["recording_path"] or "")
        if attempt["recording_status"] not in {"", "complete"}:
            issues.append(f"{side} 本地录屏尚未完成")
        if not recording_path.is_file():
            issues.append(f"{side} 缺少本地录屏")
        elif recording_path.suffix.lower() not in {".mp4", ".mov", ".webm", ".m4v"}:
            issues.append(f"{side} 本地录屏格式不受 SOLO-QA 支持")
        elif recording_path.stat().st_size > 500 * 1024 * 1024:
            issues.append(f"{side} 本地录屏超过 500 MB")
        if not attempt["video_seconds"] or not 0 < attempt["video_seconds"] <= 90:
            issues.append(f"{side} 录屏时长须在 90 秒内")
    if pair["gsb"] not in m.CHOICES or not pair["reason"].strip():
        issues.append("GSB 结论和理由尚未填写")
    elif len(re.sub(r"\s", "", pair["reason"])) < 60:
        issues.append("GSB 理由至少 60 字")
    else:
        issues.extend(gsb_reason_style_issues(pair["reason"]))
    if pair.get("review_source") and pair["review_source"] != m.current_review_source(list(attempts.values())):
        issues.append("GSB 评价对应旧的 A/B 结果，请核对并重新保存")
    for side in ("a", "b"):
        label = side.upper()
        expected_model = pair.get(f"{side}_execution_model", m.AUTO_MODEL)
        if attempts[label] and (attempts[label]["model"] != expected_model
                                or m.actual_model_name(attempts[label]) != expected_model):
            issues.append(f"{label} Session 模型与本题选定的 modelname 不一致")
        if m.actual_model_name(attempts[label]) not in m.MODEL_NAMES:
            issues.append(f"{label} Session 的实际模型不受 SOLO-QA 支持")
        if pair.get(f"{side}_delivery_score") not in range(1, 6):
            issues.append(f"{label} 交付完整性评分尚未填写")
        if not str(pair.get(f"{side}_delivery_description") or "").strip():
            issues.append(f"{label} 交付完整性描述尚未填写")
        else:
            issues.extend(delivery_description_style_issues(
                pair[f"{side}_delivery_description"], label))
    return not issues, issues


def _completed_where():
    return """
      AND (SELECT status FROM pairwise_attempts a
           WHERE a.pair_id=t.id AND a.side='A' ORDER BY a.number DESC LIMIT 1)='complete'
      AND (SELECT status FROM pairwise_attempts b
           WHERE b.pair_id=t.id AND b.side='B' ORDER BY b.number DESC LIMIT 1)='complete'
    """


def _completion_epoch(value):
    """Compare recorded completion instants, never review/recording update times."""
    try:
        text = re.sub(r"\s*([+-]\d{2})(\d{2})$", r"\1:\2", str(value or "").strip())
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return stamp.replace(tzinfo=timezone.utc).timestamp() if stamp.tzinfo is None else stamp.timestamp()
    except (ValueError, OverflowError):
        return 0


def _export_item(h, row, display_numbers):
    pair = m.task(h, row["id"])
    attempts = _latest_attempts(h, pair["id"])
    from .difficulty import assessment, submission_choice
    difficulty = assessment(h, pair, attempts)
    chosen_difficulty, _ = submission_choice(pair, difficulty)
    ready, issues = quick_readiness(h, pair["id"])
    solo_state = summary(h, pair["id"])
    finished = max((row["a_finished_at"] or "", row["b_finished_at"] or ""), key=_completion_epoch)
    return {
        "id": pair["id"], "title": pair["title"], "repo_name": pair["repo_name"],
        "project_number": pair["project_number"], "task_type": pair["task_type"],
        "project_category": pair["project_category"],
        "display_number": display_numbers.get(pair["id"], f"{int(pair['project_number']):04d}"),
        "difficulty": pair["difficulty"], "prompt": pair["prompt"],
        "submission_difficulty": pair["submission_difficulty"],
        "chosen_difficulty": chosen_difficulty,
        "difficulty_assessment": difficulty,
        "gsb": pair["gsb"], "reason": pair["reason"], "notes": pair["notes"],
        "a_delivery_score": pair["a_delivery_score"],
        "a_delivery_description": pair["a_delivery_description"],
        "a_model_name": m.actual_model_name(attempts["A"]),
        "b_execution_model": pair.get("b_execution_model", m.AUTO_MODEL),
        "b_delivery_score": pair["b_delivery_score"],
        "b_delivery_description": pair["b_delivery_description"],
        "b_model_name": m.actual_model_name(attempts["B"]),
        "human_confirmed": bool(pair["human_confirmed"]), "revision": pair["revision"],
        "review_source": pair["review_source"],
        "rerun": {
            side: {"available": solo_state["state"] != "submitting" and
                   workflow.can_rerun_side(pair, list(attempts.values()), side)}
            for side in ("A", "B")
        },
        "a_session_id": attempts["A"].get("session_id", "") if attempts["A"] else "",
        "b_session_id": attempts["B"].get("session_id", "") if attempts["B"] else "",
        "created_at": pair["created_at"], "finished_at": finished,
        "export_ready": ready, "export_issues": issues,
        "recording": {
            side: {
                "status": (attempts[side] or {}).get("recording_status", ""),
                "duration": (attempts[side] or {}).get("recording_duration"),
                "has_file": bool(attempts[side] and Path(attempts[side].get("recording_path") or "").is_file()),
                "url": (f"/api/pairwise/attempts/{attempts[side]['id']}/recording-file"
                        if attempts[side] and Path(attempts[side].get("recording_path") or "").is_file() else ""),
                "failure_evidence": str((attempts[side] or {}).get("recording_error") or "").startswith(
                    ("已录制原始产物启动失败", "已录制原始产物运行失败")
                ),
                "progress": recording.progress(h, attempts[side] or {}),
            }
            for side in ("A", "B")
        },
        "solo_qa": solo_state,
    }


def _recording_filter_state(item):
    sides = tuple((item.get("recording") or {}).get(side) or {} for side in ("A", "B"))
    statuses = {str(side.get("status") or "") for side in sides}
    if statuses & {"queued", "preparing", "recording"}:
        return "processing"
    if all(side.get("status") == "complete" and side.get("has_file") for side in sides):
        return "complete"
    if "failed" in statuses:
        return "failed"
    if any(side.get("has_file") for side in sides):
        return "partial"
    return "missing"


def exports_page(h, query):
    try:
        page = max(1, int(query.get("page", ["1"])[0]))
    except (TypeError, ValueError):
        raise h.WorkflowError("页码无效")
    term = str(query.get("query", [""])[0]).strip()
    number = str(query.get("number", [""])[0]).strip()[:40]
    task_type = str(query.get("task_type", [""])[0]).strip()
    difficulty_filter = str(query.get("difficulty", [""])[0]).strip()
    readiness_filter = str(query.get("readiness", [""])[0]).strip()
    recording_filter = str(query.get("recording", [""])[0]).strip()
    solo_filter = str(query.get("solo", [""])[0]).strip()
    date_start, date_end = date_filters.date_range(h, query)
    if task_type and task_type not in m.TASK_TYPES:
        raise h.WorkflowError("任务类型筛选无效")
    if difficulty_filter and difficulty_filter not in {"简单", "中等", "困难", "地狱", "待复核"}:
        raise h.WorkflowError("难度筛选无效")
    if readiness_filter and readiness_filter not in READINESS_FILTERS:
        raise h.WorkflowError("提交条件筛选无效")
    if recording_filter and recording_filter not in RECORDING_FILTERS:
        raise h.WorkflowError("录屏状态筛选无效")
    if solo_filter and solo_filter not in SOLO_FILTERS:
        raise h.WorkflowError("SOLO-QA 状态筛选无效")
    pattern = f"%{term}%"
    clauses = ["t.deleted_at IS NULL", "(t.title LIKE ? OR t.prompt LIKE ? OR t.repo_name LIKE ?)"]
    params = [pattern, pattern, pattern]
    if task_type:
        clauses.append("t.task_type=?")
        params.append(task_type)
    display_numbers = m.display_numbers(h)
    if number:
        matching_ids = [pair_id for pair_id, value in display_numbers.items()
                        if number.casefold() in value.casefold()]
        if matching_ids:
            clauses.append("t.id IN (" + ",".join("?" for _ in matching_ids) + ")")
            params.extend(matching_ids)
        else:
            clauses.append("0")
    where = "WHERE " + " AND ".join(clauses) + _completed_where()
    params = tuple(params)
    post_filter = bool(difficulty_filter or readiness_filter or recording_filter or solo_filter)
    order = """ORDER BY MAX(completion_epoch(a_finished_at),completion_epoch(b_finished_at)) DESC,
                         t.created_at DESC,t.id DESC"""
    with h.db_connection() as db:
        db.create_function("completion_epoch", 1, _completion_epoch)
        rows = db.execute(
            f"""SELECT t.id,
                (SELECT a.finished_at FROM pairwise_attempts a WHERE a.pair_id=t.id
                 AND a.side='A' ORDER BY a.number DESC LIMIT 1) AS a_finished_at,
                (SELECT b.finished_at FROM pairwise_attempts b WHERE b.pair_id=t.id
                 AND b.side='B' ORDER BY b.number DESC LIMIT 1) AS b_finished_at
                FROM pairwise_tasks t {where}
                {order}""",
            params,
        ).fetchall()
    if date_start or date_end:
        rows = [row for row in rows if date_filters.includes(
            max((row["a_finished_at"] or "", row["b_finished_at"] or ""),
                key=_completion_epoch), date_start, date_end)]
    if post_filter:
        filtered = []
        for row in rows:
            item = _export_item(h, row, display_numbers)
            actual_difficulty = (item["submission_difficulty"] or
                                 item["difficulty_assessment"]["level"] or "待复核")
            if difficulty_filter and actual_difficulty != difficulty_filter:
                continue
            if readiness_filter and ("ready" if item["export_ready"] else "blocked") != readiness_filter:
                continue
            if recording_filter and _recording_filter_state(item) != recording_filter:
                continue
            if solo_filter and item["solo_qa"].get("state", "not_submitted") != solo_filter:
                continue
            filtered.append(item)
        total = len(filtered)
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(page, pages)
        items = filtered[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]
    else:
        total = len(rows)
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(page, pages)
        page_rows = rows[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]
        items = [_export_item(h, row, display_numbers) for row in page_rows]
    return {"items": items, "total": total, "page": page, "pages": pages,
            "page_size": PAGE_SIZE, "submit_url": SUBMIT_URL}


def bundle(h, pair_id):
    pair = m.task(h, pair_id)
    result = preflight(h, pair_id)
    if not result["eligible"]:
        raise h.WorkflowError("提交前检查未通过：" + "；".join(result["issues"]))
    attempts = _latest_attempts(h, pair_id)
    if MODEL_POLICY_START <= h.now_text()[:10] <= MODEL_POLICY_END:
        models = {side: m.actual_model_name(attempts[side]) for side in ("A", "B")}
        if models not in ({"A": m.AUTO_MODEL, "B": "ark/urm-03"},
                          {"A": m.AUTO_MODEL, "B": m.AUTO_MODEL}):
            raise h.WorkflowError("当前规则仅允许 A auto_model/urm、B auto_model/urm 或 ark/urm-03 的提交组合")
    trajectories, recordings = {}, {}
    for side in ("A", "B"):
        item = attempts[side]
        path = Path(item["trace_path"])
        trajectories[side] = {
            "name": f"{item['session_id']}.jsonl",
            "size": path.stat().st_size,
            "sha256": item["trace_sha256"],
            "url": f"http://127.0.0.1:8765/api/pairwise/solo/{pair_id}/trajectory/{side}",
        }
        recording_path = Path(item["recording_path"] or "")
        recordings[side] = {
            "name": recording_path.name,
            "size": recording_path.stat().st_size,
            "sha256": _sha256_file(recording_path),
            "content_type": mimetypes.guess_type(str(recording_path))[0] or "video/mp4",
            "url": f"http://127.0.0.1:8765/api/pairwise/attempts/{item['id']}/recording-file",
        }
    return {
        "id": pair_id,
        "values": values(h, pair_id),
        "models": {side: m.actual_model_name(attempts[side])
                   for side in ("A", "B")},
        "payload_sha256": payload_sha256(h, pair_id),
        "trajectories": trajectories,
        "recordings": recordings,
        "solo_qa": summary(h, pair_id),
    }


def _sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _recording_signature(attempt):
    path = Path((attempt or {}).get("recording_path") or "")
    try:
        stat = path.stat()
    except OSError:
        return ""
    return {"path": str(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def trajectory_path(h, pair_id, side):
    if side not in ("A", "B"):
        raise h.WorkflowError("轨迹侧别无效")
    attempt = _latest_attempts(h, pair_id)[side]
    path = Path((attempt or {}).get("trace_path") or "")
    if not path.is_file():
        raise h.WorkflowError(f"{side} 的原始轨迹尚未归档")
    return path, mimetypes.guess_type(str(path))[0] or "application/x-ndjson"


def record_state(h, data):
    pair_id = str(data.get("pair_id") or "")
    if not re.fullmatch(r"[a-f0-9]{12}", pair_id):
        raise h.WorkflowError("Pair 标识格式不正确")
    m.task(h, pair_id)
    state = str(data.get("state") or "")
    if state not in LOCAL_STATES:
        raise h.WorkflowError("提交状态无效")
    supplied_hash = str(data.get("payload_sha256") or "")
    current_hash = payload_sha256(h, pair_id)
    if supplied_hash and supplied_hash != current_hash:
        raise h.WorkflowError("本地 Pair 数据已变化，请刷新后重新提交")
    remote_id = str(data.get("remote_id") or "")[:128]
    now = h.now_text()
    submitted_at = str(data.get("submitted_at") or (now if remote_id and not data.get("synced") else ""))[:128]
    attempts = _latest_attempts(h, pair_id) if remote_id else {}
    submitted_models = {side: m.actual_model_name(attempts.get(side)) if remote_id else ""
                        for side in ("A", "B")}
    values_to_store = (
        pair_id, state, remote_id, str(data.get("remote_status") or "")[:64],
        str(data.get("qc_summary") or "")[:4000], supplied_hash,
        submitted_models["A"], submitted_models["B"],
        submitted_at, str(data.get("remote_updated_at") or "")[:128],
        now if data.get("synced") else "", str(data.get("error") or "")[:2000], now,
    )
    with h.db_connection() as db:
        db.execute("""INSERT INTO pairwise_solo_submissions
          (pair_id,state,remote_submission_id,remote_status,qc_summary,payload_sha256,
           a_model_name,b_model_name,submitted_at,remote_updated_at,last_synced_at,error,updated_at)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(pair_id) DO UPDATE SET
           state=excluded.state,
           remote_submission_id=CASE WHEN excluded.remote_submission_id<>'' THEN excluded.remote_submission_id ELSE pairwise_solo_submissions.remote_submission_id END,
           remote_status=CASE WHEN excluded.remote_status<>'' THEN excluded.remote_status ELSE pairwise_solo_submissions.remote_status END,
           qc_summary=CASE WHEN excluded.qc_summary<>'' THEN excluded.qc_summary ELSE pairwise_solo_submissions.qc_summary END,
           payload_sha256=CASE WHEN excluded.payload_sha256<>'' THEN excluded.payload_sha256 ELSE pairwise_solo_submissions.payload_sha256 END,
           a_model_name=CASE WHEN pairwise_solo_submissions.a_model_name='' THEN excluded.a_model_name ELSE pairwise_solo_submissions.a_model_name END,
           b_model_name=CASE WHEN pairwise_solo_submissions.b_model_name='' THEN excluded.b_model_name ELSE pairwise_solo_submissions.b_model_name END,
           submitted_at=CASE WHEN excluded.submitted_at<>'' THEN excluded.submitted_at ELSE pairwise_solo_submissions.submitted_at END,
           remote_updated_at=CASE WHEN excluded.remote_updated_at<>'' THEN excluded.remote_updated_at ELSE pairwise_solo_submissions.remote_updated_at END,
           last_synced_at=CASE WHEN excluded.last_synced_at<>'' THEN excluded.last_synced_at ELSE pairwise_solo_submissions.last_synced_at END,
           error=excluded.error,updated_at=excluded.updated_at""", values_to_store)
    return summary(h, pair_id)


def sync(h, data):
    remote_items = data.get("items")
    if not isinstance(remote_items, list) or len(remote_items) > 500:
        raise h.WorkflowError("远端 GSB 列表格式不正确")
    with h.db_connection() as db:
        rows = db.execute("""SELECT t.id,
          (SELECT r.session_id FROM pairwise_attempts a JOIN runs r ON r.id=a.id
           WHERE a.pair_id=t.id AND a.side='A' ORDER BY a.number DESC LIMIT 1) a_session_id,
          (SELECT r.session_id FROM pairwise_attempts b JOIN runs r ON r.id=b.id
           WHERE b.pair_id=t.id AND b.side='B' ORDER BY b.number DESC LIMIT 1) b_session_id
          FROM pairwise_tasks t""").fetchall()
    by_sessions = {(row["a_session_id"], row["b_session_id"]): row["id"] for row in rows
                   if row["a_session_id"] and row["b_session_id"]}
    matched, unmatched, seen_ids = 0, 0, set()
    for item in remote_items:
        if not isinstance(item, dict):
            continue
        pair_id = by_sessions.get((str(item.get("a_session_id") or ""),
                                   str(item.get("b_session_id") or "")))
        if not pair_id:
            unmatched += 1
            continue
        remote_id = str(item.get("id") or "")
        if remote_id:
            seen_ids.add(remote_id)
        state = REMOTE_STATES.get(str(item.get("status") or ""), "qc_pending")
        record_state(h, {
            "pair_id": pair_id,
            "state": state,
            "remote_id": remote_id,
            "remote_status": item.get("status"),
            "qc_summary": item.get("qc_summary"),
            "submitted_at": item.get("submitted_at"),
            "remote_updated_at": item.get("updated_at"),
            "synced": True,
            # A status sync does not prove the remote payload equals today's
            # local edits. Preserve the hash recorded at submission time so
            # summary() can continue to flag local changes.
            "payload_sha256": "",
            "error": "",
        })
        if state in {"needs_fix", "discarded"}:
            try:
                from . import notebook
                notebook.record_remote_topic_qc(
                    h, m.task(h, pair_id), remote_id, str(item.get("status") or ""),
                    item.get("qc_summary"),
                )
            except Exception as exc:
                h.log_workflow_exception(pair_id, "pairwise-remote-qc-feedback", exc)
        matched += 1
    remote_missing = 0
    if data.get("complete") is True:
        with h.db_connection() as db:
            known = db.execute("""SELECT pair_id,remote_submission_id FROM pairwise_solo_submissions
                                   WHERE remote_submission_id<>''""").fetchall()
            for row in known:
                if row["remote_submission_id"] not in seen_ids:
                    db.execute("""UPDATE pairwise_solo_submissions SET state='remote_missing',
                                  error='SOLO-QA 的我的 GSB 提交中未找到',last_synced_at=?,updated_at=?
                                  WHERE pair_id=?""", (h.now_text(), h.now_text(), row["pair_id"]))
                    remote_missing += 1
    return {"matched": matched, "unmatched": unmatched, "remote_missing": remote_missing}
