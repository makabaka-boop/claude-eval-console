"""Shared field vocabulary and immutable model policy for November samples."""
import re
from urllib.parse import urlparse

VERSION = "2026.11-domain-feedback-v1"
EXECUTION_MODEL = "auto_model/urm"
ANALYSIS_MODEL = "gpt-6.1-sol"
ANALYSIS_EFFORT = "high"
CONTEXT_TOKENS = 1_000_000
DOMAINS = {
    "安全": ("漏洞检测与攻防", "身份认证", "网络与通信安全", "系统安全", "其他"),
    "游戏、图形与多媒体": ("音视频处理", "图形渲染", "游戏开发", "图像处理", "其他"),
    "数据工程": ("数据采集、爬虫与 RPA", "数仓与数据湖", "数据分析与数据应用", "其他"),
}
TASK_TYPES = ("功能开发", "Bugfix", "代码重构", "功能优化", "依赖升级与迁移", "质量保障",
              "项目维护", "技术讨论", "问题排查与诊断", "部署运维", "泛 Agent 需求")
DIFFICULTIES = ("困难", "地狱")
REPRODUCIBILITY = ("无外部依赖", "有外部依赖，未容器化", "已容器化，可一键起环境")
ACTIVE = ("queued", "starting", "running", "resuming", "archiving")
SHA = re.compile(r"^[0-9a-f]{40}$")
SNAPSHOT = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/commit/([0-9a-fA-F]{40})/?$")


def snapshot(value):
    match = SNAPSHOT.fullmatch(str(value or "").strip())
    if not match:
        raise ValueError("初始快照须为 GitHub commit 链接，包含 40 位完整 SHA")
    owner, repo, sha = match.groups()
    if owner in {".", ".."} or repo in {".", ".."}:
        raise ValueError("仓库地址无效")
    return f"https://github.com/{owner}/{repo}", sha.lower(), repo


def public_url(value):
    if not value:
        return False
    parsed = urlparse(str(value))
    return bool(parsed.scheme == "https" and parsed.hostname
                and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
                and not parsed.username and not parsed.password)


def schema():
    return {"policy_version": VERSION, "workflow_mode": "domain_feedback",
            "domains": DOMAINS, "task_types": TASK_TYPES, "difficulties": DIFFICULTIES,
            "reproducibility": REPRODUCIBILITY,
            "execution_model": EXECUTION_MODEL, "context_tokens": CONTEXT_TOKENS,
            "analysis_model": ANALYSIS_MODEL, "analysis_effort": ANALYSIS_EFFORT,
            "submission_configured": False}
