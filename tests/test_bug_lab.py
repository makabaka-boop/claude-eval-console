import json

import pytest

from pairwise import bug_lab, deployment, models
from test_pairwise import host, fake_seed_snapshot, payload


def candidate_source():
    return {
        "root_number": 260, "pair_id": "a" * 12,
        "display_number": "0260", "repo_name": "transaction-history-analyzer",
        "attempt_id": "b" * 12, "side": "A", "sha": "c" * 40,
        "tree": "d" * 40, "repo_path": "/unused",
        "project_directory": "test", "project_category": "纯后端",
        "language_framework": "Go", "reproducibility": "无外部依赖",
        "verification_commands": ["go test ./..."],
    }


def insert_batch(host, batch_id, count, *, draft=None):
    now = host.now_text()
    with host.db_connection() as db:
        db.execute("INSERT INTO pairwise_bug_batches (id,status,detail,count,created_at,updated_at) "
                   "VALUES (?,'complete','',?,?,?)", (batch_id, count, now, now))
        for slot in range(1, count + 1):
            db.execute("INSERT INTO pairwise_bug_candidates "
                       "(batch_id,slot,source,status,draft) VALUES (?,?,?,'ready',?)",
                       (batch_id, slot, json.dumps(candidate_source()),
                        json.dumps(draft or {"title": f"题目 {slot}", "prompt": "场景中出现多个错误。",
                                             "estimated_lines": 400, "task_difficulty": "困难"},
                                   ensure_ascii=False)))


def test_bug_listing_is_sql_paged_and_hides_seed_evidence(host):
    draft = {"title": "任务", "prompt": "结算员复盘时发现两个错误。",
             "estimated_lines": 400, "task_difficulty": "困难",
             "seed_snapshot": {"bundle_path": "/private/seed.bundle"},
             "confirmed_bugs": [{"minimal_fix": "private repair"}]}
    insert_batch(host, "e" * 12, 12, draft=draft)
    first = bug_lab.listing(host, {"page": ["1"]})
    second = bug_lab.listing(host, {"page": ["2"]})
    assert (first["total"], first["pages"], len(first["items"]), len(second["items"])) == (12, 2, 10, 2)
    assert first["items"][0]["draft"] == {key: draft[key] for key in
                                            ("title", "prompt", "estimated_lines", "task_difficulty")}
    assert first["generation_model"] == bug_lab.BUG_BANK_MODEL
    assert first["review_model"] == bug_lab.BUG_BANK_MODEL
    assert first["reasoning_effort"] == bug_lab.BUG_BANK_REASONING_EFFORT
    assert first["latest_batch"]["target"] == 12
    assert first["latest_batch"]["passed"] == 12
    assert first["inventory_ready"] == 12
    assert first["inventory_target"] == 6
    assert "bundle_path" not in json.dumps(first)
    with pytest.raises(host.WorkflowError, match="页码"):
        bug_lab.listing(host, {"page": ["0"]})


def test_bug_progress_belongs_only_to_generating_row(host):
    insert_batch(host, "3" * 12, 3)
    with host.db_connection() as db:
        db.execute("UPDATE pairwise_bug_batches SET status='running',detail='第 2/3 道：正在设计' WHERE id=?", ("3" * 12,))
        db.execute("UPDATE pairwise_bug_candidates SET status='failed' WHERE batch_id=? AND slot=1", ("3" * 12,))
        db.execute("UPDATE pairwise_bug_candidates SET status='generating' WHERE batch_id=? AND slot=2", ("3" * 12,))
        db.execute("UPDATE pairwise_bug_candidates SET status='pending' WHERE batch_id=? AND slot=3", ("3" * 12,))
    items = bug_lab.listing(host, {"page": ["1"]})["items"]
    assert [item["batch_detail"] for item in items] == ["", "第 2/3 道：正在设计", ""]


def test_retrying_row_does_not_show_previous_timeout_as_current_failure(host):
    insert_batch(host, "a" * 12, 1)
    with host.db_connection() as db:
        db.execute("UPDATE pairwise_bug_batches SET status='running' WHERE id=?", ("a" * 12,))
        db.execute("UPDATE pairwise_bug_candidates SET status='generating',error='seeded-bug-design 超时，已停止' WHERE batch_id=?",
                   ("a" * 12,))
    item = bug_lab.listing(host, {"page": ["1"]})["items"][0]
    assert item["status"] == "generating"
    assert item["error"] == ""


