"""Preview-only Feature iteration batches for the question bank.

The source walk uses task/attempt metadata. Generation receives an isolated
archive of only the selected leaf commit; historical material is prompt text.
"""
import difflib
import json
import re
import subprocess
import tarfile
import tempfile
import threading
import uuid
from pathlib import Path, PurePosixPath

from . import candidate_bank, history, models, sources, workflow


PAGE_SIZE = 10
ACTIVE = {"queued", "starting", "running", "recovering", "archiving", "upstream_paused"}
SHA = re.compile(r"[0-9a-f]{40}\Z")
PROMPT_MARKER = re.compile(r"<!-- prompt-start -->\s*(.*?)\s*<!-- prompt-end -->", re.S)


def _json(value, fallback):
    try:
        return json.loads(value) if value else fallback
    except (TypeError, ValueError):
        return fallback


def _history_prompts(h):
    """Collect text only, including soft-deleted Pair prompts and global history."""
    with h.db_connection() as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        prompts = [row[0] for row in db.execute("SELECT prompt FROM pairwise_tasks WHERE prompt<>''")]
        if "pairwise_legacy_prompt_index" in tables:
            prompts.extend(row[0] for row in db.execute(
                "SELECT prompt FROM pairwise_legacy_prompt_index WHERE prompt<>''"))
        if "pairwise_candidate_bank" in tables:
            prompts.extend(row[0] for row in db.execute(
                "SELECT prompt FROM pairwise_candidate_bank WHERE prompt<>''"))
        if "pairwise_zero_one_candidates" in tables:
            prompts.extend(str(_json(row[0], {}).get("first_prompt") or "") for row in db.execute(
                "SELECT draft FROM pairwise_zero_one_candidates WHERE status IN ('ready','added','queued')"))
        prompts.extend(str(_json(row[0], {}).get("prompt") or "") for row in db.execute(
            "SELECT draft FROM pairwise_iteration_candidates WHERE status IN ('ready','added','queued')"))
    # Both files contain explicitly delimited prompt blocks. Never parse their
    # metadata, review explanations, traces, or implementation notes.
    for path in (Path(h.APP_DIR) / "history-prompts.md", h.PAIRWISE_HISTORY_PATH):
        try:
            prompts.extend(match.group(1) for match in PROMPT_MARKER.finditer(
                path.read_text(encoding="utf-8")))
        except (OSError, UnicodeError):
            pass
    return list(dict.fromkeys(text.strip() for text in prompts if str(text).strip()))


def _history_chunks(prompts, limit=85000):
    chunks, current, size = [], [], 0
    for index, prompt in enumerate(prompts, 1):
        entry = f"[{index}] {prompt}"
        if current and size + len(entry) > limit:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(entry)
        size += len(entry)
    if current:
        chunks.append("\n".join(current))
    return chunks or ["（暂无历史题面）"]


def _metadata(h):
    with h.db_connection() as db:
        tasks = {row["id"]: dict(row) for row in db.execute(
            "SELECT id,project_number,sequence_number,display_number,task_type,"
            "source_pair_id,source_repo_url,source_base_sha,repo_url,repo_name,"
            "human_confirmed,gsb,verification_commands,"
            "project_directory,project_category,language_framework,reproducibility "
            "FROM pairwise_tasks")}
        attempts = {}
        for row in db.execute("SELECT a.id,a.pair_id,a.side,a.number,a.status,a.finished_at,"
                              "a.artifact_sha,a.artifact_tree,a.verification,r.repo_path "
                              "FROM pairwise_attempts a "
                              "JOIN runs r ON r.id=a.id ORDER BY a.side,a.number"):
            attempts.setdefault(row["pair_id"], []).append(dict(row))
        locks = {row["source_pair_id"]: dict(row) for row in db.execute(
            "SELECT source_pair_id,state,cooldown_until,lease_until "
            "FROM pairwise_source_locks WHERE task_type='Feature 迭代'")}
    return tasks, attempts, locks


