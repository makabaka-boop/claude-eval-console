import re
import stat
from datetime import datetime
from pathlib import Path

from . import deployment, models as m, workflow, validation, export, recording_guide, solo, recording, date_filters, upstream


PAGE_SIZE = 20
TASK_LIST_STATUSES = {"not_started", "active", "needs_attention", "completed", "skipped"}
DIFFICULTY_FILTERS = {"简单", "中等", "困难", "地狱", "待复核"}
GIT_SHA_RE = re.compile(r"[0-9a-f]{40}")


def file_written_at(value):
    if not value:
        return ""
    try:
        info = Path(value).stat()
        if stat.S_ISREG(info.st_mode):
            return datetime.fromtimestamp(info.st_mtime).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    except (OSError, ValueError, OverflowError):
        pass
    return ""


def evidence_times(attempt):
    # A trace is written throughout the session; its final write is the file's
    # generation time, not the later end of artifact verification/archiving.
    trace_time = file_written_at(attempt.get("trace_path"))
    recording_time = ""
    recording_source = ""
    if attempt.get("recording_path"):
        if attempt.get("recording_status") == "complete" and attempt.get("recording_finished_at"):
            recording_time = attempt["recording_finished_at"]
            recording_source = "recording_finished_at"
        else:
            recording_time = file_written_at(attempt["recording_path"])
            recording_source = "file_mtime" if recording_time else ""
    return {
        "trace_generated_at": trace_time,
        "trace_generated_at_source": "file_mtime" if trace_time else "",
        "recording_generated_at": recording_time,
        "recording_generated_at_source": recording_source,
    }


def git_change_summary(h, base_sha, attempt, include_files=True):
    """Summarize the archived artifact relative to the Pair's frozen main commit."""
    unavailable = {
        "available": False,
        "file_count": 0,
        "additions": 0,
        "deletions": 0,
        "binary_files": 0,
        "files": [],
    }
    artifact_sha = str(attempt.get("artifact_sha") or "").lower()
    base_sha = str(base_sha or "").lower()
    repo_path = str(attempt.get("repo_path") or "")
    if not artifact_sha or not base_sha or not repo_path:
        return unavailable
    if not GIT_SHA_RE.fullmatch(artifact_sha) or not GIT_SHA_RE.fullmatch(base_sha):
        return unavailable
    repository = Path(repo_path)
    if not repository.is_dir():
        return unavailable
    try:
        result = h.run_command(
            ["git", "diff", "--numstat", "--find-renames", base_sha, artifact_sha, "--"],
            cwd=repository,
            timeout=20,
            check=False,
        )
    except (OSError, ValueError, h.WorkflowError):
        return unavailable
    if result.returncode != 0:
        return unavailable
    files = []
    file_count = additions = deletions = binary_files = 0
    for line in result.stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        added_text, deleted_text, path = parts
        binary = added_text == "-" or deleted_text == "-"
        added = int(added_text) if added_text.isdigit() else 0
        deleted = int(deleted_text) if deleted_text.isdigit() else 0
        additions += added
        deletions += deleted
        binary_files += int(binary)
        file_count += 1
        if include_files:
            files.append({
                "path": path,
                "additions": added,
                "deletions": deleted,
                "binary": binary,
            })
    return {
        "available": True,
        "file_count": file_count,
        "additions": additions,
        "deletions": deletions,
        "binary_files": binary_files,
        "files": files,
    }