def test_rejected_bug_can_be_removed_from_list_without_losing_source_history(host):
    insert_batch(host, "5" * 12, 1)
    with pytest.raises(host.WorkflowError, match="只能删除已驳回"):
        bug_lab.operate(host, "5" * 12, 1, "delete")
    bug_lab.operate(host, "5" * 12, 1, "reject")
    assert bug_lab.listing(host, {"page": ["1"]})["total"] == 1
    bug_lab.operate(host, "5" * 12, 1, "delete")
    page = bug_lab.listing(host, {"page": ["1"]})
    assert page["total"] == 0 and page["items"] == []
    with host.db_connection() as db:
        row = db.execute("SELECT status,source FROM pairwise_bug_candidates WHERE batch_id=?",
                         ("5" * 12,)).fetchone()
    assert row["status"] == "deleted"
    assert json.loads(row["source"])["pair_id"] == candidate_source()["pair_id"]


def test_bug_sources_descend_and_skip_previously_attempted_roots(host, monkeypatch, tmp_path):
    roots = [models.create(host, payload(task_type="0-1 代码生成", snapshot_url="",
                                         repo_name=f"root-{number}")) for number in range(3)]
    work = tmp_path / "archived"
    work.mkdir()
    monkeypatch.setattr(models, "attempts", lambda _h, pair_id: [{
        "id": pair_id, "side": "A", "number": 1, "status": "complete",
        "artifact_sha": "c" * 40, "artifact_tree": "d" * 40,
        "repo_path": str(work),
    }])
    ordered = bug_lab.available_sources(host)
    assert [row["pair_id"] for row in ordered] == [row["id"] for row in reversed(roots)]
    assert [row["pair_id"] for row in bug_lab.available_sources(host, 2, 3)] == [
        roots[2]["id"], roots[1]["id"]]
    insert_batch(host, "4" * 12, 1)
    with host.db_connection() as db:
        db.execute("UPDATE pairwise_bug_candidates SET status='failed',source=? WHERE batch_id=?",
                   (json.dumps({"pair_id": roots[2]["id"], "side": "A"}), "4" * 12))
    assert [row["pair_id"] for row in bug_lab.available_sources(host)] == [
        roots[1]["id"], roots[0]["id"]]
    with host.db_connection() as db:
        db.execute("UPDATE pairwise_bug_candidates SET status='deleted' WHERE batch_id=?",
                   ("4" * 12,))
    assert [row["pair_id"] for row in bug_lab.available_sources(host)] == [
        roots[1]["id"], roots[0]["id"]]


def test_bug_sources_try_archived_b_after_a_fails(host, monkeypatch, tmp_path):
    root = models.create(host, payload(task_type="0-1 代码生成", snapshot_url="",
                                       repo_name="root-with-two-arms"))
    work = tmp_path / "archived"
    work.mkdir()
    monkeypatch.setattr(models, "attempts", lambda *_: [{
        "id": side.lower() * 12, "side": side, "number": 1, "status": "complete",
        "artifact_sha": "c" * 40, "artifact_tree": "d" * 40,
        "repo_path": str(work),
    } for side in ("A", "B")])
    assert bug_lab.available_sources(host)[0]["side"] == "A"
    insert_batch(host, "a" * 12, 1)
    with host.db_connection() as db:
        db.execute("UPDATE pairwise_bug_candidates SET status='deleted',source=? WHERE batch_id=?",
                   (json.dumps({"pair_id": root["id"], "side": "A"}), "a" * 12))
    alternate = bug_lab.available_sources(host)
    assert len(alternate) == 1
    assert (alternate[0]["pair_id"], alternate[0]["side"]) == (root["id"], "B")


