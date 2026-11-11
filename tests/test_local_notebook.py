import json
from unittest import mock

import pytest

from test_pairwise import host, ready_pair, sample_difficulty
from pairwise import notebook as book, models, auto_refill


CONTEXT = {"task_type": "0-1 代码生成", "project_category": "纯后端"}
CANDIDATE = {"title": "真实候选", "prompt": "从空仓库实现二分匹配查询", "engineering_core": "二分匹配"}


def reject(host, **kwargs):
    defaults = dict(stage="出题预审", reason="与历史二分匹配题重复", candidate=CANDIDATE, context=CONTEXT)
    return book.record(host, **{**defaults, **kwargs})


def entry(host, entry_id):
    return next(x for x in book.list_entries(host, {})["items"] if x["id"] == entry_id)


def test_exact_delivery_is_idempotent_and_repeated_rejections_keep_evidence(host):
    first = reject(host, job_id="attempt-1", review={"history_overlap": True})
    assert reject(host, job_id="attempt-1", review={"history_overlap": True}) == first
    assert reject(host, job_id="attempt-2", review={"history_overlap": True}) == first
    item = entry(host, first)
    assert item["occurrences"] == 2 and len(item["events"]) == 2
    assert item["prompt"] == CANDIDATE["prompt"]
    assert item["material"]["review"]["history_overlap"] is True
    text = book.file_path(host).read_text()
    assert "# GSB 本地补题错题本" in text and CANDIDATE["prompt"] in text
    assert "全部审核与后续记录" in text


def test_corrections_stop_retrieval_preserve_history_and_reject_stale_edits(host):
    key = reject(host)
    assert book.related(host, CONTEXT)[0]["id"] == key
    book.annotate(host, key, {"status": "dismissed", "note": "已核对：两题约束与结果不同", "revision": 0})
    assert not book.related(host, CONTEXT)
    assert entry(host, key)["prompt"] == CANDIDATE["prompt"]
    with pytest.raises(host.WorkflowError, match="已被修改"):
        book.annotate(host, key, {"status": "resolved", "note": "过期操作", "revision": 0})
    reject(host, job_id="later")
    assert entry(host, key)["status"] == "dismissed"
    assert not book.related(host, CONTEXT)
    assert [e["kind"] for e in entry(host, key)["events"]].count("annotation") == 1


@pytest.mark.parametrize("payload", [
    {"status": "resolved", "note": "", "revision": 0},
    {"status": "invented", "note": "理由", "revision": 0},
    {"status": "resolved", "note": "理由", "revision": True},
])
def test_annotations_require_reason_and_valid_revision(host, payload):
    key = reject(host)
    with pytest.raises(host.WorkflowError):
        book.annotate(host, key, payload)


def test_related_lessons_are_bounded_ranked_and_exclude_infrastructure(host):
    for i in range(6):
        reject(host, candidate={"title": str(i), "prompt": f"一般事务处理 {i}"})
    exact = reject(host, candidate={"title": "匹配关键字", "prompt": "二分匹配 增广路径"})
    reject(host, stage="生成故障", reason="模型调用超时")
    items = book.related(host, CONTEXT, "二分匹配 增广路径")
    assert len(items) == 3 and items[0]["id"] == exact
    assert all(x["category"] != "生成故障" for x in items)
    assert not book.related(host, {"task_type": "Bug 修复", "project_category": "全栈"})


def test_observer_captures_actual_candidate_and_restores_outer_context(host, monkeypatch):
    from test_app import DraftTests
    previous = mock.Mock()
    monkeypatch.setattr(host.CODEX_JOB_CONTEXT, "notebook_observer", previous, raising=False)
    fixtures = DraftTests()
    rejected = fixtures.scope_review()
    rejected["approved"] = False
    rejected["difficulty_review"].update(level="中等", routine_solution_sufficient=True)
    with mock.patch.object(host, "run_codex_task_generation", return_value=fixtures.candidate()), \
         mock.patch.object(host, "run_codex_task_validation", return_value=rejected), \
         book.generation(host, CONTEXT, "observed"):
        with pytest.raises(host.WorkflowError):
            host.generate_task_draft(1, history_override=[])
    assert host.CODEX_JOB_CONTEXT.notebook_observer is previous
    previous.assert_not_called()
    item = book.list_entries(host, {})["items"][0]
    assert item["prompt"] == fixtures.candidate()["prompt"]
    assert item["category"] == "难度不足" and item["stage"] == "出题预审"


