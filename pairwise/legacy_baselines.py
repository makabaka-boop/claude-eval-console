"""Read-only, frozen Bug baselines from completed projects in the old zzzz tree."""
import json
import re
import time
from datetime import datetime
from pathlib import Path

from . import candidate_bank


SUPPORTED_TASK_TYPES = {
    "0-1 代码生成", "0-1 重跑", "Feature 迭代", "Feature 迭代重跑",
    "Bug 修复", "Bug 修复重跑",
}


def source_id(run_id):
    return f"legacy:{run_id}"


def _ancestor_rows(h, row):
    """Return the exact root-to-snapshot path, excluding sibling branches."""
    lineage = []
    seen = set()
    current = row
    with h.db_connection() as db:
        while current and str(current["id"]) not in seen:
            seen.add(str(current["id"]))
            lineage.append(current)
            parent_id = str(current["source_run_id"] or "")
            current = db.execute("SELECT * FROM runs WHERE id=?", (parent_id,)).fetchone() if parent_id else None
    lineage.reverse()
    return lineage


def _successful_verification(row):
    try:
        commands = json.loads(row["verification_commands"] or "[]")
        second = json.loads(row["second_verification"] or "[]")
        first = json.loads(row["first_verification"] or "[]")
    except (TypeError, ValueError):
        return None
    if not isinstance(commands, list) or not commands or not all(
        isinstance(command, str) and command.startswith("docker compose ") for command in commands
    ):
        return None
    # A later failed verification cannot be hidden by an earlier pass.
    for checks in (second,) if second else (first,):
        if (isinstance(checks, list) and len(checks) == len(commands)
                and all(isinstance(check, dict) and check.get("command") == command
                        and type(check.get("exit_code")) is int and check["exit_code"] == 0
                        for check, command in zip(checks, commands))):
            return commands
    return None


def from_row(h, row, *, expected_sha="", expected_tree=""):
    """Return a source only if the old run and its local Git checkout still agree."""
    if (not row or row["deleted_at"] or row["phase"] != "complete"
            or row["project_directory"] != "zzzz"
            or row["task_type"] not in SUPPORTED_TASK_TYPES or not row["container_cleaned"]
            or row["project_category"] not in {"纯前端", "纯后端", "全栈"}
            or not str(row["first_prompt"] or "").strip()
            or not str(row["language_framework"] or "").strip()):
        return None
    commands = _successful_verification(row)
    repo_url = str(row["repo_url"] or "").removesuffix(".git")
    if not commands or not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo_url):
        return None
    root = candidate_bank.legacy_root(h).resolve()
    work = Path(row["repo_path"] or "").resolve()
    if not work.is_relative_to(root) or not (work / ".git").exists():
        return None
    try:
        if h.run_command(["git", "--no-optional-locks", "status", "--porcelain", "--untracked-files=all"],
                         cwd=work, timeout=30).stdout.strip():
            return None
        origin = h.run_command(["git", "remote", "get-url", "origin"],
                               cwd=work, timeout=30).stdout.strip().removesuffix(".git")
        if origin != repo_url:
            return None
        sha = h.run_command(["git", "rev-parse", "HEAD"], cwd=work, timeout=30).stdout.strip()
        tree = h.run_command(["git", "rev-parse", "HEAD^{tree}"], cwd=work, timeout=30).stdout.strip()
    except (OSError, h.WorkflowError):
        return None
    if (not re.fullmatch(r"[0-9a-f]{40}", sha) or not re.fullmatch(r"[0-9a-f]{40}", tree)
            or (expected_sha and sha != expected_sha)
            or (expected_tree and tree != expected_tree)):
        return None
    run_id = str(row["id"])
    lineage = _ancestor_rows(h, row)
    root_id = str(lineage[0]["id"] if lineage else run_id)
    pair = {
        "id": source_id(run_id), "title": str(row["repo_name"]),
        "repo_name": str(row["repo_name"]), "repo_url": repo_url,
        "prompt": str(row["first_prompt"]), "task_type": row["task_type"],
        "project_category": row["project_category"],
        "language_framework": row["language_framework"],
        "verification_commands": commands, "created_at": row["created_at"],
        "source_repo_url": "", "source_base_sha": "",
    }
    attempt = {"id": run_id, "side": "legacy", "status": "complete",
               "repo_path": str(work),
               "artifact_sha": sha, "artifact_tree": tree}
    return {"pair": pair, "attempt": attempt, "side": "legacy",
            "snapshot_url": f"{repo_url}/commit/{sha}", "legacy_run_id": run_id,
            "legacy_lineage": [
                {"id": str(item["id"]), "task_type": str(item["task_type"]),
                 "prompt": str(item["first_prompt"] or ""), "phase": str(item["phase"]),
                 "created_at": str(item["created_at"] or "")}
                for item in lineage
            ],
            "root_pair_id": source_id(root_id),
            "source_key": f"Bug 修复:{pair['id']}"}


