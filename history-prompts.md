# 历史 0-1 题库

这里记录已经实际创建过的首轮题面，后续出题会同时读取本文件和 SQLite，避免重复业务对象、数据模型、核心算法与交互结构。

<!-- task-entry-start {"run_id":"bc2f39a1fa49","repo_name":"api-change-radar","task_type":"0-1 代码生成"} -->
## 历史记录 · api-change-radar

- 创建时间：2026-09-08 17:39:02 +0800
- 项目类别：全栈
- 任务难度：未记录
- 语言/框架：TypeScript、SQLAlchemy、FastAPI、SQLite、React、Python、Vite

### User Prompt

<!-- prompt-start -->
从零实现一个名为 API Change Radar 的本地全栈应用，用来比较两份 OpenAPI 文档并判断接口变更影响。请直接完成可运行产品，不要只写方案。 技术栈：后端使用 Python 3、FastAPI、SQLAlchemy 和 SQLite；前端使用 React、TypeScript 和 Vite。仓库根目录清晰划分 backend、frontend，并提供完整的 README 运行说明。 功能要求： 1. 支持粘贴或上传两份 OpenAPI 3.x JSON/YAML，分别作为基线版本和候选版本；解析失败时显示具体位置和原因。 2. 自动比较 paths、HTTP 方法、参数、requestBody、responses，以及 schema 的 required、type、enum 变化，并区分破坏性变更、非破坏性变更和仅文档变化。 3. 结果页按严重级别动态统计，支持搜索和筛选；详情中展示变更前后内容、字段位置和判断依据。 4. 每次比较都保存为不可变快照到 SQLite，可查看历史记录；后续规则配置变化不能改变历史结果。 5. 规则中心的数据由后端统一提供，支持启用和停用，并把配置保存到 SQLite；系统至少保留一条启用规则。新比较只运行当前启用规则。 6. 支持对单条结果添加风险豁免，记录原因、负责人和时间，并可撤销；规则切换不能影响已有豁免。 7. 首次启动时写入两组示例文档和一组默认规则，页面提供一键载入示例，能完整走通比较流程。 8. 页面风格简约现代，适配桌面和移动端，补齐空状态、加载状态、成功反馈和错误提示。 质量要求： - 后端补充比较逻辑、快照持久化、至少保留一条启用规则、风险豁免不受规则切换影响等测试。 - pytest -q 和 npm run build 均能通过。 - 不依赖付费外部服务，不保留 TODO、占位实现或只用于演示的假接口。 - 完成后自行检查关键流程，并提交所有代码。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "d511c9cb1ddd", "repo_name": "reliable-event-relay", "task_type": "0-1 代码生成"} -->
## 0001 · reliable-event-relay

- 创建时间：2026-09-09 19:16:29 +0800
- 项目类别：纯后端
- 任务难度：地狱
- 语言/框架：Docker, Python 3.12, FastAPI, SQLAlchemy, PostgreSQL, Docker Compose, pytest

### User Prompt

<!-- prompt-start -->
项目编号 0001。从空仓库实现一个纯后端的可靠事件投递中继服务，使用 Python 3.12、FastAPI、SQLAlchemy 和 PostgreSQL，并通过 Docker Compose 启动 API、投递工作进程和数据库，不得创建任何前端或依赖外部在线服务。调用方可以注册仅允许指向 Compose 内部网络的 HTTP 目标端点，并向某个目标提交带有业务事件键、事件类型和 JSON 载荷的投递任务；同一目标与业务事件键的重复提交必须通过数据库唯一约束实现幂等，并返回首次创建的任务而不能产生重复投递。API 与工作进程共享持久化状态，工作进程需要支持多个副本并发领取任务，必须利用 PostgreSQL 行锁或等价的原子租约机制保证同一轮尝试不会被两个进程同时执行，同时确保同一目标下的任务按创建顺序投递，某条任务尚未成功或进入死信前不得越过它投递后续任务，不同目标之间则允许并行。每次请求必须发送事件标识、尝试编号、时间戳和使用目标密钥计算的 HMAC 签名；目标返回 2xx 时标记成功，返回 408、429、5xx、连接失败或超时时按指数退避重试并加入可配置抖动，其他 4xx 直接进入死信，达到最大尝试次数也进入死信，并完整保存每次尝试的起止时间、状态码、错误分类和下一次执行时间，但不得保存密钥明文。租约过期的任务应能被其他工作进程安全接管，进程在发出请求后崩溃所造成的重复投递必须在 README 中明确说明为至少一次语义，并通过稳定的事件标识帮助接收方去重。提供查询任务及尝试历史、暂停和恢复目标、轮换目标密钥、人工重放死信任务以及健康检查接口；暂停目标时不得领取新任务，恢复后应继续原有顺序，重放必须创建新的投递周期并保留旧尝试记录，正在投递或已经成功的任务不可重放。所有写接口都要校验输入并返回结构一致、可定位问题的 JSON 错误，数据库不可用、目标地址越界、非法状态转换和并发冲突必须有明确失败路径；目标地址需阻止访问 Compose 网络之外的主机以及云元数据地址，并防止通过重定向绕过限制。仓库中需包含一个只用于集成测试且行为可配置的本地接收服务，以验证成功、签名、超时、限流、永久失败和重试场景，它不能以固定结果冒充核心功能。为关键状态转换、幂等竞争、多工作进程抢占、严格顺序、租约恢复、退避边界、暂停恢复、密钥轮换和死信重放编写单元及集成测试；时间与随机抖动应可注入，使测试稳定且无需真实等待。提交完整的 Dockerfile、compose.yaml、数据库迁移、README、测试和 .gitignore，README 要说明架构、状态机、至少一次投递语义、配置、接口示例以及构建运行和测试方法，所有容器应以非 root 用户运行并提供健康检查，项目中不得留下 TODO、假数据接口、固定结果或未实现分支。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "15cbc6b37130", "repo_name": "offline-evacuation-planner", "task_type": "0-1 代码生成"} -->
## 0002 · offline-evacuation-planner

- 创建时间：2026-09-09 19:53:14 +0800
- 项目类别：纯前端
- 任务难度：困难
- 语言/框架：Docker, TypeScript, React, Vite, Zustand, IndexedDB, Web Worker, Vitest, Testing Library, Playwright

### User Prompt

<!-- prompt-start -->
从空仓库实现一个离线优先的建筑疏散编排器，使用 React、TypeScript、Vite、Zustand 与 IndexedDB，在浏览器中导入建筑平面 SVG，识别可通行区域、出口和障碍，并允许在画布上设置起点、封锁区、单向通道及出口容量；禁止创建业务后端或调用任何外部在线服务。应用需在 Web Worker 中计算多起点疏散方案，路径必须避开封锁并遵守单向约束，出口容量冲突按稳定规则分配，同时说明无法疏散的原因。计算期间允许继续编辑，旧任务结果不得覆盖新版本，用户可取消任务；Worker 崩溃、超时、SVG 非法或无可行路径时须显示可恢复错误。编辑采用可撤销、重做的命令历史，批量操作视为一次事务；方案、输入版本和计算参数持久化到 IndexedDB，刷新后恢复，跨标签页使用 BroadcastChannel 协调写入，通过修订号和明确的冲突处理防止静默覆盖。支持导出包含原始 SVG、约束、结果及校验摘要的 JSON 包并离线重新导入；导入必须校验格式版本、结构和摘要，失败不得污染已有数据。界面应支持缩放平移、键盘操作、可见焦点和移动端查看，并呈现空闲、计算中、结果过期、成功、部分成功及失败状态。使用 Vitest、Testing Library 和 Playwright 覆盖路径约束、容量竞争、任务取消竞态、事务撤销、持久化恢复、跨标签冲突及损坏导入。提交 Dockerfile、compose.yaml、README、测试和 .gitignore；README 说明架构、算法取舍、数据版本、并发策略及运行测试方法，容器须以非 root 用户运行并提供健康检查，不得留下 TODO、假数据接口、固定结果或未实现分支。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "8f7b2f40f0d6", "repo_name": "tamper-evident-calibration-ledger", "task_type": "0-1 代码生成"} -->
## 0004-1 · tamper-evident-calibration-ledger

- 创建时间：2026-09-10 00:13:35 +0800
- 项目类别：纯后端
- 任务难度：待评估
- 语言/框架：Docker, Python 3.12, FastAPI, SQLAlchemy, PostgreSQL, Alembic, RFC 6962 Merkle Tree, HMAC, pytest

### User Prompt

<!-- prompt-start -->
新增“仪器审计包”模块，使审计员能为指定 instrument_id 创建一次可追踪的离线导出任务。POST /v1/audit-packages 接收 instrument_id、幂等键和可选截止检查点，创建 pending 任务并固定已封存边界；未指定检查点时选取最新检查点，没有可用封存或检查点不覆盖该仪器事件时返回结构化错误，重试相同幂等键不得重复建包，不同参数复用键返回冲突。独立 worker 以事务锁抢占任务，按数据库序号收集边界内该仪器的全部事件，为每个事件生成现有格式的收据与证明，并生成含任务、边界、事件数量及文件 SHA-256 的规范化清单，最终写出确定性 ZIP；包内不得出现原始报告或 HMAC 密钥。任务须具有 pending、building、ready、failed 状态及失败原因、尝试次数和产物摘要，崩溃后超时的 building 可被重新领取，失败任务可经 POST /v1/audit-packages/{id}/retry 恢复且不得改变固定边界。提供状态查询和仅在 ready 时可用的下载接口，未完成下载返回明确冲突，未知任务返回既有错误信封。通过 Alembic 增加表、约束和索引，保持事件与检查点只追加不变量及现有 API 兼容；扩展 Compose 启动非 root 导出 worker并加入健康检查。端到端测试须覆盖创建、幂等冲突、封存边界隔离、确定性归档、包内逐条离线验证、双 worker 抢占、崩溃回收、失败重试、下载状态与数据库故障映射，并断言同一输入重建所得 ZIP 字节及摘要一致。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "55e53ae99b73", "repo_name": "prop-scene-console", "task_type": "0-1 代码生成"} -->
## 0006 · prop-scene-console

- 创建时间：2026-09-10 00:10:24 +0800
- 项目类别：全栈
- 任务难度：待评估
- 语言/框架：Docker, Go, Gin, GORM, PostgreSQL, Svelte, TypeScript, Vite, Server-Sent Events

### User Prompt

<!-- prompt-start -->
密室营业中途，道具门偶尔只回“已收到”却没有真正到位，店员连续点击开门，还可能让后到的复位指令被旧动作反超。请从空仓库实现本地道具场景控制台：后端采用 Go、Gin、GORM 与 PostgreSQL，前端采用 Svelte、TypeScript、Vite，以真实 API 和 Server-Sent Events 联调；Docker Compose 同时运行可配置的设备模拟器，README、.gitignore、Go test、Vitest 和 Playwright 及早验证协议，禁止 TODO、固定响应、假接口和未实现逻辑，错误须区分拒绝、超时与设备故障。技术员导入描述设备能力、互斥组和安全前置条件的 YAML 场景，校验后发布不可变版本；值班员通过平面按钮触发开门、落锁、亮灯或复位，后端为每台设备串行发出带序号的命令，只有收到匹配确认与最终状态才完成，并实时呈现执行轨迹。多设备动作按顺序推进，任一步失败便反向补偿已完成步骤，补偿失败则锁住该场景等待人工核验。模拟器可制造迟到确认、断连和错误状态；服务重启后应从持久化步骤继续查询或补偿，旧确认不得推进新命令。最终可观察正常场景完整到位、故障场景明确回滚，以及不确定设备被隔离后其他无关场景仍可操作。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "27110bf5145a", "repo_name": "cold-chain-trace-ingestor", "task_type": "0-1 代码生成"} -->
## 0007 · cold-chain-trace-ingestor

- 创建时间：2026-09-10 00:49:21 +0800
- 项目类别：纯后端
- 任务难度：待评估
- 语言/框架：Docker, Go 1.24, Fiber, PostgreSQL, Docker Compose

### User Prompt

<!-- prompt-start -->
一批冷链记录仪在运输结束后集中回收，其中既有重复导出的文件，也有断电造成的半帧数据和跨日回拨的设备时钟，质量人员必须先得到可信温度轨迹才能判断是否放行。请从空仓库实现纯后端服务，使用 Go、Fiber 与 PostgreSQL，不创建前端或调用外部在线服务；Docker Compose 运行 API、解析 worker 和数据库，并在开发过程中配套 README、.gitignore、健康检查及 Go 测试，所有输入错误返回结构化反馈，禁止 TODO、固定响应、假接口或未实现分支。API 接收二进制导出文件、设备编号、运输批次和期望时区，上传时流式计算摘要并按摘要幂等去重，不得把整文件读入内存。worker 按文件偏移保存解析检查点，校验帧头、长度、CRC、序号和时间，进程中断后从最后完整帧继续；损坏帧应记录字节位置并隔离整份文件，人工确认后可从指定合法边界重试。成功归并时按稳定规则消除重叠采样，计算超温区间及最长连续超温，只有完整文件才能原子发布批次结论。验收时可观察重复上传返回同一资源，截断文件不产生部分结论，重启 worker 后继续推进，而含时钟回拨的数据给出可定位的异常区间。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "2e91aed240b2", "repo_name": "redaction-rule-lab", "task_type": "0-1 代码生成"} -->
## 0008 · redaction-rule-lab

- 创建时间：2026-09-10 02:01:17 +0800
- 项目类别：纯前端
- 任务难度：待评估
- 语言/框架：Docker, TypeScript, Vue 3, Vite, Vitest, Playwright

### User Prompt

<!-- prompt-start -->
合同附件外发前，姓名、邮箱和证件号可能被多条规则同时命中，简单替换会重复遮蔽、错位，甚至把未处理的敏感片段带出浏览器。请从空仓库实现纯前端实验台，采用 Vue 3、TypeScript 与 Vite，只接受粘贴文本、本地 TXT 和本地 JSON 规则，不创建业务后端或连接在线服务。Docker Compose 应能启动静态应用；在规则解析、区间裁决和导出链路旁配置 Vitest 与 Playwright，README 解释规则格式和安全边界，.gitignore 排除本地产物，所有错误给出规则编号或字符位置，禁止 TODO、假接口、固定响应和占位实现。每条规则包含名称、正则、优先级、替换模板及是否为导出前必检项；引擎先收集原文上的全部命中，再按优先级、区间长度和规则顺序稳定裁决重叠，禁止基于已替换文本继续匹配。界面并排展示原文与结果，点击任一遮蔽块可查看来源规则、原始范围和裁决原因，并支持逐条启停后重新计算。非法正则、零长度匹配、声明编码无法读取或必检模式仍残留时不得污染上一份有效结果，也不得导出；通过检查后生成脱敏文本和审阅清单，两者记录的区间、规则及替换内容必须逐项对应。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "361d6acd147e", "repo_name": "tactile-sign-preflight", "task_type": "0-1 代码生成"} -->
## 0009 · tactile-sign-preflight

- 创建时间：2026-09-10 02:04:29 +0800
- 项目类别：全栈
- 任务难度：待评估
- 语言/框架：Docker, Python 3.12, FastAPI, React, TypeScript, Vite

### User Prompt

<!-- prompt-start -->
无障碍标牌进入压印机前，底图上的螺孔、边框和禁印区常与盲文触点争抢毫米级空间，肉眼预览正常也可能造成触点粘连或越界。请从空仓库实现一套全栈制版预检台，前端采用 React、TypeScript 与 Vite，后端采用 Python 3.12、FastAPI；两端通过真实 API 联调，Docker Compose 启动应用与数据库。制版员上传本地 SVG 底图，填写板材尺寸、盲文文本、行距、点径及安全间距；服务端安全解析 SVG，统一单位与坐标变换，将文本编码为触点几何，再确定性检查触点之间、触点与边框、螺孔及禁印区的距离。README 在坐标约定旁说明支持范围，pytest、Vitest 与 Playwright 覆盖编码、几何边界和联调，.gitignore 排除产物；错误必须带元素编号、坐标和规则阈值，禁止 TODO、假接口、固定响应或占位实现。界面叠加显示底图、触点和冲突连线，点击问题可定位双方；非法 SVG、不支持的变换或任一冲突都不得替换上一份有效预览，也不得生成生产文件。全部通过后可导出坐标已归一化的生产 SVG 与对应问题清单；相同输入应得到相同坐标和内容，验收最终能看到合格版面可下载，而越界触点被明确标红并阻断压印。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "94c1a22164e1", "repo_name": "transmitter-config-switch", "task_type": "0-1 代码生成", "project_category": "纯后端", "language_framework": "Docker, Java 21, Spring Boot 3, Spring Data JPA, PostgreSQL, Flyway, Docker Compose, JUnit 5", "summary": "广播站交班时，两名工程师可能同时发布发射机参数；若后写覆盖先写，频率和功率会组合成未经审核的配置。 … 参数或目标修订非法、期望值过期时均不得改变运行参数，错误须定位"} -->
## 0014 · transmitter-config-switch

- 创建时间：2026-09-10 11:02:57 +0800
- 项目类别：纯后端
- 任务难度：待评估
- 语言/框架：Docker, Java 21, Spring Boot 3, Spring Data JPA, PostgreSQL, Flyway, Docker Compose, JUnit 5

### User Prompt

<!-- prompt-start -->
广播站交班时，两名工程师可能同时发布发射机参数；若后写覆盖先写，频率和功率会组合成未经审核的配置。从空仓库实现纯后端配置切换服务，使用 Java 21、Spring Boot 3、Spring Data JPA、PostgreSQL 与 Flyway，并提供 Dockerfile、compose.yaml、README、.gitignore 和 JUnit 5 测试；Compose 包含名为 verify 的一次性验收服务，API 宿主端口可由 API_PORT 覆盖。客户端提交完整快照：频率为 87.5 至 108.0 MHz 且按 0.1 MHz 步进，功率为 0.1 至 50.0 kW，调制度为 0 至 100 的整数，区间均含端点。合法快照只生成该发射机从 1 开始递增的修订，不自动生效。激活请求必须携带 target_revision、expected_revision 和幂等键；目标修订必须已存在、属于同一发射机且尚未生效。首次尚无生效配置时 expected_revision 必须为 0，此后必须等于当前生效修订；服务在单次数据库事务中将指定目标修订切换为唯一生效版本。两个并发激活使用同一期望值时只能一个成功，另一个返回 409 及最新生效修订。相同幂等键和相同请求内容重试返回原结果，异义复用返回 409。参数或目标修订非法、期望值过期时均不得改变运行参数，错误须定位
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "0a714f0a2165", "repo_name": "subtitle-handoff-line", "task_type": "0-1 代码生成", "project_category": "纯前端", "language_framework": "TypeScript, Vue 3, Vite, Pinia, Vitest, Playwright, Docker, Docker Compose", "summary": "彩排前十分钟，字幕文件中一处毫秒级重叠就会让播控器同时显示两句台词，过短空隙也会妨碍换句辨认。 … Docker Compose 发布端口可由 WEB_PORT 覆盖，并提供 verify 一次性验收服务；Vitest 与 Playwright 覆盖"} -->
## 0015 · subtitle-handoff-line

- 创建时间：2026-09-10 11:06:35 +0800
- 项目类别：纯前端
- 任务难度：待评估
- 语言/框架：TypeScript, Vue 3, Vite, Pinia, Vitest, Playwright, Docker, Docker Compose

### User Prompt

<!-- prompt-start -->
彩排前十分钟，字幕文件中一处毫秒级重叠就会让播控器同时显示两句台词，过短空隙也会妨碍换句辨认。请从空仓库实现纯前端“字幕交接线”，供演出字幕播控员导入和修订字幕；使用 TypeScript、Vue 3、Vite 与 Pinia，处理全部留在浏览器，不建立业务后端或访问外部在线服务。输入为本地 UTF-8 WebVTT：首行必须是 WEBVTT，可带 BOM；字幕块仅允许可选十进制编号、HH:MM:SS.mmm --> HH:MM:SS.mmm 时间行及至少一行非空文本，小时固定两位，分秒限 00 至 59，结束须晚于开始，不支持注释、样式和定位参数。格式解释须采用现成 WebVTT 解析库，不得自行实现或改写解析算法；仅在库输出上校验上述子集。按文件顺序裁决相邻字幕：重叠 1 毫秒即为错误，间隔 0 至 79 毫秒为过密，80 毫秒及以上通过。界面提供导入、表格编辑、问题定位和规范化导出；非法导入不替换当前有效时间轴，非法编辑拒绝提交，并显示首个错误的行号或字幕编号。存在重叠或过密时禁止导出并聚焦首项问题。通过后导出 UTF-8 无 BOM、LF 换行、无编号、时间码固定三位毫秒、块间恰一空行且末尾一个换行的文件，文本内容与顺序不变。Docker Compose 发布端口可由 WEB_PORT 覆盖，并提供 verify 一次性验收服务；Vitest 与 Playwright 覆盖
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "4831244fd2d1", "repo_name": "port-laytime-adjudicator", "task_type": "0-1 代码生成", "project_category": "纯后端", "language_framework": "Python 3.12, FastAPI, SQLAlchemy, PostgreSQL, Alembic, pytest, Docker, Docker Compose", "summary": "散货船离港后，港方与船东常因多段停机时间重叠、越界或恰好相接而算出不同滞期费，结算员需要由同一套边界规则得到可复核结果。 … 验收可直接观察重复覆盖的停机只扣除一次、跨界停机仅扣交集，而非法请求返回具体路径且查询不到结算结果。"} -->
## 0017 · port-laytime-adjudicator