def runtime(h):
    with h.db_connection() as db:
        active_attempts = db.execute(
            "SELECT COUNT(*) FROM pairwise_attempts WHERE status IN ('starting','running','recovering','archiving')"
        ).fetchone()[0]
        queued_attempts = db.execute(
            "SELECT COUNT(*) FROM pairwise_attempts WHERE status='queued'"
        ).fetchone()[0]
        active_model_rows = db.execute(
            "SELECT a.side, r.model, COUNT(*) AS count "
            "FROM pairwise_attempts a JOIN runs r ON r.id=a.id "
            "JOIN pairwise_tasks t ON t.id=a.pair_id "
            "WHERE t.deleted_at IS NULL "
            "AND a.status IN ('starting','running','recovering','archiving') "
            "GROUP BY a.side, r.model ORDER BY a.side, r.model"
        ).fetchall()
    active_models = {"A": [], "B": []}
    for row in active_model_rows:
        active_models[row["side"]].append({"model": row["model"], "count": row["count"]})
    recording_queue = recording.queue_status(h)
    return {
        "app_version": h.APP_VERSION,
        "generation_policy_version": h.generation_policy.VERSION,
        "difficulty_policy_version": h.difficulty_policy.VERSION,
        "active_parallel": int(active_attempts),
        "max_parallel": h.configured_max_parallel_runs(),
        "queued_parallel": int(queued_attempts),
        "active_models": active_models,
        "model_defaults": m.model_defaults(h),
        "codex_active": int(getattr(h, "CODEX_SLOT_ACTIVE", 0)),
        "codex_max_parallel": int(getattr(h, "CODEX_BACKGROUND_MAX_PARALLEL", 4)),
        "solo_helper_path": str(h.SOLO_QA_EXTENSION_DIR),
        "solo_submit_url": solo.SUBMIT_URL,
        "recording": recording_queue,
        "deployment": deployment.status(h),
        "claude_upstream": upstream.status(h),
    }


def detail(h, pair_id, include_runtime=True, include_git_changes=False):
    pair = m.task(h, pair_id)
    pair["recording_guide"] = recording_guide.build(pair)
    pair["attempts"] = m.attempts(h, pair_id)
    from .difficulty import assessment
    pair["difficulty_assessment"] = assessment(h, pair)
    for attempt in pair["attempts"]:
        attempt.update(evidence_times(attempt))
        attempt["recording_progress"] = recording.progress(h, attempt)
        attempt["can_retry_unconfirmed"] = (
            attempt["id"] == m.latest(pair["attempts"], attempt["side"])["id"]
            and workflow.can_retry_unconfirmed(h, attempt, pair))
        attempt["can_rerun"] = (
            attempt["id"] == m.latest(pair["attempts"], attempt["side"])["id"]
            and workflow.can_rerun_side(pair, pair["attempts"], attempt["side"]))
    pair["local_project_folder"] = (
        Path(pair["attempts"][0]["run_directory"]).parent.name
        if pair["attempts"] else pair["project_folder"]
    )
    config = dict(pair["config"])
    config.pop("credential", None)
    pair["config"] = config
    pair["snapshot_url"] = f"{pair['repo_url']}/commit/{pair['base_sha']}" if pair["base_sha"] else ""
    pair["source_snapshot_url"] = (f"{pair['source_repo_url']}/commit/{pair['source_base_sha']}"
                                   if pair["source_base_sha"] else "")
    current = [m.latest(pair["attempts"], s) for s in ("A", "B")]
    # A retry starts a new current round.  Historical attempts must not keep the
    # task-level start time pinned to the very first run.
    started = [str(a.get("started_at") or "") for a in current if a]
    pair["started_at"] = min((value for value in started if value), default="")
    # Large artifacts can make Git diff take seconds. Page loading and polling
    # must stay independent of this optional, explicitly requested statistic.
    if include_git_changes:
        for attempt in current:
            if attempt:
                attempt["git_changes"] = git_change_summary(
                    h, pair.get("base_sha"), attempt, include_files=False)
    terminal = all(a and a["status"] not in (*m.ACTIVE, "upstream_paused") for a in current)
    pair["finished_at"] = ""
    if terminal:
        finished = [str(a.get("finished_at") or a.get("run_updated_at") or "") for a in current]
        pair["finished_at"] = max((value for value in finished if value), default="")
    pair["state"] = "待同时执行"
    if pair.get("auto_skip_reason") and not any(
            a and a["status"] in m.ACTIVE for a in current):
        pair["state"] = "已跳过"
    elif any(a and a["status"] == "upstream_paused" for a in current):
        pair["state"] = "Claude 上游暂停"
    elif upstream.is_paused(h) and any(a and a["status"] == "queued" for a in current):
        pair["state"] = "Claude 上游暂停"
    elif any(a and a["status"] == "recovering" for a in current):
        pair["state"] = "检查并恢复归档"
    elif any(a and a["status"] in m.ACTIVE for a in current):
        pair["state"] = "A/B 并行执行中"
    elif any(a and a["status"] in {"failed", "stopped", "archive_failed"} for a in current):
        pair["state"] = "需要处理"
    elif all(a and a["status"] == "complete" for a in current):
        if pair["human_confirmed"]:
            pair["state"] = "已填写 GSB"
        else:
            pair["state"] = "待人工 GSB"
    elif any(a and a["status"] == "complete" for a in current):
        pair["state"] = "等待另一侧完成"
    pair["solo_qa"] = solo.summary(h, pair_id)
    if pair["solo_qa"]["state"] == "submitting":
        for attempt in pair["attempts"]:
            attempt["can_rerun"] = False
    if include_runtime:
        pair["runtime"] = runtime(h)
    return pair


