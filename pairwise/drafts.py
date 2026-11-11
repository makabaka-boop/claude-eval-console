"""Reuse question-only generation. No attempt artifacts or trajectories are supplied."""
import json
import threading
import uuid

from . import history, notebook, candidate_bank

GUIDANCE = """本期为 09/16 Pair-wise GSB，只跑首轮，要求人类执行难度达到困难或地狱。
禁止经典小游戏及其变种（贪吃蛇、打砖块、俄罗斯方块、坦克大战、塔防、2D解谜、潜行、平台跳跃、
喂食小动物、翻牌、连连看、棋类、2048、扫雷、打地鼠），禁止粒子物理模拟（星系、烟花、落沙、布料）；
禁止 CLI、代码片段管理器、批量重命名、截图标注、文件管理/同步、书签、密码管理器；
禁止电商购物车/订单、RBAC后台、仓库库存、投票问卷、考勤OA、图书借阅、博客CMS、医院挂号/问诊、
外卖点餐、CRM、IM私信、拍卖、停车场、工单客服、商品加Excel导入、积分商城、各种预约系统；
禁止报表统计、Streamlit、CSV看板、记账、健康健身、菜谱、天气、番茄钟、习惯打卡、音乐播放器、旅行日记、观影记录。
0-1 围绕一个明确用户目标形成可持续使用、可直接验收的完整闭环；一条足够深的工作流即可，
不强制拆成两个功能模块。只有粘贴 JSON、一次求解、展示答案的窄流程不适合本批。
不要为了拓宽流程添加无关子系统或额外复杂机制。
本批返回两个互不相似的候选；每个候选只包含一份完整题面和一组固定验收命令，
不附带评分、GSB结论或评价理由。"""


def get(h, draft_id):
    with h.db_connection() as db:
        row = db.execute("SELECT * FROM pairwise_drafts WHERE id=?", (draft_id,)).fetchone()
    if not row:
        raise h.WorkflowError("题面草稿不存在")
    result = dict(row)
    result["result"] = json.loads(result["result"])
    return result


def create(h):
    with h.db_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        running = db.execute("SELECT id FROM pairwise_drafts WHERE status IN ('queued','running') LIMIT 1").fetchone()
        if running:
            draft_id = running[0]
        else:
            draft_id = uuid.uuid4().hex[:12]
            db.execute("INSERT INTO pairwise_drafts(id,status,created_at) VALUES (?,'queued',?)", (draft_id,h.now_text()))
    if running:
        return get(h,draft_id)

    def update(status, detail, result=None):
        with h.db_connection() as db:
            db.execute("UPDATE pairwise_drafts SET status=?,detail=?,result=? WHERE id=?",
                       (status,detail,json.dumps(result or {},ensure_ascii=False),draft_id))

    def worker():
        try:
            update("running","正在生成困难级 0-1 题面")
            number=history.next_project_number(h)
            context={"task_type":"0-1 代码生成","project_category":h.category_for_project_number(number)}
            with notebook.generation(h,context,f"draft:{draft_id}") as lessons:
                draft=h.generate_task_draft(
                    project_number=number,
                    base_policy=GUIDANCE,
                    history_lessons=lessons["feedback"],
                    progress=lambda text:update("running",text),
                    history_override=history.records(h),
                )
                overlap = candidate_bank.overlap(h, draft.get("first_prompt", ""))
                if overlap:
                    raise h.WorkflowError(overlap)
            update("complete","题面已生成，请人工核对再创建 Pair",draft)
        except Exception as exc:
            notebook.generation_failed(h,{"task_type":"0-1 代码生成"},f"draft:{draft_id}",str(exc))
            update("failed",str(exc))
    threading.Thread(target=worker,daemon=True).start()
    return get(h,draft_id)
