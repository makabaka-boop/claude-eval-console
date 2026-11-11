"""Private local vendor configuration; no endpoint returns a saved API Key."""
import json
import os
import stat
import tempfile
import threading
from pathlib import Path

from . import environment, policy

LOCK=threading.RLock()


def path(h):
    return Path(h.DATA_DIR)/'vendor-credentials.json'


def validate(h,model,key):
    if model!=policy.EXECUTION_MODEL:raise h.WorkflowError('本期执行模型请填写 auto_model/urm，不含末尾句号')
    if not isinstance(key,str):raise h.WorkflowError('请粘贴甲方发放的 Key 本身')
    if len(key)>4096 or any(c.isspace() or ord(c)<32 for c in key):
        raise h.WorkflowError('Key 格式无效，请仅粘贴密钥本身，不含空格或换行')
    if key.lower() in {'xxxxx','your-key','your_api_key'} or key.lower().startswith(('apikey=','api_key=','export ')):
        raise h.WorkflowError('请填写甲方发放的真实 Key，不要粘贴示例或整条启动命令')


def load(h):
    """Load only this app's dedicated file, never personal Claude settings."""
    target=path(h)
    if not target.exists() and not target.is_symlink():return False
    try:
        info=target.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077 or info.st_size>20_000:
            raise ValueError('private file required')
        data=json.loads(target.read_text())
        model=data.get('model','');key=data.get('api_key','')
        validate(h,model,key)
        with LOCK:
            h.os.environ[environment.MODEL_ENV]=model
            if key:
                h.os.environ[h.DOCKER_API_KEY_ENV]=key
                h.VENDOR_KEY_SOURCE='本机专用配置'
            h.VENDOR_CONFIG_ERROR=''
        return True
    except Exception:
        h.VENDOR_CONFIG_ERROR='本机甲方配置无法读取，请通过“模型与 Key”重新保存'
        return False


def save(h,payload):
    model=str(payload.get('model') or '').strip()
    incoming=payload.get('api_key','')
    if not isinstance(incoming,str):raise h.WorkflowError('请粘贴甲方发放的 Key 本身')
    key=incoming.strip() or str(h.os.environ.get(h.DOCKER_API_KEY_ENV,'')).strip()
    validate(h,model,key)
    with LOCK:
        changed=(model!=h.os.environ.get(environment.MODEL_ENV,'') or key!=h.os.environ.get(h.DOCKER_API_KEY_ENV,''))
        if changed:
            with h.db_connection() as db:
                active=db.execute("SELECT 1 FROM feedback_runs WHERE status IN ('queued','starting','running','resuming','interrupted','archiving','archive_failed') LIMIT 1").fetchone()
            if active:raise h.WorkflowError('仍有执行或待恢复的现场，请先完成或停止任务后再更换甲方配置')
        target=path(h);target.parent.mkdir(parents=True,exist_ok=True)
        if target.is_symlink():raise h.WorkflowError('本机甲方配置路径异常，未保存密钥')
        fd,temporary=tempfile.mkstemp(prefix='.vendor-',dir=target.parent)
        try:
            with os.fdopen(fd,'w',encoding='utf-8') as output:
                os.fchmod(output.fileno(),0o600)
                json.dump({'model':model,'api_key':key},output,ensure_ascii=False)
                output.flush();os.fsync(output.fileno())
            os.replace(temporary,target)
        except Exception:
            Path(temporary).unlink(missing_ok=True)
            raise h.WorkflowError('本机专用配置保存失败，请检查目录权限后重试') from None
        h.os.environ[environment.MODEL_ENV]=model
        if key:h.os.environ[h.DOCKER_API_KEY_ENV]=key
        h.VENDOR_KEY_SOURCE='本机专用配置'
        h.VENDOR_CONFIG_ERROR=''
    return environment.configuration(h)
