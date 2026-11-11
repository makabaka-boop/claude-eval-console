"""Isolated regression coverage; no vendor/model calls or live user-domain choice."""
import io
import json
import shutil
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

import app
from feedback import analysis, export, media, policy, review, store, trace, workflow
from pairwise import models as legacy
from pairwise import environment


@pytest.fixture
def host(tmp_path,monkeypatch):
    for key,value in {'DATA_DIR':tmp_path/'data','DB_PATH':tmp_path/'data/test.db',
        'PROJECTS_ROOT':tmp_path/'projects','TERMINAL_ASSETS_DIR':tmp_path/'terminal',
        'HISTORY_PATH':tmp_path/'history.md','PAIRWISE_HISTORY_PATH':tmp_path/'legacy.md',
        'SUBMITTER_NAME':'fixture-operator','FEEDBACK_MODE':True,'PAIRWISE_MODE':False}.items():
        monkeypatch.setattr(app,key,value)
    app.initialize_database();store.initialize(app);legacy.initialize(app)
    monkeypatch.setattr(app,'WORKER_SLOT_ACTIVE',0)
    def no_worker(h,run_id,reserved=False):
        if reserved:workflow.release_slot(h)
    monkeypatch.setattr(workflow,'enqueue',no_worker)
    monkeypatch.setattr(environment,'verify_host',lambda *_:None)
    monkeypatch.setattr(environment,'verify_container',lambda *_:None)
    workflow.STOP.clear();workflow.WORKERS.clear()
    return app


def payload(**changes):
    return {'title':'可恢复的数据投递','prompt':'  实现投递重放\n保留全部结果。\n',
            'subdirection':'数仓与数据湖','task_type':'功能开发','difficulty':'困难',
            'languages':'Python','initial_snapshot':'https://github.com/fixture/project/commit/'+'a'*40,**changes}


def create(h,**changes):
    store.select_domain(h,'数据工程')
    return store.create(h,payload(**changes))


def config():
    return {'model':policy.EXECUTION_MODEL,'context_tokens':1_000_000,'harness':'Claude Code',
            'harness_version':'2.1.197','operating_system':'macOS','credential':'private-fingerprint'}


def user(text,prompt_id,session='session-fixture'):
    return {'type':'user','sessionId':session,'promptId':prompt_id,'message':{'role':'user','content':text}}


def assistant(text='交付完成',reason='end_turn'):
    return {'type':'assistant','sessionId':'session-fixture','message':{'role':'assistant','content':[{'type':'text','text':text}],'stop_reason':reason}}


def boundary():return {'type':'system','subtype':'turn_duration','sessionId':'session-fixture'}


def write_trace(directory,events):
    directory.mkdir(parents=True,exist_ok=True);path=directory/(events[0]['sessionId']+'.jsonl')
    path.write_text(''.join(json.dumps(e,ensure_ascii=False)+'\n' for e in events))
    return path


def movie(seconds):
    def box(kind,data):return struct.pack('>I4s',8+len(data),kind)+data
    header=b'\0'*12+struct.pack('>II',1000,int(seconds*1000))+b'\0'*80
    return box(b'ftyp',b'isom\0\0\0\0isom')+box(b'moov',box(b'mvhd',header))+box(b'mdat',b'fixture-payload')


def git(path,*args):return subprocess.run(['git',*args],cwd=path,text=True,capture_output=True,check=True).stdout.strip()


