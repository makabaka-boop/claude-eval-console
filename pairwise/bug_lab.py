"""Preview Bug repair questions from isolated, optionally fault-seeded copies.

The bank never edits an archived project. A generated candidate keeps its
private seed bundle server-side until an operator creates a new root Pair.
"""

import difflib
import json
import re
import threading
import time
import uuid
from pathlib import Path

from . import bug_generation_v2, candidate_bank, deployment, history, models, workflow


PAGE_SIZE = 10
RESERVE_TARGET = 5
LEGACY_RESERVE_TARGET = 6
SOURCE_DIRECTORY = "zzzz-gsb"
POLICY_VERSION = 2
BUG_BANK_MODEL = "gpt-6-sol"
BUG_BANK_REASONING_EFFORT = "high"
SHA = re.compile(r"[0-9a-f]{40}\Z")
ACTIVE = ("queued", "running")
_BATCH_LOCKS = {}
_BATCH_LOCKS_GUARD = threading.Lock()
SOURCE_GUIDANCE = (
    "这是人工待选 Bug 题面库，仅从 zzzz-gsb 内的已归档项目选源。"
    "允许且只允许在独立 Git 副本中预置缺陷；同一项目选择两个或三个可区分的真实问题，"
    "尽量牵动多个生产文件，但须围绕一条相关用户流程。"
    "参考 Claude 既往交付表现估计完整修复与必要回归，选择困难下沿且独立盲审恰好评为困难的题；"
    "预计改动至少 200 行、最好不超过 800 行。题面以具体场景开头，只用一段话总结问题，"
    "不写根因、修法、内部难度规则或行数。对照历史具体故障及题目骨架，"
    "不能只换项目名、输入值或表述就重复已有问题组合。"
    "避免需要多天验证、高并发或纯性能压力的设计，也不在最终题面提及这些限制。"
)


class _BankModelHost:
    """Route every model stage in a bank generation to its own model settings."""

    ITERATION_GENERATION_MODEL = BUG_BANK_MODEL
    REVIEW_MODEL = BUG_BANK_MODEL
    CODEX_REASONING_EFFORT = BUG_BANK_REASONING_EFFORT

    def __init__(self, host):
        self._host = host

    def __getattr__(self, name):
        return getattr(self._host, name)

    def run_codex_structured(self, *args, **kwargs):
        kwargs["model"] = BUG_BANK_MODEL
        kwargs["reasoning_effort"] = BUG_BANK_REASONING_EFFORT
        return self._host.run_codex_structured(*args, **kwargs)


def _json(value, fallback):
    try:
        return json.loads(value) if value else fallback
    except (TypeError, ValueError):
        return fallback


def _source_summary(item):
    return {key: item.get(key) for key in (
        "root_number", "pair_id", "display_number", "repo_name", "attempt_id",
        "side", "sha", "tree", "repo_path", "project_directory",
        "project_category", "language_framework", "reproducibility",
        "verification_commands",
    )}


def available_sources(h, start=0, end=0, directory=""):
    """Choose archived roots from highest number down, trying B after A fails."""
    models.initialize(h)
    with h.db_connection() as db:
        roots = db.execute("""SELECT id,project_number,display_number,repo_name,
            project_directory,project_category,language_framework,reproducibility,
            verification_commands FROM pairwise_tasks
            WHERE source_pair_id='' AND task_type='0-1 代码生成'
              AND (?=0 OR project_number>=?)
              AND (?=0 OR project_number<=?)
              AND (?='' OR project_directory=?)
            ORDER BY project_number DESC,sequence_number""",
            (start, start, end, end, directory, directory)).fetchall()
        history_rows = db.execute("SELECT source,status FROM pairwise_bug_candidates").fetchall()
    tried = set()
    accepted_roots = set()
    for item in history_rows:
        prior = _json(item["source"], {})
        pair_id = str(prior.get("pair_id") or "")
        if pair_id:
            side = str(prior.get("side") or "")
            if side in ("A", "B"):
                tried.add((pair_id, side))
            if side not in ("A", "B") or item["status"] in ("ready", "added", "queued"):
                accepted_roots.add(pair_id)
    result = []
    for root in roots:
        if root["id"] in accepted_roots:
            continue
        attempts = [a for a in models.attempts(h, root["id"])
                    if a["status"] == "complete" and SHA.fullmatch(str(a["artifact_sha"] or ""))
                    and SHA.fullmatch(str(a["artifact_tree"] or ""))
                    and Path(str(a["repo_path"] or "")).is_dir()]
        attempts.sort(key=lambda a: (a["side"] != "A", -int(a["number"])))
        selected = next((a for a in attempts if (root["id"], a["side"]) not in tried), None)
        if selected is None:
            continue
        result.append({
            "root_number": int(root["project_number"]), "pair_id": root["id"],
            "display_number": root["display_number"], "repo_name": root["repo_name"],
            "attempt_id": selected["id"], "side": selected["side"],
            "sha": selected["artifact_sha"], "tree": selected["artifact_tree"],
            "repo_path": selected["repo_path"],
            "project_directory": root["project_directory"],
            "project_category": root["project_category"],
            "language_framework": root["language_framework"],
            "reproducibility": root["reproducibility"],
            "verification_commands": _json(root["verification_commands"], []),
        })
    return result