def test_bank_candidate_creates_one_seeded_root_pair(host, monkeypatch):
    seed = fake_seed_snapshot(host)
    draft = {"title": "事务审计", "focus_area": "事务审计多项错误",
             "prompt": "结算审计员复盘日志时，两个关联结果与实际操作不符，需要修复并核对。",
             "estimated_lines": 400, "task_difficulty": "困难",
             "seed_snapshot": seed, "difficulty_review": {}, "independent_review": {}}
    insert_batch(host, "f" * 12, 1, draft=draft)
    monkeypatch.setattr(bug_lab, "_current", lambda *_: None)
    first = bug_lab.operate(host, "f" * 12, 1, "add", confirmed=True)
    second = bug_lab.operate(host, "f" * 12, 1, "add", confirmed=True)
    assert first == second
    pair = models.task(host, first["pair_id"])
    assert pair["origin"] == "bank"
    assert pair["source_repo_url"] == ""
    assert pair["difficulty_preflight"]["seed_snapshot"] == seed
    with host.db_connection() as db:
        assert db.execute("SELECT COUNT(*) FROM pairwise_tasks WHERE id=?",
                          (first["pair_id"],)).fetchone()[0] == 1


def test_forged_bank_seed_is_rejected(host):
    seed = fake_seed_snapshot(host)
    draft = {"prompt": "实际审核过的题面", "seed_snapshot": seed}
    insert_batch(host, "1" * 12, 1, draft=draft)
    payload = {
        "title": "伪造题", "prompt": "另一道题面", "repo_name": "forged-bank-bug",
        "task_type": "Bug 修复", "difficulty": "困难",
        "language_framework": "Go", "project_category": "纯后端",
        "reproducibility": "无外部依赖", "topic_confirmed": True,
        "project_directory": "test", "verification_commands": [],
        "origin": "bank", "bank_batch_id": "1" * 12, "bank_slot": 1,
        "difficulty_preflight": {"seed_snapshot": seed},
    }
    with pytest.raises(host.WorkflowError, match="不一致"):
        models.create(host, payload)


def test_bug_worker_saves_reviewed_candidate_and_blocks_reload_while_running(host, monkeypatch):
    insert_batch(host, "2" * 12, 1)
    with host.db_connection() as db:
        db.execute("UPDATE pairwise_bug_batches SET status='running' WHERE id=?", ("2" * 12,))
        db.execute("UPDATE pairwise_bug_candidates SET status='pending' WHERE batch_id=?", ("2" * 12,))
    assert deployment.status(host)["blockers"]["draft_jobs"] == 1
    monkeypatch.setattr(bug_lab, "_current", lambda *_: ({}, {}, "/unused"))
    monkeypatch.setattr(bug_lab, "_generation_context", lambda *_: ({}, {}))
    calls = []
    monkeypatch.setattr(host, "run_codex_structured", lambda *args, **kwargs: calls.append(kwargs))
    def generate(model_host, *_):
        assert model_host.ITERATION_GENERATION_MODEL == "gpt-6-sol"
        assert model_host.REVIEW_MODEL == "gpt-6-sol"
        assert model_host.CODEX_REASONING_EFFORT == "high"
        model_host.run_codex_structured("prompt", {}, "/unused", "stage", 1,
                                        model="gpt-5.6-sol", reasoning_effort="medium")
        return {
            "title": "审计场景", "prompt": "审计员查看两项关联记录时发现结果不一致。",
            "task_difficulty": "困难", "estimated_lines": 400,
            "seed_snapshot": {"bundle_path": "/private/seed.bundle"},
        }
    monkeypatch.setattr(bug_lab.bug_generation_v2, "generate", generate)
    monkeypatch.setattr(bug_lab.candidate_bank, "overlap", lambda *_: "")
    bug_lab._generate(host, "2" * 12)
    page = bug_lab.listing(host, {"page": ["1"]})
    assert page["items"][0]["status"] == "ready"
    assert page["items"][0]["draft"]["estimated_lines"] == 400
    assert page["active_batches"] == 0
    assert deployment.status(host)["blockers"]["draft_jobs"] == 0
    assert calls == [{"model": "gpt-6-sol", "reasoning_effort": "high"}]
    assert host.REVIEW_MODEL == "gpt-5.6-sol"


