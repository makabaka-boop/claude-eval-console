"""A/B are concurrent independent first turns. No follow-up entry points."""
import json
import hashlib
import shutil
import threading
import time
import uuid
import tempfile
from pathlib import Path

from . import models as m
from . import deployment
from . import environment as env

LOCK = threading.RLock()
WORKERS = set()
LEGACY_TIMEOUT_ERROR = "执行超过 90 分钟，请停止归档并换题；耗时不作为 GSB 依据"
FIRST_TURN_TIMEOUT_ERROR_PREFIX = "首轮运行达到 90 分钟上限"
PAIRWISE_MAX_ATTEMPTS = 3
BUG_FIRST_CLAUDE_MAX_RUNTIME_SECONDS = 70 * 60
# A pasted prompt is not proof that Claude accepted a turn.  If no matching
# session trace appears soon after the one allowed send, freeze the scene
# instead of occupying a slot until the normal first-turn timeout.
FIRST_TRACE_CONFIRM_SECONDS = 5 * 60
# Kept as a public compatibility name for callers/tests that describe the two
# reruns after the initial Session.  The policy itself is expressed as a total
# attempt limit so every first-turn failure follows the same rule.
PAIRWISE_API_RETRY_LIMIT = PAIRWISE_MAX_ATTEMPTS - 1
CHECKPOINT_DEPENDENCY_DIRS = frozenset({
    "node_modules", "nodemodules", ".venv", "venv", "__pycache__", ".pytest_cache",
    "target", "dist", ".next", ".nuxt", ".output", ".svelte-kit", ".angular",
    ".turbo", ".cache", ".mypy_cache", ".ruff_cache", ".tox", ".gradle",
})
CHECKPOINT_RUNTIME_DATABASE_NAMES = frozenset({
    "workspace.db", "app.db", "database.db", "db.sqlite3", "db.sqlite",
    "development.sqlite3",
})
CHECKPOINT_DATABASE_SUFFIXES = (".db", ".sqlite", ".sqlite3")
CHECKPOINT_DATABASE_SIDECARS = ("-wal", "-shm", "-journal")


