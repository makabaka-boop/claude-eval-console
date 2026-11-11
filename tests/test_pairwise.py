import hashlib
import json
import os
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import pytest

import app
from pairwise import difficulty, generation_policy
from pairwise import deployment, models as m, environment as env, workflow as w, validation as v, export as ex, recording_guide, solo, recording, capture_helper, browser_demo, upstream
from pairwise.api import detail, evidence_times, git_change_summary
from pairwise import api as pairwise_api


def sample_complexity():
    return {
        "baseline_capabilities": "基线只有接收与查询，尚无可恢复投递",
        "required_delta": "本轮实现中断后恢复，并保证回执与投递版本一致",
        "minimal_solution": "在持久化边界记录投递状态和版本，恢复时校验回执所属版本",
        "routine_solution_sufficient": False,
        "design_tradeoffs": ["恢复重放与防重复需要协调提交边界，单独重试无法保证版本一致"],
        "verification_obligations": ["在提交与回执之间注入中断，再恢复并验证旧回执不能覆盖新状态"],
    }


def sample_difficulty(level="困难"):
    return {**sample_complexity(), "level": level,
            "hard_drivers": ["恢复与版本一致性"], "human_work_reasons": ["需验证中断重放和迟到回执"],
            "inflated_label_risk": False, "one_hour_feasible": True,
            "code_evidence": [{"path":"initial/README.md","line":1,"claim":"基线"},
                              {"path":"A/product.txt","line":1,"claim":"本轮增量"}]}


def approve_difficulty(host, pair_id, level="困难"):
    pair=m.task(host,pair_id)
    source=difficulty.source_key(pair,difficulty.latest_sides(host,pair_id))
    with host.db_connection() as db:
        db.execute("UPDATE pairwise_tasks SET difficulty_status='complete',difficulty_source=?,difficulty_review=? WHERE id=?",
                   (source,json.dumps(sample_difficulty(level),ensure_ascii=False),pair_id))


@pytest.fixture
def host(tmp_path, monkeypatch):
    for name, value in {"DATA_DIR":tmp_path, "DB_PATH":tmp_path / "test.db", "PROJECTS_ROOT":tmp_path / "projects",
                        "HISTORY_PATH":tmp_path / "history.md",
                        "PAIRWISE_HISTORY_PATH":tmp_path / "gsb历史题库.md",
                        "HARNESS_VERSION_CACHE":"2.1.197"}.items():
        monkeypatch.setattr(app, name, value)
    app.initialize_database()
    m.initialize(app)
    monkeypatch.setattr(w, "enqueue", lambda *_: None)
    monkeypatch.setattr(difficulty, "enqueue_if_ready", lambda *_, **__: None)
    monkeypatch.setattr(env, "freeze", lambda _: config())
    monkeypatch.setattr(env, "verify_host", lambda *_: None)
    real_remote_branches=w.remote_branches
    def synthetic_remote_branches(h,repo_url,cwd=None):
        if str(repo_url).startswith("https://github.com/test/pair-"):
            with h.db_connection() as db:
                pair=db.execute("SELECT id,base_sha FROM pairwise_tasks WHERE repo_url=?",(repo_url,)).fetchone()
            if pair:
                heads={"main":pair["base_sha"],"A":pair["base_sha"],"B":pair["base_sha"]}
                for item in m.attempts(h,pair["id"]):
                    if item["artifact_sha"]:heads[item["side"]]=item["artifact_sha"]
                return heads
        return real_remote_branches(h,repo_url,cwd)
    monkeypatch.setattr(w,"remote_branches",synthetic_remote_branches)
    return app


def config():
    return {"model":m.AUTO_MODEL, "context_tokens":1_000_000, "image":"sha256:fixture", "host":"test",
            "credential":"secret-hash", "harness":"Claude Code", "harness_version":"2.1.197", "operating_system":"macOS", "runtime":{}}


def payload(**overrides):
    return {"title":"Pair fixture", "repo_name":"pair-fixture", "prompt":"  原始需求\n保留尾随换行\n",
            "task_type":"Feature 迭代", "difficulty":"困难", "language_framework":"Python",
            "snapshot_url":"https://github.com/test/fixture/commit/"+"a"*40,
            "topic_confirmed":True, "project_directory":"test", **overrides}


def test_removed_automatic_origin_cannot_create_an_automatic_pair(host):
    pair = m.create(host, payload(origin="auto"))
    assert pair["origin"] == "manual"
    with pytest.raises(host.WorkflowError, match="自动选源入口已移除"):
        m.create(host, payload(repo_name="legacy-source", legacy_source_run_id="f" * 12))


def git(path, *args):
    return subprocess.check_output(["git", *args], cwd=path, text=True).strip()


def repository(path):
    path.mkdir(parents=True, exist_ok=True)
    git(path,"init","-b","main")
    git(path,"config","user.name","Test")
    git(path,"config","user.email","test@example.invalid")
    (path / "README.md").write_text("base\n")
    git(path,"add",".")
    git(path,"commit","-m","base")
    return git(path,"rev-parse","HEAD")


def seed_three_branches(work,remote,base,a=None,b=None):
    for side,sha in (("main",base),("A",a or base),("B",b or base)):
        git(work,"push",str(remote),f"{sha}:refs/heads/{side}")


def trace(path, prompt, session, extra=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {"type":"user", "sessionId":session,"promptId":"prompt-1","version":"2.1.197","message":{"content":prompt}},
        {"type":"assistant","sessionId":session,"message":{"content":[{"type":"text","text":"合成测试回复"}],"stop_reason":"end_turn"}},
        {"type":"system","sessionId":session,"subtype":"turn_duration"},
    ]
    records.extend(extra or [])
    path.write_text("".join(json.dumps(r,ensure_ascii=False)+"\n" for r in records))


def complete(host, pair_id, side):
    if not m.attempts(host, pair_id):
        w.start(host, pair_id)
    a = m.latest(m.attempts(host, pair_id), side)
    pair = m.task(host, pair_id)
    workspace = Path(a["repo_path"])
    # One shared deterministic base object for independent local fixture repositories.
    if side == "A":
        base = repository(workspace)
        git(workspace,"branch","-m","A")
        with host.db_connection() as db:
            db.execute("UPDATE pairwise_tasks SET repo_url=?,base_sha=? WHERE id=?",
                       (f"https://github.com/test/pair-{pair_id}",base,pair_id))
        pair = m.task(host,pair_id)
    else:
        first = m.latest(m.attempts(host,pair_id),"A")
        workspace.mkdir(parents=True)
        w.clone_initial(host,{"repo_url":first["repo_path"],"base_sha":pair["base_sha"]},workspace,"B")
    (workspace / "product.txt").write_text(side)
    git(workspace,"add",".")
    tree=git(workspace,"write-tree")
    sha=git(workspace,"commit-tree",tree,"-p",pair["base_sha"],"-m",side)
    session=f"session-{side}-{a['id']}"
    raw=Path(a["run_directory"]) / "traces" / "-workspace" / f"{session}.jsonl"
    trace(raw,pair["prompt"],session)
    video=Path(a["run_directory"]).parent / "录屏" / f"{side}.m4v"
    video.parent.mkdir(parents=True,exist_ok=True)
    video.write_bytes((f"synthetic-{side}-video").encode())
    host.update_run(a["id"],session_id=session,first_prompt_id="prompt-1",container_cleaned=1,trajectory_path=str(raw),phase="complete")
    m.update_attempt(host,a["id"],status="complete",prompt_sent=1,artifact_sha=sha,artifact_tree=tree,
        trace_path=str(raw),trace_sha256=hashlib.sha256(raw.read_bytes()).hexdigest(),
        environment={"fingerprint":env.digest(pair["config"]),"base_sha":pair["base_sha"],
                     "prompt_sha256":hashlib.sha256(pair["prompt"].encode()).hexdigest()},
        video_seconds=30,recording_confirmed=1,recording_status="complete",
        recording_path=str(video),recording_directory=str(video.parent),recording_duration=30)
    return m.attempt(host,a["id"])


def ready_pair(host, **overrides):
    p=m.create(host,payload(**overrides))
    a=complete(host,p["id"],"A")
    b=complete(host,p["id"],"B")
    approve_difficulty(host,p["id"])
    p=m.task(host,p["id"])
    m.save_review(host,p["id"],{"gsb":"Same","reason":"A 在核心实现、边界处理和验收输出上与题面一致，过程没有影响结果的问题；B 同样完成了核心实现和约束，验收结果也一致。两侧产物都能稳定运行，轨迹中的实现步骤和最终效果没有足以区分高下的差异，现有材料不足以支持偏向任意一侧。",
        "a_delivery_score":5,"a_delivery_description":"A 已逐项核对题面功能、构建结果和固定验收，产物能够运行，未发现缺失需求。",
        "b_delivery_score":5,"b_delivery_description":"B 已逐项核对题面功能、构建结果和固定验收，产物能够运行，未发现缺失需求。",
        "human_confirmed":True,"revision":p["revision"]})
    return p["id"],a,b


def test_create_preserves_prompt_and_reproducibility(host):
    p=m.create(host,payload())
    assert p["prompt"]==payload()["prompt"]
    assert p["reproducibility"]=="无外部依赖"
    assert p["repo_url"]=="" and p["base_sha"]==""
    assert p["source_base_sha"]=="a"*40
    assert m.attempts(host,p["id"])==[]