@pytest.fixture
def archived(host,tmp_path,monkeypatch):
    source=tmp_path/'source';source.mkdir();git(source,'init');git(source,'config','user.name','Fixture');git(source,'config','user.email','fixture@localhost')
    (source/'README.md').write_text('initial fixture\n');git(source,'add','.');git(source,'commit','-m','initial');base=git(source,'rev-parse','HEAD')
    task=create(host,initial_snapshot='https://github.com/fixture/project/commit/'+base)
    run,_=workflow.start(host,task['id']);workspace=Path(host.run_row(run['id'])['repo_path']);workspace.parent.mkdir(parents=True)
    subprocess.run(['git','clone',str(source),str(workspace)],check=True,capture_output=True)
    git(workspace,'config','user.name','Fixture');git(workspace,'config','user.email','fixture@localhost')
    (workspace/'README.md').write_text('model intermediate commit\n');git(workspace,'add','.');git(workspace,'commit','-m','model checkpoint');intermediate=git(workspace,'rev-parse','HEAD')
    (workspace/'product.py').write_text('print("fixture output")\n')
    remote=tmp_path/'remote.git';subprocess.run(['git','init','--bare',str(remote)],check=True,capture_output=True)
    actual=host.run_command
    def command(argv,**kwargs):
        if argv[:2]==['git','push']:argv=[*argv[:2],str(remote),*argv[3:]]
        return actual(argv,**kwargs)
    monkeypatch.setattr(host,'run_command',command)
    trace_root=tmp_path/'raw-traces';raw=write_trace(trace_root,[user(task['prompt'],'first-id'),assistant(),boundary()])
    monkeypatch.setattr(host,'copy_container_traces',lambda row,dest:shutil.copytree(trace_root,dest,dirs_exist_ok=True))
    store.update(host,run['id'],status='running',prompt_sent=1,config=config())
    result=workflow.archive(host,run['id'])
    return {'h':host,'task':task,'run':result,'workspace':workspace,'remote':remote,'intermediate':intermediate,'base':base,'raw':raw}


def attach_video(fixture,seconds=20):
    data=movie(seconds)
    return media.recording(fixture['h'],fixture['run']['id'],io.BytesIO(data),len(data),'真实运行.mp4')


def result():
    return {'delivery_score':1,'result_feedback':'实际交付仍有错误，核心投递没有完成。',
            'process_feedback':'模型检查过现有源码，但没有覆盖关键重放分支。',
            'other_issues':'','evidence_complete':True,'test_evidence':[],
            'evidence':[{'area':'result','path':'artifact/README.md','line':1,'quote':'model intermediate'},
                        {'area':'process','path':'trajectory.jsonl','line':1,'quote':'sessionId'}]}


def approved(fixture):
    h=fixture['h'];rid=fixture['run']['id'];attach_video(fixture)
    draft=result();source=store.fingerprint(h,rid)
    draft['other_issues']=store.run(h,rid)['other_issues']
    store.update(h,rid,analysis_status='complete',analysis=draft,analysis_model=policy.ANALYSIS_MODEL,
                 analysis_effort='high',analysis_source=source,result_feedback=draft['result_feedback'],
                 process_feedback=draft['process_feedback'],delivery_score=1)
    return review.save(h,rid,{**draft,'confirm':True,'recording_confirmed':True,'evidence_source':source})


def test_profile_deferred_and_migration_idempotent(host):
    assert store.profile(host)=={'operator':'fixture-operator','domain':'','locked':False}
    with pytest.raises(host.WorkflowError,match='实际出题前'):store.create(host,payload())
    store.initialize(host);assert not store.profile(host)['locked']
    store.select_domain(host,'数据工程');store.select_domain(host,'数据工程')
    with pytest.raises(host.WorkflowError,match='已固定'):store.select_domain(host,'安全')


@pytest.mark.parametrize('changes',[{'domain':'安全'},{'subdirection':'游戏开发'},{'task_type':'Feature 迭代'},
    {'difficulty':'中等'},{'difficulty':'简单'},{'initial_snapshot':'https://github.com/a/b/commit/abcd'}])
def test_reject_incorrect_fields(host,changes):
    store.select_domain(host,'数据工程')
    with pytest.raises(host.WorkflowError):store.create(host,payload(**changes))


def test_same_repo_multiple_tasks_and_exact_prompt(host):
    first=create(host);second=create(host)
    assert first['id']!=second['id'];assert first['repo_url']==second['repo_url']
    assert first['prompt']==payload()['prompt'];assert first['reproducibility']==''
    assert len(policy.TASK_TYPES)==11


