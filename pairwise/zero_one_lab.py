"""Human-reviewed 0-1 prompt batches for the bank page.

Generation never creates a Pair. Each candidate is persisted before the next
slot starts, so a failed model call does not discard approved earlier work.
"""
import difflib
import json
import threading
import uuid

from . import candidate_bank, deployment, generation_policy, history, models, workflow


GUIDANCE = """只生成 0-1 代码生成题。目标是人类执行视角恰好达到困难下沿：
需要真实设计取舍和可验证的异常边界，但不要靠叠加第二套系统提高难度。
Claude 预计手写业务代码不超过 2500 行；这是内部预算，绝不能写进 User Prompt。
题面要有独特的业务对象、动作顺序和最终可观察结果，避免同类题只换名词。
只与本地 Pair-wise GSB 自有题库查重；已删除的历史题面仍须避让。
避免空仓库、框架清单、Docker Compose 的固定开场套句，尤其避开已知 A-9 中英混排骨架。
不要生成历史代码、评分、GSB 理由，也不要自动创建任务或启动 A/B。"""

CYCLE = ("纯后端", "纯后端", "纯后端", "纯前端", "纯前端", "全栈")
PAGE_SIZE = 20


def _candidate_rows(db, batch_id):
    return db.execute(
        "SELECT candidate.*,task.display_number AS actual_number "
        "FROM pairwise_zero_one_candidates candidate "
        "LEFT JOIN pairwise_tasks task ON task.id=candidate.pair_id "
        "WHERE candidate.batch_id=? ORDER BY candidate.slot",
        (batch_id,),
    ).fetchall()


def get(h, batch_id):
    models.initialize(h)
    with h.db_connection() as db:
        batch = db.execute(
            "SELECT * FROM pairwise_zero_one_batches WHERE id=?", (batch_id,)
        ).fetchone()
        if not batch:
            raise h.WorkflowError("出题批次不存在")
        rows = _candidate_rows(db, batch_id)
    result = dict(batch)
    result["candidates"] = [
        {**dict(row), "draft": json.loads(row["draft"] or "{}"),
         "suggested_number": f"{batch['suggested_start'] + row['slot'] - 1:04d}"}
        for row in rows
    ]
    return result


def recent(h):
    models.initialize(h)
    with h.db_connection() as db:
        ids = [row[0] for row in db.execute(
            "SELECT id FROM pairwise_zero_one_batches ORDER BY created_at DESC, rowid DESC LIMIT 8"
        ).fetchall()]
    return {"items": [get(h, batch_id) for batch_id in ids]}


def listing(h, query):
    """Page candidate rows in SQLite, across every batch, newest batch first."""
    models.initialize(h)
    try:
        requested_page = int(query.get("page", ["1"])[0])
    except (TypeError, ValueError):
        raise h.WorkflowError("页码必须是正整数")
    if requested_page < 1:
        raise h.WorkflowError("页码必须是正整数")
    with h.db_connection() as db:
        db.execute("BEGIN")
        total = db.execute("SELECT COUNT(*) FROM pairwise_zero_one_candidates").fetchone()[0]
        active_batches = db.execute(
            "SELECT COUNT(*) FROM pairwise_zero_one_batches WHERE status IN ('queued','running')"
        ).fetchone()[0]
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(requested_page, pages)
        rows = db.execute(
            "SELECT candidate.*,batch.status AS batch_status,batch.detail AS batch_detail,"
            "batch.count AS batch_count,batch.suggested_start,batch.created_at AS batch_created_at,"
            "task.display_number AS actual_number "
            "FROM pairwise_zero_one_candidates candidate "
            "JOIN pairwise_zero_one_batches batch ON batch.id=candidate.batch_id "
            "LEFT JOIN pairwise_tasks task ON task.id=candidate.pair_id "
            "ORDER BY batch.created_at DESC,batch.rowid DESC,candidate.slot ASC "
            "LIMIT ? OFFSET ?",
            (PAGE_SIZE, (page - 1) * PAGE_SIZE),
        ).fetchall()
    items = [
        {**dict(row), "draft": json.loads(row["draft"] or "{}"),
         "suggested_number": f"{row['suggested_start'] + row['slot'] - 1:04d}"}
        for row in rows
    ]
    return {"items": items, "total": total, "page": page, "pages": pages,
            "page_size": PAGE_SIZE, "active_batches": active_batches}


