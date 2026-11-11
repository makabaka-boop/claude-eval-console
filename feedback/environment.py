"""November Docker checks; vendor credentials never fall back to personal auth."""
import json
import platform
import re

from pairwise.environment import PROBE, comparable_runtime, digest
from . import policy

IMAGE = 'adminfather/benzhi-claude-code:20261109'
HARNESS_VERSION = '2.1.319'
GATEWAY = 'https://llm.jzxhnh.com'
MODEL_ENV = 'CLAUDE_EVAL_VENDOR_MODEL'


def configuration(h):
    model=str(h.os.environ.get(MODEL_ENV,'')).strip()
    has_key=bool(str(h.os.environ.get(h.DOCKER_API_KEY_ENV,'')).strip())
    issues=[]
    if not model:issues.append('等待甲方提供并确认执行模型')
    elif model!=policy.EXECUTION_MODEL:issues.append('甲方模型与本期镜像预置模型不一致，需核对适配')
    if not has_key:issues.append('等待甲方专用 Key')
    return {'execution_ready':not issues,'execution_blockers':issues,
            'vendor_model':model,'vendor_key_configured':has_key,
            'api_key_source':h.DOCKER_API_KEY_ENV if has_key else '未配置甲方专用 Key',
            'docker_image':h.DOCKER_IMAGE,'expected_harness_version':HARNESS_VERSION,
            'model_config_env':MODEL_ENV,'key_config_env':h.DOCKER_API_KEY_ENV}


def require_execution_config(h):
    config=configuration(h)
    if not config['execution_ready']:raise h.WorkflowError('；'.join(config['execution_blockers'])+'；当前仅可准备题目和检查镜像环境')
    return config


def credential_fingerprint(h):
    require_execution_config(h)
    return digest(h.os.environ[h.DOCKER_API_KEY_ENV])


def normalize(h,output):
    data=json.loads(output)
    version=h.normalize_harness_version(data.get('version',''))
    if version!=HARNESS_VERSION:raise h.WorkflowError(f'镜像实际 Claude Code 版本须为 {HARNESS_VERSION}，读到 {version or "未知"}')
    if data.get('model')!=policy.EXECUTION_MODEL:raise h.WorkflowError('镜像实际模型与本期预置模型不一致')
    if data.get('context_tokens')!=policy.CONTEXT_TOKENS:raise h.WorkflowError('镜像实际上下文不是 1000000')
    if (data.get('env') or {}).get('ANTHROPIC_BASE_URL','').rstrip('/')!=GATEWAY:
        raise h.WorkflowError('镜像实际网关与本期文档不一致')
    data['harness_version']=version
    return data


def inspect_image(h):
    image=h.run_command(['docker','image','inspect','--format','{{.Id}}',h.DOCKER_IMAGE],timeout=30).stdout.strip()
    if not re.fullmatch(r'sha256:[a-f0-9]{64}',image):raise h.WorkflowError('无法固定镜像摘要，请先下载本期镜像')
    # No task entrypoint, no API Key, and no network: metadata inspection only.
    result=h.run_command(['docker','run','--rm','--network','none','--entrypoint','node',image,'-e',PROBE],timeout=60)
    runtime=normalize(h,result.stdout)
    return {'image':image,'harness':'Claude Code','harness_version':runtime['harness_version'],
            'model':runtime['model'],'context_tokens':runtime['context_tokens'],
            'gateway':GATEWAY,'runtime':comparable_runtime(runtime),
            'operating_system':'macOS' if platform.system()=='Darwin' else platform.system()}


def freeze(h):
    require_execution_config(h)
    return {**inspect_image(h),'host':digest((platform.node(),platform.platform(),platform.machine())),
            'credential':credential_fingerprint(h),'model_source':MODEL_ENV,
            'key_source':h.DOCKER_API_KEY_ENV}


def verify_host(h,config):
    if config['host']!=digest((platform.node(),platform.platform(),platform.machine())):
        raise h.WorkflowError('机器或操作系统与冻结配置不一致')
    if config['credential']!=credential_fingerprint(h):raise h.WorkflowError('甲方专用凭据已变化，请恢复本次执行的原始配置')


def verify_container(h,container,config,expected_model=policy.EXECUTION_MODEL):
    if expected_model!=policy.EXECUTION_MODEL:raise h.WorkflowError('执行模型配置不一致')
    image=h.run_command(['docker','inspect','--format','{{.Image}}',container]).stdout.strip()
    container_id=h.run_command(['docker','inspect','--format','{{.Id}}',container]).stdout.strip()
    output=h.run_command(['docker','exec',container,'node','-e',PROBE],timeout=60).stdout
    runtime=normalize(h,output)
    if image!=config['image'] or comparable_runtime(runtime)!=config['runtime']:
        raise h.WorkflowError('容器镜像、Harness 或运行参数与冻结配置不一致')
    if not re.fullmatch('[a-f0-9]{64}',container_id):raise h.WorkflowError('无法绑定本次容器身份')
    return {'container_id':container_id,'image':image,'runtime':comparable_runtime(runtime)}