def test_concurrent_start_creates_one_run(host):
    task=create(host)
    with ThreadPoolExecutor(max_workers=6) as pool:
        items=list(pool.map(lambda _:workflow.start(host,task['id']),range(6)))
    assert sum(created for _,created in items)==1
    assert len({r['id'] for r,_ in items})==1
    assert all(isinstance(r['config'],dict) for r,_ in items)
    with host.db_connection() as db:
        assert db.execute('SELECT COUNT(*) FROM runs').fetchone()[0]==1
        assert db.execute('SELECT COUNT(*) FROM run_turns').fetchone()[0]==1


def test_retry_new_session_preserves_old_and_requires_stop(host,monkeypatch):
    task=create(host);first,_=workflow.start(host,task['id'])
    with pytest.raises(host.WorkflowError):workflow.start(host,task['id'],retry=True)
    store.update(host,first['id'],status='failed',first_session_id='old-session')
    monkeypatch.setattr(host,'docker_container_running',lambda _:True)
    with pytest.raises(host.WorkflowError,match='先停止'):workflow.start(host,task['id'],retry=True)
    monkeypatch.setattr(host,'docker_container_running',lambda _:False)
    second,created=workflow.start(host,task['id'],retry=True)
    assert created and first['id']!=second['id'];assert store.run(host,first['id'])['first_session_id']=='old-session'
    assert len(store.task(host,task['id'])['runs'])==2


def test_resume_sends_exact_continue_once_and_retains_first_ids(host,monkeypatch):
    task=create(host);r,_=workflow.start(host,task['id']);rid=r['id'];sent=[]
    store.update(host,rid,status='interrupted',prompt_sent=1,first_session_id='session-fixture',first_turn_id='first-id')
    monkeypatch.setattr(host,'docker_container_running',lambda _:True)
    monkeypatch.setattr(host,'send_prompt_to_screen',lambda run,screen,prompt:sent.append(prompt))
    workflow.resume(host,rid,'模型自行停止输出')
    with pytest.raises(host.WorkflowError):workflow.resume(host,rid,'重复点击')
    assert sent==['继续'];assert store.run(host,rid)['first_turn_id']=='first-id'
    with host.db_connection() as db:db.execute("UPDATE feedback_continuations SET status='observed',prompt_id='continue-1' WHERE run_id=?",(rid,))
    store.update(host,rid,status='interrupted')
    workflow.resume(host,rid,'再次自行中断')
    assert sent==['继续','继续'];assert len(store.task(host,task['id'])['runs'])==1


def test_uncertain_continue_cannot_be_resent(host,monkeypatch):
    task=create(host);r,_=workflow.start(host,task['id']);rid=r['id']
    store.update(host,rid,status='interrupted',first_session_id='session-fixture',first_turn_id='first-id')
    monkeypatch.setattr(host,'docker_container_running',lambda _:True)
    calls=[]
    def uncertain(*args):calls.append(args);raise host.WorkflowError('screen disconnected after send')
    monkeypatch.setattr(host,'send_prompt_to_screen',uncertain)
    with pytest.raises(host.WorkflowError,match='不能重复'):workflow.resume(host,rid,'自行中断')
    with pytest.raises(host.WorkflowError,match='不能重复'):workflow.resume(host,rid,'再次点击')
    assert len(calls)==1;assert store.run(host,rid)['resume_count']==1


def test_resume_waits_for_single_execution_slot_before_sending(host,monkeypatch):
    task=create(host);r,_=workflow.start(host,task['id']);rid=r['id'];calls=[]
    store.update(host,rid,status='interrupted',first_session_id='session-fixture',first_turn_id='first-id')
    monkeypatch.setattr(host,'docker_container_running',lambda _:True)
    monkeypatch.setattr(host,'send_prompt_to_screen',lambda *args:calls.append(args))
    host.set_max_parallel_runs({'max_parallel':1});monkeypatch.setattr(host,'WORKER_SLOT_ACTIVE',1)
    with pytest.raises(host.WorkflowError,match='名额已满'):workflow.resume(host,rid,'模型自行中断')
    assert not calls;assert store.run(host,rid)['resume_count']==0
    host.WORKER_SLOT_ACTIVE=0;workflow.resume(host,rid,'名额空闲后继续')
    assert len(calls)==1;assert host.WORKER_SLOT_ACTIVE==0