def _chosen_side(pair, attempts):
    latest = {side: next((item for item in reversed(attempts)
                          if item["side"] == side), None) for side in ("A", "B")}
    def completed(item):
        return bool(item and item["status"] == "complete" and item["finished_at"]
                    and SHA.fullmatch(str(item["artifact_sha"] or ""))
                    and SHA.fullmatch(str(item["artifact_tree"] or "")))
    verdict = ""
    if pair["human_confirmed"]:
        verdict = pair["gsb"]
    if verdict in ("A 更好", "B 更好", "Same"):
        preferred = "B" if verdict == "B 更好" else "A"
        return latest[preferred] if completed(latest[preferred]) else None
    for side in ("A", "B"):
        item = latest[side]
        if not completed(item):
            continue
        checked = dict(item, verification=_json(item["verification"], []))
        parent = dict(pair, verification_commands=_json(pair["verification_commands"], []))
        if sources.verification_passed(parent, checked):
            return item
    return None


def _lineages(tasks, attempts):
    parents = {item["id"]: item["source_pair_id"] for item in tasks.values()
               if item["source_pair_id"] in tasks}
    # Resolve old descendants using only source metadata, never old checkouts.
    identity = {}
    for parent_id, parent in tasks.items():
        for attempt in attempts.get(parent_id, []):
            if attempt["artifact_sha"]:
                identity.setdefault((parent["repo_url"], attempt["artifact_sha"]), set()).add(parent_id)
    for item in tasks.values():
        if item["id"] not in parents and item["source_base_sha"]:
            matches = identity.get((item["source_repo_url"], item["source_base_sha"]), set())
            if len(matches) == 1 and item["id"] not in matches:
                parents[item["id"]] = next(iter(matches))
    children = {pair_id: [] for pair_id in tasks}
    for child, parent in parents.items():
        children[parent].append(child)
    return parents, children


def _family_name(repo_name):
    """Ignore generated iteration/bug suffixes when applying the eight-task cap."""
    name = str(repo_name or "").casefold()
    while True:
        shortened = re.sub(r"-(?:iteration|feature|bug)(?:-fix)?-\d+\Z", "", name)
        if shortened == name:
            return name
        name = shortened


def _available_sources(h, start=0, end=0, *, exclude_ready=False):
    tasks, attempts, locks = _metadata(h)
    parents, children = _lineages(tasks, attempts)
    ready = set()
    if exclude_ready:
        with h.db_connection() as db:
            ready = {str(_json(row[0], {}).get("pair_id") or "") for row in db.execute(
                "SELECT source FROM pairwise_iteration_candidates WHERE status IN ('pending','generating','ready')")}
    roots = sorted((item for item in tasks.values() if item["id"] not in parents),
                   key=lambda item: (item["project_number"], item["sequence_number"]))
    family_counts = {}
    for root in roots:
        family_counts.setdefault(_family_name(root["repo_name"]), set())
    for item in tasks.values():
        current, visited = item["id"], set()
        while current in parents and current not in visited:
            visited.add(current)
            current = parents[current]
        owner = tasks.get(current, item)
        if item["task_type"] in ("Feature 迭代", "Bug 修复") and item["id"] != owner["id"]:
            family_counts.setdefault(_family_name(owner["repo_name"]), set()).add(item["id"])
    result = []
    for root in roots:
        number = int(root["project_number"])
        if start and number < start or end and number > end:
            continue
        stack, lineage, seen = [(root["id"], 0)], [], set()
        while stack:
            pair_id, depth = stack.pop()
            if pair_id in seen:
                continue
            seen.add(pair_id)
            lineage.append((pair_id, depth))
            stack.extend((child, depth + 1) for child in children[pair_id])
        if len(family_counts.get(_family_name(root["repo_name"]), ())) >= 8:
            continue
        if any(any(attempt["status"] in ACTIVE for attempt in attempts.get(pair_id, []))
               for pair_id, _ in lineage):
            continue
        successful = []
        for pair_id, depth in lineage:
            pair = tasks[pair_id]
            chosen = _chosen_side(pair, attempts.get(pair_id, []))
            if chosen:
                successful.append((depth, int(pair["sequence_number"]), pair, chosen))
        if not successful:
            continue
        _, _, pair, chosen = max(successful, key=lambda item: (item[0], item[1]))
        if not pair["repo_url"] or not chosen["repo_path"]:
            continue
        prefix = f"{number:04d}-"
        suffix = max((int(item["display_number"][len(prefix):]) for item in tasks.values()
                      if item["display_number"].startswith(prefix)
                      and item["display_number"][len(prefix):].isdigit()), default=0) + 1
        if pair["id"] in ready:
            continue
        # Never fall back to an ancestor when a newer Feature child has already
        # consumed this parent or is still waiting for its artifact.
        with h.db_connection() as db:
            if sources.consumed(db, pair["id"], "Feature 迭代"):
                continue
        if sources.unavailable(locks.get(pair["id"])):
            continue
        result.append({
            "root_number": number, "root_repo_name": root["repo_name"],
            "child_repo_name": f"{root['repo_name'][:75]}-iteration-{suffix}",
            "lineage_count": len(lineage), "pair_id": pair["id"],
            "pair_name": pair["display_number"], "repo_name": pair["repo_name"],
            "repo_url": pair["repo_url"], "project_directory": pair["project_directory"],
            "project_category": pair["project_category"], "language_framework": pair["language_framework"],
            "reproducibility": pair["reproducibility"],
            "verification_commands": _json(pair["verification_commands"], []),
            "attempt_id": chosen["id"], "side": chosen["side"],
            "sha": chosen["artifact_sha"], "tree": chosen["artifact_tree"],
            "repo_path": chosen["repo_path"],
        })
    return result