def _current(h, source):
    pair = models.task(h, source["pair_id"])
    attempt = models.attempt(h, source["attempt_id"])
    if (pair["display_number"] != source["display_number"]
            or attempt["pair_id"] != pair["id"] or attempt["status"] != "complete"
            or attempt["artifact_sha"] != source["sha"]
            or attempt["artifact_tree"] != source["tree"]):
        raise h.WorkflowError("Bug 来源快照已变化，请重新生成")
    work = Path(str(attempt["repo_path"] or ""))
    if not work.is_dir() or not (work / ".git").exists():
        raise h.WorkflowError("Bug 来源归档工作区不存在")
    tree = h.run_command(["git", "rev-parse", source["sha"] + "^{tree}"], cwd=work).stdout.strip()
    if tree != source["tree"]:
        raise h.WorkflowError("Bug 来源 Git tree 与归档记录不一致")
    return pair, attempt, work


def _generation_context(h, source, pair, attempt, work):
    with h.db_connection() as db:
        family = [dict(row) for row in db.execute("""SELECT id,task_type,prompt
            FROM pairwise_tasks WHERE project_number=? ORDER BY sequence_number""",
            (source["root_number"],))]
        previews = [(row[0], row[1], _json(row[2], {}).get("prompt", ""))
                    for row in db.execute("""SELECT batch_id,slot,draft
            FROM pairwise_bug_candidates WHERE status IN ('ready','added','queued')""")]
        claude_outcomes = [dict(row) for row in db.execute("""SELECT display_number,title,
            a_delivery_score,b_delivery_score FROM pairwise_tasks
            WHERE task_type='Bug 修复' AND deleted_at IS NULL
              AND (a_delivery_score>0 OR b_delivery_score>0)
            ORDER BY created_at DESC LIMIT 8""")]
    seed_history = [{"reference_id": row["id"], "task_type": row["task_type"],
                     "prompt": row["prompt"]} for row in family]
    previous = history.records(h, limit=80)
    # The design and authenticity reviewers consume only the first dozen
    # records, so put already accepted Bug directions ahead of unrelated work.
    gsb_history = [
        {"reference_id": f"bug-bank:{batch_id}:{slot}", "task_type": "Bug 修复",
         "prompt": prompt, "summary": prompt}
        for batch_id, slot, prompt in previews if prompt
    ][-6:]
    gsb_history.extend(row for row in previous if row.get("task_type") == "Bug 修复")
    gsb_history.extend(row for row in previous if row.get("task_type") != "Bug 修复")
    generator_source = {
        "pair": pair, "attempt": attempt, "seed_history": seed_history,
        "same_repository_history": seed_history, "gsb_history": gsb_history,
        "past_claude_outcomes": claude_outcomes,
        "generation_feedback": SOURCE_GUIDANCE,
        "bug_verification_mode": "static_first",
    }
    context = {
        "repo_name": pair["repo_name"],
        "project_category": pair["project_category"],
        "language_framework": pair["language_framework"],
        "original_or_current_prompt": pair["prompt"],
        "same_repository_history": seed_history,
        "gsb_history": gsb_history,
        "past_claude_outcomes": claude_outcomes,
        "repo_path": str(work),
        "current_commit": source["sha"],
        "_bank_guidance": True,
    }
    return generator_source, context


def _private(batch_id, slot, row):
    if not row:
        raise ValueError("candidate missing")
    return {**dict(row), "batch_id": batch_id, "slot": slot,
            "source": _json(row["source"], {}), "draft": _json(row["draft"], {})}


