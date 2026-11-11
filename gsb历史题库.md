# GSB 历史题库

这是 09/16 Pair-wise GSB 的全新 0-1 题库。只记录在 Pair-wise 界面实际创建过的题面，与旧版历史题库隔离，供后续自动出题去重。

<!-- gsb-task-start {"pair_id": "d7cf4dabdb99", "repo_name": "pipe-cut-budget", "task_type": "0-1 代码生成", "project_category": "pipe-cut-budget", "language_framework": "Docker, Go 1.23, net/http, Go testing, Docker Compose", "summary": "暴雨污染会从供水入口流向取水口，须计算最小关管成本。 … 验收核对平行边、自环、多源多汇的精确代价，并用两万节点、十万边，在 10 秒请求超时内核对精确值；非法图不得进入求解且返回稳定错误结构。"} -->
## pipe-cut-budget

- Pair ID：d7cf4dabdb99
- 仓库：pipe-cut-budget
- 难度：困难
- 语言/框架：Docker, Go 1.23, net/http, Go testing, Docker Compose

### User Prompt

<!-- prompt-start -->
暴雨污染会从供水入口流向取水口，须计算最小关管成本。代码从空仓库起步，Go 实现 HTTP API。README 含请求例，.gitignore 排除构建物。仓库由 Docker Compose 启动，API_PORT 控制宿主端口，并含名为 verify 的一次性验收服务。输入仅用普通 JSON 基础类型：n 表示编号 0 至 n-1 的节点，edges 含 from、to、cost，sources 与 sinks 为节点数组。图为有向，平行边各自计费，自环不影响结果；端点组非空且互斥，2≤n≤20000、边数≤100000、cost 为 1 至 10^9 的整数，越界或非法引用返回 422。核心是自实现最大流/最小割算法：以总边费加一连接超级端点，用 64 位整数返回 minimum_shutdown_cost，其值须等于阻断 source 到 sink 路径的最小边费，原本无通路时为 0，禁止外部求解器和割集枚举。verify 运行 Go 测试并调用真实接口，禁止假接口和固定结果。验收核对平行边、自环、多源多汇的精确代价，并用两万节点、十万边，在 10 秒请求超时内核对精确值；非法图不得进入求解且返回稳定错误结构。
<!-- prompt-end -->
<!-- gsb-task-end -->

<!-- gsb-task-start {"pair_id": "d3c172bdd98e", "repo_name": "cue-distance-gate", "task_type": "0-1 代码生成", "project_category": "全栈", "language_framework": "Docker, TypeScript, React, Fastify, Vitest, Docker Compose", "summary": "舞台监督拿到计划 cue 序列和现场触发序列时，需要在开演间隙判断两者偏差是否仍在容许范围，五万项输入不能靠完整动态规划矩阵拖垮页面。 … 验收将以小规模参考结果和五万项稀疏改动样本核对数值、越界结论与页面联调，最终让监督看到明确的偏差距离或超限信号。"} -->
## cue-distance-gate

- Pair ID：d3c172bdd98e
- 仓库：cue-distance-gate
- 项目类别：全栈
- 难度：困难
- 语言/框架：Docker, TypeScript, React, Fastify, Vitest, Docker Compose

### User Prompt

<!-- prompt-start -->
舞台监督拿到计划 cue 序列和现场触发序列时，需要在开演间隙判断两者偏差是否仍在容许范围，五万项输入不能靠完整动态规划矩阵拖垮页面。代码从空仓库起步，React 提供左右两个 JSON 数组编辑区和阈值输入，Fastify API 只接收 32 位有符号整数数组与整数 K；输入是标准 JSON 基础类型，不解释文本格式。TypeScript 实现有界插入删除距离，Vitest 校验算法边界；Docker Compose 提供名为 verify 的一次性验收服务，宿主 Web 端口由 WEB_PORT 覆盖。工程核心是自行完成内存与时间受限的精确比较：元素仅按整数相等，插入或删除各计 1，替换必须计 2；数组各不超过 50000 项，K 为 0 至 500。真实距离不大于 K 时返回精确整数，超过 K 时只返回 exceeded，前端据此显示可复核结论；不得调用差异库或构造 O(nm) 表。空数组、重复 cue、首尾连续缺失及长度差已超过 K 都必须遵守同一规则，非法 JSON、非整数或超限输入返回稳定错误。验收将以小规模参考结果和五万项稀疏改动样本核对数值、越界结论与页面联调，最终让监督看到明确的偏差距离或超限信号。
<!-- prompt-end -->
<!-- gsb-task-end -->