def test_recovery_reserves_detached_runs_before_queued_work(host,monkeypatch):
    running,_=workflow.start(host,create(host)['id']);queued,_=workflow.start(host,create(host)['id'])
    store.update(host,running['id'],status='running',prompt_sent=1)
    calls=[]
    monkeypatch.setattr(workflow,'enqueue',lambda h,rid,reserved=False:calls.append((rid,reserved,h.WORKER_SLOT_ACTIVE)))
    workflow.recover(host)
    assert all(count==1 for _,_,count in calls)
    assert (running['id'],True,1) in calls;assert (queued['id'],False,1) in calls


def test_trace_all_continuations_preserve_original(tmp_path):
    prompt=payload()['prompt'];events=[user(prompt,'first-id'),assistant('部分完成','max_tokens'),boundary(),
      user('继续','c1'),assistant('继续部分','pause_turn'),boundary(),user('继续','c2'),assistant('最终结果'),boundary()]
    path=write_trace(tmp_path,events);state=trace.inspect(path,prompt,approved_resumes=2)
    assert state['complete'];assert state['first_turn_id']=='first-id';assert state['result']=='最终结果'
    assert [c['prompt_id'] for c in state['continuations']]==['c1','c2']
    assert not trace.inspect(path,prompt,approved_resumes=0)['complete']


def test_tool_result_with_reminder_is_not_a_business_prompt(tmp_path):
    prompt=payload()['prompt']
    tool={'type':'user','sessionId':'session-fixture','message':{'content':[
        {'type':'tool_result','content':'test output'},{'type':'text','text':'system reminder'}]}}
    path=write_trace(tmp_path,[user(prompt,'first-id'),assistant('读取源码','tool_use'),tool,assistant(),boundary()])
    state=trace.inspect(path,prompt);assert state['complete'];assert state['user_turns']==1


def test_pending_continue_does_not_reuse_early_completion_or_interruption(tmp_path):
    prompt=payload()['prompt'];path=write_trace(tmp_path,[user(prompt,'first-id'),assistant(),boundary()])
    assert not trace.inspect(path,prompt,approved_resumes=1)['complete']
    path=write_trace(tmp_path,[user(prompt,'first-id'),assistant('部分','max_tokens'),boundary()])
    state=trace.inspect(path,prompt,approved_resumes=1);assert not state['interrupted']


@pytest.mark.parametrize('edit', ['business','missing-id','wrong-session','prompt-space','no-boundary','duplicate-id'])
def test_trace_rejects_invalid_evidence(tmp_path,edit):
    prompt=payload()['prompt'];events=[user(prompt,'first-id'),assistant(),boundary()]
    if edit=='business':events.extend([user('另外帮我修复一个问题','c1'),assistant(),boundary()])
    if edit=='missing-id':events[0].pop('promptId')
    if edit=='wrong-session':events[0]['sessionId']='other-session'
    if edit=='prompt-space':events[0]['message']['content']=prompt.strip()
    if edit=='no-boundary':events.pop()
    if edit=='duplicate-id':events.extend([user('继续','first-id'),assistant(),boundary()])
    assert not trace.inspect(write_trace(tmp_path,events),prompt,approved_resumes=1 if edit in {'business','duplicate-id'} else 0)['complete']


def test_live_partial_line_not_accepted_as_archive(tmp_path):
    prompt=payload()['prompt'];path=write_trace(tmp_path,[user(prompt,'first-id'),assistant(),boundary()])
    with path.open('ab') as stream:stream.write(b'{"partial"')
    assert trace.inspect(path,prompt,live=True)['complete']
    assert not trace.inspect(path,prompt)['complete']


def test_git_archive_preserves_intermediate_and_published_tree(archived):
    f=archived;r=f['run'];assert r['status']=='archived'
    assert git(f['workspace'],'rev-parse',r['artifact_sha']+'^')==f['intermediate']
    assert git(f['workspace'],'merge-base',f['base'],r['artifact_sha'])==f['base']
    assert git(f['remote'],'rev-parse','refs/heads/feedback/'+r['id'])==r['artifact_sha']
    assert r['first_turn_id']=='first-id';assert r['config'].get('credential') is None
    assert store.raw_config(f['h'],r['id'])['credential']=='private-fingerprint'


