"""Build one reviewed continuation in a separate process; never start an A/B run.

Pause the service's automatic generation before invoking this module. The existing
service can claim the staged candidate after automatic generation is resumed.
"""
import argparse
import json
import threading
import uuid

from . import auto_refill as refill, candidate_bank, history, notebook, sources


def stage_once(h, task_type, progress):
    if task_type not in sources.TYPES:
        raise h.WorkflowError('独立备题只支持 Feature 迭代和 Bug 修复')
    if refill._values(h)['pairwise_auto_enabled'] == '1':
        raise h.WorkflowError('先暂停服务内自动出题；已有 A/B 不需停止')
    source = refill.continuation_source(h, task_type)
    if source is None:
        raise h.WorkflowError('当前没有可用的已归档来源，未绕过来源资格或冷却')
    if candidate_bank.has_ready(h, task_type, source):
        return {'status': 'already_ready', 'task_type': task_type}
    source = sources.claim(h, source, task_type)
    if source is None:
        raise h.WorkflowError('来源已被其他生成占用')
    key = f'pairwise-offline-prefill:{uuid.uuid4().hex[:12]}'
    previous_key = h.current_job_key()
    previous_progress = getattr(h.CODEX_JOB_CONTEXT, 'progress', None)
    stop = threading.Event()

    def keep_lease():
        while not stop.wait(30):
            try:
                if (refill._values(h)['pairwise_auto_enabled'] != '1'
                        and sources.renew(h, source)):
                    continue
            except Exception:
                pass
            h.cancel_background_job(key)
            return

    thread = threading.Thread(target=keep_lease, daemon=True)
    thread.start()
    h.CODEX_JOB_CONTEXT.key = key
    h.CODEX_JOB_CONTEXT.progress = progress
    context = None
    try:
        number = history.next_project_number(h)
        context = refill._notebook_context(h, task_type, source, number)
        progress(f'独立备题：{task_type}；冻结来源 {source["pair"]["id"]}')
        with notebook.generation(h, context, key, source['pair']['prompt']) as lessons:
            draft = refill._generate_continuation_race(
                h, source, task_type, progress, lessons['feedback'], lanes=1)
            h.ensure_job_active(key)
            if not sources.renew(h, source):
                raise h.WorkflowError('候选生成后来源租约已失效，拒绝入库')
            refill._validate_continuation_draft(h, draft, task_type)
            identifier = candidate_bank.stage(h, task_type, draft, number, source,
                                               max_ready=candidate_bank.READY_LIMIT)
            if not identifier:
                raise h.WorkflowError('候选库已满，未创建任务')
        refill._record_generation_outcome(h, task_type)
        return {'status': 'ready', 'task_type': task_type, 'candidate_id': identifier,
                'source_pair_id': source['pair']['id']}
    except Exception as exc:
        if not isinstance(exc, h.JobCancelled):
            if context is not None:
                notebook.generation_failed(h, context, key, str(exc))
            failed, cooldown = refill._source_failure_policy(h, exc)
            sources.release(h, source, str(exc), failed=failed, cooldown_after_attempt=cooldown)
            refill._record_generation_outcome(h, task_type, exc)
        raise
    finally:
        h.cancel_background_job(key)
        stop.set()
        thread.join(timeout=2)
        sources.release(h, source)
        h.CODEX_JOB_CONTEXT.key = previous_key
        h.CODEX_JOB_CONTEXT.progress = previous_progress


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('task_type', choices=sources.TYPES)
    args = parser.parse_args()
    import app as h

    def report(stage, rejection=''):
        print(json.dumps({'stage': stage, 'rejection': rejection}, ensure_ascii=False), flush=True)

    print(json.dumps(stage_once(h, args.task_type, report), ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