def tasks_page(h, query):
    try:
        page = max(1, int(query.get("page", ["1"])[0]))
    except ValueError:
        raise h.WorkflowError("页码无效")
    term = str(query.get("query", [""])[0]).strip()[:200]
    number = str(query.get("number", [""])[0]).strip()[:40]
    task_type = str(query.get("task_type", [""])[0]).strip()
    difficulty = str(query.get("difficulty", [""])[0]).strip()
    status = str(query.get("status", [""])[0]).strip()
    date_start, date_end = date_filters.date_range(h, query)
    if task_type and task_type not in m.TASK_TYPES:
        raise h.WorkflowError("任务类型筛选无效")
    if difficulty and difficulty not in DIFFICULTY_FILTERS:
        raise h.WorkflowError("难度筛选无效")
    if status and status not in TASK_LIST_STATUSES:
        raise h.WorkflowError("任务状态筛选无效")
    pattern = f"%{term}%"
    # One SQL result set for search, count and LIMIT/OFFSET.
    latest_a = "(SELECT status FROM pairwise_attempts a WHERE a.pair_id=pairwise_tasks.id AND a.side='A' ORDER BY a.number DESC LIMIT 1)"
    latest_b = "(SELECT status FROM pairwise_attempts b WHERE b.pair_id=pairwise_tasks.id AND b.side='B' ORDER BY b.number DESC LIMIT 1)"
    active_values = "'queued','starting','running','recovering','archiving','upstream_paused'"
    failed_values = "'failed','stopped','archive_failed'"
    status_sql = f"""CASE
        WHEN auto_skip_reason<>''
         AND COALESCE({latest_a},'') NOT IN ('queued','starting','running','recovering','archiving')
         AND COALESCE({latest_b},'') NOT IN ('queued','starting','running','recovering','archiving') THEN 'skipped'
        WHEN {latest_a} IN ({active_values}) OR {latest_b} IN ({active_values}) THEN 'active'
        WHEN {latest_a} IN ({failed_values}) OR {latest_b} IN ({failed_values}) THEN 'needs_attention'
        WHEN {latest_a}='complete' AND {latest_b}='complete' THEN 'completed'
        WHEN {latest_a}='complete' OR {latest_b}='complete' THEN 'active'
        ELSE 'not_started' END"""
    listing = f"""WITH visible_tasks AS (
        SELECT id,title,prompt,repo_name,created_at,task_type,
               {status_sql} AS status_key
          FROM pairwise_tasks
        WHERE deleted_at IS NULL
    """
    params = []
    listing += ") "
    clauses = ["(title LIKE ? OR prompt LIKE ? OR repo_name LIKE ?)"]
    params.extend((pattern, pattern, pattern))
    if task_type:
        clauses.append("task_type=?")
        params.append(task_type)
    if status:
        clauses.append("status_key=?")
        params.append(status)
    if date_start:
        clauses.append("substr(created_at,1,10)>=?")
        params.append(date_start)
    if date_end:
        clauses.append("substr(created_at,1,10)<=?")
        params.append(date_end)
    if number:
        matching_ids = [pair_id for pair_id, value in m.display_numbers(h).items()
                        if number.casefold() in value.casefold()]
        number_clauses = []
        if matching_ids:
            number_clauses.append("id IN (" + ",".join("?" for _ in matching_ids) + ")")
            params.extend(matching_ids)
        clauses.append("(" + " OR ".join(number_clauses or ["0"]) + ")")
    where = " WHERE " + " AND ".join(clauses)
    params = tuple(params)
    with h.db_connection() as db:
        db.execute("BEGIN")
        if difficulty:
            rows = db.execute(listing + "SELECT id FROM visible_tasks" + where +
                              " ORDER BY created_at DESC,id DESC", params).fetchall()
        else:
            total = db.execute(listing + "SELECT COUNT(*) FROM visible_tasks" + where, params).fetchone()[0]
            pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
            page = min(page, pages)
            rows = db.execute(
                listing + "SELECT id FROM visible_tasks" + where +
                " ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
                (*params, PAGE_SIZE, (page - 1) * PAGE_SIZE),
            ).fetchall()
    if difficulty:
        from .difficulty import assessment
        filtered = []
        for row in rows:
            pair = m.task(h, row["id"])
            reviewed = assessment(h, pair)
            # Use the current review shown in the table, including source
            # invalidation and manual overrides, rather than the proposed label.
            if (reviewed["level"] or "待复核") == difficulty:
                filtered.append(row)
        total = len(filtered)
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(page, pages)
        rows = filtered[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]
    items = []
    for row in rows:
        pair = detail(h, row["id"], include_runtime=False)
        summary = {k: pair[k] for k in (
            "id", "title", "repo_name", "project_number", "display_number",
            "project_folder", "local_project_folder", "project_category", "task_type",
            "difficulty", "difficulty_assessment", "state", "auto_skip_reason",
            "project_directory", "origin",
            "created_at", "started_at", "finished_at"
        )}
        summary["attempts"] = [{k: a[k] for k in ("side", "status", "number", "started_at", "prompt_sent")}
                               for a in pair["attempts"]]
        items.append(summary)
    return {"items": items, "total": total, "page": page, "pages": pages, "page_size": PAGE_SIZE}