def test_two_interruptions_execution_to_review_and_export(archived,tmp_path,monkeypatch):
    """Real SQLite/Git/JSONL/media/export; synthetic vendor responses only."""
    from pairwise import environment
    f=archived;h=f['h'];task=create(h,initial_snapshot=f['task']['initial_snapshot'])
    r,_=workflow.start(h,task['id']);rid=r['id'];row=h.run_row(rid);workspace=Path(row['repo_path'])
    source=tmp_path/'two-interruptions';events=[];sent=[];commits=[]
    monkeypatch.setattr(environment,'freeze',lambda _:{**config(),'image':'sha256:fixture'})
    monkeypatch.setattr(environment,'verify_host',lambda *_:None)
    monkeypatch.setattr(environment,'verify_container',lambda *_:None)
    def launch(row,**_):
        path=Path(row['repo_path']);assert not path.exists() or not any(path.iterdir());path.mkdir(parents=True,exist_ok=True)
    monkeypatch.setattr(h,'launch_docker_terminal',launch)
    monkeypatch.setattr(h,'wait_for_docker_container',lambda *_:None)
    monkeypatch.setattr(h,'accept_container_permission_prompt',lambda *_:None)
    monkeypatch.setattr(h,'docker_container_running',lambda _:True)
    previous=h.run_command
    def command(argv,**kwargs):
        if argv[:3]==['git','clone','--no-checkout']:argv=[*argv[:3],str(f['workspace']),argv[-1]]
        return previous(argv,**kwargs)
    monkeypatch.setattr(h,'run_command',command)
    monkeypatch.setattr(h,'copy_container_traces',lambda row,dest:shutil.copytree(source,dest,dirs_exist_ok=True))
    def send(run_id,screen,prompt):
        state=store.run(h,run_id);assert state['prompt_sent'];sent.append(prompt)
        number=len(sent);events.extend([user(prompt,'first-id' if number==1 else f'continue-{number-1}'),
            assistant('全部完成' if number==3 else '部分交付','end_turn' if number==3 else 'max_tokens'),boundary()])
        for event in events:event['sessionId']='session-two'
        write_trace(source,events)
        (workspace/'README.md').write_text(f'model intermediate commit {number}\n')
        git(workspace,'-c','user.name=Fixture','-c','user.email=fixture@localhost','add','.')
        git(workspace,'-c','user.name=Fixture','-c','user.email=fixture@localhost','commit','-m',f'model step {number}')
        commits.append(git(workspace,'rev-parse','HEAD'))
    monkeypatch.setattr(h,'send_prompt_to_screen',send)
    workflow.execute(h,rid);assert store.run(h,rid)['status']=='interrupted'
    workflow.resume(h,rid,'第一次模型自行中断');workflow.execute(h,rid)
    assert store.run(h,rid)['status']=='interrupted'
    workflow.resume(h,rid,'第二次模型自行中断');workflow.execute(h,rid)
    r=store.run(h,rid);assert r['status']=='archived';assert sent==[task['prompt'],'继续','继续']
    assert r['first_turn_id']=='first-id';assert r['resume_count']==2
    assert [c['prompt_id'] for c in r['continuations']]==['continue-1','continue-2']
    assert len(store.task(h,task['id'])['runs'])==1
    for commit in commits:git(workspace,'merge-base','--is-ancestor',commit,r['artifact_sha'])
    complete={**f,'task':task,'run':r,'workspace':workspace,'raw':source/'session-two.jsonl'}
    attach_video(complete);root=tmp_path/'analysis-all';root.mkdir();manifest=analysis.prepare(h,rid,root)
    assert len(manifest['continuations'])==2;assert (root/'trajectory.jsonl').read_bytes()==complete['raw'].read_bytes()
    confirmed=approved(complete);data=json.loads(export.bundle(h,[rid]));assert len(data['rows'])==1
    assert data['rows'][0][12]=='first-id';assert '2 次' in data['rows'][0][18]