def test_bug_worker_hides_failed_source_and_keeps_filling_target(host, monkeypatch):
    insert_batch(host, "6" * 12, 1)
    with host.db_connection() as db:
        db.execute("UPDATE pairwise_bug_batches SET status='running' WHERE id=?", ("6" * 12,))
        db.execute("UPDATE pairwise_bug_candidates SET status='pending' WHERE batch_id=?",
                   ("6" * 12,))
    monkeypatch.setattr(bug_lab, "_current", lambda *_: ({}, {}, "/unused"))
    monkeypatch.setattr(bug_lab, "_generation_context", lambda *_: ({}, {}))
    replacement = {**candidate_source(), "root_number": 259, "pair_id": "9" * 12}
    monkeypatch.setattr(bug_lab, "available_sources", lambda *_: [replacement])
    calls = []
    def generate(*_):
        calls.append(True)
        if len(calls) == 1:
            raise host.WorkflowError("不合格来源")
        return {"prompt": "审计员在两个关联记录中发现不一致。",
                "task_difficulty": "困难", "estimated_lines": 400}
    monkeypatch.setattr(bug_lab.bug_generation_v2, "generate", generate)
    monkeypatch.setattr(bug_lab.candidate_bank, "overlap", lambda *_: "")
    bug_lab._generate(host, "6" * 12)
    with host.db_connection() as db:
        rows = db.execute("SELECT slot,status,error FROM pairwise_bug_candidates WHERE batch_id=? ORDER BY slot",
                          ("6" * 12,)).fetchall()
        batch = db.execute("SELECT status,detail FROM pairwise_bug_batches WHERE id=?",
                           ("6" * 12,)).fetchone()
    assert [(row["slot"], row["status"]) for row in rows] == [(1, "deleted"), (2, "ready")]
    assert rows[0]["error"] == "不合格来源"
    assert batch["status"] == "complete"
    assert bug_lab.listing(host, {"page": ["1"]})["total"] == 1


def test_capacity_error_retries_same_source_without_burning_it(host, monkeypatch):
    insert_batch(host, "0" * 12, 1)
    with host.db_connection() as db:
        db.execute("UPDATE pairwise_bug_batches SET status='running' WHERE id=?", ("0" * 12,))
        db.execute("UPDATE pairwise_bug_candidates SET status='pending' WHERE batch_id=?",
                   ("0" * 12,))
    monkeypatch.setattr(bug_lab, "_current", lambda *_: ({}, {}, "/unused"))
    monkeypatch.setattr(bug_lab, "_generation_context", lambda *_: ({}, {}))
    monkeypatch.setattr(bug_lab, "available_sources", lambda *_: [])
    monkeypatch.setattr(bug_lab.time, "sleep", lambda _: None)
    calls = []
    def generate(*_):
        calls.append(True)
        if len(calls) == 1:
            raise host.UpstreamCapacityError("上游模型容量不足，稍后重试")
        return {"prompt": "审计员核对记录时发现两个关联结果不一致。",
                "task_difficulty": "困难", "estimated_lines": 400}
    monkeypatch.setattr(bug_lab.bug_generation_v2, "generate", generate)
    monkeypatch.setattr(bug_lab.candidate_bank, "overlap", lambda *_: "")
    bug_lab._generate(host, "0" * 12)
    with host.db_connection() as db:
        rows = db.execute("SELECT slot,status FROM pairwise_bug_candidates WHERE batch_id=?",
                          ("0" * 12,)).fetchall()
    assert len(calls) == 2
    assert [(row["slot"], row["status"]) for row in rows] == [(1, "ready")]


def test_pause_batch_cancels_current_source_and_resume_restores_pending(host, monkeypatch):
    insert_batch(host, "d" * 12, 1)
    with host.db_connection() as db:
        db.execute("UPDATE pairwise_bug_batches SET status='running' WHERE id=?", ("d" * 12,))
        db.execute("UPDATE pairwise_bug_candidates SET status='generating',error='old timeout' WHERE batch_id=?",
                   ("d" * 12,))
    cancelled = []
    monkeypatch.setattr(host, "cancel_background_job", lambda key: cancelled.append(key))
    result = bug_lab.pause_batch(host, "d" * 12)
    assert result["status"] == "paused"
    assert cancelled == ["bug-bank:" + "d" * 12]
    with host.db_connection() as db:
        row = db.execute("SELECT status,error FROM pairwise_bug_candidates WHERE batch_id=?",
                         ("d" * 12,)).fetchone()
    assert (row["status"], row["error"]) == ("pending", "")
    class FakeThread:
        def __init__(self, **_):
            pass
        def start(self):
            pass
    monkeypatch.setattr(bug_lab.threading, "Thread", FakeThread)
    assert bug_lab.continue_batch(host, "d" * 12)["status"] == "queued"