def _candidate(h, batch_id, slot):
    with h.db_connection() as db:
        row = db.execute("SELECT * FROM pairwise_bug_candidates WHERE batch_id=? AND slot=?",
                         (batch_id, slot)).fetchone()
    if not row:
        raise h.WorkflowError("Bug 候选题不存在")
    return _private(batch_id, slot, row)


def _stock_count(db):
    """Count questions that are still available to be assigned to a Pair."""
    return db.execute("""SELECT COUNT(*) FROM pairwise_bug_candidates
        WHERE status='ready' AND pair_id=''""").fetchone()[0]


def _batch_lock(batch_id):
    with _BATCH_LOCKS_GUARD:
        return _BATCH_LOCKS.setdefault(batch_id, threading.Lock())


def _job_key(batch_id):
    return f"bug-bank:{batch_id}"


def listing(h, query):
    """Return a safe projection of a SQLite LIMIT/OFFSET page."""
    models.initialize(h)
    try:
        requested = int(query.get("page", ["1"])[0])
    except (TypeError, ValueError):
        raise h.WorkflowError("页码必须是正整数")
    if requested < 1:
        raise h.WorkflowError("页码必须是正整数")
    with h.db_connection() as db:
        db.execute("BEGIN")
        total = db.execute("SELECT COUNT(*) FROM pairwise_bug_candidates WHERE status!='deleted'").fetchone()[0]
        stock = _stock_count(db)
        active = db.execute("SELECT COUNT(*) FROM pairwise_bug_batches WHERE status IN ('queued','running')").fetchone()[0]
        latest = db.execute("""SELECT batch.id,batch.status,batch.detail,batch.count,
            SUM(CASE WHEN candidate.status='ready' AND candidate.pair_id='' THEN 1 ELSE 0 END) AS passed,
            SUM(CASE WHEN candidate.status='deleted' THEN 1 ELSE 0 END) AS skipped
            FROM pairwise_bug_batches batch
            LEFT JOIN pairwise_bug_candidates candidate ON candidate.batch_id=batch.id
            GROUP BY batch.id ORDER BY batch.created_at DESC,batch.rowid DESC LIMIT 1""").fetchone()
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(requested, pages)
        rows = db.execute("""SELECT candidate.*,batch.status AS batch_status,
            batch.detail AS batch_detail,batch.created_at AS batch_created_at,
            task.display_number AS actual_number
            FROM pairwise_bug_candidates candidate
            JOIN pairwise_bug_batches batch ON batch.id=candidate.batch_id
            LEFT JOIN pairwise_tasks task ON task.id=candidate.pair_id
            WHERE candidate.status!='deleted'
            ORDER BY batch.created_at DESC,batch.rowid DESC,
              CAST(json_extract(candidate.source,'$.root_number') AS INTEGER) DESC,
              candidate.slot
            LIMIT ? OFFSET ?""", (PAGE_SIZE, (page - 1) * PAGE_SIZE)).fetchall()
    items = []
    for row in rows:
        source, draft = _json(row["source"], {}), _json(row["draft"], {})
        items.append({
            "batch_id": row["batch_id"], "slot": row["slot"],
            "status": row["status"],
            "error": row["error"] if row["status"] in ("failed", "rejected") else "",
            "pair_id": row["pair_id"], "actual_number": row["actual_number"],
            "batch_status": row["batch_status"],
            "batch_detail": row["batch_detail"] if row["status"] == "generating" else "",
            "source": {key: source.get(key) for key in
                       ("root_number", "display_number", "repo_name", "side", "sha")},
            "draft": {key: draft.get(key) for key in
                      ("title", "prompt", "estimated_lines", "task_difficulty")},
        })
    return {"items": items, "total": total, "page": page, "pages": pages,
            "page_size": PAGE_SIZE, "active_batches": active,
            "inventory_ready": stock, "inventory_target": RESERVE_TARGET,
            "latest_batch": ({"id": latest["id"], "status": latest["status"],
                              "detail": latest["detail"], "target": latest["count"],
                              "passed": latest["passed"], "skipped": latest["skipped"]}
                             if latest else None),
            "generation_model": BUG_BANK_MODEL,
            "review_model": BUG_BANK_MODEL,
            "reasoning_effort": BUG_BANK_REASONING_EFFORT}


