"""Durable prompt buffer and an opt-in source catalog of old 0-1 questions."""

import difflib
import hashlib
import json
import re
import time
import uuid
from pathlib import Path
from . import generation_policy


POLICY_VERSION = generation_policy.VERSION
READY_LIMIT = 4
RESERVATION_SECONDS = 30 * 60


def ready_count(h):
    initialize(h)
    with h.db_connection() as db:
        return db.execute("SELECT COUNT(*) FROM pairwise_candidate_bank WHERE status='ready'").fetchone()[0]


def ready_task_counts(h):
    initialize(h)
    with h.db_connection() as db:
        rows = db.execute("""SELECT task_type,COUNT(*) AS total FROM pairwise_candidate_bank
            WHERE status='ready' GROUP BY task_type""").fetchall()
    return {str(row["task_type"]): int(row["total"]) for row in rows}


def ready_records(h):
    """Expose buffered prompts to later generation without treating them as created Pairs."""
    initialize(h)
    with h.db_connection() as db:
        rows = db.execute("""SELECT id,task_type,prompt,draft_json
            FROM pairwise_candidate_bank WHERE status='ready' ORDER BY created_at,rowid""").fetchall()
    result = []
    for row in rows:
        try:
            draft = json.loads(row["draft_json"])
        except (TypeError, ValueError):
            draft = {}
        repo = str(draft.get("repo_name") or f"pending-{row['id']}")
        result.append(h.history_record(
            repo, str(row["task_type"]), str(row["prompt"]),
            {"summary": h.summarize_historical_prompt(str(row["prompt"]))},
        ))
    return result


def _source_fields(source):
    return ((source or {}).get("pair", {}).get("id", ""),
            (source or {}).get("attempt", {}).get("id", ""),
            (source or {}).get("attempt", {}).get("artifact_sha", ""),
            (source or {}).get("attempt", {}).get("artifact_tree", ""))


def has_ready(h, task_type, source=None):
    initialize(h)
    pair_id, attempt_id, sha, _ = _source_fields(source)
    with h.db_connection() as db:
        return bool(db.execute("""SELECT 1 FROM pairwise_candidate_bank WHERE status='ready'
            AND task_type=? AND source_pair_id=? AND source_attempt_id=? AND source_sha=? LIMIT 1""",
            (task_type, pair_id, attempt_id, sha)).fetchone())