- 创建时间：2026-09-10 11:18:14 +0800
- 项目类别：纯后端
- 任务难度：待评估
- 语言/框架：Python 3.12, FastAPI, SQLAlchemy, PostgreSQL, Alembic, pytest, Docker, Docker Compose

### User Prompt

<!-- prompt-start -->
散货船离港后，港方与船东常因多段停机时间重叠、越界或恰好相接而算出不同滞期费，结算员需要由同一套边界规则得到可复核结果。从空仓库实现 Python 3.12、FastAPI、SQLAlchemy 与 PostgreSQL 的纯后端服务；Docker Compose 发布的宿主端口须由 API_PORT 覆盖，并提供名为 verify 的一次性验收服务。仓库应包含迁移、README、.gitignore 与 pytest，接口必须返回可定位的字段错误，禁止 TODO、假接口或固定响应。API 接收 RFC 3339 UTC 时间，精确到整秒，带小数秒或非 UTC 偏移一律拒绝；作业区间采用左闭右开，结束必须晚于开始。暂停区间同样左闭右开，先裁剪到作业区间，再将重叠或首尾相接部分合并，区间端点相等不增加时长。可计费秒数为作业秒数减去合并后的暂停秒数，按不足一小时向上取整后乘以非负整数分/小时费率，零秒费用为零。每次成功计算都持久化原始输入、合并区间、可计费秒数、计费小时及总分值，并可按结果标识查询；任何时间倒置、空暂停或负费率都不得写入记录。验收可直接观察重复覆盖的停机只扣除一次、跨界停机仅扣交集，而非法请求返回具体路径且查询不到结算结果。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "5689b5acd0bc", "repo_name": "hazard-label-contrast-preflight", "task_type": "0-1 代码生成", "project_category": "纯前端", "language_framework": "TypeScript, React, Vite, Vitest, Playwright, Docker, Docker Compose", "summary": "化工装置的警示牌送印前，深色文字与底色在屏幕上看似清楚，实际对比度却可能刚好越过可读性边界，操作员需要在浏览器内得到唯一的放行结论。 … 界面逐项显示色块、比值与原因，只要存在不合格项就禁止导出；全部通过时生成包含输入、比值及裁决的 JSON 放行单，使临界文字可明确放行，而不足项稳定标红。"} -->
## 0018 · hazard-label-contrast-preflight

- 创建时间：2026-09-10 11:20:15 +0800
- 项目类别：纯前端
- 任务难度：待评估
- 语言/框架：TypeScript, React, Vite, Vitest, Playwright, Docker, Docker Compose

### User Prompt

<!-- prompt-start -->
化工装置的警示牌送印前，深色文字与底色在屏幕上看似清楚，实际对比度却可能刚好越过可读性边界，操作员需要在浏览器内得到唯一的放行结论。代码从空仓库起步，使用 TypeScript、React 与 Vite 实现纯前端应用，不得增加业务后端或调用外部在线服务；Docker Compose 发布端口须由 WEB_PORT 覆盖，并提供名为 verify 的一次性验收服务，Vitest 与 Playwright 覆盖计算和导入主流程。应用导入 JSON 数组，每项必须包含唯一字符串 id、格式为 #RRGGBB 的不透明前景色和背景色、正数 pt 字号及布尔值 bold；任一项非法时整批拒绝，且不得覆盖上一份有效结果。颜色通道先除以 255，值不大于 0.04045 时除以 12.92，否则计算 ((c+0.055)/1.055)^2.4；相对亮度为 0.2126R+0.7152G+0.0722B，对比度为较亮亮度加 0.05 后除以较暗亮度加 0.05。18pt 以上普通文字或 14pt 以上粗体文字阈值为 3.00，其余为 4.50，比较使用未舍入值，展示采用四舍五入两位；恰好等于阈值算合格。界面逐项显示色块、比值与原因，只要存在不合格项就禁止导出；全部通过时生成包含输入、比值及裁决的 JSON 放行单，使临界文字可明确放行，而不足项稳定标红。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "1a820ff86add", "repo_name": "sample-handoff-ledger", "task_type": "0-1 代码生成", "project_category": "全栈", "language_framework": "Docker, Python 3.12, FastAPI, SQLAlchemy, PostgreSQL, Alembic, React, TypeScript, Vite, Vitest, Playwright", "summary": "样本在存储与处理期间需补录人工测温，值班人员从批次详情选择可流转容器，填写摄氏温度、测量人、测量时间和备注，提交后立即看到判定。 … 批次详情按时间倒序展示温度、判定、测量人与时间，旧批次读取和交接请求保持兼容；pytest 覆盖上下限等值、越界原子写入和非法时间，Vitest 验证展示与输入保留，Playwright 贯通正常测温"} -->
## 0003-5 · sample-handoff-ledger

- 创建时间：2026-09-10 14:51:38 +0800
- 项目类别：全栈
- 任务难度：待评估
- 语言/框架：Docker, Python 3.12, FastAPI, SQLAlchemy, PostgreSQL, Alembic, React, TypeScript, Vite, Vitest, Playwright

### User Prompt

<!-- prompt-start -->
样本在存储与处理期间需补录人工测温，值班人员从批次详情选择可流转容器，填写摄氏温度、测量人、测量时间和备注，提交后立即看到判定。把批次温区扩展为摄氏度上下限，新建温度观测实体保存原始数值与 normal、out_of_range 判定，迁移解析回填现有“2–8°C”数据，无法识别时明确失败。POST /api/containers/{container_id}/temperature-observations 按批次上下限判定边界值，写入观测和唯一时间线事件，越界还把批次置为 review，但不改变位置、离柜计时或交接。已封存容器返回 CONTAINER_ALREADY_REPLACED，测量时间晚于服务端当前时间或早于批次创建时间返回 INVALID_OBSERVED_AT，失败时表单保留输入且事务不留部分结果。批次详情按时间倒序展示温度、判定、测量人与时间，旧批次读取和交接请求保持兼容；pytest 覆盖上下限等值、越界原子写入和非法时间，Vitest 验证展示与输入保留，Playwright 贯通正常测温及越界进入复核且各自产生唯一事件。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "fbd6b586e950", "repo_name": "3000-kiln-firing-review-console", "task_type": "0-1 代码生成", "project_category": "纯前端", "language_framework": "Node.js, React, TypeScript, Vite, Vitest, Docker Compose", "summary": "根据仓库 README 自动整理的导入基线说明：# 窑炉烧成记录检查台 面向陶瓷工作室的离线优先烧成质检工作台。 … 如果端口已被占用，可覆盖宿主端口： ```bash WEB_PORT=18080 docker compose up --build -d curl http://localhost:18080/he"} -->
## 3000 · 3000-kiln-firing-review-console

- 创建时间：2026-09-10 16:12:00 +0800
- 项目类别：纯前端
- 任务难度：待评估
- 语言/框架：Node.js, React, TypeScript, Vite, Vitest, Docker Compose

### User Prompt

<!-- prompt-start -->
根据仓库 README 自动整理的导入基线说明：# 窑炉烧成记录检查台 面向陶瓷工作室的离线优先烧成质检工作台。管理配方和烧成批次，导入温度 CSV，在趋势图上对照目标温度与允许范围，并完成异常归类和复核备注。 ## 功能 - 配方：名称、目标温度、允许温差、计划时长；被批次引用的配方不可删除。 - 批次：名称、窑炉编号、配方、开始时间、备注；支持创建、查看和删除。 - CSV：严格要求 `时间,温度` 表头，校验列数、时间、数值、重复时间点和时间顺序；错误带行号，校验失败不覆盖原记录。 - 检查：SVG 折线图展示实际温度、目标线和允许范围；自动识别温度越界与过长采样间隔。 - 复核：异常可标记为待复核、设备问题、工艺问题、已接受，并保存备注。 - 工作台：首页指标，批次名/窑炉/异常状态筛选，明确空态与操作反馈，窄屏适配。 - 数据：浏览器 `localStorage` 持久化，JSON 全量导入导出、示例数据、一键清空；非法备份不会覆盖当前数据。 ## 本地开发 要求 Node.js 20+。 ```bash npm install npm run dev npm test npm run build ``` CSV 示例： ```csv 时间,温度 2026-09-08 08:30,26 2026-09-08 09:00,120 ``` 时间应为浏览器可解析的日期时间，并按升序排列。 ## Docker ```bash docker compose up --build -d curl http://localhost:8080/health docker compose down ``` 默认打开 <http://localhost:8080>。如果端口已被占用，可覆盖宿主端口： ```bash WEB_PORT=18080 docker compose up --build -d curl http://localhost:18080/health WEB_PORT=18080 docker compose down ``` 镜像采用 Node 构建、Nginx 提供静态站点，容器自带 `/health` 健康检查。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "5ffd4267b16e", "repo_name": "3001-crate-cleaning-trace-platform", "task_type": "0-1 代码生成", "project_category": "全栈", "language_framework": "Node.js, React, TypeScript, Vite, Vitest, Python, FastAPI, SQLAlchemy, pytest, Docker Compose", "summary": "根据仓库 README 自动整理的导入基线说明：# 周转箱清洁追溯平台 面向小型食品工厂的轻量级全栈系统。 … ## 本地开发 后端（Python 3.9+）： ```bash cd backend python3 -m venv .venv source .venv/bin/activate pip inst"} -->
## 3001 · 3001-crate-cleaning-trace-platform

- 创建时间：2026-09-10 16:12:01 +0800
- 项目类别：全栈
- 任务难度：待评估
- 语言/框架：Node.js, React, TypeScript, Vite, Vitest, Python, FastAPI, SQLAlchemy, pytest, Docker Compose

### User Prompt

<!-- prompt-start -->
根据仓库 README 自动整理的导入基线说明：# 周转箱清洁追溯平台 面向小型食品工厂的轻量级全栈系统。维护周转箱台账、登记流转事件，根据当前状态自动识别使用风险，并闭环处理问题。 ## 功能 - 周转箱：新增、编辑 API、停用，维护唯一编号、名称、位置、清洁状态与备注。 - 流转记录：入库、领用、归还、清洗、检查、隔离；事件编号唯一。 - 状态推导：事件登记后更新位置、清洁状态、隔离状态及最近检查时间。 - 风险识别：未清洗再次领用、已隔离仍领用、检查超过 30 天有效期仍使用。 - 问题闭环：待处理、已确认、误报、已关闭，支持处理说明。 - 首页指标及编号、位置、清洁状态、问题状态筛选；完整加载、成功和失败反馈；响应式窄屏布局。 - 首次容器启动自动装入 3 个周转箱及流转/风险示例数据。 ## 一键启动 ```bash docker compose up --build ``` 打开 http://localhost:3001 。API 文档位于 http://localhost:3001/api/docs（通过前端代理）；健康检查为 `/api/health`。SQLite 数据保存在 Docker 命名卷 `crate_data`。 停止服务：`docker compose down`。如需同时清空示例与运行数据：`docker compose down -v`。 ## 本地开发 后端（Python 3.9+）： ```bash cd backend python3 -m venv .venv source .venv/bin/activate pip install -r requirements-dev.txt python -m app.seed uvicorn app.main:app --reload ``` 前端（Node 20+；本地开发时将 `vite.config.ts` 中代理目标改为 `http://localhost:8000`）： ```bash cd frontend npm install npm run dev ``` ## 测试 ```bash cd backend && pytest cd frontend && npm test && npm run build ``` ## API 摘要 - `GET/POST /api/crates`；`PUT /api/crates/{id}`；`POST /api/crates/{id}/deactivate` - `GET/POST /api/events` - `GET /api/issues`；`PATCH /api/issues/{id}` - `GET /api/dashboard`；`GET /api/health` 参数校验失败返回 422，非法箱号返回 404，重复箱号/事件编号与停用箱登记返回 409。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "a2b589354bb2", "repo_name": "3002-plant-specimen-label-preflight", "task_type": "0-1 代码生成", "project_category": "纯前端", "language_framework": "Node.js, React, TypeScript, Vite, Vitest, Docker Compose", "summary": "根据仓库 README 自动整理的导入基线说明：# 植物标本标签预检台 面向小型标本馆与野外团队的纯前端预检工具。 … - 当前数据只存在于当前浏览器环境，建议定期导出 JSON 备份。"} -->
## 3002 · 3002-plant-specimen-label-preflight

- 创建时间：2026-09-10 16:34:18 +0800
- 项目类别：纯前端
- 任务难度：待评估
- 语言/框架：Node.js, React, TypeScript, Vite, Vitest, Docker Compose

### User Prompt

<!-- prompt-start -->
根据仓库 README 自动整理的导入基线说明：# 植物标本标签预检台 面向小型标本馆与野外团队的纯前端预检工具。记录和问题处理状态保存在浏览器 `localStorage`，无需后端。 ## 本地运行 ```bash npm install npm run dev npm test npm run build ``` ## 数据交换 CSV 必须使用以下顺序和名称的 UTF-8 表头： ```text 采集编号,物种名称,采集人,采集日期,地点,纬度,经度,生境备注 ``` 采集编号、采集人、采集日期、地点为必填项。日期采用 `YYYY-MM-DD`；纬度范围为 -90～90，经度范围为 -180～180。导入会一次性校验全部行；任一行失败时不会写入任何数据。JSON 导出包含完整记录和问题处理说明；非法 JSON 导入同样不会改变当前数据。 ## 容器运行 ```bash docker compose up -d --build curl http://localhost:8082/health docker compose down ``` 覆盖宿主端口：`WEB_PORT=9090 docker compose up -d --build`。 ## 说明 - “已修正”和“确认保留”是人工处置状态；编辑记录后问题会实时重新计算。 - 预览中的二维码以可编码文本展示，不依赖扫码库。 - 当前数据只存在于当前浏览器环境，建议定期导出 JSON 备份。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "a99447536991", "repo_name": "3003-theatre-costume-care-tracker", "task_type": "0-1 代码生成", "project_category": "全栈", "language_framework": "Node.js, React, TypeScript, Vite, Vitest, Python, FastAPI, SQLAlchemy, pytest, Docker Compose", "summary": "根据仓库 README 自动整理的导入基线说明：# 剧场服装洗护流转台 面向剧场服装管理人员的轻量全栈工作台。 … 界面适配桌面与 390px 窄屏，所有异步操作都有加载、成功或失败反馈。"} -->
## 3003 · 3003-theatre-costume-care-tracker

- 创建时间：2026-09-10 16:34:19 +0800
- 项目类别：全栈
- 任务难度：待评估
- 语言/框架：Node.js, React, TypeScript, Vite, Vitest, Python, FastAPI, SQLAlchemy, pytest, Docker Compose

### User Prompt

<!-- prompt-start -->
根据仓库 README 自动整理的导入基线说明：# 剧场服装洗护流转台 面向剧场服装管理人员的轻量全栈工作台。支持服装档案、借还/洗护/维修事件、最新状态、异常识别与问题闭环。仓库内置 4 件服装、5 条流转记录和 1 条异常示例数据，首次启动自动写入 SQLite。 ## 一键启动 要求 Docker 与 Docker Compose： ```bash docker compose up --build ``` 打开 <http://localhost:3003>。可用 `WEB_PORT=3100 docker compose up --build` 修改宿主端口。前端通过同源 `/api` 访问后端；SQLite 数据保存在命名卷 `costume_data`。 停止： ```bash docker compose down ``` 如需同时清除运行数据：`docker compose down -v`。 ## 功能与规则 - 服装：新增、编辑、停用；唯一编号、名称、剧目、角色、尺码、位置与备注。 - 流转：登记唯一事件编号、服装、时间、操作人、说明，以及借出、归还、送洗、洗护完成、送修、维修完成六类动作。 - 状态：按每件服装最新一条事件展示在库、借出、洗护中或维修中。本项目按约定不处理复杂乱序重算。 - 问题：借出时识别“未归还再次借出”“待清洗时借出”“维修中借出”，保留触发/相关事件，可处置为待处理、已确认、误报、已关闭并填写说明。 - 总览与检索：关键数量指标，按编号、剧目、角色、状态筛选。 - API 对重复编号返回 409，不存在资源返回 404，停用服装登记事件返回 409，字段缺失/非法返回 422。 ## 本地开发与测试 后端（Python 3.11+）： ```bash python3 -m venv .venv ./.venv/bin/pip install -r backend/requirements-dev.txt ./.venv/bin/pytest ./.venv/bin/uvicorn app.main:app --app-dir backend --reload ``` 根目录 `pytest.ini` 已配置模块路径，测试命令无需手工设置 `PYTHONPATH`。 前端（Node.js 20+）： ```bash cd frontend npm ci npm test npm run build ``` 开发服务器默认需要将 `/api` 指向后端；完整联调建议使用 Compose。 ## API 摘要 - `GET /health` - `GET/POST /api/costumes`，`PUT /api/costumes/{id}`，`PATCH /api/costumes/{id}/deactivate` - `GET/POST /api/events` - `GET /api/stats` - `GET /api/issues`，`PATCH /api/issues/{id}` FastAPI 交互文档可在后端容器网络的 `/docs` 查看；宿主环境通过前端仅代理 `/api`。 ## 技术结构 `backend/` 为 FastAPI、SQLAlchemy 与 SQLite；`frontend/` 为 React、TypeScript、Vite 与 Nginx。界面适配桌面与 390px 窄屏，所有异步操作都有加载、成功或失败反馈。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "ebb46cbfff3a", "repo_name": "hazard-label-contrast-preflight", "task_type": "0-1 代码生成", "project_category": "纯前端", "language_framework": "TypeScript, React, Vite, Vitest, Playwright, Docker, Docker Compose", "summary": "警示牌版面交付印厂前，文字或危险图形若贴近裁切线，成品偏移后可能缺字，操作员要在现有页面独立完成安全边距预检并看到可定位的版面证据。 … 用固定边界样例证明恰好贴合安全线算通过、四个方向的越界量可复算，并由浏览器从粘贴稿件到查看叠加图与问题明细走通主流程，再确认非法稿件不会展示部分几何结论。"} -->
## 0018-3 · hazard-label-contrast-preflight

- 创建时间：2026-09-10 17:42:02 +0800
- 项目类别：纯前端
- 任务难度：待评估
- 语言/框架：TypeScript, React, Vite, Vitest, Playwright, Docker, Docker Compose

### User Prompt

<!-- prompt-start -->
警示牌版面交付印厂前，文字或危险图形若贴近裁切线，成品偏移后可能缺字，操作员要在现有页面独立完成安全边距预检并看到可定位的版面证据。为此建立版面稿对象及其生命周期，输入为毫米单位的画布宽高、出血量、安全边距和带唯一 id、类别、x、y、宽、高的矩形元素，类别只接受文字、危险图形或装饰，解析与几何判定不得复用现有警示牌批次对象。用户在“版面安全区”入口粘贴 JSON 后执行预检，系统以画布内缩安全边距形成安全区，文字和危险图形必须完整包含其中，装饰只校验位于含出血范围的可印区域，并在按输入顺序排列的结果中用 SVG 同比例标出越界边和毫米偏差。字段缺失、非有限数值、非正尺寸、重复 id 或元素超出可印区域时，本次稿件进入输入错误状态并逐项指出路径，已有对比度导入、建议色、撤销和放行单行为保持原样，纯前端构建及 WEB_PORT 覆盖方式不变。用固定边界样例证明恰好贴合安全线算通过、四个方向的越界量可复算，并由浏览器从粘贴稿件到查看叠加图与问题明细走通主流程，再确认非法稿件不会展示部分几何结论。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "8e4cac5ab71d", "repo_name": "3002-plant-specimen-label-preflight", "task_type": "0-1 代码生成", "project_category": "纯前端", "language_framework": "Node.js, React, TypeScript, Vite, Vitest, Docker Compose", "summary": "为需要一次打印多份馆藏标签的标本员建立“打印批次”闭环，批次保存有序记录编号，并在浏览器本地存储中经历空批次与已编排两个状态。 … 复用现有记录存储、消息样式和容器配置，补充领域单测与组件端到端测试，验收加入顺序分页、重复加入不增量、失效成员清理和八条以上记录跨页打印，现有单条打印、导入及编辑撤销继续通过。"} -->
## 3002-3 · 3002-plant-specimen-label-preflight