def _current(h, source):
    matches = [item for item in _available_sources(h, source["root_number"], source["root_number"])
               if item["pair_id"] == source["pair_id"] and item["attempt_id"] == source["attempt_id"]
               and item["side"] == source["side"] and item["sha"] == source["sha"]]
    if not matches:
        raise h.WorkflowError("来源已被使用或最新成功叶节点已变化，请重新生成")
    return matches[0]


def _archive(h, source, destination):
    repo = Path(source["repo_path"])
    if not repo.is_dir() or not SHA.fullmatch(source["sha"]):
        raise h.WorkflowError("来源代码或提交号不可用")
    target = destination / "leaf.tar"
    completed = subprocess.run(
        ["git", "archive", "--format=tar", f"--output={target}", source["sha"]],
        cwd=repo, capture_output=True, text=True, timeout=90, check=False)
    if completed.returncode:
        raise h.WorkflowError("无法读取最新叶节点的代码提交")
    tree_check = subprocess.run(["git", "rev-parse", source["sha"] + "^{tree}"],
                                cwd=repo, capture_output=True, text=True, timeout=30, check=False)
    if tree_check.returncode or tree_check.stdout.strip() != source["tree"]:
        raise h.WorkflowError("来源提交与归档 Git tree 不一致")
    work = destination / "source"
    work.mkdir()
    with tarfile.open(target) as archive:
        members = archive.getmembers()
        if any(member.issym() or member.islnk() or member.isdev()
               or PurePosixPath(member.name).is_absolute()
               or ".." in PurePosixPath(member.name).parts for member in members):
            raise h.WorkflowError("来源提交包含不安全的归档路径")
        archive.extractall(work, members=members)
    return work


GENERATION_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["title", "prompt", "estimated_lines"],
    "properties": {
        "title": {"type": "string"}, "prompt": {"type": "string"},
        "estimated_lines": {"type": "integer"},
    },
}
REVIEW_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["approved", "difficulty", "duplicate", "reason"],
    "properties": {
        "approved": {"type": "boolean"},
        "difficulty": {"type": "string", "enum": ["简单", "中等", "困难", "地狱"]},
        "duplicate": {"type": "boolean"}, "reason": {"type": "string"},
    },
}