def _update(h, batch_id, *, status=None, detail=None, slot=None,
            slot_status=None, draft=None, error=None):
    with h.db_connection() as db:
        if status is not None or detail is not None:
            fields, values = ["updated_at=?"], [h.now_text()]
            if status is not None:
                fields.append("status=?")
                values.append(status)
            if detail is not None:
                fields.append("detail=?")
                values.append(detail)
            db.execute("UPDATE pairwise_bug_batches SET " + ",".join(fields) + " WHERE id=?",
                       (*values, batch_id))
        if slot is not None:
            fields, values = [], []
            for key, value in (("status", slot_status),
                               ("draft", json.dumps(draft, ensure_ascii=False) if draft is not None else None),
                               ("error", error)):
                if value is not None:
                    fields.append(key + "=?")
                    values.append(value)
            if fields:
                db.execute("UPDATE pairwise_bug_candidates SET " + ",".join(fields)
                           + " WHERE batch_id=? AND slot=?", (*values, batch_id, slot))


def _generate(h, batch_id):
    """Serialize a batch and give its model calls a cancellable job identity."""
    with _batch_lock(batch_id):
        previous = getattr(h.CODEX_JOB_CONTEXT, "key", "")
        h.CODEX_JOB_CONTEXT.key = _job_key(batch_id)
        h.clear_job_cancellation(_job_key(batch_id))
        try:
            _generate_locked(h, batch_id)
        except h.JobCancelled:
            pass
        finally:
            h.CODEX_JOB_CONTEXT.key = previous


def _active_slot_update(h, batch_id, slot, old_status, new_status, detail,
                        *, draft=None, error=""):
    """Ignore a model result after a user has paused or skipped its source."""
    with h.db_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        fields = ["status=?", "error=?"]
        values = [new_status, error]
        if draft is not None:
            fields.append("draft=?")
            values.append(json.dumps(draft, ensure_ascii=False))
        changed = db.execute("UPDATE pairwise_bug_candidates SET " + ",".join(fields)
            + " WHERE batch_id=? AND slot=? AND status=? AND EXISTS ("
            "SELECT 1 FROM pairwise_bug_batches WHERE id=? AND status='running')",
            (*values, batch_id, slot, old_status, batch_id)).rowcount
        if changed:
            db.execute("UPDATE pairwise_bug_batches SET detail=?,updated_at=? WHERE id=?",
                       (detail, h.now_text(), batch_id))
    return bool(changed)