- 创建时间：2026-09-10 20:48:07 +0800
- 项目类别：纯前端
- 任务难度：待评估
- 语言/框架：Node.js, React, TypeScript, Vite, Vitest, Docker Compose

### User Prompt

<!-- prompt-start -->
为需要一次打印多份馆藏标签的标本员建立“打印批次”闭环，批次保存有序记录编号，并在浏览器本地存储中经历空批次与已编排两个状态。记录卡可将当前标本加入或移出批次，工具栏入口打开批次预览，按加入顺序将标签确定性排入A4纸每页八格，随后一次调用浏览器打印。领域层负责去重、保持顺序、清理已不存在的记录并生成分页模型，React界面展示批次数量、分页预览和问题记录提示，但问题提示不阻止打印。若导入JSON、载入示例或删除记录导致批次成员失效，打开预览时自动剔除并明确提示，空批次点击预览只反馈“请先选择标本”，且不改变工作集。复用现有记录存储、消息样式和容器配置，补充领域单测与组件端到端测试，验收加入顺序分页、重复加入不增量、失效成员清理和八条以上记录跨页打印，现有单条打印、导入及编辑撤销继续通过。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "0915481f13d9", "repo_name": "3004-archive-box-page-audit", "task_type": "0-1 代码生成", "project_category": "纯前端", "language_framework": "Node.js, React, TypeScript, Vite, Vitest, Docker Compose", "summary": "根据仓库 README 自动整理的导入基线说明：# 纸质档案装盒页码核对台 纯前端、本地优先的档案装盒页码核对工具。 … nginx 提供 SPA 回退和 `/health` 健康检查。"} -->
## 3004 · 3004-archive-box-page-audit

- 创建时间：2026-09-10 23:54:18 +0800
- 项目类别：纯前端
- 任务难度：待评估
- 语言/框架：Node.js, React, TypeScript, Vite, Vitest, Docker Compose

### User Prompt

<!-- prompt-start -->
根据仓库 README 自动整理的导入基线说明：# 纸质档案装盒页码核对台 纯前端、本地优先的档案装盒页码核对工具。支持档案增删改、CSV 原子导入、自动异常识别、问题处置、筛选、每盒区间视图、打印清单，以及 JSON 全量备份恢复。数据只写入当前浏览器 `localStorage`。 ## 本地运行 ```bash npm install npm run dev ``` 测试与构建：`npm test`、`npm run build`。 ## CSV 导入 文件须为 UTF-8 CSV，首行必须严格使用： ```text 档号,标题,年度,保管期限,盒号,起始页,结束页,申报页数,备注 ``` 除备注外均必填；年度与页数字段须为 0–999999 的整数。导入检查现有数据及批内重复档号、起止页合法性。任一行失败会显示行号并取消整批导入，不改变原数据。带逗号或双引号的文本请使用标准 CSV 引号规则。 ## 核对与备份 自动识别重复档号、页码倒置、实际页数与申报页数不符、同盒区间重叠。问题可标记“已修正”或“确认保留”并填写说明；编辑档案后会重新检查并清除其旧处置。JSON 导入先完整校验，非法备份不会覆盖当前数据。 ## Docker ```bash docker compose up --build -d curl http://localhost:8084/health docker compose down ``` 可用 `WEB_PORT=18084` 修改宿主端口。nginx 提供 SPA 回退和 `/health` 健康检查。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "31aca1c58847", "repo_name": "3005-ink-batch-press-release", "task_type": "0-1 代码生成", "project_category": "全栈", "language_framework": "Node.js, React, TypeScript, Vite, Vitest, Python, FastAPI, SQLAlchemy, pytest, Docker Compose", "summary": "根据仓库 README 自动整理的导入基线说明：# 油墨批次上机放行台 面向印刷生产现场的轻量全栈放行工作台。 … 标准 viewport 与断点布局保证桌面及 390px 窄屏无页面级横向溢出。"} -->
## 3005 · 3005-ink-batch-press-release

- 创建时间：2026-09-10 23:54:20 +0800
- 项目类别：全栈
- 任务难度：待评估
- 语言/框架：Node.js, React, TypeScript, Vite, Vitest, Python, FastAPI, SQLAlchemy, pytest, Docker Compose

### User Prompt

<!-- prompt-start -->
根据仓库 README 自动整理的导入基线说明：# 油墨批次上机放行台 面向印刷生产现场的轻量全栈放行工作台。管理油墨批次、上机工单、风险检查和处置闭环；首次启动自动加入 4 个批次、1 张工单及示例问题。 ## Docker 一键启动 ```bash docker compose up --build ``` 浏览器打开 <http://localhost:3005>。可通过 `WEB_PORT=3105 docker compose up --build` 改端口。Compose 项目名固定为 `ink-batch-press-release`，SQLite 数据保存在命名卷中。 停止使用 `docker compose down`；需清除运行数据时使用 `docker compose down -v`。 ## 业务能力 - 新增、编辑、停用批次，记录唯一编号、颜色、供应商、日期、黏度、质检状态与备注。 - 创建唯一工单号的上机记录，关联批次、印刷机、承印材料、计划日期、操作人与说明。 - 工单创建时检查计划日期是否过期，以及批次是否待检、不合格或隔离；不合格与待检分别记录问题类型。 - 问题可更新为待处理、已确认、特批放行、已关闭；特批放行必须填写理由。 - 首页展示批次、合格、30 天内到期、工单、未处理问题指标，支持批次及问题多条件筛选。 - 重复编号返回 409，不存在资源返回 404，停用批次上机返回 409，缺失/非法字段返回 422。 ## 本地测试 后端（Python 3.11+，根配置已提供模块路径，无需设置 `PYTHONPATH`）： ```bash python3 -m venv .venv ./.venv/bin/pip install -r backend/requirements-dev.txt ./.venv/bin/pytest ``` 前端（Node.js 20+）： ```bash cd frontend npm ci npm test npm run build ``` ## API - `GET /health` - `GET/POST /api/batches`，`PUT /api/batches/{id}`，`PATCH /api/batches/{id}/deactivate` - `GET/POST /api/jobs` - `GET /api/issues`，`PATCH /api/issues/{id}` - `GET /api/stats` 技术栈为 FastAPI、SQLAlchemy、SQLite、React、TypeScript、Vite 与 Nginx。标准 viewport 与断点布局保证桌面及 390px 窄屏无页面级横向溢出。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "c7197d31ac5c", "repo_name": "tamper-evident-calibration-ledger", "task_type": "0-1 代码生成", "project_category": "纯后端", "language_framework": "Docker, Python 3.12, FastAPI, SQLAlchemy, PostgreSQL, Alembic, RFC 6962 Merkle Tree, HMAC, pytest", "summary": "为让外部审计系统明确记录已消费到哪次封存，引入“审计接入点”模块，持久保存接入方标识、幂等注册键和最后确认的检查点。 … 通过 Alembic 增加接入点表及唯一约束，在领域服务、模式与路由中贯通契约，端到端测试验证注册重放、顺序确认、多接入点隔离，以及跳级或并发确认只有一次成功且游标无回退。"} -->
## 0004-8 · tamper-evident-calibration-ledger

- 创建时间：2026-09-11 01:48:29 +0800
- 项目类别：纯后端
- 任务难度：待评估
- 语言/框架：Docker, Python 3.12, FastAPI, SQLAlchemy, PostgreSQL, Alembic, RFC 6962 Merkle Tree, HMAC, pytest

### User Prompt

<!-- prompt-start -->
为让外部审计系统明确记录已消费到哪次封存，引入“审计接入点”模块，持久保存接入方标识、幂等注册键和最后确认的检查点。审计员通过 POST /v1/audit-consumers 注册接入点，相同键和参数重放返回原对象，参数不同则返回幂等冲突。处理完一批增量事件后，调用 POST /v1/audit-consumers/{id}/acknowledgements 提交检查点，服务仅接受当前确认点的后继，首次确认只能从首个检查点开始，并在事务中单调推进游标。未知接入点或检查点返回对应未找到错误，跳级、倒退和重复确认返回包含当前值与期望前驱的冲突；数据库故障使用现有错误信封，事件、检查点和审计包行为保持兼容。通过 Alembic 增加接入点表及唯一约束，在领域服务、模式与路由中贯通契约，端到端测试验证注册重放、顺序确认、多接入点隔离，以及跳级或并发确认只有一次成功且游标无回退。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "f6da62a24990", "repo_name": "darkroom-working-solution-mixer", "task_type": "0-1 代码生成", "project_category": "纯前端", "language_framework": "TypeScript, React, Vite, Vitest, Playwright, Docker, Docker Compose", "summary": "暗房临时更换显影罐后，操作员常把“1+4”误当成五倍浓缩液，或在毫升取整后让工作液总量发生偏差。 … 非法字段须就地反馈且不保留旧配液卡；合法结果应同时显示浓缩液、清水、可逐项勾选的量取步骤及适合打印的配液卡，最终可观察到每一步不超容量且所有步骤合计严格等于目标总量。"} -->
## 0025 · darkroom-working-solution-mixer

- 创建时间：2026-09-11 02:57:10 +0800
- 项目类别：纯前端
- 任务难度：待评估
- 语言/框架：TypeScript, React, Vite, Vitest, Playwright, Docker, Docker Compose

### User Prompt

<!-- prompt-start -->
暗房临时更换显影罐后，操作员常把“1+4”误当成五倍浓缩液，或在毫升取整后让工作液总量发生偏差。请从空仓库实现一款纯前端配液台，使用 React、TypeScript 与 Vite，让用户输入稀释式 1+n、目标总量和量筒容量；n 只允许 1 至 99 的整数，总量与容量只允许 100 至 5000 mL 的整数。浓缩液体积按总量÷(n+1)计算，精确值以 0.5 mL 为界四舍五入到整数，清水量必须用目标总量减去取整后的浓缩液，保证两者之和不变。仓库须通过 Docker Compose 启动可访问页面，宿主端口由 WEB_PORT 覆盖，并提供名为 verify 的一次性验收服务运行 Vitest 与 Playwright；README 应在该链路旁解释启动和输入边界，禁止用固定结果代替计算。若单项液体超过量筒容量，界面按“若干满量筒加最后余量”生成分次量取步骤，恰好等于容量时不得多出零余量步骤。非法字段须就地反馈且不保留旧配液卡；合法结果应同时显示浓缩液、清水、可逐项勾选的量取步骤及适合打印的配液卡，最终可观察到每一步不超容量且所有步骤合计严格等于目标总量。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "9c2511d1d4b4", "repo_name": "pharma-gtin-validation-gate", "task_type": "0-1 代码生成", "project_category": "纯后端", "language_framework": "Docker, Python 3.12, FastAPI, Pydantic, pytest, Docker Compose", "summary": "药品收货接口若把扫描到的包装码仅按“十四位数字”放行，录入差错会直接进入后续追溯链路。 … 最终可观察到混合批次中每个包装码都得到唯一、保序且可复算的放行结论。"} -->
## 0027 · pharma-gtin-validation-gate

- 创建时间：2026-09-11 04:16:35 +0800
- 项目类别：纯后端
- 任务难度：待评估
- 语言/框架：Docker, Python 3.12, FastAPI, Pydantic, pytest, Docker Compose

### User Prompt

<!-- prompt-start -->
药品收货接口若把扫描到的包装码仅按“十四位数字”放行，录入差错会直接进入后续追溯链路。请从空仓库实现一个 Python 3.12、FastAPI 纯后端服务，接收含 1 至 100 个 codes 的 JSON 数组；每项必须恰为 14 个 ASCII 数字，空白、连字符、全角数字和其他字符均不转换。GTIN-14 的前 13 位从左到右依次乘 3、1、3、1，校验位固定为 `(10 - 加权和对 10 取模) 对 10 取模`。服务必须保留输入顺序和重复项，为每项返回原值、计算出的校验位以及 valid、format_error 或 checksum_mismatch；格式错误项的计算校验位为 null。数组为空、超过上限、成员非字符串或请求结构错误时整体返回 422 且不返回部分 results，合法结构即使含无效代码也返回 200。使用 Pydantic 固定请求边界和结构化错误，pytest 覆盖公式及接口，Docker Compose 的宿主端口由 API_PORT 覆盖，并提供名为 verify 的一次性验收服务；README 在公式旁给出可复算示例，.gitignore 排除本地产物，代码不得以占位实现代替校验。最终可观察到混合批次中每个包装码都得到唯一、保序且可复算的放行结论。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "14530f197845", "repo_name": "stage-fly-sequence-rehearsal", "task_type": "0-1 代码生成", "project_category": "纯前端", "language_framework": "TypeScript, React, Vite, Vitest, Playwright, Docker, Docker Compose", "summary": "舞台监督在排练前收到一叠吊杆口令卡，顺序稍有颠倒就可能出现未锁定先移动或未归位先解锁，但纸面复核难以展示错误发生时的设备状态。 … 最终监督可看到整套口令闭合回到空载归位，或明确看到唯一首错及当时的吊杆状态。"} -->
## 0028 · stage-fly-sequence-rehearsal

- 创建时间：2026-09-11 04:27:21 +0800
- 项目类别：纯前端
- 任务难度：待评估
- 语言/框架：TypeScript, React, Vite, Vitest, Playwright, Docker, Docker Compose

### User Prompt

<!-- prompt-start -->
舞台监督在排练前收到一叠吊杆口令卡，顺序稍有颠倒就可能出现未锁定先移动或未归位先解锁，但纸面复核难以展示错误发生时的设备状态。请从空仓库实现纯前端预演台，用React、TypeScript与Vite完成动作卡拖放排序、状态推演和首错定位，并以Vitest验证裁决规则、Playwright覆盖重排后复算。初始状态固定为空载且归位；装载仅允许在空载时执行，重量必须为1至500千克的整数；装载后只能锁定，锁定后可移动到舞台位，舞台位只能归位，归位且仍锁定时才可解锁，解锁后才能卸载回到初始状态。仓库须通过Docker Compose运行，宿主端口由WEB_PORT覆盖，并提供名为verify的一次性验收服务；应用不得设置业务后端或访问在线服务。推演遇到第一张非法卡即停止，后续卡不得继续改变状态，界面同时保留此前轨迹、首错卡和具体原因；任何增删或重排都要清除旧结论后重新裁决。README说明卡片含义及启动方式，.gitignore排除依赖与产物，不能以固定响应或未实现按钮代替交互。最终监督可看到整套口令闭合回到空载归位，或明确看到唯一首错及当时的吊杆状态。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "718cbb650296", "repo_name": "3004-archive-box-page-audit", "task_type": "0-1 代码生成", "project_category": "纯前端", "language_framework": "Node.js, React, TypeScript, Vite, Vitest, Docker Compose", "summary": "库房人员需要在不改动档案登记信息的前提下核实实体盒内容，请在每盒页码区间旁加入独立的盒内盘点面板，以一次盘点会话保存所选盒号、创建时间及当时盒内档案的快照。 … 用 Vitest 验证唯一命中和歧义选择落到正确会话项，并以界面测试还原开始盘点、刷新续盘至自动完成，以及无效和重复扫描不推进进度，现有构建与 Docker Compose 健康检查继续通过。"} -->
## 3004-3 · 3004-archive-box-page-audit

- 创建时间：2026-09-11 05:44:22 +0800
- 项目类别：纯前端
- 任务难度：待评估
- 语言/框架：Node.js, React, TypeScript, Vite, Vitest, Docker Compose

### User Prompt

<!-- prompt-start -->
库房人员需要在不改动档案登记信息的前提下核实实体盒内容，请在每盒页码区间旁加入独立的盒内盘点面板，以一次盘点会话保存所选盒号、创建时间及当时盒内档案的快照。用户选择盒号开始后，可连续输入或扫描档号，唯一命中时按会话项标识登记为已找到，全部命中后自动完成，刷新页面仍能查看进度和结果。若同一档号命中多件，面板展示各候选的盘点序号、题名和页码区间，用户明确选中一件才推进进度；取消选择、档号不存在或重复扫描时给出对应提示，快照和计数保持不变。会话项在创建时生成独立标识并保留来源档案标识，匹配服务只更新盘点快照，结果写入新的本地存储键，现有档案、异常处置、连续编页及 JSON 备份内容均不被改写，旧数据可直接加载。用 Vitest 验证唯一命中和歧义选择落到正确会话项，并以界面测试还原开始盘点、刷新续盘至自动完成，以及无效和重复扫描不推进进度，现有构建与 Docker Compose 健康检查继续通过。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "d78a8c3521bf", "repo_name": "darkroom-working-solution-mixer", "task_type": "0-1 代码生成", "project_category": "纯前端", "language_framework": "TypeScript, React, Vite, Vitest, Playwright, Docker, Docker Compose", "summary": "暗房需要在现有配液计算之外建立独立的药液处理容量台账，让操作员按实际冲洗量掌握一批药液还能处理多少胶片，避免凭记忆继续使用已经耗尽的药液。 … 原配液表单、分罐步骤、打印卡和无障碍反馈保持原有行为，Docker Compose 的 web 与 verify 链路继续可用，WEB_PORT 仍可覆盖宿主端口。"} -->
## 0025-3 · darkroom-working-solution-mixer

- 创建时间：2026-09-11 05:44:54 +0800
- 项目类别：纯前端
- 任务难度：待评估
- 语言/框架：TypeScript, React, Vite, Vitest, Playwright, Docker, Docker Compose

### User Prompt

<!-- prompt-start -->
暗房需要在现有配液计算之外建立独立的药液处理容量台账，让操作员按实际冲洗量掌握一批药液还能处理多少胶片，避免凭记忆继续使用已经耗尽的药液。操作员从顶部“容量台账”入口创建带名称和额定容量的药液批次，再选中批次登记本次处理的等效胶片数量与备注，页面按时间展示使用记录、累计用量、剩余容量和使用中或已耗尽状态。领域服务以创建批次和登记用量两个命令作为契约，每条记录写入前重新计算剩余量，Vitest 应证明连续登记不会产生负数且恰好用完时状态确定转为已耗尽。批次与不可修改的使用记录保存在 localStorage，刷新后仍能还原同一台账，空名称、非正整数或超过剩余容量时就地说明原因且不写入记录，Playwright 从新建批次走到分次用完并验证刷新恢复。原配液表单、分罐步骤、打印卡和无障碍反馈保持原有行为，Docker Compose 的 web 与 verify 链路继续可用，WEB_PORT 仍可覆盖宿主端口。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "31168478d2a3", "repo_name": "stage-fly-sequence-rehearsal", "task_type": "0-1 代码生成", "project_category": "纯前端", "language_framework": "TypeScript, React, Vite, Vitest, Playwright, Docker, Docker Compose", "summary": "舞台监督需要把纸面预演转成逐张报令的走台会话，以便操作者只看到当前应执行的口令、执行后的吊杆状态和剩余张数，而不是一次读完整条轨迹。 … Vitest验证合法推进、首错停步和末张幂等，Playwright证明标准闭环逐张完成、非法序列在对应卡受阻且后续状态不变，并确认走台期间编辑入口不可用、结束后重新可用。"} -->
## 0028-3 · stage-fly-sequence-rehearsal

- 创建时间：2026-09-11 07:30:00 +0800
- 项目类别：纯前端
- 任务难度：待评估
- 语言/框架：TypeScript, React, Vite, Vitest, Playwright, Docker, Docker Compose

### User Prompt

<!-- prompt-start -->
舞台监督需要把纸面预演转成逐张报令的走台会话，以便操作者只看到当前应执行的口令、执行后的吊杆状态和剩余张数，而不是一次读完整条轨迹。监督整理任意非空序列后点击“开始走台”，系统复制当时的卡序与重量作为会话快照，随后每次点击“执行下一张”都通过现有单卡裁决推进游标，合法闭合时显示走台完成，遇到非法卡则停在执行前状态并给出卡号与原因。会话领域契约应以纯函数管理待命、进行中、完成、受阻这一组状态及快照、游标和当前吊杆状态，App负责接线，独立走台面板呈现当前卡与进度，进行中锁定牌库、排序、重量、删除和草稿操作，结束或受阻后恢复编辑。空序列开始时留在待命并显示可理解的反馈，重复点击不会越过末张或受阻卡，刷新仍按现有空序列启动，草稿格式、实时裁决、Docker Compose与WEB_PORT覆盖保持兼容。Vitest验证合法推进、首错停步和末张幂等，Playwright证明标准闭环逐张完成、非法序列在对应卡受阻且后续状态不变，并确认走台期间编辑入口不可用、结束后重新可用。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "7c172a56bc82", "repo_name": "pharma-gtin-validation-gate", "task_type": "0-1 代码生成", "project_category": "纯后端", "language_framework": "Docker, Python 3.12, FastAPI, Pydantic, pytest, Docker Compose", "summary": "药品完成收货后，质量人员为该收货单登记冷链记录，提交唯一评估号、允许温区及按时间递增的采样点，取得可复查的运输温控结论。 … pytest 与 verify 通过真实 HTTP 验证全程合规、多个越界区段的积分和读取一致性、重复评估号冲突，以及非法时间序列不产生残记录。"} -->
## 0027-3 · pharma-gtin-validation-gate

