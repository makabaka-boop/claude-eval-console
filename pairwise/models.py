"""Additive schema: old runs remain available without being reinterpreted as pairs."""
import json
import re
import sqlite3
import tempfile
import uuid
from pathlib import Path
from datetime import datetime

TASK_TYPES = ("0-1 代码生成", "Feature 迭代", "Bug 修复", "代码理解", "代码重构", "工程化", "代码测试")
PROJECT_CATEGORIES = ("纯后端", "纯前端", "全栈")
REPRODUCIBILITY_LEVELS = ("无外部依赖", "有外部依赖，未容器化", "已容器化，可一键起环境")
CHOICES = ("A 更好", "Same", "B 更好")
ACTIVE = ("queued", "starting", "running", "recovering", "archiving")
AUTO_MODEL = "auto_model/urm"
MODEL_NAMES = (AUTO_MODEL, "ark/urm-03")
PENDING_NUMBER_REMAP_KEY = "pairwise_pending_number_remap_v2"
NUMBER_REMAP_AUDIT_KEY = "pairwise_number_remap_v2_audit"


def backup_database(path):
    directory = path.parent / "backups"
    directory.mkdir(exist_ok=True)
    target = directory / "before-pairwise-v1.sqlite3"
    if not target.exists():
        # Publish only a completed backup, so an interrupted copy is never reused.
        with tempfile.TemporaryDirectory(prefix="backup-", dir=directory) as work:
            temporary = Path(work) / "database.sqlite3"
            with sqlite3.connect(path) as source, sqlite3.connect(temporary) as destination:
                source.backup(destination)
            temporary.replace(target)