def _generate_one(h, source, persist=None):
    _current(h, source)
    prompts = _history_prompts(h)
    with h.db_connection() as db:
        lineage_prompts = [row[0] for row in db.execute(
            "SELECT prompt FROM pairwise_tasks WHERE project_number=? ORDER BY sequence_number",
            (source["root_number"],))]
    prompts = list(dict.fromkeys([*lineage_prompts, *prompts]))
    chunks = _history_chunks(prompts)
    with tempfile.TemporaryDirectory(prefix="pairwise-iteration-") as temporary:
        work = _archive(h, source, Path(temporary))
        instruction = f"""只读取当前工作目录中的代码、README 和测试；它是编号 {source['root_number']:04d} 的最新成功叶节点 {source['pair_name']} 的 {source['side']} 侧提交的隔离副本。不要访问其他路径、历史代码、提交轨迹或评审理由。根据真实代码设计一项新的 Feature 迭代，输出题目标题、一段 350～550 字中文需求和预计手写代码行数。第一句必须是自然具体的业务场景。题面只含一个明确工程核心、一条主要用户流程，真实牵动三至四个必要模块或层次和多个文件；难度在人类执行视角恰好达到困难下沿，需有关键设计取舍及复杂状态、兼容、并发裁决或异常链路之一，但不能到地狱。写明跨模块契约、失败时的数据保护、向后兼容、确定性本地自动化验收。不要以普通 CRUD、字段校验、接口或补测试凑难度；不要设计漫长验证、高并发或纯性能压力，也不要在题面讲这些内部限制。不要照搬历史题面的核心行为、状态模型、算法目标、交互骨架或最终可观察结果，不能只改业务名、字段或接口路径。历史资料只能用于题面文本查重，不能据此推测历史实现。预计行数只供表格展示，不写进题面。以下是同项目题面及部分全局历史题面，所有历史题面将在独立复核时逐条检查：\n{chunks[0]}"""
        generated = h.run_codex_structured(instruction, GENERATION_SCHEMA, work,
                                           "iteration-bank-generate", 20 * 60)
        prompt = str(generated.get("prompt") or "").strip()
        title = str(generated.get("title") or "").strip()
        lines = generated.get("estimated_lines")
        if not title or not 350 <= len(prompt) <= 550 or "\n" in prompt:
            raise h.WorkflowError("题面不是 350～550 字的单段中文，需重新生成")
        if type(lines) is not int or lines < 1:
            raise h.WorkflowError("预计代码行数无效")
        draft = {"title": title, "prompt": prompt, "estimated_lines": lines,
                 "review_checks": []}
        if persist:
            persist(draft)
        if any(difflib.SequenceMatcher(None, prompt, old).ratio() >= .80 for old in prompts):
            raise h.WorkflowError("与历史题面文本高度相似")
        for index, context in enumerate(chunks, 1):
            review = h.run_codex_structured(
                "独立审核这道候选题。只读取当前目录的最新叶节点代码、README 和测试，以及下面的历史题面文本；"
                "不要读取历史代码、轨迹、提交产物或评审理由。审查是否真实贴合现有项目、属于困难偏低而非中等或地狱、"
                "只有一个工程核心、三至四个必要模块、短周期确定性验收；difficulty 只能填简单、中等、困难、地狱，"
                "达到困难下沿也必须精确填‘困难’，不要填‘困难偏低’。逐条比较本组每道历史题面的核心行为、"
                "状态模型、算法目标、交互骨架和最终可观察结果，任何实质重复都判 duplicate=true、approved=false。"
                "不能因换名或字面相似度低而放行。若贴合项目、属于困难、无实质重复且其余范围条件满足，"
                "必须填 approved=true、duplicate=false；否则 approved=false，reason 必须明确指出不通过的条件，"
                "不能只写肯定评价。\n候选："
                + prompt + f"\n历史题面分组 {index}/{len(chunks)}：\n" + context,
                REVIEW_SCHEMA, work, "iteration-bank-review", 20 * 60)
            draft["review_checks"].append({
                "group": index, "approved": review.get("approved"),
                "duplicate": review.get("duplicate"),
                "difficulty": review.get("difficulty"),
                "reason": str(review.get("reason") or "")[:1000],
            })
            if persist:
                persist(draft)
            if review.get("approved") is not True or review.get("duplicate") is not False \
                    or review.get("difficulty") != "困难":
                raise h.WorkflowError(
                    f"独立复核未通过（第 {index}/{len(chunks)} 组；"
                    f"approved={review.get('approved')!r}，duplicate={review.get('duplicate')!r}，"
                    f"difficulty={review.get('difficulty')!r}）："
                    + str(review.get("reason") or "未给出拒绝依据")[:400])
    _current(h, source)
    draft["difficulty"] = "困难"
    return draft


def _rows(db, batch_id):
    return db.execute("SELECT candidate.*,task.display_number AS actual_number "
                      "FROM pairwise_iteration_candidates candidate "
                      "LEFT JOIN pairwise_tasks task ON task.id=candidate.pair_id "
                      "WHERE candidate.batch_id=? ORDER BY candidate.slot", (batch_id,)).fetchall()


def get(h, batch_id):
    models.initialize(h)
    with h.db_connection() as db:
        batch = db.execute("SELECT * FROM pairwise_iteration_batches WHERE id=?", (batch_id,)).fetchone()
        if not batch:
            raise h.WorkflowError("迭代题面批次不存在")
        rows = _rows(db, batch_id)
    return {**dict(batch), "candidates": [
        {**dict(row), "source": _json(row["source"], {}), "draft": _json(row["draft"], {})}
        for row in rows]}