- 创建时间：2026-09-11 07:52:38 +0800
- 项目类别：纯后端
- 任务难度：待评估
- 语言/框架：Docker, Python 3.12, FastAPI, Pydantic, pytest, Docker Compose

### User Prompt

<!-- prompt-start -->
药品完成收货后，质量人员为该收货单登记冷链记录，提交唯一评估号、允许温区及按时间递增的采样点，取得可复查的运输温控结论。实现独立的冷链评估领域模块，将连续越界采样归并为异常区段，按相邻点做梯形积分，计算持续分钟数和偏离温区的度分钟，并保存原始采样与摘要。POST /cold-chain-assessments 创建评估，GET /cold-chain-assessments/{assessment_id} 返回同一份确定性结果，计算值按分钟保留两位小数。评估号重复返回409，收货单不存在返回404，温区无效、采样点不足、时间未严格递增或跨度超过七天返回422，失败请求不留记录。SQLite 启动迁移加入评估与采样表并关联现有收货单，FastAPI 模型及错误信封保持项目风格，既有接口不改变，Compose 不增加常驻服务且 API_PORT 仍可覆盖宿主端口。pytest 与 verify 通过真实 HTTP 验证全程合规、多个越界区段的积分和读取一致性、重复评估号冲突，以及非法时间序列不产生残记录。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "839ce73ab2ed", "repo_name": "3005-ink-batch-press-release", "task_type": "0-1 代码生成", "project_category": "全栈", "language_framework": "Node.js, React, TypeScript, Vite, Vitest, Python, FastAPI, SQLAlchemy, pytest, Docker Compose", "summary": "将批次建档时的一次黏度值扩展为独立的现场黏度巡检模块，操作员从批次列表展开巡检面板，按实际测量顺序登记测量时间、黏度、人员和备注，并查看按时间排列的历史与趋势。 … 前端测试走通登记两次异常读数并刷新趋势和问题，后端测试证明正常读数不告警、逆序时间返回冲突且不落库，并用并发请求确认同批次只生成一条未关闭问题。"} -->
## 3005-6 · 3005-ink-batch-press-release

- 创建时间：2026-09-11 12:48:17 +0800
- 项目类别：全栈
- 任务难度：待评估
- 语言/框架：Node.js, React, TypeScript, Vite, Vitest, Python, FastAPI, SQLAlchemy, pytest, Docker Compose

### User Prompt

<!-- prompt-start -->
将批次建档时的一次黏度值扩展为独立的现场黏度巡检模块，操作员从批次列表展开巡检面板，按实际测量顺序登记测量时间、黏度、人员和备注，并查看按时间排列的历史与趋势。后端增加巡检记录实体及批次关联，登记接口以数据库写事务拒绝早于该批次最新测量时间的补录，并按测量时间和记录编号稳定排序，使并发登记不会漏判或重复判定。每次成功登记后，以批次建档黏度为基准检查最新连续两条记录，若均向同一方向偏离超过百分之十，则原子创建该批次至多一条未关闭的黏度漂移问题，刷新后在趋势和问题处置中都能看到结果。为让巡检问题不依赖工单，问题表的工单关联改为可空，响应增加问题来源并允许工单编号为空，巡检问题显示批次巡检标识，现有工单风险问题仍展示原工单编号并保持筛选和处置行为，旧库启动时完成兼容迁移。前端测试走通登记两次异常读数并刷新趋势和问题，后端测试证明正常读数不告警、逆序时间返回冲突且不落库，并用并发请求确认同批次只生成一条未关闭问题。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "d0b22ab8bcfd", "repo_name": "accessible-egress-grid-verifier", "task_type": "0-1 代码生成", "project_category": "全栈", "language_framework": "Docker, Python 3.12, FastAPI, Pydantic, TypeScript, React, Vite, pytest, Vitest, Playwright", "summary": "改造中的社区礼堂只有一张方格化平面草图，轮椅疏散路线若靠肉眼挑选，常会遗漏被临时隔断截断的通道。 … 最终核验员能看到唯一最短疏散轨迹，或看到不可能误解为可通行的失败现象。"} -->
## 0029 · accessible-egress-grid-verifier

- 创建时间：2026-09-11 12:44:59 +0800
- 项目类别：全栈
- 任务难度：待评估
- 语言/框架：Docker, Python 3.12, FastAPI, Pydantic, TypeScript, React, Vite, pytest, Vitest, Playwright

### User Prompt

<!-- prompt-start -->
改造中的社区礼堂只有一张方格化平面草图，轮椅疏散路线若靠肉眼挑选，常会遗漏被临时隔断截断的通道。请从空仓库实现一个全栈核验器：核验员在 React 网格编辑器中创建 2 至 40 行、2 至 40 列的平面，设置恰好一个起点、一个出口及若干阻挡格，前端把结构化数据提交给 FastAPI，API 自行实现四方向最短路径搜索并返回有序坐标和步数。每格代表 0.5 米，起点计入路线但不计步；只能上下左右进入非阻挡格。存在多条等长路线时，扩展相邻格必须固定按上、右、下、左，因而结果唯一。仓库中前置配置 TypeScript、Pydantic、pytest、Vitest 与 Playwright，并提供名为 verify 的一次性验收服务；Docker Compose 启动 web 与 api，WEB_PORT、API_PORT 可覆盖宿主端口。README 应说明请求契约和运行方式，.gitignore 排除构建产物，前后端均须给出可操作的字段级错误反馈，禁止用固定响应或占位实现代替联调。行列不符、坐标越界、起终点重合或被阻挡时整次请求失败且不返回路线；出口不可达时明确显示“不可达”、已探索格数为零以外的真实值，但路线画布不得残留上一次成功结果。最终核验员能看到唯一最短疏散轨迹，或看到不可能误解为可通行的失败现象。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "bfe66a8001aa", "repo_name": "concrete-compression-release-gate", "task_type": "0-1 代码生成", "project_category": "纯后端", "language_framework": "Docker, Python 3.12, FastAPI, Pydantic, Decimal, pytest, Docker Compose", "summary": "施工现场送来的三块混凝土试件可能平均强度达标，却被异常低值掩盖，实验室需要只接收整组数据并立即给出唯一结论的纯后端接口。 … README 写明接口示例、单位和计算规则，.gitignore 排除本地产物，禁止固定响应或占"} -->
## 0030 · concrete-compression-release-gate

- 创建时间：2026-09-11 13:43:35 +0800
- 项目类别：纯后端
- 任务难度：待评估
- 语言/框架：Docker, Python 3.12, FastAPI, Pydantic, Decimal, pytest, Docker Compose

### User Prompt

<!-- prompt-start -->
施工现场送来的三块混凝土试件可能平均强度达标，却被异常低值掩盖，实验室需要只接收整组数据并立即给出唯一结论的纯后端接口。代码从空仓库起步，使用 Python 3.12、FastAPI、Pydantic 与 Decimal，实现请求契约、强度计算和批次放行裁决。每次 JSON 请求必须包含设计强度及恰好三个试件，每个试件提供受压面积 mm² 和破坏载荷 kN，所有数值均须大于零。单块强度按“载荷×1000÷面积”计算为 MPa，先以 ROUND_HALF_UP 保留 0.1 MPa，再用三个舍入后数值计算算术平均值并同法保留 0.1 MPa。仅当平均值不低于设计强度且最低单值不低于设计强度的 85.0% 时通过，等于阈值计入通过。合法响应返回三项强度、平均强度、通过布尔值和 reasons 数组；未通过时数组须包含全部未满足条件，并固定按 MEAN_BELOW_DESIGN、MIN_BELOW_85_PERCENT 排序，同时不满足时返回两项，通过时为空。字段缺失、试件数量错误或非正数值统一返回 422，且不得返回任何部分强度。Docker Compose 发布 API，宿主端口可由 API_PORT 覆盖，并提供 verify 一次性服务，以 pytest 通过真实 HTTP 验证链路。README 写明接口示例、单位和计算规则，.gitignore 排除本地产物，禁止固定响应或占
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "ab6cfe0328a1", "repo_name": "organ-roll-hole-verifier", "task_type": "0-1 代码生成", "project_category": "纯前端", "language_framework": "TypeScript, React, Vite, Vitest, Playwright, Docker, Docker Compose", "summary": "手摇风琴纸带的孔位偏差会造成阀门回位不及或同时耗气过多，制带员需要在冲孔前核验离散网格。 … 使用 Docker Compose 启动 web，宿主端口可由 WEB_PORT 覆盖，并提供一次性 verify 验收服务；README 给出网格坐标示例，.gitignore 排除构建产物，禁止固"} -->
## 0032 · organ-roll-hole-verifier

- 创建时间：2026-09-11 13:58:52 +0800
- 项目类别：纯前端
- 任务难度：待评估
- 语言/框架：TypeScript, React, Vite, Vitest, Playwright, Docker, Docker Compose

### User Prompt

<!-- prompt-start -->
手摇风琴纸带的孔位偏差会造成阀门回位不及或同时耗气过多，制带员需要在冲孔前核验离散网格。请从空仓库起步，实现 TypeScript、React、Vite 纯前端应用，不调用在线服务。编辑区固定 24 条音轨，长度可选 32、48 或 64 个节拍列，支持鼠标点击和键盘切换孔位。裁决规则唯一：第 1、2 列及最后 2 列禁孔；同一音轨任意两孔列号差至少为 2；每列最多 4 个孔；至少有 1 个孔才可制作。一次裁决列出全部违规格，面板按禁孔、复孔过近、列超载聚合展示，修正后立即重算；合法时明确显示可制作与总孔数，空白时显示尚未录入。比例化打印预览须保留 24 条音轨、列号及违规标记。Vitest 覆盖四类结论和边界列，Playwright 覆盖键鼠编辑、违规修正闭环及打印预览一致性。使用 Docker Compose 启动 web，宿主端口可由 WEB_PORT 覆盖，并提供一次性 verify 验收服务；README 给出网格坐标示例，.gitignore 排除构建产物，禁止固定响应或未实现按钮。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "1a096ddbc76e", "repo_name": "redaction-rule-lab", "task_type": "0-1 代码生成", "project_category": "纯前端", "language_framework": "Docker, TypeScript, Vue 3, Vite, Vitest, Playwright", "summary": "法务调整规则后，需要在真实合同外发前确认一组典型片段的脱敏结果没有回退，请加入本地“回归样例集”闭环，样例仅驻留浏览器内存且不参与正式导出。 … Vitest 验证样例解析、顺序执行、首差异定位及规则变化重跑，Playwright 从载入含一项失败的样例集、定位差异、修正规则触发全量通过，到继续完成原有脱敏确认和下载，证明该模块可独立验收。"} -->
## 0008-3 · redaction-rule-lab

- 创建时间：2026-09-11 20:54:47 +0800
- 项目类别：纯前端
- 任务难度：待评估
- 语言/框架：Docker, TypeScript, Vue 3, Vite, Vitest, Playwright

### User Prompt

<!-- prompt-start -->
法务调整规则后，需要在真实合同外发前确认一组典型片段的脱敏结果没有回退，请加入本地“回归样例集”闭环，样例仅驻留浏览器内存且不参与正式导出。用户选择 JSON 样例文件后自动运行，每项包含编号、原文、期望脱敏文本及可选的期望命中规则编号序列，解析器校验唯一编号和字段类型，执行器复用当前有效规则与完整管线并按文件顺序产出结果。store 维护当前样例集、逐项结果和规则变化后的重跑状态，独立面板汇总通过数，点击失败项可查看首个文本差异位置、实际与期望片段以及规则序列差异。文件语法或字段错误应定位到样例编号或数组下标并保留上一份有效报告，单项管线失败显示原有错误位置且不覆盖其他项，回归检查不改变规则启停、人工确认、例外审阅和下载闸门，旧规则与现有操作无需迁移。Vitest 验证样例解析、顺序执行、首差异定位及规则变化重跑，Playwright 从载入含一项失败的样例集、定位差异、修正规则触发全量通过，到继续完成原有脱敏确认和下载，证明该模块可独立验收。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "a3ccf619d65f", "repo_name": "port-laytime-adjudicator", "task_type": "0-1 代码生成", "project_category": "纯后端", "language_framework": "Python 3.12, FastAPI, SQLAlchemy, PostgreSQL, Alembic, pytest, Docker, Docker Compose", "summary": "同一航次可能在多个港口分别形成结算结果，结算员要把二至二十个既有结果按提交顺序生成不可变的航次封顶清单，并输入非负整数分的赔付上限。 … 现有结算创建、查询和对比契约及API_PORT配置保持可用，pytest与verify应验收未触发封顶、按比例取整且总额精确等于上限、同余数按输入顺序分配，以及失败后清单和明细数量不变。"} -->
## 0017-4 · port-laytime-adjudicator

- 创建时间：2026-09-11 21:25:05 +0800
- 项目类别：纯后端
- 任务难度：待评估
- 语言/框架：Python 3.12, FastAPI, SQLAlchemy, PostgreSQL, Alembic, pytest, Docker, Docker Compose

### User Prompt

<!-- prompt-start -->
同一航次可能在多个港口分别形成结算结果，结算员要把二至二十个既有结果按提交顺序生成不可变的航次封顶清单，并输入非负整数分的赔付上限。服务读取持久化结果，原费用合计未超过上限时逐项照录，超过时按各项原费用比例分配上限，使用最大余数法补足整分，余数相同按提交顺序决定，零费用项始终分得零。为航次清单及明细建立模型和Alembic迁移，在服务层保存结果标识、原费用、分配费用、合计与创建时间快照，通过创建接口返回清单，并由查询接口按清单标识完整回放。重复结果标识返回定位到对应下标的422，引用缺失返回指出下标与标识的404，非法上限或不足两个结果不写入清单，查询不存在的清单返回404。现有结算创建、查询和对比契约及API_PORT配置保持可用，pytest与verify应验收未触发封顶、按比例取整且总额精确等于上限、同余数按输入顺序分配，以及失败后清单和明细数量不变。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "b37918a36c1d", "repo_name": "kiln-heatwork-release", "task_type": "0-1 代码生成", "project_category": "全栈", "language_framework": "Docker, Python 3.12, FastAPI, Pydantic, TypeScript, React, Vite, pytest, Vitest, Playwright", "summary": "窑炉控制器导出的采样间隔并不固定，温度又可能在两个采样点之间越过计热起点，质检员不能再靠表格逐行估算烧成是否合格。 … 合法提交应保存原始点、未舍入积分、展示值和结论，刷新后仍能复查；任一非法点则整次不落库，界面明确指出其索引与原因，最终质检员看到唯一的欠烧、合格或过烧结果。"} -->
## 0033 · kiln-heatwork-release

- 创建时间：2026-09-12 05:48:11 +0800
- 项目类别：全栈
- 任务难度：待评估
- 语言/框架：Docker, Python 3.12, FastAPI, Pydantic, TypeScript, React, Vite, pytest, Vitest, Playwright

### User Prompt

<!-- prompt-start -->
窑炉控制器导出的采样间隔并不固定，温度又可能在两个采样点之间越过计热起点，质检员不能再靠表格逐行估算烧成是否合格。请从空仓库实现一套真实联调的全栈判定台：页面接收窑次名称及 2 至 200 个 JSON 采样点，每点含 ISO 8601 时刻和摄氏温度；时刻须严格递增、首末间隔不超过 12 小时，温度限于 0 至 1400°C。后端以相邻点间温度线性变化为唯一约定，只累计高于 600°C 的部分；跨越 600°C 时先线性求交点再切段，以梯形法得到°C·min，最终按四舍五入 half-up 保留一位。小于 18000.0 判欠烧，18000.0 至 24000.0（含两端）判合格，大于 24000.0 判过烧。采用 FastAPI 与 React，字段错误须在页面对应位置呈现；pytest、Vitest 和 Playwright 覆盖积分边界与真实联调，禁止固定响应或占位实现。Docker Compose 运行 web 与 api，宿主端口分别可由 WEB_PORT、API_PORT 覆盖，并提供名为 verify 的一次性验收服务；README 在积分实现旁解释示例，.gitignore 排除本地产物。合法提交应保存原始点、未舍入积分、展示值和结论，刷新后仍能复查；任一非法点则整次不落库，界面明确指出其索引与原因，最终质检员看到唯一的欠烧、合格或过烧结果。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "946312684788", "repo_name": "saddle-stitch-imposition-api", "task_type": "0-1 代码生成", "project_category": "纯后端", "language_framework": "Python 3.12, FastAPI, Pydantic, pytest, Docker, Docker Compose", "summary": "短版画册交付印厂前，制版员必须确认骑马订后的正反面页码位置；靠手工从首尾交替填写，内页一多就容易出现重页或漏页。 … 错误响应须可由调用方定位 total_pages，合法响应中每个页码恰好出现一次，最终制版员能直接得到唯一且可复算的印刷面次序。"} -->
## 0034 · saddle-stitch-imposition-api

- 创建时间：2026-09-12 05:57:26 +0800
- 项目类别：纯后端
- 任务难度：待评估
- 语言/框架：Python 3.12, FastAPI, Pydantic, pytest, Docker, Docker Compose

### User Prompt

<!-- prompt-start -->
短版画册交付印厂前，制版员必须确认骑马订后的正反面页码位置；靠手工从首尾交替填写，内页一多就容易出现重页或漏页。请从空仓库实现纯后端 FastAPI 服务，接收 JSON 中唯一的 total_pages；它只能是 4 至 128 的整数且必须被 4 整除，否则返回 422 的字段级原因，不得输出局部结果。仓库使用 Python 3.12，并在 Docker Compose 中提供 api 与一次性 verify 服务，api 的宿主端口由 API_PORT 覆盖；pytest 覆盖排列不变量，README 在接口示例旁解释纸张顺序，.gitignore 排除本地产物，禁止占位实现。核心算法按纸张由外到内编号 i=0…total_pages/4-1：每张正面从左到右为 [total_pages-2i, 1+2i]，背面从左到右为 [2+2i, total_pages-1-2i]。POST 接口应同步返回纸张总数及每张的序号、front、back；页码均为一基整数，不旋转、不补空白页。错误响应须可由调用方定位 total_pages，合法响应中每个页码恰好出现一次，最终制版员能直接得到唯一且可复算的印刷面次序。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "776cc5051919", "repo_name": "gel-stack-color-preview", "task_type": "0-1 代码生成", "project_category": "纯前端", "language_framework": "TypeScript, React, Vite, Vitest, Playwright, Docker, Docker Compose", "summary": "舞台灯光师常在装台前凭经验叠放色片，难以直观看出多层滤光后的颜色和亮度损失。 … 采用 TypeScript、React、Vite，使用 Vitest 覆盖计算边界、Playwright 验证拖拽与恢复流程；Docker Compose 提供 web 和一次性 verify 服务，"} -->
## 0035 · gel-stack-color-preview

- 创建时间：2026-09-12 06:07:06 +0800
- 项目类别：纯前端
- 任务难度：待评估
- 语言/框架：TypeScript, React, Vite, Vitest, Playwright, Docker, Docker Compose

### User Prompt

<!-- prompt-start -->
舞台灯光师常在装台前凭经验叠放色片，难以直观看出多层滤光后的颜色和亮度损失。请从空仓库起步，实现一个纯前端预检台：用户可从目录拖拽一至五张色片、调整顺序，并查看实际叠放次序、最终色块、透光率和明暗结论；每张色片包含六位十六进制 sRGB 颜色及 1% 至 100% 的整数透光率。计算时将通道值除以 255，小于等于 0.04045 时除以 12.92，否则使用 ((v+0.055)/1.055)^2.4 转为线性值；各层逐通道相乘后按逆公式转回 sRGB，乘 255 并四舍五入为整数，透光率按各层百分比相乘并四舍五入到 0.1%。结果不低于 20.0% 标为可用，否则标为过暗。非法六位十六进制颜色须明确报错，不得覆盖 localStorage 中最近一次有效方案，刷新后恢复该方案。采用 TypeScript、React、Vite，使用 Vitest 覆盖计算边界、Playwright 验证拖拽与恢复流程；Docker Compose 提供 web 和一次性 verify 服务，WEB_PORT 可覆盖宿主端口，README 给出一组可复算示例。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "a3b55e8692e3", "repo_name": "venue-frequency-clearance-api", "task_type": "0-1 代码生成", "project_category": "纯后端", "language_framework": "Go 1.25, Gin, testify, Docker, Docker Compose", "summary": "多场会议共用场馆无线话筒时，载波本身没有重叠也可能因保护间隔不足产生串扰，协调员需要在进场前得到唯一的放行结论。 … 合法响应按设备编号排序输出保护区间；冲突对先将两个编号字典序排列，再按第一、第二编号升序去重返回，使协调员看到 accepted=true，或得到稳定且可逐项复核的越界设备与冲突对。"} -->
## 0037 · venue-frequency-clearance-api