def _update(h, batch_id, *, status=None, detail=None, slot=None, slot_status=None,
            draft=None, error=None):
    with h.db_connection() as db:
        if status is not None or detail is not None:
            fields, values = [], []
            if status is not None:
                fields.append("status=?")
                values.append(status)
            if detail is not None:
                fields.append("detail=?")
                values.append(detail)
            fields.append("updated_at=?")
            values.append(h.now_text())
            db.execute(
                "UPDATE pairwise_zero_one_batches SET " + ",".join(fields) + " WHERE id=?",
                (*values, batch_id),
            )
        if slot is not None:
            fields, values = [], []
            if slot_status is not None:
                fields.append("status=?")
                values.append(slot_status)
            if draft is not None:
                fields.append("draft=?")
                values.append(json.dumps(draft, ensure_ascii=False))
            if error is not None:
                fields.append("error=?")
                values.append(error)
            if fields:
                db.execute(
                    "UPDATE pairwise_zero_one_candidates SET " + ",".join(fields)
                    + " WHERE batch_id=? AND slot=?", (*values, batch_id, slot),
                )


def _generate(h, batch_id):
    batch = get(h, batch_id)
    known = history.records(h, limit=10000)
    failures = 0
    _update(h, batch_id, status="running", detail="正在准备第一道题")
    for item in batch["candidates"]:
        slot, category = item["slot"], item["category"]
        label = f"第 {slot}/{batch['count']} 道 · {category}"
        _update(h, batch_id, detail=label + "：正在生成", slot=slot,
                slot_status="generating")

        def progress(message, current=label, current_slot=slot):
            _update(h, batch_id, detail=current + "：" + str(message)[:300],
                    slot=current_slot)

        try:
            draft = h.generate_task_draft(
                project_number=batch["suggested_start"] + slot - 1,
                category_override=category,
                base_policy=GUIDANCE,
                history_override=known,
                progress=progress,
            )
            prompt = str(draft.get("first_prompt") or "")
            if draft.get("category") != category:
                raise h.WorkflowError("生成题型与本批比例不一致")
            if draft.get("task_difficulty") != "困难":
                raise h.WorkflowError("独立预审未确认达到困难")
            lines = draft.get("estimated_handwritten_lines")
            if isinstance(lines, bool) or not isinstance(lines, int) or not 1 <= lines <= 2500:
                raise h.WorkflowError("预计业务代码超出 2500 行预算")
            duplicate = candidate_bank.overlap(h, prompt)
            if duplicate:
                raise h.WorkflowError(duplicate)
            for previous in known:
                old = str(previous.get("prompt") or "")
                if old and difflib.SequenceMatcher(None, prompt, old).ratio() >= 0.82:
                    raise h.WorkflowError("与本批或历史题面过于相似")
            _update(h, batch_id, detail=label + "：已通过，继续下一道",
                    slot=slot, slot_status="ready", draft=draft, error="")
            known.insert(0, h.history_record(
                str(draft.get("repo_name") or ""), "0-1 代码生成", prompt,
                {"summary": h.summarize_historical_prompt(prompt),
                 "project_category": category},
            ))
        except Exception as exc:
            failures += 1
            _update(h, batch_id, detail=label + "：未生成合规题，继续其他题",
                    slot=slot, slot_status="failed", error=str(exc)[:2000])
    _update(h, batch_id, status="partial" if failures else "complete",
            detail=(f"已生成 {batch['count'] - failures}/{batch['count']} 道；"
                    "失败题未自动补造" if failures else f"{batch['count']} 道已生成，等待人工选择"))