def test_skipping_running_source_cancels_model_and_stale_result_cannot_restore_it(host, monkeypatch):
    insert_batch(host, "f" * 12, 1)
    with host.db_connection() as db:
        db.execute("UPDATE pairwise_bug_batches SET status='running' WHERE id=?", ("f" * 12,))
        db.execute("UPDATE pairwise_bug_candidates SET status='generating' WHERE batch_id=?",
                   ("f" * 12,))
    cancelled = []
    monkeypatch.setattr(host, "cancel_background_job", lambda key: cancelled.append(key))
    assert bug_lab.operate(host, "f" * 12, 1, "skip")["skipped"] is True
    assert cancelled == ["bug-bank:" + "f" * 12]
    assert not bug_lab._active_slot_update(host, "f" * 12, 1, "generating", "ready",
                                           "stale result", draft={"prompt": "incorrect"})
    assert bug_lab.listing(host, {"page": ["1"]})["total"] == 0
    with host.db_connection() as db:
        assert db.execute("SELECT status FROM pairwise_bug_candidates WHERE batch_id=?",
                          ("f" * 12,)).fetchone()[0] == "deleted"


@pytest.mark.parametrize("cancellation_raised", [False, True])
def test_worker_moves_to_next_source_after_current_source_is_skipped(
        host, monkeypatch, cancellation_raised):
    insert_batch(host, "9" * 12, 2)
    with host.db_connection() as db:
        db.execute("UPDATE pairwise_bug_batches SET status='running' WHERE id=?", ("9" * 12,))
        db.execute("UPDATE pairwise_bug_candidates SET status='pending' WHERE batch_id=?",
                   ("9" * 12,))
    monkeypatch.setattr(bug_lab, "available_sources", lambda *_: [])
    monkeypatch.setattr(bug_lab, "_current", lambda *_: ({}, {}, "/unused"))
    monkeypatch.setattr(bug_lab, "_generation_context", lambda *_: ({}, {}))
    monkeypatch.setattr(bug_lab.candidate_bank, "overlap", lambda *_: "")
    calls = []
    def generate(*_):
        calls.append(True)
        if len(calls) == 1:
            bug_lab.operate(host, "9" * 12, 1, "skip")
            if cancellation_raised:
                raise host.JobCancelled("source skipped")
        return {"prompt": "审计员核对记录时发现两个关联结果不一致。",
                "task_difficulty": "困难", "estimated_lines": 400}
    monkeypatch.setattr(bug_lab.bug_generation_v2, "generate", generate)
    bug_lab._generate(host, "9" * 12)
    with host.db_connection() as db:
        rows = db.execute("SELECT slot,status FROM pairwise_bug_candidates WHERE batch_id=? ORDER BY slot",
                          ("9" * 12,)).fetchall()
    assert len(calls) == 2
    assert [(row["slot"], row["status"]) for row in rows] == [(1, "deleted"), (2, "ready")]


def test_bug_worker_prioritizes_higher_number_b_before_lower_pending_a(host, monkeypatch):
    insert_batch(host, "b" * 12, 1)
    lower = {**candidate_source(), "root_number": 273, "side": "A"}
    higher = {**candidate_source(), "root_number": 279, "side": "B", "pair_id": "9" * 12}
    with host.db_connection() as db:
        db.execute("UPDATE pairwise_bug_batches SET status='running' WHERE id=?", ("b" * 12,))
        db.execute("UPDATE pairwise_bug_candidates SET status='pending',source=? WHERE batch_id=?",
                   (json.dumps(lower), "b" * 12))
    chosen = []
    monkeypatch.setattr(bug_lab, "available_sources", lambda *_: [higher])
    monkeypatch.setattr(bug_lab, "_current", lambda _h, source: (chosen.append(source), {}, "/unused"))
    monkeypatch.setattr(bug_lab, "_generation_context", lambda *_: ({}, {}))
    monkeypatch.setattr(bug_lab.bug_generation_v2, "generate", lambda *_: {
        "prompt": "审计员核对记录时发现两个关联结果不一致。",
        "task_difficulty": "困难", "estimated_lines": 400,
    })
    monkeypatch.setattr(bug_lab.candidate_bank, "overlap", lambda *_: "")
    bug_lab._generate(host, "b" * 12)
    assert chosen[0]["root_number"] == 279 and chosen[0]["side"] == "B"
    with host.db_connection() as db:
        statuses = [row[0] for row in db.execute(
            "SELECT status FROM pairwise_bug_candidates WHERE batch_id=? ORDER BY slot", ("b" * 12,))]
    assert statuses == ["ready"]


