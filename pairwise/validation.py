"""Deterministic submission checks, including a current independent difficulty verdict."""
import hashlib
import re
from pathlib import Path
from urllib.parse import urlparse

from . import models as m


REVIEW_TEXT_BANNED_WORDS = ("用户",)
GSB_REASON_BANNED_WORDS = ("仅", "则", "仍", "因此", "随之", "若", "无法", "这一", "AI", "断言", "逐项", *REVIEW_TEXT_BANNED_WORDS)
GSB_REASON_BANNED_PHRASES = (
    "更看重", "选择 A 更好", "选择 B 更好", "本题判 A 更好", "本题判 B 更好",
    "结论为 A 更好", "结论为 B 更好", "故取 A", "故取 B",
    "更适合交付", "综合来看", "两边各有优劣",
    "docker compose run --rm verify",
)


def gsb_reason_style_issues(reason):
    """Check only GSB-reason wording rules; delivery descriptions use another rubric."""
    text = str(reason or "")
    issues = []
    for token in GSB_REASON_BANNED_WORDS:
        if token in text:
            issues.append(f"GSB 理由不要出现“{token}”")
    for phrase in GSB_REASON_BANNED_PHRASES:
        if phrase in text:
            issues.append(f"GSB 理由不要使用“{phrase}”这类写法")
    return issues


def delivery_description_style_issues(description, side):
    """Apply shared banned wording to each side's delivery description."""
    text = str(description or "")
    return [f"{str(side).upper()} 交付完整性描述不要出现“{token}”"
            for token in REVIEW_TEXT_BANNED_WORDS if token in text]


from .environment import digest


def public_link(value):
    parsed = urlparse(str(value or ""))
    return bool(parsed.scheme == "https" and parsed.hostname and not parsed.username
                and not parsed.password and parsed.hostname not in {"localhost", "127.0.0.1", "::1"})



def recording_ready(a):
    path = Path((a or {}).get("recording_path") or "")
    return bool(a and a["recording_status"] in {"", "complete"} and path.is_file()
                and a["video_seconds"] and 0 < a["video_seconds"] <= 90)


def user_prompt(event):
    if event.get("type") != "user" or event.get("isSidechain") or event.get("isMeta"):
        return None
    content = (event.get("message") or {}).get("content")
    if isinstance(content, list):
        text = "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
        if not text and any(b.get("type") == "tool_result" for b in content if isinstance(b, dict)):
            return None
        content = text
    if not isinstance(content, str):
        return None
    if content.lstrip().startswith("<task-notification>"):
        return None
    # In particular, DO count a user '继续' and interruption messages.
    return content


def trace_issues(h, path, pair, session, require_complete=True, expected_model=None):
    if not path.is_file():
        return ["原始 JSONL 不存在"]
    events, issues = h.read_trace_events(
        path,
        allow_trailing_partial=not require_complete,
    )
    main = [e for e in events if not e.get("isSidechain")]
    sessions = {e.get("sessionId") for e in main if e.get("sessionId")}
    if not session or sessions != {session} or path.stem != session:
        issues.append("原始 JSONL 的 SessionID 或文件名不匹配")
    prompts = [p for e in main if (p := user_prompt(e)) is not None]
    if prompts != [pair["prompt"]]:
        issues.append("必须恰好一条用户 Prompt，且与共同题面原文完全一致（包括空白）")
    if any(e.get("isApiErrorMessage") or (e.get("type") == "assistant" and any(
            b.get("type") == "text" and str(b.get("text", "")).lstrip().startswith("API Error:")
            for b in (e.get("message") or {}).get("content", []) if isinstance(b, dict))) for e in main):
        issues.append("本 Session 包含 API 工程错误，应创建新的独立 Session")
    version = pair["config"].get("harness_version")
    versions = {h.normalize_harness_version(e.get("version", "")) for e in main if e.get("version")}
    if versions and versions != {version}:
        issues.append("轨迹 Harness 版本与冻结配置不一致")
    if expected_model:
        observed_models = {str((e.get("message") or {}).get("model") or e.get("model"))
                           for e in main if e.get("type") == "assistant"
                           and ((e.get("message") or {}).get("model") or e.get("model"))}
        if observed_models and observed_models != {expected_model}:
            issues.append("原始轨迹模型与本侧选定的 modelname 不一致")
        elif not observed_models and expected_model != m.AUTO_MODEL:
            issues.append("异模型 Session 的原始轨迹未记录模型名称")
    if require_complete:
        final = [i for i, e in enumerate(main) if e.get("type") == "assistant"
                 and not e.get("isApiErrorMessage")
                 and any(b.get("type") == "text" and str(b.get("text", "")).strip()
                         for b in (e.get("message") or {}).get("content", []) if isinstance(b, dict))
                 and (e.get("message") or {}).get("stop_reason") in {"end_turn", "stop_sequence"}]
        if not final or not any(e.get("type") == "last-prompt" or
                (e.get("type") == "system" and e.get("subtype") == "turn_duration")
                for e in main[final[-1]+1:] if final):
            issues.append("缺少首轮最终回复或完成边界")
    return issues


