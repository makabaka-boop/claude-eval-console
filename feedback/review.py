"""Evidence preflight and explicit human confirmation of mandated-model drafts."""
import hashlib
import re
from pathlib import Path

from . import policy, store, trace


def file_digest(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda:source.read(1024*1024),b''):digest.update(block)
    return digest.hexdigest()


def preflight(h,run_id,deep=False,require_review=True):
    r=store.run(h,run_id);t=store.task(h,r['task_id'])
    errors=[]
    if r['status']!='archived':errors.append('执行尚未归档')
    if not r['first_session_id'] or not r['first_turn_id']:errors.append('缺少首轮 SessionID 或 TurnID/PromptID')
    if not policy.SHA.fullmatch(r['artifact_sha']):errors.append('缺少最终 40 位产物 SHA')
    config=r['config']
    if config.get('model')!=policy.EXECUTION_MODEL or config.get('context_tokens')!=policy.CONTEXT_TOKENS:
        errors.append('缺少冻结的甲方 auto/1M 配置')
    if not config.get('harness_version'):errors.append('缺少实际 Harness 版本')
    trace_path=Path(r['trace_path'])
    if not r['trace_path'] or not trace_path.is_file():errors.append('缺少原始完整 JSONL')
    elif deep:
        try:
            state=trace.inspect(trace_path,t['prompt'],r['first_session_id'],r['resume_count'])
            if state['sha256']!=r['trace_sha256']:errors.append('原始轨迹 SHA-256 已变化')
            if not state['complete'] or state['first_turn_id']!=r['first_turn_id']:
                errors.append('首轮定位或全部续写的完成边界不完整')
        except (OSError,ValueError) as exc:errors.append(f'轨迹核验失败：{exc}')
    if len(r['continuations'])!=r['resume_count'] or any(not c['prompt_id'] or c['status']!='observed' for c in r['continuations']):
        errors.append('中断继续的实际 ID 尚未全部确认')
    if r['resume_count'] and not r['other_issues'].strip():errors.append('请在其他问题中说明中断情况')
    path=Path(r['recording_path'])
    if not r['recording_path'] or not path.is_file():errors.append('缺少完整运行录屏')
    elif deep and file_digest(path)!=r['recording_sha256']:errors.append('录屏 SHA-256 已变化')
    seconds=r['recording_seconds']
    if not isinstance(seconds,(float,int)) or not 0<seconds<=90:errors.append('录屏实际时长必须在 90 秒以内')
    if r['recording_artifact_sha']!=r['artifact_sha']:errors.append('录屏未绑定当前最终产物')
    if deep and policy.SHA.fullmatch(r['artifact_sha']):
        try:
            workspace=Path(h.run_row(run_id)['repo_path'])
            if h.run_command(['git','merge-base','--is-ancestor',t['initial_sha'],r['artifact_sha']],cwd=workspace,check=False).returncode:
                errors.append('最终产物不包含初始快照的提交历史')
            actual=h.run_command(['git','rev-parse',f"{r['artifact_sha']}^{{tree}}"],cwd=workspace).stdout.strip()
            if actual!=r['artifact_tree']:errors.append('产物代码树与归档记录不一致')
        except Exception as exc:errors.append(f'Git 证据核验失败：{exc}')
    source=store.fingerprint(h,run_id)
    if r['analysis_status']!='complete' or r['analysis_source']!=source:
        errors.append('需要对当前完整证据重新进行指定模型分析')
    if r['analysis_model']!=policy.ANALYSIS_MODEL or r['analysis_effort']!=policy.ANALYSIS_EFFORT:
        errors.append('分析配置不是 gpt-6.1-sol + high')
    if require_review:
        if not r['review_confirmed'] or r['review_source']!=source:errors.append('请复核当前证据和 Feedback 后确认')
        if not r['recording_confirmed']:errors.append('请实际播放并确认完整录屏')
        if type(r['delivery_score']) is not int or not 1<=r['delivery_score']<=5:errors.append('交付完整性须为 1–5 分整数')
        if any(len(r[k].strip())<10 for k in ('result_feedback','process_feedback')):
            errors.append('Feedback 必须同时包含具体结果和过程')
    links=[]
    if not policy.public_url(r['trace_url']):links.append('缺少可直接下载的轨迹外部链接')
    if not policy.public_url(r['recording_url']):links.append('缺少可播放或下载的录屏外部链接')
    if t['attachments']:links.append('多模态附件的正式链接映射需按提交模板核对')
    return {'ready_for_local_export':not errors,'issues':list(dict.fromkeys(errors)),
            'ready_for_submission':False,'submission_issues':[*dict.fromkeys(errors),*links,'本期正式提交模板和目的地址尚未配置'],
            'evidence_source':source}


def save(h,run_id,payload):
    r=store.run(h,run_id)
    score=payload.get('delivery_score')
    if score in ('',None):score=None
    else:
        if isinstance(score,bool) or (not isinstance(score,int) and not re.fullmatch(r'[1-5]',str(score))):
            raise h.WorkflowError('交付完整性须为 1–5 分整数')
        score=int(score)
        if not 1<=score<=5:raise h.WorkflowError('交付完整性须为 1–5 分整数')
    fields={k:str(payload.get(k,r[k]) or '') for k in ('result_feedback','process_feedback','other_issues','notes','trace_url','recording_url')}
    for name in ('trace_url','recording_url'):
        if fields[name] and not policy.public_url(fields[name]):raise h.WorkflowError('外部证据链接须为不带凭据的 HTTPS 地址')
    if r['resume_count'] and not fields['other_issues'].strip():raise h.WorkflowError('发生过中断，请保留其他问题中的说明')
    confirm=payload.get('confirm') is True
    source=store.fingerprint(h,run_id)
    if confirm:
        if payload.get('evidence_source')!=source:raise h.WorkflowError('证据版本已变化，请刷新并重新复核')
        checked=preflight(h,run_id,deep=True,require_review=False)
        if checked['issues']:raise h.WorkflowError('；'.join(checked['issues']))
        if score is None or any(len(fields[k].strip())<10 for k in ('result_feedback','process_feedback')):
            raise h.WorkflowError('确认前须填写评分及具体的结果和过程反馈')
        if payload.get('recording_confirmed') is not True:raise h.WorkflowError('请实际播放并确认录屏')
    fields.update(delivery_score=score,feedback=f"结果：{fields['result_feedback']}\n\n过程：{fields['process_feedback']}",
                  review_confirmed=int(confirm),recording_confirmed=int(confirm),review_source=source if confirm else '')
    store.update(h,run_id,**fields)
    store.event(h,run_id,'评价已复核确认' if confirm else '评价草稿已保存')
    return store.run(h,run_id)
