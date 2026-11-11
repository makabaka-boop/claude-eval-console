"""Build a recording checklist from the frozen task description.

The guide deliberately uses only task metadata.  It must not inspect either
candidate's workspace, trajectory, or result because those are evidence for
the human GSB decision.
"""
import re


FRONTEND_MARKERS = (
    "react", "vue", "svelte", "next.js", "nuxt", "vite", "playwright",
    "页面", "界面", "浏览器", "按钮", "表单", "弹窗", "看板", "前端",
)
BACKEND_MARKERS = (
    "http api", "rest api", "graphql", "grpc", "fastapi", "flask", "django",
    "gin", "spring", "express", "后端", "接口", "数据库", "sqlite",
    "postgresql", "mysql",
)
FOCUS_MARKERS = (
    "核心", "验收", "必须", "精确", "返回", "错误", "非法", "边界", "超时",
    "并发", "一致", "不得", "禁止", "失败", "保证", "限制", "核对", "覆盖",
)
UI_ACTION_MARKERS = (
    "点击", "选择", "输入", "拖拽", "提交", "新增", "创建", "编辑", "删除",
    "上传", "切换", "刷新", "搜索", "筛选", "页面", "界面", "表单", "按钮",
)


def _clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _shorten(value, limit=150):
    value = _clean(value)
    return value if len(value) <= limit else value[: limit - 1].rstrip("，,；;。.") + "…"


def _sentences(prompt):
    text = _clean(prompt)
    if not text:
        return []
    return [part.strip(" -•\t") for part in re.split(r"(?<=[。！？!?])\s*|[\r\n]+", text) if part.strip()]


def _clauses(prompt):
    text = _clean(prompt)
    return [part.strip(" -•\t") for part in re.split(r"[。；;！？!?]+", text) if part.strip()]


def _summary(prompt):
    sentences = _sentences(prompt)
    if not sentences:
        return "按冻结题面完成实现，并用真实运行结果证明核心需求。"
    selected = sentences[:2]
    summary = "".join(selected)
    return _shorten(summary, 220)


def _focus(prompt):
    clauses = _clauses(prompt)
    ranked = []
    for index, clause in enumerate(clauses):
        score = sum(2 for marker in FOCUS_MARKERS if marker.casefold() in clause.casefold())
        if re.search(r"\d", clause):
            score += 1
        if any(marker in clause.casefold() for marker in ("readme", ".gitignore", "从空仓库")):
            score -= 2
        ranked.append((score, -index, clause))
    chosen = []
    for _, _, clause in sorted(ranked, reverse=True):
        item = _shorten(clause, 155)
        if item and item not in chosen:
            chosen.append(item)
        if len(chosen) == 5:
            break
    if len(chosen) < min(3, len(clauses)):
        for clause in clauses:
            item = _shorten(clause, 155)
            if item not in chosen:
                chosen.append(item)
            if len(chosen) == min(5, len(clauses)):
                break
    return chosen or ["完整走通题面的主要流程，并展示可核对的最终结果。"]


def _kind(prompt, language):
    text = f"{prompt} {language}".casefold()
    frontend = any(marker in text for marker in FRONTEND_MARKERS)
    backend = any(marker in text for marker in BACKEND_MARKERS)
    if frontend and backend:
        return "fullstack", "全栈 / 界面 + 接口"
    if frontend:
        return "frontend", "界面操作"
    if backend:
        return "backend", "后端 / API"
    return "general", "通用工程"


