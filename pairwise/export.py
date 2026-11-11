import json
import shutil
import tempfile
from pathlib import Path

from . import models as m
from .validation import preflight

COLUMNS = ["User Prompt", "任务类型", "任务难度", "语言/框架", "Harness", "Harness 版本", "操作系统",
           "环境可复现等级", "初始环境快照", "A-SessionID", "A-轨迹文件", "A-产物快照", "A-运行录屏",
           "A-交付完整性", "A-交付完整性描述", "A-模型名称",
           "B-SessionID", "B-轨迹文件", "B-产物快照", "B-运行录屏",
           "B-交付完整性", "B-交付完整性描述", "B-模型名称", "GSB 结论", "GSB 理由", "备注"]


def row(h, pair_id):
    result = preflight(h, pair_id)
    if not result["eligible"]:
        raise h.WorkflowError("交付资料不全：" + "；".join(result["issues"]))
    pair = m.task(h, pair_id)
    config, items = pair["config"], m.attempts(h, pair_id)
    values = [pair["prompt"], pair["task_type"], result["submission_difficulty"], pair["language_framework"],
              config["harness"], config["harness_version"], config["operating_system"],
              pair["reproducibility"], f"{pair['repo_url']}/commit/{pair['base_sha']}"]
    for side in ("A", "B"):
        a = m.latest(items, side)
        values.extend([
            a["session_id"], Path(a["trace_path"]).name,
            f"{pair['repo_url']}/commit/{a['artifact_sha']}", Path(a["recording_path"]).name,
            pair[f"{side.lower()}_delivery_score"],
            pair[f"{side.lower()}_delivery_description"],
            m.actual_model_name(a),
        ])
    values.extend([pair["gsb"], pair["reason"], pair["notes"]])
    return values


def workbook(h, ids):
    if not isinstance(ids, list) or not ids or len(ids) > 500 or len(set(ids)) != len(ids):
        raise h.WorkflowError("请选择 1 至 500 条不重复的 Pair")
    rows = [row(h, pair_id) for pair_id in ids]
    if not h.ARTIFACT_NODE_EXECUTABLE.is_file() or not h.ARTIFACT_NODE_MODULES.is_dir():
        raise h.WorkflowError("Excel 运行时未就绪，请配置 CLAUDE_EVAL_ARTIFACT_NODE / NODE_MODULES")
    with tempfile.TemporaryDirectory(prefix="pairwise-export-") as directory:
        root = Path(directory)
        (root / "node_modules").symlink_to(h.ARTIFACT_NODE_MODULES, target_is_directory=True)
        shutil.copyfile(h.APP_DIR / "scripts" / "export_pairwise.mjs", root / "export.mjs")
        (root / "input.json").write_text(json.dumps({"columns": COLUMNS, "rows": rows, "generated_at": h.now_text()}, ensure_ascii=False), encoding="utf-8")
        h.run_command([str(h.ARTIFACT_NODE_EXECUTABLE), str(root / "export.mjs"),
                       str(root / "input.json"), str(root / "pairwise.xlsx")], timeout=120)
        return (root / "pairwise.xlsx").read_bytes()