def test_bug_worker_reports_source_exhaustion_instead_of_claiming_target(host, monkeypatch):
    insert_batch(host, "7" * 12, 1)
    with host.db_connection() as db:
        db.execute("UPDATE pairwise_bug_batches SET status='running' WHERE id=?", ("7" * 12,))
        db.execute("UPDATE pairwise_bug_candidates SET status='pending' WHERE batch_id=?",
                   ("7" * 12,))
    monkeypatch.setattr(bug_lab, "_current", lambda *_: ({}, {}, "/unused"))
    monkeypatch.setattr(bug_lab, "_generation_context", lambda *_: ({}, {}))
    monkeypatch.setattr(bug_lab.bug_generation_v2, "generate",
                        lambda *_: (_ for _ in ()).throw(host.WorkflowError("不合格来源")))
    monkeypatch.setattr(bug_lab, "available_sources", lambda *_: [])
    bug_lab._generate(host, "7" * 12)
    with host.db_connection() as db:
        batch = db.execute("SELECT status,detail FROM pairwise_bug_batches WHERE id=?",
                           ("7" * 12,)).fetchone()
    assert batch["status"] == "exhausted"
    assert "0/1" in batch["detail"]
    assert bug_lab.listing(host, {"page": ["1"]})["total"] == 0


def test_six_question_reserve_counts_only_unbound_ready_stock(host, monkeypatch):
    insert_batch(host, "c" * 12, 6)
    with host.db_connection() as db:
        db.execute("UPDATE pairwise_bug_candidates SET status='added',pair_id='taken' "
                   "WHERE batch_id=? AND slot=1", ("c" * 12,))
        db.execute("UPDATE pairwise_bug_batches SET status='running' WHERE id=?", ("c" * 12,))
    monkeypatch.setattr(bug_lab, "available_sources", lambda *_: [])
    bug_lab._generate(host, "c" * 12)
    page = bug_lab.listing(host, {"page": ["1"]})
    assert page["inventory_ready"] == 5
    assert page["latest_batch"]["passed"] == 5
    assert page["latest_batch"]["status"] == "exhausted"


def test_rejecting_from_six_question_reserve_queues_automatic_refill(host, monkeypatch):
    insert_batch(host, "d" * 12, 6)
    started = []
    class FakeThread:
        def __init__(self, *, target, args, **_):
            started.append((target, args))
        def start(self):
            pass
    monkeypatch.setattr(bug_lab.threading, "Thread", FakeThread)
    bug_lab.operate(host, "d" * 12, 1, "reject")
    assert started == [(bug_lab._generate, (host, "d" * 12))]
    page = bug_lab.listing(host, {"page": ["1"]})
    assert page["inventory_ready"] == 5
    assert page["latest_batch"]["status"] == "queued"
    bug_lab.operate(host, "d" * 12, 2, "reject")
    assert len(started) == 1


def test_adding_from_six_question_reserve_queues_automatic_refill(host, monkeypatch):
    seed = fake_seed_snapshot(host)
    insert_batch(host, "e" * 12, 6, draft={
        "prompt": "结算审计员复盘日志时，两个关联结果与实际操作不符，需要修复并核对。",
        "estimated_lines": 400, "task_difficulty": "困难", "seed_snapshot": seed,
    })
    monkeypatch.setattr(bug_lab, "_current", lambda *_: None)
    started = []
    class FakeThread:
        def __init__(self, *, target, args, **_):
            started.append((target, args))
        def start(self):
            pass
    monkeypatch.setattr(bug_lab.threading, "Thread", FakeThread)
    bug_lab.operate(host, "e" * 12, 1, "add", confirmed=True)
    assert started == [(bug_lab._generate, (host, "e" * 12))]
    assert bug_lab.listing(host, {"page": ["1"]})["inventory_ready"] == 5