- 创建时间：2026-09-12 09:45:30 +0800
- 项目类别：纯后端
- 任务难度：待评估
- 语言/框架：Go 1.25, Gin, testify, Docker, Docker Compose

### User Prompt

<!-- prompt-start -->
多场会议共用场馆无线话筒时，载波本身没有重叠也可能因保护间隔不足产生串扰，协调员需要在进场前得到唯一的放行结论。请从空仓库实现纯后端 API，接收 1 至 200 台设备，每台含唯一字符串编号、用途、整数中心频率 kHz 和带宽 kHz；用途仅 handheld、bodypack、ifb，对应两侧保护间隔固定为 125、175、250 kHz。带宽必须是 25 至 400 的正整数，设备占用区间定义为 [中心频率-floor(带宽/2), 中心频率+ceil(带宽/2)]，再向两侧加入该用途间隔；允许频段为闭区间 [470000,694000] kHz。任一保护区间越界即整份拒绝；两个闭区间端点相等也算冲突。采用 Go 与 Gin，实现区间裁决和可定位字段的错误反馈；单元测试在规则代码旁覆盖奇数带宽、端点相触及多重冲突，README 给出可复算示例，禁止固定结果或占位实现。Docker Compose 的 API 宿主端口由 API_PORT 覆盖，并提供名为 verify 的一次性验收服务。合法响应按设备编号排序输出保护区间；冲突对先将两个编号字典序排列，再按第一、第二编号升序去重返回，使协调员看到 accepted=true，或得到稳定且可逐项复核的越界设备与冲突对。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "8ebc290107c9", "repo_name": "saddle-stitch-imposition-api", "task_type": "0-1 代码生成", "project_category": "纯后端", "language_framework": "Python 3.12, FastAPI, Pydantic, pytest, Docker, Docker Compose", "summary": "印厂接单人员需要在拼版前形成可追溯的纸张成本报价，请从零加入报价模块，让报价单拥有独立编号、金额快照及待确认和已确认的生命周期。 … 自动化验收应证明一千册十六页画册在给定单价和损耗率下得到固定数量与金额，确认后数据可重新读取，并在非法计价参数、未知或重复确认时获得对应反馈。"} -->
## 0034-3 · saddle-stitch-imposition-api

- 创建时间：2026-09-12 10:40:31 +0800
- 项目类别：纯后端
- 任务难度：待评估
- 语言/框架：Python 3.12, FastAPI, Pydantic, pytest, Docker, Docker Compose

### User Prompt

<!-- prompt-start -->
印厂接单人员需要在拼版前形成可追溯的纸张成本报价，请从零加入报价模块，让报价单拥有独立编号、金额快照及待确认和已确认的生命周期。操作员通过 POST /quotes 提交总页数、印量、每张纸单价和损耗率，领域对象按每册纸张数、印量与向上取整后的损耗计算纸张总量，再以 Decimal 保留两位金额并持久化报价快照。确认采用报价时调用 POST /quotes/{quote_id}/confirm，只有待确认报价可转为已确认，未知编号返回 404，重复确认返回 409，错误体保持现有 FastAPI 错误结构且不改写原快照。请求模型拒绝布尔值、浮点单价、负数及多余字段，SQLite 仓储与迁移随应用启动复用同一进程，不引入新服务，原拼版、定位、健康检查和 API_PORT 配置继续可用。自动化验收应证明一千册十六页画册在给定单价和损耗率下得到固定数量与金额，确认后数据可重新读取，并在非法计价参数、未知或重复确认时获得对应反馈。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "e81bb3d27e4c", "repo_name": "accessible-egress-grid-verifier", "task_type": "0-1 代码生成", "project_category": "全栈", "language_framework": "Docker, Python 3.12, FastAPI, Pydantic, TypeScript, React, Vite, pytest, Vitest, Playwright", "summary": "礼堂路线核验通过后，现场人员还缺少记录轮椅按路线逐格试走耗时的工具，请实现独立的“通行实测”模块，以不可变路线快照和当前检查点作为核心数据。 … 未启动实测的路线核验、费力格选路及旧响应保持兼容，现有 web、api、verify 编排继续工作，宿主端口仍由 WEB_PORT、API_PORT 覆盖，verify 纳入新增后端、组件和端到端用例"} -->
## 0029-4 · accessible-egress-grid-verifier

- 创建时间：2026-09-12 11:01:53 +0800
- 项目类别：全栈
- 任务难度：待评估
- 语言/框架：Docker, Python 3.12, FastAPI, Pydantic, TypeScript, React, Vite, pytest, Vitest, Playwright

### User Prompt

<!-- prompt-start -->
礼堂路线核验通过后，现场人员还缺少记录轮椅按路线逐格试走耗时的工具，请实现独立的“通行实测”模块，以不可变路线快照和当前检查点作为核心数据。核验员从成功结果启动一次实测，输入到达下一格所用秒数并逐步确认，页面持续显示下一坐标、累计时间和完成进度，到达出口后锁定总耗时；Playwright 应从核验路线贯通到实测完成。后端在 API 进程内使用 SQLite 和建表迁移保存实测记录，只开放创建实测与推进检查点两个写接口，创建时校验路线至少两格且相邻坐标仅四方向移动，推进响应返回完整进度，pytest 验证跨请求累积与最终落库一致。秒数不是 1 至 3600 的整数、实测编号不存在或完成后继续推进时，返回现有字段级错误结构且数据不发生变化；前端把反馈留在实测面板，不清除原路线。未启动实测的路线核验、费力格选路及旧响应保持兼容，现有 web、api、verify 编排继续工作，宿主端口仍由 WEB_PORT、API_PORT 覆盖，verify 纳入新增后端、组件和端到端用例。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "04d89926e8d7", "repo_name": "darkroom-working-solution-mixer", "task_type": "0-1 代码生成", "project_category": "纯前端", "language_framework": "TypeScript, React, Vite, Vitest, Playwright, Docker, Docker Compose", "summary": "暗房更换安全灯、灯距或相纸后，需要用阶梯曝光测试确定可安全操作的时长，避免凭经验判断导致材料起雾。 … 保持配液计算和容量台账行为不变，Vitest 验证阶梯生成及三种结论边界，Playwright 完成创建、评估、刷新恢复和失败输入验收，现有 web 与 verify 编排继续可用并保留 WEB_PO"} -->
## 0025-6 · darkroom-working-solution-mixer

- 创建时间：2026-09-12 15:17:19 +0800
- 项目类别：纯前端
- 任务难度：待评估
- 语言/框架：TypeScript, React, Vite, Vitest, Playwright, Docker, Docker Compose

### User Prompt

<!-- prompt-start -->
暗房更换安全灯、灯距或相纸后，需要用阶梯曝光测试确定可安全操作的时长，避免凭经验判断导致材料起雾。顶部加入“安全灯测试”入口，操作员填写测试名称、起始秒数、递增秒数和条带数量，创建后页面按曝光顺序列出各条带时长，并等待观察结果。操作员选择首条出现可见灰雾的条带并完成评估，领域服务据此把前一条时长判为安全上限；首条即起雾时显示低于起始值，全部未起雾时显示至少达到末条时长。测试草稿与已完成结论保存在独立的 localStorage 键中，刷新可恢复，非法整数、超出一小时的曝光时长、缺失观察或损坏存储应就地反馈，且不能覆盖最近一次有效数据。保持配液计算和容量台账行为不变，Vitest 验证阶梯生成及三种结论边界，Playwright 完成创建、评估、刷新恢复和失败输入验收，现有 web 与 verify 编排继续可用并保留 WEB_PORT 覆盖。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "d218dd802bff", "repo_name": "hazard-label-contrast-preflight", "task_type": "0-1 代码生成", "project_category": "纯前端", "language_framework": "TypeScript, React, Vite, Vitest, Playwright, Docker, Docker Compose", "summary": "警示牌运抵装置区后，安装人员需要按点位逐块核对实物，避免外观相近的标签装错位置，因此加入独立的现场装配核验模块。 … Vitest 用固定清单验证乱序扫码仍归入正确点位、重复与未知输入不推进状态，Playwright 从载入清单开始完成全部点位，并确认非法新清单不会覆盖已取得的核验进度。"} -->
## 0018-6 · hazard-label-contrast-preflight

- 创建时间：2026-09-12 15:27:38 +0800
- 项目类别：纯前端
- 任务难度：待评估
- 语言/框架：TypeScript, React, Vite, Vitest, Playwright, Docker, Docker Compose

### User Prompt

<!-- prompt-start -->
警示牌运抵装置区后，安装人员需要按点位逐块核对实物，避免外观相近的标签装错位置，因此加入独立的现场装配核验模块。用户粘贴包含任务编号及唯一点位码、标签码、区域名称的清单并开始核验，再依次输入扫码枪回传的标签码，系统按清单顺序匹配待装点位并展示当前目标、完成进度和剩余项。核验会话只接受清单内且尚未确认的标签码，重复扫码或未知码不改变进度并给出明确原因，清单字段非法、编码重复或任务编号为空时整批拒绝且保留上一场有效会话。领域层负责清单解析和确定性匹配，React 增加装配核验入口并用会话状态驱动进度与反馈，现有对比度、版面预检和修订比较的数据及操作互不影响，纯前端构建与 WEB_PORT 覆盖保持可用。Vitest 用固定清单验证乱序扫码仍归入正确点位、重复与未知输入不推进状态，Playwright 从载入清单开始完成全部点位，并确认非法新清单不会覆盖已取得的核验进度。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "9eefad59e984", "repo_name": "pharma-gtin-validation-gate", "task_type": "0-1 代码生成", "project_category": "纯后端", "language_framework": "Docker, Python 3.12, FastAPI, Pydantic, pytest, Docker Compose", "summary": "药品入库后需按生产批号和有效期安排上架，库管员为已有收货单提交唯一复核号、复核日期、最短可售天数及各GTIN的批号、数量和失效日期，取得货架期处置清单。 … 保持现有错误结构和路由行为，Compose继续支持API_PORT；自动化验收从建单和扫描开始，证明三类批次及排序、计划外商品复核、超量提交无残记录与创建结果一致读取。"} -->
## 0027-6 · pharma-gtin-validation-gate

- 创建时间：2026-09-12 15:33:16 +0800
- 项目类别：纯后端
- 任务难度：待评估
- 语言/框架：Docker, Python 3.12, FastAPI, Pydantic, pytest, Docker Compose

### User Prompt

<!-- prompt-start -->
药品入库后需按生产批号和有效期安排上架，库管员为已有收货单提交唯一复核号、复核日期、最短可售天数及各GTIN的批号、数量和失效日期，取得货架期处置清单。服务按自然日计算剩余天数，小于零、低于门槛和达到门槛的批次依次标记为expired、short_dated、usable，并在每个GTIN内按失效日期和批号稳定排序；批次数量合计不得超过该GTIN的已实收量，计划外但已入账的商品也可参与。POST /shelf-life-reviews 原子保存复核与批次明细并返回结果，GET /shelf-life-reviews/{review_id} 还原同一文档，所需表随现有SQLite启动过程幂等创建。复核号重复返回409，收货单或已入账GTIN不存在返回404，日期格式非法、批号重复、数量非正、门槛超出零至3650天或申报总量超出实收量返回422，失败请求不留记录。保持现有错误结构和路由行为，Compose继续支持API_PORT；自动化验收从建单和扫描开始，证明三类批次及排序、计划外商品复核、超量提交无残记录与创建结果一致读取。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "4c87b31a46b4", "repo_name": "3001-crate-cleaning-trace-platform", "task_type": "0-1 代码生成", "project_category": "全栈", "language_framework": "Node.js, React, TypeScript, Vite, Vitest, Python, FastAPI, SQLAlchemy, pytest, Docker Compose", "summary": "食品厂按库位盘点周转箱时，纸面记录无法说明盘点期间应有哪些箱，本轮建立独立盘点单，以创建时的在用箱体快照作为核对基准。 … 自动化验收覆盖正常盘点准确区分缺失与错放、备用编号计入对应主箱、盘点创建后移动箱体仍按原快照结算，以及含未知或重复编号时不完成盘点且可修正重试。"} -->
## 3001-9 · 3001-crate-cleaning-trace-platform

- 创建时间：2026-09-12 16:16:16 +0800
- 项目类别：全栈
- 任务难度：待评估
- 语言/框架：Node.js, React, TypeScript, Vite, Vitest, Python, FastAPI, SQLAlchemy, pytest, Docker Compose

### User Prompt

<!-- prompt-start -->
食品厂按库位盘点周转箱时，纸面记录无法说明盘点期间应有哪些箱，本轮建立独立盘点单，以创建时的在用箱体快照作为核对基准。操作人从盘点页选择库位并开始盘点，逐个扫描主编号或备用编号后提交，页面展示应在、实扫、缺失和错放明细，并保留已完成盘点供当次结果复看。后端增加创建与完成盘点两个契约，盘点单从进行中转为已完成，完成时统一解析编号、拒绝重复扫描，并把快照和差异结果在同一事务内固化，之后的箱体位置变更不能改写历史结论。空库位、未知编号、已停用箱和重复完成分别返回可定位的业务反馈，页面保留未提交的扫描内容；旧数据库通过增量迁移兼容，现有台账筛选、流转登记、问题处理和备用编号行为不变。自动化验收覆盖正常盘点准确区分缺失与错放、备用编号计入对应主箱、盘点创建后移动箱体仍按原快照结算，以及含未知或重复编号时不完成盘点且可修正重试。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "78a878bf1325", "repo_name": "saddle-stitch-imposition-api", "task_type": "0-1 代码生成", "project_category": "纯后端", "language_framework": "Python 3.12, FastAPI, Pydantic, pytest, Docker, Docker Compose", "summary": "装订完成后，后道人员要按报价印量生成成品装箱单，避免手工划分册号时出现重装、漏装或末箱数量错误。 … pytest 验收十六页一千册按 300 册分成 300、300、300、100 四箱，验证整除分箱、非法或超限请求不落库，并证明结果可从 SQLite 原样读回。"} -->
## 0034-6 · saddle-stitch-imposition-api

- 创建时间：2026-09-12 17:00:09 +0800
- 项目类别：纯后端
- 任务难度：待评估
- 语言/框架：Python 3.12, FastAPI, Pydantic, pytest, Docker, Docker Compose

### User Prompt

<!-- prompt-start -->
装订完成后，后道人员要按报价印量生成成品装箱单，避免手工划分册号时出现重装、漏装或末箱数量错误。操作员向 POST /packing-plans 提交报价编号和每箱容量，系统读取报价快照并从第一册连续分箱，保存装箱单编号、来源报价、容量、总箱数及各箱册号范围。装箱领域对象负责确定性分配，SQLite 仓储通过现有迁移体系保存不可变快照，GET /packing-plans/{plan_id} 供交接人员重新读取完整结果。报价或装箱单不存在时返回 404，容量不是严格正整数、请求含多余字段或箱数超过 500 时返回定位到 carton_capacity 的 422，失败不写库也不消耗编号。拼版、定位及报价各接口保持响应兼容，数据库路径和 Compose 的 API_PORT 覆盖方式不变。pytest 验收十六页一千册按 300 册分成 300、300、300、100 四箱，验证整除分箱、非法或超限请求不落库，并证明结果可从 SQLite 原样读回。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "03c68d690e3b", "repo_name": "3004-archive-box-page-audit", "task_type": "0-1 代码生成", "project_category": "纯前端", "language_framework": "Node.js, React, TypeScript, Vite, Vitest, Docker Compose", "summary": "库房人员将单件纸质档案临时交给查阅人后，目前无法在本机追踪未归还记录，请加入独立借阅台，以借阅记录作为核心对象保存档案快照、查阅人、借出时间、预计归还日和归还时间。 … 记录保留借出时的档号、题名、盒号和页码快照，因此后续编辑或删除档案仍可查阅历史，旧浏览器数据可直接加载，现有 JSON 备份恢复不导入也不清除借阅记录，并由往返测试固定这一边界。"} -->
## 3004-7 · 3004-archive-box-page-audit

- 创建时间：2026-09-12 17:17:01 +0800
- 项目类别：纯前端
- 任务难度：待评估
- 语言/框架：Node.js, React, TypeScript, Vite, Vitest, Docker Compose

### User Prompt

<!-- prompt-start -->
库房人员将单件纸质档案临时交给查阅人后，目前无法在本机追踪未归还记录，请加入独立借阅台，以借阅记录作为核心对象保存档案快照、查阅人、借出时间、预计归还日和归还时间。用户从档案清单发起借阅，填写查阅人与预计归还日后确认，服务按档案标识拒绝已有未归还记录的重复借出，并把成功记录写入新的本地存储键，界面测试应证明刷新后该档案仍显示借出中。借阅台集中展示未归还记录及历史记录，用户对未归还项执行归还后只补写归还时间，领域测试需证明重复归还不会改变原记录，档案登记、异常处置和盘点会话均不被改写。查阅人为空、日期无效或早于借出当天时在表单旁说明原因并保留输入，目标档案在确认前被删除或已被他处借出时提示重新选择，失败操作不得产生记录。记录保留借出时的档号、题名、盒号和页码快照，因此后续编辑或删除档案仍可查阅历史，旧浏览器数据可直接加载，现有 JSON 备份恢复不导入也不清除借阅记录，并由往返测试固定这一边界。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "9df3b4a8335b", "repo_name": "venue-frequency-clearance-api", "task_type": "0-1 代码生成", "project_category": "纯后端", "language_framework": "Go 1.25, Gin, testify, Docker, Docker Compose", "summary": "演出进场后，协调员用频谱仪载波记录核对设备方案，以识别漏开、误开和偏频设备，而不是重新做放行裁决。 … 验收证明乱序输入仍确定性一对一匹配、缺失与多余同时出现、MHz 与等价 kHz 结果一致，并验证非法容差的字段定位。"} -->
## 0037-7 · venue-frequency-clearance-api

- 创建时间：2026-09-12 17:31:56 +0800
- 项目类别：纯后端
- 任务难度：待评估
- 语言/框架：Go 1.25, Gin, testify, Docker, Docker Compose

### User Prompt

<!-- prompt-start -->
演出进场后，协调员用频谱仪载波记录核对设备方案，以识别漏开、误开和偏频设备，而不是重新做放行裁决。实现 POST /v1/reconcile-observations，接收 devices、1 至 500 条 observations 及 0 至 10000 的 tolerance_khz，观测项含唯一 id 和 center_khz。领域层把容差内的设备观测对按频差、设备编号、观测编号排序后依次占用，按设备编号返回 matched 或 missing，匹配项含观测编号、实测中心和有符号偏差，未占用观测按编号列为 unexpected。重复字段、非法数值、重复观测编号及容差非整数或越界使用现有错误信封定位字段，合法但未匹配的数据直接进入核对结果。保持 coordinate、check-retunes 和 healthz 响应兼容，复用设备校验与单位换算，在领域匹配、Gin 路由、README 契约及 verify 中形成闭环。验收证明乱序输入仍确定性一对一匹配、缺失与多余同时出现、MHz 与等价 kHz 结果一致，并验证非法容差的字段定位。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "1cb7ac729e3d", "repo_name": "port-laytime-adjudicator", "task_type": "0-1 代码生成", "project_category": "纯后端", "language_framework": "Python 3.12, FastAPI, SQLAlchemy, PostgreSQL, Alembic, pytest, Docker, Docker Compose", "summary": "港方交接班只留下按时发生的作业事件，结算员可提交一份事件簿及费率、允许秒数，由系统生成可回查的结算结果，避免人工先整理暂停区间。 … pytest与verify从无暂停和多次暂停两条日志验证派生区间及费用，证明非法转换原子回滚、回查内容稳定，并确认原创建、时间线、对比、航次封顶接口及API_PORT覆盖继续可用。"} -->
## 0017-8 · port-laytime-adjudicator

- 创建时间：2026-09-12 17:57:56 +0800
- 项目类别：纯后端
- 任务难度：待评估
- 语言/框架：Python 3.12, FastAPI, SQLAlchemy, PostgreSQL, Alembic, pytest, Docker, Docker Compose

### User Prompt