def _commands(pair, kind):
    prompt = _clean(pair.get("prompt"))
    language = _clean(pair.get("language_framework"))
    text = f"{prompt} {language}".casefold()
    configured = [_clean(command) for command in pair.get("verification_commands") or [] if _clean(command)]
    commands = ["pwd", "git status --short", "git rev-parse --short HEAD", "sed -n '1,220p' README.md"]
    compose = "docker compose" in text or "docker-compose" in text
    if compose:
        commands.append("docker compose down --remove-orphans")
        setup = [command for command in configured if re.search(r"docker compose (config|build)(?:\s|$)", command)]
        remaining = [command for command in configured if command not in setup]
        commands.extend(setup)
        commands.append(
            f"{'API_PORT=18080 ' if 'api_port' in text else ''}docker compose up"
            " -d"
            f"{' --build' if not any('docker compose build' in command for command in setup) else ''}"
        )
        commands.extend(remaining)
    else:
        commands.extend(configured)
    if "名为 verify" in prompt or "verify 服务" in prompt or "verify的一次性" in prompt:
        commands.append("docker compose run --rm verify")
    if not configured and not any("verify" in command for command in commands):
        if re.search(r"(^|[,，\s])go([,，\s]|$)", language, re.I):
            commands.append("go test ./...")
        elif any(marker in text for marker in ("python", "pytest")):
            commands.append("pytest -q")
        elif any(marker in text for marker in ("node.js", "typescript", "javascript", "vitest")):
            commands.append("npm test")
    # Preserve order while avoiding a configured command being repeated by an inferred one.
    return list(dict.fromkeys(commands))


def _ui_actions(prompt):
    actions = []
    for clause in _clauses(prompt):
        if any(marker in clause for marker in UI_ACTION_MARKERS):
            item = _shorten(clause, 145)
            if item not in actions:
                actions.append(item)
        if len(actions) == 4:
            break
    fallbacks = (
        "打开 README 标明的页面地址，先展示页面初始状态",
        "在题面对应的主要输入区完成一次有效操作并提交，展示结果区域",
        "回到同一操作区触发一个边界或失败场景，展示页面的错误反馈",
    )
    for item in fallbacks:
        if len(actions) >= 3:
            break
        actions.append(item)
    return actions


def build(pair):
    """Return a deterministic guide shared by A and B."""
    prompt = pair.get("prompt") or ""
    # The frozen project category is authoritative, including short bug-fix
    # prompts without API keywords and backend prompts mentioning "no UI".
    category = {
        "纯后端": ("backend", "后端 / API"),
        "纯前端": ("frontend", "界面操作"),
        "全栈": ("fullstack", "全栈 / 界面 + 接口"),
    }.get(pair.get("project_category"))
    kind, label = category or _kind(prompt, pair.get("language_framework") or "")
    steps = [
        {
            "title": "从导出列表启动",
            "description": "A/B 均归档后，在导出列表点击“开始录屏”或“重新录屏”，选择只录 A、只录 B 或两个都录；选择两侧时按 A→B 依次录制。",
        },
        {
            "title": "自动准备干净现场",
            "description": "系统分别从 A、B 的产物快照准备独立目录，从停止状态启动产物，并保留真实构建、启动与报错输出。",
        },
    ]
    if kind in {"backend", "fullstack"}:
        steps.append({
            "title": "验证真实接口",
            "description": "全栈题通过真实页面操作展示接口返回的结果；纯后端运行下列命令与 README 请求示例，保留状态码与响应体。至少检查一个成功输入和题面要求的边界或非法输入。",
        })
    if kind in {"frontend", "fullstack"}:
        steps.append({
            "title": "操作真实界面",
            "description": "纯前端和全栈均新建独立浏览器窗口，只截取网页内容。按题面完成 2～5 个点击、逐步输入或选择动作；鼠标移动和查看结果有随机停顿，A/B 分别演示不同的有效操作路径，并保留真实反馈。",
        })
    if kind == "general":
        steps.append({
            "title": "运行核心流程",
            "description": "按 README 执行主要入口，展示一个正常场景和一个题面要求的边界场景，并保留完整输出。",
        })
    steps.append({
        "title": "完成并保存",
        "description": "页面题保留最终页面结果，后端题保留验收命令的真实输出，然后自动结束并保存。失败现场也会保留。",
    })
    return {
        "kind": kind,
        "label": label,
        "summary": _summary(prompt),
        "focus": _focus(prompt),
        "commands": _commands(pair, kind),
        "ui_actions": _ui_actions(prompt) if kind in {"frontend", "fullstack"} else [],
        "steps": steps,
        "note": "可选择只录 A、只录 B 或两个都录；全局一次只录一侧。页面题只出现真实页面，文字逐步输入、操作节奏有随机停顿；单段不超过 90 秒，失败现场也会保留。",
    }