def test_old_partial_batch_can_continue_with_failed_rows_hidden(host, monkeypatch):
    insert_batch(host, "8" * 12, 2)
    with host.db_connection() as db:
        db.execute("UPDATE pairwise_bug_batches SET status='partial' WHERE id=?", ("8" * 12,))
        db.execute("UPDATE pairwise_bug_candidates SET status='failed' WHERE batch_id=? AND slot=1",
                   ("8" * 12,))
    started = []
    class FakeThread:
        def __init__(self, *, target, args, **_):
            started.append((target, args))
        def start(self):
            pass
    monkeypatch.setattr(bug_lab.threading, "Thread", FakeThread)
    result = bug_lab.continue_batch(host, "8" * 12)
    assert result["status"] == "queued"
    assert started == [(bug_lab._generate, (host, "8" * 12))]
    with host.db_connection() as db:
        statuses = [row[0] for row in db.execute(
            "SELECT status FROM pairwise_bug_candidates WHERE batch_id=? ORDER BY slot", ("8" * 12,))]
    assert statuses == ["deleted", "ready"]


def test_new_batch_keeps_requested_target_when_fewer_sources_are_initially_available(host, monkeypatch):
    monkeypatch.setattr(bug_lab, "available_sources", lambda *_: [candidate_source()])
    started = []
    class FakeThread:
        def __init__(self, *, target, args, **_):
            started.append((target, args))
        def start(self):
            pass
    monkeypatch.setattr(bug_lab.threading, "Thread", FakeThread)
    result = bug_lab.create(host, {"count": 3, "allow_seed": True})
    assert result["count"] == 3 and result["selected_sources"] == 1
    with host.db_connection() as db:
        batch = db.execute("SELECT count,status FROM pairwise_bug_batches WHERE id=?",
                           (result["id"],)).fetchone()
    assert (batch["count"], batch["status"]) == (3, "queued")
    assert started == [(bug_lab._generate, (host, result["id"]))]


def test_new_bug_batch_defaults_to_six_ready_questions(host, monkeypatch):
    monkeypatch.setattr(bug_lab, "available_sources", lambda *_: [candidate_source()])
    class FakeThread:
        def __init__(self, **_):
            pass
        def start(self):
            pass
    monkeypatch.setattr(bug_lab.threading, "Thread", FakeThread)
    result = bug_lab.create(host, {"allow_seed": True})
    assert result["count"] == 6


def test_bug_batch_does_not_leave_pending_rows_when_stock_is_already_full(host, monkeypatch):
    insert_batch(host, "1" * 12, 6)
    monkeypatch.setattr(bug_lab, "available_sources", lambda *_: [])
    result = bug_lab.create(host, {"count": 6, "allow_seed": True})
    assert result["satisfied"] is True
    with host.db_connection() as db:
        assert db.execute("SELECT COUNT(*) FROM pairwise_bug_batches").fetchone()[0] == 1
    insert_batch(host, "2" * 12, 1)
    with host.db_connection() as db:
        db.execute("UPDATE pairwise_bug_batches SET status='running' WHERE id=?", ("2" * 12,))
        db.execute("UPDATE pairwise_bug_candidates SET status='pending' WHERE batch_id=?",
                   ("2" * 12,))
    bug_lab._generate(host, "2" * 12)
    with host.db_connection() as db:
        assert db.execute("SELECT COUNT(*) FROM pairwise_bug_candidates WHERE batch_id=?",
                          ("2" * 12,)).fetchone()[0] == 0


def test_paused_batch_retries_interrupted_source(host, monkeypatch):
    insert_batch(host, "9" * 12, 1)
    with host.db_connection() as db:
        db.execute("UPDATE pairwise_bug_batches SET status='paused' WHERE id=?", ("9" * 12,))
        db.execute("UPDATE pairwise_bug_candidates SET status='generating',error='old error' WHERE batch_id=?",
                   ("9" * 12,))
    class FakeThread:
        def __init__(self, **_):
            pass
        def start(self):
            pass
    monkeypatch.setattr(bug_lab.threading, "Thread", FakeThread)
    bug_lab.continue_batch(host, "9" * 12)
    with host.db_connection() as db:
        row = db.execute("SELECT status,error FROM pairwise_bug_candidates WHERE batch_id=?",
                         ("9" * 12,)).fetchone()
    assert (row["status"], row["error"]) == ("pending", "")