<!-- prompt-start -->
港方交接班只留下按时发生的作业事件，结算员可提交一份事件簿及费率、允许秒数，由系统生成可回查的结算结果，避免人工先整理暂停区间。事件簿依次包含开工、暂停、复工和完工，领域编译器以确定性状态机校验首尾、配对和严格递增时间，再把有效暂停转换为现有左闭右开区间并调用原计费规则。创建成功时在同一事务保存原始事件、派生区间、编译摘要和关联结果标识，创建响应返回完整事件簿，第二个入口可按事件簿标识回放输入与生成结果。缺少开工或完工、连续暂停、未暂停即复工、暂停后直接完工及时间倒退应返回定位到事件下标的422，编译或计费失败不能留下事件簿或结算记录。pytest与verify从无暂停和多次暂停两条日志验证派生区间及费用，证明非法转换原子回滚、回查内容稳定，并确认原创建、时间线、对比、航次封顶接口及API_PORT覆盖继续可用。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "2921269c86ed", "repo_name": "kiln-heatwork-release", "task_type": "0-1 代码生成", "project_category": "全栈", "language_framework": "Docker, Python 3.12, FastAPI, Pydantic, TypeScript, React, Vite, pytest, Vitest, Playwright", "summary": "现场更换或年检热电偶后，质检员需要留存独立的校准核验单，避免仅凭窑次曲线判断传感器是否还能投入使用。 … 自动化验收从边界等于允许偏差的合格单走到持久化详情，再以单点超差确认不合格判定，并证明非法输入和重复记录均不增加数据，浏览器联调用刷新后的详情作为闭环证据。"} -->
## 0033-7 · kiln-heatwork-release

- 创建时间：2026-09-12 19:04:43 +0800
- 项目类别：全栈
- 任务难度：待评估
- 语言/框架：Docker, Python 3.12, FastAPI, Pydantic, TypeScript, React, Vite, pytest, Vitest, Playwright

### User Prompt

<!-- prompt-start -->
现场更换或年检热电偶后，质检员需要留存独立的校准核验单，避免仅凭窑次曲线判断传感器是否还能投入使用。质检员填写探头编号、校准时间、允许偏差及三至十二组设定温度、仪表读数和标准器读数，系统计算每组示值误差与最大绝对误差，并生成合格或不合格结论。提交成功后核验单作为不可变记录保存，页面转入详情展示判定依据，刷新后可按编号重新打开；后端以创建和详情两个接口承载这条流程，并复用现有 SQLite 连接、错误响应结构和容器服务。重复探头编号与校准时间、非递增设定温度、超出零至一千四百摄氏度的读数或非正允许偏差应阻止落库，表单在对应输入处说明原因，已有窑次提交、复算和对比行为不受影响。自动化验收从边界等于允许偏差的合格单走到持久化详情，再以单点超差确认不合格判定，并证明非法输入和重复记录均不增加数据，浏览器联调用刷新后的详情作为闭环证据。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "bf0b98a4bfb4", "repo_name": "specimen-handoff-sequence-gate", "task_type": "0-1 代码生成", "project_category": "全栈", "language_framework": "Python 3.12, FastAPI, SQLAlchemy, PostgreSQL, TypeScript, React, Vite, pytest, Vitest, Playwright", "summary": "样本在实验室交接窗口连续扫码时，网络超时会诱发重复提交，稍后抵达的旧请求又可能把已确认进度推乱，操作员必须知道下一次应使用哪个序号。 … 验收时可先截断一次确认响应再重发，界面只增加一个样本；迟到或跳号请求显示期望序号，刷新页面后仍停在同一准确进度。"} -->
## 0039 · specimen-handoff-sequence-gate

- 创建时间：2026-09-12 19:10:09 +0800
- 项目类别：全栈
- 任务难度：待评估
- 语言/框架：Python 3.12, FastAPI, SQLAlchemy, PostgreSQL, TypeScript, React, Vite, pytest, Vitest, Playwright

### User Prompt

<!-- prompt-start -->
样本在实验室交接窗口连续扫码时，网络超时会诱发重复提交，稍后抵达的旧请求又可能把已确认进度推乱，操作员必须知道下一次应使用哪个序号。从空仓库实现 React、TypeScript、Vite 前端与 Python 3.12、FastAPI API：先创建包含二至二十个唯一样本条码的交接批次，再逐条提交条码和从 1 开始的客户端整数序号。服务端只接受当前期望序号；相同序号与相同条码的重试须返回原确认且不重复计数，低序号内容不一致或高序号请求均返回当前期望序号并保持状态不变。条码必须属于创建时冻结的批次且每个只能接收一次，全部接收后状态变为完成，此后不再接受新扫描。批次、确认记录和期望序号持久化到 PostgreSQL，同一批次的并发请求必须原子裁决。Docker Compose 运行 web 与 api，WEB_PORT、API_PORT 可覆盖宿主端口，并包含名为 verify 的一次性验收服务；页面真实联调 API，超时后允许以原序号重试。验收时可先截断一次确认响应再重发，界面只增加一个样本；迟到或跳号请求显示期望序号，刷新页面后仍停在同一准确进度。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "22ec98088296", "repo_name": "tunnel-profile-clearance-api", "task_type": "0-1 代码生成", "project_category": "纯后端", "language_framework": "Python 3.12, FastAPI, Pydantic, pytest, Docker, Docker Compose", "summary": "隧道激光测量只导出了二维断面折线，复核人员需要在不依赖CAD软件的情况下判断车辆限界是否满足指定净距，并定位最危险的一对边。 … 禁止几何库代算或占位结果，最终响应应稳定呈现通过结论、最小净距和唯一危险线段对。"} -->
## 0040 · tunnel-profile-clearance-api

- 创建时间：2026-09-12 19:12:32 +0800
- 项目类别：纯后端
- 任务难度：待评估
- 语言/框架：Python 3.12, FastAPI, Pydantic, pytest, Docker, Docker Compose

### User Prompt

<!-- prompt-start -->
隧道激光测量只导出了二维断面折线，复核人员需要在不依赖CAD软件的情况下判断车辆限界是否满足指定净距，并定位最危险的一对边。请从空仓库实现纯后端JSON API，采用Python 3.12、FastAPI与Pydantic；README给出毫米坐标示例，pytest验证几何边界，Docker Compose提供api和名为verify的一次性验收服务，宿主端口由API_PORT覆盖，并给出字段级错误反馈及.gitignore。输入包含按顺序连接但不闭合的隧道折线、首尾隐式闭合的车辆限界多边形及整数要求净距，坐标和净距均为毫米整数且绝对值不超过一百万；各相邻点不得相同，多边形不得自交，输入不得重复首点作为末点。自行实现线段相交与点到线段距离，计算两组线段间的全局最小欧氏距离；相交或接触的距离为0，内部计算使用双精度，响应距离统一四舍五入到小数点后三位。仅当未相交且未舍入的最小距离大于等于要求净距时通过；并列距离差不超过1e-9时，依次选择隧道线段起点索引、限界边起点索引较小者。禁止几何库代算或占位结果，最终响应应稳定呈现通过结论、最小净距和唯一危险线段对。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "b3de788fb52c", "repo_name": "interval-energy-apportionment-api", "task_type": "0-1 代码生成", "project_category": "纯后端", "language_framework": "Python 3.12, FastAPI, Pydantic, pytest, Docker, Docker Compose", "summary": "园区月底常出现总表增量与各支路分时电量之和不一致，结算员必须把这段表差精确落到既有区间，既不能因浮点误差丢失最小单位，也不能让并列余数产生随机结果。 … 最终响应按区间编号排序，明确给出原始表差、每段分摊值及分摊后校核和，使同一结算请求始终得到可逐项复算的唯一结果。"} -->
## 0041 · interval-energy-apportionment-api

- 创建时间：2026-09-12 19:43:34 +0800
- 项目类别：纯后端
- 任务难度：待评估
- 语言/框架：Python 3.12, FastAPI, Pydantic, pytest, Docker, Docker Compose

### User Prompt

<!-- prompt-start -->
园区月底常出现总表增量与各支路分时电量之和不一致，结算员必须把这段表差精确落到既有区间，既不能因浮点误差丢失最小单位，也不能让并列余数产生随机结果。请从空仓库实现纯后端 JSON API，使用 Python 3.12、FastAPI 与 Pydantic；输入总表起止读数以及各支路按区间汇总的电量，所有电量最多三位小数且以 0.001 kWh 为最小单位。先求总表增量减支路总和所得表差，再按每个区间全部支路电量绝对值占比分摊；权重总和为零且表差非零时拒绝。自行实现定点数最大余数法：先向零截断各区间份额，剩余最小单位按余数绝对值降序补齐，并列时按区间编号字典序升序；负表差采用同一顺序补负单位。区间集合不一致、读数倒退或精度越界须返回可定位字段的错误且不产生部分结果。仓库提供 Dockerfile、端口由 API_PORT 覆盖的 compose.yaml，以及名为 verify 的一次性验收服务；pytest 应分别固定分摊总和、并列顺序和负差额边界，禁止用浮点近似、假接口或固定响应代替计算。最终响应按区间编号排序，明确给出原始表差、每段分摊值及分摊后校核和，使同一结算请求始终得到可逐项复算的唯一结果。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "89b8b71cebfb", "repo_name": "seismic-trace-resumable-ingest", "task_type": "0-1 代码生成", "project_category": "纯后端", "language_framework": "Python 3.12, FastAPI, SQLAlchemy, PostgreSQL, pytest, Docker, Docker Compose", "summary": "野外地震仪通过不稳定链路回传单个记录包时，连接中断不能迫使工程师从头上传，也不能让重试覆盖已确认字节。 … 仅当累计长度等于总长且重算整包 SHA-256 相符才能封存，封存后拒绝不同内容；整包摘要不符则会话进入不可续传的失败终态，工程师须用正确元数据新建会话，最终只能观察到摘要和长度一致的封存记录。"} -->
## 0044 · seismic-trace-resumable-ingest

- 创建时间：2026-09-12 20:02:05 +0800
- 项目类别：纯后端
- 任务难度：待评估
- 语言/框架：Python 3.12, FastAPI, SQLAlchemy, PostgreSQL, pytest, Docker, Docker Compose

### User Prompt

<!-- prompt-start -->
野外地震仪通过不稳定链路回传单个记录包时，连接中断不能迫使工程师从头上传，也不能让重试覆盖已确认字节。请从空仓库实现纯后端服务：创建会话时登记总字节数与整包 SHA-256，只接受携带起始偏移、字节数和该块 SHA-256 的二进制分块；新块必须从当前 confirmed_offset 开始且不得越过总长。已确认范围内内容完全相同的分块重发应幂等成功，其他旧偏移一律返回当前期望偏移。采用 Python 3.12、FastAPI、SQLAlchemy 与 PostgreSQL，检查点和分块内容必须持久化，使 API 进程重启后可查询并续传；仓库用 Docker Compose 编排，宿主端口由 API_PORT 覆盖，并提供名为 verify 的一次性 pytest 验收服务，错误需定位到偏移或摘要，禁止占位实现。仅当累计长度等于总长且重算整包 SHA-256 相符才能封存，封存后拒绝不同内容；整包摘要不符则会话进入不可续传的失败终态，工程师须用正确元数据新建会话，最终只能观察到摘要和长度一致的封存记录。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "e817c746e4b2", "repo_name": "fiber-route-ambiguity-tracer", "task_type": "0-1 代码生成", "project_category": "纯前端", "language_framework": "TypeScript, React, Vite, Vitest, Playwright, Docker, Docker Compose", "summary": "临时展会布线结束后，同色跳纤穿过多个转接箱，场馆弱电布线工程师需要确认两端之间究竟无路、仅有一条通路，还是存在会让切换不确定的多条路径。 … 使用 Docker Compose 运行应用，宿主端口可由 WEB_PORT 覆盖，并提供名为 verify 的一次性验收服务。"} -->
## 0045 · fiber-route-ambiguity-tracer

- 创建时间：2026-09-12 20:21:13 +0800
- 项目类别：纯前端
- 任务难度：待评估
- 语言/框架：TypeScript, React, Vite, Vitest, Playwright, Docker, Docker Compose

### User Prompt

<!-- prompt-start -->
临时展会布线结束后，同色跳纤穿过多个转接箱，场馆弱电布线工程师需要确认两端之间究竟无路、仅有一条通路，还是存在会让切换不确定的多条路径。请从空仓库起步，使用 TypeScript、React、Vite 实现纯前端单页应用，通过表单建立最多 30 个端点和 60 条无向连接边，并选择两个不同端点核查。端点标识限 1 至 20 位 ASCII 字母、数字或连字符且区分大小写；录入控件只能从已有端点选择边的两端，禁用自环和已存在的无向端点对，使用户无法构造未知引用或重复边。核查时路径不得重复经过端点：零条显示“无路”，一条显示“唯一通路”及完整端点顺序，搜索到两条即可停止并显示“存在歧义”及两条证据路径；邻接端点按标识的 ASCII 码位升序搜索，起点和终点固定。任何拓扑编辑都应立即清除旧结论，删除关键连接后重新核查可观察到歧义转为唯一通路或无路。使用 Vitest 验证路径判定，Playwright 覆盖建图、核查、编辑失效与结果变化，禁止固定结果或假接口。使用 Docker Compose 运行应用，宿主端口可由 WEB_PORT 覆盖，并提供名为 verify 的一次性验收服务。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "005ee8ad6f86", "repo_name": "spoken-clip-calibrator", "task_type": "0-1 代码生成", "project_category": "纯前端", "language_framework": "TypeScript, React, Vite, HTMLMediaElement, Vitest, Playwright, Docker, Docker Compose", "summary": "口述史整理员边听本地录音边按键记录可引用片段时，拖动进度条和显示精度差异常使试听范围与导出的时间码不一致。 … 不可解码文件、空标签、相等或反向边界须就地报错且不改变清单，禁止固定时间码、假播放状态或未实现按钮；验收者最终能看到试听游标回到精确起点，并从下载内容复算每个片段。"} -->
## 0048 · spoken-clip-calibrator

- 创建时间：2026-09-12 21:12:21 +0800
- 项目类别：纯前端
- 任务难度：待评估
- 语言/框架：TypeScript, React, Vite, HTMLMediaElement, Vitest, Playwright, Docker, Docker Compose

### User Prompt

<!-- prompt-start -->
口述史整理员边听本地录音边按键记录可引用片段时，拖动进度条和显示精度差异常使试听范围与导出的时间码不一致。请从空仓库实现纯前端片段切取校准器，使用 TypeScript、React、Vite 与 HTMLMediaElement，只读取用户选择的本地音频，不上传文件或访问在线服务。播放中可分别捕获起点、终点并填写非空标签，时间取媒体 currentTime 乘以 1000 后按四舍五入得到整数毫秒；仅当 0≤起点＜终点≤音频时长的同样毫秒值时才能加入。片段清单允许选择、删除和循环试听；试听必须从记录起点开始，在首次观测到当前时间达到或越过终点时暂停并回到起点，不得擅自吸附到整秒。导出 JSON 按起点、终点、创建序号依次升序排列，包含音频文件名、时长毫秒、标签和边界。Docker Compose 启动页面，宿主端口可由 WEB_PORT 覆盖，并提供名为 verify 的一次性验收服务；Vitest 校验取整与边界，Playwright 使用仓库内短音频走通打点、试听和导出。不可解码文件、空标签、相等或反向边界须就地报错且不改变清单，禁止固定时间码、假播放状态或未实现按钮；验收者最终能看到试听游标回到精确起点，并从下载内容复算每个片段。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "eb5fba9d366a", "repo_name": "wheel-rim-defect-segmenter", "task_type": "0-1 代码生成", "project_category": "全栈", "language_framework": "Python 3.12, FastAPI, Pydantic, TypeScript, React, Vite, pytest, Vitest, Playwright, Docker", "summary": "轮对超声复核时，检测仪导出的环形读数常把跨越零度的同一处损伤拆成两段，检修员因而可能重复计数。 … 跨零度样例最终只显示一个连续区段，其跨度与峰值角可由原始读数逐项复算。"} -->
## 0049 · wheel-rim-defect-segmenter

- 创建时间：2026-09-12 21:14:04 +0800
- 项目类别：全栈
- 任务难度：待评估
- 语言/框架：Python 3.12, FastAPI, Pydantic, TypeScript, React, Vite, pytest, Vitest, Playwright, Docker

### User Prompt

<!-- prompt-start -->
轮对超声复核时，检测仪导出的环形读数常把跨越零度的同一处损伤拆成两段，检修员因而可能重复计数。请从空仓库实现一套真实联调的判读台：React 页面提交 JSON，FastAPI 接收恰好 360 条采样；每条角度必须是互不重复的整数 0 至 359，幅值为非负毫米数，并由用户给出非负阈值。使用 Python 3.12、TypeScript 与 Vite，在核心单元测试中固定环形边界；Docker Compose 的宿主端口由 WEB_PORT、API_PORT 覆盖，仓库提供名为 verify 的一次性验收服务。幅值大于或等于阈值即为缺陷点，相邻整数角度属于同段，359 与 0 也相邻；全圆超限时唯一结果为起点 0、终点 359、跨度 360。其他区段以顺时针遇到的首个缺陷角为起点，跨度按包含的采样点数计算，峰值取段内最大幅值，峰值并列取最小角度。页面须画出可辨认的 360 度采样环，点击结果区段能突出对应角点；缺失、重复、越界或非法幅值应定位字段并清空本次结果，禁止用固定判读替代计算。跨零度样例最终只显示一个连续区段，其跨度与峰值角可由原始读数逐项复算。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "eadb4b06654d", "repo_name": "weighbridge-telegram-normalizer", "task_type": "0-1 代码生成", "project_category": "纯后端", "language_framework": "Go 1.25, Gin, testify, Docker, Docker Compose", "summary": "散货码头从旧地磅导出的电文常被不同系统按字符而非字节切割，计量复核员需要确认规范化记录确实来自同一份原始数据。 … 使用 Go 1.25、Gin 与 testify，并以 Docker Compose 启动 API；宿主端口可由 API_PORT 覆盖，仓库提供名为 verify 的一次性验收服务及覆盖合法解析、边"} -->
## 0050 · weighbridge-telegram-normalizer

- 创建时间：2026-09-13 05:56:14 +0800
- 项目类别：纯后端
- 任务难度：待评估
- 语言/框架：Go 1.25, Gin, testify, Docker, Docker Compose

### User Prompt

<!-- prompt-start -->
散货码头从旧地磅导出的电文常被不同系统按字符而非字节切割，计量复核员需要确认规范化记录确实来自同一份原始数据。请从空仓库实现纯后端 HTTP API，接收不超过 10000 行的 ASCII 文件；每行正文必须恰为 37 字节，格式为 YYYYMMDDhhmmss|SSSSSSSS|sdddddddd|UUU，行尾统一为 LF，末行也必须有 LF。时间须为真实有效的公历日期时间，设备码只能含大写字母或数字，s 为正负号，八位数字表示整数克，UUU 仅允许 KGM。合法响应保持输入顺序，返回时间、设备码、原值、整数克值及原文件 SHA-256。解析遇到首个错误即整包失败，不得返回部分记录；错误响应给出一基行号、零基字节位置和原因：字段内容非法取该字段首个违规字节，公历时间无效取 0，短行取实际长度，长行取 37，缺少末尾 LF 取 37。相同字节输入必须产生相同摘要和逐行结果。使用 Go 1.25、Gin 与 testify，并以 Docker Compose 启动 API；宿主端口可由 API_PORT 覆盖，仓库提供名为 verify 的一次性验收服务及覆盖合法解析、边界错误和确定性的测试。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "e5ce4637183b", "repo_name": "wheel-rim-defect-segmenter", "task_type": "0-1 代码生成", "project_category": "全栈", "language_framework": "Python 3.12, FastAPI, Pydantic, TypeScript, React, Vite, pytest, Vitest, Playwright, Docker", "summary": "现场更换探头或耦合剂后，检修员要先用已知厚度的参考试块校准声程，本次从零建立独立的声程校准记录模块，不借用缺陷区段或基线补偿对象。 … 用后端单元与接口测试锁定精确直线、单点超差和退化输入，再以一个 Playwright 流程完成录入、提交及不合格点高亮；原判读接口和页面仍可使用，Compose 的 API_PORT、WEB_PORT"} -->
## 0049-3 · wheel-rim-defect-segmenter

- 创建时间：2026-09-13 06:41:43 +0800
- 项目类别：全栈
- 任务难度：待评估
- 语言/框架：Python 3.12, FastAPI, Pydantic, TypeScript, React, Vite, pytest, Vitest, Playwright, Docker

### User Prompt