def _generate_locked(h, batch_id):
    with h.db_connection() as db:
        batch = db.execute("SELECT count,start_number,end_number FROM pairwise_bug_batches WHERE id=?",
                           (batch_id,)).fetchone()
    if not batch:
        return
    deployment.wait_until_accepting(h)
    with h.db_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        current = db.execute("SELECT status FROM pairwise_bug_batches WHERE id=?",
                             (batch_id,)).fetchone()
        if not current or current["status"] not in ACTIVE:
            return
        db.execute("UPDATE pairwise_bug_batches SET status='running',detail=?,updated_at=? WHERE id=?",
                   ("正在准备第一道 Bug 修复题", h.now_text(), batch_id))
    transient_retries = {}
    while True:
        with h.db_connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT count,status FROM pairwise_bug_batches WHERE id=?",
                                 (batch_id,)).fetchone()
            if not current or current["status"] != "running":
                return
            target = int(current["count"])
            passed = _stock_count(db)
            rows = db.execute("SELECT slot,source,status FROM pairwise_bug_candidates WHERE batch_id=? ORDER BY slot",
                              (batch_id,)).fetchall()
            skipped = sum(row["status"] == "deleted" for row in rows)
            if passed >= target:
                db.execute("DELETE FROM pairwise_bug_candidates WHERE batch_id=? AND status='pending'",
                           (batch_id,))
                db.execute("""UPDATE pairwise_bug_batches SET status='complete',detail=?,updated_at=?
                    WHERE id=?""", (f"可领用库存 {passed}/{target} 道；跳过 {skipped} 个未通过来源",
                                   h.now_text(), batch_id))
                return
        if h.job_is_cancelled(_job_key(batch_id)):
            # A skip can race with a model call that just completed. The
            # deleted slot is already protected by _active_slot_update.
            h.clear_job_cancellation(_job_key(batch_id))
        def priority(source):
            return (int(source.get("root_number") or 0), source.get("side") == "A")
        pending = [item for item in rows if item["status"] == "pending"]
        row = max(pending, key=lambda item: priority(_json(item["source"], {})), default=None)
        sources = available_sources(h, batch["start_number"], batch["end_number"])
        next_source = sources[0] if sources else None
        if next_source and (row is None or priority(next_source) > priority(_json(row["source"], {}))):
            slot = max((item["slot"] for item in rows), default=0) + 1
            with h.db_connection() as db:
                db.execute("INSERT INTO pairwise_bug_candidates(batch_id,slot,source) VALUES (?,?,?)",
                           (batch_id, slot, json.dumps(_source_summary(next_source), ensure_ascii=False)))
            continue
        if row is None:
            _update(h, batch_id, status="exhausted",
                    detail=f"可用来源已用尽；可领用库存 {passed}/{target} 道，跳过 {skipped} 个未通过来源")
            return
        slot, source = row["slot"], _json(row["source"], {})
        label = f"可领用库存 {passed}/{target} 道 · 来源 {source.get('root_number', '?')}"
        if not _active_slot_update(h, batch_id, slot, "pending", "generating",
                                   label + "：核对来源"):
            continue
        try:
            pair, attempt, work = _current(h, source)
            generator_source, context = _generation_context(h, source, pair, attempt, work)
            def progress(message, **_):
                with h.db_connection() as db:
                    db.execute("""UPDATE pairwise_bug_batches SET detail=?,updated_at=?
                        WHERE id=? AND status='running' AND EXISTS (
                            SELECT 1 FROM pairwise_bug_candidates
                            WHERE batch_id=? AND slot=? AND status='generating')""",
                            (label + "：" + str(message)[:220], h.now_text(),
                             batch_id, batch_id, slot))
            draft = bug_generation_v2.generate(
                _BankModelHost(h), generator_source, work, context, generator_source,
                source["sha"], source["tree"], progress)
            prompt = str(draft.get("prompt") or "")
            if draft.get("task_difficulty") != "困难":
                raise h.WorkflowError("独立复核未确认困难下沿")
            if type(draft.get("estimated_lines")) is not int or not 150 <= draft["estimated_lines"] <= 800:
                raise h.WorkflowError("预计改动不在 150 至 800 行范围内")
            overlap = candidate_bank.overlap(h, prompt)
            if overlap:
                raise h.WorkflowError(overlap)
            with h.db_connection() as db:
                other = [_json(item[0], {}).get("prompt", "") for item in db.execute("""SELECT draft
                    FROM pairwise_bug_candidates WHERE status IN ('ready','added','queued')""")]
            if any(old and difflib.SequenceMatcher(None, prompt, old).ratio() >= .80 for old in other):
                raise h.WorkflowError("与待选 Bug 题面高度相似")
            _active_slot_update(h, batch_id, slot, "generating", "ready",
                                label + "：已通过，继续下一道", draft=draft)
        except h.JobCancelled:
            with h.db_connection() as db:
                state = db.execute("""SELECT batch.status,candidate.status
                    FROM pairwise_bug_batches batch JOIN pairwise_bug_candidates candidate
                      ON candidate.batch_id=batch.id
                    WHERE batch.id=? AND candidate.slot=?""", (batch_id, slot)).fetchone()
            if state and state[0] == "running" and state[1] == "deleted":
                h.clear_job_cancellation(_job_key(batch_id))
                continue
            return
        except Exception as exc:
            message = str(exc)
            if isinstance(exc, h.UpstreamCapacityError) or "上游模型容量不足" in message:
                retries = transient_retries.get(slot, 0) + 1
                transient_retries[slot] = retries
                if not _active_slot_update(h, batch_id, slot, "generating", "pending",
                                           label + "：上游容量不足，稍后重试同一来源"):
                    continue
                time.sleep(min(60, 10 * retries))
                continue
            if "超时，已停止" in message and transient_retries.get(slot, 0) < 1:
                transient_retries[slot] = 1
                _active_slot_update(h, batch_id, slot, "generating", "pending",
                                    label + "：模型超时，重试同一来源")
                continue
            _active_slot_update(h, batch_id, slot, "generating", "deleted",
                                label + "：未通过，自动换下一个来源", error=message[:2000])


