"""Local-only single-session endpoints, independent of historical GSB writes."""
import mimetypes
import re
from pathlib import Path
from urllib.parse import urlparse

from . import analysis, export, media, policy, review, store, workflow

PREFIX = '/api/feedback'
ID = r'([a-f0-9]{12})'


def status(h):
    source=h.dependency_status()
    keys=('ready','github_owner','github_account','projects_root','submitter','app_version',
          'service_pid','codex_version','docker_image','api_key_source','commands','max_parallel')
    return {**{k:source.get(k) for k in keys},**policy.schema(),'profile':store.profile(h)}


def check_write(request, h):
    host = request.headers.get('Host', '')
    parsed = urlparse('http://' + host)
    if parsed.hostname not in {'localhost', '127.0.0.1', '::1'}:
        raise h.WorkflowError('专项写入仅接受本机访问')
    origin = request.headers.get('Origin')
    if origin:
        source = urlparse(origin)
        if source.scheme != 'http' or source.netloc != host:
            raise h.WorkflowError('拒绝跨站写入请求')
    if request.headers.get('Sec-Fetch-Site') == 'cross-site':
        raise h.WorkflowError('拒绝跨站写入请求')


def get(request, h, path, query):
    route = path[len(PREFIX):]
    if route in {'', '/config'}:
        request.send_json({**policy.schema(), 'profile':store.profile(h)})
    elif route == '/tasks':
        request.send_json(store.listing(h, query))
    elif route == '/exports':
        request.send_json(export.listing(h,query))
    elif route == '/environment':
        request.send_json(status(h))
    elif route=='/history' or route.startswith('/history/'):
        from .archive import get as get_archive
        get_archive(request,h,path,query)
    elif (match := re.fullmatch('/tasks/' + ID, route)):
        request.send_json(store.task(h, match[1]))
    elif (match := re.fullmatch('/tasks/' + ID + r'/attachments/([a-f0-9]{16})', route)):
        t = store.task(h, match[1])
        item = next((a for a in t['attachments'] if a['id']==match[2]), None)
        if not item:raise h.WorkflowError('没有找到附件')
        request.send_file_download(Path(item['path']), 'application/octet-stream')
    elif (match := re.fullmatch('/runs/' + ID, route)):
        r = store.run(h, match[1])
        request.send_json({**r, 'preflight':review.preflight(h, match[1])})
    elif (match := re.fullmatch('/runs/' + ID + '/(trace|recording|analysis)', route)):
        r = store.run(h, match[1])
        if match[2]=='analysis':
            request.send_json({'model':r['analysis_model'], 'effort':r['analysis_effort'],
                               'source':r['analysis_source'], 'result':r['analysis']})
            return
        filename = r['trace_path'] if match[2]=='trace' else r['recording_path']
        if not filename or not Path(filename).is_file():raise h.WorkflowError('没有找到证据文件')
        if match[2]=='trace':request.send_file_download(Path(filename), 'application/x-ndjson')
        else:request.send_file_inline(Path(filename), mimetypes.guess_type(filename)[0] or 'video/mp4')
    else:
        request.send_error_json('专项接口不存在',404)


def post(request, h, path, query=None):
    check_write(request,h)
    route = path[len(PREFIX):]
    query = query or {}
    # Stream evidence before JSON decoding: recordings can be up to 500 MB.
    upload = re.fullmatch('/(tasks|runs)/' + ID + '/(attachments|recording)',route)
    if upload:
        length = int(request.headers.get('Content-Length','0'))
        name = query.get('name',[''])[0]
        if upload[1]=='tasks' and upload[3]=='attachments':
            result=media.attachment(h,upload[2],request.rfile,length,name)
        elif upload[1]=='runs' and upload[3]=='recording':
            result=media.recording(h,upload[2],request.rfile,length,name)
        else:raise h.WorkflowError('上传目标无效')
        request.send_json(result,201)
        return
    if request.headers.get('Content-Type','').split(';',1)[0] != 'application/json':
        raise h.WorkflowError('请使用 application/json 发送专项操作')
    payload = request.read_json()
    if not isinstance(payload,dict):raise h.WorkflowError('请求须为 JSON 对象')
    if route=='/profile':
        request.send_json(store.select_domain(h,payload.get('domain')))
    elif route=='/tasks':
        request.send_json(store.create(h,payload),201)
    elif route=='/exports/preflight':
        request.send_json(export.preflight(h,payload.get('run_ids')))
    elif route in {'/exports/json','/exports/xlsx'}:
        ids=payload.get('run_ids')
        if route.endswith('json'):
            request.send_download(export.bundle(h,ids),'domain-feedback-local.json','application/json; charset=utf-8')
        else:
            request.send_download(export.workbook(h,ids),'domain-feedback-local.xlsx','application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    elif (match := re.fullmatch('/tasks/' + ID + '/(start|retry)',route)):
        r,created=workflow.start(h,match[1],retry=match[2]=='retry')
        request.send_json(r,202 if created else 200)
    elif route=='/submit' or route.endswith('/submit'):
        raise h.WorkflowError('本期正式提交模板与目的地址尚未配置；请先本地复核导出，不能提交到旧 GSB 地址')
    elif (match := re.fullmatch('/runs/' + ID + '/([a-z-]+)',route)):
        run_id,action=match.groups()
        store.run(h,run_id)
        if action=='resume':result=workflow.resume(h,run_id,payload.get('reason'))
        elif action=='interrupted':result=workflow.mark_interrupted(h,run_id,payload.get('reason'))
        elif action=='stop':result=workflow.stop(h,run_id)
        elif action=='prepare-recording':
            with workflow.LOCK:result=workflow.prepare_recording(h,run_id)
        elif action=='archive':
            with workflow.LOCK:
                if run_id in workflow.WORKERS:raise h.WorkflowError('后台正在监控或归档，请等待完成')
                workflow.WORKERS.add(run_id)
            try:result=workflow.archive(h,run_id)
            finally:
                with workflow.LOCK:workflow.WORKERS.discard(run_id)
        elif action=='sync':
            with workflow.LOCK:
                if run_id in workflow.WORKERS:raise h.WorkflowError('后台正在同步，请等待自动监控')
                if store.run(h,run_id)['status']=='archived':raise h.WorkflowError('已归档轨迹已冻结，无需从活动容器覆盖')
                workflow.sync_trace(h,run_id)
            result=store.run(h,run_id)
        elif action=='analyze':result=analysis.enqueue(h,run_id,payload.get('model',policy.ANALYSIS_MODEL),payload.get('effort',policy.ANALYSIS_EFFORT))
        elif action=='review':result=review.save(h,run_id,payload)
        elif action=='preflight':result=review.preflight(h,run_id,deep=True)
        else:
            request.send_error_json('专项操作不存在',404)
            return
        request.send_json(result)
    else:request.send_error_json('专项接口不存在',404)
