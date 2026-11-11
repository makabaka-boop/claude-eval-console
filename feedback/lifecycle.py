"""Exit a completed Session and remove only its verified, backed-up container."""
import json
import re
import threading
import time
from pathlib import Path

from . import policy, review, store, trace

LOCK=threading.RLock()
EXIT_WAIT_SECONDS=30


def identity(h,run_id):
    r=store.run(h,run_id);row=h.run_row(run_id)
    if row['container_name']!=f'feedback-{run_id}' or not re.fullmatch('[a-f0-9]{64}',r['container_id']):
        raise h.WorkflowError('未绑定可验证的本题容器身份，原现场保留')
    template='{"id":{{json .Id}},"running":{{json .State.Running}},"run_id":{{json (index .Config.Labels "feedback.console.run")}}}'
    result=h.run_command(['docker','inspect','--format',template,row['container_name']],timeout=30,check=False)
    if result.returncode:
        if re.search(r'no such (?:object|container)',str(result.stderr or result.stdout),re.I):return None
        raise h.WorkflowError('无法读取本题容器状态，请检查 Docker 后重试；原现场保留')
    data=json.loads(result.stdout)
    if data.get('id')!=r['container_id'] or data.get('run_id')!=run_id:
        raise h.WorkflowError('容器身份或归属已变化，拒绝操作替代容器')
    return data


def finish(h,run_id):
    """Ctrl+D twice only after a complete response; never kill an active task."""
    r=store.run(h,run_id)
    if r['status']!='archiving':raise h.WorkflowError('仅在完成归档阶段退出原会话')
    data=identity(h,run_id)
    if data is None:raise h.WorkflowError('原容器已不存在，无法核对退出后的完整轨迹')
    if not data['running']:return
    screen=h.run_row(run_id)['screen_name']
    for _ in range(2):
        h.run_command(['screen','-S',screen,'-p','0','-X','stuff','\x04'],timeout=20)
        time.sleep(.2)
        data=identity(h,run_id)
        if data is None:raise h.WorkflowError('原容器在导出前被移除，现场需核查')
        if not data['running']:return
    deadline=time.monotonic()+EXIT_WAIT_SECONDS
    while time.monotonic()<deadline:
        data=identity(h,run_id)
        if data is None:raise h.WorkflowError('原容器在导出前被移除，现场需核查')
        if not data['running']:return
        time.sleep(.5)
    raise h.WorkflowError('原会话未正常退出，容器保留；请核对终端后单独重试归档')


def verify_saved(h,run_id):
    r=store.run(h,run_id);t=store.task(h,r['task_id'])
    if r['status']!='archived' or not policy.SHA.fullmatch(r['artifact_sha']):
        raise h.WorkflowError('最终产物尚未归档，不能清理容器')
    path=Path(r['trace_path'])
    if not path.is_file() or review.file_digest(path)!=r['trace_sha256']:
        raise h.WorkflowError('本地完整轨迹缺失或哈希变化，容器保留')
    state=trace.inspect(path,t['prompt'],r['first_session_id'],r['resume_count'])
    if not state['complete'] or state['first_turn_id']!=r['first_turn_id']:
        raise h.WorkflowError('首轮定位或全部续写尚未完整保存，容器保留')
    workspace=Path(h.run_row(run_id)['repo_path'])
    actual=h.run_command(['git','rev-parse',f"{r['artifact_sha']}^{{tree}}"],cwd=workspace).stdout.strip()
    if actual!=r['artifact_tree'] or h.run_command(['git','merge-base','--is-ancestor',t['initial_sha'],r['artifact_sha']],cwd=workspace,check=False).returncode:
        raise h.WorkflowError('本地产物或提交历史未通过核验，容器保留')


def cleanup(h,run_id):
    """A cleanup failure does not discard an already valid archived sample."""
    with LOCK:
        r=store.run(h,run_id)
        if r['status']!='archived':raise h.WorkflowError('未归档的现场不能自动删除')
        if not r['container_id']:
            store.update(h,run_id,cleanup_status='preserved',cleanup_error='旧记录未绑定容器身份，保留现场')
            return store.run(h,run_id)
        try:
            verify_saved(h,run_id)
            data=identity(h,run_id)
            if data:
                if data['running']:raise h.WorkflowError('容器仍在运行，请先核对原会话退出状态，未执行删除')
                # Immutable ID, no --force, no volumes or host directories removed.
                h.run_command(['docker','rm',r['container_id']],timeout=30)
                if identity(h,run_id) is not None:raise h.WorkflowError('容器清理结果待核查，请重试状态检查')
            store.update(h,run_id,cleanup_status='complete',cleanup_error='',cleanup_at=h.now_text())
            store.event(h,run_id,'本地代码和完整轨迹已核验，本题已停止的容器已清理')
        except Exception as exc:
            store.update(h,run_id,cleanup_status='failed',cleanup_error=str(exc))
            store.event(h,run_id,f'容器收尾未完成，归档证据保留：{exc}')
        return store.run(h,run_id)
