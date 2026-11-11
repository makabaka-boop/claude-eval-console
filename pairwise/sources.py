"""Single-side baseline eligibility and durable, per-parent/per-type reservations."""
import hashlib
import json
import re
import time
import uuid
from datetime import datetime
from pathlib import Path

from . import generation_policy, models as m, validation
from .environment import digest

TYPES = ("Feature 迭代", "Bug 修复")
LEASE_SECONDS = 180
COOLDOWN_SECONDS = 30 * 60
DIRECTION_LIMIT = 3
DIFFICULTY_FAILURE_LIMIT = generation_policy.BUG_STRICT_ATTEMPTS
BUG_DIRECTION_REVIEW_POLICY = 'concrete-bug-chain-v1'
BUG_LINEAGE_MODE_SETTING = 'pairwise_auto_bug_lineage_mode'
BUG_SOURCE_EXHAUSTION_MARKER = '本来源未找到可合规预置的困难缺陷：'
BUG_SOURCE_EXHAUSTION_MARKERS = (
    BUG_SOURCE_EXHAUSTION_MARKER,
    '本来源未找到可合规预置的缺陷方向：',
    '本来源 V2 设计卡均命中常规修法高风险，植入前已换源码：',
    '本来源 V2 的候选在冻结难度盲审中均为简单或中等，已立即轮换源码：',
)
BUG_DIFFICULTY_FAILURE_MARKERS = (
    'V2 前两轮严格准入未找到合格候选',
    'V2 前两轮严格准入未找到可植入候选',
    '前两轮严格准入未通过',
    'V2 难度盲审未达到困难或地狱',
    '最小正确方案未达到困难',
    '完整定位、修复和验证路径仍属于常规方案',
)
TRANSIENT_COOLDOWN_MARKERS = (
    '超时', '超过 20 分钟', '超过 25 分钟', '45 分钟绝对上限', '上游模型容量', '容量不足',
    '名额已满', '限流', 'rate limit', '验证环境', 'Docker daemon',
    '端口已被占用', '补丁无法应用', '补丁修正后仍无法应用',
)


def exhausted_state():
    """Scope a negative source scan to the generation policy that produced it."""
    return f'exhausted:{generation_policy.VERSION}'