def continue_batch(h, batch_id):
    """Resume a legacy partial batch under the refill loop."""
    models.initialize(h)
    with h.db_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        active = db.execute("SELECT id FROM pairwise_bug_batches WHERE status IN ('queued','running') LIMIT 1").fetchone()
        if active:
            raise h.WorkflowError("已有 Bug 题面批次正在生成")
        batch = db.execute("SELECT count,status,policy_version FROM pairwise_bug_batches WHERE id=?",
                           (batch_id,)).fetchone()
        if not batch or batch["status"] not in ("partial", "exhausted", "paused"):
            raise h.WorkflowError("只能继续尚未补齐的 Bug 批次")
        passed = (_batch_accepted_count(db, batch_id) if batch["policy_version"] == POLICY_VERSION
                  else _stock_count(db, batch["policy_version"]))
        if passed >= batch["count"]:
            raise h.WorkflowError("该 Bug 批次已达到目标数量")
        db.execute("UPDATE pairwise_bug_candidates SET status='deleted' WHERE batch_id=? AND status='failed'",
                   (batch_id,))
        db.execute("""UPDATE pairwise_bug_candidates SET status='pending',error=''
            WHERE batch_id=? AND status='generating'""", (batch_id,))
        db.execute("""UPDATE pairwise_bug_batches SET status='queued',detail='等待自动补齐',updated_at=?
            WHERE id=?""", (h.now_text(), batch_id))
    threading.Thread(target=_generate, args=(h, batch_id), daemon=True,
                     name=f"bug-bank-continue-{batch_id}").start()
    return {"id": batch_id, "count": batch["count"], "status": "queued", "continued": True}


def pause_batch(h, batch_id):
    """Stop new work and cancel the model call for the current source."""
    models.initialize(h)
    with h.db_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT status FROM pairwise_bug_batches WHERE id=?",
                         (batch_id,)).fetchone()
        if not row or row["status"] not in ACTIVE:
            raise h.WorkflowError("只能暂停正在生成的 Bug 批次")
        db.execute("""UPDATE pairwise_bug_batches
            SET status='paused',detail='已暂停，可在页面继续补库',updated_at=? WHERE id=?""",
            (h.now_text(), batch_id))
        db.execute("""UPDATE pairwise_bug_candidates SET status='pending',error=''
            WHERE batch_id=? AND status='generating'""", (batch_id,))
    h.cancel_background_job(_job_key(batch_id))
    return {"id": batch_id, "status": "paused"}


def recover(h):
    """Restart an interrupted refill without revisiting accepted sources."""
    models.initialize(h)
    with h.db_connection() as db:
        rows = db.execute("SELECT id FROM pairwise_bug_batches WHERE status IN ('queued','running')").fetchall()
        for row in rows:
            db.execute("UPDATE pairwise_bug_candidates SET status='pending' WHERE batch_id=? AND status='generating'",
                       (row["id"],))
            db.execute("UPDATE pairwise_bug_batches SET status='queued',detail='服务恢复后继续生成' WHERE id=?",
                       (row["id"],))
    for row in rows:
        threading.Thread(target=_generate, args=(h, row["id"]), daemon=True,
                         name=f"bug-bank-recover-{row['id']}").start()