def create(h, payload):
    models.initialize(h)
    count = payload.get("count", 12)
    if isinstance(count, bool) or count not in (6, 12):
        raise h.WorkflowError("每批只能生成 6 或 12 道，类别比例固定为 3:2:1")
    with h.db_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        active = db.execute(
            "SELECT id FROM pairwise_zero_one_batches WHERE status IN ('queued','running') LIMIT 1"
        ).fetchone()
        batch_id = active[0] if active else uuid.uuid4().hex[:12]
        if not active:
            maximum = db.execute(
                "SELECT COALESCE(MAX(project_number),0) FROM pairwise_tasks "
                "WHERE project_number BETWEEN 1 AND 2999"
            ).fetchone()[0]
            start = int(maximum) + 1
            if start + count - 1 > 2999:
                raise h.WorkflowError("0-1 项目编号空间不足")
            now = h.now_text()
            db.execute(
                "INSERT INTO pairwise_zero_one_batches "
                "(id,status,detail,count,suggested_start,created_at,updated_at) "
                "VALUES (?,'queued','等待生成',?,?,?,?)",
                (batch_id, count, start, now, now),
            )
            db.executemany(
                "INSERT INTO pairwise_zero_one_candidates(batch_id,slot,category) VALUES (?,?,?)",
                [(batch_id, slot, CYCLE[(slot - 1) % 6]) for slot in range(1, count + 1)],
            )
    if active:
        return get(h, batch_id)
    thread = threading.Thread(target=_generate, args=(h, batch_id),
                              daemon=True, name=f"zero-one-bank-{batch_id}")
    thread.start()
    return get(h, batch_id)


def add_candidate(h, batch_id, slot, *, queue=False):
    batch = get(h, batch_id)
    item = next((row for row in batch["candidates"] if row["slot"] == slot), None)
    if not item or item["status"] not in ("ready", "added", "queued"):
        raise h.WorkflowError("这道题尚未生成合规题面")
    draft = item["draft"]
    pair_id = item["pair_id"]
    if not pair_id:
        payload = {
            "title": draft["project_name"], "prompt": draft["first_prompt"],
            "task_type": "0-1 代码生成", "difficulty": "困难",
            "language_framework": draft["language_framework"],
            "project_category": item["category"],
            "reproducibility": "已容器化，可一键起环境",
            "topic_confirmed": True, "snapshot_url": "",
            "repo_name": draft["repo_name"], "project_directory": "zzzz-gsb",
            "verification_commands": draft["verification_commands"],
            "difficulty_preflight": {
                "review": draft.get("difficulty_review"),
                "analysis": draft.get("complexity_analysis"),
                "estimated_minutes": draft.get("estimated_minutes"),
                "estimated_handwritten_lines": draft.get("estimated_handwritten_lines"),
                "scope_label": "困难偏低",
                "generation_policy_version": generation_policy.VERSION,
                "handwritten_line_limit": 2500,
            },
        }
        from . import history as prompt_history
        try:
            pair = models.create(h, payload, enforce_prompt_dedup=True)
        except h.WorkflowError:
            with h.db_connection() as db:
                existing = db.execute(
                    "SELECT id,prompt FROM pairwise_tasks WHERE repo_name=?",
                    (draft["repo_name"],),
                ).fetchone()
            if not existing or existing["prompt"] != draft["first_prompt"]:
                raise
            pair = models.task(h, existing["id"])
        pair_id = pair["id"]
        prompt_history.append(h, pair)
        with h.db_connection() as db:
            db.execute(
                "UPDATE pairwise_zero_one_candidates SET pair_id=?,status='added' "
                "WHERE batch_id=? AND slot=?", (pair_id, batch_id, slot),
            )
    pair = models.task(h, pair_id)
    if queue:
        if pair.get("deleted_at"):
            raise h.WorkflowError("此题已从任务列表移除，不能重新排队")
        with h.db_connection() as db:
            attempts = db.execute(
                "SELECT COUNT(*) FROM pairwise_attempts WHERE pair_id=?", (pair_id,)
            ).fetchone()[0]
        if not attempts:
            workflow.start(h, pair_id, None)
        with h.db_connection() as db:
            db.execute(
                "UPDATE pairwise_zero_one_candidates SET status='queued' "
                "WHERE batch_id=? AND slot=?", (batch_id, slot),
            )
    return get(h, batch_id)