def test_clean_recording_workspace_preserves_original_and_refuses_overwrite(archived):
    f=archived;prepared=workflow.prepare_recording(f['h'],f['run']['id']);directory=Path(prepared['directory'])
    assert git(directory,'rev-parse','HEAD')==f['run']['artifact_sha']
    (directory/'README.md').write_text('user edited recording copy\n')
    with pytest.raises(f['h'].WorkflowError,match='不会覆盖'):workflow.prepare_recording(f['h'],f['run']['id'])
    assert (f['workspace']/'README.md').read_text()=='model intermediate commit\n'


def test_checkpoint_rejects_rewritten_history(archived):
    f=archived;git(f['workspace'],'checkout','--orphan','bad-history');git(f['workspace'],'add','.');git(f['workspace'],'commit','-m','unrelated')
    with pytest.raises(f['h'].WorkflowError,match='祖先'):workflow.checkpoint(f['h'],f['run']['id'])


def test_attachment_immutable_after_start(host):
    t=create(host);data=b'prompt attachment'
    t=media.attachment(host,t['id'],io.BytesIO(data),len(data),'说明.txt')
    assert t['attachments'][0]['container_path']=='/tmp/feedback-inputs/说明.txt'
    workflow.start(host,t['id'])
    with pytest.raises(host.WorkflowError,match='不能修改'):media.attachment(host,t['id'],io.BytesIO(data),len(data),'other.txt')


def test_upload_short_stream_not_registered(host):
    task=create(host)
    with pytest.raises(ValueError,match='上传未完成'):media.attachment(host,task['id'],io.BytesIO(b'a'),20,'a.txt')
    assert store.task(host,task['id'])['attachments']==[]


@pytest.mark.parametrize('seconds',[0,90.001,120])
def test_recording_invalid_duration_never_clamped(archived,seconds):
    with pytest.raises(archived['h'].WorkflowError):attach_video(archived,seconds)
    assert not store.run(archived['h'],archived['run']['id'])['recording_path']


def test_recording_actual_duration_bound_to_artifact_and_unconfirmed(archived):
    r=attach_video(archived,90);assert r['recording_seconds']==90;assert not r['recording_confirmed']
    assert r['recording_artifact_sha']==r['artifact_sha']


def test_analysis_snapshots_complete_input_and_reject_fake_citations(archived,tmp_path):
    attach_video(archived);root=tmp_path/'analysis';root.mkdir()
    manifest=analysis.prepare(archived['h'],archived['run']['id'],root)
    assert (root/'trajectory.jsonl').read_bytes()==archived['raw'].read_bytes()
    assert manifest['prompt']==payload()['prompt'];assert manifest['first_turn_id']=='first-id'
    assert (root/'initial/README.md').read_text()=='initial fixture\n'
    assert (root/'artifact/product.py').is_file();analysis.validate(result(),root)
    bad=result();bad['evidence'][0]['quote']='invented output'
    with pytest.raises(ValueError,match='不匹配'):analysis.validate(bad,root)
    bad=result();bad['evidence'][0]['quote']=''
    with pytest.raises(ValueError):analysis.validate(bad,root)


@pytest.mark.parametrize('model,effort',[('DeepSeek','high'),('gpt-5.6-sol','high'),('gpt-6.1-sol','medium')])
def test_analysis_no_model_or_effort_fallback(host,model,effort):
    with pytest.raises(host.WorkflowError,match='只允许'):analysis.enqueue(host,'missing',model,effort)


