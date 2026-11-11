"""Fresh Pair-wise prompt corpus, isolated from the legacy history."""
import json
import re


HEADER = (
    "# GSB 历史题库\n\n"
    "这是 09/16 Pair-wise GSB 的独立题库。记录 Pair-wise 实际创建的 0-1、Feature 迭代和 Bug 修复题面，"
    "与旧版历史题库隔离，供后续自动出题去重。\n\n"
)


def ensure(h):
    path = h.PAIRWISE_HISTORY_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(HEADER, encoding="utf-8")
    return path


def records(h, limit=60):
    """Read only the new Pair-wise corpus (DB + its dedicated Markdown)."""
    with h.db_connection() as db:
        rows = db.execute(
            """SELECT id,repo_name,prompt,task_type,language_framework,title,project_category
               FROM pairwise_tasks
               WHERE deleted_at IS NULL
                 AND task_type IN ('0-1 代码生成','Feature 迭代','Bug 修复')
               ORDER BY created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    result = [
        h.history_record(
                str(row["repo_name"] or ""),
                str(row["task_type"] or ""),
            str(row["prompt"] or ""),
            {
                "summary": h.summarize_historical_prompt(str(row["prompt"] or "")),
                "language_framework": str(row["language_framework"] or ""),
                "project_category": str(row["project_category"] or "未记录"),
            },
        )
        for row in rows
    ]
    try:
        markdown = ensure(h).read_text(encoding="utf-8")
    except OSError:
        markdown = ""
    pattern = re.compile(
        r"<!-- gsb-task-start (?P<meta>\{.*?\}) -->.*?"
        r"<!-- prompt-start -->\s*(?P<prompt>.*?)\s*<!-- prompt-end -->",
        re.S,
    )
    known = {item["prompt"] for item in result}
    for match in reversed(list(pattern.finditer(markdown))):
        prompt = re.sub(r"\s+", " ", match.group("prompt")).strip()[:1800]
        if not prompt or prompt in known:
            continue
        try:
            metadata = json.loads(match.group("meta"))
        except json.JSONDecodeError:
            metadata = {}
        result.append(
            h.history_record(
                str(metadata.get("repo_name") or "gsb-history"),
                str(metadata.get("task_type") or "0-1 代码生成"),
                prompt,
                metadata,
            )
        )
        known.add(prompt)
        if len(result) >= limit:
            break
    return result[:limit]


def next_project_number(h):
    with h.db_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT value FROM settings WHERE key='pairwise_blueprint_cursor'"
        ).fetchone()
        try:
            number = int(row["value"]) + 1 if row else 1
        except (TypeError, ValueError):
            number = 1
        db.execute(
            """INSERT INTO settings(key,value,updated_at) VALUES ('pairwise_blueprint_cursor',?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
            (str(number), h.now_text()),
        )
    return number


def append(h, pair):
    task_type = str(pair.get("task_type") or "")
    if task_type not in {"0-1 代码生成", "Feature 迭代", "Bug 修复"}:
        return
    pair_id = str(pair.get("id") or "")
    prompt = str(pair.get("prompt") or "").strip()
    if not pair_id or not prompt:
        raise h.WorkflowError("GSB 历史题库记录缺少 Pair ID 或题面")
    metadata = {
        "pair_id": pair_id,
        "repo_name": str(pair.get("repo_name") or ""),
        "task_type": task_type,
        "project_category": str(pair.get("project_category") or "未记录"),
        "language_framework": str(pair.get("language_framework") or ""),
        "summary": h.summarize_historical_prompt(prompt),
    }
    marker = f'"pair_id": "{pair_id}"'
    with h.HISTORY_LOCK:
        path = ensure(h)
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise h.WorkflowError("无法读取 GSB 历史题库") from exc
        if marker in content:
            return
        entry = (
            f'<!-- gsb-task-start {json.dumps(metadata, ensure_ascii=False)} -->\n'
            f"## {pair.get('title') or pair.get('repo_name') or '未命名题目'}\n\n"
            f"- Pair ID：{pair_id}\n"
            f"- 仓库：{pair.get('repo_name') or '未记录'}\n"
            f"- 项目类别：{pair.get('project_category') or '未记录'}\n"
            f"- 任务类型：{task_type}\n"
            f"- 难度：{pair.get('difficulty') or '未记录'}\n"
            f"- 语言/框架：{pair.get('language_framework') or '未记录'}\n\n"
            "### User Prompt\n\n"
            "<!-- prompt-start -->\n"
            f"{prompt}\n"
            "<!-- prompt-end -->\n"
            "<!-- gsb-task-end -->\n\n"
        )
        try:
            path.write_text(content.rstrip() + "\n\n" + entry, encoding="utf-8")
        except OSError as exc:
            raise h.WorkflowError("无法写入 GSB 历史题库") from exc
