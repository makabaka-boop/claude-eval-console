"""Read historical A/B rows without starting any legacy scheduler or analysis."""
import re
from pathlib import Path


def get(request,h,path,query):
    route=path.removeprefix('/api/pairwise').removeprefix('/api/feedback/history')
    if route in {'','/'}:
        page=max(1,int(query.get('page',[1])[0]));pattern='%'+str(query.get('query',[''])[0])[:200]+'%'
        with h.db_connection() as db:
            total=db.execute('SELECT COUNT(*) FROM pairwise_tasks WHERE deleted_at IS NULL AND (title LIKE ? OR prompt LIKE ?)',(pattern,pattern)).fetchone()[0]
            pages=max(1,(total+19)//20);page=min(page,pages)
            tasks=db.execute('SELECT id,title,repo_name,task_type,difficulty,created_at FROM pairwise_tasks WHERE deleted_at IS NULL AND (title LIKE ? OR prompt LIKE ?) ORDER BY created_at DESC,id DESC LIMIT 20 OFFSET ?',(pattern,pattern,(page-1)*20)).fetchall()
        request.send_json({'items':[dict(t) for t in tasks],'total':total,'page':page,'pages':pages,'read_only':True})
        return
    if (match:=re.fullmatch(r'/([a-f0-9]{12})',route)):
        with h.db_connection() as db:
            row=db.execute('SELECT id,title,prompt,repo_url,base_sha,gsb,reason,notes,task_type,difficulty,created_at FROM pairwise_tasks WHERE id=?',(match[1],)).fetchone()
            if not row:raise h.WorkflowError('没有找到历史题目')
            attempts=db.execute('''SELECT a.id,a.side,a.number,a.status,a.error,a.artifact_sha,a.trace_path,a.recording_path,
                 r.session_id,r.first_prompt_id,r.first_result FROM pairwise_attempts a JOIN runs r ON a.id=r.id WHERE a.pair_id=? ORDER BY a.side,a.number''',(match[1],)).fetchall()
        request.send_json({**dict(row),'attempts':[dict(a) for a in attempts],'read_only':True})
        return
    if (match:=re.fullmatch(r'/attempts/([a-f0-9]{12})/(trace|recording-file)',route)):
        with h.db_connection() as db:
            row=db.execute('SELECT trace_path,recording_path FROM pairwise_attempts WHERE id=?',(match[1],)).fetchone()
        filename=(row['trace_path'] if match[2]=='trace' else row['recording_path']) if row else ''
        if not filename or not Path(filename).is_file():raise h.WorkflowError('没有找到历史证据文件')
        request.send_file_download(Path(filename),'application/octet-stream')
        return
    request.send_error_json('历史归档仅提供题目列表、详情与证据下载',404)