<!-- gsb-task-start {"pair_id": "e6f0dde130c5", "repo_name": "cue-distance-gate-feature-0013", "task_type": "Feature 迭代", "project_category": "全栈", "language_framework": "Docker, TypeScript, React, Fastify, Vitest, Docker Compose", "summary": "在 Web 入口加入独立的演出场次控制台，舞台监督创建场次后从待演进入运行，可在运行与暂停间切换，运行时逐条登记整数 cue，结束后查看只读时间线。 … Vitest 以同版本并发命令证明仅一条提交且重复请求无副作用，Compose verify 经 Web 代理走完生命周期及刷新载入，核对封存时间线及 WEB_PORT 覆盖。"} -->
## cue-distance-gate · Feature 迭代

- Pair ID：e6f0dde130c5
- 仓库：cue-distance-gate-feature-0013
- 项目类别：全栈
- 任务类型：Feature 迭代
- 难度：困难
- 语言/框架：Docker, TypeScript, React, Fastify, Vitest, Docker Compose

### User Prompt

<!-- prompt-start -->
在 Web 入口加入独立的演出场次控制台，舞台监督创建场次后从待演进入运行，可在运行与暂停间切换，运行时逐条登记整数 cue，结束后查看只读时间线。场次保存 ID、名称、状态、版本、请求标识及顺序 cue；变更携带 expectedVersion 和 requestId，经单场次串行裁决后一次提交，拒绝时数据和版本不变。POST /api/performances/commands 处理创建、状态推进和 cue 登记，GET /api/performances/:id 载入快照；React 只设提交指令和按 ID 载入两个入口，并保留偏差校验入口。查询缺失对象返回 SESSION_NOT_FOUND，非法迁移、非运行态写入、重复标识或过期版本返回 COMMAND_REJECTED 及原因，页面保留当前场次和错误码。Vitest 以同版本并发命令证明仅一条提交且重复请求无副作用，Compose verify 经 Web 代理走完生命周期及刷新载入，核对封存时间线及 WEB_PORT 覆盖。
<!-- prompt-end -->
<!-- gsb-task-end -->

<!-- gsb-task-start {"pair_id": "3cb10280141b", "repo_name": "pipe-cut-budget-bugfix-0019", "task_type": "Bug 修复", "project_category": "纯后端", "language_framework": "Docker, Go 1.23, net/http, Go testing, Docker Compose", "summary": "供水管网服务通过 HTTP 计算最小关管成本，本轮聚焦优雅停机与在途请求排空，覆盖服务入口、HTTP 接入与容器编排，确保终止信号后妥善处理已有计算再退出。供水关管计算服务在请求尚未结束时收到终止信号，当前会随监听关闭提前成功退出并截断响应，正确状态应等待请求完成，或在排空超时后以失败状态退出。"} -->
## pipe-cut-budget · Bug 修复

- Pair ID：3cb10280141b
- 仓库：pipe-cut-budget-bugfix-0019
- 项目类别：纯后端
- 任务类型：Bug 修复
- 难度：困难
- 语言/框架：Docker, Go 1.23, net/http, Go testing, Docker Compose

### User Prompt

<!-- prompt-start -->
供水管网服务通过 HTTP 计算最小关管成本，本轮聚焦优雅停机与在途请求排空，覆盖服务入口、HTTP 接入与容器编排，确保终止信号后妥善处理已有计算再退出。供水关管计算服务在请求尚未结束时收到终止信号，当前会随监听关闭提前成功退出并截断响应，正确状态应等待请求完成，或在排空超时后以失败状态退出。
<!-- prompt-end -->
<!-- gsb-task-end -->

<!-- gsb-task-start {"pair_id": "de32a5c3787c", "repo_name": "pipe-cut-budget-bugfix-0019-feature-0028", "task_type": "Feature 迭代", "project_category": "纯后端", "language_framework": "Docker, Go 1.23, net/http, Go testing, Docker Compose", "summary": "为暴雨后的供水管网加入污染采样调查，创建时保存节点、有向边和污染入口快照，以迭代遍历确定入口可达且排除入口自身的目标，环、自环和平行边不能造成重复。 … 修改调查领域层、HTTP 层和 verify 程序，以真实请求验收目标集合、生命周期、结论及并发记录数，并确认原成本接口、健康检查与优雅停机测试通过。"} -->
## pipe-cut-budget · Bug 修复 · Feature 迭代

- Pair ID：de32a5c3787c
- 仓库：pipe-cut-budget-bugfix-0019-feature-0028
- 项目类别：纯后端
- 任务类型：Feature 迭代
- 难度：困难
- 语言/框架：Docker, Go 1.23, net/http, Go testing, Docker Compose

### User Prompt