def get(handler, h, path, query):
    if path == "/api/pairwise/bug-generation-policy":
        from . import bug_lab
        handler.send_json({"version": 2, "source_directory": bug_lab.SOURCE_DIRECTORY,
                           "target_count": bug_lab.RESERVE_TARGET, "minimum_lines": 200})
        return
    if path == "/api/pairwise/bug-batches":
        from . import bug_lab
        handler.send_json({**bug_lab.listing(h, query), "runtime": runtime(h)})
        return
    if path == "/api/pairwise/iteration-batches":
        from . import iteration_lab
        handler.send_json({**iteration_lab.listing(h, query), "runtime": runtime(h)})
        return
    iteration_batch = re.fullmatch(r"/api/pairwise/iteration-batches/([a-f0-9]{12})", path)
    if iteration_batch:
        from . import iteration_lab
        handler.send_json(iteration_lab.get(h, iteration_batch[1]))
        return
    if path == "/api/pairwise/zero-one-batches":
        from . import zero_one_lab
        handler.send_json({**zero_one_lab.listing(h, query), "runtime": runtime(h)})
        return
    zero_one_batch = re.fullmatch(r"/api/pairwise/zero-one-batches/([a-f0-9]{12})", path)
    if zero_one_batch:
        from . import zero_one_lab
        handler.send_json(zero_one_lab.get(h, zero_one_batch[1]))
        return
    if path == "/api/pairwise/deployment":
        handler.send_json(deployment.status(h))
        return
    if path == "/api/pairwise/candidate-bank":
        from . import candidate_bank
        handler.send_json({**candidate_bank.listing(h, query), "runtime": runtime(h)})
        return
    if path == "/api/pairwise/candidate-bank/legacy":
        from . import candidate_bank
        handler.send_json(candidate_bank.legacy_listing(h, query))
        return
    if path == "/api/pairwise/notebook":
        from . import notebook
        handler.send_json({**notebook.list_entries(h, query), "runtime": runtime(h)})
        return
    if path == "/api/pairwise/notebook/download":
        from . import notebook
        notebook.initialize(h)
        handler.send_download(notebook.markdown(h).encode("utf-8"), "gsb-local-notebook.md", "text/markdown; charset=utf-8")
        return
    draft = re.fullmatch(r"/api/pairwise/drafts/([a-f0-9]{12})", path)
    if draft:
        from .drafts import get as get_draft
        handler.send_json(get_draft(h, draft[1]))
        return
    if path == "/api/pairwise":
        result = tasks_page(h, query)
        handler.send_json({**result,
                           "task_types": m.TASK_TYPES,
                           "default_directory": str(h.resolve_project_directory("zzzz-gsb")[1]),
                           "project_directories": h.available_project_directories(),
                           "runtime": runtime(h)})
        return
    if path == "/api/pairwise/exports":
        result = solo.exports_page(h, query)
        result["runtime"] = runtime(h)
        handler.send_json(result)
        return
    solo_payload = re.fullmatch(r"/api/pairwise/solo/([a-f0-9]{12})/payload", path)
    if solo_payload:
        handler.send_json(solo.bundle(h, solo_payload[1]))
        return
    solo_trajectory = re.fullmatch(
        r"/api/pairwise/solo/([a-f0-9]{12})/trajectory/(A|B)", path
    )
    if solo_trajectory:
        file, content_type = solo.trajectory_path(h, solo_trajectory[1], solo_trajectory[2])
        handler.send_file_download(file, content_type)
        return
    match = re.fullmatch(r"/api/pairwise/([a-f0-9]{12})", path)
    if match:
        handler.send_json(detail(h, match[1],
                                 include_git_changes=query.get("git_changes") == ["1"]))
        return
    trace = re.fullmatch(r"/api/pairwise/attempts/([a-f0-9]{12})/trace", path)
    if trace:
        a = m.attempt(h, trace[1])
        file = Path(a["trace_path"] or h.run_row(a["id"])["trajectory_path"] or "")
        if not file.is_file():
            raise h.WorkflowError("原始轨迹尚未归档")
        handler.send_file_download(file, "application/x-ndjson")
        return
    recording_file = re.fullmatch(r"/api/pairwise/attempts/([a-f0-9]{12})/recording-file", path)
    if recording_file:
        file = recording.file_path(h, recording_file[1])
        content_type = {
            ".m4v": "video/mp4",
            ".mp4": "video/mp4",
            ".mov": "video/quicktime",
            ".webm": "video/webm",
        }.get(file.suffix.lower(), "application/octet-stream")
        handler.send_file_inline(file, content_type)
        return
    raise h.WorkflowError("Pair-wise 接口不存在")