def initialize(h):
    with h.db_connection() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS pairwise_tasks (
          id TEXT PRIMARY KEY, title TEXT NOT NULL, prompt TEXT NOT NULL,
          task_type TEXT NOT NULL, difficulty TEXT NOT NULL, language_framework TEXT NOT NULL,
          project_number INTEGER NOT NULL DEFAULT 0,
          sequence_number INTEGER NOT NULL DEFAULT 0,
          display_number TEXT NOT NULL DEFAULT '',
          project_category TEXT NOT NULL DEFAULT '未记录',
          reproducibility TEXT NOT NULL DEFAULT '', repo_name TEXT NOT NULL,
          source_repo_url TEXT NOT NULL DEFAULT '', source_base_sha TEXT NOT NULL DEFAULT '',
          repo_url TEXT NOT NULL DEFAULT '', base_sha TEXT NOT NULL DEFAULT '',
          project_directory TEXT NOT NULL, verification_commands TEXT NOT NULL DEFAULT '[]',
          origin TEXT NOT NULL DEFAULT 'manual',
          legacy_source_run_id TEXT NOT NULL DEFAULT '',
          config TEXT NOT NULL DEFAULT '{}', gsb TEXT NOT NULL DEFAULT '',
          reason TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
          review_source TEXT NOT NULL DEFAULT '',
          submission_difficulty TEXT NOT NULL DEFAULT '',
          a_execution_model TEXT NOT NULL DEFAULT 'auto_model/urm',
          b_execution_model TEXT NOT NULL DEFAULT 'auto_model/urm',
          a_delivery_score INTEGER NOT NULL DEFAULT 0,
          a_delivery_description TEXT NOT NULL DEFAULT '',
          b_delivery_score INTEGER NOT NULL DEFAULT 0,
          b_delivery_description TEXT NOT NULL DEFAULT '',
          reference_gsb TEXT NOT NULL DEFAULT '', reference_reason TEXT NOT NULL DEFAULT '',
          reference_status TEXT NOT NULL DEFAULT '', reference_error TEXT NOT NULL DEFAULT '',
          reference_source TEXT NOT NULL DEFAULT '', reference_generated_at TEXT NOT NULL DEFAULT '',
          auto_skip_reason TEXT NOT NULL DEFAULT '',
          human_confirmed INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL, deleted_at TEXT
        );
        CREATE TABLE IF NOT EXISTS pairwise_attempts (
          id TEXT PRIMARY KEY REFERENCES runs(id), pair_id TEXT NOT NULL REFERENCES pairwise_tasks(id),
          side TEXT NOT NULL CHECK(side IN ('A','B')), number INTEGER NOT NULL,
          status TEXT NOT NULL DEFAULT 'queued', error TEXT NOT NULL DEFAULT '',
          environment TEXT NOT NULL DEFAULT '{}', prompt_sent INTEGER NOT NULL DEFAULT 0,
          trace_url TEXT NOT NULL DEFAULT '', video_url TEXT NOT NULL DEFAULT '',
          video_seconds REAL, recording_confirmed INTEGER NOT NULL DEFAULT 0,
          recording_status TEXT NOT NULL DEFAULT '', recording_path TEXT NOT NULL DEFAULT '',
          recording_directory TEXT NOT NULL DEFAULT '', recording_error TEXT NOT NULL DEFAULT '',
          recording_queued_at TEXT, recording_started_at TEXT, recording_finished_at TEXT,
          recording_stage TEXT NOT NULL DEFAULT '', recording_stage_started_at TEXT,
          recording_duration REAL,
          artifact_sha TEXT NOT NULL DEFAULT '', artifact_tree TEXT NOT NULL DEFAULT '',
          trace_path TEXT NOT NULL DEFAULT '', trace_sha256 TEXT NOT NULL DEFAULT '',
          verification TEXT NOT NULL DEFAULT '[]', started_at TEXT, prompt_sent_at TEXT,
          finished_at TEXT,
          created_at TEXT NOT NULL, UNIQUE(pair_id, side, number)
        );
        CREATE INDEX IF NOT EXISTS pairwise_attempts_pair ON pairwise_attempts(pair_id, side, number);
        CREATE UNIQUE INDEX IF NOT EXISTS pairwise_tasks_repo_name ON pairwise_tasks(repo_name);
        CREATE TABLE IF NOT EXISTS pairwise_drafts (
          id TEXT PRIMARY KEY, status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
          result TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pairwise_zero_one_batches (
          id TEXT PRIMARY KEY, status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
          count INTEGER NOT NULL, suggested_start INTEGER NOT NULL,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pairwise_zero_one_candidates (
          batch_id TEXT NOT NULL REFERENCES pairwise_zero_one_batches(id),
          slot INTEGER NOT NULL, category TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
          draft TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '',
          pair_id TEXT NOT NULL DEFAULT '', PRIMARY KEY(batch_id,slot)
        );
        CREATE TABLE IF NOT EXISTS pairwise_iteration_batches (
          id TEXT PRIMARY KEY, status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
          count INTEGER NOT NULL, start_number INTEGER NOT NULL DEFAULT 0,
          end_number INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pairwise_iteration_candidates (
          batch_id TEXT NOT NULL REFERENCES pairwise_iteration_batches(id),
          slot INTEGER NOT NULL, source TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
          draft TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '',
          pair_id TEXT NOT NULL DEFAULT '', PRIMARY KEY(batch_id,slot)
        );
        CREATE INDEX IF NOT EXISTS pairwise_iteration_candidate_status
          ON pairwise_iteration_candidates(status,batch_id);
        CREATE TABLE IF NOT EXISTS pairwise_bug_batches (
          id TEXT PRIMARY KEY, status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
          count INTEGER NOT NULL, start_number INTEGER NOT NULL DEFAULT 0,
          end_number INTEGER NOT NULL DEFAULT 0, policy_version INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pairwise_bug_candidates (
          batch_id TEXT NOT NULL REFERENCES pairwise_bug_batches(id),
          slot INTEGER NOT NULL, source TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
          draft TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '',
          pair_id TEXT NOT NULL DEFAULT '', PRIMARY KEY(batch_id,slot)
        );
        CREATE INDEX IF NOT EXISTS pairwise_bug_candidate_status
          ON pairwise_bug_candidates(status,batch_id);
        CREATE TABLE IF NOT EXISTS pairwise_solo_submissions (
          pair_id TEXT PRIMARY KEY REFERENCES pairwise_tasks(id),
          state TEXT NOT NULL DEFAULT 'not_submitted',
          remote_submission_id TEXT NOT NULL DEFAULT '',
          remote_status TEXT NOT NULL DEFAULT '',
          qc_summary TEXT NOT NULL DEFAULT '',
          payload_sha256 TEXT NOT NULL DEFAULT '',
          a_model_name TEXT NOT NULL DEFAULT '',
          b_model_name TEXT NOT NULL DEFAULT '',
          submitted_at TEXT NOT NULL DEFAULT '',
          remote_updated_at TEXT NOT NULL DEFAULT '',
          last_synced_at TEXT NOT NULL DEFAULT '',
          error TEXT NOT NULL DEFAULT '',
          updated_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS pairwise_solo_remote_submission_uq
          ON pairwise_solo_submissions(remote_submission_id)
          WHERE remote_submission_id <> '';
        """)
        bug_batch_columns = {row[1] for row in db.execute("PRAGMA table_info(pairwise_bug_batches)")}
        if "policy_version" not in bug_batch_columns:
            db.execute("ALTER TABLE pairwise_bug_batches ADD COLUMN policy_version INTEGER NOT NULL DEFAULT 1")
        columns = {row[1] for row in db.execute("PRAGMA table_info(pairwise_tasks)")}
        if "source_repo_url" not in columns:
            db.execute("ALTER TABLE pairwise_tasks ADD COLUMN source_repo_url TEXT NOT NULL DEFAULT ''")
        if "source_base_sha" not in columns:
            db.execute("ALTER TABLE pairwise_tasks ADD COLUMN source_base_sha TEXT NOT NULL DEFAULT ''")
        if "origin" not in columns:
            db.execute("ALTER TABLE pairwise_tasks ADD COLUMN origin TEXT NOT NULL DEFAULT 'manual'")
        if "legacy_source_run_id" not in columns:
            db.execute("ALTER TABLE pairwise_tasks ADD COLUMN legacy_source_run_id TEXT NOT NULL DEFAULT ''")
        db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS pairwise_legacy_source_uq
            ON pairwise_tasks(legacy_source_run_id) WHERE legacy_source_run_id<>''""")
        if "project_number" not in columns:
            db.execute("ALTER TABLE pairwise_tasks ADD COLUMN project_number INTEGER NOT NULL DEFAULT 0")
        if "sequence_number" not in columns:
            db.execute("ALTER TABLE pairwise_tasks ADD COLUMN sequence_number INTEGER NOT NULL DEFAULT 0")
        if "display_number" not in columns:
            db.execute("ALTER TABLE pairwise_tasks ADD COLUMN display_number TEXT NOT NULL DEFAULT ''")
        if "project_category" not in columns:
            db.execute("ALTER TABLE pairwise_tasks ADD COLUMN project_category TEXT NOT NULL DEFAULT '未记录'")
        if "deleted_at" not in columns:
            db.execute("ALTER TABLE pairwise_tasks ADD COLUMN deleted_at TEXT")
        if "a_execution_model" not in columns:
            db.execute("ALTER TABLE pairwise_tasks ADD COLUMN a_execution_model TEXT NOT NULL DEFAULT 'auto_model/urm'")
        if "b_execution_model" not in columns:
            db.execute("ALTER TABLE pairwise_tasks ADD COLUMN b_execution_model TEXT NOT NULL DEFAULT 'auto_model/urm'")
        submission_columns = {row[1] for row in db.execute("PRAGMA table_info(pairwise_solo_submissions)")}
        for name in ("a_model_name", "b_model_name"):
            if name not in submission_columns:
                db.execute(f"ALTER TABLE pairwise_solo_submissions ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
        if "auto_skip_reason" not in columns:
            db.execute("ALTER TABLE pairwise_tasks ADD COLUMN auto_skip_reason TEXT NOT NULL DEFAULT ''")
        # Keep legacy reference_* columns as archived data. They no longer
        # schedule reviews or influence current workflow decisions.
        reference_columns = {
            "source_pair_id": "TEXT NOT NULL DEFAULT ''",
            "source_attempt_id": "TEXT NOT NULL DEFAULT ''",
            "source_side": "TEXT NOT NULL DEFAULT ''",
            "source_artifact_sha": "TEXT NOT NULL DEFAULT ''",
            "source_artifact_tree": "TEXT NOT NULL DEFAULT ''",
            "difficulty_preflight": "TEXT NOT NULL DEFAULT '{}'",
            "review_source": "TEXT NOT NULL DEFAULT ''",
            "submission_difficulty": "TEXT NOT NULL DEFAULT ''",
            "difficulty_review": "TEXT NOT NULL DEFAULT '{}'",
            "difficulty_status": "TEXT NOT NULL DEFAULT ''",
            "difficulty_error": "TEXT NOT NULL DEFAULT ''",
            "difficulty_source": "TEXT NOT NULL DEFAULT ''",
            "difficulty_reviewed_at": "TEXT NOT NULL DEFAULT ''",
            "difficulty_started_epoch": "REAL NOT NULL DEFAULT 0",
            "difficulty_stage": "TEXT NOT NULL DEFAULT ''",
            "difficulty_stage_started_epoch": "REAL NOT NULL DEFAULT 0",
            "reference_gsb": "TEXT NOT NULL DEFAULT ''",
            "reference_reason": "TEXT NOT NULL DEFAULT ''",
            "reference_status": "TEXT NOT NULL DEFAULT ''",
            "reference_error": "TEXT NOT NULL DEFAULT ''",
            "reference_source": "TEXT NOT NULL DEFAULT ''",
            "reference_generated_at": "TEXT NOT NULL DEFAULT ''",
        }
        for name, definition in reference_columns.items():
            if name not in columns:
                db.execute(f"ALTER TABLE pairwise_tasks ADD COLUMN {name} {definition}")
        delivery_columns = {
            "a_delivery_score": "INTEGER NOT NULL DEFAULT 0",
            "a_delivery_description": "TEXT NOT NULL DEFAULT ''",
            "b_delivery_score": "INTEGER NOT NULL DEFAULT 0",
            "b_delivery_description": "TEXT NOT NULL DEFAULT ''",
        }
        delivery_fields_added = False
        for name, definition in delivery_columns.items():
            if name not in columns:
                db.execute(f"ALTER TABLE pairwise_tasks ADD COLUMN {name} {definition}")
                delivery_fields_added = True
        if delivery_fields_added:
            # Existing confirmations predate these newly required SOLO-QA fields.
            # Keep the written GSB, but require one explicit review before export.
            db.execute("""UPDATE pairwise_tasks SET human_confirmed=0,
                revision=revision+1,updated_at=?""", (h.now_text(),))
        db.execute("""CREATE TABLE IF NOT EXISTS pairwise_difficulty_review_attempts (
            id TEXT PRIMARY KEY, pair_id TEXT NOT NULL REFERENCES pairwise_tasks(id),
            source TEXT NOT NULL, status TEXT NOT NULL, result_json TEXT NOT NULL DEFAULT '{}',
            error TEXT NOT NULL DEFAULT '', started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL DEFAULT ''
        )""")
        db.execute("""CREATE INDEX IF NOT EXISTS pairwise_difficulty_review_attempts_pair
            ON pairwise_difficulty_review_attempts(pair_id,started_at)""")
        attempt_columns = {row[1] for row in db.execute("PRAGMA table_info(pairwise_attempts)")}
        if "prompt_sent_at" not in attempt_columns:
            db.execute("ALTER TABLE pairwise_attempts ADD COLUMN prompt_sent_at TEXT")
        recording_columns = {
            "recording_status": "TEXT NOT NULL DEFAULT ''",
            "recording_path": "TEXT NOT NULL DEFAULT ''",
            "recording_directory": "TEXT NOT NULL DEFAULT ''",
            "recording_error": "TEXT NOT NULL DEFAULT ''",
            "recording_queued_at": "TEXT",
            "recording_started_at": "TEXT",
            "recording_finished_at": "TEXT",
            "recording_duration": "REAL",
            "recording_stage": "TEXT NOT NULL DEFAULT ''",
            "recording_stage_started_at": "TEXT",
        }
        for name, definition in recording_columns.items():
            if name not in attempt_columns:
                db.execute(f"ALTER TABLE pairwise_attempts ADD COLUMN {name} {definition}")
        # The old schema did not retain category. Backfill only when every
        # generated blueprint produced a Pair, so the schedule is unambiguous.
        auto_rows = db.execute(
            "SELECT id FROM pairwise_tasks WHERE origin='auto' ORDER BY created_at,id"
        ).fetchall()
        cursor = db.execute(
            "SELECT value FROM settings WHERE key='pairwise_blueprint_cursor'"
        ).fetchone()
        try:
            blueprint_count = int(cursor[0]) if cursor else 0
        except (TypeError, ValueError):
            blueprint_count = -1
        schedule = tuple(getattr(h, "CATEGORY_SCHEDULE", ()))
        if schedule and blueprint_count == len(auto_rows):
            for index, row in enumerate(auto_rows):
                db.execute(
                    "UPDATE pairwise_tasks SET project_category=? "
                    "WHERE id=? AND project_category='未记录'",
                    (schedule[index % len(schedule)], row[0]),
                )
        # Draft Pairs created by the first implementation have not created any
        # product repository yet. Reclassify their snapshot as an import source.
        db.execute("""UPDATE pairwise_tasks
            SET source_repo_url=repo_url,source_base_sha=base_sha,repo_url='',base_sha=''
            WHERE source_repo_url='' AND source_base_sha='' AND repo_url<>''
              AND NOT EXISTS (SELECT 1 FROM pairwise_attempts a WHERE a.pair_id=pairwise_tasks.id)""")
        from . import sources
        sources.initialize(db)
        _migrate_numbering(db)
        _apply_pending_number_remap(h, db)
    from . import candidate_bank
    candidate_bank.initialize(h)


def task(h, pair_id):
    with h.db_connection() as db:
        row = db.execute("SELECT * FROM pairwise_tasks WHERE id=?", (pair_id,)).fetchone()
    if not row:
        raise h.WorkflowError("没有找到这条 Pair")
    result = dict(row)
    for key in ("config", "verification_commands", "difficulty_preflight", "difficulty_review"):
        result[key] = json.loads(result[key])
    result["display_number"] = str(result.get("display_number") or f"{int(result['project_number']):04d}")
    # New workspaces use the exact same root/follow-up number as both lists.
    # Existing workspaces are resolved from their persisted run_directory instead.
    result["project_folder"] = f"{result['display_number']}-{result['repo_name']}"
    return result


def display_numbers(h):
    """Return the persisted number shared by the database, UI and workspace."""
    with h.db_connection() as db:
        rows = db.execute(
            "SELECT id,project_number,display_number FROM pairwise_tasks ORDER BY sequence_number,id"
        ).fetchall()
    return {
        str(row["id"]): str(row["display_number"] or f"{int(row['project_number']):04d}")
        for row in rows
    }


def _legacy_display_numbers(db):
    """Freeze the old computed labels before project_number becomes a base number."""
    rows = db.execute(
        """SELECT id,project_number,sequence_number FROM pairwise_tasks
           ORDER BY CASE WHEN sequence_number>0 THEN sequence_number ELSE project_number END,id"""
    ).fetchall()
    order = {
        str(row["id"]): int(row["sequence_number"] or row["project_number"] or 0)
        for row in rows
    }
    parents = {
        str(row["id"]): str(row["source_pair_id"])
        for row in db.execute(
            "SELECT id,source_pair_id FROM pairwise_tasks WHERE source_pair_id<>''"
        ).fetchall()
    }
    # Keep the same compatibility path that supplied labels before source_pair_id
    # was persisted. Ambiguous historical matches retain their own root number.
    for row in db.execute(
        """SELECT child.id AS child_id,parent.id AS parent_id
           FROM pairwise_tasks AS child
           JOIN pairwise_tasks AS parent ON parent.repo_url=child.source_repo_url
           JOIN pairwise_attempts AS attempt
             ON attempt.pair_id=parent.id AND attempt.artifact_sha=child.source_base_sha
           WHERE child.source_pair_id='' AND child.source_repo_url<>'' AND child.source_base_sha<>''
           ORDER BY CASE WHEN parent.sequence_number>0 THEN parent.sequence_number
                         ELSE parent.project_number END,attempt.number,attempt.side"""
    ).fetchall():
        parents.setdefault(str(row["child_id"]), str(row["parent_id"]))

    roots = {}
    for pair_id in order:
        current, seen = pair_id, set()
        while current in parents and current not in seen and parents[current] in order:
            seen.add(current)
            current = parents[current]
        roots[pair_id] = current
    grouped = {}
    for pair_id, root_id in roots.items():
        grouped.setdefault(root_id, []).append(pair_id)
    labels = {}
    rows_by_id = {str(row["id"]): row for row in rows}
    for root_id, pair_ids in grouped.items():
        root_row = rows_by_id[root_id]
        base = int(root_row["project_number"] or root_row["sequence_number"] or 0)
        labels[root_id] = f"{base:04d}"
        children = sorted(
            (pair_id for pair_id in pair_ids if pair_id != root_id),
            key=lambda pair_id: (order[pair_id], pair_id),
        )
        for index, pair_id in enumerate(children, 1):
            labels[pair_id] = f"{base:04d}-{index}"
    return labels


def create(h, payload, *, pair_id=None, enforce_prompt_dedup=False):
    prompt = str(payload.get("prompt") or "")
    title = str(payload.get("title") or "").strip()
    kind = str(payload.get("task_type") or "")
    difficulty = str(payload.get("difficulty") or "")
    language = str(payload.get("language_framework") or "").strip()
    category = str(payload.get("project_category") or "").strip()
    if not title or not prompt.strip() or not language:
        raise h.WorkflowError("请填写标题、完整原始 Prompt 和语言/框架")
    allowed_difficulties = ({"中等", "困难", "地狱"}
                            if kind == "Feature 迭代" else {"困难", "地狱"})
    if kind not in TASK_TYPES or difficulty not in allowed_difficulties:
        raise h.WorkflowError("任务类型或难度无效；Feature 迭代允许中等及以上，其他任务仅允许困难或地狱")
    if category not in PROJECT_CATEGORIES:
        raise h.WorkflowError("项目类别只能选择纯后端、纯前端或全栈")
    reproducibility = str(payload.get("reproducibility") or "").strip()
    if reproducibility not in REPRODUCIBILITY_LEVELS:
        raise h.WorkflowError("请选择环境可复现等级")
    if payload.get("topic_confirmed") is not True:
        raise h.WorkflowError("请确认题目达到对应任务类型的难度门槛且不属于本期禁题")
    snapshot = str(payload.get("snapshot_url") or "").strip()
    source_repo, source_sha = parse_snapshot(h, snapshot) if snapshot else ("", "")
    preflight = payload.get("difficulty_preflight") or {}
    if not isinstance(preflight, dict):
        raise h.WorkflowError("难度预审依据格式无效")
    bank_seed = (kind == "Bug 修复" and payload.get("origin") == "bank"
                 and isinstance(preflight.get("seed_snapshot"), dict))
    if not snapshot and kind != "0-1 代码生成" and not bank_seed:
        raise h.WorkflowError("现有项目任务必须提供初始快照")
    name = h.validate_repo_name(str(payload.get("repo_name") or ""))
    group, directory = h.resolve_project_directory(str(payload.get("project_directory") or ""))
    commands = h.normalize_commands(payload.get("verification_commands") or [])
    origin = "bank" if bank_seed else "manual"
    legacy_source_run_id = str(payload.get("legacy_source_run_id") or "")
    if legacy_source_run_id:
        raise h.WorkflowError("旧题自动选源入口已移除")
    legacy_baseline_run_id = str(payload.get("legacy_baseline_run_id") or "")
    if legacy_baseline_run_id:
        raise h.WorkflowError("旧项目自动选源入口已移除")
    pair_id = pair_id or uuid.uuid4().hex[:12]
    if not isinstance(pair_id, str) or not re.fullmatch(r"[a-f0-9]{12}", pair_id):
        raise h.WorkflowError("Pair ID 无效")
    now = h.now_text()
    defaults = model_defaults(h)
    a_execution_model = str(payload.get("a_execution_model") or defaults["A"])
    b_execution_model = str(payload.get("b_execution_model") or defaults["B"])
    if a_execution_model not in MODEL_NAMES or b_execution_model not in MODEL_NAMES:
        raise h.WorkflowError("A/B 执行模型无效")
    try:
        with h.db_connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM pairwise_tasks WHERE repo_name=?", (name,)).fetchone():
                raise h.WorkflowError("每题必须使用独立仓库；该仓库名称已被另一 Pair 使用")
            if enforce_prompt_dedup:
                from . import candidate_bank
                overlap_reason = candidate_bank.overlap(h, prompt, db=db)
                if overlap_reason:
                    raise h.WorkflowError(overlap_reason)
            from . import sources
            source_fields = sources.bind_child(db, h, payload, pair_id, source_repo, source_sha, kind)
            sequence_number, project_number, display_number = _next_numbering(db, source_fields)
            db.execute("""INSERT INTO pairwise_tasks
                (id,title,prompt,task_type,difficulty,language_framework,project_number,
                 sequence_number,display_number,
                 project_category,reproducibility,repo_name,
                 source_repo_url,source_base_sha,repo_url,base_sha,project_directory,
                 verification_commands,origin,legacy_source_run_id,created_at,updated_at,difficulty_preflight,
                 a_execution_model,b_execution_model)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (pair_id, title, prompt, kind, difficulty, language, project_number,
                 sequence_number, display_number, category,
                 reproducibility, name, source_repo, source_sha, "", "", group,
                 json.dumps(commands, ensure_ascii=False), origin, legacy_source_run_id, now, now,
                 json.dumps(preflight, ensure_ascii=False), a_execution_model, b_execution_model))
            if source_fields:
                db.execute("UPDATE pairwise_tasks SET " + ','.join(f'{key}=?' for key in source_fields)
                           + ' WHERE id=?', [*source_fields.values(), pair_id])
    except sqlite3.IntegrityError as exc:
        raise h.WorkflowError("每题必须使用独立仓库；该仓库名称已被另一 Pair 使用") from exc
    return task(h, pair_id)


def save_review(h, pair_id, payload):
    if {"a_model_name", "b_model_name"} & payload.keys():
        raise h.WorkflowError("模型名称来自实际 Session，不能手动修改")
    pair = task(h, pair_id)
    verdict = str(payload.get("gsb", pair["gsb"]) or "")
    reason = str(payload.get("reason", pair["reason"]) or "")
    if verdict not in ("", *CHOICES):
        raise h.WorkflowError("GSB 只能选择 A 更好 / Same / B 更好")
    def delivery_score(side):
        key = f"{side}_delivery_score"
        raw = payload[key] if key in payload else pair.get(key, 0)
        if raw in (None, "", 0, "0"):
            return 0
        try:
            score = int(raw)
        except (TypeError, ValueError):
            raise h.WorkflowError(f"{side.upper()} 交付完整性必须填写 1 至 5 的整数")
        if score not in range(1, 6):
            raise h.WorkflowError(f"{side.upper()} 交付完整性必须填写 1 至 5 的整数")
        return score

    a_score, b_score = delivery_score("a"), delivery_score("b")
    a_description = str(payload.get("a_delivery_description", pair.get("a_delivery_description", "")) or "")
    b_description = str(payload.get("b_delivery_description", pair.get("b_delivery_description", "")) or "")
    submission_difficulty = str(payload.get("submission_difficulty", pair.get("submission_difficulty", "")) or "")
    if submission_difficulty not in ("", "简单", "中等", "困难", "地狱"):
        raise h.WorkflowError("提交难度只能选择简单、中等、困难或地狱")
    reviewed_fields = {"gsb", "reason", "a_delivery_score", "a_delivery_description",
                       "b_delivery_score", "b_delivery_description"}
    reviewed_now = bool(reviewed_fields.intersection(payload))
    items = attempts(h, pair_id)
    current_source = current_review_source(items)
    review_source = current_source if reviewed_now and current_source else pair["review_source"]
    confirmed = bool(verdict and reason.strip() and a_score and a_description.strip()
                     and b_score and b_description.strip()
                     and (not review_source or review_source == current_source))
    with h.db_connection() as db:
        changed = db.execute("""UPDATE pairwise_tasks SET gsb=?,reason=?,notes=?,
            a_delivery_score=?,a_delivery_description=?,
            b_delivery_score=?,b_delivery_description=?,submission_difficulty=?,
            review_source=?,human_confirmed=?,
            revision=revision+1,updated_at=? WHERE id=? AND revision=?""",
            (verdict, reason, str(payload.get("notes", pair["notes"]) or ""),
             a_score, a_description, b_score, b_description, submission_difficulty,
             review_source, int(confirmed), h.now_text(),
             pair_id, payload.get("revision", -1))).rowcount
    if not changed:
        raise h.WorkflowError("评价已被另一页面修改，请刷新后重新保存")
    return task(h, pair_id)


def attempts(h, pair_id):
    with h.db_connection() as db:
        rows = db.execute("""SELECT a.*, r.session_id, r.model, r.repo_path, r.run_directory,
            r.harness_version, r.phase, r.status_detail, r.first_result, r.container_name,
            r.container_cleaned, r.first_prompt_id, r.updated_at AS run_updated_at
            FROM pairwise_attempts a JOIN runs r ON r.id=a.id WHERE pair_id=?
            ORDER BY side, number""", (pair_id,)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        for key in ("environment", "verification"):
            item[key] = json.loads(item[key])
        result.append(item)
    return result


def attempt(h, run_id):
    with h.db_connection() as db:
        row = db.execute("SELECT pair_id FROM pairwise_attempts WHERE id=?", (run_id,)).fetchone()
    if not row:
        raise h.WorkflowError("没有找到这次执行")
    return next(a for a in attempts(h, row[0]) if a["id"] == run_id)


def latest(items, side):
    return next((a for a in reversed(items) if a["side"] == side), None)


def actual_model_name(attempt):
    if not attempt:
        return ""
    trace_path = Path(attempt.get("trace_path") or "")
    if trace_path.is_file():
        try:
            with trace_path.open(encoding="utf-8", errors="replace") as trace:
                for _ in range(64):
                    line = trace.readline(1024 * 1024)
                    if not line:
                        break
                    if len(line) == 1024 * 1024 and not line.endswith("\n"):
                        break
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if record.get("type") != "assistant":
                        continue
                    message = record.get("message")
                    model = message.get("model") if isinstance(message, dict) else None
                    model = model or record.get("model")
                    if isinstance(model, str) and model.strip():
                        return model.strip()
        except OSError:
            pass
    return str(attempt.get("model") or "")


def current_review_source(items):
    present = [item for item in items if item]
    sides = [latest(present, side) for side in ("A", "B")]
    return ":".join(side["id"] for side in sides) if all(
        side and side["status"] == "complete" for side in sides) else ""


def set_b_execution_model(h, pair_id, model, revision):
    """Choose the model for B's next Session; never relabel past evidence."""
    if model not in MODEL_NAMES:
        raise h.WorkflowError("B 执行模型无效")
    pair = task(h, pair_id)
    if pair.get("deleted_at"):
        raise h.WorkflowError("已移除的任务不能修改模型")
    with h.db_connection() as db:
        submission = db.execute("SELECT state FROM pairwise_solo_submissions WHERE pair_id=?",
                                (pair_id,)).fetchone()
    if submission and submission["state"] == "submitting":
        raise h.WorkflowError("SOLO-QA 正在提交，不能切换执行模型")
    latest_b = latest(attempts(h, pair_id), "B")
    if latest_b and (latest_b["status"] in ACTIVE and latest_b["status"] != "queued"
                     or latest_b["status"] == "upstream_paused"):
        raise h.WorkflowError("B 已开始执行；请先等待或停止，再为下一次独立 Session 选模型")
    if latest_b and latest_b["status"] == "queued" and latest_b["prompt_sent"]:
        raise h.WorkflowError("B 题面已发送，不能切换当前 Session 的模型")
    if pair["b_execution_model"] == model:
        return pair
    now = h.now_text()
    with h.db_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        changed = db.execute("""UPDATE pairwise_tasks SET b_execution_model=?,
            human_confirmed=0,review_source='stale',revision=revision+1,updated_at=?
            WHERE id=? AND revision=?""", (model, now, pair_id, revision)).rowcount
        if not changed:
            raise h.WorkflowError("任务已由另一页面修改，请刷新后重试")
        if latest_b and latest_b["status"] == "queued":
            db.execute("UPDATE runs SET model=?,updated_at=? WHERE id=?",
                       (model, now, latest_b["id"]))
            db.execute("UPDATE run_turns SET model=?,updated_at=? WHERE run_id=? AND turn_number=1",
                       (model, now, latest_b["id"]))
    return task(h, pair_id)


def model_defaults(h):
    with h.db_connection() as db:
        rows = db.execute("SELECT key,value FROM settings WHERE key IN (?,?)",
                          ("pairwise_a_execution_model", "pairwise_b_execution_model")).fetchall()
    saved = {row["key"]: row["value"] for row in rows}
    defaults = {}
    for side in ("A", "B"):
        model = saved.get(f"pairwise_{side.lower()}_execution_model", AUTO_MODEL)
        defaults[side] = model if model in MODEL_NAMES else AUTO_MODEL
    return defaults


def set_model_defaults(h, a_model, b_model):
    if a_model not in MODEL_NAMES or b_model not in MODEL_NAMES:
        raise h.WorkflowError("A/B 执行模型无效")
    now = h.now_text()
    with h.db_connection() as db:
        db.executemany("""INSERT INTO settings(key,value,updated_at) VALUES (?,?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
            [(f"pairwise_{side.lower()}_execution_model", model, now)
             for side, model in (("A", a_model), ("B", b_model))])
    return {"A": a_model, "B": b_model}


def update_attempt(h, run_id, **fields):
    allowed = {"status", "error", "environment", "prompt_sent", "trace_url", "video_url",
               "video_seconds", "recording_confirmed", "artifact_sha", "artifact_tree",
               "trace_path", "trace_sha256", "verification", "started_at", "prompt_sent_at",
               "finished_at",
               "recording_status", "recording_path", "recording_directory", "recording_error",
               "recording_queued_at", "recording_started_at", "recording_finished_at",
               "recording_duration", "recording_stage", "recording_stage_started_at"}
    if not fields or set(fields) - allowed:
        raise ValueError("Invalid attempt fields")
    with h.db_connection() as db:
        db.execute("UPDATE pairwise_attempts SET " + ",".join(f"{k}=?" for k in fields) + " WHERE id=?",
                   [json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
                    for v in fields.values()] + [run_id])


def parse_snapshot(h, value):
    match = re.fullmatch(r"(https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/commit/([0-9a-fA-F]{40})/?", value)
    if not match:
        raise h.WorkflowError("初始快照须为 GitHub /commit/ 地址，包含完整 40 位 SHA")
    return match[1], match[2].lower()


def _next_numbering(db, source_fields):
    sequence_number = int(db.execute(
        "SELECT COALESCE(MAX(sequence_number),0)+1 FROM pairwise_tasks"
    ).fetchone()[0])
    source_pair_id = str((source_fields or {}).get("source_pair_id") or "")
    if not source_pair_id:
        project_number = int(db.execute(
            "SELECT COALESCE(MAX(project_number),0)+1 FROM pairwise_tasks"
        ).fetchone()[0])
        return sequence_number, project_number, f"{project_number:04d}"

    parent = db.execute(
        "SELECT project_number FROM pairwise_tasks WHERE id=?",
        (source_pair_id,),
    ).fetchone()
    if not parent:
        raise RuntimeError("派生题来源不存在")
    project_number = int(parent["project_number"])
    prefix = f"{project_number:04d}-"
    suffixes = []
    for row in db.execute(
        "SELECT display_number FROM pairwise_tasks WHERE project_number=?",
        (project_number,),
    ).fetchall():
        label = str(row["display_number"] or "")
        if label.startswith(prefix) and label[len(prefix):].isdigit():
            suffixes.append(int(label[len(prefix):]))
    suffix = max(suffixes, default=0) + 1
    return sequence_number, project_number, f"{project_number:04d}-{suffix}"


def soft_delete(h, pair_id):
    """Hide a Pair from the task list while retaining every database record."""
    pair = task(h, pair_id)
    current = [latest(attempts(h, pair_id), side) for side in ("A", "B")]
    if any(item and item["status"] in (*ACTIVE, "upstream_paused") for item in current):
        raise h.WorkflowError("A/B 仍在执行或归档，不能从任务列表删除")
    if pair.get("deleted_at"):
        return pair
    now = h.now_text()
    with h.db_connection() as db:
        db.execute(
            "UPDATE pairwise_tasks SET deleted_at=?,updated_at=? WHERE id=?",
            (now, now, pair_id),
        )
    return task(h, pair_id)


def _migrate_numbering(db):
    """Separate durable creation order from the exact external project number."""
    rows = db.execute(
        """SELECT id,project_number,sequence_number FROM pairwise_tasks
           ORDER BY project_number,created_at,id"""
    ).fetchall()
    used = {int(row["sequence_number"]) for row in rows if int(row["sequence_number"] or 0) > 0}
    current = max(used, default=0)
    for row in rows:
        if int(row["sequence_number"] or 0) > 0:
            continue
        legacy = int(row["project_number"] or 0)
        candidate = legacy if legacy > 0 and legacy not in used else current + 1
        while candidate in used:
            candidate += 1
        used.add(candidate)
        current = max(current, candidate)
        db.execute(
            """UPDATE pairwise_tasks SET sequence_number=?,
               project_number=CASE WHEN project_number>0 THEN project_number ELSE ? END
               WHERE id=?""",
            (candidate, candidate, row["id"]),
        )

    legacy_labels = _legacy_display_numbers(db)
    for pair_id, label in legacy_labels.items():
        db.execute(
            "UPDATE pairwise_tasks SET display_number=? WHERE id=? AND display_number=''",
            (label, pair_id),
        )

    # Children intentionally share their root's numeric base, so the old global
    # uniqueness constraint belongs on sequence_number instead.
    db.execute("DROP INDEX IF EXISTS pairwise_tasks_project_number")
    for row in db.execute("SELECT id,display_number FROM pairwise_tasks").fetchall():
        match = re.fullmatch(r"(\d+)(?:-\d+)?", str(row["display_number"] or ""))
        if not match:
            raise RuntimeError(f"Pair {row['id']} 的持久化编号无效")
        db.execute(
            "UPDATE pairwise_tasks SET project_number=? WHERE id=?",
            (int(match.group(1)), row["id"]),
        )
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS pairwise_tasks_sequence_number "
        "ON pairwise_tasks(sequence_number) WHERE sequence_number>0"
    )
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS pairwise_tasks_display_number "
        "ON pairwise_tasks(display_number) WHERE display_number<>''"
    )


def _apply_pending_number_remap(h, db):
    """Apply an explicitly staged renumbering for roots that never started.

    The handover gate is responsible for holding queued workers while this
    startup migration runs. Keeping the request in settings makes the schema
    migration and the one-time data repair part of the same safe restart.
    """
    setting = db.execute(
        "SELECT value FROM settings WHERE key=?",
        (PENDING_NUMBER_REMAP_KEY,),
    ).fetchone()
    if not setting or not str(setting["value"] or "").strip():
        return
    try:
        remap = json.loads(setting["value"])
    except (TypeError, ValueError) as exc:
        raise RuntimeError("待执行的 Pair 编号迁移配置无效") from exc
    if not isinstance(remap, dict) or not remap:
        raise RuntimeError("待执行的 Pair 编号迁移配置必须是非空对象")

    prepared = []
    pair_ids = {str(pair_id) for pair_id in remap}
    targets = {str(label) for label in remap.values()}
    if len(pair_ids) != len(remap) or len(targets) != len(remap):
        raise RuntimeError("Pair 编号迁移的任务或目标编号重复")
    for pair_id, raw_label in remap.items():
        pair_id, label = str(pair_id), str(raw_label)
        if not re.fullmatch(r"\d{4,}", label):
            raise RuntimeError(f"Pair {pair_id} 的目标根编号无效")
        pair = db.execute(
            """SELECT id,repo_name,display_number,source_pair_id
               FROM pairwise_tasks WHERE id=?""",
            (pair_id,),
        ).fetchone()
        if not pair:
            raise RuntimeError(f"待迁移的 Pair {pair_id} 不存在")
        if str(pair["source_pair_id"] or ""):
            raise RuntimeError(f"Pair {pair_id} 是派生题，不能单独改根编号")
        conflict = db.execute(
            "SELECT id FROM pairwise_tasks WHERE display_number=? AND id<>?",
            (label, pair_id),
        ).fetchone()
        if conflict and str(conflict["id"]) not in pair_ids:
            raise RuntimeError(f"目标编号 {label} 已被其他 Pair 使用")
        attempts = db.execute(
            """SELECT a.status,a.prompt_sent,r.run_directory,r.repo_path
               FROM pairwise_attempts a JOIN runs r ON r.id=a.id
               WHERE a.pair_id=?""",
            (pair_id,),
        ).fetchall()
        if any(str(row["status"]) != "queued" or int(row["prompt_sent"] or 0) for row in attempts):
            raise RuntimeError(f"Pair {pair_id} 已开始执行，不能自动改编号")
        old_folder = f"{pair['display_number']}-{pair['repo_name']}"
        new_folder = f"{label}-{pair['repo_name']}"
        for row in attempts:
            directory = Path(str(row["run_directory"] or ""))
            if directory.parent.name == old_folder and directory.parent.exists():
                raise RuntimeError(f"Pair {pair_id} 已存在工作目录，不能自动改编号")
        prepared.append((pair_id, label, old_folder, new_folder))

    # Temporary labels allow a validated mapping to swap numbers atomically.
    for pair_id, _, _, _ in prepared:
        db.execute(
            "UPDATE pairwise_tasks SET display_number=? WHERE id=?",
            (f"__number_remap__{pair_id}", pair_id),
        )
    for pair_id, label, old_folder, new_folder in prepared:
        db.execute(
            "UPDATE pairwise_tasks SET project_number=?,display_number=? WHERE id=?",
            (int(label), label, pair_id),
        )
        for run in db.execute(
            """SELECT r.id,r.run_directory,r.repo_path
               FROM runs r JOIN pairwise_attempts a ON a.id=r.id
               WHERE a.pair_id=?""",
            (pair_id,),
        ).fetchall():
            old_segment, new_segment = f"/{old_folder}/", f"/{new_folder}/"
            run_directory = str(run["run_directory"] or "").replace(old_segment, new_segment, 1)
            repo_path = str(run["repo_path"] or "").replace(old_segment, new_segment, 1)
            db.execute(
                "UPDATE runs SET run_directory=?,repo_path=? WHERE id=?",
                (run_directory, repo_path, run["id"]),
            )
        for attempt in db.execute(
            """SELECT id,recording_path,recording_directory FROM pairwise_attempts
               WHERE pair_id=?""",
            (pair_id,),
        ).fetchall():
            old_segment, new_segment = f"/{old_folder}/", f"/{new_folder}/"
            db.execute(
                """UPDATE pairwise_attempts SET recording_path=?,recording_directory=?
                   WHERE id=?""",
                (str(attempt["recording_path"] or "").replace(old_segment, new_segment, 1),
                 str(attempt["recording_directory"] or "").replace(old_segment, new_segment, 1),
                 attempt["id"]),
            )
    now = h.now_text()
    db.execute(
        "UPDATE settings SET value='',updated_at=? WHERE key=?",
        (now, PENDING_NUMBER_REMAP_KEY),
    )
    db.execute(
        """INSERT INTO settings(key,value,updated_at) VALUES(?,?,?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
        (NUMBER_REMAP_AUDIT_KEY, json.dumps(remap, ensure_ascii=False, sort_keys=True), now),
    )