<!-- prompt-start -->
为暴雨后的供水管网加入污染采样调查，创建时保存节点、有向边和污染入口快照，以迭代遍历确定入口可达且排除入口自身的目标，环、自环和平行边不能造成重复。状态按无样本、部分采样、全部采齐依次为 PENDING、IN_PROGRESS、COMPLETED，终态不回退；无目标时创建即完成，有 contaminated 样本则结论同名，否则为 clean。POST /investigations 创建，POST /investigations/{id}/samples 采样。样本含全局唯一 sample_id、节点和 clean/contaminated，两个成功响应均返回完整调查视图。仓储在同一临界区登记样本并推进状态，并发补齐目标时记录不重不漏且只结案一次；非法输入为 422 且不留痕，未知调查为 404，重复编号或节点、非目标节点和终态写入为 409，错误保持现有结构。修改调查领域层、HTTP 层和 verify 程序，以真实请求验收目标集合、生命周期、结论及并发记录数，并确认原成本接口、健康检查与优雅停机测试通过。
<!-- prompt-end -->
<!-- gsb-task-end -->

<!-- gsb-task-start {"pair_id": "a915aa486d63", "repo_name": "track-possession-arbiter", "task_type": "0-1 代码生成", "project_category": "纯后端", "language_framework": "Docker, Go 1.23, net/http, PostgreSQL, Docker Compose, Go testing", "summary": "夜间检修班组会同时申请多段轨道的封锁窗口，系统须避免只占住部分区段而形成无法执行的残缺封锁。 … Go testing 须通过真实数据库覆盖区间边界与并发竞争；验收还应证明胜者重启后仍可查询并阻止重叠申请，而败者涉及的其他区段可立即被新申请占用。"} -->
## track-possession-arbiter

- Pair ID：a915aa486d63
- 仓库：track-possession-arbiter
- 项目类别：纯后端
- 任务类型：0-1 代码生成
- 难度：困难
- 语言/框架：Docker, Go 1.23, net/http, PostgreSQL, Docker Compose, Go testing

### User Prompt

<!-- prompt-start -->
夜间检修班组会同时申请多段轨道的封锁窗口，系统须避免只占住部分区段而形成无法执行的残缺封锁。请从空仓库开始，以 Go 1.23 的 net/http 构建纯后端 API。提交体包含申请方、非空且无重复的区段字符串数组及整数起止分钟；区间按左闭右开解释，结束须大于开始，共享区段且时间交集非空才冲突，端点相接允许。PostgreSQL 持久化已接受申请；一次提交须在单个事务中全有或全无，任一区段冲突即返回 409 和 conflict_id，不得留下部分记录；若同时命中多个既有申请，conflict_id 必须取其标识按字符串字典序最小者。两个并发的冲突申请必须恰好接受一个，并在多 API 进程、跨连接及重启后维持同一裁决不变量。提供提交和按标识查询接口，非法输入返回稳定 JSON 错误。Dockerfile 以非 root 进程运行。用 Docker Compose 编排 API、数据库及一次性 verify 服务，API_PORT 可覆盖宿主端口。Go testing 须通过真实数据库覆盖区间边界与并发竞争；验收还应证明胜者重启后仍可查询并阻止重叠申请，而败者涉及的其他区段可立即被新申请占用。
<!-- prompt-end -->
<!-- gsb-task-end -->

<!-- gsb-task-start {"pair_id": "e515f41c2d5e", "repo_name": "pipe-cut-budget-feature-0036", "task_type": "Feature 迭代", "project_category": "纯后端", "language_framework": "Docker, Go 1.23, net/http, Go testing, Docker Compose", "summary": "新增独立的污染传播事件，创建体包含节点数、管道端点与通行分钟、污染源节点与释放分钟、重点取水口和截止分钟，事件保存当前分钟与状态。 … cmd/verify 以真实 HTTP 验证分段和并发推进、精确到达分钟与两类终态，并确认 /mincut、/healthz 和 API_PORT 可用。"} -->
## pipe-cut-budget · Feature 迭代

- Pair ID：e515f41c2d5e
- 仓库：pipe-cut-budget-feature-0036
- 项目类别：纯后端
- 任务类型：Feature 迭代
- 难度：困难
- 语言/框架：Docker, Go 1.23, net/http, Go testing, Docker Compose

### User Prompt

<!-- prompt-start -->
新增独立的污染传播事件，创建体包含节点数、管道端点与通行分钟、污染源节点与释放分钟、重点取水口和截止分钟，事件保存当前分钟与状态。现有服务接入 POST /incidents 和 POST /incidents/{id}/advance，创建返回标识与快照，推进至请求分钟后返回本次首次到达节点、累计最早到达分钟和状态。internal/incident 按释放分钟加有向通行时间求多源最早到达，平行管取更早路径，自环和反向边不得产生传播；截止前触达取水口即 breached，到截止未触达则 contained。初态为 scheduled，首个污染源释放后为 propagating；事件存储将时钟校验、到达增量和状态跃迁原子提交，同分钟重试返回空增量与相同快照，并发请求只能按提交顺序单调生效。非法拓扑返回稳定 422，未知事件为 404，倒退、越过截止或终态推进为稳定 409，所有失败不得改变快照。cmd/verify 以真实 HTTP 验证分段和并发推进、精确到达分钟与两类终态，并确认 /mincut、/healthz 和 API_PORT 可用。
<!-- prompt-end -->
<!-- gsb-task-end -->