def test_analysis_explicit_cli_configuration_and_credential_separation(host,tmp_path,monkeypatch):
    observed={};monkeypatch.setenv(host.DOCKER_API_KEY_ENV,'fixture-vendor-secret');monkeypatch.setenv('ANTHROPIC_AUTH_TOKEN','fixture-secret')
    class Process:
        pid=12345;returncode=0
        def __init__(self,argv,**kwargs):observed.update(argv=argv,kwargs=kwargs)
        def communicate(self,prompt,timeout):
            observed['prompt']=prompt;(tmp_path/'result.json').write_text(json.dumps(result()))
    monkeypatch.setattr(analysis.subprocess,'Popen',Process)
    assert analysis.invoke(host,tmp_path,'fixture')['delivery_score']==1
    command=observed['argv'];assert command[command.index('--model')+1]=='gpt-6.1-sol'
    assert 'model_reasoning_effort="high"' in command;assert '--ignore-user-config' in command
    assert '--read-only' not in command;assert command[command.index('--sandbox')+1]=='read-only'
    assert host.DOCKER_API_KEY_ENV not in observed['kwargs']['env'];assert 'ANTHROPIC_AUTH_TOKEN' not in observed['kwargs']['env']
    assert '全部内容' in observed['prompt'];assert 'fixture-vendor-secret' not in (tmp_path/'request-audit.json').read_text()


def test_analysis_stale_result_not_applied(archived,monkeypatch):
    h=archived['h'];rid=archived['run']['id'];attach_video(archived)
    def invoke(*_):store.update(h,rid,artifact_sha='b'*40);return result()
    monkeypatch.setattr(analysis,'invoke',invoke);analysis.enqueue(h,rid)
    deadline=time.monotonic()+5
    while store.run(h,rid)['analysis_status']=='running' and time.monotonic()<deadline:time.sleep(.02)
    r=store.run(h,rid);assert r['analysis_status']=='failed';assert not r['analysis_source']


def test_low_score_full_export_preserves_original_and_no_gsb_columns(archived):
    h=archived['h'];r=approved(archived);rid=r['id']
    assert review.preflight(h,rid,deep=True)['ready_for_local_export']
    data=json.loads(export.bundle(h,[rid]));assert data['rows'][0][0]==payload()['prompt'];assert data['rows'][0][12]=='first-id';assert data['rows'][0][16]=='1'
    assert not any('GSB' in col or col.startswith('A ') or col.startswith('B ') for col in data['columns'])
    assert review.preflight(h,rid)['ready_for_submission'] is False
    with zipfile.ZipFile(io.BytesIO(export.workbook(h,[rid]))) as archive:
        sheet=ET.fromstring(archive.read('xl/worksheets/sheet1.xml'));ns={'s':export.NS}
        values=[e.text for e in sheet.findall('.//s:row[@r="2"]/s:c/s:is/s:t',ns)]
        assert values[0]==payload()['prompt'];assert len(values)==21;assert values[12]=='first-id'


def test_export_formula_safety_and_no_silent_truncation(archived):
    h=archived['h'];r=approved(archived);task=archived['task']
    # Prompt preservation is separately bound to the trace; use notes for cell edge cases.
    store.update(h,r['id'],notes='=HYPERLINK("https://example.com")')
    with zipfile.ZipFile(io.BytesIO(export.workbook(h,[r['id']]))) as archive:
        sheet=ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
        assert not sheet.findall('.//{'+export.NS+'}f')
    store.update(h,r['id'],notes='x'*32768)
    with pytest.raises(h.WorkflowError,match='未截断'):export.workbook(h,[r['id']])
    assert 'x'*32768 in export.bundle(h,[r['id']]).decode()


def test_excel_preserves_crlf_and_checks_utf16_limit(archived):
    h=archived['h'];r=approved(archived)
    store.update(h,r['id'],notes='first\r\nsecond\rthird')
    with zipfile.ZipFile(io.BytesIO(export.workbook(h,[r['id']]))) as archive:
        sheet=ET.fromstring(archive.read('xl/worksheets/sheet1.xml'));ns={'s':export.NS}
        assert sheet.find('.//s:c[@r="T2"]/s:is/s:t',ns).text=='first\r\nsecond\rthird'
    store.update(h,r['id'],notes='😀'*16384)
    with pytest.raises(h.WorkflowError,match='未截断'):export.workbook(h,[r['id']])


def test_export_rejects_malformed_ids(host):
    with pytest.raises(host.WorkflowError):export.rows(host,[{}])