def by_id(h, run_id, *, expected_sha="", expected_tree=""):
    with h.db_connection() as db:
        row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    return from_row(h, row, expected_sha=expected_sha, expected_tree=expected_tree)


def pick(h, *, ready_mode="any"):
    """Prefer newer verified products while allowing older unused snapshots."""
    from . import sources
    lineage_reuse = sources.bug_lineage_reuse_enabled(h)
    with h.db_connection() as db:
        all_rows = db.execute("SELECT * FROM runs WHERE project_directory='zzzz'").fetchall()
        rows = db.execute("""SELECT * FROM runs WHERE project_directory='zzzz'
            AND deleted_at IS NULL AND phase='complete'
            AND container_cleaned=1 AND repo_url LIKE 'https://github.com/%'
            ORDER BY created_at DESC,id DESC""").fetchall()
        consumed_ids = {
            str(item[0]).removeprefix("legacy:")
            for item in db.execute("""SELECT source_pair_id FROM pairwise_tasks
                WHERE task_type='Bug 修复' AND source_pair_id LIKE 'legacy:%'""").fetchall()
        }
    by_id = {str(row["id"]): row for row in all_rows}

    def origin(run_id):
        seen = set()
        current = str(run_id)
        while current and current not in seen:
            seen.add(current)
            row = by_id.get(current)
            parent = str(row["source_run_id"] or "") if row else ""
            if not parent:
                return current
            current = parent
        return current or str(run_id)

    consumed_origins = set() if lineage_reuse else {origin(run_id) for run_id in consumed_ids}
    # A completed legacy Bug consumes only its exact parent product. Other
    # independently verified snapshots in that family remain eligible.
    completed_bug_parents = {
        str(row["source_run_id"] or "") for row in all_rows
        if row["task_type"] in {"Bug 修复", "Bug 修复重跑"}
        and row["phase"] == "complete" and str(row["source_run_id"] or "")
    }
    if not lineage_reuse:
        consumed_origins.update(
            origin(row["id"]) for row in all_rows
            if row["task_type"] in {"Bug 修复", "Bug 修复重跑"}
            and row["phase"] == "complete"
        )
    ranked = []
    for rank, row in enumerate(rows):
        if (row["task_type"] not in SUPPORTED_TASK_TYPES
                or (not lineage_reuse and row["task_type"] in {"Bug 修复", "Bug 修复重跑"})):
            continue
        root_id = origin(row["id"])
        if root_id in consumed_origins:
            continue
        # Reuse never means replaying the same product; another verified
        # snapshot in the same lineage can still be a separate source.
        if lineage_reuse and (str(row["id"]) in consumed_ids
                              or str(row["id"]) in completed_bug_parents):
            continue
        root_pair_id = source_id(root_id)
        if sources.lineage_unavailable(h, root_pair_id, "Bug 修复"):
            continue
        key = source_id(row["id"])
        lock = sources.get_lock(h, key, "Bug 修复")
        if sources.unavailable(lock):
            continue
        with h.db_connection() as db:
            root_stats = db.execute("""SELECT COUNT(*) AS source_count,
                    COALESCE(SUM(failure_count),0) AS failures,
                    COALESCE(MAX(updated_at),'') AS updated_at
                FROM pairwise_source_locks
                WHERE source_root_pair_id=? AND task_type='Bug 修复'""",
                (root_pair_id,)).fetchone()
        recently_failed = False
        if int(root_stats["failures"]) > 0:
            try:
                updated = datetime.strptime(root_stats["updated_at"], "%Y-%m-%d %H:%M:%S %z").timestamp()
                recently_failed = time.time() - updated < 60
            except (TypeError, ValueError):
                pass
        if recently_failed:
            continue
        # Untouched lineages first, then the least-recently attempted source.
        ranked.append(((bool(root_stats["source_count"]), str(root_stats["updated_at"]),
                        int(root_stats["failures"]), rank), row))

    for _, row in sorted(ranked, key=lambda item: item[0]):
        source = from_row(h, row)
        if source:
            if ready_mode != "any":
                buffered = candidate_bank.has_ready(h, "Bug 修复", source)
                if ready_mode == "only" and not buffered:
                    continue
                if ready_mode == "exclude" and buffered:
                    continue
            return source
    return None