def listing(h, query):
    models.initialize(h)
    try:
        requested = int(query.get("page", ["1"])[0])
    except (TypeError, ValueError):
        raise h.WorkflowError("页码必须是正整数")
    if requested < 1:
        raise h.WorkflowError("页码必须是正整数")
    with h.db_connection() as db:
        db.execute("BEGIN")
        total = db.execute("SELECT COUNT(*) FROM pairwise_iteration_candidates").fetchone()[0]
        active = db.execute("SELECT COUNT(*) FROM pairwise_iteration_batches WHERE status IN ('queued','running')").fetchone()[0]
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(requested, pages)
        rows = db.execute(
            "SELECT candidate.*,batch.status AS batch_status,batch.detail AS batch_detail,"
            "batch.created_at AS batch_created_at,task.display_number AS actual_number "
            "FROM pairwise_iteration_candidates candidate "
            "JOIN pairwise_iteration_batches batch ON batch.id=candidate.batch_id "
            "LEFT JOIN pairwise_tasks task ON task.id=candidate.pair_id "
            "ORDER BY batch.created_at DESC,batch.rowid DESC,candidate.slot "
            "LIMIT ? OFFSET ?", (PAGE_SIZE, (page - 1) * PAGE_SIZE)).fetchall()
    return {"items": [{**dict(row), "source": _json(row["source"], {}),
                       "draft": _json(row["draft"], {})} for row in rows],
            "total": total, "page": page, "pages": pages,
            "page_size": PAGE_SIZE, "active_batches": active}


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
            db.execute("UPDATE pairwise_iteration_batches SET " + ",".join(fields) + " WHERE id=?",
                       (*values, batch_id))
        if slot is not None:
            fields, values = [], []
            for key, value in (("status", slot_status), ("draft", json.dumps(draft, ensure_ascii=False)
                                if draft is not None else None), ("error", error)):
                if value is not None:
                    fields.append(key + "=?")
                    values.append(value)
            if fields:
                db.execute("UPDATE pairwise_iteration_candidates SET " + ",".join(fields)
                           + " WHERE batch_id=? AND slot=?", (*values, batch_id, slot))


def _generate(h, batch_id):
    batch = get(h, batch_id)
    _update(h, batch_id, status="running", detail="正在生成第一道迭代题")
    for item in batch["candidates"]:
        if item["status"] not in ("pending", "generating"):
            continue
        slot, source = item["slot"], item["source"]
        label = f"第 {slot}/{batch['count']} 道 · {source['root_number']:04d}"
        _update(h, batch_id, detail=label + "：读取最新叶节点", slot=slot, slot_status="generating")
        try:
            draft = _generate_one(h, source, persist=lambda value: _update(
                h, batch_id, slot=slot, draft=value))
            _update(h, batch_id, detail=label + "：已通过复核", slot=slot,
                    slot_status="ready", draft=draft, error="")
        except Exception as exc:
            _update(h, batch_id, detail=label + "：未通过，继续下一题", slot=slot,
                    slot_status="failed", error=str(exc)[:2000])
    final = get(h, batch_id)
    passed = sum(item["status"] in ("ready", "added", "queued") for item in final["candidates"])
    failed = any(item["status"] == "failed" for item in final["candidates"])
    _update(h, batch_id, status="partial" if failed else "complete",
            detail=f"已生成 {passed}/{batch['count']} 道；等待人工核对")