def preflight(h, pair_id):
    pair, items = m.task(h, pair_id), m.attempts(h, pair_id)
    issues, checks = [], {}
    from .difficulty import assessment, submission_choice
    difficulty = assessment(h, pair)
    submission_difficulty, difficulty_issues = submission_choice(pair, difficulty)
    issues.extend(difficulty_issues)
    config = pair["config"]
    if not re.fullmatch(r"[0-9a-f]{40}", pair["base_sha"]):
        issues.append("缺少 40 位初始快照")
    if not public_link(pair["repo_url"]):
        issues.append("缺少本题独立 GitHub 仓库")
    if not config or config.get("context_tokens") != 1_000_000 or config.get("model") != m.AUTO_MODEL:
        issues.append("缺少已冻结的 auto / 1000000 配置")
    sessions = []
    for side in ("A", "B"):
        a = m.latest(items, side)
        problems = []
        if not a or a["status"] != "complete":
            problems.append("尚未完成归档")
        else:
            sessions.append(a["session_id"])
            expected_model = pair.get(f"{side.lower()}_execution_model", m.AUTO_MODEL)
            if a["model"] != expected_model or m.actual_model_name(a) != expected_model:
                problems.append("Session 模型与本侧选定的 modelname 不一致")
            expected = {"fingerprint": digest(config), "base_sha": pair["base_sha"],
                        "prompt_sha256": hashlib.sha256(pair["prompt"].encode()).hexdigest()}
            if any(a["environment"].get(k) != v for k, v in expected.items()):
                problems.append("配置、Prompt 或初始 SHA 不一致")
            path = Path(a["trace_path"])
            problems.extend(trace_issues(h, path, pair, a["session_id"], expected_model=expected_model))
            if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() != a["trace_sha256"]:
                problems.append("原始轨迹 SHA-256 不匹配")
            if not re.fullmatch(r"[0-9a-f]{40}", a["artifact_sha"]):
                problems.append("缺少 40 位产物 SHA")
            else:
                try:
                    actual = h.run_command(["git", "show", "-s", "--format=%P%n%T", a["artifact_sha"]], cwd=Path(a["repo_path"])).stdout.splitlines()
                    if actual != [pair["base_sha"], a["artifact_tree"]]:
                        problems.append("产物唯一父提交或内容树不匹配")
                except Exception:
                    problems.append("产物 Git 对象无法核验")
            recording_path = Path(a["recording_path"] or "")
            if a["recording_status"] not in {"", "complete"}:
                problems.append("本地录屏尚未完成")
            if not recording_path.is_file():
                problems.append("缺少本地录屏文件")
            elif recording_path.suffix.lower() not in {".mp4", ".mov", ".webm", ".m4v"}:
                problems.append("本地录屏格式必须是 mp4、mov、webm 或 m4v")
            elif recording_path.stat().st_size > 500 * 1024 * 1024:
                problems.append("本地录屏超过 SOLO-QA 的 500 MB 上限")
            if not a["video_seconds"] or not 0 < a["video_seconds"] <= 90:
                problems.append("录屏时长须大于 0 且不超过 90 秒")
        checks[side] = {"eligible": not problems, "issues": problems}
        issues.extend(f"{side}：{x}" for x in problems)
    if len(sessions) == 2 and sessions[0] == sessions[1]:
        issues.append("A/B 必须使用不同 SessionID")
    try:
        from .workflow import remote_branches
        expected = {"main": pair["base_sha"], "A": pair["base_sha"], "B": pair["base_sha"]}
        for side in ("A", "B"):
            attempt = m.latest(items, side)
            if attempt and attempt["artifact_sha"]:
                expected[side] = attempt["artifact_sha"]
        if remote_branches(h, pair["repo_url"]) != expected:
            issues.append("本题远端仓库必须且只能有 main、A、B 三个分支，SHA 需与快照一致")
    except Exception:
        issues.append("无法核验本题远端仓库的 main、A、B 三个分支")
    with h.db_connection() as db:
        for attempt in items:
            turns = db.execute("SELECT turn_number,prompt FROM run_turns WHERE run_id=? ORDER BY turn_number",
                               (attempt["id"],)).fetchall()
            if len(turns) != 1 or turns[0]["turn_number"] != 1 or turns[0]["prompt"] != pair["prompt"]:
                issues.append(f"{attempt['side']}-{attempt['number']}：数据库中必须恰好保留一轮共同 Prompt")
    if pair["gsb"] not in m.CHOICES or not pair["reason"].strip():
        issues.append("请填写 GSB 结论和详细对比理由")
    elif len(re.sub(r"\s", "", pair["reason"])) < 60:
        issues.append("GSB 理由至少 60 字，并分别写清 A/B 的优缺点、具体依据和最终权衡")
    else:
        issues.extend(gsb_reason_style_issues(pair["reason"]))
    if pair.get("review_source") and pair["review_source"] != m.current_review_source(items):
        issues.append("GSB 评价对应旧的 A/B 结果，请核对并重新保存")
    for side in ("a", "b"):
        label = side.upper()
        if pair.get(f"{side}_delivery_score") not in range(1, 6):
            issues.append(f"请填写 {label} 交付完整性评分（1 至 5）")
        if not str(pair.get(f"{side}_delivery_description") or "").strip():
            issues.append(f"请填写 {label} 交付完整性描述")
        else:
            issues.extend(delivery_description_style_issues(
                pair[f"{side}_delivery_description"], label))
    return {"id": pair_id, "eligible": not issues, "issues": issues, "checks": checks,
            "difficulty_assessment": difficulty, "submission_difficulty": submission_difficulty,
            "notice": "提交助手会把本地 A/B 轨迹和录屏直接上传到 SOLO-QA；提交前仍需人工核对录屏与理由中的事实。"}


def save_evidence(h, run_id, payload):
    a = m.attempt(h, run_id)
    if a["status"] != "complete":
        raise h.WorkflowError("请先完成本次归档")
    raw_seconds = payload.get("video_seconds")
    try:
        seconds = float(raw_seconds) if raw_seconds not in (None, "") else None
    except (TypeError, ValueError):
        raise h.WorkflowError("录屏时长格式错误")
    if seconds is not None and not 0 < seconds <= 90:
        raise h.WorkflowError("录屏时长须在 0 到 90 秒之间")
    confirmed = payload.get("recording_confirmed") is True
    recording_path = Path(a["recording_path"] or "")
    if confirmed and (not recording_path.is_file() or seconds is None):
        raise h.WorkflowError("确认录屏前请先生成并检查本地录屏及实际时长")
    with h.db_connection() as db:
        db.execute("""UPDATE pairwise_attempts SET video_seconds=?,recording_confirmed=?
            WHERE id=?""", (seconds, int(confirmed), run_id))
        db.execute("UPDATE pairwise_tasks SET human_confirmed=0,revision=revision+1,updated_at=? WHERE id=?",
                   (h.now_text(), a["pair_id"]))
    return m.attempt(h, run_id)