def create(h, payload):
    models.initialize(h)
    count = payload.get("count", RESERVE_TARGET)
    if type(count) is not int or count not in (1, 3, 5, 6, 10):
        raise h.WorkflowError("目标库存只能设为 1、3、5、6 或 10 道")
    if payload.get("allow_seed") is not True:
        raise h.WorkflowError("当前 Bug 题面库需要允许在独立副本中预置缺陷")
    try:
        start, end = int(payload.get("start_number") or 0), int(payload.get("end_number") or 0)
    except (TypeError, ValueError):
        raise h.WorkflowError("项目编号须为正整数")
    if start < 0 or end < 0 or start and end and start > end:
        raise h.WorkflowError("项目编号范围无效")
    with h.db_connection() as db:
        active = db.execute("SELECT id FROM pairwise_bug_batches WHERE status IN ('queued','running') LIMIT 1").fetchone()
        stock = _stock_count(db)
    if active:
        return {"id": active[0], "count": 0, "status": "running", "continued": True}
    if stock >= count:
        return {"id": "", "count": count, "status": "complete", "satisfied": True}
    selected = available_sources(h, start, end)[:count]
    if not selected:
        raise h.WorkflowError("该编号范围没有可用的已归档项目快照")
    batch_id = uuid.uuid4().hex[:12]
    with h.db_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        active = db.execute("SELECT id FROM pairwise_bug_batches WHERE status IN ('queued','running') LIMIT 1").fetchone()
        if active:
            return {"id": active[0], "count": 0, "status": "running", "continued": True}
        if _stock_count(db) >= count:
            return {"id": "", "count": count, "status": "complete", "satisfied": True}
        now = h.now_text()
        db.execute("""INSERT INTO pairwise_bug_batches
            (id,status,detail,count,start_number,end_number,created_at,updated_at)
            VALUES (?,'queued','等待生成',?,?,?,?,?)""",
            (batch_id, count, start, end, now, now))
        db.executemany("INSERT INTO pairwise_bug_candidates(batch_id,slot,source) VALUES (?,?,?)",
                       [(batch_id, index, json.dumps(_source_summary(item), ensure_ascii=False))
                        for index, item in enumerate(selected, 1)])
    threading.Thread(target=_generate, args=(h, batch_id), daemon=True,
                     name=f"bug-bank-{batch_id}").start()
    return {"id": batch_id, "count": count, "selected_sources": len(selected), "status": "queued"}


def _refill_reserve(h, batch_id):
    """Retain the old reserve behavior for batches created under policy 1."""
    with h.db_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        source_batch = db.execute("SELECT policy_version FROM pairwise_bug_batches WHERE id=?",
                                  (batch_id,)).fetchone()
        if not source_batch or source_batch["policy_version"] != 1:
            return
        if _stock_count(db, 1) >= LEGACY_RESERVE_TARGET:
            return
        active = db.execute("""SELECT 1 FROM pairwise_bug_batches
            WHERE status IN ('queued','running') LIMIT 1""").fetchone()
        if active:
            return
        batch = db.execute("""SELECT id FROM pairwise_bug_batches
            WHERE count>=? AND status='complete' AND policy_version=?
            ORDER BY created_at DESC,rowid DESC LIMIT 1""", (LEGACY_RESERVE_TARGET, 1)).fetchone()
        if not batch:
            return
        db.execute("""UPDATE pairwise_bug_batches
            SET status='queued',detail='库存不足，继续补齐',updated_at=? WHERE id=?""",
            (h.now_text(), batch["id"]))
    threading.Thread(target=_generate, args=(h, batch["id"]), daemon=True,
                     name=f"bug-bank-refill-{batch['id']}").start()


def _add(h, item, *, queue=False):
    if item["status"] not in {"ready", "added", "queued"}:
        raise h.WorkflowError("只能领用已通过审核的 Bug 候选题")
    source, draft = item["source"], item["draft"]
    pair_id = item["pair_id"]
    if not pair_id:
        _current(h, source)
        repo_name = f"{source['repo_name'][:58]}-bug-bank-{item['batch_id']}-{item['slot']}"
        with h.db_connection() as db:
            existing = db.execute("SELECT id,prompt,origin FROM pairwise_tasks WHERE repo_name=?",
                                  (repo_name,)).fetchone()
        if existing:
            if existing["prompt"] != draft["prompt"] or existing["origin"] != "bank":
                raise h.WorkflowError("拟用仓库名已被其他任务占用")
            pair_id = existing["id"]
        else:
            payload = {
                "title": draft.get("focus_area") or f"{source['root_number']:04d} Bug 修复",
                "prompt": draft["prompt"], "task_type": "Bug 修复",
                "difficulty": "困难", "language_framework": source["language_framework"],
                "project_category": source["project_category"],
                "reproducibility": source["reproducibility"],
                "topic_confirmed": True, "repo_name": repo_name,
                "project_directory": source["project_directory"],
                "verification_commands": source["verification_commands"],
                "origin": "bank", "bank_batch_id": item["batch_id"],
                "bank_slot": item["slot"],
                "difficulty_preflight": {
                    "seed_snapshot": draft["seed_snapshot"],
                    "review": draft.get("difficulty_review") or {},
                    "analysis": draft.get("independent_review") or {},
                    "estimated_handwritten_lines": draft["estimated_lines"],
                    "scope_label": "困难偏低",
                    "source_project_number": source["root_number"],
                    "source_artifact_sha": source["sha"],
                },
            }
            try:
                pair = models.create(h, payload, enforce_prompt_dedup=True)
                pair_id = pair["id"]
            except h.WorkflowError:
                with h.db_connection() as db:
                    existing = db.execute("SELECT id,prompt,origin FROM pairwise_tasks WHERE repo_name=?",
                                          (repo_name,)).fetchone()
                if not existing or existing["prompt"] != draft["prompt"] or existing["origin"] != "bank":
                    raise
                pair_id = existing["id"]
        pair = models.task(h, pair_id)
        if pair.get("deleted_at"):
            raise h.WorkflowError("此题已从任务列表移除，不能重复领用")
        history.append(h, pair)
        with h.db_connection() as db:
            db.execute("UPDATE pairwise_bug_candidates SET pair_id=?,status='added' WHERE batch_id=? AND slot=?",
                       (pair_id, item["batch_id"], item["slot"]))
    if queue:
        pair = models.task(h, pair_id)
        if pair.get("deleted_at"):
            raise h.WorkflowError("此题已从任务列表移除，不能重新排队")
        if not models.attempts(h, pair_id):
            workflow.start(h, pair_id, None)
        with h.db_connection() as db:
            db.execute("UPDATE pairwise_bug_candidates SET status='queued' WHERE batch_id=? AND slot=?",
                       (item["batch_id"], item["slot"]))
    if item["status"] == "ready":
        _refill_reserve(h, item["batch_id"])
    return {"ok": True, "pair_id": pair_id}


