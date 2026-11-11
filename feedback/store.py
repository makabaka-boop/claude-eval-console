"""Additive storage. One row per Session; continuations never replace first IDs."""
import hashlib
import json
import sqlite3
import tempfile
import uuid
from pathlib import Path

from . import policy


def backup_database(path):
    directory=Path(path).parent/'backups'
    directory.mkdir(parents=True,exist_ok=True)
    target=directory/'before-feedback-202611-v1.sqlite3'
    if target.exists():return target
    with tempfile.TemporaryDirectory(dir=directory) as work:
        temporary=Path(work)/'database.sqlite3'
        with sqlite3.connect(path) as source,sqlite3.connect(temporary) as destination:
            source.backup(destination)
        temporary.replace(target)
    return target


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def initialize(h):
    with h.db_connection() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS feedback_profiles (
          operator TEXT PRIMARY KEY, domain TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS feedback_tasks (
          id TEXT PRIMARY KEY, operator TEXT NOT NULL, domain TEXT NOT NULL,
          subdirection TEXT NOT NULL, task_type TEXT NOT NULL, difficulty TEXT NOT NULL,
          title TEXT NOT NULL, prompt TEXT NOT NULL, languages TEXT NOT NULL,
          reproducibility TEXT NOT NULL DEFAULT '', repo_url TEXT NOT NULL,
          initial_sha TEXT NOT NULL, repo_name TEXT NOT NULL,
          attachments TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS feedback_runs (
          id TEXT PRIMARY KEY REFERENCES runs(id), task_id TEXT NOT NULL REFERENCES feedback_tasks(id),
          status TEXT NOT NULL DEFAULT 'queued', detail TEXT NOT NULL DEFAULT '',
          error TEXT NOT NULL DEFAULT '', config TEXT NOT NULL DEFAULT '{}',
          prompt_sent INTEGER NOT NULL DEFAULT 0, first_session_id TEXT NOT NULL DEFAULT '',
          first_turn_id TEXT NOT NULL DEFAULT '', trace_path TEXT NOT NULL DEFAULT '',
          trace_url TEXT NOT NULL DEFAULT '', recording_url TEXT NOT NULL DEFAULT '',
          trace_sha256 TEXT NOT NULL DEFAULT '', trace_state TEXT NOT NULL DEFAULT '{}',
          artifact_sha TEXT NOT NULL DEFAULT '', artifact_tree TEXT NOT NULL DEFAULT '',
          recording_path TEXT NOT NULL DEFAULT '', recording_sha256 TEXT NOT NULL DEFAULT '',
          recording_seconds REAL, recording_confirmed INTEGER NOT NULL DEFAULT 0,
          recording_artifact_sha TEXT NOT NULL DEFAULT '',
          analysis_status TEXT NOT NULL DEFAULT 'pending', analysis TEXT NOT NULL DEFAULT '{}',
          analysis_model TEXT NOT NULL DEFAULT '', analysis_effort TEXT NOT NULL DEFAULT '',
          analysis_source TEXT NOT NULL DEFAULT '', analysis_error TEXT NOT NULL DEFAULT '',
          delivery_score INTEGER, feedback TEXT NOT NULL DEFAULT '',
          result_feedback TEXT NOT NULL DEFAULT '', process_feedback TEXT NOT NULL DEFAULT '',
          other_issues TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
          review_confirmed INTEGER NOT NULL DEFAULT 0,
          review_source TEXT NOT NULL DEFAULT '', resume_count INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS feedback_runs_task ON feedback_runs(task_id,created_at);
        CREATE UNIQUE INDEX IF NOT EXISTS feedback_runs_session ON feedback_runs(first_session_id)
            WHERE first_session_id<>'';
        CREATE TABLE IF NOT EXISTS feedback_continuations (
          run_id TEXT NOT NULL REFERENCES feedback_runs(id), number INTEGER NOT NULL,
          prompt_id TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'sending', created_at TEXT NOT NULL,
          PRIMARY KEY(run_id,number)
        );
        CREATE TABLE IF NOT EXISTS feedback_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES feedback_runs(id),
          message TEXT NOT NULL, created_at TEXT NOT NULL
        );
        """)
        # Additive, idempotent migration for development snapshots as well.
        columns={r[1] for r in db.execute('PRAGMA table_info(feedback_runs)')}
        for name in ('trace_url','recording_url','result_feedback','process_feedback'):
            if name not in columns:
                db.execute(f"ALTER TABLE feedback_runs ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")


def profile(h):
    with h.db_connection() as db:
        row = db.execute("SELECT * FROM feedback_profiles WHERE operator=?", (h.SUBMITTER_NAME,)).fetchone()
    return {"operator": h.SUBMITTER_NAME, "domain": row["domain"] if row else "", "locked": bool(row)}


def select_domain(h, domain):
    if domain not in policy.DOMAINS:
        raise h.WorkflowError("请选择三个专项领域之一")
    with h.db_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        old = db.execute("SELECT domain FROM feedback_profiles WHERE operator=?", (h.SUBMITTER_NAME,)).fetchone()
        if old and old["domain"] != domain:
            raise h.WorkflowError("本期个人领域已固定，不能随任务切换；修改须重新确认本期配置")
        db.execute("INSERT OR IGNORE INTO feedback_profiles VALUES (?,?,?)", (h.SUBMITTER_NAME, domain, h.now_text()))
    return profile(h)


def create(h, payload):
    operator = profile(h)
    if not operator["domain"]:
        raise h.WorkflowError("实际出题前请选择并固定本期个人领域")
    domain = payload.get("domain", operator["domain"])
    if domain != operator["domain"]:
        raise h.WorkflowError("任务领域必须与本期个人领域一致")
    subdirection = str(payload.get("subdirection") or "")
    if subdirection not in policy.DOMAINS[domain]:
        raise h.WorkflowError("子方向不属于选定领域")
    kind, difficulty = payload.get("task_type"), payload.get("difficulty")
    if kind not in policy.TASK_TYPES or difficulty not in policy.DIFFICULTIES:
        raise h.WorkflowError("请选择本期任务类型，难度仅允许困难或地狱")
    title, prompt, languages = (str(payload.get(k) or "") for k in ("title", "prompt", "languages"))
    if not title.strip() or not prompt.strip() or not languages.strip():
        raise h.WorkflowError("标题、Prompt 原文和语言/框架不能为空")
    if len(title) > 200 or len(prompt) > 500_000:
        raise h.WorkflowError("标题或 Prompt 超出保存范围")
    reproducibility = str(payload.get("reproducibility") or "")
    if reproducibility and reproducibility not in policy.REPRODUCIBILITY:
        raise h.WorkflowError("环境可复现等级无效，也可留空")
    try:
        repo_url, sha, repo_name = policy.snapshot(payload.get("initial_snapshot"))
    except ValueError as exc:
        raise h.WorkflowError(str(exc)) from exc
    task_id = uuid.uuid4().hex[:12]
    with h.db_connection() as db:
        db.execute("""INSERT INTO feedback_tasks
          (id,operator,domain,subdirection,task_type,difficulty,title,prompt,languages,
           reproducibility,repo_url,initial_sha,repo_name,created_at)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (task_id,h.SUBMITTER_NAME,domain,subdirection,kind,difficulty,title,prompt,languages,
           reproducibility,repo_url,sha,repo_name,h.now_text()))
    return task(h, task_id)


def task(h, task_id):
    with h.db_connection() as db:
        row = db.execute("SELECT * FROM feedback_tasks WHERE id=? AND operator=?", (task_id,h.SUBMITTER_NAME)).fetchone()
        runs = db.execute("SELECT id FROM feedback_runs WHERE task_id=? ORDER BY created_at,id", (task_id,)).fetchall()
    if not row:
        raise h.WorkflowError("没有找到专项题目")
    result = dict(row)
    result["attachments"] = json.loads(result["attachments"])
    result["initial_snapshot"] = f"{result['repo_url']}/commit/{result['initial_sha']}"
    result["runs"] = [run(h, r["id"]) for r in runs]
    return result


def run(h, run_id):
    with h.db_connection() as db:
        row = db.execute("""SELECT r.* FROM feedback_runs r JOIN feedback_tasks t ON t.id=r.task_id
                            WHERE r.id=? AND t.operator=?""", (run_id,h.SUBMITTER_NAME)).fetchone()
        continuations = db.execute("SELECT * FROM feedback_continuations WHERE run_id=? ORDER BY number", (run_id,)).fetchall()
        events = db.execute("SELECT message,created_at FROM feedback_events WHERE run_id=? ORDER BY id DESC LIMIT 30", (run_id,)).fetchall()
    if not row:
        raise h.WorkflowError("没有找到专项执行记录")
    result = dict(row)
    for key in ("config", "analysis", "trace_state"):
        result[key] = json.loads(result[key])
    # Freeze contains only a credential fingerprint, never the actual key.
    result["config"].pop("credential", None)
    result["continuations"] = [dict(r) for r in continuations]
    result["events"] = [dict(r) for r in events]
    return result


def raw_config(h, run_id):
    with h.db_connection() as db:
        row = db.execute("SELECT config FROM feedback_runs WHERE id=?", (run_id,)).fetchone()
    return json.loads(row["config"]) if row else {}


def listing(h, query):
    page = max(1, int(query.get("page", [1])[0]))
    search = str(query.get("query", [""])[0])[:200]
    kind = str(query.get("task_type", [""])[0])
    where = "operator=? AND (title LIKE ? OR prompt LIKE ?)"
    args = [h.SUBMITTER_NAME, f"%{search}%", f"%{search}%"]
    if kind:
        where += " AND task_type=?"
        args.append(kind)
    with h.db_connection() as db:
        total = db.execute(f"SELECT COUNT(*) FROM feedback_tasks WHERE {where}", args).fetchone()[0]
        pages = max(1, (total + 19) // 20)
        page = min(page, pages)
        rows = db.execute(f"SELECT id FROM feedback_tasks WHERE {where} ORDER BY created_at DESC,id DESC LIMIT 20 OFFSET ?", [*args,(page-1)*20]).fetchall()
    return {"items": [task(h,row["id"]) for row in rows], "total":total, "page":page,"pages":pages}


RUN_FIELDS = {"status","detail","error","config","prompt_sent","first_session_id","first_turn_id",
              "trace_path","trace_sha256","trace_state","artifact_sha","artifact_tree",
              "recording_path","recording_sha256","recording_seconds","recording_confirmed","recording_artifact_sha",
              "analysis_status","analysis","analysis_model","analysis_effort","analysis_source","analysis_error",
              "delivery_score","feedback","other_issues","notes","review_confirmed","review_source","resume_count"}
RUN_FIELDS.update({'trace_url','recording_url','result_feedback','process_feedback'})


def update(h, run_id, **fields):
    if not fields or set(fields) - RUN_FIELDS:
        raise ValueError("Invalid feedback run update")
    for key in ("config", "analysis", "trace_state"):
        if key in fields:
            fields[key] = dumps(fields[key])
    fields["updated_at"] = h.now_text()
    with h.db_connection() as db:
        db.execute("UPDATE feedback_runs SET " + ",".join(f"{key}=?" for key in fields) + " WHERE id=?", [*fields.values(),run_id])


def event(h, run_id, message):
    with h.db_connection() as db:
        db.execute("INSERT INTO feedback_events(run_id,message,created_at) VALUES (?,?,?)", (run_id,str(message),h.now_text()))


def fingerprint(h, run_id):
    r = run(h, run_id)
    t = task(h, r["task_id"])
    value = {"version":policy.VERSION, "prompt":t["prompt"], "initial":t["initial_sha"],
             "artifact":r["artifact_sha"], "trace":r["trace_sha256"],
             "session":r["first_session_id"], "turn":r["first_turn_id"],
             "recording":r["recording_sha256"],"recording_seconds":r["recording_seconds"],
             "recording_artifact":r["recording_artifact_sha"],
             "continuations":[(c["number"],c["prompt_id"],c["reason"],c["status"]) for c in r["continuations"]],
             "attachments":[(a["id"],a["sha256"]) for a in t["attachments"]],
             "model":policy.ANALYSIS_MODEL, "effort":policy.ANALYSIS_EFFORT}
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def evidence_root(h):
    root = Path(h.DATA_DIR) / "feedback"
    root.mkdir(parents=True, exist_ok=True)
    return root