def stage(h, task_type, draft, project_number, source=None, *, max_ready=None, capacity_task_type=None):
    initialize(h)
    prompt = str(draft.get("first_prompt") or draft.get("prompt") or "").strip()
    if not prompt:
        raise h.WorkflowError("不能保存空题面候选")
    reason = overlap(h, prompt)
    if reason:
        raise h.WorkflowError(reason)
    pair_id, attempt_id, sha, tree = _source_fields(source)
    now = h.now_text()
    identifier = uuid.uuid4().hex[:12]
    with h.db_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        if capacity_task_type is not None and capacity_task_type != task_type:
            raise h.WorkflowError("候选题型不属于当前补题范围")
        capacity_sql = "SELECT COUNT(*) FROM pairwise_candidate_bank WHERE status='ready'"
        capacity_args = ()
        if capacity_task_type is not None:
            capacity_sql += " AND task_type=?"
            capacity_args = (capacity_task_type,)
        if max_ready is not None and db.execute(capacity_sql, capacity_args).fetchone()[0] >= max_ready:
            return None
        exists = db.execute("""SELECT id,prompt_hash,draft_json FROM pairwise_candidate_bank WHERE status='ready'
            AND task_type=? AND source_pair_id=? AND source_sha=? AND (source_pair_id<>'' OR prompt_hash=?)""",
            (task_type, pair_id, sha, fingerprint(prompt))).fetchone()
        if exists:
            existing_source = json.loads(exists["draft_json"]).get("legacy_source_run_id", "")
            if (exists["prompt_hash"] != fingerprint(prompt)
                    or existing_source != draft.get("legacy_source_run_id", "")):
                raise h.WorkflowError("同一来源已有另一道待选题，请先领用或驳回")
            return str(exists["id"])
        target = _shingles(prompt)
        for prior in db.execute("""SELECT prompt,prompt_hash,draft_json FROM pairwise_candidate_bank
            WHERE status='ready'"""):
            if prior["prompt_hash"] == fingerprint(prompt):
                raise h.WorkflowError("待选库已有相同题面")
            if not pair_id and draft.get("repo_name"):
                previous_repo = json.loads(prior["draft_json"]).get("repo_name", "")
                if str(previous_repo).casefold() == str(draft["repo_name"]).casefold():
                    raise h.WorkflowError("待选库已有相同仓库名")
            previous = normalize(prior["prompt"])
            pieces = _shingles(previous)
            union = target | pieces
            rough = len(target & pieces) / len(union) if union else 0
            if rough >= 0.54 and difflib.SequenceMatcher(None, normalize(prompt), previous).ratio() >= 0.82:
                raise h.WorkflowError("与待选库题面高度相似")
        db.execute("""INSERT INTO pairwise_candidate_bank
            (id,task_type,status,prompt,prompt_hash,draft_json,project_number,
             source_pair_id,source_attempt_id,source_sha,source_tree,
             policy_version,review_model,created_at,updated_at)
             VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (identifier, task_type, "ready", prompt, fingerprint(prompt),
             json.dumps(draft, ensure_ascii=False), project_number,
             pair_id, attempt_id, sha, tree,
             f"{POLICY_VERSION}:{h.difficulty_policy.VERSION}", str(h.REVIEW_MODEL), now, now))
    return identifier


def claim(h, task_type, pending_id, source=None):
    initialize(h)
    pair_id, attempt_id, sha, tree = _source_fields(source)
    with h.db_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("""UPDATE pairwise_candidate_bank SET status='used',pair_id=reserved_pair_id,
            lease_until=0,updated_at=? WHERE status='reserved' AND reserved_pair_id IN
            (SELECT id FROM pairwise_tasks)""", (h.now_text(),))
        expired = db.execute("""SELECT id,task_type,source_pair_id,source_sha,prompt_hash
            FROM pairwise_candidate_bank WHERE status='reserved' AND lease_until<?""",
            (time.time(),)).fetchall()
        for old in expired:
            duplicate = db.execute("""SELECT 1 FROM pairwise_candidate_bank WHERE status='ready'
                AND task_type=? AND source_pair_id=? AND source_sha=?
                AND (source_pair_id<>'' OR prompt_hash=?) LIMIT 1""",
                (old["task_type"], old["source_pair_id"], old["source_sha"], old["prompt_hash"])).fetchone()
            db.execute("""UPDATE pairwise_candidate_bank SET status=?,reserved_pair_id='',
                lease_until=0,reason=?,updated_at=? WHERE id=?""",
                ("stale" if duplicate else "ready",
                 "过期锁已有新候选替代" if duplicate else "预留超时后恢复",
                 h.now_text(), old["id"]))
        row = db.execute("""SELECT * FROM pairwise_candidate_bank WHERE status='ready'
            AND task_type=? AND source_pair_id=? AND source_attempt_id=? AND source_sha=?
            ORDER BY created_at,rowid LIMIT 1""", (task_type, pair_id, attempt_id, sha)).fetchone()
        if not row:
            return None
        if task_type == 'Bug 修复' and source:
            from . import sources
            root_pair_id = str(source.get('root_pair_id') or sources.lineage_root_id(db, pair_id))
            if ((source['pair'].get('task_type') == 'Bug 修复'
                 and not sources.bug_lineage_reuse_enabled(h, db))
                    or (not sources.bug_lineage_reuse_enabled(h, db)
                        and sources.lineage_consumed(db, root_pair_id, task_type))):
                db.execute("UPDATE pairwise_candidate_bank SET status='stale',reason=?,updated_at=? WHERE id=?",
                           ("项目根血缘已经创建过 Bug 修复题", h.now_text(), row['id']))
                return None
        if (str(row["policy_version"]) != f"{POLICY_VERSION}:{h.difficulty_policy.VERSION}"
                or str(row["review_model"]) != str(h.REVIEW_MODEL)):
            db.execute("UPDATE pairwise_candidate_bank SET status='stale',reason=?,updated_at=? WHERE id=?",
                       ("出题审核规则或审核模型已变化", h.now_text(), row["id"]))
            return None
        if source and tree != row["source_tree"]:
            db.execute("UPDATE pairwise_candidate_bank SET status='stale',reason=?,updated_at=? WHERE id=?",
                       ("来源 Git tree 已变化", h.now_text(), row["id"]))
            return None
        db.execute("""UPDATE pairwise_candidate_bank SET status='reserved',reserved_pair_id=?,
            lease_until=?,updated_at=? WHERE id=?""",
            (pending_id, time.time() + RESERVATION_SECONDS, h.now_text(), row["id"]))
    return {**dict(row), "draft": json.loads(row["draft_json"])}


def finish(h, candidate_id, pair_id):
    with h.db_connection() as db:
        db.execute("""UPDATE pairwise_candidate_bank SET status='used',pair_id=?,lease_until=0,
            updated_at=? WHERE id=? AND status='reserved' AND reserved_pair_id=?""",
            (pair_id, h.now_text(), candidate_id, pair_id))


def release(h, candidate_id, reason="", stale=False):
    if not candidate_id:
        return
    with h.db_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT reserved_pair_id FROM pairwise_candidate_bank WHERE id=? AND status='reserved'",
                         (candidate_id,)).fetchone()
        if not row:
            return
        if db.execute("SELECT 1 FROM pairwise_tasks WHERE id=?", (row["reserved_pair_id"],)).fetchone():
            db.execute("""UPDATE pairwise_candidate_bank SET status='used',pair_id=reserved_pair_id,
                lease_until=0,updated_at=? WHERE id=?""", (h.now_text(), candidate_id))
        else:
            old = db.execute("""SELECT task_type,source_pair_id,source_sha,prompt_hash FROM pairwise_candidate_bank
                WHERE id=?""", (candidate_id,)).fetchone()
            duplicate = db.execute("""SELECT 1 FROM pairwise_candidate_bank WHERE status='ready'
                AND task_type=? AND source_pair_id=? AND source_sha=?
                AND (source_pair_id<>'' OR prompt_hash=?) LIMIT 1""",
                (old["task_type"], old["source_pair_id"], old["source_sha"], old["prompt_hash"])).fetchone()
            db.execute("""UPDATE pairwise_candidate_bank SET status=?,reserved_pair_id='',
                lease_until=0,reason=?,updated_at=? WHERE id=?""",
                ("stale" if stale or duplicate else "ready",
                 str(reason or ("已有新候选替代" if duplicate else ""))[:1200],
                 h.now_text(), candidate_id))


def reject(h, candidate_id, reason):
    if not normalize(reason):
        raise h.WorkflowError("请填写驳回原因")
    with h.db_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT status FROM pairwise_candidate_bank WHERE id=?", (candidate_id,)).fetchone()
        if not row:
            raise h.WorkflowError("候选题不存在")
        if row["status"] != "ready":
            raise h.WorkflowError("只有未被领用的候选题可以驳回")
        db.execute("UPDATE pairwise_candidate_bank SET status='rejected',reason=?,updated_at=? WHERE id=?",
                   (normalize(reason)[:1200], h.now_text(), candidate_id))


def listing(h, query):
    initialize(h)
    try:
        page = max(1, int(query.get("page", ["1"])[0]))
    except ValueError:
        raise h.WorkflowError("页码无效")
    status = str(query.get("status", [""])[0])
    if status not in {"", "ready", "reserved", "used", "rejected", "stale"}:
        raise h.WorkflowError("候选状态无效")
    search = str(query.get("query", [""])[0])[:100]
    where = "WHERE (?='' OR status=?) AND (prompt LIKE ? OR task_type LIKE ?)"
    args = (status, status, f"%{search}%", f"%{search}%")
    with h.db_connection() as db:
        total = db.execute(f"SELECT COUNT(*) FROM pairwise_candidate_bank {where}", args).fetchone()[0]
        pages = max(1, (total + 19) // 20)
        page = min(page, pages)
        rows = db.execute(f"""SELECT id,task_type,status,prompt,project_number,
            source_pair_id,source_sha,reason,pair_id,created_at,updated_at
            FROM pairwise_candidate_bank {where} ORDER BY created_at DESC,id DESC LIMIT 20 OFFSET ?""",
            (*args, (page - 1) * 20)).fetchall()
        counts = {row["status"]: row["total"] for row in db.execute(
            "SELECT status,COUNT(*) AS total FROM pairwise_candidate_bank GROUP BY status")}
        legacy_count = db.execute("SELECT COUNT(*) FROM pairwise_legacy_prompt_index").fetchone()[0]
    return {"items": [dict(row) for row in rows], "page": page, "pages": pages,
            "total": total, "counts": counts, "legacy_count": legacy_count,
            "legacy_root": str(legacy_root(h))}


def _finish_legacy_review(h, run_id, token, status, reason, review=None):
    with h.db_connection() as db:
        db.execute("""UPDATE pairwise_legacy_reuse SET status=?,reason=?,review_json=?,
            reserved_token='',lease_until=0,updated_at=?
            WHERE run_id=? AND status='reviewing' AND reserved_token=?""",
            (status, str(reason)[:2000], json.dumps(review or {}, ensure_ascii=False),
             h.now_text(), run_id, token))


def legacy_reuse_counts(h):
    initialize(h)
    with h.db_connection() as db:
        return {row["status"]: row["n"] for row in db.execute(
            "SELECT status,COUNT(*) AS n FROM pairwise_legacy_reuse GROUP BY status")}


def review_next_legacy(h, project_number, pair_history, progress):
    """Independently review one old root question; never send old product code."""
    initialize(h)
    category = h.category_for_project_number(project_number)
    token = uuid.uuid4().hex
    with h.db_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("""UPDATE pairwise_legacy_reuse SET status='available',reserved_token='',
            lease_until=0,reason='复核租约超时，重新排队',updated_at=?
            WHERE status='reviewing' AND lease_until<?""", (h.now_text(), time.time()))
        row = db.execute("""SELECT * FROM pairwise_legacy_reuse WHERE status='available'
            AND project_category=? ORDER BY priority DESC,created_at,run_id LIMIT 1""",
            (category,)).fetchone()
        if not row:
            return None
        db.execute("""UPDATE pairwise_legacy_reuse SET status='reviewing',reserved_token=?,
            lease_until=?,updated_at=? WHERE run_id=?""",
            (token, time.time() + RESERVATION_SECONDS, h.now_text(), row["run_id"]))
    original = dict(row)
    run_id = original["run_id"]
    try:
        duplicate = overlap(h, original["prompt"])
        if duplicate:
            _finish_legacy_review(h, run_id, token, "blocked", duplicate)
            return None
        progress(f"正在复核 zzzz 旧题 {original['repo_name']}")
        source_name = re.sub(r"^\d{4}(?:-\d+)?-", "", original["repo_name"])
        candidate = {
            "title": source_name, "repo_name": source_name,
            "first_prompt": original["prompt"],
            "language_framework": original["language_framework"],
            "verification_commands": json.loads(original["verification_commands"]),
        }
        nearest = h.closest_history_for_candidate(candidate, pair_history,
                                                  h.TASK_GENERATION_REVIEW_HISTORY_LIMIT)
        review = h.run_codex_task_validation(candidate, category, nearest,
                                             timeout_seconds=10 * 60)
        feedback, scope_errors = h.task_review_feedback(review)
        level = h.reviewed_task_difficulty(review)
        errors = h.difficulty_review_errors({"difficulty_review": review.get("difficulty_review")})
        if (not review.get("approved") or scope_errors or h.task_review_has_history_overlap(review)
                or level not in {"困难", "地狱"} or errors):
            reason = feedback + ("；" + "；".join(errors) if errors else "")
            _finish_legacy_review(h, run_id, token, "rejected", reason, review)
            return None
        base = re.sub(r"[^a-z0-9-]+", "-", source_name.lower()).strip("-")[:82]
        repo_name = h.unique_repo_name(f"{base}-gsb")
        draft = {
            "title": source_name, "repo_name": repo_name,
            "first_prompt": original["prompt"], "category": category,
            "language_framework": original["language_framework"],
            "verification_commands": candidate["verification_commands"],
            "task_difficulty": level, "difficulty_review": review["difficulty_review"],
            "complexity_analysis": review["difficulty_review"],
            "legacy_source_run_id": run_id,
        }
        with h.db_connection() as db:
            changed = db.execute("""UPDATE pairwise_legacy_reuse SET review_json=?,updated_at=?
                WHERE run_id=? AND status='reviewing' AND reserved_token=? AND lease_until>?""",
                (json.dumps(review, ensure_ascii=False), h.now_text(), run_id, token,
                 time.time())).rowcount
        if not changed:
            raise h.WorkflowError("旧题复核租约已失效")
        return draft
    except Exception:
        with h.db_connection() as db:
            db.execute("""UPDATE pairwise_legacy_reuse SET status='available',reserved_token='',
                lease_until=0,updated_at=? WHERE run_id=? AND status='reviewing' AND reserved_token=?""",
                (h.now_text(), run_id, token))
        raise


def mark_legacy_staged(h, run_id, candidate_id):
    if not run_id:
        return
    with h.db_connection() as db:
        changed = db.execute("""UPDATE pairwise_legacy_reuse SET status='staged',candidate_id=?,
            reserved_token='',lease_until=0,updated_at=?
            WHERE run_id=? AND status='reviewing'""", (candidate_id, h.now_text(), run_id)).rowcount
        if changed != 1:
            raise h.WorkflowError("旧题复核状态已变化，不能放入待选库")


def normalize(prompt):
    return re.sub(r"\s+", " ", str(prompt or "")).strip()


def fingerprint(prompt):
    return hashlib.sha256(normalize(prompt).encode("utf-8")).hexdigest()


def initialize(h):
    with h.db_connection() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS pairwise_candidate_bank (
          id TEXT PRIMARY KEY, task_type TEXT NOT NULL, status TEXT NOT NULL,
          prompt TEXT NOT NULL, prompt_hash TEXT NOT NULL,
          draft_json TEXT NOT NULL, project_number INTEGER NOT NULL,
          source_pair_id TEXT NOT NULL DEFAULT '', source_attempt_id TEXT NOT NULL DEFAULT '',
          source_sha TEXT NOT NULL DEFAULT '', source_tree TEXT NOT NULL DEFAULT '',
          policy_version TEXT NOT NULL, review_model TEXT NOT NULL,
          reserved_pair_id TEXT NOT NULL DEFAULT '', lease_until REAL NOT NULL DEFAULT 0,
          pair_id TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS pairwise_candidate_lookup
          ON pairwise_candidate_bank(status,task_type,source_pair_id,source_sha,created_at);
        CREATE UNIQUE INDEX IF NOT EXISTS pairwise_candidate_ready_uq
          ON pairwise_candidate_bank(task_type,source_pair_id,source_sha)
          WHERE status='ready' AND source_pair_id<>'';
        CREATE TABLE IF NOT EXISTS pairwise_legacy_prompt_index (
          prompt_hash TEXT PRIMARY KEY, prompt TEXT NOT NULL, root_number TEXT NOT NULL,
          repo_name TEXT NOT NULL, task_type TEXT NOT NULL,
          source_paths TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS pairwise_legacy_root ON pairwise_legacy_prompt_index(root_number);
        CREATE TABLE IF NOT EXISTS pairwise_legacy_reuse (
          run_id TEXT PRIMARY KEY, prompt_hash TEXT NOT NULL UNIQUE,
          repo_name TEXT NOT NULL, prompt TEXT NOT NULL, project_category TEXT NOT NULL,
          language_framework TEXT NOT NULL, verification_commands TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'available', reason TEXT NOT NULL DEFAULT '',
          priority INTEGER NOT NULL DEFAULT 0, reserved_token TEXT NOT NULL DEFAULT '',
          lease_until REAL NOT NULL DEFAULT 0, candidate_id TEXT NOT NULL DEFAULT '',
          pair_id TEXT NOT NULL DEFAULT '', review_json TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS pairwise_legacy_reuse_select
          ON pairwise_legacy_reuse(status,project_category,priority,created_at);
        """)
        # Old databases used one unique slot for every 0-1 draft because all
        # roots have an empty source. Replace that index only once; initialize
        # is also called by read paths and must not rebuild indexes every poll.
        index = db.execute("""SELECT sql FROM sqlite_master
            WHERE type='index' AND name='pairwise_candidate_ready_uq'""").fetchone()
        if index and "source_pair_id<>''" not in str(index["sql"] or ""):
            db.execute("DROP INDEX pairwise_candidate_ready_uq")
            db.execute("""CREATE UNIQUE INDEX pairwise_candidate_ready_uq
                ON pairwise_candidate_bank(task_type,source_pair_id,source_sha)
                WHERE status='ready' AND source_pair_id<>''""")
        db.execute("""UPDATE pairwise_candidate_bank SET status='stale',reason=?,updated_at=?
            WHERE status='ready' AND (policy_version<>? OR review_model<>?)""",
            ("出题审核规则或审核模型已变化", h.now_text(),
             f"{POLICY_VERSION}:{h.difficulty_policy.VERSION}", str(h.REVIEW_MODEL)))
        db.execute("""UPDATE pairwise_candidate_bank SET status='stale',reason=?,updated_at=?
            WHERE status='ready' AND source_pair_id<>'' AND EXISTS (
                SELECT 1 FROM pairwise_tasks AS task
                WHERE task.source_pair_id=pairwise_candidate_bank.source_pair_id
                  AND task.task_type=pairwise_candidate_bank.task_type)""",
            ("来源已创建同类型子题", h.now_text()))
        # Exact-source checks are not enough for terminal Bug tasks: a root,
        # Feature descendant, and older snapshot all represent one project
        # family. Retire buffered drafts as soon as any member creates a Bug.
        from . import sources
        ready_bugs = db.execute("""SELECT id,source_pair_id FROM pairwise_candidate_bank
            WHERE status='ready' AND task_type='Bug 修复' AND source_pair_id<>''""").fetchall()
        for candidate in ready_bugs:
            root_pair_id = sources.lineage_root_id(db, candidate['source_pair_id'])
            parent = None if candidate['source_pair_id'].startswith('legacy:') else db.execute(
                'SELECT task_type FROM pairwise_tasks WHERE id=?',
                (candidate['source_pair_id'],)).fetchone()
            if ((parent and parent['task_type'] == 'Bug 修复'
                 and not sources.bug_lineage_reuse_enabled(h, db))
                    or (not sources.bug_lineage_reuse_enabled(h, db)
                        and sources.lineage_consumed(db, root_pair_id, 'Bug 修复'))):
                db.execute("""UPDATE pairwise_candidate_bank
                    SET status='stale',reason=?,updated_at=? WHERE id=?""",
                    ("项目根血缘已经创建过 Bug 修复题", h.now_text(), candidate['id']))
        # Pair creation and candidate bookkeeping use separate existing
        # transactions. Recover the narrow crash window after Pair creation.
        db.execute("""UPDATE pairwise_candidate_bank SET status='used',
            pair_id=reserved_pair_id,lease_until=0,updated_at=?
            WHERE status='reserved' AND reserved_pair_id IN
            (SELECT id FROM pairwise_tasks)""", (h.now_text(),))
        db.execute("""UPDATE pairwise_legacy_reuse SET status='used',pair_id=
            (SELECT id FROM pairwise_tasks WHERE legacy_source_run_id=pairwise_legacy_reuse.run_id LIMIT 1),
            reserved_token='',lease_until=0,updated_at=?
            WHERE EXISTS (SELECT 1 FROM pairwise_tasks
                          WHERE legacy_source_run_id=pairwise_legacy_reuse.run_id)
              AND status<>'used'""", (h.now_text(),))