def initialize(db):
    db.executescript("""
      CREATE TABLE IF NOT EXISTS pairwise_source_locks (
        source_pair_id TEXT NOT NULL, task_type TEXT NOT NULL,
        source_root_pair_id TEXT NOT NULL DEFAULT '',
        source_attempt_id TEXT NOT NULL, source_side TEXT NOT NULL,
        source_artifact_sha TEXT NOT NULL, source_artifact_tree TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'ready', token TEXT NOT NULL DEFAULT '',
        lease_until REAL NOT NULL DEFAULT 0, cooldown_until REAL NOT NULL DEFAULT 0,
        reason TEXT NOT NULL DEFAULT '', child_pair_id TEXT NOT NULL DEFAULT '',
        failure_count INTEGER NOT NULL DEFAULT 0, direction_checkpoint INTEGER NOT NULL DEFAULT 0,
        difficulty_failure_count INTEGER NOT NULL DEFAULT 0,
        difficulty_policy_version TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY(source_pair_id,task_type)
      );
      CREATE TABLE IF NOT EXISTS pairwise_source_directions (
        source_pair_id TEXT NOT NULL, task_type TEXT NOT NULL, fingerprint TEXT NOT NULL,
        identity TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(source_pair_id,task_type,fingerprint)
      );
      CREATE TABLE IF NOT EXISTS pairwise_source_direction_reviews (
        source_pair_id TEXT NOT NULL, task_type TEXT NOT NULL, fingerprint TEXT NOT NULL,
        policy_version TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(source_pair_id,task_type,fingerprint,policy_version)
      );
    """)
    lock_columns = {row[1] for row in db.execute('PRAGMA table_info(pairwise_source_locks)')}
    if 'source_root_pair_id' not in lock_columns:
        db.execute("ALTER TABLE pairwise_source_locks ADD COLUMN source_root_pair_id TEXT NOT NULL DEFAULT ''")
    if 'difficulty_failure_count' not in lock_columns:
        db.execute("ALTER TABLE pairwise_source_locks ADD COLUMN difficulty_failure_count INTEGER NOT NULL DEFAULT 0")
    if 'difficulty_policy_version' not in lock_columns:
        db.execute("ALTER TABLE pairwise_source_locks ADD COLUMN difficulty_policy_version TEXT NOT NULL DEFAULT ''")
    # Retain root identities for history and source rotation. Bug generation
    # may now run on distinct verified products within one lineage.
    db.execute("""WITH RECURSIVE lineage(id,root_id) AS (
          SELECT id,id FROM pairwise_tasks WHERE source_pair_id=''
          UNION ALL
          SELECT child.id,lineage.root_id FROM pairwise_tasks AS child
          JOIN lineage ON child.source_pair_id=lineage.id
        )
        UPDATE pairwise_source_locks
        SET source_root_pair_id=COALESCE(
          (SELECT root_id FROM lineage WHERE lineage.id=pairwise_source_locks.source_pair_id LIMIT 1),
          source_pair_id)
        WHERE source_root_pair_id=''""")
    db.execute("""CREATE INDEX IF NOT EXISTS pairwise_source_lineage_lease_idx
        ON pairwise_source_locks(source_root_pair_id,task_type,state,lease_until)""")
    # Older policies put ordinary Bug quality rejections into a 30-minute
    # cooldown. Release those locks on upgrade; keep only genuine operational
    # cooldowns such as capacity, timeout, or environment failures.
    active_bug_cooldowns = db.execute("""SELECT source_pair_id,reason
        FROM pairwise_source_locks
        WHERE task_type='Bug 修复' AND state='cooldown' AND cooldown_until>0""").fetchall()
    for row in active_bug_cooldowns:
        if not transient_cooldown_reason(row['reason']):
            db.execute("""UPDATE pairwise_source_locks
                SET state='ready',cooldown_until=0
                WHERE source_pair_id=? AND task_type='Bug 修复' AND state='cooldown'""",
                (row['source_pair_id'],))
    # seed_design and the older seeded_bug path historically used different
    # wording for the same terminal result.  Before this migration only the
    # latter was recognized, so a thoroughly scanned source could return to
    # the queue indefinitely.  Preserve active leases, but retire completed
    # no-direction scans immediately under the current policy.
    stale_no_direction = db.execute("""SELECT source_pair_id,reason
        FROM pairwise_source_locks
        WHERE task_type='Bug 修复' AND state IN ('ready','cooldown')""").fetchall()
    for row in stale_no_direction:
        if source_exhaustion_reason(row['reason']):
            db.execute("""UPDATE pairwise_source_locks
                SET state=?,cooldown_until=0,failure_count=0,updated_at=?
                WHERE source_pair_id=? AND task_type='Bug 修复'
                  AND state IN ('ready','cooldown')""",
                (exhausted_state(), datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %z'),
                 row['source_pair_id']))
    # Backfill only when BOTH the precise attempt and the parent's one child
    # are unambiguous. Historical duplicates remain untouched and consumed().
    rows = db.execute("""SELECT c.id,c.task_type,a.pair_id,a.id AS attempt_id,a.side,
                          a.artifact_sha,a.artifact_tree
        FROM pairwise_tasks c JOIN pairwise_tasks p ON c.source_repo_url=p.repo_url
        JOIN pairwise_attempts a ON a.pair_id=p.id AND a.artifact_sha=c.source_base_sha
        WHERE c.source_pair_id='' AND c.source_base_sha<>'' AND c.task_type IN ('Feature 迭代','Bug 修复')
        """).fetchall()
    grouped = {}
    for row in rows:
        grouped.setdefault(row['id'], []).append(row)
    for matches in grouped.values():
        if len(matches) != 1:
            continue
        r = matches[0]
        if consumed(db, r['pair_id'], r['task_type'], exclude=r['id']):
            continue
        db.execute("""UPDATE pairwise_tasks SET source_pair_id=?,source_attempt_id=?,source_side=?,
                   source_artifact_sha=?,source_artifact_tree=? WHERE id=? AND source_pair_id=''""",
                   (r['pair_id'], r['attempt_id'], r['side'], r['artifact_sha'], r['artifact_tree'], r['id']))
    db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS pairwise_source_child_uq
                  ON pairwise_tasks(source_pair_id,task_type)
                  WHERE source_pair_id<>'' AND task_type IN ('Feature 迭代','Bug 修复')""")


def consumed(db, pair_id, kind, exclude=''):
    # Soft deletion never frees a source; also covers unverifiable old metadata.
    return bool(db.execute("""SELECT 1 FROM pairwise_tasks c WHERE c.task_type=? AND c.id<>?
        AND (c.source_pair_id=? OR (c.source_base_sha<>'' AND EXISTS (
          SELECT 1 FROM pairwise_tasks p JOIN pairwise_attempts a ON a.pair_id=p.id
          WHERE p.id=? AND p.repo_url=c.source_repo_url AND a.artifact_sha=c.source_base_sha))) LIMIT 1""",
        (kind, exclude, pair_id, pair_id)).fetchone())


def verification_passed(pair, attempt):
    checks, commands = attempt.get('verification') or [], pair.get('verification_commands') or []
    return bool(commands and checks and isinstance(checks, list)
                and all(isinstance(c, dict) and type(c.get('exit_code')) is int and c['exit_code'] == 0
                        for c in checks)
                and [c.get('command') for c in checks] == commands)


def eligibility_issues(h, pair, a):
    problems = []
    if not a or a.get('status') != 'complete' or not a.get('finished_at'):
        return ['本侧尚未完成归档']
    if not verification_passed(pair, a):
        problems.append('固定验收必须至少执行一项且全部通过，不允许缺项')
    config = pair.get('config') or {}
    expected = {'fingerprint': digest(config), 'base_sha': pair.get('base_sha'),
                'prompt_sha256': hashlib.sha256(pair['prompt'].encode()).hexdigest()}
    if (config.get('model') != m.AUTO_MODEL or config.get('context_tokens') != 1_000_000
            or any((a.get('environment') or {}).get(k) != v for k, v in expected.items())):
        problems.append('本侧环境校验与冻结配置不一致')
    if problems:
        return problems  # Failed checks must not repeatedly scan large trace files.
    try:
        path = Path(a.get('trace_path') or '')
        if path.suffix.lower() != '.jsonl':
            problems.append('来源轨迹必须是原始 .jsonl 文件')
        problems.extend(validation.trace_issues(h, path, pair, a.get('session_id')))
        if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() != a.get('trace_sha256'):
            problems.append('原始 JSONL 摘要不一致')
        if not all(re.fullmatch(r'[0-9a-f]{40}', str(value or '')) for value in
                   (pair.get('base_sha'), a.get('artifact_sha'), a.get('artifact_tree'))):
            problems.append('缺少完整产物 SHA、tree 或初始 SHA')
        else:
            work = Path(a['repo_path'])
            result = h.run_command(['git', 'show', '-s', '--format=%P%n%T', a['artifact_sha']], cwd=work).stdout.splitlines()
            if result != [pair['base_sha'], a['artifact_tree']]:
                problems.append('产物唯一父提交或 Git tree 不合法')
            for flags in ([], ['--cached']):
                if h.run_command(['git', '--no-optional-locks', 'diff', '--quiet', *flags,
                                  a['artifact_sha'], '--'], cwd=work, check=False).returncode:
                    problems.append('已归档工作区与产物不一致')
                    break
        with h.db_connection() as db:
            turns = db.execute('SELECT turn_number,prompt FROM run_turns WHERE run_id=?', (a['id'],)).fetchall()
        if len(turns) != 1 or turns[0]['turn_number'] != 1 or turns[0]['prompt'] != pair['prompt']:
            problems.append('本侧数据库记录不满足单轮约束')
    except (OSError, ValueError, TypeError, h.WorkflowError):
        problems.append('本侧归档证据或工作区无法核验')
    return problems


def completion_key(a):
    try:
        # app.now_text() stores a space before %z, which fromisoformat() does
        # not accept on the Python 3.9 runtime used by the service.
        epoch = datetime.strptime(a['finished_at'], '%Y-%m-%d %H:%M:%S %z').timestamp()
    except (ValueError, TypeError, KeyError):
        try:
            epoch = datetime.fromisoformat(a['finished_at']).timestamp()
        except (ValueError, TypeError, KeyError):
            epoch = float('inf')
    return epoch, str(a.get('side', '')), str(a.get('id', ''))


def get_lock(h, pair_id, kind):
    with h.db_connection() as db:
        row = db.execute('SELECT * FROM pairwise_source_locks WHERE source_pair_id=? AND task_type=?',
                         (pair_id, kind)).fetchone()
    return dict(row) if row else None


def unavailable(lock):
    return bool(lock and (lock['state'] in {'created', 'incompatible', exhausted_state()}
                          or lock['cooldown_until'] > time.time()
                          or (lock['state'] == 'generating' and lock['lease_until'] > time.time())))


def lineage_root_id(db, source_pair_id):
    """Resolve either a Pair-wise or legacy source to its durable root id."""
    source_pair_id = str(source_pair_id or '')
    if not source_pair_id:
        return ''
    legacy = source_pair_id.removeprefix('legacy:') if source_pair_id.startswith('legacy:') else ''
    if legacy:
        current, seen = legacy, set()
        while current and current not in seen:
            seen.add(current)
            row = db.execute('SELECT source_run_id FROM runs WHERE id=?', (current,)).fetchone()
            parent = str(row['source_run_id'] or '') if row else ''
            if not parent:
                return f'legacy:{current}'
            current = parent
        return f'legacy:{legacy}'
    current, seen = source_pair_id, set()
    while current and current not in seen:
        seen.add(current)
        row = db.execute('SELECT source_pair_id FROM pairwise_tasks WHERE id=?', (current,)).fetchone()
        parent = str(row['source_pair_id'] or '') if row else ''
        if not parent or parent.startswith('legacy:'):
            return current
        current = parent
    return source_pair_id


def lineage_consumed(db, root_pair_id, kind):
    """Return whether a root lineage already produced this terminal task type.

    Bug generation is terminal: an existing Bug anywhere in the project
    lineage consumes the entire root, even when the child was soft-deleted or
    predates source-lock bookkeeping.  Other continuation types retain their
    existing multi-iteration behavior.
    """
    if kind != 'Bug 修复' or not root_pair_id:
        return False
    if db.execute("""SELECT 1 FROM pairwise_source_locks
            WHERE source_root_pair_id=? AND task_type=? AND state='created' LIMIT 1""",
            (root_pair_id, kind)).fetchone():
        return True
    if str(root_pair_id).startswith('legacy:'):
        run_id = str(root_pair_id).removeprefix('legacy:')
        return bool(db.execute("""WITH RECURSIVE descendants(id) AS (
                SELECT id FROM runs WHERE id=?
                UNION
                SELECT child.id FROM runs AS child
                JOIN descendants AS parent ON child.source_run_id=parent.id
            )
            SELECT 1 FROM (
                SELECT run.id FROM runs AS run JOIN descendants ON descendants.id=run.id
                WHERE run.task_type IN ('Bug 修复','Bug 修复重跑')
                UNION ALL
                SELECT task.id FROM pairwise_tasks AS task
                WHERE task.task_type='Bug 修复'
                  AND task.source_pair_id IN (
                    SELECT 'legacy:' || id FROM descendants
                  )
            ) LIMIT 1""", (run_id,)).fetchone())
    return bool(db.execute("""WITH RECURSIVE descendants(id) AS (
            SELECT id FROM pairwise_tasks WHERE id=?
            UNION
            SELECT child.id FROM pairwise_tasks AS child
            JOIN descendants AS parent ON child.source_pair_id=parent.id
        )
        SELECT 1 FROM pairwise_tasks AS task
        JOIN descendants ON descendants.id=task.id
        WHERE task.task_type='Bug 修复' LIMIT 1""", (root_pair_id,)).fetchone())


def lineage_unavailable(h, root_pair_id, kind):
    """Keep lineage-wide reservations only for task types that require them."""
    if not root_pair_id:
        return False
    # Bug sources are frozen per product. A different verified product in the
    # same lineage remains eligible while another Bug is being generated.
    if kind == 'Bug 修复' and bug_lineage_reuse_enabled(h):
        return False
    with h.db_connection() as db:
        if (not bug_lineage_reuse_enabled(h, db)
                and lineage_consumed(db, root_pair_id, kind)):
            return True
        row = db.execute("""SELECT 1 FROM pairwise_source_locks
            WHERE source_root_pair_id=? AND task_type=? AND state='generating'
              AND lease_until>? LIMIT 1""", (root_pair_id, kind, time.time())).fetchone()
    return bool(row)


def latest_lineage_product_id(db, root_pair_id):
    """Return the newest Pair in one lineage, including unfinished children.

    Including unfinished children prevents a caller from falling back to an
    older snapshot while the newest product is still being archived.
    """
    if not root_pair_id or str(root_pair_id).startswith('legacy:'):
        return ''
    row = db.execute("""WITH RECURSIVE descendants(id) AS (
            SELECT id FROM pairwise_tasks WHERE id=?
            UNION
            SELECT child.id FROM pairwise_tasks AS child
            JOIN descendants AS parent ON child.source_pair_id=parent.id
        )
        SELECT task.id FROM pairwise_tasks AS task
        JOIN descendants ON descendants.id=task.id
        WHERE task.deleted_at IS NULL
        ORDER BY task.sequence_number DESC,task.created_at DESC,task.id DESC LIMIT 1""",
        (root_pair_id,)).fetchone()
    return str(row['id']) if row else ''


def claim(h, source, kind):
    pair, a = source['pair'], source['attempt']
    root_pair_id = str(source.get('root_pair_id') or pair['id'])
    if source.get('legacy_run_id'):
        from . import legacy_baselines
        current = legacy_baselines.by_id(h, source['legacy_run_id'],
            expected_sha=a['artifact_sha'], expected_tree=a['artifact_tree'])
        if not current or current['pair']['id'] != pair['id']:
            raise h.WorkflowError('zzzz 旧项目基线已变化或验收记录不完整')
    else:
        with h.db_connection() as db:
            root_pair_id = lineage_root_id(db, pair['id'])
        issues = eligibility_issues(h, pair, a)
        if issues:
            raise h.WorkflowError('来源资格发生变化：' + '；'.join(issues))
    if kind == 'Bug 修复':
        # The isolated difficulty export has the same restriction. Check it
        # before spending any model call on design, patching or prompt review.
        checked_attempt = current['attempt'] if source.get('legacy_run_id') else a
        issue = _snapshot_compatibility_issue(h, pair, checked_attempt)
        if issue:
            _mark_snapshot_incompatible(h, source, kind, issue)
            return None
    now, token = time.time(), uuid.uuid4().hex
    with h.db_connection() as db:
        db.execute('BEGIN IMMEDIATE')
        lineage_reuse = bug_lineage_reuse_enabled(h, db)
        if not lineage_reuse and lineage_consumed(db, root_pair_id, kind):
            return None
        if consumed(db, pair['id'], kind):
            return None
        # Keep the lineage fence for types that need it. Bug claims rely on
        # the exact parent lock and consumed() instead.
        if (kind != 'Bug 修复' and db.execute("""SELECT 1 FROM pairwise_source_locks
                WHERE source_root_pair_id=? AND task_type=? AND state='generating'
                  AND lease_until>? LIMIT 1""", (root_pair_id, kind, now)).fetchone()):
            return None
        row = db.execute('SELECT * FROM pairwise_source_locks WHERE source_pair_id=? AND task_type=?',
                         (pair['id'], kind)).fetchone()
        if row and (unavailable(row) or row['source_attempt_id'] != a['id']
                    or row['source_artifact_sha'] != a['artifact_sha']):
            return None
        db.execute("""INSERT OR IGNORE INTO pairwise_source_locks
            (source_pair_id,task_type,source_root_pair_id,source_attempt_id,source_side,
             source_artifact_sha,source_artifact_tree,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?)""", (pair['id'], kind, root_pair_id, a['id'], a['side'],
                                             a['artifact_sha'], a['artifact_tree'], h.now_text(), h.now_text()))
        db.execute("""UPDATE pairwise_source_locks SET source_root_pair_id=?,state='generating',token=?,lease_until=?,
            cooldown_until=0,updated_at=? WHERE source_pair_id=? AND task_type=?""",
                   (root_pair_id, token, now + LEASE_SECONDS, h.now_text(), pair['id'], kind))
        difficulty_failures = (
            int(row['difficulty_failure_count'] or 0)
            if row and str(row['difficulty_policy_version'] or '') == generation_policy.VERSION else 0
        )
    return {
        **source,
        'reservation_token': token,
        'lineage_reuse': lineage_reuse,
        'task_type': kind,
        'difficulty_failure_count': difficulty_failures,
        'bug_admission_attempt': difficulty_failures + 1,
        'bug_admission_mode': 'hard_only',
    }


def renew(h, source):
    with h.db_connection() as db:
        return db.execute("""UPDATE pairwise_source_locks SET lease_until=?
            WHERE source_pair_id=? AND task_type=? AND token=? AND state='generating'""",
            (time.time() + LEASE_SECONDS, source['pair']['id'], source['task_type'],
             source['reservation_token'])).rowcount == 1


def release(h, source, reason='', failed=False, cooldown_after_attempt=False):
    if not source or not source.get('reservation_token'):
        return
    with h.db_connection() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute("""SELECT * FROM pairwise_source_locks WHERE source_pair_id=? AND task_type=?
            AND token=? AND state='generating'""", (source['pair']['id'], source['task_type'],
                                                    source['reservation_token'])).fetchone()
        if not row:
            return
        count = db.execute('SELECT COUNT(*) FROM pairwise_source_directions WHERE source_pair_id=? AND task_type=?',
                           (source['pair']['id'], source['task_type'])).fetchone()[0]
        failures = row['failure_count'] + int(failed)
        current_policy = generation_policy.VERSION
        difficulty_failures = (
            int(row['difficulty_failure_count'] or 0)
            if str(row['difficulty_policy_version'] or '') == current_policy else 0
        )
        # Rolling upgrades did not have a dedicated counter. Preserve one
        # immediately preceding difficulty failure so a known-bad source does
        # not consume two more complete rounds after deployment.
        if (not difficulty_failures and not row['difficulty_policy_version']
                and int(row['failure_count'] or 0) > 0 and _difficulty_failure(row['reason'])):
            difficulty_failures = 1
        hard_difficulty_failure = (
            failed
            and source['task_type'] == 'Bug 修复'
            and _difficulty_failure(reason)
        )
        if hard_difficulty_failure:
            difficulty_failures += 1
        exhausted = (failed and source['task_type'] == 'Bug 修复'
                     and source_exhaustion_reason(reason))
        direction_rotation_required = (
            hard_difficulty_failure and difficulty_failures >= DIFFICULTY_FAILURE_LIMIT
        )
        # Bug quality failures rotate by failure_count/rejected directions and
        # remain immediately eligible. Only an explicitly classified transient
        # failure may cool a Bug source. Feature keeps its existing bounded
        # retry behavior.
        automatic_quality_cooldown = (
            source['task_type'] != 'Bug 修复'
            and (count - row['direction_checkpoint'] >= DIRECTION_LIMIT
                 or failures >= DIRECTION_LIMIT)
        )
        cooling = not exhausted and failed and (
            cooldown_after_attempt or automatic_quality_cooldown
        )
        stored_reason = str(reason)[:2000]
        if direction_rotation_required:
            stored_reason = (
                f'同一规则版本连续 {DIFFICULTY_FAILURE_LIMIT} 轮未达到困难或地狱；下一轮必须更换真实故障方向：'
                + stored_reason
            )[:2000]
        db.execute("""UPDATE pairwise_source_locks SET state=?,token='',lease_until=0,
            cooldown_until=?,reason=?,failure_count=?,direction_checkpoint=?,
            difficulty_failure_count=?,difficulty_policy_version=?,updated_at=?
            WHERE source_pair_id=? AND task_type=?""",
            (exhausted_state() if exhausted else 'cooldown' if cooling else 'ready',
             time.time() + COOLDOWN_SECONDS if cooling else 0,
             stored_reason, 0 if exhausted or direction_rotation_required or cooling else failures,
             count if exhausted or cooling else row['direction_checkpoint'],
             difficulty_failures, current_policy, h.now_text(),
             source['pair']['id'], source['task_type']))


def bind_child(db, h, payload, pair_id, repo, sha, kind):
    """Called inside the child INSERT transaction; fencing stops stale workers."""
    seed = (payload.get('difficulty_preflight') or {}).get('seed_snapshot')
    if kind == 'Bug 修复' and payload.get('origin') == 'bank' and isinstance(seed, dict):
        batch_id = str(payload.get('bank_batch_id') or '')
        slot = payload.get('bank_slot')
        if not re.fullmatch(r'[a-f0-9]{12}', batch_id) or type(slot) is not int or slot < 1:
            raise h.WorkflowError('Bug 题面库候选标识无效')
        row = db.execute('''SELECT status,draft,pair_id FROM pairwise_bug_candidates
            WHERE batch_id=? AND slot=?''', (batch_id, slot)).fetchone()
        if not row or row['status'] != 'ready':
            raise h.WorkflowError('Bug 题面库候选尚未通过审核')
        draft = json.loads(row['draft'] or '{}')
        if (repo or sha or payload.get('prompt') != draft.get('prompt')
                or seed != draft.get('seed_snapshot') or row['pair_id']):
            raise h.WorkflowError('Bug 题面库预置快照与审核结果不一致')
        seed_path = Path(str(seed.get('bundle_path') or '')).resolve()
        seed_root = (Path(h.DATA_DIR) / 'pairwise-seeded').resolve()
        if (not seed_path.is_relative_to(seed_root) or not seed_path.is_file()
                or not all(re.fullmatch(r'[0-9a-f]{40}', str(seed.get(key) or ''))
                           for key in ('sha', 'tree'))):
            raise h.WorkflowError('Bug 题面库预置快照无效')
        return {}
    legacy_id = str(payload.get('legacy_baseline_run_id') or '')
    lineage_reuse = bug_lineage_reuse_enabled(h, db)
    seeded = kind == 'Bug 修复' and isinstance(seed, dict)
    if seeded:
        if repo or sha or payload.get('origin') != 'auto':
            raise h.WorkflowError('预置缺陷题必须使用新仓库的独立初始快照')
        seed_path = Path(str(seed.get('bundle_path') or '')).resolve()
        seed_root = (Path(h.DATA_DIR) / 'pairwise-seeded').resolve()
        if (not seed_path.is_relative_to(seed_root) or not seed_path.is_file()
                or not all(re.fullmatch(r'[0-9a-f]{40}', str(seed.get(key) or ''))
                           for key in ('sha', 'tree'))):
            raise h.WorkflowError('预置缺陷初始快照无效')
    if seeded and not legacy_id:
        parent_id = str(payload.get('source_pair_id') or '')
        attempt_id = str(payload.get('source_attempt_id') or '')
        token = str(payload.get('source_reservation_token') or '')
        attempt = db.execute('''SELECT attempt.*,parent.task_type AS parent_task_type
            FROM pairwise_attempts AS attempt
            JOIN pairwise_tasks AS parent ON parent.id=attempt.pair_id
            WHERE attempt.id=? AND attempt.pair_id=?''', (attempt_id, parent_id)).fetchone()
        lock = db.execute('SELECT * FROM pairwise_source_locks WHERE source_pair_id=? AND task_type=?',
                          (parent_id, kind)).fetchone()
        if (not attempt or not lock or not token or lock['token'] != token
                or lock['state'] != 'generating' or lock['lease_until'] <= time.time()
                or lock['source_attempt_id'] != attempt_id
                or lock['source_artifact_sha'] != attempt['artifact_sha']
                or lock['source_artifact_tree'] != attempt['artifact_tree']
                or consumed(db, parent_id, kind)):
            raise h.WorkflowError('预置缺陷来源锁或产物快照已变化，不能创建子题')
        root_pair_id = str(lock['source_root_pair_id'] or lineage_root_id(db, parent_id))
        if attempt['parent_task_type'] == 'Bug 修复' and not lineage_reuse:
            raise h.WorkflowError('Bug 修复任务不能继续作为新的 Bug 来源')
        if not lineage_reuse and lineage_consumed(db, root_pair_id, kind):
            raise h.WorkflowError('该项目根血缘已经创建过 Bug 修复题')
        db.execute('''UPDATE pairwise_source_locks SET state='created',child_pair_id=?,token='',
            lease_until=0,cooldown_until=0,updated_at=? WHERE source_pair_id=? AND task_type=?''',
                   (pair_id, h.now_text(), parent_id, kind))
        return dict(source_pair_id=parent_id, source_attempt_id=attempt_id,
                    source_side=attempt['side'], source_artifact_sha=attempt['artifact_sha'],
                    source_artifact_tree=attempt['artifact_tree'])
    if kind not in TYPES or (not sha and not legacy_id):
        return {}
    if legacy_id:
        if kind != 'Bug 修复' or not re.fullmatch(r'[0-9a-f]{12}', legacy_id):
            raise h.WorkflowError('zzzz 旧项目只能作为自动 Bug 修复题基线')
        from . import legacy_baselines
        baseline_sha = sha or str(payload.get('source_artifact_sha') or '')
        source = legacy_baselines.by_id(h, legacy_id, expected_sha=baseline_sha,
            expected_tree=str(payload.get('source_artifact_tree') or ''))
        if not source or (repo and source['pair']['repo_url'] != repo):
            raise h.WorkflowError('zzzz 来源快照或本地验收证据已变化')
        if source['pair']['task_type'] == 'Bug 修复' and not lineage_reuse:
            raise h.WorkflowError('Bug 修复任务不能继续作为新的 Bug 来源')
        if not lineage_reuse and lineage_consumed(db, source['root_pair_id'], kind):
            raise h.WorkflowError('该项目根血缘已经创建过 Bug 修复题')
        fields = dict(source_pair_id=source['pair']['id'], source_attempt_id=legacy_id,
                      source_side='legacy', source_artifact_sha=baseline_sha,
                      source_artifact_tree=source['attempt']['artifact_tree'])
        if any(payload.get(key) and payload[key] != value for key, value in fields.items()):
            raise h.WorkflowError('zzzz 来源身份或 Git tree 不一致')
        if consumed(db, fields['source_pair_id'], kind):
            raise h.WorkflowError('该 zzzz 基线已经创建过 Bug 修复题')
        lock = db.execute('''SELECT * FROM pairwise_source_locks
            WHERE source_pair_id=? AND task_type=?''',
            (fields['source_pair_id'], kind)).fetchone()
        token = str(payload.get('source_reservation_token') or '')
        if (not token or not lock or lock['token'] != token or lock['state'] != 'generating'
                or lock['lease_until'] <= time.time()
                or lock['source_attempt_id'] != legacy_id
                or lock['source_artifact_sha'] != baseline_sha
                or lock['source_artifact_tree'] != fields['source_artifact_tree']):
            raise h.WorkflowError('zzzz 来源生成锁已失效，不能重复或越权创建')
        db.execute('''UPDATE pairwise_source_locks SET state='created',child_pair_id=?,token='',
            lease_until=0,cooldown_until=0,updated_at=? WHERE source_pair_id=? AND task_type=?''',
            (pair_id, h.now_text(), fields['source_pair_id'], kind))
        return fields
    matches = db.execute("""SELECT p.id AS parent_id,p.task_type AS parent_task_type,a.* FROM pairwise_tasks p
          JOIN pairwise_attempts a ON a.pair_id=p.id WHERE p.repo_url=? AND a.artifact_sha=?""", (repo, sha)).fetchall()
    for parent in {r['parent_id'] for r in matches}:
        if consumed(db, parent, kind):
            raise h.WorkflowError('该父 Pair 已创建过同类型子题，不能从另一侧重复创建')
        if kind == 'Bug 修复':
            parent_rows = [row for row in matches if row['parent_id'] == parent]
            if (not lineage_reuse
                    and any(row['parent_task_type'] == 'Bug 修复' for row in parent_rows)):
                raise h.WorkflowError('Bug 修复任务不能继续作为新的 Bug 来源')
            if (not lineage_reuse
                    and lineage_consumed(db, lineage_root_id(db, parent), kind)):
                raise h.WorkflowError('该项目根血缘已经创建过 Bug 修复题')
    requested = payload.get('source_attempt_id')
    exact = [r for r in matches if r['id'] == requested] if requested else matches
    if requested and len(exact) != 1:
        raise h.WorkflowError('来源记录与固定快照不匹配')
    if len(exact) != 1:
        if matches:
            raise h.WorkflowError('来源快照对应多个尝试，请明确 source_attempt_id；不能推测来源')
        return {}
    a = exact[0]
    fields = dict(source_pair_id=a['parent_id'], source_attempt_id=a['id'], source_side=a['side'],
                  source_artifact_sha=sha, source_artifact_tree=a['artifact_tree'])
    if any(payload.get(k) and payload[k] != v for k, v in fields.items()):
        raise h.WorkflowError('来源身份或 Git tree 不一致')
    lock = db.execute('SELECT * FROM pairwise_source_locks WHERE source_pair_id=? AND task_type=?',
                      (a['parent_id'], kind)).fetchone()
    token = payload.get('source_reservation_token', '')
    if token and (not lock or lock['token'] != token or lock['state'] != 'generating'
                  or lock['lease_until'] <= time.time()):
        raise h.WorkflowError('来源生成锁已失效，请等待当前生成任务，不能重复创建')
    if lock and (lock['source_attempt_id'] != a['id'] or lock['source_artifact_sha'] != sha
                 or (not token and lock['state'] in ('generating', 'created'))):
        raise h.WorkflowError('来源已锁定，不能替换或重复创建')
    if lock:
        db.execute("""UPDATE pairwise_source_locks SET state='created',child_pair_id=?,token='',
            lease_until=0,cooldown_until=0,updated_at=? WHERE source_pair_id=? AND task_type=?""",
                   (pair_id, h.now_text(), a['parent_id'], kind))
    return fields


def direction_identity(candidate):
    identity = candidate.get('direction_identity') if isinstance(candidate, dict) else None


def direction_fingerprint(identity):
    return hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _direction_source_ids(h, source, kind):
    pair_id = str(source['pair']['id'])
    if kind != 'Bug 修复' or not bug_lineage_reuse_enabled(h):
        return [pair_id]
    root_pair_id = str(source.get('root_pair_id') or '')
    with h.db_connection() as db:
        if not root_pair_id:
            lock = db.execute('''SELECT source_root_pair_id FROM pairwise_source_locks
                WHERE source_pair_id=? AND task_type=?''', (pair_id, kind)).fetchone()
            root_pair_id = str(lock['source_root_pair_id'] or '') if lock else ''
        rows = db.execute('''SELECT source_pair_id FROM pairwise_source_locks
            WHERE source_root_pair_id=? AND task_type=?''',
            (root_pair_id, kind)).fetchall() if root_pair_id else []
    return list(dict.fromkeys([pair_id, *(str(row['source_pair_id']) for row in rows)]))


def rejected_directions(h, source, kind):
    source_ids = _direction_source_ids(h, source, kind)
    placeholders = ','.join('?' for _ in source_ids)
    with h.db_connection() as db:
        if kind == 'Bug 修复':
            rows = db.execute(f'''SELECT d.identity,r.reason FROM pairwise_source_directions d
                JOIN pairwise_source_direction_reviews r ON r.source_pair_id=d.source_pair_id
                  AND r.task_type=d.task_type AND r.fingerprint=d.fingerprint
                WHERE d.source_pair_id IN ({placeholders}) AND d.task_type=? AND r.policy_version=?
                ORDER BY r.created_at''',
                (*source_ids, kind, BUG_DIRECTION_REVIEW_POLICY)).fetchall()
        else:
            rows = db.execute(f'''SELECT identity,reason FROM pairwise_source_directions
                WHERE source_pair_id IN ({placeholders}) AND task_type=? ORDER BY created_at''',
                (*source_ids, kind)).fetchall()
    result = []
    for row in rows:
        record = {'identity': json.loads(row['identity']), 'reason': row['reason']}
        if record not in result:
            result.append(record)
    return result


def historical_direction_warnings(h, source, kind):
    """Retain old Bug rejection evidence without treating it as a current verdict."""
    if kind != 'Bug 修复':
        return []
    source_ids = _direction_source_ids(h, source, kind)
    placeholders = ','.join('?' for _ in source_ids)
    with h.db_connection() as db:
        rows = db.execute(f'''SELECT d.identity,d.reason FROM pairwise_source_directions d
            WHERE d.source_pair_id IN ({placeholders}) AND d.task_type=? AND NOT EXISTS (
              SELECT 1 FROM pairwise_source_direction_reviews r WHERE r.source_pair_id=d.source_pair_id
                AND r.task_type=d.task_type AND r.fingerprint=d.fingerprint AND r.policy_version=?)
            ORDER BY d.created_at''', (*source_ids, kind, BUG_DIRECTION_REVIEW_POLICY)).fetchall()
    result = []
    for row in rows:
        record = {'identity': json.loads(row['identity']), 'reason': row['reason']}
        if record not in result:
            result.append(record)
    return result


def prior_bug_directions(h, source):
    """Internal accepted Bug cards for root-wide semantic deduplication."""
    root_pair_id = str(source.get('root_pair_id') or '')
    if not root_pair_id:
        return []
    with h.db_connection() as db:
        rows = db.execute('''SELECT task.id,task.prompt,task.difficulty_preflight
            FROM pairwise_source_locks AS lock
            JOIN pairwise_tasks AS task ON task.id=lock.child_pair_id
            WHERE lock.source_root_pair_id=? AND lock.task_type='Bug 修复'
              AND lock.state='created' ORDER BY task.created_at''',
            (root_pair_id,)).fetchall()
    result = []
    for row in rows:
        try:
            preflight = json.loads(row['difficulty_preflight'] or '{}')
        except (TypeError, ValueError):
            preflight = {}
        seed = preflight.get('seed_snapshot') if isinstance(preflight, dict) else None
        design = seed.get('design_preflight') if isinstance(seed, dict) else None
        candidate = design.get('candidate') if isinstance(design, dict) else None
        result.append({
            'reference_id': f"pair:{row['id']}",
            'prompt': str(row['prompt'] or '')[:2400],
            'direction': ({
                key: candidate.get(key) for key in (
                    'affected_flow', 'proposed_change', 'trigger',
                    'observable_failure', 'expected_behavior', 'code_locations')
                if candidate.get(key)
            } if isinstance(candidate, dict) else {}),
        })
    return result


def reject_direction(h, source, kind, candidate, reason):
    identity = direction_identity(candidate)
    if not identity:
        return  # Never invent defect families from an error string or prompt.
    with h.db_connection() as db:
        db.execute('INSERT OR IGNORE INTO pairwise_source_directions VALUES (?,?,?,?,?,?)',
                   (source['pair']['id'], kind, direction_fingerprint(identity),
                    json.dumps(identity, ensure_ascii=False), str(reason)[:2000], h.now_text()))
        if kind == 'Bug 修复':
            db.execute('INSERT OR IGNORE INTO pairwise_source_direction_reviews VALUES (?,?,?,?,?,?)',
                       (source['pair']['id'], kind, direction_fingerprint(identity),
                        BUG_DIRECTION_REVIEW_POLICY, str(reason)[:2000], h.now_text()))


def transient_cooldown_reason(reason):
    """Only operational failures may delay a Bug source.

    A quality rejection is actionable evidence: the scheduler should remember
    that direction and immediately try another defect or source. Waiting does
    not make an easy defect difficult.
    """
    text = str(reason or '')
    folded = text.casefold()
    return any(marker.casefold() in folded for marker in TRANSIENT_COOLDOWN_MARKERS)


def bug_lineage_reuse_enabled(h, db=None):
    """Allow distinct products in a lineage as separate Bug sources."""
    return True


def source_exhaustion_reason(reason):
    """Recognize every source-wide no-direction result emitted by Bug flows."""
    text = str(reason or '')
    return any(marker in text for marker in BUG_SOURCE_EXHAUSTION_MARKERS)


def _snapshot_compatibility_issue(h, pair, attempt):
    """Reject source layouts the later blinded export cannot represent safely."""
    work = Path(str(attempt.get('repo_path') or '')).resolve()
    sha = str(attempt.get('artifact_sha') or '')
    if not work.is_dir() or not (work / '.git').exists() or not re.fullmatch(r'[0-9a-f]{40}', sha):
        return ''  # Existing source verification reports missing or stale workspaces.
    try:
        entries = h.run_command(
            ['git', 'ls-files', '--stage', '-z'], cwd=work, timeout=30).stdout.split('\0')
    except (OSError, h.WorkflowError):
        return ''
    root = work.resolve()
    for entry in entries:
        if not entry:
            continue
        metadata, separator, relative = entry.partition('\t')
        mode = metadata.split(' ', 1)[0]
        if not separator:
            continue
        if mode == '160000':
            return 'Bug 来源快照包含未展开子模块，已在设计前标记为不兼容'
        if mode != '120000':
            continue
        path = work / relative
        try:
            outside = not path.resolve().is_relative_to(root)
        except (OSError, RuntimeError):
            outside = True
        if outside:
            return 'Bug 来源快照含越界符号链接，已在设计前标记为不兼容'
    return ''


def _mark_snapshot_incompatible(h, source, kind, reason):
    pair, attempt = source['pair'], source['attempt']
    root_pair_id = str(source.get('root_pair_id') or pair['id'])
    now = h.now_text()
    with h.db_connection() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute("""INSERT OR IGNORE INTO pairwise_source_locks
            (source_pair_id,task_type,source_root_pair_id,source_attempt_id,source_side,
             source_artifact_sha,source_artifact_tree,state,reason,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,'incompatible',?,?,?)""",
            (pair['id'], kind, root_pair_id, attempt['id'], attempt['side'],
             attempt['artifact_sha'], attempt['artifact_tree'], reason, now, now))
        db.execute("""UPDATE pairwise_source_locks
            SET source_root_pair_id=?,state='incompatible',token='',lease_until=0,
                cooldown_until=0,reason=?,updated_at=?
            WHERE source_pair_id=? AND task_type=?
              AND NOT (state='generating' AND lease_until>?)""",
            (root_pair_id, reason, now, pair['id'], kind, time.time()))


def _difficulty_failure(reason):
    text = str(reason or '')
    return any(marker in text for marker in BUG_DIFFICULTY_FAILURE_MARKERS)