def post(handler, h, path, payload):
    if path == "/api/pairwise/start-batch":
        handler.send_json(workflow.start_batch(h, payload.get("ids")))
        return
    if path == "/api/pairwise/zero-one-batches/bulk-add":
        from . import zero_one_lab
        handler.send_json(zero_one_lab.add_candidates(
            h, payload.get("items"), queue=payload.get("queue") is True))
        return
    if path == "/api/pairwise/bug-batches":
        if deployment.is_draining(h):
            raise h.WorkflowError("服务正在交接更新，暂不生成题面")
        from . import bug_lab
        handler.send_json(bug_lab.create(h, payload), 202)
        return
    bug_continue = re.fullmatch(r"/api/pairwise/bug-batches/([a-f0-9]{12})/continue", path)
    if bug_continue:
        if deployment.is_draining(h):
            raise h.WorkflowError("服务正在交接更新，暂不处理 Bug 候选题")
        from . import bug_lab
        handler.send_json(bug_lab.continue_batch(h, bug_continue.group(1)), 202)
        return
    bug_pause = re.fullmatch(r"/api/pairwise/bug-batches/([a-f0-9]{12})/pause", path)
    if bug_pause:
        from . import bug_lab
        handler.send_json(bug_lab.pause_batch(h, bug_pause.group(1)))
        return
    bug_candidate = re.fullmatch(
        r"/api/pairwise/bug-batches/([a-f0-9]{12})/candidates/(\d+)/(add|queue|retry|reject|delete|skip)", path)
    if bug_candidate:
        if deployment.is_draining(h):
            raise h.WorkflowError("服务正在交接更新，暂不处理 Bug 候选题")
        from . import bug_lab
        batch_id, slot, operation = bug_candidate.groups()
        handler.send_json(bug_lab.operate(h, batch_id, int(slot), operation,
                                          confirmed=payload.get("confirmed") is True))
        return
    if path == "/api/pairwise/iteration-batches/bulk-retry":
        if deployment.is_draining(h):
            raise h.WorkflowError("服务正在交接更新，暂不生成题面")
        from . import iteration_lab
        handler.send_json(iteration_lab.retry_candidates(h, payload.get("items")), 202)
        return
    if path == "/api/pairwise/iteration-batches":
        if deployment.is_draining(h):
            raise h.WorkflowError("服务正在交接更新，暂不启动新的题面生成")
        from . import iteration_lab
        handler.send_json(iteration_lab.create(h, payload), 202)
        return
    iteration_candidate = re.fullmatch(
        r"/api/pairwise/iteration-batches/([a-f0-9]{12})/candidates/(\d+)/(add|queue|retry)", path
    )
    if iteration_candidate:
        if deployment.is_draining(h):
            raise h.WorkflowError("服务正在交接更新，暂不生成题面或创建任务")
        from . import iteration_lab
        batch_id, slot, operation = iteration_candidate.groups()
        if operation == "retry":
            handler.send_json(iteration_lab.retry_candidate(h, batch_id, int(slot)), 202)
            return
        handler.send_json(iteration_lab.add_candidate(
            h, batch_id, int(slot), queue=operation == "queue",
            confirmed=payload.get("confirmed") is True,
        ))
        return
    if path == "/api/pairwise/zero-one-batches":
        if deployment.is_draining(h):
            raise h.WorkflowError("服务正在交接更新，暂不启动新的题面生成")
        from . import zero_one_lab
        handler.send_json(zero_one_lab.create(h, payload), 202)
        return
    zero_one_candidate = re.fullmatch(
        r"/api/pairwise/zero-one-batches/([a-f0-9]{12})/candidates/(\d+)/(add|queue)", path
    )
    if zero_one_candidate:
        from . import zero_one_lab
        batch_id, slot, operation = zero_one_candidate.groups()
        handler.send_json(zero_one_lab.add_candidate(
            h, batch_id, int(slot), queue=operation == "queue",
        ))
        return
    if path == "/api/pairwise/deployment":
        handler.send_json(deployment.configure(h, payload.get("draining")))
        return
    if path == "/api/pairwise/candidate-bank/import-legacy":
        from . import candidate_bank
        handler.send_json(candidate_bank.import_legacy(h))
        return
    legacy = re.fullmatch(r"/api/pairwise/candidate-bank/legacy/([a-f0-9]{12})/prioritize", path)
    if legacy:
        from . import candidate_bank
        candidate_bank.prioritize_legacy(h, legacy[1])
        handler.send_json({"ok": True})
        return
    candidate = re.fullmatch(r"/api/pairwise/candidate-bank/([a-f0-9]{12})/reject", path)
    if candidate:
        from . import candidate_bank
        candidate_bank.reject(h, candidate[1], str(payload.get("reason") or ""))
        handler.send_json({"ok": True})
        return
    entry = re.fullmatch(r"/api/pairwise/notebook/([a-f0-9]{12})", path)
    if entry:
        from . import notebook
        handler.send_json(notebook.annotate(h, entry[1], payload))
        return
    if path == "/api/pairwise/max-parallel":
        value = payload.get("max_parallel")
        if isinstance(value, bool) or not isinstance(value, int) or not 2 <= value <= 7:
            raise h.WorkflowError("Pair-wise 并行上限必须是 2 至 7 的整数")
        handler.send_json(h.set_max_parallel_runs({"max_parallel": value}))
        return
    if path == "/api/pairwise/model-defaults":
        handler.send_json(m.set_model_defaults(h, payload.get("A"), payload.get("B")))
        return
    if path == "/api/pairwise/drafts":
        if deployment.is_draining(h):
            raise h.WorkflowError("服务正在交接更新，暂不启动新的题面生成")
        from .drafts import create as create_draft
        handler.send_json(create_draft(h), 202)
        return
    if path == "/api/pairwise":
        from .history import append as append_history
        pair = m.create(h, payload, enforce_prompt_dedup=True)
        append_history(h, pair)
        handler.send_json(detail(h, pair["id"]), 201)
        return
    if path == "/api/pairwise/export":
        content = export.workbook(h, payload.get("ids"))
        handler.send_download(content, "pairwise-gsb.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        return
    if path == "/api/pairwise/solo/state":
        handler.send_json(solo.record_state(h, payload))
        return
    if path == "/api/pairwise/solo/sync":
        handler.send_json(solo.sync(h, payload))
        return
    model_policy = re.fullmatch(r"/api/pairwise/([a-f0-9]{12})/model-policy", path)
    if model_policy:
        with workflow.LOCK:
            m.set_b_execution_model(h, model_policy[1],
                                    str(payload.get("b_execution_model") or ""),
                                    payload.get("revision"))
        handler.send_json(detail(h, model_policy[1]))
        return
    match = re.fullmatch(r"/api/pairwise/([a-f0-9]{12})/(start|rerun|review|preflight|delete|difficulty-review|recording)", path)
    if match:
        pair_id, action = match.groups()
        if action == "start":
            if m.task(h, pair_id).get("deleted_at"):
                raise h.WorkflowError("该任务已从列表删除，不能再启动")
            workflow.start(h, pair_id, payload.get("side"))
        elif action == "rerun":
            if deployment.is_draining(h):
                raise h.WorkflowError("服务正在交接更新，暂不启动单侧重跑")
            workflow.rerun_side(h, pair_id, payload.get("side"))
        elif action == "review":
            m.save_review(h, pair_id, payload)
        elif action == "delete":
            m.soft_delete(h, pair_id)
            handler.send_json({"ok": True, "id": pair_id})
            return
        elif action == "difficulty-review":
            if deployment.is_draining(h):
                raise h.WorkflowError("服务正在交接更新，暂不启动难度复评")
            from .difficulty import enqueue_if_ready
            if not enqueue_if_ready(h, pair_id, force=True):
                raise h.WorkflowError("请等待 A/B 完成归档；已有难度复评运行时不能重复启动")
        elif action == "recording":
            recording.enqueue_pair_if_ready(h, pair_id, force=True, side=payload.get("side"))
        else:
            handler.send_json(validation.preflight(h, pair_id))
            return
        handler.send_json(detail(h, pair_id))
        return
    match = re.fullmatch(r"/api/pairwise/attempts/([a-f0-9]{12})/(evidence|stop|archive|recover|retry-unconfirmed|recording)", path)
    if match:
        run_id, action = match.groups()
        if action == "recording":
            attempt = recording.enqueue(h, run_id, force=True)
            handler.send_json(detail(h, attempt["pair_id"]))
            return
        if action == "evidence":
            a = validation.save_evidence(h, run_id, payload)
        elif action == "archive":
            a = workflow.retry_archive(h, run_id)
        elif action == "recover":
            a = workflow.request_recovery(h, run_id)
        elif action == "retry-unconfirmed":
            a = workflow.retry_unconfirmed(h, run_id)
        else:
            a = workflow.stop(h, run_id)
        handler.send_json(detail(h, a["pair_id"]))
        return
    raise h.WorkflowError("Pair-wise 接口不存在")
