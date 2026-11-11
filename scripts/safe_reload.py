#!/usr/bin/env python3
"""Reload the console while detached Claude A/B sessions keep running."""
from __future__ import annotations

import argparse
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
import urllib.request


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASE = "http://127.0.0.1:8765"
LABEL = f"gui/{os.getuid()}/com.makabaka.claude-eval-console"
LOCK_PATH = ROOT / ".data/safe-reload.lock"


def request(base, route, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(base + route, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.load(response)


def source_version():
    match = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"',
                      (ROOT / "app.py").read_text(), re.MULTILINE)
    if not match:
        raise RuntimeError("app.py 中没有 APP_VERSION")
    return match.group(1)


def immutable_rows(db, table, columns):
    rows = db.execute(
        "SELECT " + ",".join(columns) + " FROM " + table + " ORDER BY id"
    ).fetchall()
    return {str(row[0]): tuple(row) for row in rows}


def preserves(before, after):
    return all(after.get(key) == value for key, value in before.items())


def only_finished_archive_failures_block(state):
    blockers = state.get("blockers") or {}
    critical = int(blockers.get("critical_attempts") or 0)
    if not critical or any(value for key, value in blockers.items() if key != "critical_attempts"):
        return False
    with sqlite3.connect(f"file:{ROOT}/.data/console.db?mode=ro", uri=True) as db:
        rows = db.execute(
            "SELECT a.status,a.finished_at,r.phase,r.status_detail "
            "FROM pairwise_attempts a JOIN runs r ON r.id=a.id "
            "WHERE a.status IN ('starting','recovering','archiving')"
        ).fetchall()
    return len(rows) == critical and all(
        status == "archiving" and finished_at and phase == "failed"
        and detail.startswith("归档失败")
        for status, finished_at, phase, detail in rows
    )


def main():
    parser = argparse.ArgumentParser(description="安全交接并重启评测控制台")
    parser.add_argument("--base-url", default=DEFAULT_BASE)
    parser.add_argument("--target-version", default=source_version())
    parser.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args()
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_handle = LOCK_PATH.open("a+")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise RuntimeError("已有一次安全更新正在执行") from exc
    previous_health = request(args.base_url, "/api/health")
    previous_pid = previous_health.get("service_pid")
    drain_enabled = False
    loaded = False
    backup = None
    try:
        state = request(args.base_url, "/api/pairwise/deployment", {"draining": True})
        drain_enabled = True
        print(json.dumps({"phase": "draining", **state}, ensure_ascii=False), flush=True)
        deadline = time.monotonic() + args.timeout
        while not state["restart_safe"] and not only_finished_archive_failures_block(state):
            if time.monotonic() >= deadline:
                raise RuntimeError("等待启动临界区、题面生成或录屏结束超时")
            time.sleep(2)
            state = request(args.base_url, "/api/pairwise/deployment")
            print(json.dumps({"phase": "waiting", **state}, ensure_ascii=False), flush=True)

        backup = ROOT / (".data/backups/before-safe-reload-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
        backup.mkdir(parents=True)
        snapshot = backup / "before-reload.sqlite3"
        with sqlite3.connect(f"file:{ROOT}/.data/console.db?mode=ro", uri=True) as source, \
                sqlite3.connect(snapshot) as destination:
            source.backup(destination)
        with sqlite3.connect(snapshot) as before:
            task_rows = immutable_rows(
                before, "pairwise_tasks", ("id", "prompt", "repo_name", "base_sha")
            )
            attempt_rows = immutable_rows(
                before, "pairwise_attempts", ("id", "pair_id", "side", "number", "prompt_sent")
            )

        # The handover gate remains persisted while the old process exits and
        # the new process reattaches monitors to detached screen containers.
        subprocess.run(["launchctl", "kill", "SIGTERM", LABEL], check=True)
        health_deadline = time.monotonic() + 60
        while time.monotonic() < health_deadline:
            try:
                health = request(args.base_url, "/api/health")
                if (
                    health.get("app_version") == args.target_version
                    and health.get("service_pid")
                    and health.get("service_pid") != previous_pid
                ):
                    loaded = True
                    break
            except Exception:
                pass
            time.sleep(1)
        if not loaded:
            raise RuntimeError("服务没有加载目标版本 " + args.target_version)

        with sqlite3.connect(f"file:{ROOT}/.data/console.db?mode=ro", uri=True) as after:
            if not preserves(task_rows, immutable_rows(
                    after, "pairwise_tasks", ("id", "prompt", "repo_name", "base_sha"))):
                raise RuntimeError("交接前后任务题面或基线发生变化")
            if not preserves(attempt_rows, immutable_rows(
                    after, "pairwise_attempts", ("id", "pair_id", "side", "number", "prompt_sent"))):
                raise RuntimeError("交接前后 A/B Session 标识或发送状态发生变化")

        resumed = request(args.base_url, "/api/pairwise/deployment", {"draining": False})
        drain_enabled = False
        if resumed["draining"]:
            raise RuntimeError("服务已更新，但未退出交接模式")
        print(json.dumps({
            "phase": "complete",
            "app_version": args.target_version,
            "preserved": state["preserved"],
            "backup": str(backup),
        }, ensure_ascii=False), flush=True)
    except BaseException:
        if drain_enabled:
            try:
                request(args.base_url, "/api/pairwise/deployment", {"draining": False})
            except Exception:
                pass
        raise


if __name__ == "__main__":
    main()