def create(h, payload):
    models.initialize(h)
    count = payload.get("count", 5)
    if type(count) is not int or count not in (5, 10):
        raise h.WorkflowError("每批只能生成 5 或 10 道迭代题")
    try:
        start, end = int(payload.get("start_number") or 0), int(payload.get("end_number") or 0)
    except (TypeError, ValueError):
        raise h.WorkflowError("编号范围必须是正整数")
    if start < 0 or end < 0 or start and end and start > end:
        raise h.WorkflowError("编号范围无效")
    with h.db_connection() as db:
        active = db.execute("SELECT id FROM pairwise_iteration_batches WHERE status IN ('queued','running') LIMIT 1").fetchone()
    if active:
        return get(h, active[0])
    selected = _available_sources(h, start, end, exclude_ready=True)[:count]
    if not selected:
        raise h.WorkflowError("该编号范围暂无可用的最新成功叶节点；请检查在途子任务、来源占用和八题上限")
    batch_id = uuid.uuid4().hex[:12]
    with h.db_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        active = db.execute("SELECT id FROM pairwise_iteration_batches WHERE status IN ('queued','running') LIMIT 1").fetchone()
        if active:
            batch_id = active[0]
        else:
            now = h.now_text()
            db.execute("INSERT INTO pairwise_iteration_batches "
                       "(id,status,detail,count,start_number,end_number,created_at,updated_at) "
                       "VALUES (?,'queued','等待生成',?,?,?,?,?)",
                       (batch_id, len(selected), start, end, now, now))
            db.executemany("INSERT INTO pairwise_iteration_candidates(batch_id,slot,source) VALUES (?,?,?)",
                           [(batch_id, slot, json.dumps(source, ensure_ascii=False))
                            for slot, source in enumerate(selected, 1)])
    if not active:
        threading.Thread(target=_generate, args=(h, batch_id), daemon=True,
                         name=f"iteration-bank-{batch_id}").start()
    return get(h, batch_id)


def retry_candidate(h, batch_id, slot):
    """Regenerate one failed preview; never create or start a Pair."""
    retry_candidates(h, [{"batch_id": batch_id, "slot": slot}])
    return get(h, batch_id)


def _retry_many(h, batch_ids):
    for batch_id in batch_ids:
        try:
            _generate(h, batch_id)
        except Exception as exc:
            with h.db_connection() as db:
                db.execute("UPDATE pairwise_iteration_candidates SET status='failed',error=? "
                           "WHERE batch_id=? AND status IN ('pending','generating')",
                           (str(exc)[:2000], batch_id))
            _update(h, batch_id, status="partial", detail="批量重试中断；已完成候选仍可使用")


def retry_candidates(h, targets):
    """Atomically queue failed previews, then regenerate batches serially."""
    models.initialize(h)
    if not isinstance(targets, list) or not 1 <= len(targets) <= 20:
        raise h.WorkflowError("每次只能重试 1 至 20 道失败的迭代题")
    normalized = []
    for item in targets:
        if not isinstance(item, dict):
            raise h.WorkflowError("所选候选题格式无效")
        batch_id, slot = item.get("batch_id"), item.get("slot")
        if not isinstance(batch_id, str) or not re.fullmatch(r"[a-f0-9]{12}", batch_id) \
                or type(slot) is not int or slot < 1:
            raise h.WorkflowError("所选候选题标识无效")
        normalized.append((batch_id, slot))
    if len(set(normalized)) != len(normalized):
        raise h.WorkflowError("所选候选题有重复项")
    with h.db_connection() as db:
        rows = [db.execute("SELECT source,status FROM pairwise_iteration_candidates "
                           "WHERE batch_id=? AND slot=?", key).fetchone() for key in normalized]
    if any(not row or row["status"] != "failed" for row in rows):
        raise h.WorkflowError("所选题目已变化；只能重试生成失败的候选题")
    sources_seen = set()
    for row in rows:
        source = _json(row["source"], {})
        identity = (source.get("pair_id"), source.get("side"), source.get("sha"))
        if identity in sources_seen:
            raise h.WorkflowError("同一来源不能在一次批量操作中重复生成")
        sources_seen.add(identity)
        _current(h, source)
    batch_ids = list(dict.fromkeys(batch_id for batch_id, _ in normalized))
    with h.db_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        active = db.execute(
            "SELECT id FROM pairwise_iteration_batches WHERE status IN ('queued','running') LIMIT 1"
        ).fetchone()
        if active:
            raise h.WorkflowError("已有迭代题面批次正在生成，请稍后重试")
        for batch_id, slot in normalized:
            changed = db.execute(
                "UPDATE pairwise_iteration_candidates SET status='pending',error='' "
                "WHERE batch_id=? AND slot=? AND status='failed'", (batch_id, slot)
            ).rowcount
            if not changed:
                raise h.WorkflowError("候选状态已变化，请刷新列表")
        for batch_id in batch_ids:
            db.execute("UPDATE pairwise_iteration_batches SET status='queued',detail=?,updated_at=? WHERE id=?",
                       ("等待批量重新生成", h.now_text(), batch_id))
    try:
        threading.Thread(target=_retry_many, args=(h, batch_ids), daemon=True,
                         name=f"iteration-bank-retry-{batch_ids[0]}").start()
    except Exception as exc:
        with h.db_connection() as db:
            for batch_id, slot in normalized:
                db.execute("UPDATE pairwise_iteration_candidates SET status='failed',error=? "
                           "WHERE batch_id=? AND slot=? AND status='pending'",
                           (str(exc)[:2000], batch_id, slot))
            for batch_id in batch_ids:
                db.execute("UPDATE pairwise_iteration_batches SET status='partial',detail='重试线程未启动',"
                           "updated_at=? WHERE id=?", (h.now_text(), batch_id))
        raise
    return {"accepted": len(normalized), "batch_ids": batch_ids}


