"""Read and freeze configuration. Never send credentials to the browser."""
import hashlib
import json
import platform

from .models import AUTO_MODEL, MODEL_NAMES

PROBE = r'''
const fs=require('fs'), path=require('path'), crypto=require('crypto');
const hash=x=>crypto.createHash('sha256').update(x).digest('hex');
// The image digest pins the executable. Reading its package metadata avoids
// starting a second Claude process while the interactive one initializes.
const executable=fs.realpathSync('/usr/local/bin/claude');
const version=JSON.parse(fs.readFileSync(path.join(path.dirname(path.dirname(executable)),
 'package.json'),'utf8')).version+' (Claude Code)';
const env=Object.fromEntries(Object.entries(process.env).filter(([k])=>
 /^(ANTHROPIC_|CLAUDE_CODE_|DISABLE_AUTOUPDATER)/.test(k) && !/KEY|TOKEN|AUTH/.test(k)));
// Context setting includes TOKEN in its name; it is a non-secret integer.
env.CLAUDE_CODE_MAX_CONTEXT_TOKENS=process.env.CLAUDE_CODE_MAX_CONTEXT_TOKENS||'';
const files={};
for(const p of ['/usr/local/bin/entrypoint.sh','/home/node/.claude/settings.json'])
 files[p]=fs.existsSync(p)?hash(fs.readFileSync(p)):'';
console.log(JSON.stringify({version,
 model:process.env.ANTHROPIC_MODEL,context_tokens:Number(env.CLAUDE_CODE_MAX_CONTEXT_TOKENS),
 env,files,platform:process.platform,arch:process.arch}));
'''


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def credential_fingerprint(h):
    # Hash only; do not store or expose credentials themselves.
    for key in (h.DOCKER_API_KEY_ENV, "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
        if h.os.environ.get(key):
            return digest(h.os.environ[key])
    try:
        env = json.loads(h.CLAUDE_SETTINGS_PATH.read_text()).get("env", {})
        key = env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY")
    except (OSError, ValueError, AttributeError):
        key = None
    if not key:
        raise h.WorkflowError("请先配置容器 API Key；Pair 执行需能核验 A/B 使用同一凭据")
    return digest(key)


def normalize(h, output, expected_model=AUTO_MODEL):
    data = json.loads(output)
    data["harness_version"] = h.normalize_harness_version(data["version"])
    if not data["harness_version"] or data.get("context_tokens") != 1_000_000:
        raise h.WorkflowError("容器实际配置未满足 max context token = 1000000，请使用管理员配置的镜像")
    if expected_model not in MODEL_NAMES or data.get("model") != expected_model:
        raise h.WorkflowError("容器实际模型与本侧冻结的 modelname 不一致")
    return data


def comparable_runtime(runtime):
    """Keep immutable launch configuration; omit Claude's generated client state.

    The image digest and entrypoint hash pin the actual launch flags.  Claude
    Code creates settings.json only after its interactive first-run prompt, so
    a clean-image probe necessarily sees it missing while a live probe sees it
    present.  The entrypoint starts Claude with --safe-mode and
    --setting-sources '', so that file is not an input to the task session.
    This projection also supports pairs frozen before this bug was corrected.
    """
    result = dict(runtime)
    files = dict(result.get("files") or {})
    files.pop("/home/node/.claude/settings.json", None)
    result["files"] = files
    return result


def freeze(h):
    image = h.run_command(["docker", "image", "inspect", "--format", "{{.Id}}", h.DOCKER_IMAGE], timeout=30).stdout.strip()
    if not image.startswith("sha256:"):
        raise h.WorkflowError("无法固定镜像摘要，请先下载本期镜像")
    result = h.run_command(["docker", "run", "--rm", "--entrypoint", "node", "-e",
                           f"ANTHROPIC_MODEL={AUTO_MODEL}", image, "-e", PROBE], timeout=60)
    env = normalize(h, result.stdout)
    return {"image": image, "harness": "Claude Code", "harness_version": env["harness_version"],
            "model": AUTO_MODEL, "context_tokens": 1_000_000,
            "operating_system": "macOS" if platform.system() == "Darwin" else platform.system(),
            "host": digest((platform.node(), platform.platform(), platform.machine())),
            "credential": credential_fingerprint(h), "runtime": comparable_runtime(env)}


def verify_host(h, config):
    if config["host"] != digest((platform.node(), platform.platform(), platform.machine())):
        raise h.WorkflowError("机器或操作系统与本 Pair 冻结配置不一致")
    if config["credential"] != credential_fingerprint(h):
        raise h.WorkflowError("API 凭据已变化，请恢复本 Pair 的原始配置")


def verify_container(h, container, config, expected_model=AUTO_MODEL):
    image = h.run_command(["docker", "inspect", "--format", "{{.Image}}", container]).stdout.strip()
    output = h.run_command(["docker", "exec", container, "node", "-e", PROBE], timeout=90).stdout
    env = normalize(h, output, expected_model)
    expected_runtime = json.loads(json.dumps(config["runtime"]))
    expected_runtime["model"] = expected_model
    expected_runtime.setdefault("env", {})["ANTHROPIC_MODEL"] = expected_model
    if image != config["image"] or comparable_runtime(env) != comparable_runtime(expected_runtime):
        raise h.WorkflowError("容器配置、Harness 版本或镜像与冻结配置不一致")
    return {"fingerprint": digest(config), "runtime": comparable_runtime(env), "image": image,
            "context_tokens": 1_000_000, "harness_version": env["harness_version"]}