<!-- prompt-start -->
现场更换探头或耦合剂后，检修员要先用已知厚度的参考试块校准声程，本次从零建立独立的声程校准记录模块，不借用缺陷区段或基线补偿对象。校准入口接收记录名称、三至八个厚度与往返时间测点及允许残差，提交后由 FastAPI 返回拟合声速、零点偏移、各点预测时间和残差，并把记录评定为未评定、合格或不合格。核心按普通最小二乘拟合直线，所有厚度和时间应为正有限数且厚度互异，测点不足、重复厚度、退化斜率或非法容差要定位到具体字段，失败后页面清除旧曲线与结论。React 在现有判读台增加“声程校准”入口，以表格编辑测点并绘制实测点和拟合线，结果区突出最大绝对残差点，让检修员能从返回明细复算结论。用后端单元与接口测试锁定精确直线、单点超差和退化输入，再以一个 Playwright 流程完成录入、提交及不合格点高亮；原判读接口和页面仍可使用，Compose 的 API_PORT、WEB_PORT 覆盖方式保持有效。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "91cb10c81470", "repo_name": "gallery-light-cue-rehearsal", "task_type": "0-1 代码生成", "project_category": "纯前端", "language_framework": "TypeScript, React, Vite, Vitest, Playwright, Docker, Docker Compose", "summary": "展厅闭馆后的灯光联排常因浏览器标签页降频而跳慢，技术员需要确认一串提示在暂停、恢复和回调延迟后仍落在正确时间线上。 … 最后一项到期后界面稳定显示“已完成”、全部提示的计划截止时间及实际处理时间，使验收者能看到一次长延迟虽造成集中处理，却没有延长整段演练。"} -->
## 0052 · gallery-light-cue-rehearsal

- 创建时间：2026-09-13 08:46:01 +0800
- 项目类别：纯前端
- 任务难度：待评估
- 语言/框架：TypeScript, React, Vite, Vitest, Playwright, Docker, Docker Compose

### User Prompt

<!-- prompt-start -->
展厅闭馆后的灯光联排常因浏览器标签页降频而跳慢，技术员需要确认一串提示在暂停、恢复和回调延迟后仍落在正确时间线上。请使用 TypeScript、React 与 Vite 从空仓库实现纯前端演练器；Docker Compose 启动页面，宿主端口由 WEB_PORT 覆盖，并提供名为 verify 的一次性验收服务，Vitest 与 Playwright 覆盖计时链路，README 在此处说明启动和 JSON 格式，禁止固定轨迹或占位按钮。页面导入 UTF-8 JSON 数组，每项只能含唯一 id、非空 label 和整数 durationMs，durationMs 范围为 100 至 600000；任一项非法则整份拒绝且不得替换当前有效数据。启动后以数组顺序执行，使用可测试的单调时钟计算截止时刻，不得靠递减 tick 累计；暂停只冻结当前项剩余毫秒数，恢复以该余量建立新截止时刻。延迟回调若跨过多项，须按各自截止时刻依次记入轨迹并直接显示当前应执行项；未载入时启动、状态不符的重复操作均就地报错且不改变状态。最后一项到期后界面稳定显示“已完成”、全部提示的计划截止时间及实际处理时间，使验收者能看到一次长延迟虽造成集中处理，却没有延长整段演练。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "e635b888de57", "repo_name": "tunnel-profile-clearance-api", "task_type": "0-1 代码生成", "project_category": "纯后端", "language_framework": "Python 3.12, FastAPI, Pydantic, pytest, Docker, Docker Compose", "summary": "激光测量复核人员需要把同一隧道断面的本期测点与基准测点对齐，排除仪器整体平移后识别真正发生位移的位置，形成可保存的断面变化报告。 … 保持原有两个净距接口、健康检查和API_PORT编排行为不变，在模型、独立比较服务、FastAPI路由及pytest与验收脚本间打通契约，端到端验证纯整体平移全部合格、单点真实位移被定位、并列选择稳定"} -->
## 0040-3 · tunnel-profile-clearance-api

- 创建时间：2026-09-13 09:28:01 +0800
- 项目类别：纯后端
- 任务难度：待评估
- 语言/框架：Python 3.12, FastAPI, Pydantic, pytest, Docker, Docker Compose

### User Prompt

<!-- prompt-start -->
激光测量复核人员需要把同一隧道断面的本期测点与基准测点对齐，排除仪器整体平移后识别真正发生位移的位置，形成可保存的断面变化报告。实现POST /api/profiles/compare，请求接收两组按唯一测点名称对应的毫米整数坐标、一个共同存在的基准点名称和位移容差，响应给出对齐修正量、逐点修正后坐标与位移、最大位移测点、超限名称列表及整体是否合格。比较层以两组基准点的坐标差平移全部本期测点，再用未舍入欧氏距离判断容差，输出距离按现有规则保留三位小数，最大值并列时选择输入顺序靠前的测点。两组名称顺序或数量不一致、名称重复、基准点缺失、容差为负以及修正后坐标越界均返回现有422错误信封，定位到具体列表项或字段，任何校验失败都不生成部分报告。保持原有两个净距接口、健康检查和API_PORT编排行为不变，在模型、独立比较服务、FastAPI路由及pytest与验收脚本间打通契约，端到端验证纯整体平移全部合格、单点真实位移被定位、并列选择稳定和名称不匹配被拒绝。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "dc09c08ddcd3", "repo_name": "seismic-trace-resumable-ingest", "task_type": "0-1 代码生成", "project_category": "纯后端", "language_framework": "Python 3.12, FastAPI, SQLAlchemy, PostgreSQL, pytest, Docker, Docker Compose", "summary": "工程师在交付同一测线的两份封存记录包前，需要定位内容是否一致及首个差异字节，而不是下载整包后自行比对。 … pytest 通过不同分块边界的相同内容、共同前缀后的单字节差异、前缀相同但长度不同三类记录核对结论和偏移，并验证非封存请求无写入、查询结果经压实与 API 重启仍稳定，Compose 继续支持 AP"} -->
## 0044-7 · seismic-trace-resumable-ingest

- 创建时间：2026-09-13 11:23:25 +0800
- 项目类别：纯后端
- 任务难度：待评估
- 语言/框架：Python 3.12, FastAPI, SQLAlchemy, PostgreSQL, pytest, Docker, Docker Compose

### User Prompt

<!-- prompt-start -->
工程师在交付同一测线的两份封存记录包前，需要定位内容是否一致及首个差异字节，而不是下载整包后自行比对。引入不可变的比对记录，提交基准会话与候选会话后，同步校验两份归档的分块连续性、块摘要和整包摘要，再按偏移流式比较并持久化双方长度与摘要快照、相同前缀长度、首个差异偏移及一致、内容不同或长度不同的结论。创建接口返回 201 和完整结果，查询接口凭比对标识重取同一记录；结果应跨进程重启保留，任一记录后来压实也不能改变已保存的快照。只有两端均已封存才执行，未知会话按现有未找到错误反馈，活动或失败会话返回能指出基准端或候选端及其状态的冲突，归档校验异常不得留下比对记录，现有上传、封存、压实、审计和下载契约保持兼容。pytest 通过不同分块边界的相同内容、共同前缀后的单字节差异、前缀相同但长度不同三类记录核对结论和偏移，并验证非封存请求无写入、查询结果经压实与 API 重启仍稳定，Compose 继续支持 API_PORT 覆盖。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "4dae4d035660", "repo_name": "spoken-clip-calibrator", "task_type": "0-1 代码生成", "project_category": "纯前端", "language_framework": "TypeScript, React, Vite, HTMLMediaElement, Vitest, Playwright, Docker, Docker Compose", "summary": "整理员面对较长口述录音时缺少全局声量参照，请实现本地音频振幅概览模块；音频载入后自动生成独立的 AudioEnvelope 对象，呈现整段峰值轮廓，点击轮廓可将共享播放器定位到对应整数毫秒。 … Vitest 以合成多声道采样验证分桶、归一化、静音和位置换算，Playwright 载入仓库短音频后确认轮廓就绪、点击可预测位置并继续捕获片段，再模拟分析失败证明原清单不变，npm run veri"} -->
## 0048-6 · spoken-clip-calibrator

- 创建时间：2026-09-13 12:30:11 +0800
- 项目类别：纯前端
- 任务难度：待评估
- 语言/框架：TypeScript, React, Vite, HTMLMediaElement, Vitest, Playwright, Docker, Docker Compose

### User Prompt

<!-- prompt-start -->
整理员面对较长口述录音时缺少全局声量参照，请实现本地音频振幅概览模块；音频载入后自动生成独立的 AudioEnvelope 对象，呈现整段峰值轮廓，点击轮廓可将共享播放器定位到对应整数毫秒。分析服务接收 File 和目标桶数，以浏览器解码后的各声道绝对峰值按时长等分，返回 durationMs、bucketCount 与 0 至 1 的 peaks，结果不依赖画布尺寸或播放进度。React 用分析中、可用、失败三态管理可访问的 canvas 和时间提示，点击位置按横向比例换算并钳制到音频时长，定位前结束正在进行的单条或顺序审听。更换音频应清除旧概览并忽略迟到结果，分析失败只在概览区说明原因，不清空片段、选择或已加载音频；打点、校准、导入导出与播放边界保持兼容，全程不联网，Compose 继续支持 WEB_PORT。Vitest 以合成多声道采样验证分桶、归一化、静音和位置换算，Playwright 载入仓库短音频后确认轮廓就绪、点击可预测位置并继续捕获片段，再模拟分析失败证明原清单不变，npm run verify 可完成验收。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "3f2e7e282433", "repo_name": "tunnel-profile-clearance-api", "task_type": "0-1 代码生成", "project_category": "纯后端", "language_framework": "Python 3.12, FastAPI, Pydantic, pytest, Docker, Docker Compose", "summary": "隧道激光采集结束后，复核人员要确认设计指定的拱顶、侧墙和设备邻近控制点均被测量轨迹有效覆盖，避免断面净距计算建立在缺失区域上。 … 改动贯通请求响应模型、独立覆盖分析服务、FastAPI路由以及pytest与一次性验收脚本，端到端验证全部覆盖、首个遗漏仍返回完整结果、线段并列选择稳定和无效折线拒绝，并确认既有四个业务接口及API_"} -->
## 0040-6 · tunnel-profile-clearance-api

- 创建时间：2026-09-13 12:52:09 +0800
- 项目类别：纯后端
- 任务难度：待评估
- 语言/框架：Python 3.12, FastAPI, Pydantic, pytest, Docker, Docker Compose

### User Prompt

<!-- prompt-start -->
隧道激光采集结束后，复核人员要确认设计指定的拱顶、侧墙和设备邻近控制点均被测量轨迹有效覆盖，避免断面净距计算建立在缺失区域上。实现POST /api/profiles/coverage，请求接收按序连接且不闭合的测量折线、带唯一名称的控制点列表和非负覆盖半径，响应按输入顺序给出每个控制点到折线的最短距离、最近线段起点索引、是否覆盖，并汇总首个未覆盖名称与整体结论。覆盖服务复用点到线段距离基础能力，以未舍入距离判断半径边界，输出按现有三位小数规则处理，多个线段距离相差不超过1e-9时选择起点索引较小者。测量折线少于两点或含相邻重合点、控制点名称为空白或重复、列表为空及半径非法时返回现有422错误信封并定位具体字段，任何错误都不生成部分报告。改动贯通请求响应模型、独立覆盖分析服务、FastAPI路由以及pytest与一次性验收脚本，端到端验证全部覆盖、首个遗漏仍返回完整结果、线段并列选择稳定和无效折线拒绝，并确认既有四个业务接口及API_PORT编排行为保持兼容。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "e3e3322a9aed", "repo_name": "interval-energy-apportionment-api", "task_type": "0-1 代码生成", "project_category": "纯后端", "language_framework": "Python 3.12, FastAPI, Pydantic, pytest, Docker, Docker Compose", "summary": "园区采集系统只保存电表累计示值，遇到计数器回零时，结算员目前无法可靠还原相邻采样点的区间电量，也无法判断一次下降是合法翻转还是坏数据。 … 通过新的请求响应模型、序列推导服务和路由形成闭环，pytest 固定普通与翻转混合序列的逐段结果、方向矛盾的定位反馈及边界示值，verify 用一组跨量程样本复算总量并确认旧分摊调用仍可用。"} -->
## 0041-6 · interval-energy-apportionment-api

- 创建时间：2026-09-13 12:53:58 +0800
- 项目类别：纯后端
- 任务难度：待评估
- 语言/框架：Python 3.12, FastAPI, Pydantic, pytest, Docker, Docker Compose

### User Prompt

<!-- prompt-start -->
园区采集系统只保存电表累计示值，遇到计数器回零时，结算员目前无法可靠还原相邻采样点的区间电量，也无法判断一次下降是合法翻转还是坏数据。请建立独立的电表示值序列模块，结算员提交电表编号、三位小数的量程上限和按时间排列的采样点，并为每段声明普通递增或量程翻转，系统返回按起止时间排序的精确区间电量及整段合计。推导服务以最小电量单位做整数计算，普通段取后值减前值，翻转段取量程上限减前值再加后值，结果携带段类型并保证各段之和与总量一致。时间重复或逆序、示值超出量程、段类型与示值方向矛盾时，整次请求返回定位到具体采样点或段类型的错误，不产生部分区间；原有分摊接口、响应结构和容器端口配置不受影响。通过新的请求响应模型、序列推导服务和路由形成闭环，pytest 固定普通与翻转混合序列的逐段结果、方向矛盾的定位反馈及边界示值，verify 用一组跨量程样本复算总量并确认旧分摊调用仍可用。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "7ab99798f60b", "repo_name": "film-edgecode-converter", "task_type": "0-1 代码生成", "project_category": "纯前端", "language_framework": "TypeScript, React, Vite, Vitest, Playwright, Docker, Docker Compose", "summary": "电影资料馆交接胶片扫描片段时，边码中的英尺和帧位常被误当作普通十进制数，跨英尺定位便会偏离实际画格。 … 格式错误、越界或负偏移须定位到字段并保留上一份有效定位单，成功时同时展示规范化目标边码、总偏移帧数和可复制的交接文本，使跨英尺样例能够逐帧复算。"} -->
## 0055 · film-edgecode-converter

- 创建时间：2026-09-13 13:57:21 +0800
- 项目类别：纯前端
- 任务难度：待评估
- 语言/框架：TypeScript, React, Vite, Vitest, Playwright, Docker, Docker Compose

### User Prompt

<!-- prompt-start -->
电影资料馆交接胶片扫描片段时，边码中的英尺和帧位常被误当作普通十进制数，跨英尺定位便会偏离实际画格。请从空仓库实现纯前端换算工作台，让编目员选择 35mm 四齿孔或三齿孔制式，输入起始边码后，可用目标边码求相对帧数，也可用非负相对帧数反算目标边码。项目采用 TypeScript、React 与 Vite；Docker Compose 仅承载前端且宿主端口由 WEB_PORT 覆盖，仓库提供名为 verify 的一次性验收服务，Vitest 覆盖进位边界，Playwright 贯通录入与复制，README 随换算规则说明运行方式，不得调用在线服务或返回固定结果。边码格式固定为卷号-英尺+帧位，其中卷号为四位数字、英尺为六位数字；四齿孔每英尺 16 帧，合法帧位为 00 至 15，三齿孔每英尺 21 帧，合法帧位为 00 至 20。两端卷号必须相同，目标不得早于起点，所有运算使用整数且不作舍入；切换制式立即清除旧结果，但保留当前输入供修正。格式错误、越界或负偏移须定位到字段并保留上一份有效定位单，成功时同时展示规范化目标边码、总偏移帧数和可复制的交接文本，使跨英尺样例能够逐帧复算。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "65ac3de0597d", "repo_name": "subtitle-cue-tap-aligner", "task_type": "0-1 代码生成", "project_category": "全栈", "language_framework": "Python 3.12, FastAPI, Pydantic, TypeScript, React, Vite, pytest, Vitest, Playwright, Docker", "summary": "字幕联排时，操作员的敲击可能同时靠近两条计划提示，人工选择会让同一场记录出现不同配对。 … 通过 Docker Compose 启动 Web 与 API，WEB_PORT、API_PORT 可覆盖宿主端口，并提供名为 verify 的一次性验收服务。"} -->
## 0056 · subtitle-cue-tap-aligner

- 创建时间：2026-09-13 14:00:00 +0800
- 项目类别：全栈
- 任务难度：待评估
- 语言/框架：Python 3.12, FastAPI, Pydantic, TypeScript, React, Vite, pytest, Vitest, Playwright, Docker

### User Prompt

<!-- prompt-start -->
字幕联排时，操作员的敲击可能同时靠近两条计划提示，人工选择会让同一场记录出现不同配对。请从空仓库起步，建立供剧场字幕操作员使用的全栈对点台。React 页面接收计划时间表，每行格式为“字幕文本|整数毫秒”；字幕不得为空，时间不得为负，且各行时间必须严格递增、不重复，错误需定位到具体行，非法导入不得覆盖上一份有效结果。开始联排后，以浏览器单调时钟记录每次敲击相对首击的整数毫秒，并提交 FastAPI。服务按敲击时间升序处理，每条计划和敲击至多使用一次，只考虑绝对偏差不超过 800 毫秒的计划；选择绝对偏差最小者，若相同则选择时间较早者。页面逐行连接已配对的计划与敲击，显示原始时间及带符号偏差，并分别列出所有未配对计划和敲击，不再增加独立的合格阈值或分类裁决。采用 Python 3.12、FastAPI、Pydantic、TypeScript、React 与 Vite，使用 pytest、Vitest、Playwright 覆盖解析、配对及页面联调，禁止写死结果。通过 Docker Compose 启动 Web 与 API，WEB_PORT、API_PORT 可覆盖宿主端口，并提供名为 verify 的一次性验收服务。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "7215d9630f0e", "repo_name": "silo-fumigation-exposure-api", "task_type": "0-1 代码生成", "project_category": "纯后端", "language_framework": "Python 3.12, FastAPI, Pydantic, pytest, Docker, Docker Compose", "summary": "粮仓熏蒸结束后，采样仪只留下不等间隔的磷化氢浓度读数，复核员需要判断是否存在一段连续且足够长的有效暴露，不能把分离的短时达标区间相加。 … 成功响应列出全部有效区间、最长持续毫秒数，并仅以最长持续毫秒数是否不小于最低持续秒数乘以 1000 作出合格结论；非法输入返回字段级定位且不产生判定。"} -->
## 0057 · silo-fumigation-exposure-api

- 创建时间：2026-09-13 14:31:58 +0800
- 项目类别：纯后端
- 任务难度：待评估
- 语言/框架：Python 3.12, FastAPI, Pydantic, pytest, Docker, Docker Compose

### User Prompt

<!-- prompt-start -->
粮仓熏蒸结束后，采样仪只留下不等间隔的磷化氢浓度读数，复核员需要判断是否存在一段连续且足够长的有效暴露，不能把分离的短时达标区间相加。请从空仓库实现纯后端 JSON API，使用 Python 3.12、FastAPI、Pydantic 和 pytest，并配置 Docker Compose；宿主端口由 API_PORT 覆盖，提供名为 verify 的一次性验收服务。请求包含仓号、目标阈值 ppm、最低持续秒数和按时间排列的读数；最低持续秒数必须为大于零的有限数且最多三位小数，浓度与阈值必须为非负有限数。时间戳必须是以 Z 结尾、精确到毫秒的 ISO 8601 UTC 格式，至少两条且严格递增，采样首尾跨度不得短于最低持续时间。相邻读数间按直线插值，浓度等于阈值计入有效区间；穿越时刻换算为 Unix 毫秒后四舍五入，恰为半毫秒时向远离零方向取整。原始端点直接使用其毫秒值，区间持续毫秒数为右端点减左端点；首尾均闭合，相接区间合并，分离区间不得累计。实现领域契约、独立计算器及路由错误映射，拒绝占位或固定结果。成功响应列出全部有效区间、最长持续毫秒数，并仅以最长持续毫秒数是否不小于最低持续秒数乘以 1000 作出合格结论；非法输入返回字段级定位且不产生判定。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "82410d01a002", "repo_name": "incubator-label-print-calibrator", "task_type": "0-1 代码生成", "project_category": "纯前端", "language_framework": "TypeScript, React, Vite, CSS Paged Media, Vitest, Playwright, Docker, Docker Compose", "summary": "恒温箱样本牌若在屏幕完整、打印后却裁掉批次或到期日，接收员便无法安全放行样本。 … 不得联网、伪造状态或保留未实现按钮，Vitest 覆盖规则，Playwright 验证预览与打印样"} -->
## 0058 · incubator-label-print-calibrator

- 创建时间：2026-09-13 16:36:17 +0800
- 项目类别：纯前端
- 任务难度：待评估
- 语言/框架：TypeScript, React, Vite, CSS Paged Media, Vitest, Playwright, Docker, Docker Compose

### User Prompt