def add_candidate(h, batch_id, slot, *, queue=False, confirmed=False):
    if confirmed is not True:
        raise h.WorkflowError("请先人工核对题面并确认")
    item = next((row for row in get(h, batch_id)["candidates"] if row["slot"] == slot), None)
    if not item or item["status"] not in ("ready", "added", "queued"):
        raise h.WorkflowError("这道迭代题尚未生成合规题面")
    source, draft = item["source"], item["draft"]
    pair_id = item["pair_id"]
    if not pair_id:
        repo_name = source["child_repo_name"]
        with h.db_connection() as db:
            existing = db.execute("SELECT id,prompt,source_pair_id,source_artifact_sha "
                                  "FROM pairwise_tasks WHERE repo_name=?", (repo_name,)).fetchone()
        if existing:
            if existing["prompt"] != draft["prompt"] \
                    or existing["source_pair_id"] != source["pair_id"] \
                    or existing["source_artifact_sha"] != source["sha"]:
                raise h.WorkflowError("拟用仓库名已被其他任务占用")
            pair = models.task(h, existing["id"])
        else:
            _current(h, source)
            # Numbering and the source binding are checked inside models.create.
            pair = _create_pair(h, source, draft, repo_name)
        pair_id = pair["id"]
        history.append(h, pair)
        with h.db_connection() as db:
            db.execute("UPDATE pairwise_iteration_candidates SET pair_id=?,status='added' "
                       "WHERE batch_id=? AND slot=?", (pair_id, batch_id, slot))
    if queue:
        pair = models.task(h, pair_id)
        if pair.get("deleted_at"):
            raise h.WorkflowError("此题已从任务列表移除，不能重新排队")
        with h.db_connection() as db:
            attempts = db.execute("SELECT COUNT(*) FROM pairwise_attempts WHERE pair_id=?", (pair_id,)).fetchone()[0]
        if not attempts:
            workflow.start(h, pair_id, None)
        with h.db_connection() as db:
            db.execute("UPDATE pairwise_iteration_candidates SET status='queued' "
                       "WHERE batch_id=? AND slot=?", (batch_id, slot))
    return get(h, batch_id)


def _create_pair(h, source, draft, repo_name):
    payload = {
        "title": draft["title"], "prompt": draft["prompt"],
        "task_type": "Feature 迭代", "difficulty": "困难",
        "language_framework": source["language_framework"],
        "project_category": source["project_category"],
        "reproducibility": source["reproducibility"],
        "topic_confirmed": True,
        "snapshot_url": source["repo_url"].rstrip("/") + "/commit/" + source["sha"],
        "repo_name": repo_name, "project_directory": source["project_directory"],
        "verification_commands": source["verification_commands"],
        "source_pair_id": source["pair_id"], "source_attempt_id": source["attempt_id"],
        "source_side": source["side"], "source_artifact_sha": source["sha"],
        "source_artifact_tree": source["tree"],
        "difficulty_preflight": {"estimated_handwritten_lines": draft["estimated_lines"],
                                 "scope_label": "困难偏低"},
    }
    try:
        pair = models.create(h, payload, enforce_prompt_dedup=True)
    except h.WorkflowError:
        with h.db_connection() as db:
            existing = db.execute("SELECT id,prompt,source_pair_id,source_artifact_sha "
                                  "FROM pairwise_tasks WHERE repo_name=?", (repo_name,)).fetchone()
        if not existing or existing["prompt"] != draft["prompt"] \
                or existing["source_pair_id"] != source["pair_id"] \
                or existing["source_artifact_sha"] != source["sha"]:
            raise
        pair = models.task(h, existing["id"])
    return pair