def test_followup_pass_is_linked_without_claiming_original_error_fixed(host):
    key = reject(host)
    with book.generation(host, CONTEXT, "followup") as lessons:
        assert key in lessons["ids"] and "不是全局禁题规则" in lessons["feedback"]
        host.record_generation_review("出题预审", {"prompt": "换了方向的新题"}, outcome="passed")
    item = entry(host, key)
    assert item["status"] == "open" and item["occurrences"] == 1
    assert item["events"][0]["kind"] == "preflight_passed"
    assert "不代表原问题已解决" in item["events"][0]["data"]["detail"]


def test_post_review_downgrade_and_later_result_are_kept(host):
    pair_id, _, _ = ready_pair(host)
    pair = models.task(host, pair_id)
    review = sample_difficulty("中等")
    book.initialize(host)
    book.post_review(host, pair, review, "snapshot-v1")
    book.post_review(host, pair, review, "snapshot-v1")
    item = book.list_entries(host, {})["items"][0]
    assert item["category"] == "难度不足" and item["occurrences"] == 1
    book.post_review(host, pair, sample_difficulty(), "snapshot-v1")
    assert entry(host, item["id"])["events"][0]["kind"] == "difficulty_reviewed"
    assert entry(host, item["id"])["status"] == "open"


def test_historical_import_is_partial_idempotent_and_does_not_change_gates(host):
    auto_refill.initialize(host)
    host.write_settings({"pairwise_auto_blocked_sources": json.dumps({"Feature 迭代:missing-root": "规则 C：重复操作"}),
                         "pairwise_auto_last_rejection": "候选复核与一次定向改写后仍不合规：匹配题重复"})
    before = host.settings_values(("pairwise_auto_enabled", "pairwise_auto_blocked_sources"))
    book.backfill(host)
    first = book.list_entries(host, {})
    book.backfill(host)
    second = book.list_entries(host, {})
    assert first["total"] == second["total"] == 2
    assert all(x["prompt"] == "" and x["occurrences"] == 1 for x in first["items"])
    assert "原题面未保存" in first["items"][0]["material"]["evidence_status"]
    assert host.settings_values(tuple(before)) == before


def test_search_filters_and_pagination(host, monkeypatch):
    monkeypatch.setattr(book, "PAGE_SIZE", 1)
    reject(host, candidate={"title": "匹配题", "prompt": "第一题"})
    reject(host, candidate={"title": "状态题", "prompt": "第二题"}, reason="最小方案难度不足")
    assert book.list_entries(host, {})["pages"] == 2
    assert book.list_entries(host, {"query": ["状态题"]})["total"] == 1
    assert book.list_entries(host, {"category": ["难度不足"]})["total"] == 1
    assert book.list_entries(host, {"page": ["99"]})["page"] == 2


def test_refill_injects_related_lessons_without_starting_unapproved_pair(host, monkeypatch):
    key = reject(host)
    seen = {}
    def generate(**kwargs):
        seen.update(kwargs)
        raise host.WorkflowError("模型调用超时")
    monkeypatch.setattr(host, "generate_task_draft", generate)
    monkeypatch.setattr(host, "category_for_project_number", lambda _: "纯后端")
    auto_refill.configure(host, {"enabled": True})
    assert not auto_refill._prefill_once(host)
    assert key in seen["initial_feedback"]
    assert CANDIDATE["prompt"] in seen["initial_feedback"]
    with host.db_connection() as db:
        assert db.execute("SELECT COUNT(*) FROM pairwise_tasks").fetchone()[0] == 0


def test_generated_markdown_keeps_untrusted_fences_inside_data(host):
    reject(host, candidate={"prompt": "```\n<script>unsafe</script>\n```"})
    assert "````\n```\n<script>unsafe</script>\n```\n````" in book.markdown(host)


def test_concurrent_records_are_not_lost(host):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as pool:
        keys = list(pool.map(lambda n: reject(host, job_id=f"concurrent-{n}"), range(8)))
    assert len(set(keys)) == 1
    assert entry(host, keys[0])["occurrences"] == 8
    assert book.file_path(host).read_text() == book.markdown(host)


def test_human_correction_also_removes_short_retry_feedback(host):
    reason = "候选复核与一次定向改写后仍不合规：二分匹配题重复"
    key = reject(host, reason=reason)
    values = {"pairwise_auto_last_rejection": reason}
    assert reason in auto_refill._retry_feedback(host, values, "0-1 代码生成")
    book.annotate(host, key, {"status": "dismissed", "note": "原对照题不适用", "revision": 0})
    assert not auto_refill._retry_feedback(host, values, "0-1 代码生成")


def test_notebook_feedback_is_not_sent_to_blind_difficulty_judge(host):
    data = host.difficulty_policy.blind_materials({"prompt": "当前真实要求", "_concept_retry_feedback": "错题本判断"})
    assert data == {"prompt": "当前真实要求"}
