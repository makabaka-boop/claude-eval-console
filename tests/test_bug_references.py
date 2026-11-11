from pairwise import bug_references
from test_pairwise import host


def test_reference_import_is_deduplicated(host):
    prompts = [
        "React 页面保存每日记录后统计卡片不刷新，重新进入才显示新记录；筛选列表仍保留旧汇总，用户会依据过期状态继续操作。请保持现有数据流并修复相关同步问题。",
        "Django 排班把首尾相接的预约误判为冲突，新增和编辑都会被阻断；取消预约仍按原规则排除。请保持现有技术栈，修复时间区间边界并验证冲突列表。",
    ]
    first = bug_references.store_many(host, prompts, source_label="examples.txt")
    second = bug_references.store_many(host, prompts, source_label="examples.txt")
    assert first["inserted"] == 2
    assert second["inserted"] == 0
    assert second["duplicates"] == 2
    assert bug_references.count(host) == 2


def test_relevant_examples_prefer_matching_stack_and_domain(host):
    bug_references.store_many(host, [
        "Vue 页面里的搜索词与 URL query 不同步，刷新后输入框清空但结果仍被过滤；直接访问带参数地址也无法回填。请保持现有路由和请求方式并修复状态同步。",
        "Django 排班把首尾相接的预约误判为冲突，新增和编辑都会被阻断；取消预约仍按原规则排除。请保持现有技术栈并修复时间区间边界判断。",
    ])
    records = bug_references.relevant(host, {
        "language_framework": "Vue 3, Vite",
        "original_or_current_prompt": "搜索页面使用 query 参数保存筛选词",
    }, limit=1)
    assert len(records) == 1
    assert "搜索词" in records[0]["summary"]
    assert "prompt" not in records[0]
    assert records[0]["reference_id"].startswith("bugref:")


def test_reference_retrieval_is_capped_at_three_short_summaries(host):
    bug_references.store_many(host, [
        f"Vue 搜索页面第 {index} 个状态不同步问题；刷新后仍显示旧结果。" + "边界描述。" * 80
        for index in range(5)
    ])
    records = bug_references.relevant(host, {
        "language_framework": "Vue 3",
        "original_or_current_prompt": "搜索页面状态",
    }, limit=8)
    assert len(records) == 3
    assert all(len(record["summary"]) <= 320 for record in records)
    assert all("prompt" not in record for record in records)


def test_v2_history_keeps_prompt_references_separate():
    from pairwise import bug_generation_v2
    materials = bug_generation_v2._history_materials({
        "bug_prompt_references": [{"reference_id": "bugref:abc", "prompt": "状态不同步。"}]
    })
    assert materials["prompt_references"] == [
        {"reference_id": "bugref:abc", "summary": "状态不同步。"}]
    assert bug_generation_v2._history_materials(
        {"bug_prompt_references": [{"reference_id": "bugref:abc", "prompt": "状态不同步。"}]},
        include_prompt_references=False,
    )["prompt_references"] == []


def test_cookie_credentials_are_redacted_before_storage(host):
    bug_references.store_many(host, [
        "添加账号时 Cookie 无效，请排查 cookie 为“web_session=secret-token; id_token=secret-jwt; webId=public-id”；"
        "同时修复内容去重和错误提示，保持现有接口与页面流程。"
    ])
    with host.db_connection() as db:
        prompt = db.execute(
            "SELECT prompt FROM pairwise_bug_prompt_references").fetchone()[0]
    assert "secret-token" not in prompt and "secret-jwt" not in prompt
    assert "web_session=<REDACTED>" in prompt
