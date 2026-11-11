"""Single-Session execution with explicit, auditable interruption continuations."""
import threading
import time
import uuid
from pathlib import Path

from . import policy, store, trace

LOCK = threading.RLock()
WORKERS = set()
STOP = threading.Event()


class TraceUnavailable(RuntimeError):
    """Container copy may be temporarily unavailable before its first event."""


def start(h, task_id, retry=False):
    t = store.task(h,task_id)
    with LOCK, h.db_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        previous = db.execute("SELECT * FROM feedback_runs WHERE task_id=? ORDER BY created_at DESC,id DESC LIMIT 1", (task_id,)).fetchone()
        if previous and not retry:
            return store.run(h,previous['id']),False
        if retry and (not previous or previous["status"] not in {"failed","stopped"}):
            raise h.WorkflowError("只有失败或已停止的执行可以建立新 Session 重跑；中断请使用继续")
        if retry and (previous['id'] in WORKERS or h.docker_container_running(h.run_row(previous['id'])['container_name'])):
            raise h.WorkflowError('请先停止上次执行并等待监控结束，再建立独立 Session')
        run_id = uuid.uuid4().hex[:12]
        directory = Path(h.PROJECTS_ROOT)/"domain-feedback"/task_id/run_id
        now = h.now_text()
        db.execute("""INSERT INTO runs
          (id,repo_name,model,task_type,task_difficulty,language_framework,project_directory,
           repo_path,run_directory,repo_url,base_sha,snapshot_url,phase,status_detail,
           first_prompt,container_name,screen_name,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (run_id,t["repo_name"],policy.EXECUTION_MODEL,t["task_type"],t["difficulty"],t["languages"],
           "domain-feedback",str(directory/"workspace"),str(directory),t["repo_url"],t["initial_sha"],
           t["initial_snapshot"],"queued","专项任务排队中",t["prompt"],f"feedback-{run_id}",f"feedback-{run_id}",now,now))
        db.execute("INSERT INTO run_turns(run_id,turn_number,intent_type,prompt,model,created_at,updated_at) VALUES (?,1,?,?,?,?,?)",
                   (run_id,t["task_type"],t["prompt"],policy.EXECUTION_MODEL,now,now))
        db.execute("INSERT INTO feedback_runs(id,task_id,created_at,updated_at) VALUES (?,?,?,?)",(run_id,task_id,now,now))
    store.event(h,run_id,"已建立独立执行记录；只发送原始任务一次")
    enqueue(h,run_id)
    return store.run(h,run_id),True


def reserve_resume_slot(h):
    with h.WORKER_SLOT_CONDITION:
        if h.WORKER_SLOT_ACTIVE >= h.configured_max_parallel_runs():
            raise h.WorkflowError('执行名额已满；请等待其他任务结束后继续原 Session')
        h.WORKER_SLOT_ACTIVE += 1


def release_slot(h):
    with h.WORKER_SLOT_CONDITION:
        h.WORKER_SLOT_ACTIVE=max(0,h.WORKER_SLOT_ACTIVE-1)
        h.WORKER_SLOT_CONDITION.notify_all()


def enqueue(h, run_id, reserved=False):
    with LOCK:
        if run_id in WORKERS:
            if reserved:release_slot(h)
            return
        WORKERS.add(run_id)
    def guarded():
        try:
            with h.worker_slot(reserved=reserved):
                execute(h,run_id)
        except Exception as exc:
            current = store.run(h,run_id)
            if current["status"] not in {"stopped","archive_failed"}:
                status = "interrupted" if current["prompt_sent"] and current["first_session_id"] else "failed"
                store.update(h,run_id,status=status,error=str(exc),detail="现场已保留，请检查后继续或重新归档")
                store.event(h,run_id,f"执行暂停：{exc}")
        finally:
            with LOCK:
                WORKERS.discard(run_id)
    threading.Thread(target=guarded,name=f"feedback-{run_id}",daemon=True).start()


def active(h, run_id):
    return not STOP.is_set() and store.run(h,run_id)["status"] not in {"stopped","failed"}


def execute(h, run_id):
    from pairwise import environment
    r = store.run(h,run_id)
    t = store.task(h,r["task_id"])
    row = h.run_row(run_id)
    workspace = Path(row["repo_path"])
    if not r["prompt_sent"]:
        # Only a newly queued run may launch. A crash during launch is never
        # interpreted as permission to create another container or send again.
        if r["status"] != "queued":
            raise h.WorkflowError("启动现场需要人工检查，不能自动重复创建容器")
        store.update(h,run_id,status="starting",detail="冻结甲方执行模型、镜像、Key 摘要与 1M 配置")
        config = environment.freeze(h)
        if config["model"] != policy.EXECUTION_MODEL or config["context_tokens"] != policy.CONTEXT_TOKENS:
            raise h.WorkflowError("执行配置不符合甲方模型/1M 要求")
        store.update(h,run_id,config=config)
        if not active(h,run_id):
            return
        h.launch_docker_terminal(row,image=config["image"])
        h.wait_for_docker_container(run_id,row["container_name"])
        h.accept_container_permission_prompt(run_id,row["screen_name"],row["container_name"])
        environment.verify_host(h,config)
        environment.verify_container(h,row["container_name"],config,policy.EXECUTION_MODEL)
        if not active(h,run_id):
            return
        h.run_command(["git","clone","--no-checkout",t["repo_url"],str(workspace)],timeout=180)
        h.run_command(["git","checkout","-b",f"feedback/{run_id}",t["initial_sha"]],cwd=workspace)
        if h.run_command(["git","status","--porcelain"],cwd=workspace).stdout.strip():
            raise h.WorkflowError("起始工作区不干净")
        for attachment in t["attachments"]:
            h.run_command(["docker","exec",row["container_name"],"mkdir","-p","/tmp/feedback-inputs"])
            h.run_command(["docker","cp",attachment["path"],f"{row['container_name']}:/tmp/feedback-inputs/{attachment['name']}"])
        with LOCK, h.db_connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT status,prompt_sent FROM feedback_runs WHERE id=?",(run_id,)).fetchone()
            if STOP.is_set() or current["status"] != "starting" or current["prompt_sent"]:
                return
            db.execute("UPDATE feedback_runs SET prompt_sent=1,status='running',detail=?,updated_at=? WHERE id=?",
                       ("首轮执行中，只读监控",h.now_text(),run_id))
        # At-most-once delivery: even a failed send cannot authorize a resend.
        h.update_run(run_id,phase="first_running",harness_version=config["harness_version"])
        h.send_prompt_to_screen(run_id,row["screen_name"],t["prompt"])
        store.event(h,run_id,"原始 Prompt 已提交；后续只允许登记的中断继续")
    monitor(h,run_id)


def sync_trace(h, run_id):
    r = store.run(h,run_id)
    t = store.task(h,r["task_id"])
    row = h.run_row(run_id)
    directory = Path(row["run_directory"])/"traces"
    try:
        h.copy_container_traces(row,directory)
    except h.WorkflowError as exc:
        raise TraceUnavailable(str(exc)) from exc
    path,state = trace.locate(directory,t["prompt"],r["first_session_id"],r["resume_count"])
    if state is None:
        return None
    if r["first_turn_id"] and state["first_turn_id"] != r["first_turn_id"]:
        raise h.WorkflowError("首轮 PromptID 发生变化，停止归并")
    errors = state["errors"]
    if errors:
        raise h.WorkflowError("；".join(errors))
    with h.db_connection() as db:
        previous=db.execute('SELECT id FROM feedback_runs WHERE first_session_id=? AND id<>?',(state['session_id'],run_id)).fetchone()
    if previous:raise h.WorkflowError('该 Session 已绑定其他执行，不能合并独立样本')
    fields = {"first_session_id":state["session_id"],"first_turn_id":state["first_turn_id"],
              "trace_path":str(path),"trace_sha256":state["sha256"],"trace_state":state}
    if state["sha256"] != r["trace_sha256"]:
        fields.update(review_confirmed=0,analysis_status="stale" if r["analysis_source"] else "pending")
    store.update(h,run_id,**fields)
    h.update_run(run_id,session_id=state["session_id"],first_prompt_id=state["first_turn_id"],trajectory_path=str(path))
    h.update_turn(run_id,1,prompt_id=state["first_turn_id"])
    with h.db_connection() as db:
        for number,c in enumerate(state["continuations"],1):
            db.execute("UPDATE feedback_continuations SET prompt_id=?,status='observed' WHERE run_id=? AND number=?",
                       (c["prompt_id"],run_id,number))
        if state["continuations"]:
            note = f"模型自行中断，已在原 Session 继续 {len(state['continuations'])} 次；提交定位仍为首轮，评价覆盖全部续写。"
            existing = r["other_issues"]
            # Replace the generated interruption summary; preserve manual notes.
            manual = "\n".join(line for line in existing.splitlines() if not line.startswith("模型自行中断，已在原 Session 继续"))
            db.execute("UPDATE feedback_runs SET other_issues=? WHERE id=?",(note+("\n"+manual if manual else ""),run_id))
    return state


def monitor(h, run_id):
    started = time.monotonic()
    while active(h,run_id):
        r = store.run(h,run_id)
        if r["status"] not in {"running","resuming"}:
            return
        row = h.run_row(run_id)
        running = h.docker_container_running(row["container_name"])
        try:
            state = sync_trace(h,run_id)
        except TraceUnavailable:
            if not running:
                raise
            time.sleep(h.POLL_SECONDS)
            continue
        if state:
            if state["api_error"]:
                store.update(h,run_id,status="failed",detail="API 工程故障，现场保留；可创建新 Session 重跑",error=state["api_error"])
                return
            if state["interrupted"]:
                store.update(h,run_id,status="interrupted",detail="模型自行中断，可在原 Session 继续")
                store.event(h,run_id,"检测到模型中断，等待操作人员选择继续")
                return
            if state["complete"]:
                archive(h,run_id)
                return
        if not running:
            store.update(h,run_id,status="interrupted",detail="容器停止，现场保留；需恢复原会话或停止后独立重跑")
            return
        if time.monotonic()-started > h.RUN_TIMEOUT_SECONDS:
            store.update(h,run_id,status="interrupted",detail="达到只读监控时间上限，现场保留")
            return
        time.sleep(h.POLL_SECONDS)


def resume(h, run_id, reason):
    from pairwise import environment
    reason = str(reason or "").strip()
    if not reason:
        raise h.WorkflowError("请填写模型自行中断的实际情况")
    with LOCK:
        r = store.run(h,run_id)
        row = h.run_row(run_id)
        if r["status"] != "interrupted" or not r["first_session_id"]:
            raise h.WorkflowError("只有已定位原 Session 的中断任务可以继续")
        if not h.docker_container_running(row["container_name"]):
            raise h.WorkflowError("原容器未运行，不能向另一个 Session 发送继续；请保留现场处理")
        if run_id in WORKERS:
            raise h.WorkflowError("上一次监控正在收尾，请稍后再试")
        if any(c['status'] != 'observed' for c in r['continuations']):
            raise h.WorkflowError('上一条继续的发送结果尚未在轨迹确认，请先同步轨迹，不能重复发送')
        frozen=store.raw_config(h,run_id)
        environment.verify_host(h,frozen)
        environment.verify_container(h,row['container_name'],frozen,policy.EXECUTION_MODEL)
        # Reserve before sending: resumed generation also obeys a limit of one.
        reserve_resume_slot(h)
        try:
            with h.db_connection() as db:
                db.execute("BEGIN IMMEDIATE")
                current = db.execute("SELECT status,resume_count FROM feedback_runs WHERE id=?",(run_id,)).fetchone()
                if current["status"] != "interrupted":
                    raise h.WorkflowError("继续已登记，请勿重复发送")
                number = current["resume_count"]+1
                db.execute("INSERT INTO feedback_continuations(run_id,number,reason,created_at) VALUES (?,?,?,?)",(run_id,number,reason,h.now_text()))
                db.execute("""UPDATE feedback_runs SET status='resuming',resume_count=?,detail=?,
                     analysis_status='stale',review_confirmed=0,recording_confirmed=0,updated_at=? WHERE id=?""",
                     (number,"正在原 Session 继续，首轮提交 ID 保持不变",h.now_text(),run_id))
            h.send_prompt_to_screen(run_id,row["screen_name"],"继续")
        except Exception as exc:
            release_slot(h)
            store.update(h,run_id,status="interrupted",error=f"继续发送状态待核查，不能直接重发：{exc}")
            raise h.WorkflowError("继续已登记但发送状态待核查，请先同步轨迹，不能重复点击发送") from exc
        store.event(h,run_id,f"已登记第 {number} 次继续：{reason}")
        enqueue(h,run_id,reserved=True)
    return store.run(h,run_id)


def mark_interrupted(h, run_id, reason):
    r = store.run(h,run_id)
    if r["status"] not in {"running","resuming"} or not str(reason or "").strip():
        raise h.WorkflowError("请在运行期间确认实际中断原因")
    state = sync_trace(h,run_id)
    if not state or not state["first_turn_id"] or state["complete"]:
        raise h.WorkflowError("轨迹未定位或任务已完成，不能标记为中断")
    if "esc to interrupt" in h.terminal_screen_text(run_id,h.run_row(run_id)["screen_name"]).casefold():
        raise h.WorkflowError("模型仍在执行，不能标记为自行中断")
    store.update(h,run_id,status="interrupted",detail=str(reason),error="")
    store.event(h,run_id,f"人工确认模型自行中断：{reason}")
    return store.run(h,run_id)


def checkpoint(h, run_id):
    r = store.run(h,run_id)
    t = store.task(h,r["task_id"])
    workspace = Path(h.run_row(run_id)["repo_path"])
    if not workspace.is_dir():
        raise h.WorkflowError("没有可归档的工作区")
    def git(*args,**kwargs):
        return h.run_command(["git",*args],cwd=workspace,**kwargs)
    if git("merge-base","--is-ancestor",t["initial_sha"],"HEAD",check=False).returncode:
        raise h.WorkflowError("初始快照不是当前模型提交的祖先，不能归档")
    # Stage source changes while excluding dependencies and credential files.
    git("add","-A")
    names = git("diff","--cached","--name-only","-z").stdout.split("\0")
    excluded = []
    for name in filter(None,names):
        parts = Path(name).parts
        if any(p in {"node_modules",".venv","venv","__pycache__",".pytest_cache",".git","dist","target"} for p in parts):
            excluded.append(name)
        if any(p==".env" or (p.startswith('.env.') and not p.endswith(('.example','.sample','.template'))) or p in {"id_rsa","id_ed25519"} or p.endswith(('.pem','.key')) for p in parts):
            raise h.WorkflowError(f"待归档内容含可能的凭据文件，请先处理并加入忽略规则：{name}")
    for offset in range(0,len(excluded),100):
        git("reset","--quiet","HEAD","--",*excluded[offset:offset+100])
    if git("diff","--cached","--quiet",check=False).returncode:
        git("-c","user.name=Feedback Console","-c","user.email=feedback@localhost",
            "commit","-m",f"Archive domain feedback {run_id}\n\nSession-ID: {r['first_session_id']}\nPrompt-ID: {r['first_turn_id']}")
    sha = git("rev-parse","HEAD").stdout.strip()
    if not policy.SHA.fullmatch(sha):
        raise h.WorkflowError("无法取得最终完整 SHA")
    # Push the full model commit chain to a task-specific branch; no forced update.
    git("push",t["repo_url"],f"{sha}:refs/heads/feedback/{run_id}",timeout=180)
    return sha,git("rev-parse",f"{sha}^{{tree}}").stdout.strip()


def archive(h, run_id):
    r = store.run(h,run_id)
    if r["status"] not in {"running","resuming","archiving","archive_failed"}:
        raise h.WorkflowError("任务尚未完成，不能归档最终产物")
    state = sync_trace(h,run_id)
    if not state or not state["complete"]:
        raise h.WorkflowError("缺少全部续写后的完成边界，不能用早期回复归档")
    current=store.run(h,run_id);task=store.task(h,current['task_id'])
    strict=trace.inspect(Path(current['trace_path']),task['prompt'],current['first_session_id'],current['resume_count'])
    if not strict['complete']:raise h.WorkflowError('原始 JSONL 尚未完整写入，保留现场后重新核验')
    store.update(h,run_id,status="archiving",detail="保留模型提交历史并归档完整轨迹")
    try:
        sha,tree = checkpoint(h,run_id)
        store.update(h,run_id,status="archived",detail="证据已归档，可关联完整录屏并分析",artifact_sha=sha,artifact_tree=tree,error="",review_confirmed=0)
        h.update_run(run_id,phase="first_idle",first_result=state["result"])
        store.event(h,run_id,f"最终快照 {sha}，完整轨迹已保存；未改写模型提交历史")
    except Exception as exc:
        store.update(h,run_id,status="archive_failed",detail="归档失败，原现场保留，可单独重试归档",error=str(exc))
        raise
    return store.run(h,run_id)


def stop(h, run_id):
    with LOCK:return _stop(h,run_id)


def _stop(h, run_id):
    r = store.run(h,run_id)
    if r["status"] == "archived":
        raise h.WorkflowError("已归档任务无需停止；不能为择优结果直接重跑")
    store.update(h,run_id,status="stopped",detail="已停止，现场与轨迹保留")
    row = h.run_row(run_id)
    if row["container_name"] and h.docker_container_running(row["container_name"]):
        try:
            sync_trace(h,run_id)
        except Exception:
            pass
        h.run_command(["docker","stop","--time","10",row["container_name"]],timeout=30,check=False)
    h.update_run(run_id,phase="stopped",status_detail="专项任务停止，未删除现场")
    return store.run(h,run_id)


def prepare_recording(h,run_id):
    """Prepare the exact archived source; never execute a guessed demo script."""
    r=store.run(h,run_id)
    if r['status']!='archived':raise h.WorkflowError('请先归档最终产物')
    row=h.run_row(run_id)
    destination=Path(row['run_directory'])/'recording-workspace'
    if not destination.exists():
        h.run_command(['git','clone','--shared','--no-checkout',row['repo_path'],str(destination)],timeout=120)
        h.run_command(['git','checkout','--detach',r['artifact_sha']],cwd=destination)
    head=h.run_command(['git','rev-parse','HEAD'],cwd=destination).stdout.strip()
    dirty=h.run_command(['git','status','--porcelain'],cwd=destination).stdout.strip()
    if head!=r['artifact_sha'] or dirty:
        raise h.WorkflowError(f'录屏目录已改变，请保留现场并人工核对，系统不会覆盖：{destination}')
    store.event(h,run_id,'已准备最终 SHA 的干净录屏目录，等待实际运行并录制')
    return {'directory':str(destination),'artifact_sha':r['artifact_sha'],
            'note':'在此目录使用题目真实环境启动并完整录屏（≤90秒），失败过程也保留。'}


def recover(h):
    STOP.clear()
    with h.db_connection() as db:
        rows = db.execute("SELECT id,status,prompt_sent FROM feedback_runs WHERE status IN ('queued','starting','running','resuming','archiving')").fetchall()
        db.execute("UPDATE feedback_runs SET analysis_status='failed',analysis_error='服务重启，可重试分析' WHERE analysis_status='running'")
    # Count detached execution first, before any newly queued task can launch.
    detached=[row for row in rows if row['prompt_sent'] and row['status'] in {'running','resuming'}]
    for row in detached:h.reserve_existing_worker_slot()
    for row in rows:
        if row in detached:
            enqueue(h,row['id'],reserved=True)
        elif row["status"] == "queued":
            enqueue(h,row["id"])
        else:
            store.update(h,row["id"],status="archive_failed" if row["status"]=="archiving" else "failed",detail="重启现场已保留；启动状态不明时不自动重发 Prompt")


def shutdown():
    STOP.set()