def test_pending_number_remap_keeps_internal_sequence_separate(host):
    first=m.create(host,payload(task_type="0-1 代码生成",snapshot_url="",repo_name="remap-first"))
    second=m.create(host,payload(task_type="0-1 代码生成",snapshot_url="",repo_name="remap-second"))
    with host.db_connection() as db:
        db.execute("""INSERT INTO settings(key,value,updated_at) VALUES(?,?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
            (m.PENDING_NUMBER_REMAP_KEY,json.dumps({second["id"]:"0005"}),host.now_text()))
    m.initialize(host)
    remapped=m.task(host,second["id"])
    assert first["display_number"]=="0001"
    assert remapped["display_number"]=="0005"
    assert remapped["project_number"]==5
    assert remapped["sequence_number"]==2
    next_root=m.create(host,payload(task_type="0-1 代码生成",snapshot_url="",repo_name="remap-next"))
    assert next_root["display_number"]=="0006"
    assert next_root["sequence_number"]==3
    with host.db_connection() as db:
        assert db.execute("SELECT value FROM settings WHERE key=?",
                          (m.PENDING_NUMBER_REMAP_KEY,)).fetchone()[0]==""
        audit=json.loads(db.execute("SELECT value FROM settings WHERE key=?",
                                    (m.NUMBER_REMAP_AUDIT_KEY,)).fetchone()[0])
    assert audit=={second["id"]:"0005"}


def test_backend_recording_guide_uses_frozen_prompt_and_commands(host):
    prompt=("暴雨污染会从供水入口流向取水口，须计算最小关管成本。"
            "代码从空仓库起步，Go 实现 HTTP API。仓库由 Docker Compose 启动，"
            "API_PORT 控制宿主端口，并含名为 verify 的一次性验收服务。"
            "非法引用返回 422；验收核对平行边、自环、多源多汇的精确代价。")
    p=m.create(host,payload(task_type="0-1 代码生成",snapshot_url="",prompt=prompt,
        language_framework="Go, Docker Compose",
        verification_commands=["docker compose config --quiet","docker compose build","go test ./..."]))
    guide=detail(host,p["id"])["recording_guide"]
    assert guide["kind"]=="backend"
    assert guide["label"]=="后端 / API"
    assert "最小关管成本" in guide["summary"]
    assert "API_PORT=18080 docker compose up -d" in guide["commands"]
    assert guide["commands"].index("docker compose build") < guide["commands"].index("API_PORT=18080 docker compose up -d")
    assert "go test ./..." in guide["commands"]
    assert "docker compose run --rm verify" in guide["commands"]
    assert any("非法引用" in item for item in guide["focus"])


def test_frontend_recording_guide_lists_user_operations():
    guide=recording_guide.build({
        "prompt":"实现 React 排班界面。用户选择日期并点击生成按钮，页面显示冲突；刷新后保留筛选条件。",
        "language_framework":"TypeScript, React, Vite",
        "verification_commands":["npm run test"],
    })
    assert guide["kind"]=="frontend"
    assert guide["ui_actions"]
    assert any("点击生成按钮" in item for item in guide["ui_actions"])
    assert guide["commands"][-1]=="npm run test"


def test_page_recording_opens_and_tracks_a_new_browser_window(tmp_path,monkeypatch):
    executable=tmp_path/"Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
    monkeypatch.setattr(recording,"_recording_browser_executable",lambda:executable)
    lines="\n".join(recording._browser_window_lines(tmp_path,"http://127.0.0.1:18081"))
    assert "set demoWindow to make new window" in lines
    assert "set URL of active tab of demoWindow to targetURL" in lines
    assert "--new-window" in lines
    assert "browser-window.id" in lines and "browser-window.kind" in lines
    assert "open http://127.0.0.1:18081" not in lines
    assert "Google Chrome for Testing.app" in lines
    assert "Safari" not in lines and "/Applications/Google Chrome.app" not in lines
    assert "/usr/bin/open -n http" not in lines


def test_recording_browser_uses_newest_cached_testing_build(tmp_path,monkeypatch):
    architecture="chrome-mac-arm64" if os.uname().machine=="arm64" else "chrome-mac-x64"
    paths=[]
    for build in (999,1001):
        executable=tmp_path/f"Library/Caches/ms-playwright/chromium-{build}/{architecture}/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
        executable.parent.mkdir(parents=True)
        executable.touch()
        paths.append(executable)
    original_is_file=Path.is_file
    monkeypatch.setattr(Path,"home",classmethod(lambda cls:tmp_path))
    monkeypatch.setattr(Path,"is_file",lambda path:path.is_relative_to(tmp_path) and original_is_file(path))
    assert recording._recording_browser_executable()==paths[-1]


def test_recording_browser_missing_testing_stops_without_fallback(tmp_path,monkeypatch):
    monkeypatch.setattr(Path,"home",classmethod(lambda cls:tmp_path))
    monkeypatch.setattr(Path,"is_file",lambda path:False)
    with pytest.raises(RuntimeError,match="Google Chrome for Testing"):
        recording._recording_browser_executable()


def test_frontend_demo_uses_new_window_instead_of_plain_url_open(host,tmp_path):
    p=m.create(host,payload(
        prompt="实现 React 页面，由 Docker Compose 启动并含名为 verify 的验收服务。",
        language_framework="TypeScript, React, Docker Compose",project_category="全栈",
        verification_commands=["docker compose build","docker compose run --rm verify"],
    ))
    script=recording._script_text(
        p,{"side":"A","artifact_sha":"a"*40},tmp_path,tmp_path,
        {"API_PORT":"18080","WEB_PORT":"18081","PORT":"18082","COMPOSE_PROJECT_NAME":"demo"},
    )
    assert "set demoWindow to make new window" in script
    assert "http://127.0.0.1:18081" in script
    assert "open http://127.0.0.1:18081" not in script


def test_pure_frontend_startup_stays_in_background(tmp_path):
    (tmp_path/"compose.yaml").write_text("services: {}")
    state=tmp_path/"state"
    state.mkdir()
    lines=recording._frontend_start_lines(tmp_path,state,{
        "API_PORT":"18080","WEB_PORT":"18081","PORT":"18082","COMPOSE_PROJECT_NAME":"demo",
    })
    script="\n".join(lines)
    assert "docker compose up -d --build" in script
    assert "preparation-start" in script
    assert "osascript" not in script
    assert "Terminal" not in script
    assert "printf '\\n$" not in script


def test_frontend_startup_builds_page_service_without_verify_image(tmp_path):
    (tmp_path/"compose.yaml").write_text("""services:
  web:
    build: .
    ports:
      - '8080:80'
  verify:
    build: .
    depends_on:
      - web
""")
    state=tmp_path/"state"
    state.mkdir()
    script="\n".join(recording._frontend_start_lines(tmp_path,state,{
        "API_PORT":"18080","WEB_PORT":"18081","PORT":"18082","COMPOSE_PROJECT_NAME":"demo",
    }))
    assert "docker compose up -d --build web" in script
    assert "app.pid" in script

    next_root=m.create(host,payload(task_type="0-1 代码生成",snapshot_url="",repo_name="next-root"))
    assert next_root["project_number"]==2
    assert next_root["sequence_number"]==5
    assert next_root["display_number"]=="0002"
    assert next_root["project_folder"]=="0002-next-root"


def test_recording_compose_remaps_only_page_and_removes_fixed_names():
    config = {
        "name": "original",
        "services": {
            "api": {
                "container_name": "fixed-api",
                "ports": [{"target": 8000, "published": "8000", "protocol": "tcp"}],
            },
            "web": {
                "container_name": "fixed-web",
                "ports": [{"target": 80, "published": "8080", "protocol": "tcp"}],
                "depends_on": {"api": {"condition": "service_started"}},
            },
        },
    }
    isolated = recording._isolated_recording_compose(config, "web", "18081", "recording-a")
    assert isolated["name"] == "recording-a"
    assert "container_name" not in isolated["services"]["api"]
    assert "ports" not in isolated["services"]["api"]
    assert "container_name" not in isolated["services"]["web"]
    assert isolated["services"]["web"]["ports"] == [{
        "mode": "ingress", "target": 80, "published": "18081",
        "protocol": "tcp", "host_ip": "127.0.0.1",
    }]
    assert config["services"]["api"]["container_name"] == "fixed-api"


def test_recording_compose_recognizes_board_page_service(tmp_path):
    (tmp_path / "compose.yml").write_text(
        "services:\n  standings:\n    image: python:3.12\n  board:\n    image: nginx\n"
    )
    assert recording._compose_page_service(tmp_path, "compose.yml") == "board"


def test_recording_compose_uses_single_custom_named_page_service(tmp_path):
    (tmp_path / "compose.yml").write_text(
        "services:\n  pixels:\n    image: nginx\n    ports:\n      - '8080:80'\n"
    )
    assert recording._compose_page_service(tmp_path, "compose.yml") == "pixels"


def test_recording_compose_uses_only_published_custom_service(tmp_path):
    (tmp_path / "compose.yml").write_text(
        "services:\n  grammar:\n    expose:\n      - '8080'\n"
        "  desk:\n    ports:\n      - '8081:80'\n"
    )
    assert recording._compose_page_service(tmp_path, "compose.yml") == "desk"


def test_recording_compose_prefers_desk_over_published_api(tmp_path):
    (tmp_path / "compose.yml").write_text(
        "services:\n  grammar:\n    ports:\n      - '8080:8080'\n"
        "  desk:\n    ports:\n      - '5173:80'\n"
    )
    assert recording._compose_page_service(tmp_path, "compose.yml") == "desk"


def test_recording_detects_crashed_desk_service(tmp_path, monkeypatch):
    (tmp_path / "compose.yml").write_text(
        "services:\n  units:\n    image: golang\n  desk:\n    image: node\n"
    )

    def fake_run(command, **kwargs):
        if "ps" in command:
            return subprocess.CompletedProcess(command, 0, json.dumps([
                {"Service": "units", "State": "running"},
                {"Service": "desk", "State": "restarting", "ExitCode": 1},
            ]), "")
        assert command[-2:] == ["25", "desk"]
        return subprocess.CompletedProcess(command, 0, "EACCES: permission denied", "")

    monkeypatch.setattr(recording.subprocess, "run", fake_run)
    result = recording._compose_page_failure(tmp_path, {"COMPOSE_PROJECT_NAME": "fixture"})
    assert result.startswith("页面服务 desk restarting")
    assert "EACCES" in result


def test_recording_compose_prefers_viewer_over_published_api(tmp_path):
    (tmp_path / "compose.yml").write_text(
        "services:\n  gates:\n    ports:\n      - '5001:5000'\n"
        "  viewer:\n    ports:\n      - '8080:8080'\n"
    )
    assert recording._compose_page_service(tmp_path, "compose.yml") == "viewer"


def test_recording_compose_name_preserves_side_after_truncation():
    name = "aliquot-lineage-ledger-bugfix-snapshot-consistency"
    a = recording._recording_compose_name(name, "A")
    b = recording._recording_compose_name(name, "B")
    assert a != b
    assert len(a) <= 50 and a.endswith("-a")
    assert len(b) <= 50 and b.endswith("-b")


def test_frontend_startup_exports_disposable_compose_file(tmp_path):
    (tmp_path/"compose.yaml").write_text("services:\n  web:\n    image: nginx\n")
    state=tmp_path/"state"
    state.mkdir()
    isolated=state/"recording-compose.json"
    script="\n".join(recording._frontend_start_lines(tmp_path,state,{
        "API_PORT":"18080","WEB_PORT":"18081","PORT":"18082","COMPOSE_PROJECT_NAME":"demo",
        "COMPOSE_FILE":str(isolated),
    }))
    assert f"export COMPOSE_FILE={isolated}" in script


def test_pure_frontend_prefers_native_package_server_over_compose(tmp_path):
    (tmp_path/"compose.yaml").write_text("services:\n  web:\n    build: .\n")
    (tmp_path/"package.json").write_text(json.dumps({
        "scripts":{"build":"vite build","dev":"vite"},
    }))
    state=tmp_path/"state"
    state.mkdir()
    script="\n".join(recording._frontend_start_lines(tmp_path,state,{
        "API_PORT":"18080","WEB_PORT":"18081","PORT":"18082","COMPOSE_PROJECT_NAME":"demo",
    },prefer_native_package=True))
    assert "npm ci" in script and "npm run build" in script and "npm run dev" in script
    assert "docker compose up" not in script


def test_static_frontend_serves_src_document_root(tmp_path):
    import shlex
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "index.html").write_text("<script src='app.js'></script>")
    state = tmp_path / "state"
    state.mkdir()
    script = "\n".join(recording._frontend_start_lines(tmp_path, state, {
        "API_PORT": "18080", "WEB_PORT": "18081", "PORT": "18082", "COMPOSE_PROJECT_NAME": "demo",
    }))
    assert "python3 -m http.server 18081 --bind 127.0.0.1" in script
    assert "--directory " + shlex.quote(str(tmp_path / "src")) in script
    assert "app-started" in script


def test_package_frontend_builds_before_exposing_page(tmp_path):
    (tmp_path/"package.json").write_text(json.dumps({
        "scripts":{"build":"vite build","dev":"vite"},
    }))
    (tmp_path/"package-lock.json").write_text("{}")
    state=tmp_path/"state"
    state.mkdir()
    script="\n".join(recording._frontend_start_lines(tmp_path,state,{
        "API_PORT":"18080","WEB_PORT":"18081","PORT":"18082","COMPOSE_PROJECT_NAME":"demo",
    }))
    assert script.index("npm run build") < script.index("npm run dev")
    assert script.index("npm run build") < script.index("app-started")
    assert "app-start-failed" in script


def test_frontend_plans_are_side_specific_even_if_model_repeats(host,tmp_path,monkeypatch):
    pair=m.create(host,payload(
        prompt="实现 React 排班页面，选择日期后点击生成，并展示冲突提示。",
        language_framework="React, TypeScript",
    ))
    action={"type":"click","target":"生成排班","value":"","file_name":"","purpose":"展示生成结果"}
    repeated={"summary":"生成排班并展示冲突结果","actions":[action,{
        "type":"fill","target":"日期","value":"2026-09-18","file_name":"","purpose":"设置排班日期",
    }]}
    prompts=[]
    def fake(prompt,schema,*_args,**_kwargs):
        prompts.append(prompt)
        action_schema=schema["properties"]["actions"]["items"]
        assert set(action_schema["required"])==set(action_schema["properties"])
        if len(prompts)==3:
            return {"summary":"换用冲突路径演示提示","actions":[
                {"type":"fill","target":"日期","value":"2026-09-19","file_name":"","purpose":"设置冲突日期"},
                {"type":"click","target":"生成排班","value":"","file_name":"","purpose":"查看冲突提示"},
            ]}
        return json.loads(json.dumps(repeated))
    monkeypatch.setattr(host,"run_codex_structured",fake)
    pair_root=tmp_path/"pair"
    a_root=pair_root/"A-attempt"
    b_root=pair_root/"B-attempt"
    a=recording._frontend_plan(host,pair,{"side":"A"},tmp_path,a_root)
    b=recording._frontend_plan(host,pair,{"side":"B"},tmp_path,b_root)
    assert a["actions"]
    assert b["actions"] and b["actions"]!=a["actions"]
    assert "A 侧路线" in prompts[1]
    assert "上一版与 A 完全相同" in prompts[2]
    assert (pair_root/"A-demo-plan.json").is_file()


def test_frontend_rerecord_reuses_successful_plan_without_model(tmp_path):
    root=tmp_path/"A-attempt"
    root.mkdir()
    plan={"summary":"重新展示已验证的页面路径","actions":[
        {"type":"upload","target":"导入 JSON","value":"{}","file_name":"cues.json","purpose":"导入字幕"},
        {"type":"click","target":"生成预览","value":"","file_name":"","purpose":"查看结果"},
    ]}
    (root/"successful-demo-plan.json").write_text(json.dumps({
        "artifact_sha":"a"*40,"plan":plan,
    },ensure_ascii=False))
    assert recording._frontend_plan(None,{"prompt":"题面"},{"side":"A","artifact_sha":"a"*40},tmp_path,root)==plan


def test_frontend_rerecord_reuses_valid_plan_after_picker_failure(tmp_path):
    root = tmp_path / "B-attempt"
    root.mkdir()
    plan = {"summary": "双文件接缝", "actions": [
        {"type": "click", "target": "扫描片段接缝"},
        {"type": "upload", "target": "选择前段 JSON", "value": '{"readings":[1],"queries":[]}'},
        {"type": "upload", "target": "选择后段 JSON", "value": '{"readings":[1],"queries":[]}'},
    ]}
    (root / "demo-plan.json").write_text(json.dumps(plan, ensure_ascii=False))
    pair = {"prompt": "从本地 JSON 分别选择前段和后段。"}
    assert recording._frontend_plan(None, pair, {"side": "B", "artifact_sha": "b" * 40}, tmp_path, root) == plan


def test_file_focused_frontend_plan_requires_real_file_selection():
    pair = {"prompt": "从本地 JSON 分别选择前段和后段，展示接缝。"}
    click_only = {"actions": [
        {"type": "click", "target": "扫描片段接缝"},
        {"type": "click", "target": "选择 JSON 文件"},
    ]}
    assert "必须用 upload" in recording._frontend_plan_problem(pair, click_only)
    path_as_select = {"actions": [
        {"type": "click", "target": "扫描片段接缝"},
        {"type": "select", "target": "选择前段 JSON", "value": "public/sample.json"},
    ]}
    assert "必须用 upload" in recording._frontend_plan_problem(pair, path_as_select)
    one_file = {"actions": [
        {"type": "click", "target": "扫描片段接缝"},
        {"type": "upload", "target": "选择前段 JSON", "value": '{"readings":[1],"queries":[]}'},
    ]}
    assert "至少需要两次 upload" in recording._frontend_plan_problem(pair, one_file)
    both = {"actions": one_file["actions"] + [
        {"type": "upload", "target": "选择后段 JSON", "value": '{"readings":[1],"queries":[]}'},
    ]}
    assert recording._frontend_plan_problem(pair, both) == ""


def test_page_capture_is_cropped_to_viewport_and_keeps_pointer(tmp_path):
    command=capture_helper.capture_command("cursor:40,120,1280,720",48,tmp_path/"page.mov",tmp_path)
    assert command[1:]==[str(tmp_path),str(tmp_path/"page.mov"),"48","cursor:40,120,1280,720"]
    assert "screencapture" not in command[0]


def test_page_recording_closes_only_its_tracked_window(host,tmp_path,monkeypatch):
    (tmp_path/"browser-window.kind").write_text("Google Chrome")
    (tmp_path/"browser-window.id").write_text("481516")
    calls=[]
    monkeypatch.setattr(host,"run_command",lambda command,**kwargs:calls.append(command))
    recording._close_demo_browser(host,tmp_path)
    assert calls and calls[0][:2]==["osascript","-e"]
    assert 'application "Google Chrome"' in calls[0][2]
    assert calls[0][2].startswith('if application "Google Chrome" is running then')
    assert "id is 481516" in calls[0][2]
    assert not (tmp_path/"browser-window.kind").exists()
    assert not (tmp_path/"browser-window.id").exists()


def test_recording_script_uses_ranged_pauses_and_has_no_completion_message(host,tmp_path):
    p=m.create(host,payload())
    script=recording._script_text(
        p,{"side":"A","artifact_sha":"a"*40},tmp_path,tmp_path,
        {"API_PORT":"18080","WEB_PORT":"18081","PORT":"18082","COMPOSE_PROJECT_NAME":"demo"},
    )
    lines=script.splitlines()
    command_index=next(index for index,line in enumerate(lines) if line.startswith("cd "))
    prompt_index=command_index-2
    assert lines[prompt_index].startswith("printf ")
    copy_pause=float(lines[prompt_index-1].split()[1])
    command_pause=float(lines[command_index-1].split()[1])
    result_pause=float(lines[command_index+1].split()[1])
    assert recording.COPY_PAUSE_RANGE[0] <= copy_pause <= recording.COPY_PAUSE_RANGE[1]
    assert recording.COMMAND_PAUSE_RANGE[0] <= command_pause <= recording.COMMAND_PAUSE_RANGE[1]
    assert recording.RESULT_PAUSE_RANGE[0] <= result_pause <= recording.RESULT_PAUSE_RANGE[1]
    assert "自动录屏" not in script
    assert "演示目标" not in script
    assert "产物快照" not in script
    assert "退出码" not in script
    assert "录制完成" not in script
    assert "回到控制台" not in script


def test_backend_recording_script_exports_isolated_compose(host, tmp_path):
    pair = m.create(host, payload())
    compose_path = tmp_path / "recording-compose.json"
    script = recording._script_text(
        pair, {"side": "A", "artifact_sha": "a" * 40}, tmp_path, tmp_path,
        {"API_PORT": "18080", "WEB_PORT": "18081", "PORT": "18082",
         "COMPOSE_PROJECT_NAME": "demo", "COMPOSE_FILE": str(compose_path)},
    )
    assert f"export COMPOSE_FILE={compose_path}" in script


def test_backend_recording_runs_source_tests_when_compose_service_is_product_entrypoint(tmp_path):
    environment = {"API_PORT": "18080", "WEB_PORT": "18081", "PORT": "18082",
                   "COMPOSE_PROJECT_NAME": "demo"}
    (tmp_path / "tests").mkdir()
    python_pair = payload(
        prompt="Python CLI 使用 Docker Compose 启动。",
        verification_commands=["docker compose run --rm assembler pytest -q"],
    )
    python_script = recording._script_text(
        python_pair, {"side": "A"}, tmp_path, tmp_path, environment,
    )
    assert "python3 -m pytest -q" in python_script
    assert "docker compose run --rm assembler pytest -q" not in python_script

    (tmp_path / "go.mod").write_text("module example.test/demo\n")
    go_pair = payload(
        prompt="Go API 使用 Docker Compose 启动。",
        language_framework="Go",
        verification_commands=["docker compose run --rm api go test ./..."],
    )
    go_script = recording._script_text(
        go_pair, {"side": "B"}, tmp_path, tmp_path, environment,
    )
    assert "go test ./..." in go_script
    assert "docker compose run --rm api go test ./..." not in go_script


def test_backend_recording_uses_sha_bound_local_demo_commands(tmp_path):
    pair = payload(verification_commands=["docker compose run --rm assembler pytest -q"])
    override = tmp_path / "demo-commands.json"
    override.write_text(json.dumps({"artifact_sha": "a" * 40,
                                    "commands": ["python3 -m pytest -q"]}))
    environment = {"API_PORT": "18080", "WEB_PORT": "18081", "PORT": "18082",
                   "COMPOSE_PROJECT_NAME": "demo"}
    script = recording._script_text(
        pair, {"side": "A", "artifact_sha": "a" * 40}, tmp_path, tmp_path, environment,
    )
    assert "python3 -m pytest -q" in script
    assert "docker compose run --rm assembler pytest -q" not in script
    stale = recording._script_text(
        pair, {"side": "B", "artifact_sha": "b" * 40}, tmp_path, tmp_path, environment,
    )
    assert "docker compose run --rm assembler pytest -q" in stale


def test_recording_content_duration_excludes_safety_tail(tmp_path):
    start=tmp_path/"recording-start"
    done=tmp_path/"done"
    start.touch()
    done.touch()
    os.utime(start,(1000,1000))
    os.utime(done,(1042,1042))
    assert recording._content_duration(tmp_path)==42.5


def test_recording_compression_trims_to_content_duration(tmp_path,monkeypatch):
    source=tmp_path/"recording.mov"
    source.write_bytes(b"x"*12000)
    output=source.with_suffix(".m4v")
    def fake_run(command,**kwargs):
        assert command[command.index("--duration")+1]=="42.50"
        output.write_bytes(b"y"*5000)
        return mock.Mock(returncode=0)
    monkeypatch.setattr(recording.subprocess,"run",fake_run)
    result=recording._compress_recording(source,42.5)
    assert result==output
    assert output.is_file() and not source.exists()


def test_terminal_capture_hides_pointer_but_ui_capture_keeps_it(tmp_path):
    terminal=capture_helper.capture_command("0,44,1512,845",88,tmp_path/"terminal.mov",tmp_path)
    interface=capture_helper.capture_command("display",88,tmp_path/"interface.mov",tmp_path)
    assert terminal[-1]=="0,44,1512,845"
    assert interface[-1]=="display"
    assert capture_helper.pointer_park_position("0,44,1512,845")== (756.0,466.5)
    assert capture_helper.pointer_park_position("-1512,-900,1512,845")== (-756.0,-477.5)
    assert capture_helper.pointer_park_position("display") is None
    assert capture_helper.pointer_park_position("cursor:0,44,1512,845") is None


def test_page_capture_does_not_move_prepositioned_pointer(monkeypatch):
    monkeypatch.setattr(capture_helper.ctypes,"CDLL",lambda *_:pytest.fail("page cursor must not be parked outside"))
    capture_helper.park_terminal_pointer("cursor:0,44,1512,845")


@pytest.mark.parametrize("category,expected", [("纯后端","backend"),("纯前端","frontend"),("全栈","fullstack")])
@pytest.mark.parametrize("prompt", ["修复分块预算计算与断流处理", "HTTP API 不需要前端页面和按钮"])
def test_recording_category_overrides_keyword_guess(category, expected, prompt):
    guide=recording_guide.build({"project_category":category,"prompt":prompt,"language_framework":"Go, net/http"})
    assert guide["kind"]==expected


def test_terminal_capture_accepts_only_valid_window_ids(tmp_path):
    assert capture_helper.capture_command("window:123",6,tmp_path/"sample.mov",tmp_path)[-1]=="window:123"
    for target in ("window:0", "window:-1", "window:", "window:1,2", "window:123junk", "window:4294967296"):
        with pytest.raises(ValueError, match="invalid capture target"):
            capture_helper.capture_command(target,6,tmp_path/"sample.mov",tmp_path)


@pytest.mark.parametrize("category,prompt", [
    ("纯后端", "修复分块预算计算与断流处理"),
    ("纯后端", "HTTP API 不需要前端页面和按钮"),
    ("", "修复分块预算计算与断流处理"),
])
@pytest.mark.parametrize("target", ["", "window:123"])
def test_terminal_flow_never_falls_back_to_display(host,tmp_path,monkeypatch,category,prompt,target):
    pair_id,a,_=ready_pair(host,title="terminal flow",repo_name="terminal-flow")
    pair=m.task(host,pair_id)
    pair.update(project_category=category,prompt=prompt,language_framework="Go, net/http")
    monkeypatch.setattr(host,"run_command",lambda *args,**kwargs:mock.Mock(returncode=0))
    monkeypatch.setattr(recording,"_cleanup_demo",lambda *args:None)
    monkeypatch.setattr(recording,"_focus_recording_terminal",lambda *args,**kwargs:target)
    monkeypatch.setattr(recording,"_standardize_recording_terminal",lambda *args,**kwargs:target)
    monkeypatch.setattr(recording,"_record_frontend_prepared",lambda *args:pytest.fail("后端不得打开页面"))
    seen=[]
    def capture(root, output, capture_target):
        seen.append(capture_target)
        raise RuntimeError("capture reached")
    monkeypatch.setattr(recording,"_start_capture",capture)
    with pytest.raises((RuntimeError,host.WorkflowError),match="capture reached" if target else "无法取得验收终端"):
        recording._record_prepared(host,a,pair,tmp_path)
    assert seen==([target] if target else [])


@pytest.mark.parametrize("code,output,expected", [(0,"123\n","window:123"),(0,"0",""),(0,"12, 34",""),(0,"","")])
def test_terminal_window_identity_must_be_valid(code,output,expected):
    host=mock.Mock()
    host.run_command.return_value=mock.Mock(returncode=code,stdout=output)
    assert recording._focus_recording_terminal(host,"demo-A")==expected
    script=host.run_command.call_args.args[0][-1]
    assert "set demoTab to do script" in script
    assert "set demoWindow to front window" in script
    assert "do script COMMAND in demoTab" not in script
    assert "repeat with candidate in windows" not in script


@pytest.mark.parametrize("detail", ["Terminal 自动化权限未允许", "无法打开新窗口"])
def test_unavailable_dedicated_terminal_reports_error_without_fallback(detail):
    host=mock.Mock(WorkflowError=app.WorkflowError)
    host.run_command.return_value=mock.Mock(returncode=1,stdout="",stderr=detail)
    with pytest.raises(app.WorkflowError,match=detail):
        recording._focus_recording_terminal(host,"demo-A")
    assert host.run_command.call_count==1


def test_dedicated_terminal_cleanup_targets_only_owned_window(tmp_path):
    host=mock.Mock()
    host.run_command.return_value=mock.Mock(returncode=0,stdout="123\n",stderr="")
    assert recording._focus_recording_terminal(host,"demo-A",root=tmp_path)=="window:123"
    title=(tmp_path/"terminal-window.title").read_text()
    assert title.startswith("Codex-recording-demo-A-")
    recording._close_recording_terminal(host,tmp_path)
    script=host.run_command.call_args.args[0][-1]
    assert "first window whose id is 123" in script
    assert title in script
    assert "if (custom title of selected tab of demoWindow)" in script
    assert not (tmp_path/"terminal-window.id").exists()
    assert not (tmp_path/"terminal-window.title").exists()


def test_capture_start_rejects_empty_target_before_launch(tmp_path,monkeypatch):
    monkeypatch.setattr(recording.subprocess,"Popen",lambda *a,**k:pytest.fail("不得启动整屏录制"))
    with pytest.raises(ValueError,match="invalid capture target"):
        recording._start_capture(tmp_path,tmp_path/"sample.mov","")


def test_terminal_partial_video_does_not_hide_capture_failure(host,tmp_path,monkeypatch):
    pair_id,a,_=ready_pair(host,title="partial terminal",repo_name="partial-terminal")
    pair=m.task(host,pair_id)
    monkeypatch.setattr(host,"run_command",lambda *args,**kwargs:mock.Mock(returncode=0))
    monkeypatch.setattr(recording,"_cleanup_demo",lambda *args:None)
    monkeypatch.setattr(recording,"_focus_recording_terminal",lambda *args,**kwargs:"window:123")
    monkeypatch.setattr(recording,"_standardize_recording_terminal",lambda *args,**kwargs:"window:123")
    failure={"phase":"error","error":"录屏终端窗口尺寸发生变化，请重新录制"}
    monkeypatch.setattr(recording,"_read_capture_state",lambda *args:failure)
    monkeypatch.setattr(recording,"_stop_capture",lambda *args:(failure["error"],failure))
    monkeypatch.setattr(recording,"_compress_recording",lambda *args:pytest.fail("失败片段不得作为完整视频压缩"))
    def capture(root, output, target):
        output.write_bytes(b"x"*8192)
        return mock.Mock(poll=lambda:0),root/"capture-state.json",root/"capture.stop"
    monkeypatch.setattr(recording,"_start_capture",capture)
    with pytest.raises(host.WorkflowError,match="尺寸发生变化"):
        recording._record_prepared(host,a,pair,tmp_path)
    updated=m.attempt(host,a["id"])
    assert updated["recording_status"]!="complete"
    assert Path(updated["recording_path"]).is_file()


def test_rerecord_archives_previous_movies_instead_of_deleting(tmp_path):
    output = tmp_path / "A.mov"
    output.write_bytes(b"previous raw footage")
    output.with_suffix(".m4v").write_bytes(b"previous compressed footage")
    recording._preserve_previous_recording(output)
    assert not output.exists() and not output.with_suffix(".m4v").exists()
    assert next((tmp_path / "历史录屏").glob("*/A.mov")).read_bytes() == b"previous raw footage"
    assert next((tmp_path / "历史录屏").glob("*/A.m4v")).read_bytes() == b"previous compressed footage"
    recording._preserve_previous_recording(output)
    assert len(list((tmp_path / "历史录屏").iterdir())) == 1


def test_native_capture_rejects_invalid_regions(tmp_path):
    for target in ("cursor:1,2,0,4", "1,2,3,-4", "garbage", "display;anything"):
        with pytest.raises(ValueError, match="invalid capture target"):
            capture_helper.capture_command(target, 88, tmp_path / "A.mov", tmp_path)


def test_capture_runs_inside_authorized_python_without_sending_sigint(tmp_path, monkeypatch):
    output = tmp_path / "A.mov"
    state = tmp_path / "capture-state.json"
    def finished(arguments):
        assert json.loads(state.read_text())["phase"] == "starting"
        assert arguments[1] == str(tmp_path)
        capture_helper.write_state(state, phase="complete", size=8192, duration=4)
        return 0
    monkeypatch.setattr(capture_helper.sys, "argv", ["capture_helper.py", str(tmp_path), str(output), "88", "0,0,800,600"])
    monkeypatch.setattr(capture_helper, "park_terminal_pointer", lambda *_: None)
    monkeypatch.setattr(capture_helper, "_run_native", finished)
    assert capture_helper.main() == 0
    assert json.loads(state.read_text())["duration"] == 4


def test_recording_script_keeps_only_concise_product_proof(host,tmp_path):
    p=m.create(host,payload(
        prompt="代码从空仓库起步，Go 实现 HTTP API，由 Docker Compose 启动并含名为 verify 的一次性验收服务。",
        language_framework="Go, Docker Compose",
        verification_commands=["docker compose config --quiet","docker compose build","docker compose run --rm verify"],
    ))
    script=recording._script_text(
        p,{"side":"A","artifact_sha":"a"*40},tmp_path,tmp_path,
        {"API_PORT":"18080","WEB_PORT":"18081","PORT":"18082","COMPOSE_PROJECT_NAME":"demo"},
    )
    assert "git rev-parse" not in script
    assert "docker compose down" not in script
    assert "docker compose build" not in script
    assert "docker compose up -d --build" in script
    assert "1,60p" in script and "README.md" in script
    assert "docker compose run --rm verify" in script


def test_recording_video_directory_lives_inside_pair_project(tmp_path):
    run_directory=tmp_path/"pair-abc-example"/"A-retry-02"
    assert recording._video_directory({"run_directory":str(run_directory)})==run_directory.parent/"录屏"


def test_pair_soft_delete_hides_only_list_record(host):
    p=m.create(host,payload())
    deleted=m.soft_delete(host,p["id"])
    assert deleted["deleted_at"]
    assert m.task(host,p["id"])["prompt"]==payload()["prompt"]
    with host.db_connection() as db:
        assert db.execute("SELECT COUNT(*) FROM pairwise_tasks WHERE id=?",(p["id"],)).fetchone()[0]==1


def test_pair_finished_at_waits_for_both_latest_attempts(host):
    p=m.create(host,payload())
    w.start(host,p["id"])
    a=m.latest(m.attempts(host,p["id"]),"A")
    b=m.latest(m.attempts(host,p["id"]),"B")
    m.update_attempt(host,a["id"],status="complete",finished_at="2026-09-17 10:00:00 +0800")
    assert detail(host,p["id"])["finished_at"]==""
    m.update_attempt(host,b["id"],status="failed",finished_at="2026-09-17 11:00:00 +0800")
    assert detail(host,p["id"])["finished_at"]=="2026-09-17 11:00:00 +0800"


def test_detail_shows_each_file_generation_time_not_task_finish(host):
    pair_id,a,b=ready_pair(host)
    trace_epoch=1789603200
    os.utime(a["trace_path"],(trace_epoch,trace_epoch))
    recording_time="2026-09-17 12:27:19 +0800"
    m.update_attempt(host,a["id"],recording_finished_at=recording_time,
                     finished_at="2026-09-17 10:00:00 +0800")
    result=m.latest(detail(host,pair_id)["attempts"],"A")
    assert result["trace_generated_at"]==datetime.fromtimestamp(trace_epoch).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    assert result["trace_generated_at_source"]=="file_mtime"
    assert result["recording_generated_at"]==recording_time
    assert result["recording_generated_at_source"]=="recording_finished_at"
    m.update_attempt(host,a["id"],recording_finished_at="2026-09-17 13:00:01 +0800")
    attempts=detail(host,pair_id)["attempts"]
    assert m.latest(attempts,"A")["recording_generated_at"]=="2026-09-17 13:00:01 +0800"
    assert m.latest(attempts,"B")["recording_generated_at_source"]=="file_mtime"


def test_detail_loads_git_changes_only_when_explicitly_requested(host, monkeypatch):
    pair_id,_,_=ready_pair(host)
    summary=mock.Mock(wraps=git_change_summary)
    monkeypatch.setattr(pairwise_api,"git_change_summary",summary)
    handler=mock.Mock()
    pairwise_api.get(handler,host,f"/api/pairwise/{pair_id}",{})
    pair=handler.send_json.call_args.args[0]
    assert "runtime" in pair
    assert all("git_changes" not in a for a in pair["attempts"])
    summary.assert_not_called()
    # The periodic detail read must stay cheap too.
    detail(host,pair_id)
    summary.assert_not_called()
    pairwise_api.get(handler,host,f"/api/pairwise/{pair_id}",{"git_changes":["1"]})
    attempts=handler.send_json.call_args.args[0]["attempts"]
    assert summary.call_count==2
    for side in ("A","B"):
        changes=m.latest(attempts,side)["git_changes"]
        assert changes=={
            "available":True,
            "file_count":1,
            "additions":1,
            "deletions":0,
            "binary_files":0,
            "files":[],
        }


def test_git_change_summary_handles_modified_deleted_and_binary_files(host,tmp_path):
    work=tmp_path/"git-summary"
    base=repository(work)
    (work/"README.md").write_text("updated\nsecond line\n")
    (work/"gone.txt").write_text("remove me\n")
    git(work,"add",".")
    git(work,"commit","-m","prepare deletion")
    prepared=git(work,"rev-parse","HEAD")
    (work/"gone.txt").unlink()
    (work/"asset.bin").write_bytes(b"\x00\x01\x02")
    git(work,"add","-A")
    git(work,"commit","-m","final")
    artifact=git(work,"rev-parse","HEAD")
    changes=git_change_summary(host,prepared,{"artifact_sha":artifact,"repo_path":str(work)})
    assert changes["available"] is True
    assert changes["file_count"]==2
    assert changes["additions"]==0 and changes["deletions"]==1
    assert changes["binary_files"]==1
    assert {item["path"] for item in changes["files"]}=={"asset.bin","gone.txt"}
    totals=git_change_summary(host,prepared,{"artifact_sha":artifact,"repo_path":str(work)},include_files=False)
    assert totals=={**changes,"files":[]}
    assert git_change_summary(host,base,{"artifact_sha":"bad","repo_path":str(work)})["available"] is False


def test_legacy_recording_generation_time_uses_file_write_time(tmp_path):
    video=tmp_path/"A.m4v"
    video.write_bytes(b"synthetic-video")
    stamp=1789603200
    os.utime(video,(stamp,stamp))
    times=evidence_times({"recording_path":str(video),"recording_status":"complete"})
    assert times["recording_generated_at"]==datetime.fromtimestamp(stamp).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    assert times["recording_generated_at_source"]=="file_mtime"


@pytest.mark.parametrize("path_kind",["empty","missing","directory"])
def test_missing_evidence_does_not_use_attempt_finish_as_generation(tmp_path,path_kind):
    path={"empty":"","missing":str(tmp_path/"missing.jsonl"),"directory":str(tmp_path)}[path_kind]
    times=evidence_times({"trace_path":path,"recording_path":path,
                          "finished_at":"2026-09-17 12:00:00 +0800"})
    assert times=={"trace_generated_at":"","trace_generated_at_source":"",
                   "recording_generated_at":"","recording_generated_at_source":""}


@pytest.mark.parametrize("overrides",[{"difficulty":"简单"},{"difficulty":"中等"},{"topic_confirmed":False},
    {"snapshot_url":"https://github.com/a/b/commit/1234"},{"snapshot_url":""}, {"task_type":"invalid"}])
def test_reject_invalid_question(host,overrides):
    with pytest.raises(app.WorkflowError):
        m.create(host,payload(**overrides))


def test_feature_iteration_accepts_medium_but_other_types_do_not(host):
    assert m.create(host, payload(difficulty="中等"))["difficulty"] == "中等"
    with pytest.raises(app.WorkflowError, match="其他任务仅允许困难或地狱"):
        m.create(host, payload(task_type="Bug 修复", difficulty="中等"))


def test_new_empty_repo_allowed_only_for_zero_one(host):
    p=m.create(host,payload(task_type="0-1 代码生成",snapshot_url=""))
    assert not p["base_sha"]
    assert not p["source_base_sha"]


def test_each_pair_requires_unique_new_repository_name(host):
    m.create(host,payload())
    with pytest.raises(app.WorkflowError,match="独立仓库"):
        m.create(host,payload(title="another"))


def test_pair_creates_new_repository_with_only_main_a_b_and_imported_tree(host,tmp_path,monkeypatch):
    source=tmp_path/"source"
    source_sha=repository(source)
    (source/"source-code.py").write_text("print('source')\n")
    git(source,"add", ".");git(source,"commit","-m","source snapshot")
    source_sha=git(source,"rev-parse","HEAD")
    p=m.create(host,payload(task_type="0-1 代码生成",snapshot_url="",repo_name="new-pair-repo"))
    with host.db_connection() as db:
        db.execute("UPDATE pairwise_tasks SET source_repo_url=?,source_base_sha=? WHERE id=?",
                   (str(source),source_sha,p["id"]))
    w.start(host,p["id"])
    a=m.latest(m.attempts(host,p["id"]),"A")
    target=tmp_path/"target.git"
    repo_url="https://github.com/fixture-owner/new-pair-repo"
    monkeypatch.setattr(host,"GITHUB_OWNER","fixture-owner")
    real_run=host.run_command
    def local_github(command,**kwargs):
        command=list(command)
        if command[:3]==["gh","repo","view"]:
            return subprocess.CompletedProcess(command,1,"","")
        if command[:3]==["gh","repo","create"]:
            subprocess.run(["git","init","--bare",str(target)],check=True,capture_output=True)
            real_run(["git","push",str(target),"main:refs/heads/main"],cwd=kwargs["cwd"])
            return subprocess.CompletedProcess(command,0,"","")
        command=[str(target) if value==repo_url else value for value in command]
        return real_run(command,**kwargs)
    monkeypatch.setattr(host,"run_command",local_github)
    result=w.ensure_pair_repository(host,a["id"],m.task(host,p["id"]))
    assert result["repo_url"]==repo_url
    assert result["base_sha"]!=source_sha
    assert w.remote_branches(host,repo_url)=={
        "main":result["base_sha"],"A":result["base_sha"],"B":result["base_sha"]}
    initial=Path(a["run_directory"]).parent/"main"
    assert git(initial,"show",f"{result['base_sha']}:source-code.py")=="print('source')"
    assert git(initial,"show","-s","--format=%P",result["base_sha"])==""


def test_zero_one_main_contains_only_empty_readme(host,tmp_path,monkeypatch):
    p=m.create(host,payload(task_type="0-1 代码生成",snapshot_url="",repo_name="zero-empty-readme"))
    w.start(host,p["id"])
    a=m.latest(m.attempts(host,p["id"]),"A")
    target=tmp_path/"zero.git"
    repo_url="https://github.com/fixture-owner/zero-empty-readme"
    monkeypatch.setattr(host,"GITHUB_OWNER","fixture-owner")
    real_run=host.run_command
    def local_github(command,**kwargs):
        command=list(command)
        if command[:3]==["gh","repo","view"]:
            return subprocess.CompletedProcess(command,1,"","")
        if command[:3]==["gh","repo","create"]:
            subprocess.run(["git","init","--bare",str(target)],check=True,capture_output=True)
            real_run(["git","push",str(target),"main:refs/heads/main"],cwd=kwargs["cwd"])
            return subprocess.CompletedProcess(command,0,"","")
        command=[str(target) if value==repo_url else value for value in command]
        return real_run(command,**kwargs)
    monkeypatch.setattr(host,"run_command",local_github)
    result=w.ensure_pair_repository(host,a["id"],m.task(host,p["id"]))
    initial=Path(a["run_directory"]).parent/"main"
    assert git(initial,"ls-tree","--name-only",result["base_sha"])=="README.md"
    assert git(initial,"show",f"{result['base_sha']}:README.md")==""
    assert git(initial,"show","-s","--format=%P",result["base_sha"])==""
    assert w.remote_branches(host,repo_url)=={
        "main":result["base_sha"],"A":result["base_sha"],"B":result["base_sha"]}


def test_initial_start_creates_a_and_b_together(host,monkeypatch):
    p=m.create(host,payload())
    with pytest.raises(app.WorkflowError,match="同时运行"):
        w.start(host,p["id"],"B")
    scheduled=[]
    monkeypatch.setattr(w,"enqueue",lambda _,run_id:scheduled.append(run_id))
    created=w.start(host,p["id"])
    assert [a["side"] for a in created]==["A","B"]
    assert scheduled==[a["id"] for a in created]
    assert Path(created[0]["run_directory"]).name=="A"
    assert Path(created[1]["run_directory"]).name=="B"
    assert created[0]["created_at"]==created[1]["created_at"]
    assert host.run_row(created[0]["id"])["first_prompt"]==host.run_row(created[1]["id"])["first_prompt"]==p["prompt"]


def test_unsent_failed_pair_can_restart_both_sides_together(host):
    p=m.create(host,payload())
    w.start(host,p["id"])
    originals=m.attempts(host,p["id"])
    with host.db_connection() as db:
        db.execute("UPDATE pairwise_tasks SET repo_url=?,base_sha=? WHERE id=?",
                   ("https://github.com/test/pair-recovery","a"*40,p["id"]))
    for a in originals:
        m.update_attempt(host,a["id"],status="failed",error="pre-prompt setup failed")
        host.update_run(a["id"],container_cleaned=1)
    created=w.start(host,p["id"])
    assert [(a["side"],a["number"]) for a in created]==[("A",2),("B",2)]
    assert created[0]["created_at"]==created[1]["created_at"]
    assert all(a["status"]=="queued" for a in created)
    assert all("-retry-02" in a["run_directory"] for a in created)
    assert m.task(host,p["id"])["base_sha"]=="a"*40
    assert len(m.attempts(host,p["id"]))==4


def test_unsent_stopped_pair_without_main_can_requeue_both_sides(host):
    p=m.create(host,payload())
    originals=w.start(host,p["id"])
    assert m.task(host,p["id"])["base_sha"]==""
    for a in originals:
        m.update_attempt(host,a["id"],status="stopped")
        host.update_run(a["id"],container_cleaned=1)

    created=w.start(host,p["id"])

    assert [(a["side"],a["number"],a["status"]) for a in created]==[
        ("A",2,"queued"),("B",2,"queued")]
    assert all(a["prompt_sent"]==0 for a in originals)
    assert m.task(host,p["id"])["base_sha"]==""
    assert len(m.attempts(host,p["id"]))==4


def test_pair_restart_requires_both_sides_unsent_and_cleaned(host):
    p=m.create(host,payload())
    w.start(host,p["id"])
    originals=m.attempts(host,p["id"])
    with host.db_connection() as db:
        db.execute("UPDATE pairwise_tasks SET repo_url=?,base_sha=? WHERE id=?",
                   ("https://github.com/test/pair-recovery","a"*40,p["id"]))
    for a in originals:
        m.update_attempt(host,a["id"],status="failed")
    with pytest.raises(host.WorkflowError,match="清理"):
        w.start(host,p["id"])
    for a in originals:
        host.update_run(a["id"],container_cleaned=1)
    m.update_attempt(host,originals[0]["id"],prompt_sent=1)
    with pytest.raises(host.WorkflowError,match="发送过 Prompt"):
        w.start(host,p["id"])


def test_recording_confirmation_cannot_bypass_missing_evidence(host):
    p=m.create(host,payload())
    a=complete(host,p["id"],"A")
    Path(a["recording_path"]).unlink()
    with pytest.raises(app.WorkflowError,match="本地录屏"):
        v.save_evidence(host,a["id"],{"recording_confirmed":True,"video_seconds":30})
    assert m.latest(m.attempts(host,p["id"]),"B") is not None


def test_legacy_lists_and_recovery_exclude_pair_attempts(host,monkeypatch):
    p=m.create(host,payload())
    a=complete(host,p["id"],"A")
    host.update_turn(a["id"],1,status="complete")
    assert host.all_runs()==[]
    assert host.completed_turn_rows()==[]
    host.update_run(a["id"],phase="queued")
    called=[]
    monkeypatch.setattr(host,"schedule_worker",lambda *args:called.append(args))
    host.recover_monitors()
    assert not called


def test_draft_generation_is_question_only_and_durable(host,monkeypatch):
    from pairwise import drafts
    class ImmediateThread:
        def __init__(self, target, **_): self.target=target
        def start(self): self.target()
    monkeypatch.setattr(drafts.threading,"Thread",ImmediateThread)
    captured={}
    def generate(**kw):
        captured.update(kw)
        kw["progress"]("合成题面生成")
        return {"first_prompt":"合成题面", "task_difficulty":"困难","difficulty_review":sample_difficulty(),"complexity_analysis":sample_complexity()}
    monkeypatch.setattr(host,"generate_task_draft",generate)
    result=drafts.create(host)
    assert result["status"]=="complete"
    assert drafts.get(host,result["id"])["result"]["first_prompt"]=="合成题面"
    assert set(captured)=={"project_number","initial_feedback","progress","history_override"}
    assert captured["project_number"]==1 and captured["history_override"]==[]
    assert "禁止 CLI" in captured["initial_feedback"]


def test_draft_reuses_pending_job_and_restart_marks_interrupted(host,monkeypatch):
    from pairwise import drafts
    class PendingThread:
        def __init__(self, **_): pass
        def start(self): pass
    monkeypatch.setattr(drafts.threading,"Thread",PendingThread)
    first=drafts.create(host)
    assert first["status"]=="queued"
    assert drafts.create(host)["id"]==first["id"]
    w.recover(host)
    assert drafts.get(host,first["id"])["status"]=="failed"


def test_pairwise_auto_refill_defaults_to_requested_absolute_directory(host):
    from pairwise import auto_refill
    config=auto_refill.configuration(host)
    assert config["project_directory"]==str(host.PROJECTS_ROOT/"zzzz-gsb")
    assert (host.PROJECTS_ROOT/"zzzz-gsb").is_dir()
    assert config["created_count"]==0
    assert config["start_after_hours"]==0
    assert "target_count" not in config
    assert config["task_type_counts"]=={
        "0-1 代码生成":0,"Feature 迭代":0,"Bug 修复":0
    }
    assert config["daily_qualified_counts"]==dict.fromkeys(auto_refill.BALANCED_TASK_TYPES,0)
    assert config["daily_downgraded_counts"]==dict.fromkeys(auto_refill.BALANCED_TASK_TYPES,0)
    assert config["daily_count_date"]==host.now_text()[:10]
    assert config["next_task_type"]=="0-1 代码生成"
    assert config["task_mix_policy"]=="daily_balance"
    assert config["task_mix_max_difference"] is None
    assert config["bug_lineage_mode"] == "evolved"
    assert config["bug_lineage_reuse"] is True
    assert config["history_file"]==str(host.PAIRWISE_HISTORY_PATH)


def test_pairwise_runtime_separates_claude_and_codex_slots(host,monkeypatch):
    from pairwise import api
    monkeypatch.setattr(host,"WORKER_SLOT_ACTIVE",1)
    monkeypatch.setattr(host,"CODEX_SLOT_ACTIVE",1)
    runtime=api.runtime(host)
    assert runtime["active_parallel"]==0
    assert runtime["codex_active"]==1
    assert runtime["codex_max_parallel"]==2


def test_deployment_handover_preserves_running_and_queued_attempts(host, monkeypatch):
    monkeypatch.setattr(host, "CODEX_SLOT_ACTIVE", 0)
    pair = m.create(host, payload(repo_name="handover-fixture"))
    w.start(host, pair["id"])
    attempts = m.attempts(host, pair["id"])
    m.update_attempt(host, attempts[0]["id"], status="running", prompt_sent=1)
    state = deployment.configure(host, True)
    assert state["restart_safe"]
    assert state["preserved"] == {"running": 1, "queued": 1, "recording_queued": 0}
    m.update_attempt(host, attempts[1]["id"], status="starting")
    state = deployment.status(host)
    assert not state["restart_safe"]
    assert state["blockers"]["critical_attempts"] == 1
    m.update_attempt(host, attempts[1]["id"], status="queued")
    assert deployment.status(host)["restart_safe"]
    assert not deployment.configure(host, False)["draining"]


def test_deployment_counts_each_active_prefill_lane(host, monkeypatch):
    from pairwise import auto_refill
    monkeypatch.setattr(host, "CODEX_SLOT_ACTIVE", 0)
    monkeypatch.setattr(auto_refill, "configuration", lambda _h: {
        "generating": False,
        "prefill_generating": True,
        "prefill_lanes": [{"lane": 1}, {"lane": 2}],
    })
    state = deployment.configure(host, True)
    assert state["blockers"]["generation_active"] == 0
    assert state["blockers"]["prefill_active"] == 2
    assert not state["restart_safe"]
    deployment.configure(host, False)


def test_recording_queue_is_not_claimed_during_deployment_handover(host, monkeypatch):
    monkeypatch.setattr(host, "CODEX_SLOT_ACTIVE", 0)
    pair = m.create(host, payload(repo_name="recording-handover-fixture"))
    w.start(host, pair["id"])
    attempt = m.attempts(host, pair["id"])[0]
    m.update_attempt(host, attempt["id"], recording_status="queued")
    deployment.configure(host, True)
    assert recording._claim_next(host) is None
    assert m.attempt(host, attempt["id"])["recording_status"] == "queued"
    deployment.configure(host, False)
    assert recording._claim_next(host)["id"] == attempt["id"]


def test_deployment_api_toggles_persistent_handover(host, monkeypatch):
    from pairwise import api
    monkeypatch.setattr(host, "CODEX_SLOT_ACTIVE", 0)
    handler = mock.Mock()
    api.post(handler, host, "/api/pairwise/deployment", {"draining": True})
    assert handler.send_json.call_args[0][0]["draining"]
    handler.reset_mock()
    api.get(handler, host, "/api/pairwise/deployment", {})
    assert handler.send_json.call_args[0][0]["restart_safe"]
    deployment.configure(host, False)


@pytest.mark.parametrize("hours",[-1,169,True,"2",1.5])
def test_pairwise_auto_refill_schedule_requires_whole_hours(host,hours):
    from pairwise import auto_refill
    with pytest.raises(host.WorkflowError,match="0 至 168 小时"):
        auto_refill.configure(host,{"enabled":True,"start_after_hours":hours,
            "project_directory":str(host.PROJECTS_ROOT/"zzzz-gsb")})


def test_pairwise_auto_refill_can_be_scheduled(host):
    from pairwise import auto_refill
    config=auto_refill.configure(host,{"enabled":True,"start_after_hours":2,
        "project_directory":str(host.PROJECTS_ROOT/"scheduled")})
    assert config["enabled"] and config["scheduled"]
    assert config["start_after_hours"]==2
    assert config["scheduled_for"]
    assert config["project_directory"]==str(host.PROJECTS_ROOT/"scheduled")
    assert auto_refill.run_once(host) is False


def test_pairwise_auto_refill_continues_without_count_limit_and_waits_for_slots(host,monkeypatch):
    from pairwise import auto_refill
    generated={
        "repo_name":"continuous-project",
        "first_prompt":"从空 README 实现一个具备跨请求不变量的困难项目。",
        "task_difficulty":"困难","difficulty_review":sample_difficulty(),"complexity_analysis":sample_complexity(),
        "category":"纯后端",
        "language_framework":"Python, FastAPI, SQLite",
        "verification_commands":["pytest -q"],
    }
    calls=[]
    def generate(**kwargs):
        calls.append(kwargs)
        return {**generated,"repo_name":f"continuous-project-{len(calls)}"}
    monkeypatch.setattr(host,"generate_task_draft",generate)
    host.set_max_parallel_runs({"max_parallel":2})
    config=auto_refill.configure(host,{"enabled":True,"start_after_hours":0,
        "project_directory":str(host.PROJECTS_ROOT/"zzzz-gsb")})
    assert config["enabled"] and not config["scheduled"]
    assert auto_refill._prefill_once(host)
    assert auto_refill.run_once(host)
    assert auto_refill.configuration(host)["enabled"]
    assert auto_refill.run_once(host) is False
    assert len(calls)==1
    assert "等待 Claude 执行队列消化" in auto_refill.configuration(host)["detail"]
    with host.db_connection() as db:
        first=db.execute("SELECT id FROM pairwise_attempts ORDER BY id LIMIT 1").fetchone()
        db.execute("UPDATE pairwise_attempts SET status='complete' WHERE id=?",(first["id"],))
    # One free slot is enough to bootstrap a new root, even before an archived
    # source is available; the other side queues under the existing slot limit.
    assert auto_refill.run_once(host)
    assert len(calls)==2
    assert auto_refill.configuration(host)["enabled"]
    assert "等待 Claude 执行队列消化" not in auto_refill.configuration(host)["detail"]

def test_zero_one_bank_batch_is_preview_only_until_added(host,monkeypatch):
    from pairwise import zero_one_lab
    class PendingThread:
        def __init__(self, **_): pass
        def start(self): pass
    monkeypatch.setattr(zero_one_lab.threading,"Thread",PendingThread)
    batch=zero_one_lab.create(host,{"count":6})
    assert [item["category"] for item in batch["candidates"]]==[
        "纯后端","纯后端","纯后端","纯前端","纯前端","全栈"]
    assert zero_one_lab.create(host,{"count":6})["id"]==batch["id"]
    with host.db_connection() as db:
        assert db.execute("SELECT COUNT(*) FROM pairwise_tasks").fetchone()[0]==0
    calls=[]
    def generate(**kwargs):
        calls.append(kwargs)
        index=len(calls)
        prompts=["电站绝缘层破损位置的脉冲反射核查。","星图中变星周期的离散峰值搜索。",
                 "古籍不同刻本的异体字对齐追溯。","剧场灯光曲线的视觉阶梯编辑。",
                 "河口盐度分层图的交互剖面复核。","胶片冲洗温度记录与曝光剂量联查。"]
        return {"project_name":f"独立题 {index}","repo_name":f"distinct-bank-{index}",
                "category":kwargs["category_override"],"task_difficulty":"困难",
                "estimated_minutes":40,"estimated_handwritten_lines":1800,
                "first_prompt":prompts[index-1],
                "language_framework":"Go, Docker, Docker Compose",
                "verification_commands":["docker compose config --quiet","docker compose build"],
                "difficulty_review":{},"complexity_analysis":{}}
    monkeypatch.setattr(host,"generate_task_draft",generate)
    zero_one_lab._generate(host,batch["id"])
    result=zero_one_lab.get(host,batch["id"])
    assert result["status"]=="complete",[item["error"] for item in result["candidates"]]
    assert all(item["status"]=="ready" for item in result["candidates"])
    assert [call["category_override"] for call in calls]==[
        "纯后端","纯后端","纯后端","纯前端","纯前端","全栈"]
    added=zero_one_lab.add_candidate(host,batch["id"],1)
    item=added["candidates"][0]
    assert item["status"]=="added" and item["actual_number"]=="0001"
    queued=[]
    monkeypatch.setattr(zero_one_lab.workflow,"start",lambda _h,pair_id,side:queued.append((pair_id,side)))
    after=zero_one_lab.add_candidate(host,batch["id"],1,queue=True)
    assert after["candidates"][0]["status"]=="queued"
    assert queued==[(item["pair_id"],None)]
    with host.db_connection() as db:
        assert db.execute("SELECT COUNT(*) FROM pairwise_tasks").fetchone()[0]==1
    direct=zero_one_lab.add_candidate(host,batch["id"],2,queue=True)
    assert direct["candidates"][1]["status"]=="queued"
    assert queued[-1]==(direct["candidates"][1]["pair_id"],None)
    with host.db_connection() as db:
        assert db.execute("SELECT COUNT(*) FROM pairwise_tasks").fetchone()[0]==2


def test_iteration_bank_uses_latest_selected_leaf_and_only_generates_preview(host,monkeypatch):
    from pairwise import iteration_lab
    root=m.create(host,payload(repo_name="iteration-source",task_type="0-1 代码生成",snapshot_url=""))
    a=complete(host,root["id"],"A")
    b=complete(host,root["id"],"B")
    with host.db_connection() as db:
        db.execute("UPDATE pairwise_tasks SET human_confirmed=1,gsb='B 更好',reference_status='complete',reference_gsb='A 更好' WHERE id=?",(root["id"],))
    selected=iteration_lab._available_sources(host)
    assert len(selected)==1
    assert selected[0]["pair_id"]==root["id"]
    assert selected[0]["side"]=="B" and selected[0]["sha"]==b["artifact_sha"]
    class PendingThread:
        def __init__(self,**_): pass
        def start(self): pass
    monkeypatch.setattr(iteration_lab.threading,"Thread",PendingThread)
    batch=iteration_lab.create(host,{"count":5})
    assert batch["count"]==1 and batch["candidates"][0]["status"]=="pending"
    with host.db_connection() as db:
        assert db.execute("SELECT COUNT(*) FROM pairwise_tasks").fetchone()[0]==1
    prompt="口述馆整理人员在核查一批数字录音时，发现旧规则只给出整段告警，无法确认异常片段的来源。"+"请沿现有解码与展示链路增加一条明确的片段复核流程，保持原有结论和兼容格式不变；新旧结果必须共用原始帧坐标，并在相同输入下输出稳定的差异区间。"*5
    prompt=prompt[:400]
    assert 350<=len(prompt)<=550
    calls=[]
    def model(instruction,schema,cwd,prefix,*_):
        calls.append((instruction,cwd,prefix))
        assert not (cwd/".git").exists()
        assert (cwd/"product.txt").read_text()=="B"
        return {"title":"片段复核","prompt":prompt,"estimated_lines":900} if prefix=="iteration-bank-generate" else {"approved":True,"difficulty":"困难","duplicate":False,"reason":""}
    monkeypatch.setattr(iteration_lab,"_history_prompts",lambda _:[])
    monkeypatch.setattr(host,"run_codex_structured",model)
    iteration_lab._generate(host,batch["id"])
    ready=iteration_lab.get(host,batch["id"])
    assert ready["candidates"][0]["status"]=="ready"
    assert len(calls)==2
    with pytest.raises(host.WorkflowError,match="人工核对"):
        iteration_lab.add_candidate(host,batch["id"],1)
    added=iteration_lab.add_candidate(host,batch["id"],1,confirmed=True)
    item=added["candidates"][0]
    assert item["status"]=="added" and item["actual_number"].endswith("-1")
    assert m.task(host,item["pair_id"])["source_attempt_id"]==b["id"]
    assert iteration_lab._available_sources(host)==[]


def test_iteration_bank_listing_paginates_in_database_and_recovers(host):
    from pairwise import iteration_lab
    now=host.now_text()
    with host.db_connection() as db:
        db.execute("INSERT INTO pairwise_iteration_batches "
                   "(id,status,detail,count,created_at,updated_at) VALUES (?,'running','测试',?,?,?)",
                   ("a"*12,11,now,now))
        db.executemany("INSERT INTO pairwise_iteration_candidates(batch_id,slot,source,status) "
                       "VALUES (?,?,?,'generating')",
                       [("a"*12,index,json.dumps({"root_number":index})) for index in range(1,12)])
        db.execute("UPDATE pairwise_iteration_candidates SET status='ready' WHERE slot=1")
    first=iteration_lab.listing(host,{"page":["1"]})
    second=iteration_lab.listing(host,{"page":["2"]})
    assert (first["total"],len(first["items"]),len(second["items"]),first["pages"])==(11,10,1,2)
    assert iteration_lab.listing(host,{"page":["999"]})["page"]==2
    with pytest.raises(host.WorkflowError,match="页码"):
        iteration_lab.listing(host,{"page":["bad"]})
    w.recover(host)
    restored=iteration_lab.get(host,"a"*12)
    assert restored["status"]=="partial"
    assert restored["candidates"][0]["status"]=="ready"
    assert restored["candidates"][1]["status"]=="failed"


def test_iteration_bank_failed_review_retains_prompt_and_can_retry_one_slot(host,monkeypatch):
    from pairwise import iteration_lab
    root=m.create(host,payload(repo_name="retry-source",task_type="0-1 代码生成",snapshot_url=""))
    complete(host,root["id"],"A")
    complete(host,root["id"],"B")
    class PendingThread:
        def __init__(self,**_): pass
        def start(self): pass
    monkeypatch.setattr(iteration_lab.threading,"Thread",PendingThread)
    monkeypatch.setattr(iteration_lab,"_history_prompts",lambda _:[])
    batch=iteration_lab.create(host,{"count":5})
    prompt=("馆员核查录音时发现新旧规则的时间区间不一致，需要在原始采样坐标中得到可试听的差异。"
            +"候选规则从现有解码后的数据计算，不改变基线结论；异常输入保留上次有效预览，换文件时清除旧结果。"
            +"跨扫描器、页面状态与波形共用同一差异结果，边界以原始帧判断，并用独立逐帧预言机与浏览器流程验收。"*7)[:400]
    assert len(prompt)>=350
    review_level=["困难偏低"]
    def model(_instruction,_schema,_cwd,prefix,*_):
        return ({"title":"规则差异复核","prompt":prompt,"estimated_lines":850}
                if prefix=="iteration-bank-generate" else
                {"approved":True,"duplicate":False,"difficulty":review_level[0],"reason":"内容符合要求"})
    monkeypatch.setattr(host,"run_codex_structured",model)
    iteration_lab._generate(host,batch["id"])
    failed=iteration_lab.get(host,batch["id"])["candidates"][0]
    assert failed["status"]=="failed"
    assert failed["draft"]["prompt"]==prompt
    assert failed["draft"]["estimated_lines"]==850
    assert failed["draft"]["review_checks"][0]["difficulty"]=="困难偏低"
    assert "difficulty='困难偏低'" in failed["error"]
    assert iteration_lab.retry_candidate(host,batch["id"],1)["candidates"][0]["status"]=="pending"
    review_level[0]="困难"
    iteration_lab._generate(host,batch["id"])
    final=iteration_lab.get(host,batch["id"])
    assert final["status"]=="complete"
    assert final["candidates"][0]["status"]=="ready"
    with host.db_connection() as db:
        assert db.execute("SELECT COUNT(*) FROM pairwise_tasks").fetchone()[0]==1


def test_iteration_bank_bulk_retry_queues_selected_failures_atomically(host,monkeypatch):
    from pairwise import iteration_lab
    now=host.now_text()
    batch_ids=["a"*12,"b"*12]
    with host.db_connection() as db:
        for index,batch_id in enumerate(batch_ids,1):
            db.execute("INSERT INTO pairwise_iteration_batches "
                       "(id,status,detail,count,created_at,updated_at) "
                       "VALUES (?,'partial','复核失败',1,?,?)",(batch_id,now,now))
            db.execute("INSERT INTO pairwise_iteration_candidates "
                       "(batch_id,slot,source,status,error) VALUES (?,?,?,'failed','原失败原因')",
                       (batch_id,1,json.dumps({"pair_id":str(index),"side":"A","sha":str(index)*40}),))
    checked=[]
    monkeypatch.setattr(iteration_lab,"_current",lambda _h,source: checked.append(source["pair_id"]))
    dispatched=[]
    class PendingThread:
        def __init__(self,*,target,args,**_): dispatched.append((target,args))
        def start(self): pass
    monkeypatch.setattr(iteration_lab.threading,"Thread",PendingThread)
    with pytest.raises(host.WorkflowError,match="只能重试"):
        iteration_lab.retry_candidates(host,[{"batch_id":batch_ids[0],"slot":1},
                                             {"batch_id":batch_ids[1],"slot":2}])
    with host.db_connection() as db:
        assert db.execute("SELECT COUNT(*) FROM pairwise_iteration_candidates WHERE status='failed'").fetchone()[0]==2
    result=iteration_lab.retry_candidates(host,[{"batch_id":value,"slot":1} for value in batch_ids])
    assert result=={"accepted":2,"batch_ids":batch_ids}
    assert checked==["1","2"]
    assert dispatched==[(iteration_lab._retry_many,(host,batch_ids))]
    with host.db_connection() as db:
        assert db.execute("SELECT COUNT(*) FROM pairwise_iteration_candidates WHERE status='pending' AND error=''").fetchone()[0]==2
        assert db.execute("SELECT COUNT(*) FROM pairwise_iteration_batches WHERE status='queued'").fetchone()[0]==2
        assert db.execute("SELECT COUNT(*) FROM pairwise_tasks").fetchone()[0]==0


def test_iteration_bank_rechecks_source_before_adding_candidate(host,monkeypatch):
    from pairwise import iteration_lab
    root=m.create(host,payload(repo_name="occupied-root",task_type="0-1 代码生成",snapshot_url=""))
    attempt=complete(host,root["id"],"A")
    m.update_attempt(host,m.latest(m.attempts(host,root["id"]),"B")["id"],status="stopped")
    class PendingThread:
        def __init__(self,**_): pass
        def start(self): pass
    monkeypatch.setattr(iteration_lab.threading,"Thread",PendingThread)
    batch=iteration_lab.create(host,{"count":5})
    with host.db_connection() as db:
        db.execute("UPDATE pairwise_iteration_candidates SET status='ready',draft=? WHERE batch_id=? AND slot=1",
                   (json.dumps({"title":"旧候选","prompt":"旧候选题面","estimated_lines":800}),batch["id"]))
    parent=m.task(host,root["id"])
    m.create(host,payload(repo_name="already-used-child",snapshot_url=parent["repo_url"]+"/commit/"+attempt["artifact_sha"],
                          source_pair_id=root["id"],source_attempt_id=attempt["id"],source_side="A",
                          source_artifact_sha=attempt["artifact_sha"],source_artifact_tree=attempt["artifact_tree"]))
    assert iteration_lab._available_sources(host)==[]
    with pytest.raises(host.WorkflowError,match="来源已被使用"):
        iteration_lab.add_candidate(host,batch["id"],1,confirmed=True)


def test_zero_one_bank_restart_keeps_finished_candidates(host,monkeypatch):
    from pairwise import zero_one_lab
    class PendingThread:
        def __init__(self, **_): pass
        def start(self): pass
    monkeypatch.setattr(zero_one_lab.threading,"Thread",PendingThread)
    batch=zero_one_lab.create(host,{"count":6})
    with host.db_connection() as db:
        db.execute("UPDATE pairwise_zero_one_batches SET status='running' WHERE id=?",(batch["id"],))
        db.execute("UPDATE pairwise_zero_one_candidates SET status='ready',draft=? WHERE batch_id=? AND slot=1",
                   (json.dumps({"first_prompt":"已完成题面"}),batch["id"]))
        db.execute("UPDATE pairwise_zero_one_candidates SET status='generating' WHERE batch_id=? AND slot=2",
                   (batch["id"],))
    w.recover(host)
    result=zero_one_lab.get(host,batch["id"])
    assert result["status"]=="partial"
    assert result["candidates"][0]["status"]=="ready"
    assert result["candidates"][0]["draft"]["first_prompt"]=="已完成题面"
    assert all(item["status"]=="failed" for item in result["candidates"][1:])


def test_zero_one_bank_listing_pages_candidates_in_database(host):
    from pairwise import api, zero_one_lab
    m.initialize(host)
    with host.db_connection() as db:
        for batch_id, created_at, start, status in (
            ("aaaaaaaaaaaa", "2026-09-26 10:00:00", 100, "complete"),
            ("bbbbbbbbbbbb", "2026-09-27 10:00:00", 200, "running"),
        ):
            db.execute(
                "INSERT INTO pairwise_zero_one_batches "
                "(id,status,detail,count,suggested_start,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (batch_id, status, "进度", 12, start, created_at, created_at),
            )
            for slot in range(1, 13):
                db.execute(
                    "INSERT INTO pairwise_zero_one_candidates "
                    "(batch_id,slot,category,status,draft) VALUES (?,?,?,?,?)",
                    (batch_id, slot, "纯后端", "ready",
                     json.dumps({"project_name": f"{batch_id}-{slot}"})),
                )
    first = zero_one_lab.listing(host, {"page": ["1"]})
    assert (first["total"], first["pages"], first["page_size"], first["active_batches"]) == (24, 2, 20, 1)
    assert [item["suggested_number"] for item in first["items"]] == [
        *[f"{number:04d}" for number in range(200, 212)],
        *[f"{number:04d}" for number in range(100, 108)],
    ]
    second = zero_one_lab.listing(host, {"page": ["2"]})
    assert (second["page"], second["total"], second["active_batches"]) == (2, 24, 1)
    assert [item["suggested_number"] for item in second["items"]] == [
        "0108", "0109", "0110", "0111"
    ]
    assert zero_one_lab.listing(host, {"page": ["999"]})["page"] == 2
    with pytest.raises(host.WorkflowError, match="页码"):
        zero_one_lab.listing(host, {"page": ["bad"]})
    handler = mock.Mock()
    api.get(handler, host, "/api/pairwise/zero-one-batches", {"page": ["2"]})
    assert handler.send_json.call_args[0][0]["page"] == 2



def test_background_prefill_starts_with_idle_claude_and_parallel_main_lane(host, monkeypatch):
    from pairwise import auto_refill, candidate_bank
    prompts = [
        "剧场灯光指令在设备掉线后恢复，拒绝旧版本回执。",
        "冷链交接核对每次签收的温区版本，超限样本不能重新放行。",
        "图像发布审核拒绝回滚后迟到的元数据，维持预览和正式版本一致。",
        "广播时隙调度在终端离线和重连时保持公平次序。",
    ]
    calls = []

    def generate(**kwargs):
        index = len(calls)
        calls.append(kwargs)
        return {
            "repo_name": f"prefill-idle-{index}", "first_prompt": prompts[index],
            "task_difficulty": "困难", "difficulty_review": sample_difficulty(),
            "complexity_analysis": sample_complexity(), "category": "纯后端",
            "language_framework": "Python, FastAPI, SQLite",
            "verification_commands": ["pytest -q"],
        }

    monkeypatch.setattr(host, "generate_task_draft", generate)
    monkeypatch.setattr(auto_refill, "ACTIVE", True)
    monkeypatch.setattr(auto_refill, "PREFILL_ACTIVE", False)
    monkeypatch.setattr(auto_refill, "PREFILL_NEXT_AT", 0.0)
    auto_refill.configure(host, {"enabled": True, "start_after_hours": 0,
        "project_directory": str(host.PROJECTS_ROOT / "zzzz-gsb")})
    for expected in range(1, 5):
        assert auto_refill._prefill_once(host)
        assert candidate_bank.ready_count(host) == expected
    assert auto_refill._prefill_once(host) is False
    assert len(calls) == 4
    assert auto_refill.configuration(host)["created_count"] == 0
    assert auto_refill.configuration(host)["candidate_target"] == 4
    monkeypatch.setattr(auto_refill, "ACTIVE", False)
    assert auto_refill.run_once(host)
    assert len(calls) == 4  # Main lane claims stock instead of generating again.
    assert candidate_bank.ready_count(host) == 3


def test_background_prefill_diversifies_while_bug_dispatch_is_generating(host, monkeypatch):
    from pairwise import auto_refill
    auto_refill.configure(host, {"enabled": True,
        "project_directory": str(host.PROJECTS_ROOT / "zzzz-gsb")})
    host.write_settings({"pairwise_auto_next_type": "Bug 修复"})
    monkeypatch.setattr(auto_refill, "ACTIVE", True)
    monkeypatch.setattr(auto_refill, "PREFILL_ACTIVE", False)
    monkeypatch.setattr(auto_refill, "PREFILL_NEXT_AT", 0.0)
    host.write_settings({"pairwise_auto_created": "1"})
    monkeypatch.setattr(auto_refill, "daily_task_type_counts", lambda h: (
        {"0-1 代码生成": 3, "Feature 迭代": 1, "Bug 修复": 0}, {}, {}
    ))
    planned = []
    monkeypatch.setattr(auto_refill, "next_available_task",
                        lambda h, counts, blocked, **kwargs: (planned.append(counts) or ("", None, [])))
    assert auto_refill._prefill_once(host) is False
    assert planned == [{"0-1 代码生成": 3, "Feature 迭代": 1, "Bug 修复": 0}]


def test_new_balanced_batch_reserves_one_prefill_lane_per_task_type(host):
    from pairwise import auto_refill
    values = {
        "pairwise_auto_created": "0",
        "pairwise_auto_task_type_mode": "balanced",
    }
    historical = {"0-1 代码生成": 3, "Feature 迭代": 3, "Bug 修复": 0}
    ready = {"0-1 代码生成": 0, "Feature 迭代": 0, "Bug 修复": 0}

    first = auto_refill._prefill_planned_counts(values, historical, ready, [])
    second = auto_refill._prefill_planned_counts(
        values, historical, ready, ["0-1 代码生成"]
    )
    third = auto_refill._prefill_planned_counts(
        values, historical, ready, ["0-1 代码生成", "Feature 迭代"]
    )

    assert auto_refill.balanced_task_type(first) == "0-1 代码生成"
    assert auto_refill.balanced_task_type(second) == "Feature 迭代"
    assert auto_refill.balanced_task_type(third) == "Bug 修复"


def test_balanced_prefill_does_not_duplicate_an_active_task_type(host, monkeypatch):
    from pairwise import auto_refill
    monkeypatch.setattr(
        auto_refill, "continuation_source",
        lambda h, kind, *args, **kwargs: {"kind": kind},
    )
    counts = {kind: 0 for kind in auto_refill.BALANCED_TASK_TYPES}

    kind, source, _ = auto_refill.next_available_task(
        host,
        counts,
        {},
        ready_mode="exclude",
        excluded_task_types={"0-1 代码生成", "Bug 修复"},
    )

    assert kind == "Feature 迭代"
    assert source == {"kind": "Feature 迭代"}


def test_balanced_prefill_reserves_lane_while_feature_retries(host, monkeypatch):
    from pairwise import auto_refill
    auto_refill.configure(host, {"enabled": True,
        "project_directory": str(host.PROJECTS_ROOT / "zzzz-gsb")})
    monkeypatch.setattr(auto_refill, "PREFILL_ACTIVE", False)
    monkeypatch.setattr(auto_refill, "PREFILL_NEXT_AT", 0.0)
    monkeypatch.setattr(auto_refill, "PREFILL2_ACTIVE", True)
    monkeypatch.setattr(auto_refill, "PREFILL2_TASK_TYPE", "0-1 代码生成")
    monkeypatch.setattr(auto_refill, "PREFILL3_ACTIVE", True)
    monkeypatch.setattr(auto_refill, "PREFILL3_TASK_TYPE", "Bug 修复")
    seen = {}

    def choose(h, counts, blocked, **kwargs):
        seen.update(kwargs)
        return "", None, []

    monkeypatch.setattr(auto_refill, "next_available_task", choose)

    assert auto_refill._prefill_once(host, lane=1) is False
    assert seen["excluded_task_types"] == {"0-1 代码生成", "Bug 修复"}


def test_existing_batch_keeps_daily_deficit_planning(host):
    from pairwise import auto_refill
    values = {
        "pairwise_auto_created": "1",
        "pairwise_auto_task_type_mode": "balanced",
    }
    historical = {"0-1 代码生成": 3, "Feature 迭代": 3, "Bug 修复": 0}
    ready = {"0-1 代码生成": 0, "Feature 迭代": 0, "Bug 修复": 0}

    planned = auto_refill._prefill_planned_counts(values, historical, ready, [])

    assert planned == historical
    assert auto_refill.balanced_task_type(planned) == "Bug 修复"


def test_pairwise_auto_refill_generates_creates_history_and_starts_both_sides(host,monkeypatch):
    from pairwise import auto_refill, history
    generated={
        "repo_name":"auto-hard-project",
        "first_prompt":"从空 README 实现一个有跨请求不变量的困难项目。",
        "task_difficulty":"困难","difficulty_review":sample_difficulty(),"complexity_analysis":sample_complexity(),
        "language_framework":"Python, FastAPI, SQLite",
        "verification_commands":["pytest -q"],
    }
    seen={}
    def generate(**kwargs):
        seen.update(kwargs)
        kwargs["progress"]("合成生成进度")
        return generated
    monkeypatch.setattr(host,"generate_task_draft",generate)
    config=auto_refill.configure(host,{"enabled":True,"start_after_hours":0,
        "project_directory":str(host.PROJECTS_ROOT/"zzzz-gsb")})
    assert config["enabled"]
    assert auto_refill._prefill_once(host)
    assert auto_refill.run_once(host)
    config=auto_refill.configuration(host)
    assert config["created_count"]==1 and config["enabled"]
    with host.db_connection() as db:
        row=db.execute("SELECT id,origin,difficulty,project_directory FROM pairwise_tasks").fetchone()
    assert dict(row)["origin"]=="auto"
    assert dict(row)["difficulty"]=="困难"
    assert dict(row)["project_directory"]=="zzzz-gsb"
    assert {item["side"] for item in m.attempts(host,row["id"])}=={"A","B"}
    assert seen["history_override"]==[] and seen["project_number"]==1
    assert seen["race_lanes"]==1
    assert "一次计算" in seen["initial_feedback"]
    assert history.records(host)[0]["repo_name"]=="auto-hard-project"
    text=host.PAIRWISE_HISTORY_PATH.read_text(encoding="utf-8")
    assert "auto-hard-project" in text and "<!-- prompt-start -->" in text


def test_pairwise_auto_refill_balances_all_three_task_types(host):
    from pairwise import auto_refill
    m.create(host,payload(title="root one",repo_name="root-one",task_type="0-1 代码生成",snapshot_url=""))
    m.create(host,payload(title="root two",repo_name="root-two",task_type="0-1 代码生成",snapshot_url=""))
    counts=auto_refill.task_type_counts(host)
    assert counts=={"0-1 代码生成":2,"Feature 迭代":0,"Bug 修复":0}
    assert auto_refill.balanced_task_type(counts)=="Feature 迭代"
    m.create(host,payload(title="feature one",repo_name="feature-one",task_type="Feature 迭代"))
    assert auto_refill.balanced_task_type(auto_refill.task_type_counts(host))=="Bug 修复"
    m.create(host,payload(title="bug one",repo_name="bug-one",task_type="Bug 修复"))
    counts=auto_refill.task_type_counts(host)
    assert counts=={"0-1 代码生成":2,"Feature 迭代":1,"Bug 修复":1}
    assert auto_refill.balanced_task_type(counts)=="Feature 迭代"


def test_daily_mix_excludes_yesterday_and_post_review_medium_without_deleting(host):
    from pairwise import auto_refill
    root=m.create(host,payload(repo_name="todays-hard-root",task_type="0-1 代码生成",snapshot_url=""))
    medium=m.create(host,payload(repo_name="todays-medium-root",task_type="0-1 代码生成",snapshot_url=""))
    old=m.create(host,payload(repo_name="yesterdays-feature",task_type="Feature 迭代"))
    with host.db_connection() as db:
        db.execute("UPDATE pairwise_tasks SET difficulty_status='complete',difficulty_review=? WHERE id=?",
                   (json.dumps({'level':'中等'}),medium['id']))
        db.execute("UPDATE pairwise_tasks SET created_at='2001-01-01 00:00:00 +0800' WHERE id=?",(old['id'],))
        db.execute("UPDATE pairwise_tasks SET difficulty_status='complete',difficulty_review=? WHERE id=?",
                   (json.dumps({'level':'困难'}),root['id']))
    counts,qualified,downgraded=auto_refill.daily_task_type_counts(host)
    assert counts=={'0-1 代码生成':1,'Feature 迭代':0,'Bug 修复':0}
    assert qualified==counts
    assert downgraded=={'0-1 代码生成':1,'Feature 迭代':0,'Bug 修复':0}
    config=auto_refill.configuration(host)
    assert config['task_type_counts']==counts and config['daily_downgraded_counts']==downgraded
    assert auto_refill.task_type_counts(host)=={'0-1 代码生成':2,'Feature 迭代':1,'Bug 修复':0}


def test_pairwise_continuation_source_prefers_confirmed_winner(host):
    from pairwise import auto_refill
    pair_id,a,b=ready_pair(host,task_type="0-1 代码生成",snapshot_url="",
                           title="source root",repo_name="source-root")
    pair=m.task(host,pair_id)
    m.save_review(host,pair_id,{"gsb":"B 更好","reason":"B 的实现完整且验收通过，A 也完成需求，但 B 的边界处理和产物结构更清楚。两侧都有可复核轨迹，后续快照采用 B 可保留更明确的边界行为。",
        "human_confirmed":True,"revision":pair["revision"]})
    source=auto_refill.continuation_source(host,"Feature 迭代")
    assert source["pair"]["id"]==pair_id
    assert source["side"]=="B"
    assert source["attempt"]["artifact_sha"]==b["artifact_sha"]


def refill_root_draft(name="bootstrap-root"):
    return {"repo_name":name,"first_prompt":"从空仓库建立具有跨请求不变量的困难项目。",
        "task_difficulty":"困难","difficulty_review":sample_difficulty(),"complexity_analysis":sample_complexity(),"category":"纯后端","language_framework":"Python"}


def test_pairwise_auto_refill_bootstraps_without_followup_source(host,monkeypatch):
    from pairwise import auto_refill
    m.create(host,payload(title="unfinished root",repo_name="unfinished-root",
                          task_type="0-1 代码生成",snapshot_url=""))
    called=[]
    monkeypatch.setattr(host,"generate_task_draft",lambda **kwargs:called.append(kwargs) or refill_root_draft())
    auto_refill.configure(host,{"enabled":True,"start_after_hours":0,
        "project_directory":str(host.PROJECTS_ROOT/"zzzz-gsb")})
    assert auto_refill._prefill_once(host)
    assert auto_refill.run_once(host)
    config=auto_refill.configuration(host)
    assert config["created_count"]==1 and config["enabled"]
    assert config["status"]=="ready" and not config["needs_attention"]
    assert not config["generating"]
    assert "1:1:1" in config["dispatch_reason"]
    assert config["task_type_counts"]=={"0-1 代码生成":2,"Feature 迭代":0,"Bug 修复":0}
    assert len(called)==1


def test_pairwise_auto_refill_generates_feature_pair_from_archived_snapshot(host,monkeypatch):
    from pairwise import auto_refill, history
    pair_id,a,_=ready_pair(host,task_type="0-1 代码生成",snapshot_url="",
                           title="root product",repo_name="root-product")
    captured={}
    candidate={
        "task_type":"Feature 迭代",
        "prompt":"在现有项目中新增一个拥有独立核心对象、状态生命周期和主流程的完整模块，并接入既有服务与验收入口。",
        "task_difficulty":"困难","difficulty_review":sample_difficulty(),"complexity_analysis":sample_complexity(),
    }
    def clone(repo_url,path,sha):
        repository(path)
        return sha
    def generate(context,target_task_type,*feedback):
        captured.update(context)
        assert target_task_type=="Feature 迭代"
        return candidate
    monkeypatch.setattr(host,"clone_repository_snapshot",clone)
    monkeypatch.setattr(host,"_generate_iteration_candidate_for_context",generate)
    monkeypatch.setattr(host,"unique_repo_name",lambda value:value)
    auto_refill.configure(host,{"enabled":True,"start_after_hours":0,
        "project_directory":str(host.PROJECTS_ROOT/"zzzz-gsb")})
    host.write_settings({"pairwise_auto_created": "1"})
    assert auto_refill._prefill_once(host)
    assert auto_refill.run_once(host)
    with host.db_connection() as db:
        row=db.execute("""SELECT * FROM pairwise_tasks
                          WHERE id<>? ORDER BY created_at DESC LIMIT 1""",(pair_id,)).fetchone()
    created=m.task(host,row["id"])
    assert created["task_type"]=="Feature 迭代"
    assert created["difficulty"]=="困难"
    assert created["source_repo_url"]==m.task(host,pair_id)["repo_url"]
    assert created["source_base_sha"]==a["artifact_sha"]
    assert captured["iteration_history"][0]["task_type"]=="0-1 代码生成"
    assert "_feature_shape_mode" not in captured
    assert "_concept_overlap_feedback" not in captured
    assert any(
        item["task_type"]=="Feature 迭代" and item["repo_name"]==created["repo_name"]
        for item in history.records(host)
    )
    assert auto_refill.configuration(host)["task_type_counts"]=={
        "0-1 代码生成":1,"Feature 迭代":1,"Bug 修复":0
    }


def test_pairwise_refill_falls_back_to_equally_missing_bug_type(host,monkeypatch):
    from pairwise import auto_refill
    pair_id,_,_=ready_pair(host,task_type="0-1 代码生成",snapshot_url="",
        title="fallback root",repo_name="fallback-root")
    auto_refill.configure(host,{"enabled":True,"start_after_hours":0,
        "project_directory":str(host.PROJECTS_ROOT/"zzzz-gsb")})
    blocked={f"Feature 迭代:{pair_id}":"规则 C"}
    host.write_settings({"pairwise_auto_blocked_sources":json.dumps(blocked)})
    seen=[]
    def generate(h,source,target,progress,notebook_feedback=""):
        seen.append(target)
        progress("正在生成真实 Bug 题面")
        current=auto_refill.configuration(h)
        assert current["prefill_generating"]
        assert current["prefill_task_type"]=="Bug 修复"
        return {"task_type":target,"task_difficulty":"困难","difficulty_review":sample_difficulty(),"complexity_analysis":sample_complexity(),
            "prompt":"修复现有项目中已复现的并发状态冲突和重复执行问题，通过确定性验收验证结果。"}
    monkeypatch.setattr(auto_refill,"_generate_continuation",generate)
    monkeypatch.setattr(natural_bug,"discover",lambda *args:None)
    monkeypatch.setattr(host,"clone_repository_snapshot",
                        lambda repo_url,path,sha:clone_archived_fixture(host,repo_url,path,sha))
    monkeypatch.setattr(host,"unique_repo_name",lambda value:value)
    monkeypatch.setattr(host,"generate_task_draft",lambda **_:pytest.fail("must not widen 0-1 count"))
    assert auto_refill._prefill_once(host)
    assert auto_refill.run_once(host)
    assert seen and set(seen)=={"Bug 修复"} and len(seen)<=2
    assert auto_refill.task_type_counts(host)=={"0-1 代码生成":1,"Feature 迭代":0,"Bug 修复":1}
    assert len(auto_refill._blocked_sources(auto_refill._values(host)))==1


def test_all_followup_types_blocked_bootstrap_without_self_wake(host,monkeypatch):
    from pairwise import auto_refill
    from pairwise import natural_bug
    pair_id,_,_=ready_pair(host,task_type="0-1 代码生成",snapshot_url="",
        title="blocked root",repo_name="blocked-root")
    auto_refill.configure(host,{"enabled":True,"start_after_hours":0,
        "project_directory":str(host.PROJECTS_ROOT/"zzzz-gsb")})
    host.write_settings({"pairwise_auto_blocked_sources":json.dumps({
        f"{kind}:{pair_id}":"规则 C" for kind in ("Feature 迭代","Bug 修复")})})
    wake=mock.Mock()
    monkeypatch.setattr(auto_refill,"WAKE",wake)
    monkeypatch.setattr(auto_refill,"_generate_continuation",lambda *_:pytest.fail("blocked roots must stay blocked"))
    monkeypatch.setattr(host,"generate_task_draft",lambda **_:refill_root_draft())
    assert auto_refill._prefill_once(host)
    assert auto_refill.run_once(host)
    config=auto_refill.configuration(host)
    assert config["status"]=="ready" and config["enabled"]
    assert not config["needs_attention"] and not config["generating"]
    assert "1:1:1" in config["dispatch_reason"]
    assert config["blocked_source_count"]==2
    wake.set.assert_not_called()


def test_bootstrap_returns_to_deficits_after_artifact_arrives(host,monkeypatch):
    from pairwise import auto_refill
    p=m.create(host,payload(task_type="0-1 代码生成",snapshot_url="",repo_name="arriving-root"))
    auto_refill.configure(host,{"enabled":True,"start_after_hours":0,
        "project_directory":str(host.PROJECTS_ROOT/"zzzz-gsb")})
    w.start(host,p["id"])
    host.set_max_parallel_runs({"max_parallel":6})
    monkeypatch.setattr(host,"generate_task_draft",lambda **_:refill_root_draft())
    assert auto_refill._prefill_once(host)
    assert auto_refill.run_once(host)
    complete(host,p["id"],"A")
    complete(host,p["id"],"B")
    approve_difficulty(host,p["id"])
    monkeypatch.setattr(auto_refill,"_generate_continuation",lambda h,s,k,progress,notebook_feedback="":{
        "task_type":k,"task_difficulty":"困难","difficulty_review":sample_difficulty(),"complexity_analysis":sample_complexity(),"prompt":"新增独立完整模块，包含独立用户流程和确定性验收。"})
    monkeypatch.setattr(host,"unique_repo_name",lambda value:value)
    monkeypatch.setattr(host,"generate_task_draft",lambda **_:pytest.fail("repay available deficit first"))
    assert auto_refill._prefill_once(host)
    assert auto_refill.run_once(host)
    assert not auto_refill.configuration(host)["needs_attention"]
    assert auto_refill.task_type_counts(host)=={"0-1 代码生成":2,"Feature 迭代":1,"Bug 修复":0}


def test_pairwise_auto_refill_blocks_rule_c_root_and_rotates_source(host,monkeypatch):
    from pairwise import auto_refill
    first_id,_,_=ready_pair(host,task_type="0-1 代码生成",snapshot_url="",
                            title="first root",repo_name="first-root")
    second_id,_,_=ready_pair(host,task_type="0-1 代码生成",snapshot_url="",
                             title="second root",repo_name="second-root")
    seen=[]
    def clone(repo_url,path,sha):
        repository(path)
        return sha
    def generate(context,target,*feedback):
        seen.append((context["repo_name"],context.get("_concept_overlap_feedback",""),feedback))
        assert "_feature_shape_mode" not in context
        if len(seen)<=2:
            raise host.SameRepositoryOverlapError(
                "同仓库规则 C 预检未通过：候选仍沿用原主流程"
            )
        return {
            "task_type":target,
            "prompt":"在现有运行架构中新增具有独立核心对象、状态生命周期和用户主流程的完整模块，并接入公共入口与确定性验收。",
            "task_difficulty":"困难","difficulty_review":sample_difficulty(),"complexity_analysis":sample_complexity(),
        }
    monkeypatch.setattr(host,"clone_repository_snapshot",clone)
    monkeypatch.setattr(host,"_generate_iteration_candidate_for_context",generate)
    monkeypatch.setattr(host,"unique_repo_name",lambda value:value)
    auto_refill.configure(host,{"enabled":True,"start_after_hours":0,
        "project_directory":str(host.PROJECTS_ROOT/"zzzz-gsb")})
    host.write_settings({"pairwise_auto_created": "1"})
    assert auto_refill._prefill_once(host) is False
    config=auto_refill.configuration(host)
    assert config["enabled"] and config["blocked_source_count"]==1
    assert "排除记录" in config["detail"]
    assert len(seen)==4 and len({item[0] for item in seen})==1
    assert any("仍沿用原主流程" in item[1] for item in seen)
    assert any(item[2] and "仍沿用原主流程" in item[2][0] for item in seen)
    assert auto_refill._prefill_once(host)
    assert auto_refill.run_once(host)
    assert len(seen)>=3
    with host.db_connection() as db:
        created=db.execute("SELECT source_repo_url FROM pairwise_tasks WHERE task_type='Feature 迭代'").fetchone()
    assert created["source_repo_url"]==m.task(host,second_id if seen[2][0]=="second-root" else first_id)["repo_url"]


def test_pairwise_auto_refill_generates_bugfix_pair_and_rejects_medium(host,monkeypatch):
    from pairwise import auto_refill
    pair_id,a,_=ready_pair(host,task_type="0-1 代码生成",snapshot_url="",
                           title="bug source",repo_name="bug-source")
    source_pair=m.task(host,pair_id)
    feature = m.create(host,payload(title="existing feature",repo_name="existing-feature",
        task_type="Feature 迭代",
        snapshot_url=f"{source_pair['repo_url']}/commit/{a['artifact_sha']}"))
    complete(host, feature["id"], "A")
    def clone(repo_url,path,sha):
        repository(path)
        return sha
    monkeypatch.setattr(host,"clone_repository_snapshot",clone)
    monkeypatch.setattr(host,"unique_repo_name",lambda value:value)
    monkeypatch.setattr(host,"_generate_iteration_candidate_for_context",lambda context,target,*feedback:{
        "task_type":target,
        "prompt":"修复同一主流程中已复现的并发重复写入、过期状态误接受和失败重试丢失输入等真实问题。",
        "task_difficulty":"中等",
    })
    auto_refill.configure(host,{"enabled":True,"start_after_hours":0,
        "project_directory":str(host.PROJECTS_ROOT/"zzzz-gsb")})
    host.write_settings({"pairwise_auto_created": "1"})
    host.set_max_parallel_runs({"max_parallel": 6})
    assert auto_refill._prefill_once(host) is False
    assert auto_refill.task_type_counts(host)["Bug 修复"]==0
    assert "困难度复核" in auto_refill.configuration(host)["prefill_error"]
    assert auto_refill.configuration(host)["blocked_source_count"]==0
    with host.db_connection() as db:
        db.execute(
            "UPDATE pairwise_source_locks SET updated_at=? WHERE source_pair_id=? AND task_type=?",
            ('2020-01-01 00:00:00 +0800', feature["id"], 'Bug 修复'),
        )

    monkeypatch.setattr(host,"_generate_iteration_candidate_for_context",lambda context,target,*feedback:{
        "task_type":target,
        "prompt":"修复同一主流程中已复现的并发重复写入、过期状态误接受和失败重试丢失输入等真实问题。",
        "task_difficulty":"困难","difficulty_review":sample_difficulty(),"complexity_analysis":sample_complexity(),
    })
    assert auto_refill._prefill_once(host)
    assert auto_refill.run_once(host)
    with host.db_connection() as db:
        row=db.execute("""SELECT task_type,difficulty,source_repo_url,source_base_sha
                          FROM pairwise_tasks WHERE task_type='Bug 修复'""").fetchone()
    assert dict(row)["task_type"]=="Bug 修复"
    assert dict(row)["difficulty"]=="困难"
    assert dict(row)["source_repo_url"]==source_pair["repo_url"]
    assert dict(row)["source_base_sha"]


def test_pairwise_auto_refill_seeds_static_bug_only_after_natural_routes_miss(host,monkeypatch):
    from pairwise import auto_refill, seeded_bug, natural_bug
    pair_id,attempt,_=ready_pair(host,task_type="0-1 代码生成",snapshot_url="",
                                title="existing bug source",repo_name="existing-bug-source")
    source_pair=m.task(host,pair_id)
    feature = m.create(host,payload(title="existing feature",repo_name="existing-bug-feature",
        task_type="Feature 迭代",
        snapshot_url=f"{source_pair['repo_url']}/commit/{attempt['artifact_sha']}"))
    complete(host, feature["id"], "A")
    monkeypatch.setattr(host,"clone_repository_snapshot",
                        lambda repo_url,path,sha:clone_archived_fixture(host,repo_url,path,sha))
    monkeypatch.setattr(host,"unique_repo_name",lambda value:value)
    natural_calls=[]
    monkeypatch.setattr(natural_bug,"discover_verified",
                        lambda *args,**kwargs:natural_calls.append(kwargs.get("lanes")) or None)
    monkeypatch.setattr(seeded_bug,"reference_fix",lambda *args:pytest.fail("Static seed must not run a reference repair"))
    design={'candidate_id':'A','candidate':{'proposed_change':'接受过期结果','original_invariants':'输入版本一致',
            'code_locations':['product.txt:apply']}}
    monkeypatch.setattr(seeded_bug,'design_candidates',lambda *args:[design])
    def seed(h,source,work,progress,design=None):
        assert source['bug_verification_mode']=='static_first'
        return {'sha':source['attempt']['artifact_sha'],'tree':source['attempt']['artifact_tree'],
                'verification_mode':'static_first','runtime_verified':False,'changed_files':['product.txt'],
                'description':{'affected_flow':'输入与状态绑定','observable_failure':'错误接受过期结果',
                               'expected_behavior':'保持同一输入版本'}}
    monkeypatch.setattr(seeded_bug,'seed',seed)
    monkeypatch.setattr(seeded_bug,'bundle',lambda h,*args:{**fake_seed_snapshot(h),
        'verification_mode':'static_first','runtime_verified':False,'code_evidence':['product.txt:apply'],
        'static_analysis':{'counterexample':'输入与返回次序交错'}})
    review={"approved":True,"difficulty_review":sample_difficulty(),"bug_review":{
        "verified_bug_count":2,"unverified_bugs":[],"overlapping_sequences":[],
        "solution_leaks":[],"style_issues":[],"scope_too_large":False,"single_focus":True}}
    def generate(context,target,*feedback):
        assert target=="Bug 修复"
        assert context['_bug_verification_mode']=='static_first'
        assert context['_seeded_defect']['changed_files']==['product.txt']
        assert '_existing_defect' not in context
        return {"task_type":target,"prompt":"修复输入更新过程中错误接受过期结果的问题，保持同一输入版本的一致性。",
                "task_difficulty":"困难","difficulty_review":sample_difficulty(),
                "complexity_analysis":sample_complexity(),"confirmed_bugs":[{"evidence":"静态代码推导"}],
                "independent_review":review,"evidence_source_commit":context["current_commit"],
                "evidence_verified_at":host.now_text()}
    monkeypatch.setattr(host,"_generate_iteration_candidate_for_context",generate)
    auto_refill.configure(host,{"enabled":True,"start_after_hours":0,
        "project_directory":str(host.PROJECTS_ROOT/"zzzz-gsb"),'task_type_mode':'bug_only'})
    assert auto_refill.configuration(host)['bug_generation_strategy']=='existing_first_then_seed'
    assert auto_refill.configuration(host)['bug_verification_mode']=='static_first'
    assert auto_refill._prefill_once(host)
    dispatched = auto_refill.run_once(host)
    with host.db_connection() as db:
        bank_rows = [dict(row) for row in db.execute(
            "SELECT task_type,status,source_pair_id,source_attempt_id,source_sha,reason FROM pairwise_candidate_bank"
        )]
    dispatch_config = auto_refill.configuration(host)
    assert dispatched, json.dumps({
        "status": dispatch_config["status"], "detail": dispatch_config["detail"],
        "error": dispatch_config["error"], "next": dispatch_config["next_task_type"],
        "ready": dispatch_config["candidate_ready"], "bank": bank_rows,
    }, ensure_ascii=False)
    with host.db_connection() as db:
        row=db.execute("""SELECT source_repo_url,source_base_sha,difficulty_preflight
                          FROM pairwise_tasks WHERE task_type='Bug 修复'""").fetchone()
    assert row["source_repo_url"]==row["source_base_sha"]==''
    preflight=json.loads(row['difficulty_preflight'])
    assert natural_calls==[2]
    assert 'existing_bug_evidence' not in preflight
    assert preflight['bug_generation_strategy']=='existing_first_fallback_seed'
    assert preflight['bug_verification_mode']=='static_first' and preflight['runtime_verified'] is False
    assert preflight['seed_snapshot']['verification_mode']=='static_first'


def test_natural_bug_no_finding_is_not_cached_across_runs(tmp_path):
    from pairwise import natural_bug
    calls=[]
    class FakeHost:
        ITERATION_GENERATION_MODEL="model"
        CODEX_ROUTINE_REASONING_EFFORT="medium"
        def run_codex_structured(self,*args,**kwargs):
            calls.append(args)
            if len(calls)==1:
                return {"found":False}
            return {"found":True,"affected_flow":"flow","problems":[
                    {"trigger":"run one","observable_failure":"failure one",
                     "expected_behavior":"expected one","evidence":"observed one",
                     "code_locations":["app.py:run"]},
                    {"trigger":"run two","observable_failure":"failure two",
                     "expected_behavior":"expected two","evidence":"observed two",
                     "code_locations":["app.py:save"]}],
                    "minimal_fix":"fix","routine_assessment":"不足够",
                    "counterexample":"counterexample","code_locations":["app.py:run"]}
    source={"pair":{"prompt":"source task"},"attempt":{"artifact_sha":"a"*40,"artifact_tree":"b"*40}}
    assert natural_bug.discover(FakeHost(),source,tmp_path,"[]") is None
    found=natural_bug.discover(FakeHost(),source,tmp_path,"[]")
    assert found["source_sha"]=="a"*40 and len(calls)==2


def test_natural_bug_discovery_and_lane_reverification_are_audited(host, monkeypatch, tmp_path):
    from pairwise import natural_bug
    source={"pair":{"id":"source-pair","prompt":"source task"},
            "attempt":{"artifact_sha":"a"*40,"artifact_tree":"b"*40}}
    def model(prompt,schema,work,stage,*args,**kwargs):
        if stage=="natural-bug-discovery":
            return {"found":True,"affected_flow":"flow","problems":[
                    {"trigger":"run one","observable_failure":"failure one",
                     "expected_behavior":"expected one","evidence":"observed one",
                     "code_locations":["app.py:run"]},
                    {"trigger":"run two","observable_failure":"failure two",
                     "expected_behavior":"expected two","evidence":"observed two",
                     "code_locations":["app.py:save"]}],
                    "minimal_fix":"fix","routine_assessment":"不足够",
                    "counterexample":"counterexample","code_locations":["app.py:run"]}
        return {"verified":True,"verified_problem_count":2,
                "unverified_problem_indexes":[],
                "actual_evidence":"same failures observed","reason":"reproduced"}
    monkeypatch.setattr(host,"run_codex_structured",model)
    found=natural_bug.discover(host,source,tmp_path,"[]")
    assert found["discovery_id"]
    assert natural_bug.reverify(host,found,tmp_path)["source_sha"]=="a"*40
    with host.db_connection() as db:
        discovery=db.execute("SELECT source_sha,evidence_json FROM pairwise_natural_bug_discoveries WHERE id=?",
                             (found["discovery_id"],)).fetchone()
        lane=db.execute("SELECT source_sha,verified,actual_evidence FROM pairwise_natural_bug_reverifications WHERE discovery_id=?",
                        (found["discovery_id"],)).fetchone()
    assert discovery["source_sha"]=="a"*40 and "counterexample" in discovery["evidence_json"]
    assert lane["source_sha"]=="a"*40 and lane["verified"]==1


def test_natural_bug_reverification_rejects_partially_confirmed_problem_group(host, monkeypatch, tmp_path):
    from pairwise import natural_bug
    evidence={"source_sha":"a"*40,"source_tree":"b"*40,
              "problems":[{"trigger":"one"},{"trigger":"two"}]}
    monkeypatch.setattr(host,"run_codex_structured",lambda *args,**kwargs:{
        "verified":False,"verified_problem_count":1,"unverified_problem_indexes":[2],
        "actual_evidence":"only the first problem is supported","reason":"second is speculative",
    })
    assert natural_bug.reverify(host,evidence,tmp_path) is None


def test_natural_bug_runs_two_discovery_lanes_and_accepts_verified_winner(host, monkeypatch, tmp_path):
    from pairwise import natural_bug
    barrier=threading.Barrier(2)
    discoveries=[]
    source={"pair":{"id":"source-pair","prompt":"source task"},
            "attempt":{"artifact_sha":"a"*40,"artifact_tree":"b"*40}}

    def discover(*args, **kwargs):
        lane=len(discoveries)+1
        discoveries.append(lane)
        barrier.wait(timeout=2)
        return {"discovery_id":f"d{lane}","source_sha":"a"*40,"source_tree":"b"*40,
                "affected_flow":"flow","problems":[{"trigger":"one"},{"trigger":"two"}]}

    monkeypatch.setattr(natural_bug,"discover",discover)
    monkeypatch.setattr(natural_bug,"reverify",lambda h,evidence,work:{
        "actual_evidence":"verified","source_sha":evidence["source_sha"]
    })
    result=natural_bug.discover_verified(host,source,tmp_path,"[]",lanes=2)
    assert sorted(discoveries)==[1,2]
    assert result["lane"] in {1,2}
    assert result["discovery"]["discovery_id"] in {"d1","d2"}


def test_medium_overlap_requires_confirmed_same_result_or_defect_chain(host):
    generic={"overlap_level":"medium","closest_history_reference":"old",
             "same_operation_archetype":True,"same_core_behavior_result":False,
             "same_defect_chain":False,"reason":"共用创建查询模式"}
    assert host.same_repository_overlap_feedback(generic, defer_unconfirmed_medium=True)==""
    assert "规则 C" in host.same_repository_overlap_feedback(generic)
    confirmed={**generic,"same_core_behavior_result":True,
               "reason":"用户目标与验收结果相同"}
    assert "#old" in host.same_repository_overlap_feedback(
        confirmed, defer_unconfirmed_medium=True)
    assert "#old" in host.same_repository_overlap_feedback(
        {**generic,"overlap_level":"high"}, defer_unconfirmed_medium=True)


def test_generation_author_assessment_is_not_final_difficulty_veto(host):
    analysis={**sample_complexity(),"routine_assessment":"有争议",
              "routine_path":"直接局部更新","counterexample":"并发交错导致旧结果覆盖"}
    assert host.difficulty_policy.generation_analysis_errors(analysis)==[]
    easy={**sample_difficulty(),"level":"中等","routine_solution_sufficient":True}
    assert host.difficulty_review_errors({"difficulty_review":easy})


def test_source_failure_policy_classifies_capacity_and_patch_separately(host):
    from pairwise import auto_refill
    assert auto_refill._source_failure_policy(host,host.UpstreamCapacityError("busy"))==(False,False)
    assert auto_refill._source_failure_policy(host,host.WorkflowError("预置缺陷补丁无法应用"))==(False,False)
    assert auto_refill._source_failure_policy(host,host.WorkflowError("困难度复核不足"))==(True,False)
    assert auto_refill._source_failure_policy(host,host.WorkflowError("本来源 Bug 题生成超过 25 分钟"))==(True,True)


@pytest.mark.parametrize("counts,available,expected",[
    ((9,0,0),(),"0-1 代码生成"),
    ((9,0,0),("Bug 修复",),"Bug 修复"),
    ((9,0,2),("Bug 修复",),"Bug 修复"),
    ((9,1,0),("Feature 迭代","Bug 修复"),"Bug 修复"),
    ((9,0,1),("Feature 迭代","Bug 修复"),"Feature 迭代"),
    ((0,1,1),("Feature 迭代","Bug 修复"),"0-1 代码生成"),
])
def test_refill_soft_ratio_selects_least_represented_available_type(host,monkeypatch,counts,available,expected):
    from pairwise import auto_refill as refill
    monkeypatch.setattr(refill,"continuation_source",lambda h,k,b:{"kind":k} if k in available else None)
    kind,source,_=refill.next_available_task(host,dict(zip(refill.BALANCED_TASK_TYPES,counts)),{})
    assert kind==expected
    assert bool(source)==(expected!="0-1 代码生成")


def test_refill_rotates_away_from_type_after_three_distinct_source_timeouts(host, monkeypatch):
    from pairwise import auto_refill as refill
    counts = dict(zip(refill.BALANCED_TASK_TYPES, (3, 1, 0)))
    monkeypatch.setattr(refill, "continuation_source",
                        lambda h, kind, blocked, **kwargs: {"kind": kind} if kind == "Bug 修复" else None)
    cooldowns = [{"task_type": "Bug 修复", "source_pair_id": f"legacy:{i}",
                  "cooldown_until": refill.time.time() + 1200,
                  "reason": "本来源 Bug 题生成超过 8 分钟，已停止并换来源"}
                 for i in range(3)]
    monkeypatch.setattr(refill, "source_cooldowns", lambda h: cooldowns)
    kind, source, unavailable = refill.next_available_task(host, counts, {})
    assert kind == "0-1 代码生成" and source is None
    assert any("连续多个来源出题超时" in reason for reason in unavailable)
    cooldowns.pop()
    kind, source, _ = refill.next_available_task(host, counts, {})
    assert kind == "Bug 修复" and source == {"kind": "Bug 修复"}


def test_refill_exclusions_and_rejections_survive_toggle_and_initialize(host):
    from pairwise import auto_refill as refill
    config={"enabled":True,"project_directory":str(host.PROJECTS_ROOT/"zzzz-gsb")}
    refill.configure(host,config)
    blocked={"Bug 修复:root-id":"重复的解析缺陷"}
    host.write_settings({"pairwise_auto_blocked_sources":json.dumps(blocked),
        "pairwise_auto_last_rejection":"重复的解析缺陷"})
    for enabled in (False,True):
        refill.configure(host,{**config,"enabled":enabled})
        refill.initialize(host)
        assert refill._blocked_sources(refill._values(host))==blocked
        assert refill.configuration(host)["last_rejection"]=="重复的解析缺陷"


def test_refill_reports_real_stage_time_and_ignores_stale_batch(host,monkeypatch):
    from pairwise import auto_refill as refill
    refill.configure(host,{"enabled":True})
    batch=refill._values(host)["pairwise_auto_batch"]
    monkeypatch.setattr(refill,"ACTIVE",True)
    monkeypatch.setattr(refill.time,"time",lambda:1000)
    refill._progress(host,batch,"Bug 修复","生成概念卡")
    monkeypatch.setattr(refill.time,"time",lambda:1060)
    refill._progress(host,batch,"Bug 修复","概念卡查重")
    monkeypatch.setattr(refill.time,"time",lambda:1080)
    # Repeated reports for the same stage must not reset its timer.
    refill._progress(host,batch,"Bug 修复","概念卡查重")
    config=refill.configuration(host)
    assert config["stage"]=="概念卡查重"
    assert config["stage_elapsed_seconds"]==20
    assert config["generation_elapsed_seconds"]==80
    refill._progress(host,batch,"Bug 修复","规则 C 未通过，重新查找缺陷",rejection="已存在相同缺陷")
    config=refill.configuration(host)
    assert config["rewrite_count"]==1 and config["last_rejection"]=="已存在相同缺陷"
    refill._progress(host,"old-batch","Feature 迭代","错误阶段",rejection="不应保存")
    assert refill.configuration(host)["stage"]==config["stage"]
    assert refill.configuration(host)["last_rejection"]==config["last_rejection"]


def test_prefill_reports_current_stage_and_total_time_separately(host, monkeypatch):
    from pairwise import auto_refill as refill
    refill.configure(host, {"enabled": True})
    monkeypatch.setattr(refill, "PREFILL_ACTIVE", True)
    monkeypatch.setattr(refill, "PREFILL_STAGE", "Bug 修复：正在定向重试（1/2）")
    monkeypatch.setattr(refill, "PREFILL_STARTED_AT", 900.0)
    monkeypatch.setattr(refill, "PREFILL_STAGE_STARTED_AT", 980.0)
    monkeypatch.setattr(refill.time, "monotonic", lambda: 1000.0)
    config = refill.configuration(host)
    assert config["prefill_elapsed_seconds"] == 100
    assert config["prefill_stage_elapsed_seconds"] == 20


def test_refill_configuration_reports_three_single_route_prefill_lanes(host, monkeypatch):
    from pairwise import auto_refill as refill
    refill.configure(host, {"enabled": True})
    monkeypatch.setattr(refill, "PREFILL_ACTIVE", True)
    monkeypatch.setattr(refill, "PREFILL_STAGE", "Bug 修复：核对候选 A")
    monkeypatch.setattr(refill, "PREFILL_STARTED_AT", 900.0)
    monkeypatch.setattr(refill, "PREFILL_STAGE_STARTED_AT", 980.0)
    monkeypatch.setattr(refill, "PREFILL_CREATED_AT", "2026-09-20T10:00:00+08:00")
    monkeypatch.setattr(refill, "PREFILL_PENDING_ID", "111111111111")
    monkeypatch.setattr(refill, "PREFILL_TASK_TYPE", "Bug 修复")
    monkeypatch.setattr(refill, "PREFILL_TITLE", "后台备题 1 · source-one")
    monkeypatch.setattr(refill, "PREFILL3_ACTIVE", True)
    monkeypatch.setattr(refill, "PREFILL3_STAGE", "0-1 代码生成：独立复核中")
    monkeypatch.setattr(refill, "PREFILL3_STARTED_AT", 950.0)
    monkeypatch.setattr(refill, "PREFILL3_STAGE_STARTED_AT", 990.0)
    monkeypatch.setattr(refill, "PREFILL3_CREATED_AT", "2026-09-20T10:01:00+08:00")
    monkeypatch.setattr(refill, "PREFILL3_PENDING_ID", "333333333333")
    monkeypatch.setattr(refill, "PREFILL3_TASK_TYPE", "0-1 代码生成")
    monkeypatch.setattr(refill, "PREFILL3_TITLE", "独立出题 3/3 · 新的 0-1 项目")
    monkeypatch.setattr(refill.time, "monotonic", lambda: 1000.0)
    config = refill.configuration(host)
    assert config["prefill_parallel"] == 3
    assert config["generation_race_lanes"] == 1
    assert [item["lane"] for item in config["prefill_lanes"]] == [1, 3]
    assert [item["pending_id"] for item in config["prefill_lanes"]] == ["111111111111", "333333333333"]
    assert [item["elapsed_seconds"] for item in config["prefill_lanes"]] == [100, 50]
    assert config["prefill_pending_id"] == "111111111111"
    assert config["prefill_generating"]


def test_refill_iteration_stage_callback_is_scoped_and_restored(host,monkeypatch):
    from pairwise import auto_refill as refill
    previous=mock.Mock()
    monkeypatch.setattr(host.CODEX_JOB_CONTEXT,"progress",previous,raising=False)
    def generate(**kwargs):
        host.update_current_iteration_job_stage("独立复核中",rejection="上一版难度不足")
        config=refill.configuration(host)
        assert config["prefill_stage"].endswith("独立复核中")
        raise host.WorkflowError("本次生成失败")
    monkeypatch.setattr(host,"generate_task_draft",generate)
    refill.configure(host,{"enabled":True})
    assert not refill._prefill_once(host)
    assert host.CODEX_JOB_CONTEXT.progress is previous
    previous.assert_not_called()


def test_refill_bootstrap_failure_stops_after_three_without_touching_sessions(host,monkeypatch):
    from pairwise import auto_refill as refill
    p=m.create(host,payload(task_type="0-1 代码生成",snapshot_url=""))
    w.start(host,p["id"])
    before=m.attempts(host,p["id"])
    host.set_max_parallel_runs({"max_parallel":4})
    def fail(**kwargs):
        raise host.WorkflowError("0-1 独立复核失败")
    monkeypatch.setattr(host,"generate_task_draft",fail)
    refill.configure(host,{"enabled":True})
    for _ in range(3):
        monkeypatch.setattr(refill,"PREFILL_NEXT_AT",0.0)
        assert not refill._prefill_once(host)
    config=refill.configuration(host)
    assert config["status"]=="needs_attention" and not config["enabled"]
    assert "0-1 独立复核失败" in config["last_rejection"]
    assert m.attempts(host,p["id"])==before


def test_refill_failed_directions_reach_next_generation_and_survive_resume(host,monkeypatch):
    from pairwise import auto_refill as refill
    seen=[]
    def fail(**kwargs):
        seen.append(kwargs["initial_feedback"])
        raise host.WorkflowError(f"候选复核与一次定向改写后仍不合规：方向 {len(seen)} 难度不足")
    monkeypatch.setattr(host,"generate_task_draft",fail)
    refill.configure(host,{"enabled":True})
    for _ in range(3):
        monkeypatch.setattr(refill,"PREFILL_NEXT_AT",0.0)
        assert not refill._prefill_once(host)
    assert "方向 1 难度不足" in seen[1]
    assert "方向 1 难度不足" in seen[2] and "方向 2 难度不足" in seen[2]
    assert all("禁止 CLI" in text for text in seen)
    assert not refill.configuration(host)["enabled"]
    refill.configure(host,{"enabled":True})
    assert not refill._prefill_once(host)
    assert all(f"方向 {n} 难度不足" in seen[3] for n in (1,2,3))
    history=refill._rejections(refill._values(host))
    assert len(history)==3 and "方向 4" in history[-1]["reason"]


def test_refill_continuation_feedback_is_scoped_to_type_and_root(host,monkeypatch):
    from pairwise import auto_refill as refill
    first,_,_=ready_pair(host,task_type="0-1 代码生成",snapshot_url="",repo_name="feedback-first")
    second,_,_=ready_pair(host,task_type="0-1 代码生成",snapshot_url="",repo_name="feedback-second")
    source={"pair":m.task(host,first)}
    refill.configure(host,{"enabled":True})
    batch=refill._values(host)["pairwise_auto_batch"]
    refill._generation_failed(host,batch,host.WorkflowError("普通状态表已经足够"),source,"Feature 迭代")
    values=refill._values(host)
    assert "普通状态表已经足够" in refill._retry_feedback(host,values,"Feature 迭代",source)
    assert not refill._retry_feedback(host,values,"Bug 修复",source)
    assert not refill._retry_feedback(host,values,"Feature 迭代",{"pair":m.task(host,second)})
    def clone(repo_url,path,sha):
        repository(path)
    monkeypatch.setattr(host,"clone_repository_snapshot",clone)
    source=refill.continuation_source(host,"Feature 迭代",{f"Feature 迭代:{second}":"other root"})
    assert source["pair"]["id"]==first
    generated=mock.Mock(return_value={"ok":True})
    monkeypatch.setattr(host,"_generate_iteration_candidate_for_context",generated)
    refill._generate_continuation(host,source,"Feature 迭代",lambda *args,**kwargs:None)
    context,kind,feedback=generated.call_args.args
    assert "普通状态表已经足够" in context["_concept_retry_feedback"]
    assert "普通状态表已经足够" in feedback and kind=="Feature 迭代"


def test_refill_paused_plan_detects_new_baseline_without_starting_work(host):
    from pairwise import auto_refill as refill
    refill.initialize(host)
    host.write_settings({"pairwise_auto_enabled":"0","pairwise_auto_status":"needs_attention",
                         "pairwise_auto_error":"旧基线不可用","pairwise_auto_failures":"3",
                         "pairwise_auto_dispatch_reason":"旧规则：根项目被永久排除"})
    assert refill.configuration(host)["recovery"]["task_type"]=="0-1 代码生成"
    assert refill.configuration(host)["dispatch_reason"]==""
    pair_id,_,_=ready_pair(host,task_type="0-1 代码生成",snapshot_url="",repo_name="new-qualified-root")
    before=m.attempts(host,pair_id)
    config=refill.configuration(host)
    assert config["recovery"]["source_id"]==pair_id
    assert config["recovery"]["task_type"]=="Feature 迭代"
    assert "当前已有可用基线" in config["recovery"]["detail"]
    assert not config["enabled"] and m.attempts(host,pair_id)==before


def pending_generation_config(**overrides):
    return {"generating":True,"pending_id":"abcdef012345",
        "pending_title":"新的 0-1 项目 · 题面生成中","next_task_type":"0-1 代码生成",
        "generation_started_at":"2026-09-17T12:00:00+08:00","stage":"独立复核中",
        "generation_elapsed_seconds":125,"project_directory":"zzzz-gsb",**overrides}


def test_task_list_shows_generation_without_polluting_real_tasks_or_quota(host):
    from pairwise import api, auto_refill
    result=api.tasks_page(host,{},pending_generation_config())
    assert result["total"]==1 and result["page_size"]==20
    item=result["items"][0]
    assert item["is_generating"] and item["state"]=="正在生成"
    assert item["difficulty"]=="待复核" and item["attempts"]==[]
    assert item["display_number"]=="待编号" and item["generation_stage"]=="独立复核中"
    assert item["created_at"]=="2026-09-17T12:00:00+08:00"
    assert item["generation_elapsed_seconds"]==125
    assert auto_refill.task_type_counts(host)==dict.fromkeys(auto_refill.BALANCED_TASK_TYPES,0)
    with host.db_connection() as db:
        assert db.execute("SELECT COUNT(*) FROM pairwise_tasks").fetchone()[0]==0
        assert db.execute("SELECT COUNT(*) FROM pairwise_attempts").fetchone()[0]==0


def test_task_list_generation_uses_backend_pagination_and_filtering(host):
    from pairwise import api
    for i in range(20):
        m.create(host,payload(title=f"existing {i}",repo_name=f"existing-{i}"))
    config=pending_generation_config()
    first=api.tasks_page(host,{},config)
    second=api.tasks_page(host,{"page":["2"]},config)
    assert first["total"]==second["total"]==21
    assert first["pages"]==2 and len(first["items"])==20 and len(second["items"])==1
    assert first["items"][0]["is_generating"]
    assert not second["items"][0].get("is_generating")
    assert len({i["id"] for i in first["items"]+second["items"]})==21
    found=api.tasks_page(host,{"query":["独立复核"],"page":["2"]},config)
    assert found["total"]==1 and found["page"]==1
    assert found["items"][0]["is_generating"]
    assert api.tasks_page(host,{"query":["unmatched"]},config)["items"]==[]
    existing=api.tasks_page(host,{"query":["existing"]},config)
    assert existing["total"]==20 and not any(i.get("is_generating") for i in existing["items"])


@pytest.mark.parametrize("hidden",[False,True])
def test_task_list_creation_replaces_pending_row_atomically(host,hidden):
    from pairwise import api
    config=pending_generation_config()
    pair=m.create(host,payload(),pair_id=config["pending_id"])
    if hidden:
        m.soft_delete(host,pair["id"])
    # Generator has not yet published the ready status. The committed Pair
    # (even when soft-deleted) must suppress its placeholder by the same ID.
    result=api.tasks_page(host,{},config)
    assert result["total"]==(0 if hidden else 1)
    assert not any(i.get("is_generating") for i in result["items"])
    if not hidden:
        assert result["items"][0]["id"]==config["pending_id"]
        assert result["items"][0]["title"]==pair["title"]


@pytest.mark.parametrize("overrides",[
    {"generating":False},{"pending_id":""},{"pending_id":"not-a-pair"}
])
def test_task_list_hides_stale_or_invalid_generation(host,overrides):
    from pairwise import api
    assert api.tasks_page(host,{},pending_generation_config(**overrides))["total"]==0


def test_refill_prefill_placeholder_is_replaced_by_created_pair(host,monkeypatch):
    from pairwise import auto_refill as refill, api
    seen=[]
    def generate(**kwargs):
        kwargs["progress"]("独立复核中")
        config=refill.configuration(host)
        row=api.tasks_page(host,{},config)["items"][0]
        seen.append(row["id"])
        assert row["created_at"] and row["is_generating"]
        return refill_root_draft()
    monkeypatch.setattr(host,"generate_task_draft",generate)
    refill.configure(host,{"enabled":True})
    assert refill._prefill_once(host)
    assert refill.run_once(host)
    result=api.tasks_page(host,{},refill.configuration(host))
    assert result["total"]==1 and result["items"][0]["id"]!=seen[0]
    assert not result["items"][0].get("is_generating")
    assert {a["side"] for a in result["items"][0]["attempts"]}=={"A","B"}


def test_no_duplicate_start_no_quality_reroll(host):
    p=m.create(host,payload())
    w.start(host,p["id"])
    a=m.latest(m.attempts(host,p["id"]),"A")
    with pytest.raises(app.WorkflowError,match="仍有执行"):
        w.start(host,p["id"],"A")
    with pytest.raises(app.WorkflowError,match="同时启动一次"):
        w.start(host,p["id"])
    m.update_attempt(host,a["id"],status="complete")
    with pytest.raises(app.WorkflowError,match="择优再跑"):
        w.start(host,p["id"],"A")


def test_completed_side_rerun_keeps_old_attempt_and_requires_fresh_gsb(host):
    pair_id, old_a, old_b = ready_pair(host)
    pair = m.task(host, pair_id)
    assert w.can_rerun_side(pair, m.attempts(host, pair_id), "B")
    assert m.latest(detail(host, pair_id, include_runtime=False)["attempts"], "B")["can_rerun"]
    with pytest.raises(app.WorkflowError, match="择优再跑"):
        w.start(host, pair_id, "B")

    fresh = w.rerun_side(host, pair_id, "B")
    assert fresh["side"] == "B" and fresh["number"] == 2 and fresh["status"] == "queued"
    assert Path(fresh["run_directory"]).name == "B-retry-02"
    assert m.latest(m.attempts(host, pair_id), "A")["id"] == old_a["id"]
    assert m.attempt(host, old_b["id"])["recording_path"] == old_b["recording_path"]
    after = m.task(host, pair_id)
    assert after["gsb"] == pair["gsb"] and after["reason"] == pair["reason"]
    assert after["review_source"] == "stale" and not after["human_confirmed"]
    assert not m.latest(detail(host, pair_id, include_runtime=False)["attempts"], "B")["can_rerun"]
    with pytest.raises(app.WorkflowError, match="不能单侧重跑"):
        w.rerun_side(host, pair_id, "B")

    complete(host, pair_id, "B")
    approve_difficulty(host, pair_id)
    assert any("旧的 A/B" in issue for issue in solo.quick_readiness(host, pair_id)[1])
    after = m.task(host, pair_id)
    difficulty_only = m.save_review(host, pair_id, {
        "submission_difficulty": "地狱", "revision": after["revision"]})
    assert difficulty_only["review_source"] == "stale"
    assert any("旧的 A/B" in issue for issue in v.preflight(host, pair_id)["issues"])
    refreshed = m.save_review(host, pair_id, {
        "gsb": difficulty_only["gsb"], "reason": difficulty_only["reason"],
        "revision": difficulty_only["revision"]})
    assert refreshed["review_source"] == m.current_review_source(m.attempts(host, pair_id))
    assert solo.quick_readiness(host, pair_id)[0]
    exported = solo.exports_page(host, {})["items"][0]
    assert exported["rerun"]["A"]["available"] and exported["rerun"]["B"]["available"]
    solo.record_state(host, {"pair_id": pair_id, "state": "submitting"})
    assert not solo.exports_page(host, {})["items"][0]["rerun"]["B"]["available"]
    with pytest.raises(app.WorkflowError, match="正在提交"):
        w.rerun_side(host, pair_id, "B")


def test_b_uses_frozen_model_config_and_new_directory(host,monkeypatch):
    p=m.create(host,payload())
    w.start(host,p["id"])
    a=m.latest(m.attempts(host,p["id"]),"A")
    b=m.latest(m.attempts(host,p["id"]),"B")
    monkeypatch.setattr(host,"current_model",lambda:"some-other-model")
    assert b["repo_path"]!=a["repo_path"]
    assert Path(a["repo_path"]).parent.name=="A"
    assert Path(b["repo_path"]).parent.name=="B"
    assert host.run_row(b["id"])["model"]==m.AUTO_MODEL
    assert host.run_row(b["id"])["first_prompt"]==p["prompt"]
    assert not Path(b["repo_path"]).exists()


def test_failed_reroll_retains_prior_attempt_and_requires_cleanup(host):
    p=m.create(host,payload())
    w.start(host,p["id"])
    a=m.latest(m.attempts(host,p["id"]),"A")
    m.update_attempt(host,a["id"],status="failed",error="API Error: 503")
    with pytest.raises(app.WorkflowError,match="尚未归档"):
        w.start(host,p["id"],"A")
    host.update_run(a["id"],container_cleaned=1)
    b=w.start(host,p["id"],"A")
    assert b["number"]==2 and b["id"]!=a["id"]
    assert m.attempts(host,p["id"])[0]["error"]=="API Error: 503"


@pytest.mark.parametrize("prompt_sent",[0,1])
def test_stop_without_trace_preserves_sent_session_only(host,monkeypatch,prompt_sent):
    from types import SimpleNamespace
    p=m.create(host,payload())
    w.start(host,p["id"])
    a=m.latest(m.attempts(host,p["id"]),"A")
    m.update_attempt(host,a["id"],status="running" if prompt_sent else "starting",prompt_sent=prompt_sent)
    calls=[]
    monkeypatch.setattr(host,"cancel_background_job",lambda *_:None)
    monkeypatch.setattr(host,"screen_session_running",lambda *_:False)
    monkeypatch.setattr(host,"run_command",lambda command,**_:calls.append(command) or SimpleNamespace(returncode=0))
    def no_trace(*_,**kwargs):raise host.WorkflowError("No JSONL")
    monkeypatch.setattr(host,"export_and_remove_container",no_trace)
    removed=[]
    monkeypatch.setattr(host,"remove_container_resources_without_trace",lambda row:removed.append(row["id"]))
    if prompt_sent:
        with pytest.raises(host.WorkflowError,match="现场已保留"):
            w.stop(host,a["id"])
        assert not removed
        assert ["docker","stop","--time","10",a["container_name"]] in calls
        assert not m.attempt(host,a["id"])["container_cleaned"]
    else:
        w.stop(host,a["id"])
        assert removed==[a["id"]]
        assert m.attempt(host,a["id"])["container_cleaned"]
    assert m.attempt(host,a["id"])["status"]=="stopped"


def test_unconfirmed_pair_side_retries_with_old_container_preserved(host, monkeypatch):
    pair = m.create(host, payload())
    w.start(host, pair["id"])
    a = m.latest(m.attempts(host, pair["id"]), "A")
    b = m.latest(m.attempts(host, pair["id"]), "B")
    old = (datetime.now().astimezone() - timedelta(minutes=8)).strftime(
        "%Y-%m-%d %H:%M:%S %z")
    m.update_attempt(host, a["id"], status="running", prompt_sent=1,
                     started_at=old, prompt_sent_at=old)
    host.update_run(a["id"], phase="first_running")
    with host.db_connection() as db:
        db.execute("UPDATE pairwise_tasks SET repo_url=?,base_sha=? WHERE id=?",
                   ("https://github.com/test/unconfirmed", "a" * 40, pair["id"]))
    scheduled = []
    running = {"value": True}
    commands = []

    def command(args, **_):
        commands.append(args)
        if args[:2] == ["docker", "stop"]:
            running["value"] = False
        return mock.Mock(returncode=0, stdout=str(running["value"]).lower(), stderr="")

    monkeypatch.setattr(host, "run_command", command)
    monkeypatch.setattr(host, "docker_container_running", lambda *_: running["value"])
    monkeypatch.setattr(host, "copy_container_traces", lambda _, dest: dest.mkdir(parents=True, exist_ok=True))
    monkeypatch.setattr(host, "cancel_background_job", lambda *_: None)
    monkeypatch.setattr(w, "enqueue", lambda _h, run_id: scheduled.append(run_id))

    retry = w.retry_unconfirmed(host, a["id"])

    old_attempt = m.attempt(host, a["id"])
    assert old_attempt["status"] == "failed" and not old_attempt["container_cleaned"]
    assert old_attempt["session_id"] is None and old_attempt["trace_path"] == ""
    assert ["docker", "stop", "--time", "10", a["container_name"]] in commands
    assert retry["side"] == "A" and retry["number"] == 2 and retry["status"] == "queued"
    assert retry["run_directory"].endswith("A-retry-02")
    assert scheduled == [retry["id"]]
    assert m.latest(m.attempts(host, pair["id"]), "B")["id"] == b["id"]
    assert host.run_row(retry["id"])["first_prompt"] == pair["prompt"]
    with pytest.raises(host.WorkflowError, match="更新的 Session"):
        w.retry_unconfirmed(host, a["id"])


def test_unconfirmed_retry_never_stops_a_session_with_jsonl(host, monkeypatch):
    pair = m.create(host, payload())
    w.start(host, pair["id"])
    a = m.latest(m.attempts(host, pair["id"]), "A")
    old = (datetime.now().astimezone() - timedelta(minutes=8)).strftime(
        "%Y-%m-%d %H:%M:%S %z")
    m.update_attempt(host, a["id"], status="running", prompt_sent=1,
                     started_at=old, prompt_sent_at=old)
    commands = []
    monkeypatch.setattr(host, "run_command", lambda args, **_: commands.append(args))

    def copy_trace(_, dest):
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "late.jsonl").write_text("late original trace\n")

    monkeypatch.setattr(host, "copy_container_traces", copy_trace)
    with pytest.raises(host.WorkflowError, match="已有 JSONL"):
        w.retry_unconfirmed(host, a["id"])
    assert commands == []
    assert m.attempt(host, a["id"])["status"] == "running"
    assert len([x for x in m.attempts(host, pair["id"]) if x["side"] == "A"]) == 1


def test_unconfirmed_retry_preserves_late_trace_without_queuing(host, monkeypatch):
    pair = m.create(host, payload())
    w.start(host, pair["id"])
    a = m.latest(m.attempts(host, pair["id"]), "A")
    old = (datetime.now().astimezone() - timedelta(minutes=8)).strftime(
        "%Y-%m-%d %H:%M:%S %z")
    m.update_attempt(host, a["id"], status="running", prompt_sent=1,
                     started_at=old, prompt_sent_at=old)
    running = {"value": True}
    copies = []
    monkeypatch.setattr(host, "cancel_background_job", lambda *_: None)
    monkeypatch.setattr(host, "docker_container_running", lambda *_: running["value"])

    def command(args, **_):
        if args[:2] == ["docker", "stop"]:
            running["value"] = False
        return mock.Mock(returncode=0, stdout=str(running["value"]).lower(), stderr="")

    def copy_trace(_, dest):
        dest.mkdir(parents=True, exist_ok=True)
        copies.append(dest)
        if len(copies) == 2:
            (dest / "late.jsonl").write_text("late original trace\n")

    monkeypatch.setattr(host, "run_command", command)
    monkeypatch.setattr(host, "copy_container_traces", copy_trace)
    with pytest.raises(host.WorkflowError, match="停止后发现原始 JSONL"):
        w.retry_unconfirmed(host, a["id"])
    assert len(copies) == 2 and not running["value"]
    assert m.attempt(host, a["id"])["status"] == "failed"
    assert (Path(a["run_directory"]) / "traces" / "late.jsonl").is_file()
    assert len([x for x in m.attempts(host, pair["id"]) if x["side"] == "A"]) == 1


def test_first_claude_bug_session_over_70_minutes_skips_both_sides(host, monkeypatch):
    pair = m.create(host, payload(task_type="Bug 修复"))
    w.start(host, pair["id"])
    attempts = m.attempts(host, pair["id"])
    a = m.latest(attempts, "A")
    b = m.latest(attempts, "B")
    old = (datetime.now().astimezone() - timedelta(minutes=71)).strftime(
        "%Y-%m-%d %H:%M:%S %z")
    m.update_attempt(host, a["id"], status="running", prompt_sent=1,
                     started_at=old, prompt_sent_at=old)
    m.update_attempt(host, b["id"], status="running", prompt_sent=1,
                     started_at=host.now_text(), prompt_sent_at=host.now_text())
    monkeypatch.setattr(host, "docker_container_running", lambda *_: True)
    monkeypatch.setattr(host, "refresh_trace_snapshot", lambda *_: (None, None))
    stopped = []
    monkeypatch.setattr(w, "stop", lambda _h, run_id: stopped.append(run_id))
    cancelled = []
    monkeypatch.setattr(host, "cancel_background_job", lambda key: cancelled.append(key))

    w.monitor(host, a["id"])

    assert set(stopped) == {a["id"], b["id"]}
    assert set(cancelled) == {f"run:{a['id']}", f"run:{b['id']}"}
    assert all(m.attempt(host, item["id"])["status"] == "stopped" for item in (a, b))
    assert "70 分钟" in m.task(host, pair["id"])["auto_skip_reason"]
    assert m.attempt(host, a["id"])["error"] == m.attempt(host, b["id"])["error"]


def test_bug_70_minute_rule_uses_prompt_time_and_first_sent_session_only(host, monkeypatch):
    pair = m.create(host, payload(task_type="Bug 修复"))
    w.start(host, pair["id"])
    a = m.latest(m.attempts(host, pair["id"]), "A")
    old = (datetime.now().astimezone() - timedelta(minutes=71)).strftime(
        "%Y-%m-%d %H:%M:%S %z")
    m.update_attempt(host, a["id"], status="running", prompt_sent=1,
                     started_at=old, prompt_sent_at=host.now_text())
    monkeypatch.setattr(host, "docker_container_running", lambda *_: True)
    monkeypatch.setattr(host, "refresh_trace_snapshot", lambda *_: (None, None))
    monkeypatch.setattr(host, "terminal_screen_text", lambda *_: "")
    monkeypatch.setattr(host, "first_turn_stop_reason", lambda *_: None)
    monkeypatch.setattr(w, "skip_overlong_bug_pair", lambda *_: pytest.fail("too early"))
    class EndPoll(Exception):
        pass
    monkeypatch.setattr(w.time, "sleep", lambda *_: (_ for _ in ()).throw(EndPoll))
    with pytest.raises(EndPoll):
        w.monitor(host, a["id"])

    # A second real Claude Session is governed by the existing general limits.
    m.update_attempt(host, a["id"], status="failed")
    host.update_run(a["id"], container_cleaned=1)
    retry = w.start(host, pair["id"], "A")
    m.update_attempt(host, retry["id"], status="running", prompt_sent=1,
                     started_at=old, prompt_sent_at=old)
    with pytest.raises(EndPoll):
        w.monitor(host, retry["id"])
    assert not m.task(host, pair["id"])["auto_skip_reason"]


def test_bug_timeout_does_not_stop_archived_sibling_or_auto_retry(host, monkeypatch):
    pair = m.create(host, payload(task_type="Bug 修复"))
    w.start(host, pair["id"])
    a = m.latest(m.attempts(host, pair["id"]), "A")
    b = m.latest(m.attempts(host, pair["id"]), "B")
    old = (datetime.now().astimezone() - timedelta(minutes=71)).strftime(
        "%Y-%m-%d %H:%M:%S %z")
    m.update_attempt(host, a["id"], status="running", prompt_sent=1, prompt_sent_at=old)
    m.update_attempt(host, b["id"], status="complete", prompt_sent=1)
    stopped = []
    monkeypatch.setattr(w, "stop", lambda _h, run_id: stopped.append(run_id))
    monkeypatch.setattr(host, "cancel_background_job", lambda *_: None)

    w.skip_overlong_bug_pair(host, a["id"])

    assert stopped == [a["id"]]
    assert m.attempt(host, b["id"])["status"] == "complete"
    assert detail(host, pair["id"], include_runtime=False)["state"] == "已跳过"
    m.update_attempt(host, a["id"], status="failed")
    host.update_run(a["id"], container_cleaned=1)
    assert w._create_automatic_retry(host, m.attempt(host, a["id"])) is None
    assert len(m.attempts(host, pair["id"])) == 2
    # Explicit manual restart is still available; it starts a new round.
    w.start(host, pair["id"], "A")
    assert not m.task(host, pair["id"])["auto_skip_reason"]


@pytest.mark.parametrize("change",["continue","retry","another_input","mixed_tool_text","prompt","session","api","api_text","empty_final","completion","version"])
def test_raw_trace_strict_single_turn(host,tmp_path,change):
    p={**payload(),"config":config()}
    path=tmp_path/"s.jsonl"
    trace(path,p["prompt"],"s")
    events=[json.loads(x) for x in path.read_text().splitlines()]
    if change=="continue":events.append({"type":"user","sessionId":"s","message":{"content":"继续"}})
    if change=="retry":events.append({"type":"user","sessionId":"s","message":{"content":"重试"}})
    if change=="another_input":events.append({"type":"user","sessionId":"s","message":{"content":"请修一下测试"}})
    if change=="mixed_tool_text":events.append({"type":"user","sessionId":"s","message":{"content":[
        {"type":"tool_result","content":"ok"},{"type":"text","text":"继续"}]}})
    if change=="prompt":events[0]["message"]["content"]=p["prompt"].strip()
    if change=="session":events[0]["sessionId"]="other"
    if change=="api":events.append({"type":"assistant","isApiErrorMessage":True})
    if change=="api_text":events[1]["message"]["content"][0]["text"]="API Error: 503"
    if change=="empty_final":events[1]["message"]["content"]=[]
    if change=="completion":events.pop()
    if change=="version":events[0]["version"]="9.9.9"
    path.write_text("\n".join(json.dumps(e) for e in events))
    assert v.trace_issues(host,path,p,"s")


def test_http_pair_routes_and_legacy_write_protection(host,monkeypatch):
    from http.client import HTTPConnection
    import threading
    monkeypatch.setattr(host,"PAIRWISE_MODE",True)
    server=host.ThreadingHTTPServer(("127.0.0.1",0),host.ApiHandler)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    def request(method,path,body=None):
        connection=HTTPConnection("127.0.0.1",server.server_port,timeout=10)
        connection.request(method,path,json.dumps(body) if body is not None else None,
                           {"Content-Type":"application/json"})
        response=connection.getresponse()
        result=response.status,response.read(),response.getheader("Content-Type")
        connection.close()
        return result
    try:
        status,body,_=request("GET","/")
        assert status==200 and b"pairwise.js" in body
        assert b'pairwise-max-parallel' in body and b'auto-toggle' not in body
        status,body,_=request("GET","/api/pairwise/auto-refill")
        assert status==404 and "接口不存在" in body.decode()
        status,body,_=request("POST","/api/pairwise/auto-refill",{"enabled":True})
        assert status==400 and "接口不存在" in body.decode()
        status,body,_=request("POST","/api/pairwise/max-parallel",{"max_parallel":1})
        assert status>=400 and "2 至 6" in body.decode()
        status,body,_=request("POST","/api/pairwise/max-parallel",{"max_parallel":3})
        assert status==200 and json.loads(body)["max_parallel"]==3
        status,body,_=request("POST","/api/pairwise",payload())
        assert status==201
        p=json.loads(body)
        assert p["prompt"]==payload()["prompt"]
        status,body,_=request("GET","/api/pairwise?page=1")
        listing=json.loads(body)
        assert status==200 and listing["total"]==1
        assert listing["runtime"]["app_version"]==host.APP_VERSION
        assert listing["pages"]==1
        status,body,_=request("POST",f"/api/pairwise/{p['id']}/start",{})
        assert status==200 and "credential" not in json.loads(body)["config"]
        assert {a["side"] for a in json.loads(body)["attempts"]}=={"A","B"}
        run_id=json.loads(body)["attempts"][0]["id"]
        status,body,_=request("POST",f"/api/pairwise/{p['id']}/delete",{})
        assert status>=400 and "仍在执行" in body.decode()
        status,body,_=request("POST","/api/pairwise",payload(title="delete me",repo_name="delete-me"))
        hidden=json.loads(body)
        status,body,_=request("POST",f"/api/pairwise/{hidden['id']}/delete",{})
        assert status==200 and json.loads(body)["ok"]
        status,body,_=request("GET","/api/pairwise?page=1")
        assert status==200 and json.loads(body)["total"]==1
        status,body,_=request("GET",f"/api/pairwise/{hidden['id']}")
        assert status==200 and json.loads(body)["deleted_at"]
        for index in range(20):
            status,_,_=request("POST","/api/pairwise",payload(
                title=f"page item {index}",repo_name=f"page-item-{index}"
            ))
            assert status==201
        status,body,_=request("GET","/api/pairwise?page=1")
        first_page=json.loads(body)
        assert first_page["total"]==21 and first_page["pages"]==2 and len(first_page["items"])==20
        status,body,_=request("GET","/api/pairwise?page=2")
        assert status==200 and len(json.loads(body)["items"])==1
        for path in ("/api/runs","/api/runs/auto","/api/task-draft","/api/settings/model"):
            status,body,_=request("POST",path,{})
            assert status>=400 and "只读归档" in body.decode()
        status,_,_=request("DELETE",f"/api/runs/{run_id}")
        assert status>=400
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5)


def test_recording_file_opens_inline_and_supports_video_ranges(host,monkeypatch):
    from http.client import HTTPConnection
    import threading

    _, a, _ = ready_pair(host)
    monkeypatch.setattr(host,"PAIRWISE_MODE",True)
    server=host.ThreadingHTTPServer(("127.0.0.1",0),host.ApiHandler)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    try:
        connection=HTTPConnection("127.0.0.1",server.server_port,timeout=10)
        connection.request(
            "GET",f"/api/pairwise/attempts/{a['id']}/recording-file",
            headers={"Range":"bytes=0-3"},
        )
        response=connection.getresponse()
        body=response.read()
        assert response.status==206
        assert response.getheader("Content-Type")=="video/mp4"
        assert response.getheader("Content-Disposition").startswith("inline;")
        assert response.getheader("Accept-Ranges")=="bytes"
        assert response.getheader("Content-Range")==f"bytes 0-3/{Path(a['recording_path']).stat().st_size}"
        assert body==Path(a["recording_path"]).read_bytes()[:4]
        connection.close()
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5)


def test_tool_results_are_not_user_interactions(host,tmp_path):
    p={**payload(),"config":config()}
    path=tmp_path/"s.jsonl"
    trace(path,p["prompt"],"s",[{"type":"user","sessionId":"s","message":{"content":[{"type":"tool_result","content":"test"}]}}])
    assert v.trace_issues(host,path,p,"s")==[]


def test_live_trace_ignores_only_unterminated_trailing_json(host,tmp_path):
    p={**payload(),"config":config()}
    path=tmp_path/"s.jsonl"
    trace(path,p["prompt"],"s")
    with path.open("a") as destination:
        destination.write('\n{"type":"assistant"')
    assert v.trace_issues(host,path,p,"s",require_complete=False)==[]
    assert "无法解析" in "；".join(v.trace_issues(host,path,p,"s"))


def test_live_trace_keeps_newline_terminated_parse_error_strict(host,tmp_path):
    p={**payload(),"config":config()}
    path=tmp_path/"s.jsonl"
    trace(path,p["prompt"],"s")
    with path.open("a") as destination:
        destination.write('\n{"type":"assistant"\n')
    assert "无法解析" in "；".join(
        v.trace_issues(host,path,p,"s",require_complete=False)
    )


def test_preflight_and_export_complete_pair(host):
    pair_id,a,b=ready_pair(host)
    result=v.preflight(host,pair_id)
    assert result["eligible"], result["issues"]
    row=ex.row(host,pair_id)
    assert len(row)==len(ex.COLUMNS)==26
    assert row[0]==payload()["prompt"]
    assert row[9]==a["session_id"] and row[16]==b["session_id"]
    assert row[11].endswith(a["artifact_sha"])
    assert row[7]=="无外部依赖" and row[13]==5 and row[20]==5 and row[23]=="Same"
    assert row[15]==row[22]==m.AUTO_MODEL
    assert "credential" not in detail(host,pair_id)["config"]


def test_new_zero_one_submission_allows_artifact_above_generation_line_budget(host):
    prompt = "代码从空仓库起步，围绕一个困难下沿的业务闭环实现服务，并通过 Docker Compose 完成本地验收。"
    pair_id, a, _ = ready_pair(
        host, task_type="0-1 代码生成", snapshot_url="", prompt=prompt,
        difficulty_preflight={
            "generation_policy_version": generation_policy.VERSION,
            "handwritten_line_limit": generation_policy.ZERO_ONE_HANDWRITTEN_LINE_LIMIT,
        },
    )
    workspace = Path(a["repo_path"])
    (workspace / "large.py").write_text("value = 1\n" * 2501)
    (workspace / "package-lock.json").write_text("{}\n" * 4000)
    (workspace / "dist").mkdir()
    (workspace / "dist" / "bundle.js").write_text("x();\n" * 4000)
    (workspace / ".venv" / "lib").mkdir(parents=True)
    (workspace / ".venv" / "lib" / "dependency.py").write_text("generated = 1\n" * 4000)
    git(workspace, "add", ".")
    tree = git(workspace, "write-tree")
    artifact = git(
        workspace, "commit-tree", tree, "-p", m.task(host, pair_id)["base_sha"],
        "-m", "oversized artifact",
    )
    m.update_attempt(host, a["id"], artifact_sha=artifact, artifact_tree=tree)
    approve_difficulty(host, pair_id)

    result = v.preflight(host, pair_id)

    assert result["eligible"], result["issues"]
    assert solo.bundle(host, pair_id)["id"] == pair_id


def test_pairwise_export_list_is_backend_paginated_and_keeps_submission_state(host,monkeypatch):
    first,_,_=ready_pair(host)
    second,_,_=ready_pair(host,title="second pair",repo_name="second-pair")
    monkeypatch.setattr(solo,"PAGE_SIZE",1)
    page=solo.exports_page(host,{"page":["1"],"query":[""]})
    assert page["total"]==2 and page["pages"]==2 and page["page_size"]==1
    assert len(page["items"])==1 and page["items"][0]["export_ready"]
    assert set(page["items"][0]["recording"])=={"A","B"}
    assert page["items"][0]["solo_qa"]["state"]=="not_submitted"
    assert page["items"][0]["display_number"]
    assert page["items"][0]["prompt"]==payload()["prompt"]
    assert page["items"][0]["gsb"]=="Same"
    assert page["items"][0]["reason"]
    assert page["items"][0]["human_confirmed"] is True
    assert isinstance(page["items"][0]["revision"],int)
    assert page["items"][0]["created_at"]
    other=solo.exports_page(host,{"page":["2"],"query":[""]})
    assert {page["items"][0]["id"],other["items"][0]["id"]}=={first,second}


def test_export_list_filters_number_type_difficulty_readiness_recording_and_solo(host):
    first,_,_=ready_pair(host)
    second,second_a,_=ready_pair(host,title="bug pair",repo_name="bug-pair",task_type="Bug 修复")
    approve_difficulty(host, second, "地狱")
    m.update_attempt(host, second_a["id"], recording_status="failed", recording_path="",
                     recording_confirmed=0, recording_error="fixture failure")
    solo.record_state(host,{"pair_id":second,"state":"needs_fix","remote_id":"99"})
    labels=m.display_numbers(host)

    assert [item["id"] for item in solo.exports_page(host,{"number":[labels[first]]})["items"]] == [first]
    assert [item["id"] for item in solo.exports_page(host,{"task_type":["Bug 修复"]})["items"]] == [second]
    assert [item["id"] for item in solo.exports_page(host,{"difficulty":["地狱"]})["items"]] == [second]
    assert [item["id"] for item in solo.exports_page(host,{"readiness":["ready"]})["items"]] == [first]
    assert [item["id"] for item in solo.exports_page(host,{"readiness":["blocked"]})["items"]] == [second]
    assert [item["id"] for item in solo.exports_page(host,{"recording":["complete"]})["items"]] == [first]
    assert [item["id"] for item in solo.exports_page(host,{"recording":["failed"]})["items"]] == [second]
    assert [item["id"] for item in solo.exports_page(host,{"solo":["needs_fix"]})["items"]] == [second]


def test_export_filters_cover_every_type_and_difficulty_option(host):
    pair_id, _, _ = ready_pair(host, repo_name="filter-options")
    for task_type in m.TASK_TYPES:
        with host.db_connection() as db:
            db.execute("UPDATE pairwise_tasks SET task_type=? WHERE id=?", (task_type, pair_id))
        result = solo.exports_page(host, {"task_type": [task_type]})
        assert result["total"] == 1
        assert result["items"][0]["id"] == pair_id
        assert result["items"][0]["task_type"] == task_type
    for level in ("简单", "中等", "困难", "地狱"):
        approve_difficulty(host, pair_id, level)
        result = solo.exports_page(host, {"difficulty": [level]})
        assert result["total"] == 1
        assert result["items"][0]["difficulty_assessment"]["level"] == level


def test_export_filters_cover_all_recording_states(host):
    pair_id, a, b = ready_pair(host)
    cases = [
        ("queued", True, "complete", True, "processing"),
        ("preparing", True, "failed", True, "processing"),
        ("recording", False, "complete", True, "processing"),
        ("complete", True, "complete", True, "complete"),
        ("failed", True, "complete", True, "failed"),
        ("complete", True, "", False, "partial"),
        ("", False, "", False, "missing"),
    ]
    for a_status, a_file, b_status, b_file, expected in cases:
        m.update_attempt(host, a["id"], recording_status=a_status,
                         recording_path=a["recording_path"] if a_file else "")
        m.update_attempt(host, b["id"], recording_status=b_status,
                         recording_path=b["recording_path"] if b_file else "")
        result = solo.exports_page(host, {"recording": [expected]})
        assert result["total"] == 1
        assert result["items"][0]["id"] == pair_id
        other = "complete" if expected != "complete" else "missing"
        assert solo.exports_page(host, {"recording": [other]})["total"] == 0


def test_export_filters_cover_all_solo_states_including_local_changes(host):
    pair_id, _, _ = ready_pair(host)
    for state in sorted(solo.LOCAL_STATES):
        solo.record_state(host, {"pair_id": pair_id, "state": state})
        result = solo.exports_page(host, {"solo": [state]})
        assert result["total"] == 1
        assert result["items"][0]["solo_qa"]["state"] == state
    solo.record_state(host, {"pair_id": pair_id, "state": "qc_pending", "remote_id": "123",
                            "payload_sha256": solo.payload_sha256(host, pair_id)})
    pair = m.task(host, pair_id)
    m.save_review(host, pair_id, {"reason": pair["reason"] + "本地评价已更新。",
                                "revision": pair["revision"]})
    result = solo.exports_page(host, {"solo": ["local_changed"]})
    assert result["total"] == 1
    assert result["items"][0]["solo_qa"]["state"] == "local_changed"
    assert solo.exports_page(host, {"solo": ["qc_pending"]})["total"] == 0


def test_export_list_filters_latest_pair_completion_date_before_pagination(host, monkeypatch):
    first, first_a, first_b = ready_pair(host)
    second, second_a, second_b = ready_pair(host, title="next", repo_name="next")
    m.update_attempt(host, first_a["id"], finished_at="2026-09-16 23:59:59 +0800")
    m.update_attempt(host, first_b["id"], finished_at="2026-09-17 00:00:00 +0800")
    m.update_attempt(host, second_a["id"], finished_at="2026-09-18 08:00:00 +0800")
    m.update_attempt(host, second_b["id"], finished_at="2026-09-18 09:00:00 +0800")
    monkeypatch.setattr(solo, "PAGE_SIZE", 1)
    result = solo.exports_page(host, {"date_start": ["2026-09-17"],
                                      "date_end": ["2026-09-17"], "page": ["2"]})
    assert result["total"] == 1 and result["page"] == 1
    assert [item["id"] for item in result["items"]] == [first]
    assert solo.exports_page(host, {"date_start": ["2026-09-18"]})["total"] == 1
    assert solo.exports_page(host, {"date_end": ["2026-09-16"]})["total"] == 0
    with pytest.raises(host.WorkflowError, match="开始日期不能晚于结束日期"):
        solo.exports_page(host, {"date_start": ["2026-09-18"], "date_end": ["2026-09-17"]})


def test_export_soft_delete_preserves_evidence_and_clamps_last_page(host,monkeypatch):
    first,a,b=ready_pair(host)
    second,_,_=ready_pair(host,title="remaining pair",repo_name="remaining-pair")
    monkeypatch.setattr(solo,"PAGE_SIZE",1)
    before=m.attempts(host,first)
    original=m.task(host,first)
    m.soft_delete(host,first)
    page=solo.exports_page(host,{"page":["2"]})
    assert page["total"]==1 and page["pages"]==1 and page["page"]==1
    assert [item["id"] for item in page["items"]]==[second]
    assert solo.exports_page(host,{"query":[original["repo_name"]]})["total"]==0
    assert m.attempts(host,first)==before
    assert m.task(host,first)["prompt"]==original["prompt"]
    assert m.task(host,first)["reason"]==original["reason"]
    for attempt in (a,b):
        assert Path(attempt["trace_path"]).is_file()


def test_export_order_uses_latest_pair_completion_not_edits(host,monkeypatch):
    older,old_a,old_b=ready_pair(host)
    newer,new_a,new_b=ready_pair(host,title="newer",repo_name="newer-pair")
    unknown,_,_=ready_pair(host,title="legacy",repo_name="legacy-pair")
    m.update_attempt(host,old_a["id"],finished_at="2026-09-17 09:00:00 +0800")
    m.update_attempt(host,old_b["id"],finished_at="2026-09-17 10:00:00 +0800")
    # A ends last; the UTC spelling must compare by instant rather than text.
    m.update_attempt(host,new_a["id"],finished_at="2026-09-17T03:00:00Z")
    m.update_attempt(host,new_b["id"],finished_at="2026-09-17 10:30:00 +0800")
    pair=m.task(host,older)
    with host.db_connection() as db:
        prior=w._insert_attempt(db,host,pair,"A",0,pair["config"],"test",
                               host.DATA_DIR / "prior",host.now_text())
    # Old retry history must not participate, even with a later timestamp.
    m.update_attempt(host,prior,status="complete",finished_at="2030-01-01 00:00:00 +0800")
    monkeypatch.setattr(solo,"PAGE_SIZE",1)
    def ordered():
        return [solo.exports_page(host,{"page":[str(n)]})["items"][0] for n in (1,2,3)]
    rows=ordered()
    assert [x["id"] for x in rows]==[newer,older,unknown]
    assert rows[0]["finished_at"]=="2026-09-17T03:00:00Z"
    assert rows[-1]["finished_at"]==""  # never fall back to mutable run.updated_at
    m.save_review(host,older,{"gsb":"B 更好","reason":pair["reason"]+" B 的表达更清楚。",
                             "human_confirmed":True,"revision":pair["revision"]})
    m.update_attempt(host,old_a["id"],recording_finished_at="2031-01-01 00:00:00 +0800")
    solo.record_state(host,{"pair_id":older,"state":"failed","error":"test"})
    with host.db_connection() as db:
        db.execute("UPDATE pairwise_tasks SET updated_at=? WHERE id=?",("2032-01-01 00:00:00 +0800",older))
        db.execute("UPDATE runs SET updated_at=? WHERE id=?",("2032-01-01 00:00:00 +0800",old_a["id"]))
    assert [x["id"] for x in ordered()]==[newer,older,unknown]
    assert solo.exports_page(host,{"query":["newer"]})["items"][0]["id"]==newer
    # A new incomplete latest attempt removes the pair from exports.
    with host.db_connection() as db:
        w._insert_attempt(db,host,pair,"A",2,pair["config"],"test",
                          host.DATA_DIR / "next",host.now_text())
    assert solo.exports_page(host,{})["total"]==2


def test_export_completion_ties_are_stable_across_pages(host,monkeypatch):
    first,a,b=ready_pair(host)
    second,c,d=ready_pair(host,title="second",repo_name="second-pair")
    for attempt in (a,b,c,d):
        m.update_attempt(host,attempt["id"],finished_at="2026-09-17 10:00:00 +0800")
    with host.db_connection() as db:
        db.execute("UPDATE pairwise_tasks SET created_at='2026-09-16 10:00:00 +0800'")
    monkeypatch.setattr(solo,"PAGE_SIZE",1)
    expected=sorted([first,second],reverse=True)
    for _ in range(2):
        assert [solo.exports_page(host,{"page":[str(n)]})["items"][0]["id"]
                for n in (1,2)]==expected


def test_solo_gsb_bundle_contains_two_verified_trajectories_and_detects_local_change(host):
    pair_id,a,b=ready_pair(host)
    bundle=solo.bundle(host,pair_id)
    assert bundle["values"]["question_type"]=="feature迭代"
    assert bundle["values"]["os_platform"]=="MacOS/Linux"
    assert bundle["values"]["repro_level"]=="无外部依赖"
    assert bundle["values"]["validity"]=="有效"
    assert bundle["values"]["a_delivery_score"]==5
    assert bundle["values"]["b_delivery_score"]==5
    assert bundle["models"]=={"A":m.AUTO_MODEL,"B":m.AUTO_MODEL}
    assert "逐项核对" in bundle["values"]["A-交付完整性描述"]
    assert bundle["trajectories"]["A"]["sha256"]==a["trace_sha256"]
    assert bundle["trajectories"]["B"]["sha256"]==b["trace_sha256"]
    recorded=solo.record_state(host,{"pair_id":pair_id,"state":"qc_pending",
        "remote_id":"812","remote_status":"SUBMITTED",
        "payload_sha256":bundle["payload_sha256"]})
    assert recorded["remote_id"]=="812" and recorded["state"]=="qc_pending"
    pair=m.task(host,pair_id)
    updated_reason=pair["reason"]+" A 的错误处理更清楚。"
    m.save_review(host,pair_id,{"gsb":"A 更好","reason":updated_reason,
        "human_confirmed":True,"revision":pair["revision"]})
    assert solo.summary(host,pair_id)["state"]=="local_changed"
    updated=solo.bundle(host,pair_id)
    assert updated["values"]["gsb_verdict"]=="A 更好"
    assert updated["values"]["gsb_reason"]==updated_reason


def test_export_model_display_uses_actual_session_and_rejects_override(host, monkeypatch):
    pair_id, _, _ = ready_pair(host)
    pair = m.task(host, pair_id)
    with pytest.raises(host.WorkflowError, match="不能手动修改"):
        m.save_review(host, pair_id, {"b_model_name": "ark/urm-03", "revision": pair["revision"]})
    b = m.latest(m.attempts(host, pair_id), "B")
    trace_path = Path(b["trace_path"])
    records = [json.loads(line) for line in trace_path.read_text().splitlines()]
    next(record for record in records if record["type"] == "assistant")["message"]["model"] = "ark/urm-03"
    trace_path.write_text("".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records))
    m.update_attempt(host, b["id"], trace_sha256=hashlib.sha256(trace_path.read_bytes()).hexdigest())
    assert m.actual_model_name(m.attempt(host, b["id"])) == "ark/urm-03"
    item = next(x for x in solo.exports_page(host, {})["items"] if x["id"] == pair_id)
    monkeypatch.setattr(solo, "preflight", lambda *_: {"eligible": True, "issues": []})
    monkeypatch.setattr(ex, "preflight", lambda *_: {"eligible": True, "issues": [],
                                                       "submission_difficulty": "困难"})
    bundle = solo.bundle(host, pair_id)
    assert (item["a_model_name"], item["b_model_name"]) == (m.AUTO_MODEL, "ark/urm-03")
    assert bundle["models"] == {"A": m.AUTO_MODEL, "B": "ark/urm-03"}
    assert ex.row(host, pair_id)[15:23:7] == [m.AUTO_MODEL, "ark/urm-03"]


def test_b_model_policy_changes_only_future_or_queued_b_attempt(host):
    pair = m.create(host, payload(b_execution_model="ark/urm-03"))
    assert pair["b_execution_model"] == "ark/urm-03"
    with host.db_connection() as db:
        a_id = w._insert_attempt(db, host, pair, "A", 1, config(), "test",
                                 host.DATA_DIR / "model-a", host.now_text())
        b_id = w._insert_attempt(db, host, pair, "B", 1, config(), "test",
                                 host.DATA_DIR / "model-b", host.now_text())
    assert m.attempt(host, a_id)["model"] == m.AUTO_MODEL
    assert m.attempt(host, b_id)["model"] == "ark/urm-03"
    changed = m.set_b_execution_model(host, pair["id"], m.AUTO_MODEL, pair["revision"])
    assert changed["b_execution_model"] == m.AUTO_MODEL
    assert m.attempt(host, b_id)["model"] == m.AUTO_MODEL
    with host.db_connection() as db:
        assert db.execute("SELECT model FROM run_turns WHERE run_id=?", (b_id,)).fetchone()[0] == m.AUTO_MODEL
    m.update_attempt(host, b_id, status="running", prompt_sent=1)
    with pytest.raises(host.WorkflowError, match="B 已开始执行"):
        m.set_b_execution_model(host, pair["id"], "ark/urm-03", changed["revision"])


def test_ark_b_container_differs_only_in_modelname(host, monkeypatch):
    from types import SimpleNamespace
    base = {"version": "2.1.197 (Claude Code)", "model": m.AUTO_MODEL,
            "context_tokens": 1_000_000,
            "env": {"ANTHROPIC_MODEL": m.AUTO_MODEL,
                    "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "1000000"},
            "files": {"/usr/local/bin/entrypoint.sh": "same"},
            "platform": "linux", "arch": "arm64"}
    frozen = {**config(), "runtime": env.normalize(host, json.dumps(base))}
    actual = json.loads(json.dumps(base))
    actual["model"] = actual["env"]["ANTHROPIC_MODEL"] = "ark/urm-03"
    monkeypatch.setattr(host, "run_command", lambda command, **_: SimpleNamespace(
        stdout=frozen["image"] + "\n" if command[:2] == ["docker", "inspect"] else json.dumps(actual)))
    result = env.verify_container(host, "pair-test", frozen, "ark/urm-03")
    assert result["fingerprint"] == env.digest(frozen)
    with pytest.raises(host.WorkflowError, match="modelname"):
        env.verify_container(host, "pair-test", frozen, m.AUTO_MODEL)


def test_submission_accepts_same_model_pair_and_snapshots_submitted_models(host):
    standard, _, _ = ready_pair(host)
    assert solo.bundle(host, standard)["id"] == standard
    mixed, _, b = ready_pair(host, title="mixed", repo_name="mixed-pair")
    with host.db_connection() as db:
        db.execute("UPDATE pairwise_tasks SET b_execution_model='ark/urm-03' WHERE id=?", (mixed,))
        db.execute("UPDATE runs SET model='ark/urm-03' WHERE id=?", (b["id"],))
    path = Path(b["trace_path"])
    records = [json.loads(line) for line in path.read_text().splitlines()]
    next(row for row in records if row["type"] == "assistant")["message"]["model"] = "ark/urm-03"
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in records))
    m.update_attempt(host, b["id"], trace_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    approve_difficulty(host, mixed)
    assert solo.bundle(host, mixed)["models"] == {"A": m.AUTO_MODEL, "B": "ark/urm-03"}
    solo.record_state(host, {"pair_id": mixed, "state": "qc_pending", "remote_id": "mixed-1",
                             "submitted_at": host.now_text()})
    with host.db_connection() as db:
        submitted = db.execute("SELECT a_model_name,b_model_name FROM pairwise_solo_submissions WHERE pair_id=?",
                               (mixed,)).fetchone()
    assert tuple(submitted) == (m.AUTO_MODEL, "ark/urm-03")
    assert solo.bundle(host, standard)["id"] == standard
    # Later local reruns cannot rewrite the model recorded for an earlier remote submission.
    with host.db_connection() as db:
        db.execute("UPDATE runs SET model=? WHERE id=?", (m.AUTO_MODEL, b["id"]))
    solo.record_state(host, {"pair_id": mixed, "state": "qc_pending", "remote_id": "mixed-1",
                             "submitted_at": host.now_text(), "synced": True})
    with host.db_connection() as db:
        submitted = db.execute("SELECT a_model_name,b_model_name FROM pairwise_solo_submissions WHERE pair_id=?",
                               (mixed,)).fetchone()
    assert tuple(submitted) == (m.AUTO_MODEL, "ark/urm-03")


def test_bundle_allows_same_model_after_two_standard_submissions(host):
    for index in range(2):
        pair_id, _, _ = ready_pair(
            host, title=f"standard {index}", repo_name=f"standard-{index}")
        solo.record_state(host, {
            "pair_id": pair_id, "state": "qc_pending",
            "remote_id": f"standard-remote-{index}", "submitted_at": host.now_text(),
        })
    next_pair, _, _ = ready_pair(
        host, title="next standard", repo_name="next-standard")
    assert solo.bundle(host, next_pair)["models"] == {
        "A": m.AUTO_MODEL, "B": m.AUTO_MODEL}


def test_solo_gsb_infers_containerized_level_for_legacy_environment_description(host):
    pair_id,_,_=ready_pair(
        host,
        language_framework="Go, Docker Compose",
        reproducibility="已容器化，可一键起环境",
    )
    with host.db_connection() as db:
        db.execute(
            "UPDATE pairwise_tasks SET reproducibility=? WHERE id=?",
            ("全新空 README 基线；A/B 从同一 main SHA 启动", pair_id),
        )
    assert solo.bundle(host,pair_id)["values"]["repro_level"]=="已容器化，可一键起环境"


def test_preflight_rejects_any_second_database_turn(host):
    pair_id,a,_=ready_pair(host)
    with host.db_connection() as db:
        db.execute("""INSERT INTO run_turns(run_id,turn_number,intent_type,prompt,model,created_at,updated_at)
            VALUES (?,2,'Bug 修复','重试',?,?,?)""",(a["id"],m.AUTO_MODEL,host.now_text(),host.now_text()))
    result=v.preflight(host,pair_id)
    assert not result["eligible"]
    assert any("恰好保留一轮" in issue for issue in result["issues"])


@pytest.mark.parametrize("field,value",[("trace_sha256","bad"),("artifact_tree","bad"),("video_seconds",91),
    ("trace_path",""),("recording_path","")])
def test_tampering_or_missing_evidence_blocks_export(host,field,value):
    pair_id,a,_=ready_pair(host)
    m.update_attempt(host,a["id"],**{field:value})
    assert not v.preflight(host,pair_id)["eligible"]
    with pytest.raises(app.WorkflowError):ex.row(host,pair_id)


def test_gsb_optimistic_lock_and_evidence_invalidation(host):
    pair_id,a,_=ready_pair(host)
    p=m.task(host,pair_id)
    with pytest.raises(app.WorkflowError,match="另一页面"):
        m.save_review(host,pair_id,{"revision":p["revision"]-1})
    v.save_evidence(host,a["id"],{"video_seconds":30,"recording_confirmed":True})
    assert not m.task(host,pair_id)["human_confirmed"]


def test_review_is_complete_when_all_fields_are_saved(host):
    p=m.create(host,payload())
    draft=m.save_review(host,p["id"],{"gsb":"A 更好","reason":"草稿",
        "a_delivery_score":4,"a_delivery_description":"A 主流程完整，异常提示还有一处不够清楚。",
        "revision":p["revision"]})
    assert draft["a_delivery_score"]==4 and not draft["human_confirmed"]
    complete=m.save_review(host,p["id"],{"b_delivery_score":5,
        "b_delivery_description":"B 主流程已核对，产物能够运行。", "revision":draft["revision"]})
    assert complete["human_confirmed"]


def test_submission_no_longer_requires_video_or_gsb_confirmation(host):
    pair_id,a,b=ready_pair(host)
    pair=m.task(host,pair_id)
    m.save_review(host,pair_id,{"reason":"A 先核对了订单入口，在服务中补齐状态流转，终端复测成功，产物覆盖了题面要求。B 先调整了展示层，随后补了服务校验，最终也可运行，但重复提交时反馈不够明确。比较两侧轨迹和交付结果，A 的处理路径更清楚，异常场景也更完整。",
        "revision":pair["revision"]})
    for attempt in (a,b):
        m.update_attempt(host,attempt["id"],recording_confirmed=0)
    with host.db_connection() as db:
        db.execute("UPDATE pairwise_tasks SET human_confirmed=0 WHERE id=?",(pair_id,))
    assert solo.quick_readiness(host,pair_id)[0]
    result=v.preflight(host,pair_id)
    assert result["eligible"], result["issues"]
    assert solo.bundle(host,pair_id)["recordings"]["A"]["url"].endswith("/recording-file")


def test_export_difficulty_choice_overrides_automatic_review(host):
    pair_id,a,b=ready_pair(host)
    approve_difficulty(host,pair_id,"中等")
    assert solo.quick_readiness(host,pair_id)[0]
    pair=m.task(host,pair_id)
    updated=m.save_review(host,pair_id,{"submission_difficulty":"地狱",
        "reason":"A 先核对了订单入口，在服务中补齐状态流转，终端复测成功，产物覆盖了题面要求。B 先调整了展示层，随后补了服务校验，最终也可运行，但重复提交时反馈不够明确。比较两侧轨迹和交付结果，A 的处理路径更清楚，异常场景也更完整。",
        "revision":pair["revision"]})
    assert updated["submission_difficulty"]=="地狱"
    assert solo.quick_readiness(host,pair_id)[0]
    checked=v.preflight(host,pair_id)
    assert checked["eligible"], checked["issues"]
    assert checked["submission_difficulty"]=="地狱"
    assert ex.row(host,pair_id)[2]=="地狱"
    assert solo.values(host,pair_id)["difficulty"]=="地狱"
    item=solo.exports_page(host,{"difficulty":["地狱"]})["items"][0]
    assert item["id"]==pair_id and item["export_ready"]
    assert item["recording"]["A"]["url"].endswith(f"{a['id']}/recording-file")
    assert item["recording"]["B"]["url"].endswith(f"{b['id']}/recording-file")
    lower=m.save_review(host,pair_id,{"submission_difficulty":"中等","revision":updated["revision"]})
    assert solo.quick_readiness(host,pair_id)[0]
    assert v.preflight(host,pair_id)["eligible"]
    assert lower["submission_difficulty"]=="中等"


def test_gsb_reason_style_rules_are_separate_from_delivery_descriptions():
    from pairwise.validation import gsb_reason_style_issues
    bad="A模型完成修改，因此选择 A 更好；docker compose run --rm verify 通过，当前更适合交付。"
    issues="；".join(gsb_reason_style_issues(bad))
    for token in ("因此","选择 A 更好","docker compose run --rm verify","更适合交付"):
        assert token in issues
    later="若请求失败，状态随之清空。这一结果无法复核。检查中加入断言，逐项复核。"
    later_issues="；".join(gsb_reason_style_issues(later))
    for token in ("若", "随之", "这一", "无法", "断言", "逐项"):
        assert token in later_issues
    for verdict in ("故取 A", "故取 B"):
        assert verdict in "；".join(gsb_reason_style_issues("改动使旧值丢失，" + verdict + "。"))
    assert gsb_reason_style_issues("A 在 src/app.ts 处理读取失败，B 在同一入口保留了过期结果，文件切换后页面会显示上一份数据。") == []
    assert gsb_reason_style_issues("A模型处理了读取失败，B模型保留了过期结果；A的修改覆盖了文件切换。") == []


def test_canonical_commit_preserves_model_history_and_pushes_sibling_branch(host,tmp_path):
    p=m.create(host,payload())
    w.start(host,p["id"])
    a=m.latest(m.attempts(host,p["id"]),"A")
    work=Path(a["repo_path"])
    base=repository(work)
    # Existing baseline files remain part of the frozen task, even when their
    # directory name matches a generated-output filter.
    (work/"target").mkdir()
    (work/"target"/"tracked-fixture.txt").write_text("baseline fixture")
    (work/"dist").mkdir()
    (work/"dist"/"baseline.js").write_text("frozen baseline asset")
    (work/".tools"/"go").mkdir(parents=True)
    (work/".tools"/"go"/"baseline.txt").write_text("frozen toolchain fixture")
    git(work,"add",".");git(work,"commit","-m","baseline target fixture")
    base=git(work,"rev-parse","HEAD")
    (work/"model.txt").write_text("model output")
    dependency=work/"node_modules"/"sample"/"index.js"
    dependency.parent.mkdir(parents=True)
    dependency.write_text("installed dependency")
    rust_outputs=[work/"target"/"debug"/"host.bin",work/"plugin-host"/"target"/"release"/"libdep.rlib"]
    for output in rust_outputs:
        output.parent.mkdir(parents=True)
        output.write_bytes(b"\0compiled Rust output")
    generated = [work/"frontend"/"dist"/"assets"/"ts.worker.js",
                 work/"frontend"/".next"/"bundle.js", work/"frontend"/"app.tsbuildinfo",
                 work/"backend"/"workspace.db", work/"backend"/"workspace.db-wal",
                 work/"backend"/"workspace.db-shm", work/"backend"/"custom.sqlite3",
                 work/"backend"/"custom.sqlite3-wal", work/"backend"/"cache.pyc",
                 work/".tools"/"go"/"bin"/"go",
                 work/".tools"/"go"/"src"/"runtime"/"runtime.go",
                 work/".tools"/"go"/"test"/"fixedbugs"/"Þfoo.go",
                 work/"service"/".tools"/"go"/"pkg"/"tool"/"compile"]
    for output in generated:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"generated file")
    retained = [work/"frontend"/"package-lock.json", work/"backend"/"schema.sql",
                work/"backend"/"tests"/"fixtures"/"workspace.db",
                work/"backend"/"tests"/"fixtures"/"workspace.db-wal",
                work/"backend"/"reference.db", work/".tools"/"check.sh",
                work/".tools"/"golang-helper"/"main.go"]
    for source in retained:
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"source or intentional fixture")
    git(work,"add",".");git(work,"commit","-m","model intermediate")
    model_head=git(work,"rev-parse","HEAD")
    remote=tmp_path/"remote.git"
    subprocess.run(["git","init","--bare",str(remote)],check=True,capture_output=True)
    seed_three_branches(work,remote,base)
    with host.db_connection() as db:
        db.execute("UPDATE pairwise_tasks SET repo_url=?,base_sha=? WHERE id=?",(str(remote),base,p["id"]))
    host.update_run(a["id"],session_id="session-a")
    sha=w.checkpoint(host,a["id"],{**m.task(host,p["id"]),"base_sha":base,"repo_url":str(remote)})
    assert git(work,"show","-s","--format=%P",sha)==base
    assert git(work,"rev-parse","HEAD")==model_head
    assert git(work,"show",f"{sha}:model.txt")=="model output"
    assert git(work,"show",f"{model_head}:node_modules/sample/index.js")=="installed dependency"
    assert "node_modules/sample/index.js" not in git(work,"ls-tree","-r","--name-only",sha)
    assert dependency.read_text()=="installed dependency"
    assert git(work,"show",f"{sha}:target/tracked-fixture.txt")=="baseline fixture"
    assert git(work,"show",f"{sha}:dist/baseline.js")=="frozen baseline asset"
    assert git(work,"show",f"{sha}:.tools/go/baseline.txt")=="frozen toolchain fixture"
    product_paths=git(work,"-c","core.quotePath=false","ls-tree","-r","--name-only",sha).splitlines()
    model_paths=git(work,"-c","core.quotePath=false","ls-tree","-r","--name-only",model_head).splitlines()
    for output in rust_outputs:
        assert output.relative_to(work).as_posix() not in product_paths
        assert output.relative_to(work).as_posix() in model_paths
        assert output.read_bytes()==b"\0compiled Rust output"
    for output in generated:
        assert output.relative_to(work).as_posix() not in product_paths
        assert output.relative_to(work).as_posix() in model_paths
        assert output.read_bytes() == b"generated file"
    for source in retained:
        assert source.relative_to(work).as_posix() in product_paths
    assert git(work,"ls-remote",str(remote),"refs/heads/A")==f"{sha}\trefs/heads/A"
    assert w.remote_branches(host,str(remote),work)=={"main":base,"A":sha,"B":base}
    assert w.checkpoint(host,a["id"],{**m.task(host,p["id"]),"base_sha":base,"repo_url":str(remote)})==sha


def test_product_branch_names_are_exactly_uppercase_a_and_b(host,tmp_path):
    pair_id,a,b=ready_pair(host)
    remote=tmp_path/"remote.git"
    subprocess.run(["git","init","--bare",str(remote)],check=True,capture_output=True)
    with host.db_connection() as db:
        db.execute("UPDATE pairwise_tasks SET repo_url=? WHERE id=?",(str(remote),pair_id))
    pair=m.task(host,pair_id)
    m.update_attempt(host,a["id"],status="archiving")
    m.update_attempt(host,b["id"],status="archiving")
    git(Path(a["repo_path"]),"fetch",b["repo_path"],b["artifact_sha"])
    seed_three_branches(Path(a["repo_path"]),remote,pair["base_sha"],b=b["artifact_sha"])
    assert w.checkpoint(host,a["id"],pair)==a["artifact_sha"]
    m.update_attempt(host,a["id"],status="complete")
    assert w.checkpoint(host,b["id"],pair)==b["artifact_sha"]
    assert w.remote_branches(host,str(remote),Path(a["repo_path"]))=={
        "main":pair["base_sha"],"A":a["artifact_sha"],"B":b["artifact_sha"]}


def test_single_side_rerun_replaces_only_its_previous_remote_product(host,tmp_path):
    pair_id, old_a, old_b = ready_pair(host)
    remote = tmp_path / "rerun.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    seed_work = Path(old_a["repo_path"])
    git(seed_work, "fetch", old_b["repo_path"], old_b["artifact_sha"])
    pair = m.task(host, pair_id)
    seed_three_branches(seed_work, remote, pair["base_sha"],
                        a=old_a["artifact_sha"], b=old_b["artifact_sha"])
    with host.db_connection() as db:
        db.execute("UPDATE pairwise_tasks SET repo_url=? WHERE id=?", (str(remote), pair_id))

    fresh = w.rerun_side(host, pair_id, "B")
    pair = m.task(host, pair_id)
    w.validate_remote_heads(host, pair, w.remote_branches(host, str(remote)))
    workspace = Path(fresh["repo_path"])
    workspace.mkdir(parents=True)
    w.clone_initial(host, pair, workspace, "B")
    (workspace / "product.txt").write_text("new B")
    host.update_run(fresh["id"], session_id="session-new-b")
    m.update_attempt(host, fresh["id"], status="archiving")
    new_sha = w.checkpoint(host, fresh["id"], pair)
    assert new_sha != old_b["artifact_sha"]
    assert w.remote_branches(host, str(remote)) == {
        "main": pair["base_sha"], "A": old_a["artifact_sha"], "B": new_sha}
    m.update_attempt(host, fresh["id"], status="complete")
    w.validate_remote_heads(host, pair, w.remote_branches(host, str(remote)))


def test_a_b_checkpoint_pushes_are_safe_when_they_finish_together(host,tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    pair_id,a,b=ready_pair(host)
    remote=tmp_path/"concurrent.git"
    subprocess.run(["git","init","--bare",str(remote)],check=True,capture_output=True)
    with host.db_connection() as db:
        db.execute("UPDATE pairwise_tasks SET repo_url=? WHERE id=?",(str(remote),pair_id))
    pair=m.task(host,pair_id)
    m.update_attempt(host,a["id"],status="archiving")
    m.update_attempt(host,b["id"],status="archiving")
    seed_three_branches(Path(a["repo_path"]),remote,pair["base_sha"])
    barrier=threading.Barrier(2)
    def push(run_id):
        barrier.wait()
        return w.checkpoint(host,run_id,pair)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(push,a["id"]),pool.submit(push,b["id"])]
        assert {future.result(timeout=10) for future in futures}=={a["artifact_sha"],b["artifact_sha"]}
    assert w.remote_branches(host,str(remote))=={
        "main":pair["base_sha"],"A":a["artifact_sha"],"B":b["artifact_sha"]}


def test_product_branch_never_overwrites_existing_a_or_b(host,tmp_path):
    pair_id,a,b=ready_pair(host)
    remote=tmp_path/"remote.git"
    subprocess.run(["git","init","--bare",str(remote)],check=True,capture_output=True)
    work=Path(a["repo_path"])
    with host.db_connection() as db:
        db.execute("UPDATE pairwise_tasks SET repo_url=? WHERE id=?",(str(remote),pair_id))
    pair=m.task(host,pair_id)
    m.update_attempt(host,a["id"],status="archiving")
    git(work,"fetch",b["repo_path"],b["artifact_sha"])
    other=b["artifact_sha"]
    seed_three_branches(work,remote,pair["base_sha"],a=other)
    with pytest.raises(app.WorkflowError,match="外部修改|独立仓库"):
        w.checkpoint(host,a["id"],m.task(host,pair_id))
    assert git(work,"ls-remote",str(remote),"refs/heads/A")==f"{other}\trefs/heads/A"


def test_recording_clone_is_clean_and_exact(host):
    pair_id,a,_=ready_pair(host)
    (Path(a["repo_path"])/"untracked-secret").write_text("must not copy")
    result=w.prepare_recording(host,a["id"])
    root=Path(result["path"])
    assert root.parent==host.DATA_DIR/"pairwise-recording-workspaces"
    assert git(root,"rev-parse","HEAD")==a["artifact_sha"]
    assert not (root/"untracked-secret").exists()
    assert not git(root,"status","--porcelain")
    recording._remove_prepared_workspace(host,a,root)
    assert not root.exists()


@pytest.mark.parametrize("fail",[False,True])
def test_automatic_recording_cleans_temporary_clone_on_success_or_failure(host,monkeypatch,fail):
    _,a,_=ready_pair(host)
    monkeypatch.setattr(recording,"_recording_supported",lambda:True)
    seen=[]
    def fake_record_prepared(h,attempt,pair,workspace):
        seen.append(workspace)
        assert workspace.is_dir()
        assert git(workspace,"rev-parse","HEAD")==attempt["artifact_sha"]
        if fail:
            raise RuntimeError("capture failed")
        video=workspace.parent.parent/"recording-result.m4v"
        video.write_bytes(b"video")
        seen.append(video)
    monkeypatch.setattr(recording,"_record_prepared",fake_record_prepared)
    if fail:
        with pytest.raises(RuntimeError,match="capture failed"):
            recording._record(host,a)
    else:
        recording._record(host,a)
        assert seen[1].read_bytes()==b"video"
    assert not seen[0].exists()


def test_recording_clone_failure_removes_partial_workspace(host,monkeypatch):
    _,a,_=ready_pair(host)
    original=host.run_command
    def fail_fetch(command,**kwargs):
        if command[:2]==["git","fetch"]:
            raise host.WorkflowError("fetch failed")
        return original(command,**kwargs)
    monkeypatch.setattr(host,"run_command",fail_fetch)
    with pytest.raises(host.WorkflowError,match="fetch failed"):
        w.prepare_recording(host,a["id"])
    assert not list((host.DATA_DIR/"pairwise-recording-workspaces").iterdir())


def test_recording_recovery_cleans_only_owned_stale_workspaces(host,monkeypatch):
    root=host.DATA_DIR/"pairwise-recording-workspaces"
    stale=root/"workspace-attempt-stale"
    unrelated=root/"keep-me"
    stale.mkdir(parents=True)
    unrelated.mkdir()
    (stale/"partial").write_text("discard")
    monkeypatch.setattr(recording,"enqueue_pair_if_ready",lambda *_:pytest.fail("恢复时不得自动加入已完成 Pair"))
    monkeypatch.setattr(recording,"start_coordinator",lambda *_:None)
    recording.recover(host)
    assert not stale.exists()
    assert unrelated.is_dir()


def test_manually_started_recording_rejects_a_second_pair_instead_of_queueing(host):
    first_id,first_a,first_b=ready_pair(host,title="first",repo_name="recording-first")
    second_id,second_a,second_b=ready_pair(host,title="second",repo_name="recording-second")
    for item in (first_a,first_b,second_a,second_b):
        m.update_attempt(host,item["id"],video_url="",video_seconds=None,recording_confirmed=0,
            recording_status="",recording_path="",recording_error="")
    recording.enqueue_pair_if_ready(host,first_id)
    with pytest.raises(host.WorkflowError,match="本次没有排队"):
        recording.enqueue_pair_if_ready(host,second_id)
    assert all(not item["recording_status"] for item in m.attempts(host,second_id))
    seen=[]
    busy=False
    def fake_recorder(h,item):
        nonlocal busy
        assert not busy
        busy=True
        seen.append((item["pair_id"],item["side"]))
        m.update_attempt(h,item["id"],recording_status="complete",recording_path=f"/{item['id']}.mov",
            recording_duration=12.5,video_seconds=12.5,recording_finished_at=h.now_text())
        busy=False
    results=[recording.run_next_once(host,fake_recorder) for _ in range(2)]
    assert seen==[(first_id,"A"),(first_id,"B")]
    assert all(item["recording_status"]=="complete" for item in results)
    assert recording.run_next_once(host,fake_recorder) is None


def test_recording_request_during_deployment_is_rejected_without_queueing(host,monkeypatch):
    monkeypatch.setattr(host,"CODEX_SLOT_ACTIVE",0)
    pair_id,a,b=ready_pair(host,title="draining",repo_name="recording-draining")
    for item in (a,b):
        m.update_attempt(host,item["id"],video_url="",video_seconds=None,recording_confirmed=0,
            recording_status="",recording_path="",recording_error="")
    deployment.configure(host,True)
    try:
        with pytest.raises(host.WorkflowError,match="没有加入录屏队列"):
            recording.enqueue_pair_if_ready(host,pair_id)
        assert all(not item["recording_status"] for item in m.attempts(host,pair_id))
    finally:
        deployment.configure(host,False)


def test_recording_waits_for_both_sides_and_does_not_auto_confirm(host):
    p=m.create(host,payload(title="record",repo_name="recording-wait"))
    a=complete(host,p["id"],"A")
    m.update_attempt(host,a["id"],video_seconds=None,recording_confirmed=0,
        recording_status="",recording_path="",recording_error="")
    assert recording.enqueue_pair_if_ready(host,p["id"])==[]
    b=complete(host,p["id"],"B")
    m.update_attempt(host,b["id"],video_seconds=None,recording_confirmed=0,
        recording_status="",recording_path="",recording_error="")
    queued=recording.enqueue_pair_if_ready(host,p["id"])
    assert [item["side"] for item in queued]==["A","B"]
    assert all(item["recording_status"]=="queued" and not item["recording_confirmed"] for item in queued)


def test_manual_rerecord_requeues_both_completed_sides(host):
    pair_id,a,b=ready_pair(host,title="rerecord",repo_name="manual-rerecord")
    for item in (a,b):
        m.update_attempt(host,item["id"],recording_status="complete",
            recording_path=f"/{item['side']}.m4v",recording_duration=30,recording_confirmed=1)
    queued=recording.enqueue_pair_if_ready(host,pair_id,force=True)
    assert [item["side"] for item in queued]==["A","B"]
    assert all(item["recording_status"]=="queued" and not item["recording_confirmed"] for item in queued)


@pytest.mark.parametrize("side", ["A", "B"])
def test_single_side_rerecord_preserves_other_recording(host, side):
    pair_id,a,b=ready_pair(host,title="single side",repo_name="single-side-recording")
    other = b if side == "A" else a
    before = m.attempt(host,other["id"])
    content = Path(before["recording_path"]).read_bytes()
    queued = recording.enqueue_pair_if_ready(host,pair_id,force=True,side=side)
    assert [item["side"] for item in queued] == [side]
    assert queued[0]["recording_status"] == "queued"
    assert queued[0]["recording_confirmed"] == 0
    assert m.attempt(host,other["id"]) == before
    assert Path(before["recording_path"]).read_bytes() == content


@pytest.mark.parametrize("side", ["a", "AB", "", [], 1])
def test_invalid_recording_side_does_not_change_either_attempt(host, side):
    pair_id,_,_=ready_pair(host,title="invalid side",repo_name="invalid-side-recording")
    before = m.attempts(host,pair_id)
    with pytest.raises(host.WorkflowError,match="录屏范围"):
        recording.enqueue_pair_if_ready(host,pair_id,force=True,side=side)
    assert m.attempts(host,pair_id) == before


@pytest.mark.parametrize("side,expected", [("A",["A"]),("B",["B"]),("both",["A","B"])])
def test_recording_api_queues_selected_side(host,side,expected):
    from pairwise.api import post
    pair_id,_,_=ready_pair(host,title="selected API",repo_name="selected-recording-api")
    responses=[]
    handler=mock.Mock(send_json=responses.append)
    post(handler,host,f"/api/pairwise/{pair_id}/recording",{"side":side})
    assert [a["side"] for a in responses[0]["attempts"] if a["recording_status"]=="queued"] == expected


@pytest.mark.parametrize("category,language", [("纯前端","React"),("全栈","React, FastAPI")])
def test_frontend_and_fullstack_use_page_interactions(host,tmp_path,monkeypatch,category,language):
    pair_id,a,_=ready_pair(host,title="page flow",repo_name="page-flow")
    pair=m.task(host,pair_id)
    pair.update(project_category=category,language_framework=language,prompt="实现页面和 HTTP API")
    called=[]
    monkeypatch.setattr(recording,"_record_frontend_prepared",lambda *args:called.append(args))
    recording._record_prepared(host,a,pair,tmp_path)
    assert len(called)==1 and called[0][1]["id"]==a["id"]


def test_recording_runtime_reports_queue_and_active_job(host):
    pair_id,a,b=ready_pair(host,title="runtime",repo_name="recording-runtime")
    for item in (a,b):
        m.update_attempt(host,item["id"],video_url="",video_seconds=None,recording_confirmed=0,
            recording_status="",recording_path="",recording_error="")
    recording.enqueue_pair_if_ready(host,pair_id)
    claimed=recording._claim_next(host)
    status=recording.queue_status(host)
    assert claimed["side"]=="A"
    assert status["active"]==1 and status["queued"]==1
    assert status["current"]["title"]=="runtime"
    assert status["max_seconds"]<90
    assert status["current"]["progress"]["stage"]=="workspace"
    assert status["current"]["progress"]["label"]=="准备干净产物"


def test_recording_progress_tracks_step_elapsed_and_exports(host,monkeypatch):
    pair_id,a,b=ready_pair(host,title="progress",repo_name="recording-progress")
    monkeypatch.setattr(host,"now_text",lambda:"2026-09-17 16:00:00 +0800")
    recording._set_stage(host,a["id"],"planning",recording_status="preparing")
    monkeypatch.setattr(host,"now_text",lambda:"2026-09-17 16:02:10 +0800")
    progress=recording.progress(host,m.attempt(host,a["id"]))
    assert progress["label"]=="生成操作路线" and progress["elapsed_seconds"]==130
    assert m.latest(detail(host,pair_id)["attempts"],"A")["recording_progress"]==progress
    assert solo.exports_page(host,{})["items"][0]["recording"]["A"]["progress"]==progress
    recording._set_stage(host,a["id"],"starting_app")
    assert recording.progress(host,m.attempt(host,a["id"]))["elapsed_seconds"]==0
    m.update_attempt(host,a["id"],recording_status="failed",recording_finished_at="2026-09-17 16:03:00 +0800")
    monkeypatch.setattr(host,"now_text",lambda:"2026-09-17 17:00:00 +0800")
    failed=recording.progress(host,m.attempt(host,a["id"]))
    assert failed["description"]=="失败步骤：启动项目" and failed["elapsed_seconds"]==50
    recording.enqueue(host,a["id"],force=True)
    assert recording.progress(host,m.attempt(host,a["id"]))["stage"]=="queued"
    assert m.attempt(host,b["id"])["recording_status"]=="complete"


def test_export_summary_marks_failure_video_without_marking_capture_failed(host):
    pair_id,a,_=ready_pair(host,title="failure evidence",repo_name="failure-evidence")
    m.update_attempt(host,a["id"],recording_status="complete",recording_path="/tmp/A.m4v",
        recording_error='已录制原始产物启动失败：nginx: invalid option: "g"')
    summary=solo.exports_page(host,{})["items"][0]["recording"]["A"]
    assert summary["status"]=="complete"
    assert summary["failure_evidence"] is True


def test_export_summary_marks_runtime_failure_video(host):
    pair_id,a,_=ready_pair(host,title="runtime failure evidence",repo_name="runtime-failure-evidence")
    m.update_attempt(host,a["id"],recording_status="complete",recording_path="/tmp/A.m4v",
        recording_error="已录制原始产物运行失败：点击计算后页面始终显示计算中")
    summary=solo.exports_page(host,{})["items"][0]["recording"]["A"]
    assert summary["status"]=="complete"
    assert summary["failure_evidence"] is True


def test_legacy_recording_progress_does_not_invent_substep(host):
    assert recording.progress(host,{}) is None
    result=recording.progress(host,{"recording_status":"preparing","recording_started_at":host.now_text()})
    assert result["label"]=="准备录屏" and result["stage"]==""
    assert result["elapsed_seconds"] is None


def test_recording_progress_schema_migration_is_additive(host):
    pair_id,a,_=ready_pair(host,title="migration",repo_name="progress-migration")
    with host.db_connection() as db:
        db.execute("ALTER TABLE pairwise_attempts DROP COLUMN recording_stage")
        db.execute("ALTER TABLE pairwise_attempts DROP COLUMN recording_stage_started_at")
    m.initialize(host)
    m.initialize(host)
    after=m.attempt(host,a["id"])
    assert after["recording_stage"]=="" and after["recording_stage_started_at"] is None
    assert after["recording_path"]==a["recording_path"]
    assert after["recording_confirmed"]==1


def test_frontend_recording_emits_real_stage_transitions(host,tmp_path,monkeypatch):
    pair_id,a,_=ready_pair(host,title="stages",repo_name="frontend-stages")
    pair=m.task(host,pair_id)
    root=tmp_path/"capture-state"
    root.mkdir()
    workspace=tmp_path/"page"
    workspace.mkdir()
    (workspace/"index.html").write_text("<p>Fixture</p>")
    output=tmp_path/"video"/"A.mov"
    stages=[]
    real_set=recording._set_stage
    def set_stage(h,run_id,stage,**fields):
        stages.append(stage)
        real_set(h,run_id,stage,**fields)
    monkeypatch.setattr(recording,"_set_stage",set_stage)
    monkeypatch.setattr(recording,"_frontend_plan",lambda *_:{"actions":[
        {"type":"click","target":"演示","value":"","file_name":"","purpose":"执行页面操作"}
    ]})
    monkeypatch.setattr(host,"run_command",lambda *_args,**_kwargs:None)
    def wait_url(root,environment,on_waiting_page,workspace=None):
        assert stages[-1]=="starting_app"
        assert workspace==tmp_path/"page"
        on_waiting_page()
        return "http://127.0.0.1:12345"
    monkeypatch.setattr(recording,"_wait_frontend_url",wait_url)
    monkeypatch.setattr(recording,"_launch_frontend_browser",lambda *_:mock.Mock(pid=12345))
    monkeypatch.setattr(browser_demo,"prepare",lambda *_:(mock.Mock(),"0,0,800,600"))
    ordering=[]
    def place_pointer(*_):
        ordering.append("pointer")
        assert stages[-1]=="opening_browser"
        return (320,260)
    monkeypatch.setattr(browser_demo,"place_initial_pointer",place_pointer)
    def run_demo(*_args,**kwargs):
        ordering.append("demo")
        assert kwargs["initial_pointer"]==(320,260)
    monkeypatch.setattr(browser_demo,"run",run_demo)
    def capture(*_args,**_kwargs):
        ordering.append("capture")
        output.write_bytes(b"synthetic-video"*500)
        return mock.Mock(poll=lambda:0),root/"state",root/"stop"
    monkeypatch.setattr(recording,"_start_capture",capture)
    def no_full_duration_wait(*_):
        raise AssertionError("finished UI actions must stop immediately, not poll until 88 seconds")
    monkeypatch.setattr(recording,"_read_capture_state",no_full_duration_wait)
    def stop_capture(*_):
        assert (root/"done").exists()
        return "", {"phase":"complete"}
    monkeypatch.setattr(recording,"_stop_capture",stop_capture)
    monkeypatch.setattr(recording,"_stop_frontend_browser",lambda *_:None)
    monkeypatch.setattr(recording,"_cleanup_demo",lambda *_:None)
    monkeypatch.setattr(recording,"_content_duration",lambda *_:30)
    monkeypatch.setattr(recording,"_compress_recording",lambda path,*_:path)
    monkeypatch.setattr(recording,"_duration",lambda *_:30)
    monkeypatch.setattr(recording.time,"sleep",lambda *_:None)
    monkeypatch.setattr(recording,"_STOP",mock.Mock(is_set=lambda:False))
    recording._record_frontend_prepared(host,a,pair,workspace,root,output.parent,output,
        {"API_PORT":"12344","WEB_PORT":"12345","PORT":"12346","COMPOSE_PROJECT_NAME":"fixture"})
    assert stages==["planning","starting_app","waiting_page","opening_browser","starting_capture","recording","finalizing","encoding"]
    assert m.attempt(host,a["id"])["recording_stage"]=="complete"
    assert ordering==["pointer","capture","demo"]


def test_frontend_recording_browser_uses_deterministic_media_device(tmp_path,monkeypatch):
    calls=[]
    class Process:
        pid=123
    monkeypatch.setattr(recording.subprocess,"Popen",lambda argv,**options:
        calls.append((argv,options)) or Process())
    executable=tmp_path/"Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
    monkeypatch.setattr(recording,"_recording_browser_executable",lambda:executable)
    process=recording._launch_frontend_browser(tmp_path,"http://127.0.0.1:1234/",9222)
    assert process.pid==123
    argv=calls[0][0]
    assert argv[0]==str(executable)
    assert "--use-fake-ui-for-media-stream" in argv
    assert "--use-fake-device-for-media-stream" in argv
    assert "--autoplay-policy=no-user-gesture-required" in argv
    assert "--disable-save-password-bubble" in argv
    preferences=json.loads((tmp_path/"chrome-profile"/"Default"/"Preferences").read_text())
    assert preferences["credentials_enable_service"] is False
    assert preferences["profile"]["password_manager_enabled"] is False
    assert preferences["password_manager_leak_detection"] is False


def test_frontend_page_wait_reports_readiness_stage(tmp_path):
    (tmp_path/"app-started").touch()
    response=mock.MagicMock()
    response.__enter__.return_value.headers={"Content-Type":"text/html"}
    callback=mock.Mock()
    with mock.patch.object(recording.urllib.request,"urlopen",return_value=response):
        assert recording._wait_frontend_url(tmp_path,{"WEB_PORT":"12345","PORT":"12346","API_PORT":"12347"},on_waiting_page=callback)=="http://127.0.0.1:12345/"
    callback.assert_called_once_with()


def test_frontend_page_wait_reports_crashed_compose_service_immediately(tmp_path,monkeypatch):
    (tmp_path/"docker-compose.yml").write_text("services: {}")
    (tmp_path/"app-started").touch()
    environment={"WEB_PORT":"12345","PORT":"12346","API_PORT":"12347","COMPOSE_PROJECT_NAME":"fixture"}
    calls=[]
    def docker(command,**kwargs):
        calls.append(command)
        if "ps" in command:
            return mock.Mock(returncode=0,stdout=json.dumps([
                {"Service":"verify","State":"exited","ExitCode":0},
                {"Service":"web","State":"restarting","ExitCode":1},
            ]),stderr="")
        return mock.Mock(returncode=0,stdout="web-1 | nginx: invalid option: \"g\"\n",stderr="")
    monkeypatch.setattr(recording.subprocess,"run",docker)
    monkeypatch.setattr(recording.urllib.request,"urlopen",lambda *_,**__: (_ for _ in ()).throw(
        recording.urllib.error.URLError("connection refused")))
    with pytest.raises(RuntimeError,match='nginx: invalid option: "g"'):
        recording._wait_frontend_url(tmp_path,environment,timeout=10,workspace=tmp_path)
    assert any("ps" in command for command in calls)
    assert any("logs" in command for command in calls)


def test_frontend_failure_script_replays_original_compose_and_logs(tmp_path):
    text=recording._frontend_failure_script(tmp_path,tmp_path,{
        "COMPOSE_PROJECT_NAME":"original-a","WEB_PORT":"12345","API_PORT":"12346","PORT":"12347",
    },"web")
    assert "docker compose up -d --build web" in text
    assert "docker compose ps -a" in text
    assert "docker compose logs --no-color --tail 18 web" in text
    assert "sed -i" not in text and "nginx -g" not in text
    assert text.index("recording-start") < text.index("docker compose up -d --build")


def test_frontend_failure_script_replays_compose_build_failure_without_a_service(tmp_path):
    (tmp_path/"compose.yaml").write_text("services: {}")
    text=recording._frontend_failure_script(tmp_path,tmp_path,{
        "COMPOSE_PROJECT_NAME":"original-a","WEB_PORT":"12345","API_PORT":"12346","PORT":"12347",
    },replay_compose=True)
    assert "docker compose up -d --build" in text
    assert "failure-reproduced" in text
    assert "npm run build" not in text


def test_frontend_plan_allows_eleven_actions_for_multi_field_workflows():
    plan={"actions":[{"type":"click","target":f"动作 {index}"} for index in range(11)]}
    assert recording._frontend_plan_problem({"prompt":"题面"},plan)==""
    plan["actions"].append({"type":"click","target":"超出上限"})
    assert "2 至 11" in recording._frontend_plan_problem({"prompt":"题面"},plan)


def test_frontend_failure_script_replays_original_package_build(tmp_path):
    (tmp_path/"package.json").write_text(json.dumps({
        "scripts":{"build":"tsc --noEmit && vite build","dev":"vite"},
    }))
    text=recording._frontend_failure_script(tmp_path,tmp_path,{
        "COMPOSE_PROJECT_NAME":"original-a","WEB_PORT":"12345","API_PORT":"12346","PORT":"12347",
    })
    assert "npm install && npm run build" in text
    assert "failure-reproduced" in text
    assert "sed -i" not in text


def test_frontend_crash_uses_real_failure_capture_not_browser(host,tmp_path,monkeypatch):
    pair_id,a,_=ready_pair(host,title="crash",repo_name="frontend-crash")
    pair=m.task(host,pair_id)
    workspace=tmp_path/"page"
    workspace.mkdir()
    (workspace/"docker-compose.yml").write_text("services: {}")
    root=tmp_path/"state"
    root.mkdir()
    output=tmp_path/"video"/"A.mov"
    monkeypatch.setattr(recording,"_frontend_plan",lambda *_:{"actions":[]})
    monkeypatch.setattr(host,"run_command",lambda *_,**__:None)
    monkeypatch.setattr(recording,"_wait_frontend_url",lambda *_,**__: (_ for _ in ()).throw(RuntimeError("页面服务 web restarting")))
    monkeypatch.setattr(recording,"_compose_page_failure",lambda *_:"页面服务 web restarting；nginx: invalid option: g")
    captured=[]
    monkeypatch.setattr(recording,"_record_frontend_start_failure",lambda *args:captured.append(args))
    monkeypatch.setattr(recording,"_launch_frontend_browser",lambda *_:pytest.fail("failed product must not open a success page"))
    monkeypatch.setattr(recording,"_cleanup_demo",lambda *_:None)
    recording._record_frontend_prepared(host,a,pair,workspace,root,output.parent,output,{
        "API_PORT":"12344","WEB_PORT":"12345","PORT":"12346","COMPOSE_PROJECT_NAME":"fixture",
    })
    assert len(captured)==1
    assert captured[0][-1].startswith("页面服务 web restarting")


def test_force_requeue_never_duplicates_an_active_recording(host):
    pair_id,a,_=ready_pair(host,title="active",repo_name="recording-active")
    m.update_attempt(host,a["id"],recording_status="recording",recording_path="/tmp/live.mov",
        recording_started_at=host.now_text())
    result=recording.enqueue(host,a["id"],force=True)
    assert result["recording_status"]=="recording"
    assert result["recording_path"]=="/tmp/live.mov"


def test_recovery_never_resends_or_enters_legacy_review(host,monkeypatch):
    p=m.create(host,payload())
    w.start(host,p["id"])
    a=m.latest(m.attempts(host,p["id"]),"A")
    b=m.latest(m.attempts(host,p["id"]),"B")
    m.update_attempt(host,b["id"],status="stopped")
    m.update_attempt(host,a["id"],status="starting")
    scheduled=[]
    monkeypatch.setattr(w,"enqueue",lambda h,r:scheduled.append(r))
    with mock.patch.object(host,"review_worker",side_effect=AssertionError("AI review forbidden")):
        w.recover(host)
    assert not scheduled
    assert m.attempt(host,a["id"])["status"]=="failed"
    m.update_attempt(host,a["id"],status="running",prompt_sent=1)
    w.recover(host)
    assert scheduled==[a["id"]]


@pytest.fixture
def failed_scene(host,monkeypatch):
    from types import SimpleNamespace
    p=m.create(host,payload())
    w.start(host,p["id"])
    a=m.latest(m.attempts(host,p["id"]),"A")
    raw=Path(a["run_directory"])/"traces"/"-workspace"/"original-session.jsonl"
    trace(raw,p["prompt"],"original-session")
    m.update_attempt(host,a["id"],status="failed",prompt_sent=1,error=w.LEGACY_TIMEOUT_ERROR,
                     started_at="2026-09-17 10:00:00 +0800")
    host.update_run(a["id"],session_id="original-session",phase="failed")
    scene={"pair":p,"a":a,"raw":raw,"running":True,"calls":[],"archived":[]}
    def command(args,**kwargs):
        scene["calls"].append(args)
        if args[:2]==["docker","stop"]:scene["running"]=False
        return SimpleNamespace(returncode=0,stdout="",stderr="")
    def copy(row,destination):
        assert not scene["running"],"must freeze the original writer before copying evidence"
        scene["calls"].append(["copy",row["id"]])
        return destination
    def archive(h,run_id):
        scene["archived"].append(run_id)
        assert h.run_row(run_id)["session_id"]=="original-session"
        m.update_attempt(h,run_id,status="complete",error="",finished_at=h.now_text())
    monkeypatch.setattr(host,"run_command",command)
    monkeypatch.setattr(host,"docker_container_running",lambda _:scene["running"])
    monkeypatch.setattr(host,"copy_container_traces",copy)
    monkeypatch.setattr(w,"archive",archive)
    for name in ("send_prompt_to_screen","send_api_resume_to_screen","launch_docker_terminal","create_followup_turn"):
        monkeypatch.setattr(host,name,lambda *args,**kwargs:pytest.fail("recovery must never call the model"))
    return scene


def test_recover_complete_original_session_without_reroll_or_touching_b(host,failed_scene,monkeypatch):
    scene=failed_scene;a=scene["a"];pair_id=scene["pair"]["id"]
    b=m.latest(m.attempts(host,pair_id),"B")
    queued=w.request_recovery(host,a["id"])
    assert queued["status"]=="recovering"
    assert detail(host,pair_id)["state"]=="检查并恢复归档"
    assert detail(host,pair_id)["finished_at"]==""
    from pairwise import auto_refill
    assert auto_refill._active_attempt_count(host)==2
    with pytest.raises(host.WorkflowError):m.soft_delete(host,pair_id)
    deployment.configure(host,True)
    monkeypatch.setattr(w.deployment,"wait_until_accepting",
                        lambda *_args,**_kwargs:pytest.fail("恢复收尾不能被安全交接门暂停"))
    w.execute(host,a["id"])
    result=m.attempt(host,a["id"])
    assert result["status"]=="complete" and result["session_id"]=="original-session"
    assert scene["archived"]==[a["id"]]
    assert scene["calls"][1]==["docker","stop","--time","10",a["container_name"]]
    assert m.latest(m.attempts(host,pair_id),"B")==b
    assert len(m.attempts(host,pair_id))==2
    assert host.run_row(a["id"])["first_result"]=="合成测试回复"
    deployment.configure(host,False)


@pytest.mark.parametrize("problem",["unfinished","continue","session","api","version","malformed"])
def test_recovery_reruns_incomplete_or_invalid_trace_after_preserving_it(host,failed_scene,problem):
    scene=failed_scene;raw=scene["raw"]
    records=[json.loads(line) for line in raw.read_text().splitlines()]
    if problem=="unfinished":records=records[:1]
    elif problem=="continue":records.append({"type":"user","sessionId":"original-session","message":{"content":"继续"}})
    elif problem=="session":host.update_run(scene["a"]["id"],session_id="different-session")
    elif problem=="api":records.insert(1,{"type":"assistant","isApiErrorMessage":True,"message":{"content":[{"type":"text","text":"API Error: 503"}]}})
    elif problem=="version":records[0]["version"]="9.9.9"
    raw.write_text("\n".join(json.dumps(r,ensure_ascii=False) for r in records)+("\n{bad json" if problem=="malformed" else "\n"))
    w.request_recovery(host,scene["a"]["id"])
    w.execute(host,scene["a"]["id"])
    result=m.attempt(host,scene["a"]["id"])
    assert result["status"]=="failed" and result["finished_at"]
    assert not scene["running"] and not scene["archived"]
    assert raw.exists() and result["container_cleaned"]
    assert any(args[:2]==["docker","rm"] for args in scene["calls"])
    retry=m.latest(m.attempts(host,scene["pair"]["id"]),"A")
    assert retry["id"]!=result["id"] and retry["number"]==2 and retry["status"]=="queued"


def test_timeout_freezes_and_preserves_incomplete_first_turn(host,monkeypatch,failed_scene):
    scene=failed_scene;a=scene["a"]
    scene["raw"].write_text(scene["raw"].read_text().splitlines()[0]+"\n")
    m.update_attempt(host,a["id"],status="running")
    monkeypatch.setattr(host,"seconds_between",lambda *_:host.FIRST_TURN_MAX_RUNTIME_SECONDS)
    monkeypatch.setattr(host,"refresh_trace_snapshot",lambda row:(scene["raw"].parent,host.trace_turn_state(scene["raw"].parent,row["first_prompt"])))
    w.execute(host,a["id"])
    result=m.attempt(host,a["id"])
    assert result["status"]=="failed" and "已停止并保留现场" in result["error"]
    assert not scene["running"] and not scene["archived"]
    assert result["trace_path"]==str(scene["raw"]) and result["container_cleaned"]
    retry=m.latest(m.attempts(host,scene["pair"]["id"]),"A")
    assert retry["id"]!=result["id"] and retry["number"]==2 and retry["status"]=="queued"


def test_timeout_rechecks_completion_race_before_rejecting_result(host,monkeypatch,failed_scene):
    scene=failed_scene;a=scene["a"]
    m.update_attempt(host,a["id"],status="running")
    monkeypatch.setattr(host,"seconds_between",lambda *_:host.FIRST_TURN_MAX_RUNTIME_SECONDS)
    # Last poll failed to read; freezing and copying now finds the final boundary.
    monkeypatch.setattr(host,"refresh_trace_snapshot",lambda row:(scene["raw"].parent,None))
    w.execute(host,a["id"])
    assert m.attempt(host,a["id"])["status"]=="complete"
    assert scene["archived"]==[a["id"]]


def test_pairwise_active_final_stage_extends_past_ninety_minutes(
        host,monkeypatch,failed_scene):
    scene=failed_scene;a=scene["a"]
    m.update_attempt(host,a["id"],status="running")
    host.update_run(a["id"],phase="first_running")
    complete=host.trace_turn_state(scene["raw"].parent,scene["pair"]["prompt"])
    incomplete={**complete,"complete":False}
    states=iter([incomplete,complete])
    monkeypatch.setattr(
        host,"refresh_trace_snapshot",
        lambda row:(scene["raw"].parent,next(states)),
    )
    monkeypatch.setattr(host,"seconds_between",lambda *_:host.FIRST_TURN_MAX_RUNTIME_SECONDS)
    monkeypatch.setattr(host,"trace_activity_signature",lambda _:(1,1,time.time_ns()))
    monkeypatch.setattr(host,"first_turn_is_in_final_stage",lambda _:True)
    monkeypatch.setattr(w.deployment,"wait_until_accepting",lambda *_args,**_kwargs:None)
    monkeypatch.setattr(w.time,"sleep",lambda _:None)
    monkeypatch.setattr(
        w,"archive_completed_state",
        lambda h,run_id,state:m.update_attempt(h,run_id,status="complete"),
    )

    w.monitor(host,a["id"])

    assert m.attempt(host,a["id"])["status"]=="complete"
    assert scene["running"]
    assert not any(call[:2]==["docker","stop"] for call in scene["calls"])


def test_stop_failure_keeps_actionable_error_and_never_archives(host,monkeypatch,failed_scene):
    scene=failed_scene
    monkeypatch.setattr(host,"docker_container_running",lambda _:True)
    w.request_recovery(host,scene["a"]["id"])
    w.execute(host,scene["a"]["id"])
    result=m.attempt(host,scene["a"]["id"])
    assert result["status"]=="failed" and "容器未能停止" in result["error"]
    assert not scene["archived"] and not result["container_cleaned"]


def test_copy_failure_keeps_stopped_container_and_no_fake_archive(host,monkeypatch,failed_scene):
    scene=failed_scene
    def fail(*_):raise host.WorkflowError("轨迹复制失败")
    monkeypatch.setattr(host,"copy_container_traces",fail)
    w.request_recovery(host,scene["a"]["id"])
    w.execute(host,scene["a"]["id"])
    result=m.attempt(host,scene["a"]["id"])
    assert result["status"]=="failed" and "轨迹复制失败" in result["error"]
    assert not scene["running"] and not scene["archived"] and not result["container_cleaned"]


def test_recovery_archive_failure_remains_retryable_without_model(host,monkeypatch,failed_scene):
    scene=failed_scene
    def fail(*_):raise host.WorkflowError("Git 推送失败")
    monkeypatch.setattr(w,"archive",fail)
    w.request_recovery(host,scene["a"]["id"])
    w.execute(host,scene["a"]["id"])
    result=m.attempt(host,scene["a"]["id"])
    assert result["status"]=="archive_failed" and "Git 推送失败" in result["error"]
    w.retry_archive(host,scene["a"]["id"])
    assert m.attempt(host,scene["a"]["id"])["status"]=="archiving"


def test_recovery_rejects_superseded_or_unsent_attempt(host,failed_scene):
    a=failed_scene["a"]
    m.update_attempt(host,a["id"],prompt_sent=0)
    with pytest.raises(host.WorkflowError):w.request_recovery(host,a["id"])
    m.update_attempt(host,a["id"],prompt_sent=1)
    host.update_run(a["id"],container_cleaned=1)
    w.start(host,a["pair_id"],"A")
    with pytest.raises(host.WorkflowError,match="替代"):w.request_recovery(host,a["id"])


@pytest.mark.parametrize("timeout_error", [
    w.LEGACY_TIMEOUT_ERROR,
    "首轮运行达到 90 分钟上限；未检测到首轮最终回复与完成边界，已停止并保留现场；需要处理，不会自动重跑",
])
def test_recovery_reservation_is_idempotent_and_restart_resumes(
        host,monkeypatch,failed_scene,timeout_error):
    a=failed_scene["a"];scheduled=[]
    monkeypatch.setattr(w,"enqueue",lambda h,r:scheduled.append(r))
    b=m.latest(m.attempts(host,a["pair_id"]),"B")
    m.update_attempt(host,b["id"],status="stopped")
    m.update_attempt(host,a["id"],error=timeout_error)
    # Old timeout failures, including the transitional expanded message, are
    # repaired exactly once during upgrade.
    w.recover(host)
    w.request_recovery(host,a["id"])
    assert scheduled==[a["id"]]
    assert m.attempt(host,a["id"])["status"]=="recovering"
    scheduled.clear();w.recover(host)
    assert scheduled==[a["id"]]
    m.update_attempt(host,a["id"],status="failed",error="恢复收尾失败：轨迹复制失败")
    scheduled.clear();w.recover(host)
    assert not scheduled  # no infinite retry for an unrecoverable scene


@pytest.mark.parametrize("status",["complete","stopped","running","archiving","archive_failed"])
def test_recovery_does_not_restart_success_or_user_stopped_attempts(host,failed_scene,status):
    a=failed_scene["a"]
    m.update_attempt(host,a["id"],status=status)
    with pytest.raises(host.WorkflowError):w.request_recovery(host,a["id"])
    assert len(m.attempts(host,a["pair_id"]))==2


def test_recovery_api_only_queues_original_attempt(host,failed_scene):
    from pairwise import api
    handler=mock.Mock();a=failed_scene["a"]
    api.post(handler,host,f"/api/pairwise/attempts/{a['id']}/recover",{})
    assert handler.send_json.call_args[0][0]["state"]=="检查并恢复归档"
    assert len(m.attempts(host,a["pair_id"]))==2


@pytest.mark.parametrize("tokens,model",[(999999,m.AUTO_MODEL),(1_000_000,"opus"),(0,m.AUTO_MODEL)])
def test_environment_enforces_real_context_and_auto(host,tokens,model):
    with pytest.raises(app.WorkflowError):
        env.normalize(host,json.dumps({"version":"2.1.197 (Claude Code)","context_tokens":tokens,"model":model}))


def test_generated_claude_settings_file_does_not_invalidate_frozen_runtime(host,monkeypatch):
    from types import SimpleNamespace
    base={"version":"2.1.197 (Claude Code)","model":m.AUTO_MODEL,"context_tokens":1_000_000,
          "env":{"ANTHROPIC_MODEL":m.AUTO_MODEL,"CLAUDE_CODE_MAX_CONTEXT_TOKENS":"1000000"},
          "files":{"/usr/local/bin/entrypoint.sh":"fixed-entrypoint",
                   "/home/node/.claude/settings.json":""},"platform":"linux","arch":"arm64"}
    frozen=env.normalize(host,json.dumps(base))
    actual={**base,"files":{**base["files"],"/home/node/.claude/settings.json":"generated-client-state"}}
    frozen_config={**config(),"runtime":frozen}
    def probe(command,**_):
        if command[:2]==["docker","inspect"]:
            return SimpleNamespace(stdout=frozen_config["image"]+"\n")
        return SimpleNamespace(stdout=json.dumps(actual))
    monkeypatch.setattr(host,"run_command",probe)
    evidence=env.verify_container(host,"pair-test",frozen_config)
    assert evidence["harness_version"]=="2.1.197"
    assert "/home/node/.claude/settings.json" not in evidence["runtime"]["files"]
    assert evidence["runtime"]["files"]["/usr/local/bin/entrypoint.sh"]=="fixed-entrypoint"


@pytest.mark.parametrize("change",["image","entrypoint","model","context","version"])
def test_container_still_rejects_material_config_drift(host,monkeypatch,change):
    from types import SimpleNamespace
    base={"version":"2.1.197 (Claude Code)","model":m.AUTO_MODEL,"context_tokens":1_000_000,
          "env":{"ANTHROPIC_MODEL":m.AUTO_MODEL,"CLAUDE_CODE_MAX_CONTEXT_TOKENS":"1000000"},
          "files":{"/usr/local/bin/entrypoint.sh":"fixed-entrypoint"},
          "platform":"linux","arch":"arm64"}
    frozen_config={**config(),"runtime":env.normalize(host,json.dumps(base))}
    actual=json.loads(json.dumps(base))
    image=frozen_config["image"]
    if change=="image":image="sha256:changed"
    if change=="entrypoint":actual["files"]["/usr/local/bin/entrypoint.sh"]="changed"
    if change=="model":actual["model"]="other-model"
    if change=="context":actual["context_tokens"]=999_999
    if change=="version":actual["version"]="2.1.198 (Claude Code)"
    monkeypatch.setattr(host,"run_command",lambda command,**_:SimpleNamespace(
        stdout=image+"\n" if command[:2]==["docker","inspect"] else json.dumps(actual)))
    with pytest.raises(host.WorkflowError):
        env.verify_container(host,"pair-test",frozen_config)


def test_migration_is_additive_idempotent_and_backup_contains_legacy(host):
    pair_id,_,_=ready_pair(host)
    with host.db_connection() as db:
        before=db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    m.backup_database(host.DB_PATH)
    m.initialize(host);m.initialize(host)
    with host.db_connection() as db:
        assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]==before
    assert m.task(host,pair_id)["gsb"]=="Same"
    assert (host.DATA_DIR/"backups"/"before-pairwise-v1.sqlite3").is_file()


@pytest.mark.parametrize("reference_status", ["", "queued", "running", "failed", "complete"])
@pytest.mark.parametrize("confirmed", [False, True])
def test_completed_pair_state_uses_only_human_review(host, reference_status, confirmed):
    pair_id, _, _ = ready_pair(host)
    with host.db_connection() as db:
        db.execute("UPDATE pairwise_tasks SET reference_status=?,human_confirmed=? WHERE id=?",
                   (reference_status, int(confirmed), pair_id))
    pair = detail(host, pair_id, include_runtime=False)
    assert pair["state"] == ("已填写 GSB" if confirmed else "待人工 GSB")
    assert pair["reference_status"] == reference_status


def test_removed_reference_review_endpoint_cannot_queue_work(host):
    from pairwise import api
    pair_id, _, _ = ready_pair(host)
    handler = mock.Mock()
    with pytest.raises(host.WorkflowError, match="Pair-wise 接口不存在"):
        api.post(handler, host, f"/api/pairwise/{pair_id}/reference-review", {})
    handler.send_json.assert_not_called()
    assert m.task(host, pair_id)["reference_status"] == ""


def test_legacy_reference_job_does_not_block_deployment(host):
    pair_id, _, _ = ready_pair(host)
    with host.db_connection() as db:
        db.execute("UPDATE pairwise_tasks SET reference_status='running' WHERE id=?", (pair_id,))
    deployment.configure(host, True)
    state = deployment.status(host)
    assert state["blockers"]["review_jobs"] == 0
    assert state["restart_safe"]
    with host.db_connection() as db:
        db.execute("UPDATE pairwise_tasks SET difficulty_status='running' WHERE id=?", (pair_id,))
    assert deployment.status(host)["blockers"]["review_jobs"] == 1


@pytest.mark.parametrize("reference_verdict", ["A 更好", "B 更好", "Same"])
def test_iteration_source_ignores_legacy_reference_verdict(host, reference_verdict):
    from pairwise import iteration_lab
    pair_id, a, _ = ready_pair(host)
    with host.db_connection() as db:
        db.execute("UPDATE pairwise_tasks SET human_confirmed=0,gsb='',"
                   "reference_status='complete',reference_gsb=? WHERE id=?",
                   (reference_verdict, pair_id))
    tasks, attempts, _ = iteration_lab._metadata(host)
    assert iteration_lab._chosen_side(tasks[pair_id], attempts[pair_id])["id"] == a["id"]


def test_b_initial_clone_does_not_fetch_a_product_branch(host,tmp_path):
    source=tmp_path/"source"
    base=repository(source)
    git(source,"branch","B",base)
    (source/"a-result.txt").write_text("not visible to B")
    git(source,"add",".");git(source,"commit","-m","A product")
    git(source,"branch","A")
    target=tmp_path/"initial";target.mkdir()
    w.clone_initial(host,{"repo_url":str(source),"base_sha":base},target,"B")
    assert git(target,"rev-parse","HEAD")==base
    assert not (target/"a-result.txt").exists()
    assert git(target,"for-each-ref","--format=%(refname)")=="refs/heads/B"


@pytest.mark.parametrize("recover_failed",[False,True])
def test_single_turn_execution_archives_failed_verification_without_ai(host,tmp_path,monkeypatch,recover_failed):
    source=tmp_path/"source"
    base=repository(source)
    git(source,"branch","A",base);git(source,"branch","B",base)
    p=m.create(host,payload(verification_commands=["false"]))
    with host.db_connection() as db:
        db.execute("UPDATE pairwise_tasks SET repo_url=?,base_sha=? WHERE id=?",(str(source),base,p["id"]))
    w.start(host,p["id"])
    a=m.latest(m.attempts(host,p["id"]),"A")
    raw=Path(a["run_directory"])/"traces"/"-workspace"/"test-session.jsonl"
    calls=[]
    def launch(row,**kwargs):Path(row["repo_path"]).mkdir(parents=True)
    def send(run_id,screen,prompt):
        calls.append(prompt)
        work=Path(host.run_row(run_id)["repo_path"])
        git(work,"config","user.name","Test");git(work,"config","user.email","test@example.invalid")
        (work/"output.txt").write_text("real product tree")
        trace(raw,prompt,"test-session")
    def snapshot(row):return raw.parent,host.trace_turn_state(raw.parent,row["first_prompt"])
    def close(run_id,**kwargs):host.update_run(run_id,container_cleaned=1,trajectory_path=str(raw))
    def verify(commands,cwd,run_id):
        assert cwd!=Path(a["repo_path"])
        (cwd/"test-generated-file").write_text("isolated")
        return [{"command":"false","exit_code":1,"output":"expected test failure"}]
    for name,fn in {"launch_docker_terminal":launch,"wait_for_docker_container":lambda *_:None,
        "accept_container_permission_prompt":lambda *_:None,"send_prompt_to_screen":send,
        "docker_container_running":lambda *_:True,"refresh_trace_snapshot":snapshot,
        "export_and_remove_container":close,"verification_results":verify}.items():
        monkeypatch.setattr(host,name,fn)
    monkeypatch.setattr(env,"verify_container",lambda *_:{"fingerprint":env.digest(config())})
    for name in ("review_worker","final_review_worker","send_api_resume_to_screen","create_followup_turn"):
        monkeypatch.setattr(host,name,lambda *_:pytest.fail("forbidden legacy path"))
    if recover_failed:
        # Seed a completed original turn, then exercise real recovery + Git archive.
        Path(a["repo_path"]).mkdir(parents=True)
        w.clone_initial(host,m.task(host,p["id"]),Path(a["repo_path"]),"A")
        send(a["id"],"fixture",p["prompt"])
        host.update_run(a["id"],session_id="test-session")
        m.update_attempt(host,a["id"],status="failed",prompt_sent=1,error=w.LEGACY_TIMEOUT_ERROR)
        real_command=host.run_command
        def recovery_command(args,**kwargs):
            if args[0]=="docker":
                return subprocess.CompletedProcess(args,0,"","")
            return real_command(args,**kwargs)
        monkeypatch.setattr(host,"run_command",recovery_command)
        monkeypatch.setattr(host,"docker_container_running",lambda *_:False)
        monkeypatch.setattr(host,"copy_container_traces",lambda row,destination:destination)
        w.request_recovery(host,a["id"])
    w.execute(host,a["id"])
    result=m.attempt(host,a["id"])
    assert result["status"]=="complete", result["error"]
    assert calls==[p["prompt"]]
    assert result["verification"][0]["exit_code"]==1
    assert not (Path(a["repo_path"])/"test-generated-file").exists()
    assert git(Path(a["repo_path"]),"show","-s","--format=%P",result["artifact_sha"])==base


@pytest.mark.skipif(not app.ARTIFACT_NODE_EXECUTABLE.exists(),reason="Bundled spreadsheet runtime unavailable")
def test_real_xlsx_has_twenty_six_columns_and_literal_prompt(host):
    import io
    import zipfile
    from xml.etree import ElementTree as ET
    pair_id,_,_=ready_pair(host,prompt="=SUM(1,2)\n原文不得被执行或修改\n")
    content=ex.workbook(host,[pair_id])
    ns={"x":"http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        doc=ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        assert not doc.findall(".//x:f",ns)
        assert len(doc.findall(".//x:row",ns)[0].findall("x:c",ns))==26
        cell=doc.find(".//x:c[@r='A2']",ns)
        if cell.get("t")=="s":
            strings=ET.fromstring(archive.read("xl/sharedStrings.xml"))
            si=strings.findall("x:si",ns)[int(cell.find("x:v",ns).text)]
            text="".join(e.text or "" for e in si.findall(".//x:t",ns))
        elif cell.get("t")=="str":
            text=cell.find("x:v",ns).text or ""
        else:
            text="".join(e.text or "" for e in cell.findall(".//x:t",ns))
        assert text=="=SUM(1,2)\n原文不得被执行或修改\n"


def fake_seed_snapshot(host):
    path = Path(host.DATA_DIR) / "pairwise-seeded" / "test.bundle"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fixture bundle")
    return {"bundle_path": str(path), "sha": "a" * 40, "tree": "b" * 40}


def sample_bug_audit(prompt, seed, review):
    from pairwise.generation_policy import VERSION
    return {'difficulty_review': review, 'diagnosis_path': ['源码故障链'],
            'code_evidence': ['product.txt:apply'], 'snapshot': {
                'mode': 'faulty_snapshot_only', 'policy_version': VERSION,
                'sha': seed['sha'], 'tree': seed['tree'],
                'prompt_sha256': hashlib.sha256(prompt.encode('utf-8')).hexdigest()}}


def clone_archived_fixture(host, repo_url, path, sha):
    with host.db_connection() as db:
        row = db.execute('SELECT r.repo_path FROM pairwise_attempts a JOIN runs r ON r.id=a.id WHERE a.artifact_sha=?',
                         (sha,)).fetchone()
    repository(path)
    git(path, 'fetch', row['repo_path'], sha)
    git(path, 'checkout', '--detach', sha)
    return sha