def release_legacy_review(h, run_id):
    if not run_id:
        return
    with h.db_connection() as db:
        db.execute("""UPDATE pairwise_legacy_reuse SET status='available',reserved_token='',
            lease_until=0,updated_at=? WHERE run_id=? AND status='reviewing'""",
            (h.now_text(), run_id))


def mark_legacy_used(h, run_id, pair_id):
    if not run_id:
        return
    with h.db_connection() as db:
        db.execute("""UPDATE pairwise_legacy_reuse SET status='used',pair_id=?,updated_at=?
            WHERE run_id=?""", (pair_id, h.now_text(), run_id))


def legacy_listing(h, query):
    initialize(h)
    try:
        page = max(1, int(query.get("page", ["1"])[0]))
    except ValueError:
        raise h.WorkflowError("页码无效")
    status = str(query.get("status", [""])[0])
    if status not in {"", "available", "reviewing", "staged", "used", "rejected", "blocked"}:
        raise h.WorkflowError("旧题状态无效")
    search = str(query.get("query", [""])[0])[:100]
    where = "WHERE (?='' OR status=?) AND (prompt LIKE ? OR repo_name LIKE ?)"
    args = (status, status, f"%{search}%", f"%{search}%")
    with h.db_connection() as db:
        total = db.execute(f"SELECT COUNT(*) FROM pairwise_legacy_reuse {where}", args).fetchone()[0]
        pages = max(1, (total + 19) // 20)
        page = min(page, pages)
        rows = db.execute(f"""SELECT run_id,repo_name,prompt,project_category,
            status,reason,priority,candidate_id,pair_id FROM pairwise_legacy_reuse
            {where} ORDER BY priority DESC,created_at,run_id LIMIT 20 OFFSET ?""",
            (*args, (page - 1) * 20)).fetchall()
    return {"items": [dict(row) for row in rows], "page": page, "pages": pages,
            "total": total, "counts": legacy_reuse_counts(h)}


def prioritize_legacy(h, run_id):
    initialize(h)
    with h.db_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT status FROM pairwise_legacy_reuse WHERE run_id=?", (run_id,)).fetchone()
        if not row or row["status"] != "available":
            raise h.WorkflowError("只有尚未复核的可用旧题可以优先选用")
        maximum = db.execute("SELECT COALESCE(MAX(priority),0) FROM pairwise_legacy_reuse").fetchone()[0]
        db.execute("UPDATE pairwise_legacy_reuse SET priority=?,updated_at=? WHERE run_id=?",
                   (maximum + 1, h.now_text(), run_id))


def _shingles(value):
    value = normalize(value)
    return {value[index:index + 4] for index in range(max(0, len(value) - 3))}


def overlap(h, prompt, db=None):
    """Only GSB Pair/history is exclusionary; old zzzz questions may be reused."""
    normalized = normalize(prompt)
    a9_issue = h.prompt_a9_mixed_delivery_skeleton_issue(normalized)
    if a9_issue:
        return a9_issue
    key = fingerprint(normalized)
    if db is None:
        with h.db_connection() as connection:
            rows = connection.execute("SELECT prompt,repo_name FROM pairwise_tasks").fetchall()
    else:
        rows = db.execute("SELECT prompt,repo_name FROM pairwise_tasks").fetchall()
    history_path = h.PAIRWISE_HISTORY_PATH
    if history_path.is_file():
        try:
            old_text = history_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            old_text = ""
        for match in re.finditer(r"<!-- prompt-start -->\s*(.*?)\s*<!-- prompt-end -->", old_text, re.S):
            rows.append({"prompt": match.group(1), "repo_name": "GSB 历史题库"})
    target = _shingles(normalized)
    for row in rows:
        other = normalize(row["prompt"])
        if fingerprint(other) == key:
            return f"与已做题 {row['repo_name']} 题面完全相同"
        pieces = _shingles(other)
        union = target | pieces
        rough = len(target & pieces) / len(union) if union else 0
        if rough >= 0.54 and difflib.SequenceMatcher(None, normalized, other).ratio() >= 0.82:
            return f"与已做题 {row['repo_name']} 题面高度相似"
    return ""


def legacy_root(h):
    return Path(getattr(h, "LEGACY_GSB_ROOT", h.APP_DIR.parent / "zzzz"))


def _first_user_prompt(path):
    try:
        with path.open(encoding="utf-8") as stream:
            for number, line in enumerate(stream):
                if number > 100:
                    break
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if item.get("type") != "user":
                    continue
                content = (item.get("message") or {}).get("content")
                if isinstance(content, str):
                    return normalize(content)
                if isinstance(content, list):
                    return normalize(" ".join(
                        block.get("text", "") for block in content
                        if isinstance(block, dict) and block.get("type") == "text"
                    ))
                return ""
    except (OSError, UnicodeError):
        return ""
    return ""


def _legacy_history(h, roots):
    path = h.APP_DIR / "history-prompts.md"
    if not path.is_file():
        return []
    try:
        document = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return []
    pattern = re.compile(
        r"<!-- task-entry-start (?P<meta>\{.*?\}) -->.*?"
        r"<!-- prompt-start -->\s*(?P<prompt>.*?)\s*<!-- prompt-end -->", re.S
    )
    result = []
    for match in pattern.finditer(document):
        try:
            meta = json.loads(match.group("meta"))
        except json.JSONDecodeError:
            continue
        heading = document[match.start():match.end()]
        numbered = re.search(r"^## (\d{4})(?:-\d+)?\s", heading, re.M)
        root_number = numbered.group(1) if numbered else ""
        if root_number in roots:
            result.append((root_number, str(meta.get("repo_name") or ""),
                           str(meta.get("task_type") or "历史题型未核实"),
                           normalize(match.group("prompt")), str(path)))
    return result