<!-- prompt-start -->
恒温箱样本牌若在屏幕完整、打印后却裁掉批次或到期日，接收员便无法安全放行样本。请从空仓库建立纯前端打印校准台，使用 TypeScript、React、Vite 与 CSS Paged Media，Docker Compose 运行单一 Web 应用并提供一次性 verify 服务，WEB_PORT 可覆盖宿主端口。用户录入样本编号、批次、培养条件、到期日并选择方向；前三项分别最多 18、24、24 个 Unicode 码点且不得换行，编号必填，日期须为有效 YYYY-MM-DD。横向边界固定 70×35 毫米，纵向固定 35×70 毫米，内边距均为 2 毫米；预览毫米值指 CSS 绝对单位逻辑边界。标签采用随仓库交付的同一 WOFF2 字体，字号 3 毫米、行高 4 毫米，四字段按四个等高行排列，每行标签列固定 12 毫米、值列占余宽，文字不得换行或缩放。以各值元素的 scrollWidth、scrollHeight 与 clientWidth、clientHeight 判定溢出；字段非法或任一值溢出时汇总原因并禁用打印。打印时页边距为零且仅输出样本牌，方向、外框和可见文字须与预览一致。打印能力仅按可观测条件裁决：window.print 不是函数时禁用并提示；调用同步抛错时显示失败。不得联网、伪造状态或保留未实现按钮，Vitest 覆盖规则，Playwright 验证预览与打印样
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "5d1c96458051", "repo_name": "stage-rope-cut-planner", "task_type": "0-1 代码生成", "project_category": "全栈", "language_framework": "Python 3.12, FastAPI, Pydantic, TypeScript, React, Vite, pytest, Vitest, Playwright, Docker", "summary": "剧场临时换景前，索具备料员需把多卷原绳裁成指定吊索，锯口损耗会使仅按总长度排料的方案失败。 … 使用 pytest、Vitest、Playwright 覆盖求解与提交展示；Docker Compose 启动 Web 和 API，WEB_PORT、API_PORT 可覆盖宿主端口，并提供一次性 v"} -->
## 0059 · stage-rope-cut-planner

- 创建时间：2026-09-13 18:16:06 +0800
- 项目类别：全栈
- 任务难度：待评估
- 语言/框架：Python 3.12, FastAPI, Pydantic, TypeScript, React, Vite, pytest, Vitest, Playwright, Docker

### User Prompt

<!-- prompt-start -->
剧场临时换景前，索具备料员需把多卷原绳裁成指定吊索，锯口损耗会使仅按总长度排料的方案失败。请从空仓库实现全栈编排台：浏览器录入各卷可用长度、若干“目标长度×数量”和统一锯口损耗；长度须为正整数毫米，损耗须为非负整数毫米，每取得一段即消耗目标长度加一次损耗，卷尾不另计。系统只生成覆盖全部目标段的方案，不得部分满足。最优规则依次为：最小化实际使用原绳卷的余料总和、最小化使用卷数、最小化规范化方案的字典序；规范化方案包含全部原绳并按录入序号排列，每卷目标长度升序，未使用卷为空列表，按列表的标准逐项字典序比较，以此唯一裁决。自行实现带剪枝的确定性搜索，不得调用外部求解器。采用 React、TypeScript、FastAPI、Pydantic 完成真实联调，页面逐卷展示切割长度、累计消耗和余料；非法字段就地提示并保留上一次有效方案。计入损耗后无完整解时，API 与界面仅给出明确不可行结论，清空待执行切割单，不要求计算缺口。使用 pytest、Vitest、Playwright 覆盖求解与提交展示；Docker Compose 启动 Web 和 API，WEB_PORT、API_PORT 可覆盖宿主端口，并提供一次性 verify 服务；README 前段说明运行方式，禁止固定响应和未实现占位。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "d0e578bd8e10", "repo_name": "exhibit-label-contrast-checker", "task_type": "0-1 代码生成", "project_category": "纯前端", "language_framework": "TypeScript, React, Vite, Vitest, Playwright, Docker, Docker Compose", "summary": "博物馆展签送印前，视觉设计师与无障碍审校员需要避免仅凭屏幕观感判断临界配色；请从空仓库起步实现纯浏览器核验台，使用 TypeScript、React 与 Vite。 … Docker Compose 提供浏览器应用和名为 verify 的一次性验收服务，应用宿主端口可由 WEB_PORT 覆盖，使临界样例得到唯一且可复算的印前结论。"} -->
## 0062 · exhibit-label-contrast-checker

- 创建时间：2026-09-13 21:14:35 +0800
- 项目类别：纯前端
- 任务难度：待评估
- 语言/框架：TypeScript, React, Vite, Vitest, Playwright, Docker, Docker Compose

### User Prompt

<!-- prompt-start -->
博物馆展签送印前，视觉设计师与无障碍审校员需要避免仅凭屏幕观感判断临界配色；请从空仓库起步实现纯浏览器核验台，使用 TypeScript、React 与 Vite。表单只接受形如 #RRGGBB 的六位十六进制前景色和背景色、大于 0 的 CSS 像素字号，以及普通或粗体选项。每个 sRGB 通道先除以 255；值不大于 0.04045 时除以 12.92，否则取 ((值+0.055)/1.055)^2.4；相对亮度为 0.2126R+0.7152G+0.0722B，对比度为较亮亮度加 0.05 后除以较暗亮度加 0.05。普通字重且字号至少 24px，或粗体且至少 18.66px，归为大号文字；普通文字 AA、AAA 阈值为 4.5、7，大号文字为 3、4.5。裁决必须使用未舍入比值并包含等于阈值的情况，仅展示值四舍五入到两位。任一字段非法时就地报错并保留上一份有效结果。结果卡呈现原始配色、两位比值及普通与大号文字的 AA、AAA 四项结论，并可复制与当前结果一致的纯文本摘要。使用 Vitest 覆盖公式、分类和阈值边界，Playwright 覆盖合法输入、错误保留及摘要复制；禁止外部在线调用和占位计算。Docker Compose 提供浏览器应用和名为 verify 的一次性验收服务，应用宿主端口可由 WEB_PORT 覆盖，使临界样例得到唯一且可复算的印前结论。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "2e9727b75cb7", "repo_name": "wireless-intermod-screening-console", "task_type": "0-1 代码生成", "project_category": "全栈", "language_framework": "Python 3.12, FastAPI, Pydantic, TypeScript, React, Vite, pytest, Vitest, Playwright, Docker", "summary": "演出彩排临时增加无线话筒后，单看每个载频都合法，三阶互调产物却可能贴近另一支话筒并造成现场爆音。 … 验收者应能看到保护带边界被稳定命中，而安全清单明确显示零项冲突。"} -->
## 0063 · wireless-intermod-screening-console

- 创建时间：2026-09-13 21:18:22 +0800
- 项目类别：全栈
- 任务难度：待评估
- 语言/框架：Python 3.12, FastAPI, Pydantic, TypeScript, React, Vite, pytest, Vitest, Playwright, Docker

### User Prompt

<!-- prompt-start -->
演出彩排临时增加无线话筒后，单看每个载频都合法，三阶互调产物却可能贴近另一支话筒并造成现场爆音。请从空仓库起步，用 React、TypeScript 与 FastAPI 建成联调排查台；开发说明随频率示例解释判定口径，自动化测试覆盖边界、排序和页面提交，所有错误须给出对应行号，不得返回假接口。Docker Compose 提供 Web、API 和名为 verify 的一次性验收服务，WEB_PORT、API_PORT 可覆盖宿主端口。用户一次提交二至三十二个名称唯一的频道，频率范围为 470.000 至 694.000 MHz，必须最多三位小数且互不重复；系统先将频率精确换成整数 kHz。对每一对不同频道 A、B 分别计算 2A-B 与 2B-A，只保留仍在上述频段内的产物；若产物与生成它的两个频道之外任一已分配频率相差不超过 50 kHz，包括恰好 50 kHz，即记为冲突。相同产物与目标频道只展示一次，但须列全来源组合，来源内按频道名称排序；总结果依次按目标频率、产物频率和目标名称排序。页面以受影响频道分组显示差值、产物和来源，并生成可复制摘要；整批输入有误时不得给出部分风险。验收者应能看到保护带边界被稳定命中，而安全清单明确显示零项冲突。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "2bd0413e446f", "repo_name": "mortar-moisture-correction-api", "task_type": "0-1 代码生成", "project_category": "纯后端", "language_framework": "Python 3.12, FastAPI, Pydantic, Decimal, pytest, Docker, Docker Compose", "summary": "雨后砂堆含水变化会让预拌砂浆按干配方直接投料时同时偏离骨料量和实际加水量，配料复核员需要在开机前得到可复算的修正单。 … Docker Compose 仅运行 API，API_PORT 可覆盖宿主端口，并提供名为 verify 的一次性验收服务，使多骨料样例最终返回唯一的湿投料清单和三位小数加水量。"} -->
## 0064 · mortar-moisture-correction-api

- 创建时间：2026-09-13 22:32:42 +0800
- 项目类别：纯后端
- 任务难度：待评估
- 语言/框架：Python 3.12, FastAPI, Pydantic, Decimal, pytest, Docker, Docker Compose

### User Prompt

<!-- prompt-start -->
雨后砂堆含水变化会让预拌砂浆按干配方直接投料时同时偏离骨料量和实际加水量，配料复核员需要在开机前得到可复算的修正单。请从空仓库实现纯后端 API，使用 Python 3.12、FastAPI、Pydantic 与 Decimal；无需数据库，单次请求携带设计加水量以及一至八种骨料的名称、干基目标质量、含水率和吸水率，质量单位统一为 kg，两个百分率均按质量百分数输入。对每种骨料固定计算湿投料量＝干基目标质量×(1＋含水率/100)，自由水量＝干基目标质量×(含水率－吸水率)/100，最终加水量＝设计加水量－各项自由水量之和；中间值保持完整精度，响应中的逐项质量和总量统一按 ROUND_HALF_UP 保留三位小数。实现请求校验、修正计算和批次汇总三个模块，pytest 覆盖公式与边界，README 说明契约，错误反馈定位到骨料下标，禁止假接口或固定响应。含水率限定 0 至 40、吸水率限定 0 至 15，端点均包含，所有质量须大于零；最终加水量为零合法，小于零则以 422 整体拒绝且不返回部分修正单。Docker Compose 仅运行 API，API_PORT 可覆盖宿主端口，并提供名为 verify 的一次性验收服务，使多骨料样例最终返回唯一的湿投料清单和三位小数加水量。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "83801c532b81", "repo_name": "mural-overlay-inspection", "task_type": "0-1 代码生成", "project_category": "纯前端", "language_framework": "TypeScript, React, Vite, Canvas 2D API, Vitest, Playwright, Docker, Docker Compose", "summary": "修复前后的壁画照片常被分别放大查看，细小补绘差异会因视口漂移而被误判；请从空仓库起步实现纯浏览器擦镜核验台，使用 TypeScript、React、Vite 与 Canvas 2D API，只读取用户本地 PNG 或 JPEG，不上传文件或访问在线服务。 … 损坏、非目标格式或异尺寸的新文件须定位原因且保留上一组有效影像与视口，最终拖到任一边缘时应只剩对应单图，回到中部仍逐点对齐。"} -->
## 0065 · mural-overlay-inspection

- 创建时间：2026-09-13 22:46:35 +0800
- 项目类别：纯前端
- 任务难度：待评估
- 语言/框架：TypeScript, React, Vite, Canvas 2D API, Vitest, Playwright, Docker, Docker Compose

### User Prompt

<!-- prompt-start -->
修复前后的壁画照片常被分别放大查看，细小补绘差异会因视口漂移而被误判；请从空仓库起步实现纯浏览器擦镜核验台，使用 TypeScript、React、Vite 与 Canvas 2D API，只读取用户本地 PNG 或 JPEG，不上传文件或访问在线服务。载入两图时以天然像素宽高为准，尺寸必须完全相同；成功后共享同一原图坐标视口，左侧显示修复前、右侧显示修复后，竖直分界线限定在画布内。倍率仅允许 1、2、4 倍，切换时保持当前视口中心对应的原图坐标不变；平移后按各轴钳制到不出现画布空白，图像小于视口的轴固定居中。分界位置取指针相对画布左边缘的 CSS 像素四舍五入为整数，再限制到 0 至画布 CSS 宽度，方向键每次移动 1 像素，并显示倍率、分界像素及视口中心原图坐标。用 Vitest 单测坐标归一化与边界，用 Playwright 覆盖载入和交互；Docker Compose 运行 Web，WEB_PORT 可覆盖宿主端口，并提供名为 verify 的一次性验收服务。损坏、非目标格式或异尺寸的新文件须定位原因且保留上一组有效影像与视口，最终拖到任一边缘时应只剩对应单图，回到中部仍逐点对齐。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "c99f37dcd44f", "repo_name": "allergen-claim-release", "task_type": "0-1 代码生成", "project_category": "全栈", "language_framework": "Python 3.12, FastAPI, Pydantic, TypeScript, React, Vite, pytest, Vitest, Playwright, Docker", "summary": "包装即将送印时，配方原料本身与共线接触信息常被分开核对，复核员可能因此放过一条不成立的“不含”声明；请从空仓库起步，以 Python 3.12、FastAPI、Pydantic、TypeScript、React 和 Vite 实现真实联调的放行台。 … 非法枚举、空配方或缺失标记返回字段级错误且不产生判定，合法请求则逐条返回直接成分与共线证据，使安全配方显示“可印刷”，任一命中都明确阻断对应声明。"} -->
## 0066 · allergen-claim-release

- 创建时间：2026-09-13 23:05:05 +0800
- 项目类别：全栈
- 任务难度：待评估
- 语言/框架：Python 3.12, FastAPI, Pydantic, TypeScript, React, Vite, pytest, Vitest, Playwright, Docker

### User Prompt

<!-- prompt-start -->
包装即将送印时，配方原料本身与共线接触信息常被分开核对，复核员可能因此放过一条不成立的“不含”声明；请从空仓库起步，以 Python 3.12、FastAPI、Pydantic、TypeScript、React 和 Vite 实现真实联调的放行台。浏览器提供结构化配方表：每行分别勾选牛奶、花生、小麦、大麦、黑麦成分，并填写同组共线接触标记，声明只能选择“不含牛奶”“不含花生”或“不含麸质”。README 说明启动与数据约定，.gitignore 排除生成物，禁止用假接口、固定响应或未实现占位；pytest、Vitest 与 Playwright 覆盖裁决和交互。Docker Compose 启动 Web 与 API，宿主端口分别由 WEB_PORT、API_PORT 覆盖，并提供名为 verify 的一次性验收服务。放行条件固定为所有原料及共线标记均未命中目标项；其中麸质命中集合严格等于小麦、大麦、黑麦。非法枚举、空配方或缺失标记返回字段级错误且不产生判定，合法请求则逐条返回直接成分与共线证据，使安全配方显示“可印刷”，任一命中都明确阻断对应声明。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "47d0e9b9ca35", "repo_name": "mural-overlay-inspection", "task_type": "0-1 代码生成", "project_category": "纯前端", "language_framework": "TypeScript, React, Vite, Canvas 2D API, Vitest, Playwright, Docker, Docker Compose", "summary": "壁画裂隙核验需要一把独立的原图像素测距尺，让文保人员在擦镜画面上记录裂隙两端并读取水平差、垂直差和欧氏长度。 … Vitest 在每次点击推进生命周期时校验距离与留白保持行为，Playwright 从载图、两点落尺到视口变化检查读数和覆盖位置，并确认退出后原有擦镜、取样及文件校验仍可使用。"} -->
## 0065-3 · mural-overlay-inspection

- 创建时间：2026-09-14 08:07:20 +0800
- 项目类别：纯前端
- 任务难度：待评估
- 语言/框架：TypeScript, React, Vite, Canvas 2D API, Vitest, Playwright, Docker, Docker Compose

### User Prompt

<!-- prompt-start -->
壁画裂隙核验需要一把独立的原图像素测距尺，让文保人员在擦镜画面上记录裂隙两端并读取水平差、垂直差和欧氏长度。载入图像对后开启测距，第一次左键确定起点，第二次确定终点并完成测量，第三次点击开始新一轮，退出模式只隐藏尺线和读数而保留最近一次完成结果。建立测距对象及等待起点、等待终点、已完成的生命周期，纯函数接收原图坐标并产出整数端点与保留两位小数的距离，App 持有状态，CompareCanvas 通过专用回调提交点击并绘制青色端点、实线和长度标签。点击居中留白时显示“此处无法测距”并保持当前阶段，测距模式下左键不拖动分界而空格或中键仍可平移，缩放和平移只改变尺线屏幕位置，同尺寸单侧替换继续使用原测量坐标。Vitest 在每次点击推进生命周期时校验距离与留白保持行为，Playwright 从载图、两点落尺到视口变化检查读数和覆盖位置，并确认退出后原有擦镜、取样及文件校验仍可使用。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "64fa175af69f", "repo_name": "mortar-moisture-correction-api", "task_type": "0-1 代码生成", "project_category": "纯后端", "language_framework": "Python 3.12, FastAPI, Pydantic, Decimal, pytest, Docker, Docker Compose", "summary": "实验员需要把烘干法原始称量留成可追溯的含水检测批次，而不是先在表外算出百分率再录入修正单。 … pytest与一次性验收从创建多组称量开始，核对完整精度计算后按ROUND_HALF_UP保留三位的中位数与确认状态，并验证非法称量不落库、重复确认不改结果、重建仓储后仍能读取同一批次。"} -->
## 0064-3 · mortar-moisture-correction-api

- 创建时间：2026-09-14 09:46:37 +0800
- 项目类别：纯后端
- 任务难度：待评估
- 语言/框架：Python 3.12, FastAPI, Pydantic, Decimal, pytest, Docker, Docker Compose

### User Prompt

<!-- prompt-start -->
实验员需要把烘干法原始称量留成可追溯的含水检测批次，而不是先在表外算出百分率再录入修正单。实现独立的取样批次模块，创建时接收料堆名称及二至五组湿样、干样质量，生成批次编号并以“待确认”保存；确认时按（湿样质量－干样质量）÷干样质量×100计算各组结果，以中位数形成代表含水率并置为“已确认”。在当前无数据库基线下用标准库SQLite持久化批次、原始读数、状态和确认结果，仓储层保证整批写入，Pydantic拒绝干样不小于湿样、非正质量及未知字段，错误沿用detail数组并定位readings下标。增加创建和确认两个FastAPI入口，不存在编号与重复确认分别返回结构化404和409，已确认数据不可改动，现有修正单契约保持兼容，Compose仍只启动API且API_PORT可覆盖宿主端口。pytest与一次性验收从创建多组称量开始，核对完整精度计算后按ROUND_HALF_UP保留三位的中位数与确认状态，并验证非法称量不落库、重复确认不改结果、重建仓储后仍能读取同一批次。
<!-- prompt-end -->
<!-- task-entry-end -->

<!-- task-entry-start {"run_id": "7786b62a0396", "repo_name": "allergen-claim-release", "task_type": "0-1 代码生成", "project_category": "全栈", "language_framework": "Python 3.12, FastAPI, Pydantic, TypeScript, React, Vite, pytest, Vitest, Playwright, Docker", "summary": "多款产品共用生产线时，复核员需要在排产前识别上一批残留会否带入后续产品，请在现有放行台旁建立独立的换线残留推演模块，以“生产批次序列”为核心对象。 … 原有裁决和方案比较接口、页面状态及Compose端口覆盖保持兼容，Playwright从录入未清洁序列走到真实服务返回的来源解释，并确认清洁后后续批次不再显示残留。"} -->
## 0066-4 · allergen-claim-release

- 创建时间：2026-09-14 13:19:07 +0800
- 项目类别：全栈
- 任务难度：待评估
- 语言/框架：Python 3.12, FastAPI, Pydantic, TypeScript, React, Vite, pytest, Vitest, Playwright, Docker

### User Prompt

<!-- prompt-start -->
多款产品共用生产线时，复核员需要在排产前识别上一批残留会否带入后续产品，请在现有放行台旁建立独立的换线残留推演模块，以“生产批次序列”为核心对象。用户按生产顺序录入至少两个批次的名称和五类过敏原直接成分，并在相邻批次间标记是否完成经验证清洁，提交后逐批查看进入残留、前序批次带入物及离开残留，调整顺序后可重新推演。后端增加推演契约与服务，经验证清洁会在下一批开始前清空残留，否则离开残留为进入残留与本批直接成分的并集，带入项仅取本批未直接含有的进入残留，并保留最近来源批次，pytest据此验证连续带入、清洁归零和直接成分不误报。批次数量不足、名称空白或重复、清洁边界缺失及成分标记非布尔时，返回定位到具体批次或边界的字段错误且不产生结果，页面保留输入并就地提示，Vitest验证修正后可再次提交。原有裁决和方案比较接口、页面状态及Compose端口覆盖保持兼容，Playwright从录入未清洁序列走到真实服务返回的来源解释，并确认清洁后后续批次不再显示残留。
<!-- prompt-end -->
<!-- task-entry-end -->

