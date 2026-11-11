#!/usr/bin/env python3
"""Import a pasted collection of Bug prompt examples into the local database."""

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app
from pairwise import bug_references


STARTS = (
    "请修复 Chrome", "你是本仓库的全栈修复负责人", "bug修复：有几个问题",
    "bug修复：单位管理", "你是本仓库维护者", "本仓库是「定时提醒系统」",
    "项目：LORE", "你在维护 **LovEat", "请先把 app.js", "深入理解整个系统",
    "深入阅读这个全栈系统", "bug修复：我这鱼", "bug修复：店里排班",
    "目前匿名忏悔室", "工艺协调员", "导入存量 JSON", "bug修复：最近民政口",
    "bug修复：试用的时候", "bug修复：有两个问题", "输入、状态机、物理",
    "系统出现四个故障", "宠物健康 PWA", "请完整修复可视化 SQL",
    "链路修订 API", "高斯别名工作台", "bug修复：先是权限",
    "请逐项定位并修复", "在线课堂平台", "请先修复程序化地形",
    "排练现场连续性核验平台",
)


def split_prompts(text):
    chunks = []
    current = []
    for line in text.replace("\r\n", "\n").splitlines():
        stripped = line.strip()
        if any(stripped.startswith(prefix) for prefix in STARTS) and any(
                item.strip() for item in current):
            chunks.append("\n".join(current).strip())
            current = []
        current.append(line)
    if any(item.strip() for item in current):
        chunks.append("\n".join(current).strip())
    return [re.sub(r"\n{3,}", "\n\n", chunk) for chunk in chunks if len(chunk) >= 80]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument("--source-label", default="")
    args = parser.parse_args()
    prompts = split_prompts(args.path.read_text(encoding="utf-8"))
    result = bug_references.store_many(
        app, prompts, source="user_attachment",
        source_label=args.source_label or args.path.name)
    print({**result, "parsed": len(prompts)})


if __name__ == "__main__":
    main()