def _insert_attempt(db, h, pair, side, number, config, group, directory, now):
    run_id = uuid.uuid4().hex[:12]
    model = pair.get(f"{side.lower()}_execution_model", m.AUTO_MODEL)
    db.execute("""INSERT INTO runs (id,repo_name,model,task_type,task_difficulty,
        language_framework,project_directory,repo_path,run_directory,repo_url,base_sha,
        snapshot_url,phase,status_detail,first_prompt,container_name,screen_name,
        harness_version,verification_commands,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (run_id, pair["repo_name"], model, pair["task_type"], pair["difficulty"],
         pair["language_framework"], group, str(directory / "workspace"), str(directory),
         pair["repo_url"], pair["base_sha"],
         f"{pair['repo_url']}/commit/{pair['base_sha']}" if pair["base_sha"] else "",
         "queued", f"{side} 等待并行首次执行", pair["prompt"], f"pair-{run_id}", f"pair-{run_id}",
         config["harness_version"], json.dumps(pair["verification_commands"]), now, now))
    db.execute("""INSERT INTO run_turns (run_id,turn_number,intent_type,prompt,model,
        created_at,updated_at) VALUES (?,1,?,?,?,?,?)""",
        (run_id, pair["task_type"], pair["prompt"], model, now, now))
    db.execute("INSERT INTO pairwise_attempts(id,pair_id,side,number,created_at) VALUES (?,?,?,?,?)",
               (run_id, pair["id"], side, number, now))
    return run_id


def can_rerun_side(pair, items, side):
    """Expose the same safe completed-side condition to both UIs."""
    if side not in {"A", "B"} or pair.get("deleted_at"):
        return False
    previous = m.latest(items, side)
    return bool(previous and previous["status"] == "complete"
                and previous["container_cleaned"] and previous["id"] not in WORKERS)


def rerun_side(h, pair_id, side):
    """Start a fresh Session for one completed side, preserving its old attempt."""
    if side not in {"A", "B"}:
        raise h.WorkflowError("重跑侧只能选择 A 或 B")
    return _start(h, pair_id, side, rerun_completed=True)


def start(h, pair_id, side=None):
    """Start both sides together, or retry an eligible failed engineering attempt."""
    return _start(h, pair_id, side)


def _start(h, pair_id, side=None, *, rerun_completed=False):
    with LOCK:
        pair = m.task(h, pair_id)
        items = m.attempts(h, pair_id)
        if rerun_completed and not can_rerun_side(pair, items, side):
            raise h.WorkflowError(f"{side} 尚未完成归档或现场未清理，不能单侧重跑")
        if rerun_completed:
            with h.db_connection() as db:
                submission = db.execute(
                    "SELECT state FROM pairwise_solo_submissions WHERE pair_id=?", (pair_id,)
                ).fetchone()
            if submission and submission["state"] == "submitting":
                raise h.WorkflowError("SOLO-QA 正在提交，请等待完成后再单侧重跑")
        if side is None:
            if h.configured_max_parallel_runs() < 2:
                raise h.WorkflowError("A/B 同时运行要求并行上限至少为 2")
            retrying = bool(items)
            if retrying:
                previous = {name: m.latest(items, name) for name in ("A", "B")}
                if any(not item or item["status"] not in {"failed", "stopped"}
                       or item["prompt_sent"] or item["id"] in WORKERS
                       for item in previous.values()):
                    raise h.WorkflowError("A/B 初次执行必须且只能同时启动一次；已有发送过 Prompt 或仍在执行的 Session 不能同时重建")
                if any(not item["container_cleaned"] for item in previous.values()):
                    raise h.WorkflowError("请先分别停止并清理两侧未发送 Prompt 的旧容器")
                if bool(pair["base_sha"]) != bool(pair["repo_url"]):
                    raise h.WorkflowError("共同 main 快照记录不完整，不能复用本题")
            config = pair["config"] or env.freeze(h)
            env.verify_host(h, config)
            group, root = h.resolve_project_directory(pair["project_directory"])
            project_root = pair_root(root, pair, items)
            directories = {
                name: project_root / (
                    f"{name}-retry-{previous[name]['number'] + 1:02d}"
                    if retrying else name
                ) for name in ("A", "B")
            }
            if any(directory.exists() for directory in directories.values()):
                raise h.WorkflowError("A 或 B 执行目录已经存在，请检查现场")
            now = h.now_text()
            with h.db_connection() as db:
                run_ids = [
                    _insert_attempt(
                        db, h, pair, name,
                        previous[name]["number"] + 1 if retrying else 1,
                        config, group, directories[name], now,
                    )
                    for name in ("A", "B")
                ]
                db.execute("""UPDATE pairwise_tasks SET config=?,human_confirmed=0,auto_skip_reason='',
                    revision=revision+1,updated_at=? WHERE id=?""",
                    (json.dumps(config, ensure_ascii=False), now, pair_id))
            for run_id in run_ids:
                enqueue(h, run_id)
            return [m.attempt(h, run_id) for run_id in run_ids]

        if side not in {"A", "B"}:
            raise h.WorkflowError("执行侧只能是大写 A 或 B")
        if not all(m.latest(items, name) for name in ("A", "B")):
            raise h.WorkflowError("初次执行必须使用“同时运行 A / B”")
        previous = m.latest(items, side)
        if previous["status"] in m.ACTIVE or previous["id"] in WORKERS:
            raise h.WorkflowError(f"{side} 仍有执行或归档任务，请等待结束")
        if previous and previous["status"] in {"complete", "archive_failed"} and not rerun_completed:
            raise h.WorkflowError("已完成的结果不能择优再跑；归档失败请使用不调用模型的重新归档")
        if previous and not previous["container_cleaned"]:
            raise h.WorkflowError("上次执行的容器尚未归档清理，请先点击停止并保留现场")
        config = pair["config"]
        if not config:
            raise h.WorkflowError("冻结配置不存在，不能建立重试 Session")
        env.verify_host(h, config)
        number = previous["number"] + 1 if previous else 1
        group, root = h.resolve_project_directory(pair["project_directory"])
        directory = pair_root(root, pair, items) / f"{side}-retry-{number:02d}"
        if directory.exists():
            raise h.WorkflowError("执行目录已存在，请检查现场")
        now = h.now_text()
        with h.db_connection() as db:
            run_id = _insert_attempt(db, h, pair, side, number, config, group, directory, now)
            if rerun_completed:
                db.execute("""UPDATE pairwise_tasks SET config=?,human_confirmed=0,
                    review_source='stale',auto_skip_reason='',revision=revision+1,updated_at=?
                    WHERE id=?""", (json.dumps(config, ensure_ascii=False), now, pair_id))
            else:
                db.execute("UPDATE pairwise_tasks SET config=?,human_confirmed=0,auto_skip_reason='',revision=revision+1,updated_at=? WHERE id=?",
                           (json.dumps(config, ensure_ascii=False), now, pair_id))
        enqueue(h, run_id)
        return m.attempt(h, run_id)


def enqueue(h, run_id, *, recovered_session=False):
    with LOCK:
        if run_id in WORKERS:
            return
        WORKERS.add(run_id)
        if recovered_session:
            # Reserve synchronously: queued workers must see every detached
            # session before any of them can acquire a fresh slot.
            h.reserve_existing_worker_slot()
    h.clear_job_cancellation(f"run:{run_id}")

    def worker():
        previous = h.current_job_key()
        h.CODEX_JOB_CONTEXT.key = f"run:{run_id}"
        try:
            from . import upstream
            if recovered_session:
                with h.worker_slot(reserved=True):
                    upstream.wait_until_ready(h, run_id)
                    execute(h, run_id)
            else:
                upstream.wait_until_ready(h, run_id)
                with h.worker_slot():
                    upstream.wait_until_ready(h, run_id)
                    execute(h, run_id)
        except h.JobCancelled:
            pass
        finally:
            h.CODEX_JOB_CONTEXT.key = previous
            with LOCK:
                WORKERS.discard(run_id)
    threading.Thread(target=worker, daemon=True).start()


def check_active(h, run_id):
    h.ensure_job_active()
    if m.attempt(h, run_id)["status"] in {"stopped", "upstream_paused"}:
        raise h.JobCancelled("执行已停止")


def execute(h, run_id):
    from . import upstream
    try:
        check_active(h, run_id)
        a = m.attempt(h, run_id)
        pair = m.task(h, a["pair_id"])
        # The handover gate must stop new Claude sessions, but recovery and
        # archiving are exactly the critical sections the reload is waiting to
        # finish. Pausing those states here creates a permanent drain deadlock.
        if a["status"] == "queued":
            upstream.wait_until_ready(h, run_id)
            deployment.wait_until_accepting(
                h, run_id,
                f"{a['side']} 已排队；服务更新交接完成后继续，不会重建 Session",
            )
            upstream.wait_until_ready(h, run_id)
            a = m.attempt(h, run_id)
        row = h.run_row(run_id)
        if a["status"] == "recovering":
            recover_result(h, run_id)
            return
        if a["status"] in {"archiving", "archive_failed"}:
            archive(h, run_id)
            return
        if a["status"] == "queued":
            upstream.wait_until_ready(h, run_id)
            env.verify_host(h, pair["config"])
            with LOCK:
                if upstream.is_paused(h):
                    return
                check_active(h, run_id)
                m.update_attempt(h, run_id, status="starting", started_at=h.now_text())
                h.update_run(run_id, phase="first_starting", status_detail=f"{a['side']} 等待共同 main 准备完成")
            # A/B are inserted together. Serialize only the shared repository
            # bootstrap, then run their sibling local workspaces in parallel.
            with LOCK:
                pair = ensure_pair_repository(h, run_id, m.task(h, a["pair_id"]))
            h.update_run(run_id, repo_url=pair["repo_url"], base_sha=pair["base_sha"],
                         snapshot_url=f"{pair['repo_url']}/commit/{pair['base_sha']}")
            with LOCK:
                if upstream.is_paused(h):
                    return
                check_active(h, run_id)
                h.update_run(run_id, status_detail=f"{a['side']} 启动固定镜像")
                h.launch_docker_terminal(row, image=pair["config"]["image"])
            h.wait_for_docker_container(run_id, row["container_name"])
            check_active(h, run_id)
            h.accept_container_permission_prompt(run_id, row["screen_name"], row["container_name"])
            actual = env.verify_container(h, row["container_name"], pair["config"], a["model"])
            workspace = Path(row["repo_path"])
            clone_initial(h, pair, workspace, a["side"])
            head = h.run_command(["git", "rev-parse", "HEAD"], cwd=workspace).stdout.strip()
            dirty = h.run_command(["git", "status", "--porcelain", "--untracked-files=all"], cwd=workspace).stdout
            if head != pair["base_sha"] or dirty:
                raise h.WorkflowError("初始工作区不是干净的共同快照")
            actual["base_sha"] = head
            actual["prompt_sha256"] = hashlib.sha256(pair["prompt"].encode()).hexdigest()
            m.update_attempt(h, run_id, environment=actual)
            h.update_run(run_id, repo_url=pair["repo_url"], base_sha=head,
                         snapshot_url=f"{pair['repo_url']}/commit/{head}")
            # Persist BEFORE sending. A crash in this interval never authorizes a resend.
            with LOCK:
                if upstream.is_paused(h):
                    return
                check_active(h, run_id)
                m.update_attempt(
                    h, run_id, prompt_sent=1, prompt_sent_at=h.now_text(), status="running")
                h.update_run(run_id, phase="first_running", status_detail=f"{a['side']} 首轮执行中")
                h.send_prompt_to_screen(run_id, row["screen_name"], pair["prompt"])
        monitor(h, run_id)
    except h.JobCancelled:
        return
    except RetryableClaudeApiError as exc:
        if m.attempt(h, run_id)["status"] in {"stopped", "upstream_paused"}:
            return
        try:
            _preserve_api_failure_and_retry(h, run_id, str(exc))
        except Exception as retry_exc:
            current = m.attempt(h, run_id)
            error = f"{exc}；自动重跑准备失败：{retry_exc}"
            m.update_attempt(
                h, run_id, status="failed", error=error,
                finished_at=current["finished_at"] or h.now_text())
            h.update_run(
                run_id, phase="failed",
                status_detail="Claude API 工程故障现场未能安全归档，已停止自动重跑",
                error=error,
            )
            h.add_event(run_id, error, "error")
        return
    except Exception as exc:
        current = m.attempt(h, run_id)
        if current["status"] in {"stopped", "upstream_paused"}:
            return
        if current["status"] in {"running", "recovering"} and current["prompt_sent"]:
            try:
                _preserve_first_turn_failure_and_retry(h, run_id, str(exc))
            except Exception as retry_exc:
                current = m.attempt(h, run_id)
                error = f"{exc}；自动重跑准备失败：{retry_exc}"
                m.update_attempt(
                    h, run_id, status="failed", error=error,
                    finished_at=current["finished_at"] or h.now_text())
                h.update_run(
                    run_id, phase="failed",
                    status_detail="失败现场未能安全归档，已停止自动重跑",
                    error=error,
                )
                h.add_event(run_id, error, "error")
            return
        status = "archive_failed" if current["status"] in {"archiving", "archive_failed"} else "failed"
        detail = ("归档失败，可执行不调用模型的重新归档" if status == "archive_failed" else
                  "恢复收尾失败，请检查错误后重试；未发送任何追加消息" if current["status"] == "recovering" else
                  "执行中断，请先检查并恢复归档，或停止并保留现场")
        error = f"恢复收尾失败：{exc}" if current["status"] == "recovering" else str(exc)
        m.update_attempt(h, run_id, status=status, error=error, finished_at=current["finished_at"] or h.now_text())
        h.update_run(run_id, phase="failed", status_detail=detail, error=error)
        h.add_event(run_id, str(exc), "error")


def remote_branches(h, repo_url, cwd=None):
    output = h.run_command(["git", "ls-remote", "--heads", repo_url], cwd=cwd, timeout=60).stdout
    result = {}
    for line in output.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[1].startswith("refs/heads/"):
            result[fields[1].removeprefix("refs/heads/")] = fields[0]
    return result


def previous_product_sha(items, side):
    """Last completed product before the current same-side attempt, if any."""
    current = m.latest(items, side)
    return next((item["artifact_sha"] for item in reversed(items)
                 if item["side"] == side and item["id"] != current["id"]
                 and item["status"] == "complete" and item["artifact_sha"]), "") if current else ""


def validate_remote_heads(h, pair, heads):
    if set(heads) != {"main", "A", "B"} or heads.get("main") != pair["base_sha"]:
        raise h.WorkflowError("本题仓库只能包含 main、A、B，且 main 不可变化")
    items = m.attempts(h, pair["id"])
    for side in ("A", "B"):
        item = m.latest(items, side)
        allowed = {pair["base_sha"]}
        if item and item["artifact_sha"]:
            allowed.add(item["artifact_sha"])
            if item["status"] == "complete":
                allowed = {item["artifact_sha"]}
        if item and item["status"] != "complete":
            prior_sha = previous_product_sha(items, side)
            if prior_sha:
                allowed.add(prior_sha)
        if heads.get(side) not in allowed:
            raise h.WorkflowError(f"本题仓库的 {side} 分支已被外部修改")


def ensure_pair_repository(h, run_id, pair):
    """Create or recover the one public repository owned by this Pair."""
    if pair["repo_url"] and pair["base_sha"]:
        heads = remote_branches(h, pair["repo_url"])
        validate_remote_heads(h, pair, heads)
        return pair

    # Local layout: pair-root/main, pair-root/A and pair-root/B.
    directory = Path(h.run_row(run_id)["run_directory"]).parent / "main"
    directory.parent.mkdir(parents=True, exist_ok=True)
    if directory.exists() and not (directory / ".git").is_dir():
        raise h.WorkflowError("共同 main 目录已存在但不是本题 Git 仓库，请检查现场")
    directory.mkdir(parents=True, exist_ok=True)
    h.run_command(["git", "init", "-b", "main"], cwd=directory)
    source = ""
    seed = (pair.get("difficulty_preflight") or {}).get("seed_snapshot")
    if isinstance(seed, dict):
        bundle = Path(str(seed.get("bundle_path") or "")).resolve()
        seed_root = (Path(h.DATA_DIR) / "pairwise-seeded").resolve()
        if not bundle.is_relative_to(seed_root) or not bundle.is_file():
            raise h.WorkflowError("冻结的预置缺陷快照丢失，不能创建远端仓库")
        h.run_command(["git", "fetch", str(bundle), "refs/heads/seed-main"], cwd=directory, timeout=120)
        fetched = h.run_command(["git", "rev-parse", "FETCH_HEAD"], cwd=directory).stdout.strip()
        tree = h.run_command(["git", "show", "-s", "--format=%T", fetched], cwd=directory).stdout.strip()
        parents = h.run_command(["git", "show", "-s", "--format=%P", fetched], cwd=directory).stdout.strip()
        if fetched != seed.get("sha") or tree != seed.get("tree") or parents:
            raise h.WorkflowError("预置缺陷快照不匹配或包含旧项目 Git 历史")
    elif pair["source_repo_url"] and pair["source_base_sha"]:
        h.run_command(["git", "remote", "remove", "source"], cwd=directory, check=False)
        h.run_command(["git", "remote", "add", "source", pair["source_repo_url"]], cwd=directory)
        h.run_command(["git", "fetch", "--depth=1", "source", pair["source_base_sha"]],
                      cwd=directory, timeout=180)
        tree = h.run_command(["git", "show", "-s", "--format=%T", pair["source_base_sha"]],
                             cwd=directory).stdout.strip()
        if pair.get('source_artifact_tree') and tree != pair['source_artifact_tree']:
            raise h.WorkflowError('来源 Git tree 与题面审核时的临时 main 不一致，不能创建远端仓库')
        source = f"{pair['source_repo_url']}/commit/{pair['source_base_sha']}"
    else:
        (directory / "README.md").write_bytes(b"")
        h.run_command(["git", "add", "README.md"], cwd=directory)
        tree = h.run_command(["git", "write-tree"], cwd=directory).stdout.strip()
    message = f"chore: initialize pair repository\n\nPair-ID: {pair['id']}"
    if source:
        message += f"\nSource-Snapshot: {source}"
    sha = h.run_command(["git", "-c", "user.name=Pair-wise GSB",
                         "-c", "user.email=pairwise-gsb@localhost", "commit-tree", tree, "-m", message],
                        cwd=directory).stdout.strip()
    h.run_command(["git", "update-ref", "refs/heads/main", sha], cwd=directory)
    h.run_command(["git", "checkout", "--detach", sha], cwd=directory)
    h.run_command(["git", "symbolic-ref", "HEAD", "refs/heads/main"], cwd=directory)
    h.run_command(["git", "remote", "remove", "source"], cwd=directory, check=False)

    slug = f"{h.GITHUB_OWNER}/{pair['repo_name']}"
    repo_url = f"https://github.com/{slug}"
    existing = h.run_command(["gh", "repo", "view", slug, "--json", "name"], timeout=30, check=False)
    if existing.returncode == 0:
        heads = remote_branches(h, repo_url, directory)
        if set(heads) not in ({"main"}, {"main", "A", "B"}):
            raise h.WorkflowError("同名仓库已存在且分支结构不属于本 Pair")
        h.run_command(["git", "fetch", repo_url, heads["main"]], cwd=directory, timeout=120)
        remote_tree = h.run_command(["git", "show", "-s", "--format=%T", heads["main"]],
                                    cwd=directory).stdout.strip()
        remote_message = h.run_command(["git", "show", "-s", "--format=%B", heads["main"]],
                                       cwd=directory).stdout
        if remote_tree != tree or f"Pair-ID: {pair['id']}" not in remote_message:
            raise h.WorkflowError("GitHub 同名仓库属于其他题目，请修改仓库名称")
        sha = heads["main"]
        if any(heads.get(side, sha) != sha for side in ("A", "B")):
            raise h.WorkflowError("同名仓库的 A/B 分支已包含其他产物")
    else:
        h.run_command(["git", "remote", "remove", "origin"], cwd=directory, check=False)
        h.run_command(["gh", "repo", "create", slug, "--public", "--source", str(directory),
                       "--remote", "origin", "--push"], cwd=directory, timeout=120)
    heads = remote_branches(h, repo_url, directory)
    for side in ("A", "B"):
        if side not in heads:
            h.run_command(["git", "push", repo_url, f"{sha}:refs/heads/{side}"],
                          cwd=directory, timeout=120)
    heads = remote_branches(h, repo_url, directory)
    if heads != {"main": sha, "A": sha, "B": sha}:
        raise h.WorkflowError("本题新仓库必须且只能有 main、A、B 三个分支")
    with h.db_connection() as db:
        db.execute("UPDATE pairwise_tasks SET repo_url=?,base_sha=?,updated_at=? WHERE id=?",
                   (repo_url, sha, h.now_text(), pair["id"]))
    h.add_event(run_id, f"已为本题创建独立仓库，main 初始快照 {sha[:8]}", "success")
    return m.task(h, pair["id"])


def clone_initial(h, pair, workspace, side):
    if side not in {"A", "B"}:
        raise h.WorkflowError("本地分支只能是大写 A 或 B")
    # Fetch only the common base. Neither side receives the sibling product.
    h.run_command(["git", "init", "-b", "main"], cwd=workspace)
    h.run_command(["git", "remote", "add", "origin", pair["repo_url"]], cwd=workspace)
    h.run_command(["git", "fetch", "--depth=1", "origin", pair["base_sha"]], cwd=workspace, timeout=180)
    h.run_command(["git", "checkout", "-b", side, pair["base_sha"]], cwd=workspace)


def monitor(h, run_id):
    last_activity_at = time.monotonic()
    last_activity_signature = None
    final_extension_reported = False
    while True:
        check_active(h, run_id)
        row = h.run_row(run_id)
        running = h.docker_container_running(row["container_name"])
        snapshot = None
        try:
            snapshot, state = h.refresh_trace_snapshot(row)
        except h.WorkflowError:
            if not running:
                raise h.WorkflowError("容器已停止且轨迹尚未归档")
            state = None
        if state:
            if row["session_id"] and row["session_id"] != state["session_id"]:
                raise h.WorkflowError("SessionID 在执行中发生变化")
            h.update_run(run_id, session_id=state["session_id"], first_prompt_id=state["prompt_id"])
            h.update_turn(run_id, 1, prompt_id=state["prompt_id"])
            if state["api_error"]:
                api_error = str(state["api_error"])
                if h.retryable_api_error(api_error):
                    raise RetryableClaudeApiError(api_error)
                raise h.WorkflowError(api_error)
            if state["interrupted"]:
                raise h.WorkflowError(state["interruption_reason"])
            from .validation import trace_issues
            pair = m.task(h, m.attempt(h, run_id)["pair_id"])
            issues = trace_issues(h, Path(state["path"]), pair, state["session_id"], require_complete=False)
            if issues:
                raise h.WorkflowError("；".join(issues))
            if state.get("ended_without_final"):
                raise h.WorkflowError(
                    "模型首轮已结束但没有合规最终回复，请归档现场并创建新的独立 Session"
                )
            if state["complete"]:
                deployment.wait_until_accepting(
                    h, run_id,
                    f"{m.attempt(h, run_id)['side']} 已完成首轮；服务更新交接后继续归档",
                )
                archive_completed_state(h, run_id, state)
                return
        if not running:
            raise h.WorkflowError("模型未完成首轮即退出，请归档现场并创建新的独立 Session")
        a = m.attempt(h, run_id)
        now = time.monotonic()
        signature = h.trace_activity_signature(snapshot) if snapshot else None
        if signature and signature != last_activity_signature:
            first_observation = last_activity_signature is None
            last_activity_signature = signature
            if first_observation:
                trace_age = max(0.0, time.time() - (signature[2] / 1_000_000_000))
                last_activity_at = now - min(trace_age, h.RUN_TIMEOUT_SECONDS)
            else:
                last_activity_at = now
        elapsed_seconds = (
            h.seconds_between(a["started_at"], h.now_text()) if a["started_at"] else 0
        )
        pair = m.task(h, a["pair_id"])
        if (pair["task_type"] == "Bug 修复" and a["prompt_sent_at"]
                and h.seconds_between(a["prompt_sent_at"], h.now_text())
                >= BUG_FIRST_CLAUDE_MAX_RUNTIME_SECONDS):
            sent_on_side = [item for item in m.attempts(h, a["pair_id"])
                            if item["side"] == a["side"] and item["prompt_sent"]]
            if len(sent_on_side) == 1 and sent_on_side[0]["id"] == run_id:
                skip_overlong_bug_pair(h, run_id)
                return
        inactive_seconds = max(0.0, now - last_activity_at)
        terminal_text = h.terminal_screen_text(run_id, row["screen_name"])
        in_final_stage = h.first_turn_is_in_final_stage(
            f"{terminal_text}\n{h.recent_trace_text(snapshot)}"
        )
        stop_reason = h.first_turn_stop_reason(
            elapsed_seconds, inactive_seconds, in_final_stage
        )
        if stop_reason:
            deployment.wait_until_accepting(
                h, run_id,
                f"{a['side']} 已达到运行上限；服务更新交接后检查原 Session",
            )
            check_active(h, run_id)
            m.update_attempt(h, run_id, status="recovering", error=stop_reason)
            h.update_run(run_id, status_detail="达到运行上限，正在停止容器、保存现场并核对完成边界")
            h.add_event(
                run_id,
                f"{stop_reason}，自动检查并收尾；不追加消息，未完成时按三次总尝试上限新建 Session",
                "warning",
            )
            recover_result(h, run_id)
            return
        if (
            elapsed_seconds >= h.FIRST_TURN_MAX_RUNTIME_SECONDS
            and not final_extension_reported
        ):
            h.add_event(
                run_id,
                "90 分钟时仍处于活跃的测试、构建或验收收尾阶段，额外延时 15 分钟；105 分钟为硬上限",
                "warning",
            )
            final_extension_reported = True
        attention = h.terminal_attention_reason_from_text(terminal_text)
        detail = (
            f"{a['side']} 测试收尾延时中（最多至 105 分钟，只读监控）"
            if elapsed_seconds >= h.FIRST_TURN_MAX_RUNTIME_SECONDS
            else f"{a['side']} 首轮执行中（只读监控）"
        )
        h.update_run(run_id, status_detail=attention or detail)
        time.sleep(h.POLL_SECONDS)


def archive_completed_state(h, run_id, state):
    """Only the original, strictly single-turn session can become a product."""
    check_active(h, run_id)
    a = m.attempt(h, run_id)
    row = h.run_row(run_id)
    if row["session_id"] and row["session_id"] != state["session_id"]:
        raise h.WorkflowError("SessionID 在执行中发生变化，不能恢复归档")
    if not state["complete"] or state["api_error"] or state["interrupted"]:
        raise h.WorkflowError("原始轨迹没有合规的首轮完成边界")
    from .validation import trace_issues
    problems = trace_issues(h, Path(state["path"]), m.task(h, a["pair_id"]), state["session_id"])
    if problems:
        raise h.WorkflowError("；".join(problems))
    check_active(h, run_id)
    h.update_run(run_id, session_id=state["session_id"], first_prompt_id=state["prompt_id"],
                 first_result=state["result"], phase="first_idle", status_detail="首轮结束，保存产物与原始轨迹")
    h.update_turn(run_id, 1, prompt_id=state["prompt_id"], result=state["result"], status="reviewing")
    m.update_attempt(h, run_id, status="archiving")
    archive(h, run_id)


def recover_result(h, run_id):
    """Freeze and validate the original Session; rerun only when it is incomplete."""
    check_active(h, run_id)
    a = m.attempt(h, run_id)
    if a["status"] != "recovering" or not a["prompt_sent"]:
        raise h.WorkflowError("只有已发送题面的失败首轮可检查恢复")
    row = h.run_row(run_id)
    pair = m.task(h, a["pair_id"])
    if m.latest(m.attempts(h, a["pair_id"]), a["side"])["id"] != run_id:
        raise h.WorkflowError("不能恢复已被新 Session 替代的旧尝试")
    original_error = a["error"] or "首轮执行中断"
    trace_root = Path(a["run_directory"]) / "traces"
    if not row["container_cleaned"]:
        exists = h.run_command(["docker", "inspect", row["container_name"]], check=False, timeout=20)
        if exists.returncode != 0:
            # A daemon/network failure must not be mistaken for a deleted container.
            containers = h.run_command(["docker", "container", "ls", "-a", "--format", "{{.Names}}"], timeout=20)
            if row["container_name"] in containers.stdout.splitlines():
                raise h.WorkflowError("容器仍存在但无法检查，请检查 Docker 后重试")
            if not any(trace_root.rglob("*.jsonl")):
                raise h.WorkflowError("未找到容器或已保存的原始轨迹，现场状态需人工检查")
            h.update_run(run_id, container_cleaned=1)
        else:
            h.update_run(run_id, status_detail="正在停止原容器并冻结现场；不向会话输入内容")
            if h.docker_container_running(row["container_name"]):
                h.run_command(["docker", "stop", "--time", "10", row["container_name"]], timeout=30)
                if h.docker_container_running(row["container_name"]):
                    raise h.WorkflowError("容器未能停止，尚未释放现场；请检查 Docker 后重试")
            check_active(h, run_id)
            # Retain the stopped container on missing/invalid JSONL or copy failure.
            h.copy_container_traces(row, trace_root)
    elif row["trajectory_path"] and Path(row["trajectory_path"]).is_file():
        trace_root = Path(row["trajectory_path"]).parent
    check_active(h, run_id)
    h.update_run(run_id, status_detail="原进程已停止，正在核验原始题面、SessionID 与首轮完成边界")
    state = h.trace_turn_state(trace_root, pair["prompt"])
    if state is None:
        error = (f"{original_error}；停止后仍未发现与原题面匹配的 JSONL；"
                 "容器、工作区与终端日志已保留，未发送追加消息，也未自动重跑")
        m.update_attempt(h, run_id, status="failed", error=error, finished_at=h.now_text())
        h.update_turn(run_id, 1, status="failed")
        h.update_run(run_id, phase="failed", status_detail="首轮启动未确认；现场已保留，需人工检查", error=error)
        h.add_event(run_id, error, "error")
        return
    # Preserve even incomplete evidence for inspection; do not invent a SessionID.
    session = str(row["session_id"] or (state or {}).get("session_id") or "")
    candidates = list(trace_root.rglob(f"{session}.jsonl")) if session else []
    if candidates:
        source = candidates[0]
        m.update_attempt(h, run_id, trace_path=str(source), trace_sha256=hashlib.sha256(source.read_bytes()).hexdigest())
        h.update_run(run_id, trajectory_path=str(source))
    if state and state["complete"]:
        h.add_event(run_id, "检测到原 Session 的首轮最终回复，核验后仅补归档与验收，不重新调用模型", "info")
        archive_completed_state(h, run_id, state)
        return
    detail = "未检测到首轮最终回复与完成边界，已停止并保留现场"
    if state and (state["api_error"] or state["interrupted"]):
        detail = f"{state['api_error'] or state['interruption_reason']}；已停止并保留现场"
    error = f"{original_error}；{detail}"
    check_active(h, run_id)
    _preserve_first_turn_failure_and_retry(h, run_id, error)


def request_recovery(h, run_id):
    with LOCK:
        a = m.attempt(h, run_id)
        pair = m.task(h, a["pair_id"])
        if pair.get("deleted_at"):
            raise h.WorkflowError("该题已从列表删除，不能恢复执行")
        if m.latest(m.attempts(h, a["pair_id"]), a["side"])["id"] != run_id:
            raise h.WorkflowError("不能恢复已被新 Session 替代的旧尝试")
        if a["status"] == "recovering":
            return a
        if a["status"] != "failed" or not a["prompt_sent"] or run_id in WORKERS:
            raise h.WorkflowError("当前不是可检查恢复的失败首轮；已完成或手动停止的会话不会自动恢复")
        m.update_attempt(h, run_id, status="recovering")
        h.update_run(run_id, status_detail="等待检查并恢复归档（不调用模型）")
        h.add_event(
            run_id,
            "请求检查失败现场；先保留并核验原 Session，不追加消息；未完成时按三次总尝试上限新建 Session",
            "info",
        )
        enqueue(h, run_id)
    return m.attempt(h, run_id)


def checkpoint(h, run_id, pair):
    """A canonical product commit with EXACTLY the shared base as its one parent.

    The model's working-tree and commit history are preserved. The new commit
    references the finished tree without rebasing or rewriting that history.
    """
    a = m.attempt(h, run_id)
    workspace = Path(a["repo_path"])
    branch = a["side"]
    sha = a["artifact_sha"]
    if not sha:
        h.run_command(["git", "add", "-A"], cwd=workspace)
        added = h.run_command(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=A",
             "--no-renames", "-z", pair["base_sha"], "--"],
            cwd=workspace,
        ).stdout.split("\0")
        dependencies = checkpoint_excluded_paths(added)
        for offset in range(0, len(dependencies), 100):
            h.run_command(
                ["git", "rm", "--cached", "--quiet", "--ignore-unmatch", "--",
                 *dependencies[offset:offset + 100]],
                cwd=workspace,
            )
        if dependencies:
            h.add_event(
                run_id,
                f"产物快照排除了 {len(dependencies)} 个依赖、构建、缓存或运行数据库文件；原工作区与轨迹已保留",
                "info",
            )
        tree = h.run_command(["git", "write-tree"], cwd=workspace).stdout.strip()
        message = f"Pair {pair['id']} / {a['side']}\n\nSession-ID: {a['session_id']}\nInitial-Snapshot: {pair['base_sha']}"
        sha = h.run_command(["git", "commit-tree", tree, "-p", pair["base_sha"], "-m", message], cwd=workspace).stdout.strip()
        m.update_attempt(h, run_id, artifact_sha=sha, artifact_tree=tree)
    parents = h.run_command(["git", "show", "-s", "--format=%P", sha], cwd=workspace).stdout.strip()
    if parents != pair["base_sha"]:
        raise h.WorkflowError("产物 commit 的唯一直接父提交不是初始快照")
    model_head = h.run_command(["git", "rev-parse", "HEAD"], cwd=workspace).stdout.strip()
    if model_head not in {pair["base_sha"], sha}:
        h.run_command(["git", "update-ref", f"refs/pairwise/model-head-{branch}", model_head], cwd=workspace)
    h.run_command(["git", "update-ref", f"refs/heads/{branch}", sha], cwd=workspace)
    # Both workers may finish together. Serialize read/check/push and allow the
    # sibling to remain at main until its own archive reaches this section.
    with LOCK:
        pair = m.task(h, pair["id"])
        heads = remote_branches(h, pair["repo_url"], workspace)
        validate_remote_heads(h, pair, heads)
        previous_sha = previous_product_sha(m.attempts(h, pair["id"]), branch)
        if heads.get(branch) not in {pair["base_sha"], sha, previous_sha}:
            raise h.WorkflowError(f"远端分支 {branch} 已存在其他提交；请使用独立仓库新建 Pair")
        if heads.get(branch) != sha:
            lease = ([f"--force-with-lease=refs/heads/{branch}:{previous_sha}"]
                     if previous_sha and heads.get(branch) == previous_sha else [])
            h.run_command(["git", "push", *lease, pair["repo_url"], f"{sha}:refs/heads/{branch}"],
                          cwd=workspace, timeout=180)
        heads = remote_branches(h, pair["repo_url"], workspace)
        validate_remote_heads(h, pair, heads)
        if heads.get(branch) != sha:
            raise h.WorkflowError(f"远端分支 {branch} 没有指向本侧产物")
    h.update_turn(run_id, 1, commit_sha=sha)
    return sha


def archive(h, run_id):
    check_active(h, run_id)
    a = m.attempt(h, run_id)
    pair = m.task(h, a["pair_id"])
    m.update_attempt(h, run_id, status="archiving", error="")
    if not a["container_cleaned"]:
        h.export_and_remove_container(run_id, force=True)
    row = h.run_row(run_id)
    source = Path(row["trajectory_path"] or "")
    from .validation import trace_issues
    problems = trace_issues(h, source, pair, row["session_id"])
    if problems:
        raise h.WorkflowError("；".join(problems))
    m.update_attempt(h, run_id, trace_path=str(source), trace_sha256=hashlib.sha256(source.read_bytes()).hexdigest())
    sha = checkpoint(h, run_id, pair)
    check_active(h, run_id)
    # Commands run against a separate clean clone AFTER the immutable snapshot.
    # Failed verification is retained as a valid result, never auto-repaired.
    with tempfile.TemporaryDirectory(prefix="verify-", dir=a["run_directory"]) as directory:
        work = Path(directory)
        h.run_command(["git", "clone", "--quiet", "--no-local", a["repo_path"], str(work)], timeout=120)
        h.run_command(["git", "fetch", "origin", sha], cwd=work, timeout=120)
        h.run_command(["git", "checkout", "--detach", sha], cwd=work)
        checks = h.verification_results(pair["verification_commands"], work, run_id)
    check_active(h, run_id)
    m.update_attempt(h, run_id, verification=checks, status="complete", error="", finished_at=h.now_text())
    h.update_turn(run_id, 1, status="complete", verification=json.dumps(checks, ensure_ascii=False),
                  trajectory_path=str(source), trajectory_sha256=hashlib.sha256(source.read_bytes()).hexdigest())
    h.update_run(run_id, phase="complete", status_detail=f"{a['side']} 已归档，等待手动开始录屏与人工 GSB", error=None)
    h.add_event(run_id, "首轮已结束，产物与原始 JSONL 已归档", "success")
    from .difficulty import enqueue_if_ready as enqueue_difficulty
    enqueue_difficulty(h, pair["id"])


def retry_archive(h, run_id):
    with LOCK:
        a = m.attempt(h, run_id)
        if a["status"] != "archive_failed" or run_id in WORKERS:
            raise h.WorkflowError("当前没有可重新归档的现场")
        m.update_attempt(h, run_id, status="archiving", error="")
        enqueue(h, run_id)
    return m.attempt(h, run_id)


def stop(h, run_id):
    with LOCK:
        a = m.attempt(h, run_id)
        if a["status"] == "complete":
            raise h.WorkflowError("执行已经完成")
        m.update_attempt(h, run_id, status="stopped", finished_at=h.now_text())
        h.cancel_background_job(f"run:{run_id}")
        h.update_run(run_id, phase="stopped", status_detail="已停止；正在保留现场")
        # End a pending launcher before inspecting Docker; it must not create a
        # container after the stop handler has declared this attempt cleaned.
        row = h.run_row(run_id)
        if not a["prompt_sent"] and h.screen_session_running(row["screen_name"]):
            h.run_command(["screen", "-S", row["screen_name"], "-X", "quit"], timeout=20, check=False)
    row = h.run_row(run_id)
    if not row["container_cleaned"]:
        exists = h.run_command(["docker", "inspect", row["container_name"]], check=False, timeout=20)
        if exists.returncode == 0:
            try:
                h.export_and_remove_container(run_id, force=True)
            except h.WorkflowError:
                if not a["prompt_sent"]:
                    h.remove_container_resources_without_trace(row)
                    h.update_run(run_id, container_cleaned=1)
                    return m.attempt(h, run_id)
                # Keep the stopped container if no trace was obtainable. Nothing deleted.
                h.run_command(["docker", "stop", "--time", "10", row["container_name"]], check=False, timeout=30)
                raise h.WorkflowError("容器已停止但原始轨迹导出失败，现场已保留；修复后重新归档现场（不调用模型）")
        else:
            h.update_run(run_id, container_cleaned=1)
    return m.attempt(h, run_id)


def skip_overlong_bug_pair(h, run_id):
    """Stop both unfinished sides without retrying or deleting an unexported trace."""
    with LOCK:
        trigger = m.attempt(h, run_id)
        pair = m.task(h, trigger["pair_id"])
        if (pair["task_type"] != "Bug 修复" or pair.get("auto_skip_reason")
                or trigger["status"] != "running" or not trigger["prompt_sent_at"]
                or h.seconds_between(trigger["prompt_sent_at"], h.now_text())
                < BUG_FIRST_CLAUDE_MAX_RUNTIME_SECONDS):
            return
        sent_on_side = [item for item in m.attempts(h, pair["id"])
                        if item["side"] == trigger["side"] and item["prompt_sent"]]
        if len(sent_on_side) != 1 or sent_on_side[0]["id"] != run_id:
            return
        reason = (f"Bug 修复首次 Claude Session 超过 70 分钟仍未完成"
                  f"（{trigger['side']} 触发）；已跳过本题，不自动重跑")
        now = h.now_text()
        with h.db_connection() as db:
            db.execute("""UPDATE pairwise_tasks SET auto_skip_reason=?,revision=revision+1,
                updated_at=? WHERE id=?""", (reason, now, pair["id"]))
        current = [m.latest(m.attempts(h, pair["id"]), side) for side in ("A", "B")]
        targets = [item for item in current if item and item["status"] in
                   {"queued", "starting", "running", "recovering"}]
        for item in targets:
            m.update_attempt(h, item["id"], status="stopped", error=reason, finished_at=now)
            h.cancel_background_job(f"run:{item['id']}")
            h.update_run(item["id"], phase="stopped", status_detail="本题超过 70 分钟上限，已跳过；正在保留现场", error=reason)
            h.add_event(item["id"], reason, "warning")
    # The triggering monitor is itself a cancellable worker. Clear only this
    # thread's context while exporting; sibling workers remain cancelled.
    previous = h.current_job_key()
    h.CODEX_JOB_CONTEXT.key = ""
    try:
        for item in targets:
            try:
                stop(h, item["id"])
                h.update_run(item["id"], status_detail="已跳过；运行现场已安全保存")
            except Exception as exc:
                h.add_event(item["id"], f"自动停止后现场清理未完成：{exc}；容器与轨迹现场已保留", "error")
                h.update_run(item["id"], status_detail="已跳过；现场尚需人工检查与归档")
    finally:
        h.CODEX_JOB_CONTEXT.key = previous


def prepare_recording(h, run_id):
    a = m.attempt(h, run_id)
    if a["status"] != "complete":
        raise h.WorkflowError("请先完成产物归档")
    temporary_root = h.DATA_DIR / "pairwise-recording-workspaces"
    temporary_root.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix=f"workspace-{run_id}-", dir=temporary_root))
    try:
        h.run_command(["git", "clone", "--quiet", "--no-local", a["repo_path"], str(directory)], timeout=120)
        h.run_command(["git", "fetch", "origin", a["artifact_sha"]], cwd=directory, timeout=120)
        h.run_command(["git", "checkout", "--detach", a["artifact_sha"]], cwd=directory)
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    return {"path": str(directory), "commit": a["artifact_sha"]}


def recover(h):
    from . import upstream
    with h.db_connection() as db:
        db.execute("UPDATE pairwise_drafts SET status='failed',detail='服务重启中断了题面生成，请重新生成' WHERE status IN ('queued','running')")
        db.execute("UPDATE pairwise_zero_one_batches SET status='partial',detail='服务重启中断了批量出题；已完成候选仍可使用' WHERE status IN ('queued','running')")
        db.execute("UPDATE pairwise_zero_one_candidates SET status='failed',error='服务重启中断了本题生成' WHERE status IN ('pending','generating') AND batch_id IN (SELECT id FROM pairwise_zero_one_batches WHERE status='partial')")
        db.execute("UPDATE pairwise_iteration_batches SET status='partial',detail='服务重启中断了批量出题；已完成候选仍可使用' WHERE status IN ('queued','running')")
        db.execute("UPDATE pairwise_iteration_candidates SET status='failed',error='服务重启中断了本题生成' WHERE status IN ('pending','generating') AND batch_id IN (SELECT id FROM pairwise_iteration_batches WHERE status='partial')")
        rows = db.execute("SELECT id,status FROM pairwise_attempts WHERE status IN ('queued','starting','running','recovering','archiving')").fetchall()
        timed_out = db.execute("""SELECT a.id FROM pairwise_attempts a JOIN runs r ON r.id=a.id
            JOIN pairwise_tasks t ON t.id=a.pair_id
            WHERE a.status='failed' AND (a.error=? OR a.error LIKE ?) AND a.prompt_sent=1
              AND r.container_cleaned=0 AND t.deleted_at IS NULL
              AND a.number=(SELECT MAX(b.number) FROM pairwise_attempts b
                            WHERE b.pair_id=a.pair_id AND b.side=a.side)""",
            (LEGACY_TIMEOUT_ERROR, f"{FIRST_TURN_TIMEOUT_ERROR_PREFIX}%")).fetchall()
    for row in rows:
        if row["status"] == "starting":
            if upstream.is_paused(h):
                # The outage coordinator must still stop a launcher that was
                # interrupted between the persisted pause and service reload.
                continue
            m.update_attempt(h, row["id"], status="failed", error="启动期间服务重启，现场已保留，请归档后创建新的独立 Session")
            h.update_run(row["id"], phase="failed", status_detail="启动中断，未自动重发 Prompt")
    # Detached sessions already occupy real Claude capacity even though the
    # new process starts with an empty in-memory slot counter. Reserve and
    # monitor them before any queued attempt may launch a new container.
    for row in rows:
        if row["status"] in {"running", "recovering", "archiving"}:
            enqueue(h, row["id"], recovered_session=True)
    for row in rows:
        if row["status"] == "queued":
            enqueue(h, row["id"])
    for row in timed_out:
        request_recovery(h, row["id"])


def pair_root(root, pair, items):
    """Name new folders with the list number; retain persisted roots on any retry."""
    if items:
        return Path(items[0]["run_directory"]).parent
    return root / pair["project_folder"]


class RetryableClaudeApiError(Exception):
    """A network/API failure that invalidates this Session but not the task."""


def _create_automatic_retry(h, failed_attempt, failure_kind="首轮执行失败",
                            *, preserved_without_trace=False):
    """Create a fresh same-side Session while the three-attempt budget remains."""
    with LOCK:
        pair = m.task(h, failed_attempt["pair_id"])
        if pair.get("deleted_at"):
            raise h.WorkflowError("该任务已从列表删除，不能自动重跑")
        if pair.get("auto_skip_reason"):
            return None
        items = m.attempts(h, pair["id"])
        current = m.latest(items, failed_attempt["side"])
        if not current or current["id"] != failed_attempt["id"]:
            raise h.WorkflowError("失败 Session 已被新的同侧尝试替代，不能重复自动重跑")
        if current["status"] != "failed":
            raise h.WorkflowError("失败 Session 的轨迹或容器尚未安全归档，不能自动重跑")
        if preserved_without_trace:
            row = h.run_row(current["id"])
            traces = Path(current["run_directory"]) / "traces"
            state = h.run_command(
                ["docker", "inspect", "--format", "{{.State.Running}}", row["container_name"]],
                check=False, timeout=20,
            )
            if (current["container_cleaned"] or not current["prompt_sent"]
                    or current["session_id"] or current["trace_path"]
                    or any(traces.rglob("*.jsonl"))
                    or state.returncode != 0 or state.stdout.strip() != "false"):
                raise h.WorkflowError("原容器或轨迹状态已变化，不能按无轨迹现场重跑")
        elif not current["container_cleaned"]:
            raise h.WorkflowError("失败 Session 的轨迹或容器尚未安全归档，不能自动重跑")
        if current["number"] >= PAIRWISE_MAX_ATTEMPTS:
            return None
        config = pair["config"]
        if not config:
            raise h.WorkflowError("冻结配置不存在，不能自动重跑")
        env.verify_host(h, config)
        group, root = h.resolve_project_directory(pair["project_directory"])
        number = current["number"] + 1
        directory = pair_root(root, pair, items) / f"{current['side']}-retry-{number:02d}"
        if directory.exists():
            raise h.WorkflowError("自动重跑目录已经存在，请检查现场")
        now = h.now_text()
        with h.db_connection() as db:
            run_id = _insert_attempt(
                db, h, pair, current["side"], number, config, group, directory, now)
            db.execute("""UPDATE pairwise_tasks SET config=?,human_confirmed=0,
                revision=revision+1,updated_at=? WHERE id=?""",
                       (json.dumps(config, ensure_ascii=False), now, pair["id"]))
        preserved_detail = "无 JSONL 的原容器已停止并保留" if preserved_without_trace else f"{failure_kind}已归档"
        h.update_run(failed_attempt["id"], status_detail=(
            f"{preserved_detail}；已创建 {current['side']} 第 {number} 次独立 Session"))
        h.add_event(
            failed_attempt["id"],
            (f"{preserved_detail}；新建同侧独立 Session {run_id}，"
             "未向原 Session 追加消息"),
            "warning",
        )
        h.add_event(
            run_id,
            (f"由 {failed_attempt['id']} 的{failure_kind}触发自动重跑；"
             "使用相同题面、冻结配置和共同 main 初始快照"),
            "warning",
        )
    enqueue(h, run_id)
    return m.attempt(h, run_id)


def can_retry_unconfirmed(h, attempt, pair=None):
    """Show the recovery action only after an unconfirmed first prompt is stale."""
    pair = pair or m.task(h, attempt["pair_id"])
    if (pair.get("deleted_at") or pair.get("auto_skip_reason")
            or attempt["status"] not in {"running", "failed", "stopped"}
            or not attempt["prompt_sent"] or attempt["session_id"]
            or attempt["trace_path"] or attempt["container_cleaned"]
            or attempt["number"] >= PAIRWISE_MAX_ATTEMPTS):
        return False
    return (attempt["status"] != "running" or bool(attempt["prompt_sent_at"] and
            h.seconds_between(attempt["prompt_sent_at"], h.now_text())
            >= FIRST_TRACE_CONFIRM_SECONDS))


def retry_unconfirmed(h, run_id):
    """Keep a stopped, traceless original and queue one fresh same-side Session."""
    with LOCK:
        attempt = m.attempt(h, run_id)
        pair = m.task(h, attempt["pair_id"])
        if m.latest(m.attempts(h, pair["id"]), attempt["side"])["id"] != run_id:
            raise h.WorkflowError("该侧已有更新的 Session，不能重复重跑")
        if not can_retry_unconfirmed(h, attempt, pair):
            raise h.WorkflowError("当前不是可重跑的无轨迹首次会话")
        row = h.run_row(run_id)
        traces = Path(attempt["run_directory"]) / "traces"
        # Look before touching the old process. A late JSONL means this is a
        # real Session and must go through normal recovery instead.
        h.copy_container_traces(row, traces)
        if any(traces.rglob("*.jsonl")):
            raise h.WorkflowError("原会话已有 JSONL；请检查并恢复归档，不能按无轨迹重跑")
        attempt = m.attempt(h, run_id)
        if not can_retry_unconfirmed(h, attempt, pair):
            raise h.WorkflowError("原会话状态已变化，未停止或重跑")
        if attempt["status"] == "running":
            with h.db_connection() as db:
                changed = db.execute("""UPDATE pairwise_attempts SET status='stopped'
                    WHERE id=? AND status='running' AND NOT EXISTS (
                        SELECT 1 FROM runs WHERE id=? AND COALESCE(session_id,'')<>'')""",
                    (run_id, run_id)).rowcount
            if not changed:
                raise h.WorkflowError("原会话刚刚取得轨迹或状态已变化，未停止或重跑")
            h.cancel_background_job(f"run:{run_id}")
            h.update_run(run_id, phase="stopped", status_detail="正在停止原容器并保留无轨迹现场")
        state = h.run_command(
            ["docker", "inspect", "--format", "{{.State.Running}}", row["container_name"]],
            check=False, timeout=20,
        )
        if state.returncode != 0:
            raise h.WorkflowError("无法确认原容器状态；现场保持原样，未新建 Session")
        if state.stdout.strip() == "true":
            h.run_command(["docker", "stop", "--time", "10", row["container_name"]], timeout=30)
        elif state.stdout.strip() != "false":
            raise h.WorkflowError("原容器状态不明；未新建 Session")
        if h.docker_container_running(row["container_name"]):
            raise h.WorkflowError("原容器仍在运行；未新建 Session")
        # The stopped container, workspace, and terminal log stay in place.
        h.copy_container_traces(row, traces)
        if any(traces.rglob("*.jsonl")):
            error = "停止后发现原始 JSONL；现场已保留，请检查并恢复归档"
            m.update_attempt(h, run_id, status="failed", error=error, finished_at=h.now_text())
            h.update_run(run_id, phase="failed", status_detail=error, error=error)
            h.add_event(run_id, error, "warning")
            raise h.WorkflowError(error)
        error = "送题后未建立 Claude JSONL；原容器、工作区和终端日志已保留；本次不作为有效 A/B"
        m.update_attempt(h, run_id, status="failed", error=error, finished_at=h.now_text())
        h.update_turn(run_id, 1, status="failed")
        h.update_run(run_id, phase="failed", error=error)
        h.add_event(run_id, error, "warning")
        return _create_automatic_retry(
            h, m.attempt(h, run_id), "首轮启动未确认", preserved_without_trace=True)


def _preserve_first_turn_failure_and_retry(
        h, run_id, failure, *, failure_kind="首轮执行失败", api_failure=False):
    """Archive a failed first turn and rerun its side up to three total attempts."""
    attempt = m.attempt(h, run_id)
    pair = m.task(h, attempt["pair_id"])
    if m.latest(m.attempts(h, pair["id"]), attempt["side"])["id"] != run_id:
        raise h.WorkflowError("失败 Session 已被新的同侧尝试替代")
    if not attempt["prompt_sent"]:
        raise h.WorkflowError("题面尚未发送，不能按首轮失败自动重跑")
    h.update_run(run_id, status_detail=f"检测到{failure_kind}，正在保存原始轨迹并关闭旧 Session")
    row = h.run_row(run_id)
    if h.docker_container_running(row["container_name"]):
        h.run_command(["docker", "stop", "--time", "10", row["container_name"]], timeout=30)
        if h.docker_container_running(row["container_name"]):
            raise h.WorkflowError(f"{failure_kind}容器未能停止，现场已保留")
    h.export_and_remove_container(run_id, force=True)
    row = h.run_row(run_id)
    trace_path = Path(str(row["trajectory_path"] or ""))
    if not trace_path.is_file():
        raise h.WorkflowError(f"{failure_kind}的原始 JSONL 未能保存，停止自动重跑")
    trace_sha = hashlib.sha256(trace_path.read_bytes()).hexdigest()
    error = (
        f"Claude API 工程故障（不参与 GSB 与难度判断）：{failure}"
        if api_failure else
        f"{failure_kind}（本次不参与 GSB 与难度判断）：{failure}"
    )
    m.update_attempt(
        h, run_id, status="failed", error=error, trace_path=str(trace_path),
        trace_sha256=trace_sha, finished_at=h.now_text())
    h.update_turn(
        run_id, 1, status="failed", trajectory_path=str(trace_path),
        trajectory_sha256=trace_sha)
    h.update_run(run_id, phase="failed", error=error)
    h.add_event(run_id, f"{failure_kind}轨迹已保存；本次 Session 不作为有效 A/B", "warning")
    if attempt["number"] >= PAIRWISE_MAX_ATTEMPTS:
        h.update_run(
            run_id,
            status_detail=(f"{attempt['side']} 已达到 {PAIRWISE_MAX_ATTEMPTS} 次总尝试上限"
                           f"（含 {PAIRWISE_API_RETRY_LIMIT} 次自动重跑）；已停止该侧，请人工检查"),
        )
        h.add_event(run_id, "自动重跑次数已用完；未换题，也未影响另一侧结果", "error")
        return None
    return _create_automatic_retry(h, m.attempt(h, run_id), failure_kind)


def _preserve_api_failure_and_retry(h, run_id, api_error):
    """Archive an invalid API-error Session, then reroll without follow-up input."""
    return _preserve_first_turn_failure_and_retry(
        h, run_id, api_error, failure_kind="Claude API 工程故障", api_failure=True)


def checkpoint_excluded_paths(added):
    """Omit newly added generated files while retaining frozen baseline fixtures."""
    paths = {path for path in added if path}
    excluded = []
    for path in sorted(paths):
        parts = path.split("/")
        name = parts[-1]
        # Installed Go distributions include compiler binaries and standard
        # library sources. Keep project helpers elsewhere under .tools.
        go_toolchain = any(parts[index:index + 2] == [".tools", "go"]
                           for index in range(len(parts) - 2))
        if (any(part in CHECKPOINT_DEPENDENCY_DIRS for part in parts[:-1])
                or go_toolchain
                or name.endswith((".pyc", ".pyo", ".tsbuildinfo"))):
            excluded.append(path)
            continue
        # Named database fixtures are source material, not application state.
        if any(part in {"fixtures", "testdata", "test_data"} for part in parts[:-1]):
            continue
        database = next((path[:-len(suffix)] for suffix in CHECKPOINT_DATABASE_SIDECARS
                         if path.endswith(suffix)), path)
        if not database.endswith(CHECKPOINT_DATABASE_SUFFIXES):
            continue
        if (database != path
                or database.rsplit("/", 1)[-1] in CHECKPOINT_RUNTIME_DATABASE_NAMES
                or any(database + suffix in paths for suffix in CHECKPOINT_DATABASE_SIDECARS)):
            excluded.append(path)
    return excluded


def start_batch(h, pair_ids):
    """Queue first A/B attempts atomically using one freshly frozen environment."""
    if (not isinstance(pair_ids, list) or not 1 <= len(pair_ids) <= 200
            or any(not isinstance(value, str) or len(value) != 12
                   or any(char not in "0123456789abcdef" for char in value)
                   for value in pair_ids) or len(set(pair_ids)) != len(pair_ids)):
        raise h.WorkflowError("批量排队需提供 1 至 200 个不同的有效 Pair ID")
    if deployment.is_draining(h):
        raise h.WorkflowError("服务正在交接更新，暂不批量排队")
    if h.configured_max_parallel_runs() < 2:
        raise h.WorkflowError("A/B 同时运行要求并行上限至少为 2")

    def prepare():
        prepared = []
        for pair_id in pair_ids:
            pair = m.task(h, pair_id)
            if pair.get("deleted_at"):
                raise h.WorkflowError("该任务已从列表删除，不能再启动")
            attempts = m.attempts(h, pair_id)
            group, root = h.resolve_project_directory(pair["project_directory"])
            directories = {side: root / pair["project_folder"] / side for side in ("A", "B")}
            if not attempts and any(path.exists() for path in directories.values()):
                raise h.WorkflowError("A 或 B 执行目录已经存在，请检查现场")
            prepared.append((pair, attempts, group, directories))
        return prepared

    # Docker probing may take time; do not hold the worker lock while it runs.
    prepared = prepare()
    shared_config = (env.freeze(h) if any(not pair["config"] and not attempts
                                        for pair, attempts, _, _ in prepared) else None)
    created = []
    with LOCK:
        if deployment.is_draining(h):
            raise h.WorkflowError("服务正在交接更新，暂不批量排队")
        prepared = prepare()
        checked_configs = set()
        configs = {}
        for pair, attempts, _, _ in prepared:
            if attempts:
                continue  # Repeated requests never create new Sessions or retry failures.
            config = pair["config"] or shared_config
            if not config:
                raise h.WorkflowError("任务配置已变化，请重新提交批量排队")
            fingerprint = env.digest(config)
            if fingerprint not in checked_configs:
                env.verify_host(h, config)
                checked_configs.add(fingerprint)
            configs[pair["id"]] = config
        now = h.now_text()
        with h.db_connection() as db:
            db.execute("BEGIN IMMEDIATE")
            for pair, attempts, group, directories in prepared:
                if attempts:
                    continue
                current = db.execute(
                    "SELECT revision,deleted_at FROM pairwise_tasks WHERE id=?", (pair["id"],)
                ).fetchone()
                if (not current or current["deleted_at"] or current["revision"] != pair["revision"]
                        or db.execute("SELECT 1 FROM pairwise_attempts WHERE pair_id=?",
                                      (pair["id"],)).fetchone()):
                    raise h.WorkflowError("任务状态已变化，请重新提交批量排队")
                config = configs[pair["id"]]
                for side in ("A", "B"):
                    created.append(_insert_attempt(
                        db, h, pair, side, 1, config, group, directories[side], now))
                db.execute("""UPDATE pairwise_tasks SET config=?,human_confirmed=0,
                    auto_skip_reason='',revision=revision+1,updated_at=? WHERE id=?""",
                    (json.dumps(config, ensure_ascii=False), now, pair["id"]))
        # Every attempt is committed before the first worker can start.
        for run_id in created:
            enqueue(h, run_id)
        return {
            "queued_pairs": len(created) // 2,
            "queued_attempts": len(created),
            "items": [{"id": pair_id, "attempts": m.attempts(h, pair_id)}
                      for pair_id in pair_ids],
        }
