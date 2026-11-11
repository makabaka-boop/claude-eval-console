"""Streamed evidence uploads and real media duration inspection."""
import hashlib
import json
import math
import shutil
import struct
import subprocess
import uuid
from pathlib import Path

from . import policy, store

MAX_ATTACHMENT = 20 * 1024 * 1024
MAX_RECORDING = 500 * 1024 * 1024


def safe_name(value):
    value = str(value or "")
    if not value or len(value)>180 or Path(value).name != value or any(ord(c)<32 or c in '/\\:' for c in value) or value in {'.','..'}:
        raise ValueError("文件名无效")
    return value


def receive(stream, length, path):
    digest = hashlib.sha256()
    remaining = length
    path.parent.mkdir(parents=True,exist_ok=True)
    try:
        with path.open('xb') as target:
            while remaining:
                block = stream.read(min(1024*1024,remaining))
                if not block:
                    raise ValueError("上传未完成，文件未登记")
                target.write(block)
                digest.update(block)
                remaining -= len(block)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return digest.hexdigest()


def attachment(h, task_id, stream, length, name):
    t = store.task(h,task_id)
    if t['runs']:
        raise h.WorkflowError("执行记录建立后不能修改原始题目附件，请新建题目")
    if not 0 < length <= MAX_ATTACHMENT or len(t['attachments']) >= 10:
        raise h.WorkflowError("每份附件应在 20 MB 内，每题最多 10 份")
    try:
        name = safe_name(name)
    except ValueError as exc:
        raise h.WorkflowError(str(exc)) from exc
    if any(a['name']==name for a in t['attachments']):
        raise h.WorkflowError("题目已有同名附件")
    file_id = uuid.uuid4().hex[:16]
    path = store.evidence_root(h)/'attachments'/task_id/file_id/name
    digest = receive(stream,length,path)
    item = {'id':file_id,'name':name,'path':str(path),'sha256':digest,'size':length,
            'container_path':f'/tmp/feedback-inputs/{name}'}
    with h.db_connection() as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT 1 FROM feedback_runs WHERE task_id=?',(task_id,)).fetchone():
            path.unlink(missing_ok=True)
            raise h.WorkflowError("任务已开始，附件未变更")
        current = json.loads(db.execute('SELECT attachments FROM feedback_tasks WHERE id=?',(task_id,)).fetchone()[0])
        if any(a['name']==name for a in current) or len(current)>=10:
            path.unlink(missing_ok=True)
            raise h.WorkflowError("并发附件上传冲突，请刷新后重试")
        db.execute('UPDATE feedback_tasks SET attachments=? WHERE id=?',(store.dumps([*current,item]),task_id))
    return store.task(h,task_id)


def mp4_duration(path):
    """Read the actual movie-header timescale/duration without clamping it."""
    size = path.stat().st_size
    def boxes(source, start, end):
        at = start
        while at+8 <= end:
            source.seek(at)
            header = source.read(8)
            length,kind = struct.unpack('>I4s',header)
            header_size = 8
            if length == 1:
                extra=source.read(8)
                if len(extra)!=8:raise ValueError("视频容器不完整")
                length=struct.unpack('>Q',extra)[0];header_size=16
            elif length==0:
                length=end-at
            if length<header_size or at+length>end:raise ValueError("视频容器长度无效")
            yield kind,at+header_size,at+length
            at+=length
    result=None;has_media=False
    with path.open('rb') as source:
        for kind,start,end in boxes(source,0,size):
            if kind==b'mdat' and end>start:has_media=True
            if kind!=b'moov':continue
            for child,content,child_end in boxes(source,start,end):
                if child!=b'mvhd':continue
                source.seek(content);data=source.read(min(40,child_end-content))
                if not data:raise ValueError("视频缺少时长头")
                if data[0]==0 and len(data)>=20:
                    scale,duration=struct.unpack('>II',data[12:20])
                elif data[0]==1 and len(data)>=32:
                    scale=struct.unpack('>I',data[20:24])[0];duration=struct.unpack('>Q',data[24:32])[0]
                else:raise ValueError("视频时长版本不受支持")
                if scale:result=duration/scale
    if not has_media or result is None or not math.isfinite(result) or result<=0:
        raise ValueError("未找到可核验的视频内容和时长，请导出 MP4/MOV 后上传")
    return result


def duration(path):
    executable=shutil.which('ffprobe')
    if executable:
        result=subprocess.run([executable,'-v','error','-show_entries','format=duration','-of','json',str(path)],capture_output=True,text=True,timeout=30)
        try:
            value=float(json.loads(result.stdout)['format']['duration'])
            if result.returncode==0 and math.isfinite(value) and value>0:return value
        except (KeyError,ValueError,TypeError):pass
    if path.suffix.lower() in {'.mp4','.mov','.m4v'}:
        return mp4_duration(path)
    raise ValueError("无法读取视频真实时长，请上传 MP4/MOV，或安装 ffprobe 后使用 WebM")


def recording(h, run_id, stream, length, name):
    r=store.run(h,run_id)
    if r['analysis_status']=='running':
        raise h.WorkflowError('分析正在读取证据，请完成后再更换录屏')
    if r['status']!='archived' or not policy.SHA.fullmatch(r['artifact_sha']):
        raise h.WorkflowError("请先完成并归档最终产物，再关联完整录屏")
    try:name=safe_name(name)
    except ValueError as exc:raise h.WorkflowError(str(exc)) from exc
    if Path(name).suffix.lower() not in {'.mp4','.mov','.m4v','.webm'} or not 0<length<=MAX_RECORDING:
        raise h.WorkflowError("请上传 500 MB 内的 MP4、MOV、M4V 或 WebM 视频")
    path=store.evidence_root(h)/'recordings'/run_id/uuid.uuid4().hex[:12]/name
    digest=receive(stream,length,path)
    try:
        seconds=duration(path)
        if not 0<seconds<=90:raise ValueError(f"录屏实际时长 {seconds:.2f} 秒，必须大于 0 且不超过 90 秒；原文件未登记")
    except (ValueError,OSError,subprocess.SubprocessError) as exc:
        path.unlink(missing_ok=True)
        raise h.WorkflowError(str(exc)) from exc
    with h.db_connection() as db:
        db.execute('BEGIN IMMEDIATE')
        latest=db.execute('SELECT status,artifact_sha,analysis_status FROM feedback_runs WHERE id=?',(run_id,)).fetchone()
        if latest['status']!='archived' or latest['artifact_sha']!=r['artifact_sha'] or latest['analysis_status']=='running':
            path.unlink(missing_ok=True)
            raise h.WorkflowError("上传期间产物发生变化，请重新核对录屏")
        db.execute("""UPDATE feedback_runs SET recording_path=?,recording_sha256=?,recording_seconds=?,
             recording_artifact_sha=?,recording_confirmed=0,review_confirmed=0,
             analysis_status=CASE WHEN analysis_source<>'' THEN 'stale' ELSE 'pending' END,updated_at=? WHERE id=?""",
             (str(path),digest,seconds,r['artifact_sha'],h.now_text(),run_id))
    store.event(h,run_id,f"录屏已关联，实际 {seconds:.2f} 秒；请播放检查真实运行和失败过程")
    return store.run(h,run_id)