<!-- gsb-task-start {"pair_id": "3bff540e86ed", "repo_name": "power-conflict-min-cover", "task_type": "0-1 代码生成", "project_category": "纯前端", "language_framework": "Docker, TypeScript, React, Vite, Vitest, Playwright, Docker Compose", "summary": "展馆临时供电联调中，一条冲突表示某供电支路与某临时设备不可同时带电；安全员要停用尽量少的两侧端点，使每条冲突至少有一端被停用。 … 验收会逐边核对覆盖性及最小数量，并要求上限样本在八秒内完成，最终安全员看到可直接执行的最小停用清单。"} -->
## power-conflict-min-cover

- Pair ID：3bff540e86ed
- 仓库：power-conflict-min-cover
- 项目类别：纯前端
- 任务类型：0-1 代码生成
- 难度：困难
- 语言/框架：Docker, TypeScript, React, Vite, Vitest, Playwright, Docker Compose

### User Prompt

<!-- prompt-start -->
展馆临时供电联调中，一条冲突表示某供电支路与某临时设备不可同时带电；安全员要停用尽量少的两侧端点，使每条冲突至少有一端被停用。代码从空仓库起步，使用 TypeScript、React 制作双栏关系编辑器；README 定义普通 JSON 字符串和数组契约，.gitignore 排除构建产物。用户可在两栏增删端点、连边或粘贴数据，规模上限为两侧各 50000 个端点、200000 条边；左右同名仍是不同端点，重复边合并，孤立点保留。页面必须计算精确最小停用数并给出覆盖集合，不能用按度数贪心或反复试删；若最优集合不唯一，任一覆盖全部边且元素数等于最小值的集合都合格，不与预置集合比较。未知端点、越界或结构错误应拒绝本次数据并清除旧高亮，空边集则显示零和空集合。Vitest 以小图穷举校验最优性，并覆盖增广深链与上限稀疏图；Playwright 验证编辑、导入和高亮闭环。Docker Compose 只运行浏览器应用，不设业务后端或外部调用；宿主端口由 WEB_PORT 覆盖，并带名为 verify 的一次性验收服务。验收会逐边核对覆盖性及最小数量，并要求上限样本在八秒内完成，最终安全员看到可直接执行的最小停用清单。
<!-- prompt-end -->
<!-- gsb-task-end -->

<!-- gsb-task-start {"pair_id": "b9b967be2758", "repo_name": "coating-scan-order-index", "task_type": "0-1 代码生成", "project_category": "纯前端", "language_framework": "TypeScript, React, Vite, Vitest, Docker, Docker Compose", "summary": "涂层线扫仪一次产生二十万条强度读数，质检员要同时复核十万个局部窗口的第 k 小值；逐窗口复制排序会让浏览器在换卷前无法给出结论。 … 载入成功后，页面按原查询顺序展示精确读数和查询下标，首尾相邻窗口不得偏移，任一非法查询也不得留下部分答案。"} -->
## coating-scan-order-index

- Pair ID：b9b967be2758
- 仓库：coating-scan-order-index
- 项目类别：纯前端
- 任务类型：0-1 代码生成
- 难度：困难
- 语言/框架：TypeScript, React, Vite, Vitest, Docker, Docker Compose

### User Prompt

<!-- prompt-start -->
涂层线扫仪一次产生二十万条强度读数，质检员要同时复核十万个局部窗口的第 k 小值；逐窗口复制排序会让浏览器在换卷前无法给出结论。代码从空仓库起步，本地文件由 TypeScript、React 和 Vite 页面读取，不调用业务后端或在线服务；JSON 只含 readings 整数数组与 queries 数组，每个查询含 start、end、k，并按半开区间 [start,end) 解释。readings 长度为 1 至 200000，值固定在 0 至 65535；查询不超过 100000，须满足 0≤start<end≤长度且 1≤k≤end-start。任何结构或边界错误都拒绝整个文件，按数组下标反馈并清除旧结果。Vitest 用直接排序作小样本预言机，覆盖重复值、全相等、单元素、首尾窗口及 k 的两端；确定性满规模样本须在四秒内完成并核对结果摘要。Docker Compose 发布静态页面，WEB_PORT 可覆盖宿主端口，并提供名为 verify 的一次性验收服务。载入成功后，页面按原查询顺序展示精确读数和查询下标，首尾相邻窗口不得偏移，任一非法查询也不得留下部分答案。
<!-- prompt-end -->
<!-- gsb-task-end -->