def test_changed_trace_and_recording_invalidate_confirmation(archived):
    h=archived['h'];r=approved(archived);path=Path(r['trace_path']);path.write_text(path.read_text()+'\n')
    checked=review.preflight(h,r['id'],deep=True)
    assert not checked['ready_for_local_export'];assert any('SHA-256' in i for i in checked['issues'])
    with pytest.raises(h.WorkflowError):export.bundle(h,[r['id']])
    r=attach_video(archived,30);assert not r['review_confirmed'];assert r['analysis_status']=='stale'


def test_recording_change_blocked_during_analysis(archived):
    h=archived['h'];store.update(h,archived['run']['id'],analysis_status='running')
    with pytest.raises(h.WorkflowError,match='分析正在'):attach_video(archived)


def test_human_confirmation_requires_playback_and_current_revision(archived):
    h=archived['h'];r=approved(archived)
    with pytest.raises(h.WorkflowError,match='播放'):review.save(h,r['id'],{**result(),'confirm':True,'evidence_source':store.fingerprint(h,r['id'])})
    with pytest.raises(h.WorkflowError,match='证据版本'):review.save(h,r['id'],{**result(),'confirm':True,'recording_confirmed':True,'evidence_source':'old'})


def test_recovery_never_resends_uncertain_initial_prompt(host,monkeypatch):
    t=create(host);r,_=workflow.start(host,t['id']);store.update(host,r['id'],status='starting')
    calls=[];monkeypatch.setattr(workflow,'enqueue',lambda *args:calls.append(args));workflow.recover(host)
    assert not calls;assert store.run(host,r['id'])['status']=='failed'


def test_sqlite_backup_is_complete_and_once(host):
    t=create(host);target=store.backup_database(host.DB_PATH);assert target.is_file()
    import sqlite3
    with sqlite3.connect(target) as db:assert db.execute('SELECT title FROM feedback_tasks WHERE id=?',(t['id'],)).fetchone()[0]==t['title']
    assert store.backup_database(host.DB_PATH)==target


@pytest.fixture
def http(host):
    server=app.ThreadingHTTPServer(('127.0.0.1',0),app.ApiHandler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    yield f'http://127.0.0.1:{server.server_port}'
    server.shutdown();server.server_close();thread.join(timeout=2)


def http_request(base,path,body=None,headers=None):
    data=None if body is None else json.dumps(body).encode()
    req=urllib.request.Request(base+path,data=data,headers={'Content-Type':'application/json',**(headers or {})})
    try:
        with urllib.request.urlopen(req,timeout=10) as response:return response.status,response.read()
    except urllib.error.HTTPError as exc:return exc.code,exc.read()


def test_http_mode_and_legacy_writes_blocked(http):
    status,body=http_request(http,'/api/feedback/config');assert status==200;assert json.loads(body)['workflow_mode']=='domain_feedback'
    status,body=http_request(http,'/');assert status==200;assert b'/feedback.js' in body
    status,body=http_request(http,'/pairwise.html');assert status==200;assert b'/feedback-history.js' in body
    for path in ['/api/pairwise','/api/pairwise/auto','/api/runs','/api/settings/model']:
        status,body=http_request(http,path,{});assert status==400;assert '只读' in body.decode()
    status,body=http_request(http,'/api/pairwise');assert status==200;assert json.loads(body)['read_only']
    status,_=http_request(http,'/api/feedback/submit',{});assert status==400


def test_http_cross_site_and_invalid_json_rejected(http):
    status,_=http_request(http,'/api/feedback/profile',{'domain':'安全'},{'Origin':'https://evil.example'});assert status==400
    status,_=http_request(http,'/api/feedback/profile',{'domain':'安全'},{'Content-Type':'text/plain'});assert status==400


def test_http_raw_upload_and_range_download(http,host):
    task=create(host);file=b'actual attachment'
    req=urllib.request.Request(http+f"/api/feedback/tasks/{task['id']}/attachments?name=test.txt",data=file,headers={'Content-Type':'application/octet-stream'})
    with urllib.request.urlopen(req) as response:assert response.status==201;task=json.loads(response.read())
    status,body=http_request(http,f"/api/feedback/tasks/{task['id']}/attachments/{task['attachments'][0]['id']}");assert status==200;assert body==file
