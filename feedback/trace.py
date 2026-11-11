"""Deterministic JSONL inspection; never edits or truncates source evidence."""
import hashlib
import json
import re
from pathlib import Path


def text_content(event):
    message = event.get("message") or {}
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text") or "") for b in content
                         if isinstance(b, dict) and b.get("type") in {"text", "input_text"})
    return ""


def inspect(path, prompt, expected_session="", approved_resumes=0, live=False):
    path = Path(path)
    raw = path.read_bytes()
    events, errors = [], []
    lines = raw.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError("not an object")
            events.append(event)
        except (ValueError, UnicodeError):
            if live and index == len(lines) - 1 and not line.endswith(b"\n"):
                continue
            errors.append(f"JSONL 第 {index+1} 行无效")
    main = [e for e in events if not e.get("isSidechain") and not e.get("isMeta")]
    sessions = {str(e.get("sessionId")) for e in main if e.get("sessionId")}
    session = next(iter(sessions)) if len(sessions) == 1 else ""
    if not session or (expected_session and session != expected_session) or path.stem != session:
        errors.append("SessionID 或轨迹文件名不一致")
    users, results = [], []
    for index, event in enumerate(main):
        value = text_content(event)
        if event.get("type") == "user":
            content = (event.get("message") or {}).get("content")
            if isinstance(content, list) and any(isinstance(b,dict) and b.get("type")=="tool_result" for b in content):
                continue
            if not value or value.lstrip().startswith("<task-notification>"):
                continue
            if value.strip().casefold().startswith("[request interrupted by user"):
                continue
            users.append({"index":index,"text":value,"prompt_id":str(event.get("promptId") or ""),
                          "timestamp":str(event.get("timestamp") or "")})
        if event.get("type") == "assistant" and value:
            results.append({"index":index,"text":value,"stop_reason":(event.get("message") or {}).get("stop_reason"),
                            "api_error":bool(event.get("isApiErrorMessage") or value.startswith("API Error:"))})
    if not users or users[0]["text"] != prompt:
        errors.append("原始 Prompt 与题面不完全一致（保留空白和换行）")
    first_id = users[0]["prompt_id"] if users else ""
    if not first_id:
        errors.append("轨迹缺少首轮 promptId，不能用续写 ID 替代")
    continuations = users[1:]
    if len(continuations) > approved_resumes:
        errors.append("轨迹存在未通过继续操作登记的额外输入")
    for item in continuations:
        if item["text"].strip() != "继续":
            errors.append("续写仅允许“继续”，不能追加新需求或纠错提示")
        if not item["prompt_id"]:
            errors.append("续写缺少实际 promptId")
    ids = [item["prompt_id"] for item in users if item["prompt_id"]]
    if len(ids) != len(set(ids)):
        errors.append("轨迹中的 PromptID 重复")
    latest_index = users[-1]["index"] if users else -1
    current = [r for r in results if r["index"] > latest_index]
    last = current[-1] if current else None
    api_error = next((r["text"] for r in reversed(current) if r["api_error"]), "")
    final = last if last and not last["api_error"] and last["stop_reason"] in {"end_turn","stop_sequence"} else None
    boundary = bool(final and any(e.get("type")=="last-prompt" or
                    (e.get("type")=="system" and e.get("subtype")=="turn_duration")
                    for e in main[final["index"]+1:]))
    ended = any(e.get("type")=="system" and e.get("subtype")=="turn_duration"
                for e in main[latest_index+1:])
    interrupted = bool((last and last["stop_reason"] in {"max_tokens","pause_turn"})
                       or (ended and not final and not api_error))
    return {"session_id":session,"first_turn_id":first_id,"continuations":continuations,
            "complete":bool(final and boundary and len(continuations)==approved_resumes and not errors and not api_error),
            "interrupted":interrupted and len(continuations)==approved_resumes,"api_error":api_error,"errors":list(dict.fromkeys(errors)),
            "result":final["text"] if final else "", "sha256":hashlib.sha256(raw).hexdigest(),
            "user_turns":len(users),"assistant_messages":len(results),"event_count":len(events)}


def locate(root, prompt, expected_session="", approved_resumes=0):
    candidates = sorted(Path(root).rglob("*.jsonl"), key=lambda p:p.stat().st_mtime, reverse=True)
    for path in candidates:
        if expected_session and path.stem != expected_session:
            continue
        state = inspect(path,prompt,expected_session,approved_resumes,live=True)
        if state["first_turn_id"] and not any("原始 Prompt" in e for e in state["errors"]):
            return path,state
    return None,None