def operate(h, batch_id, slot, operation, *, confirmed=False):
    models.initialize(h)
    item = _candidate(h, batch_id, slot)
    if operation == "skip":
        if item["pair_id"] or item["status"] not in {"pending", "generating"}:
            raise h.WorkflowError("只能跳过待生成或生成中的来源")
        with h.db_connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM pairwise_bug_batches WHERE id=?",
                             (batch_id,)).fetchone()
            if not row or row["status"] not in ACTIVE:
                raise h.WorkflowError("批次未运行，请先继续补库")
            changed = db.execute("""UPDATE pairwise_bug_candidates
                SET status='deleted',error='用户从页面跳过来源'
                WHERE batch_id=? AND slot=? AND status IN ('pending','generating') AND pair_id=''""",
                (batch_id, slot)).rowcount
            if not changed:
                raise h.WorkflowError("来源状态已变化，请刷新列表")
        if item["status"] == "generating":
            h.cancel_background_job(_job_key(batch_id))
        return {"ok": True, "skipped": True}
    if operation in {"add", "queue"}:
        if not confirmed:
            raise h.WorkflowError("请先核对题面并确认")
        return _add(h, item, queue=operation == "queue")
    if operation == "reject":
        if item["status"] not in {"ready", "failed"} or item["pair_id"]:
            raise h.WorkflowError("已领用或正在生成的候选题不能驳回")
        _update(h, batch_id, slot=slot, slot_status="rejected")
        if item["status"] == "ready":
            _refill_reserve(h)
        return {"ok": True}
    if operation == "delete":
        with h.db_connection() as db:
            changed = db.execute("""UPDATE pairwise_bug_candidates SET status='deleted'
                WHERE batch_id=? AND slot=? AND status='rejected' AND pair_id=''""",
                (batch_id, slot)).rowcount
        if not changed:
            raise h.WorkflowError("只能删除已驳回且未加入任务列表的 Bug 候选题")
        return {"ok": True}
    if operation == "retry":
        if item["status"] != "failed":
            raise h.WorkflowError("只能重新生成失败的 Bug 候选题")
        with h.db_connection() as db:
            db.execute("BEGIN IMMEDIATE")
            active = db.execute("SELECT 1 FROM pairwise_bug_batches WHERE status IN ('queued','running') LIMIT 1").fetchone()
            if active:
                raise h.WorkflowError("已有 Bug 题面批次正在生成")
            db.execute("UPDATE pairwise_bug_candidates SET status='pending',error='' WHERE batch_id=? AND slot=?",
                       (batch_id, slot))
            db.execute("UPDATE pairwise_bug_batches SET status='queued',detail='等待重新生成',updated_at=? WHERE id=?",
                       (h.now_text(), batch_id))
        threading.Thread(target=_generate, args=(h, batch_id), daemon=True,
                         name=f"bug-bank-retry-{batch_id}").start()
        return {"ok": True}
    raise h.WorkflowError("未知的 Bug 候选操作")
