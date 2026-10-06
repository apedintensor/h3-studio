# 快速创作：后端设计审查与真实复用边界

日期：2026-10-05 Australia/Sydney。范围：本地源码只读审查；遵守最新要求，先整体设计、再确定改造任务。本文件是给集成负责人的审查输入，不是最终 API 契约，也不是实现或上线回执。未调用 Google、视频生成、云资源或生产数据库；未修改配置、预算、任务或现有用户资产。

## 产品数据必须成为唯一权威

体验基准是 `../video-studio-design/quick-chat-mock/IMPLEMENTATION-HANDOFF.zh-CN.md` 的最新需求，不是 mock 假逻辑。用户面对的是创作会话、素材、本轮输入、任务卡、抽卡结果。进入页面直接上传、输入；故事、章节和镜头不作为快速创作的必填条件。

建议权威实体为 `session → turn / assistant_run → card_revision → submission → batch_item → result_link`，素材通过独立引用与这些实体关联。现有 `workspace:freestyle` 项目和每份镜头只是内部执行与资产权限投影，保留现有生成链路，不用旧项目树倒推聊天产品。

**双写不是两个权威源。** 卡片版本包含原始输入快照、明确使用的素材、模型参数、份数与每份 uint64 种子；隐藏镜头带来源卡片版本和快照哈希。预检和提交必须验证投影等于卡片快照。旧项目 API 若仍可写这些镜头，同一账户 Agent 就能改镜头而卡片不变，造成 UI、预检和执行分叉。因此：

- 标记内部投影的 `integration_kind=quick_chat` 和来源身份；已有普通故事保持原行为。
- 公共旧项目写入口拒绝编辑此类投影，并指向原生聊天 API；包括完整 PUT、actions、实体写入、raw generation-plans 和不带原生 submission 绑定的生成提交。
- 聊天服务在进程内复用纯校验、素材解析、compile/admission/queue 原语，不用请求本机 HTTP 串联多个有副作用端点。
- 投影失配时关闭该次提交并报告具体错误；可从未提交的权威版本重建投影，不覆盖已有任务的不可变输入。
- 快速聊天目录与普通故事目录分离展示；隐藏项目 ID、shot ID 只作为内部关联，用户和 Agent 使用 session/card/item 身份。

## 保留、改造、重写表

| 能力 | 现有证据与路径 | 处理 | 具体理由 / 缺口 |
|---|---|---|---|
| 账户、Cookie、PAT、权限 | `studio_platform/auth.py`、`api.py` middleware | 保留 | 复用 owner/tenant 与现有 scopes，不能客户端传 owner |
| 一次性 Agent 连接码 | 交接明确尚缺；旧教程并非新要求 | 新增独立认证切片 | 短 TTL、原子兑换、撤销、限流、防重放；不能搬 mock Key |
| 上传、不可变原件、CPU 解码与选段 | `assets.py`、`upload_route.py`、`media.py` | 保留并包装原生 session 媒体入口 | 资产当前按 project_id 隔离；以隐藏执行 workspace 映射会话，不重复创建副本 |
| 素材参与状态与上下文继承 | mock 内存 assets/settings | 重写 | 持久 composer、轮次输入快照、卡片快照；删除引用不删除原件 |
| 生成控制和输入语义 | `generation_draft.py`、`capabilities.py` | 保留纯校验并改造契约映射 | inputs 支持 source_range、video include_audio、独立 guides；取实际 execution_support 更严范围 |
| 会话、轮次、助手运行、卡片版本 | 当前不存在生产持久实体 | 新增原生权威实体 | 不把项目全文当聊天历史，不使用 mock turns 数组 |
| Google 文本适配 | `google_chat.py` | 保留上游适配、改造正式编排 | 当前为精确两个模型、无降级、文本适配；缺账户、运行状态、持久上下文和结构化提案 |
| 已保存镜头草稿 / 预检 | `generation_draft.py`、`api.py::plan_response` | 改造为内部执行投影服务 | 现有状态钩子可复用；必须绑原生 card_revision + immutable input hash |
| 多份计划 / 批次 | `batches.py` | 保留部分机制、改造业务幂等与 DTO | 有逐项持久链接和崩溃恢复；当前 actor 级幂等不足以防网页与 Agent 同卡双投 |
| 任务队列、尝试、预算与租赁 | `repository.py`、`queue.py`、现有 fleet/scaler | 保留 | 不因新产品重置预算、租赁周期、未知提交与已接收任务 |
| 真实任务状态与结果下载 | `api.py` public_job/public_artifact、`repository.py` bulk reads | 保留并投影到原生 batch_item | 不计算假进度，不把 UI 定时器当执行证据 |
| 输出显式用作参考 | `guided.py::artifact.adopt` | 新增 CPU artifact→asset 桥 | adopt 只附项目节点；generation_draft 需要 cloudAssetId，不能直接传 artifact_id |
| 项目活动审计 | `project_activity.py` | 保留，补原生会话活动来源 | 保留不含提示词和媒体 URL 的审计；不能 poll 时重复写“事件” |
| 页面假回复 / 假状态 / 演示 Key | quick-chat-mock/app.js、agent 页面 | 丢弃假逻辑，保留已认可交互 | 不搬假模型理解、假生成结果、定时百分比或仅内存历史 |

## 数据结构建议与不变量

| 实体 | 必须持久的字段 | 不变量 |
|---|---|---|
| session | tenant、owner、id、title、model_id、version、composer 输入 / 参数、内部 workspace、created/updated | 不同账户/会话隔离；明确模型 ID，不静默切换 |
| turn | session、顺序号、text、model_id、输入 / 参数快照、actor、created、client operation key | 发送前冻结快照；刷新或重登可读；不由之后 composer 覆盖 |
| assistant_run | turn、operation_id、status、fence / started、reply、error_code、提案引用、usage | 同一发送最多一个上游请求；超时未知不自动重试；打开页面不发请求 |
| media_binding | session、asset_id、种类、用途 / slot、参与状态、选段 / 原声、version | 引用指向已验证同 owner/session 素材；解绑不删除原件 |
| card_revision | card_id、revision、turn_id、完整 prompt / recipe / controls / inputs、copies、各份 seed、hash | 已提交版本不可变；编辑产生新版本；不让模型输出授予资产权限 |
| execution_projection | card_revision、item_index、内部 shot、projection_hash | 投影可再现、可核验；没有独立用户编辑权限 |
| preflight | card_revision、capabilities / policy 版本、各份 plan、expires、estimate、blockers | 不是排队或租 GPU；不是永久可执行凭证；双击不能重新随机种子 |
| submission | card_revision、唯一业务 operation_id、提交 actor、创建时间、各份绑定、cancel_requested | 同一版本最多一条首次 submission；跨网页/Agent 并发也同一条 |
| batch_item | submission、index、seed、plan_id、job_id、重试 lineage | 独立状态/输出；成功项不能被失败项覆盖；unknown 不产生新 job |
| result_link | batch_item、artifact_id、kind、validated metadata | 复用真实产物，授权后提供内容 URL；输出不自动成为下一轮参考 |

### 上下文继承

session composer 是“下一轮”的可编辑值；turn、card_revision 和 job request 分别冻结当时输入。默认沿用 composer，但页面清楚列出本轮参与素材，使用者可单项移除。仅有用户显式“用作参考”才将生成视频转为可用素材，再添加到 composer；“继续修改”复制该版本的原输入，不偷偷加入输出视频。

普通讨论只创建 turn / assistant_run，不创建 H3 job 或 GPU intent。模型可以生成可编辑卡片提案，但提案不自动预检或提交。提案只能提供明确允许的文本字段；资产、controls、账户和基础设施权限来自服务器已授权快照，不能由模型响应扩大。

Google 现有适配只发送文本。素材元信息不等于视觉/听觉理解。第一批若只复用该适配，界面必须说明助手未读取媒体内容；不能把上传缩略图或元数据说成模型已理解图片、动作或声音。真实多模态输入需要单独设计文件 / inline 数据传输、各模型能力与 size/type 校验，不能直接猜支持范围。

## 事务、并发与未知状态

### 用户发送与助手运行

1. 同一事务检查 session expected_version、重复操作指纹与权限，冻结 turn 输入，分配顺序号并创建 assistant_run。输入已保存，模型未执行。
2. 运行获取唯一 claim/fence 后，离开数据库事务调用精确模型；不能持写锁等待最长 120 秒网络请求。
3. 成功后同一事务提交 reply 与合法 card 提案。即使用户在等待期间编辑下一轮，也只使用 turn 快照，不覆盖新 composer。
4. 上游超时、发出请求后网络失败、进程在请求期间退出，保留 unknown / recovery_hold。没有 provider 幂等 / 结果查询保证时不得自动重发。显示“可能已计费”，允许用户明确发起新一轮。
5. 同 operation key 返回持久 run；不同 payload 使用同 key 报冲突。进程重启不能把已发送状态重置到 queued。MVP 可限制每 session 同时一条助手运行，同时仍可编辑 composer。

原适配 `GoogleChatClient.generate` 是同步调用，不提供 provider idempotency 或结果恢复。把它塞进 endpoint 的内存 BackgroundTasks 不等于持久编排；必须先持久记录调用边界与恢复状态。元数据查询成功也不证明生成可用。

### 卡片版本与执行投影

所有 card_revision 与对应投影应在同一 repo.transaction 创建或使用持久 rebuild receipt。复用 `guided.new_entity`、`generation_draft.prepare_patch/configure`、`validate_project`、`append_activity` 的纯规则，不调用嵌套 guided.mutate 开第二个写事务。已解析的资产引用在进入事务前检查，事务内再检查关联身份与版本。

SQLite 已使用 BEGIN IMMEDIATE；PostgreSQL 需要固定锁顺序与 row lock。新表增量创建沿用启动 DDL advisory lock，不 drop/rebuild 老表。所有读查询必须同时过滤 tenant、owner、session 并核验 API scopes；不能只拿随机 UUID 当授权。

### 提交与双投防止

现有 BatchService 唯一约束是 `(tenant, owner, project_id, actor_id, idempotency_key)`；现有 job 幂等也包含 actor_id。这是旧业务的有效机制，**但不保证一个卡片版本只有一批任务**：网页和两个 Agent 的 actor 不同，用同 key 仍可建不同批次。

因此原生 submission 必须有 `(tenant, owner, card_id, revision, initial_submission)` 唯一业务约束，在任何 job 创建前持久化 submission 身份及计划绑定。当前 caller 决定权限；稳定的 submission / item 身份决定执行幂等 namespace，不能随登录、更换 Key 或接手恢复而变。审核记录保留首次提交人与恢复人，不悄悄冒充原 Key。

原服务的“先创建 planned job、再绑定 item、最后 enqueue”可继续复用。崩溃后查同 operation/item 的原 job，补全链接或入队，不能创建新 job。投影验证、预算预留、pool idle shutdown 的容量锁和 waiting_capacity 保护仍由现有 admission / Repository 承担。

### 单份重试

- 未接受的单份预检 / admission 错误可修复并重新预检，但其他已接受 item 保留。
- 未提交 planned job 的恢复应继续同 job，不叫“重新生成”。
- 显式 retry 仅针对真实 failed（或明确设计的 cancelled）且所有相关 attempts 都证明 upstream_stopped / 从未提交、无活跃 lease。
- `submitting`、`submission_unknown`、`running`、`collecting`、`cancel_requested`、`recovery_hold` 都不能新投；collecting 失败是结果收集恢复，不重新付费生成。
- 重试先持久绑定 source_item/source_job、新 retry operation、种子和新计划；固定幂等重放相同 retry。不得覆盖原失败条目、成功结果或账务。
- 账務 pending 不等于任务仍运行，也不能因此清零预留；遵守现有结算机制。

## 页面历史与 API 读取

建议会话列表只返回小摘要；详情采用稳定 sequence / cursor 分页，一次读取有界 timeline，不每次返回所有项目全文。按“用户输入 → 助手回复 → 卡片 / 版本”顺序渲染，job/batch 状态读最新数据库事实。不要分别分页 turns/cards 后在前端按当前时间猜顺序。

重登后按 owner 拉同一会话历史；本地未提交文本与服务器 version 冲突时保留本地编辑，让用户确认同步。由现有 request `X-Expected-Account` 防止跨标签账户切换误写。

批次状态使用已有 `get_job_summaries_for_owner` / `list_artifacts_for_jobs` 有界批量读取，避免 N+1；目前最多 100 exact pairs，可用每页 20 个卡片 × 4 份限制并按 version 分页。列表页不返回私有提示词或完整 effective_request；详情按授权提供。错误只返回安全 error_code 和可操作说明，不返回 token、供应商凭据、签名 URL、SQL 或内部预算身份。

## 建议纵向实施任务与验收

1. **原生领域模型与投影边界**：唯一权威表、版本/幂等、隐藏 workspace 标记及旧写入口隔离。验收：旧 Agent 改投影被拒绝，原普通故事保持可编辑。
2. **创建会话与真实素材**：进入即有会话，上传 / 选段 / 本轮用途，刷新保留；同会话允许历史素材重用，不允许跨账户/会话串引用。
3. **助手运行与卡片**：准确模型、明确 disabled/unknown、持久上下文、合法提案；讨论与形成卡片都没有生成 job / GPU intent。
4. **版本预检与明确提交**：冻结参数与各份种子，聚合真实计划/费用/限制，跨 actor 同卡并发只一条 submission。
5. **结果与故障恢复**：真实 job / artifact，部分成功保留，仅单份安全重试；网络超时、进程重启、未知上游均不双投。
6. **结果用作参考**：拥有者校验后 CPU artifact→asset 导入收据，真实解码 / 片段校验，明确添加composer。复制继续修改不自动引入输出。
7. **公开 Agent 接入**：一次码、正式Key管理、原生 API与Skill，网页能看到同一数据。认证独立切片，由另一负责人审查。

离线验收需要临时 DB、假上游与既有 Fake/Mock 执行链路并显式标注模拟；重点包括 owner/scope、会话素材隔离、版本冲突、跨 actor 双击、每个崩溃窗口、未知请求、失败单项重试、分页顺序与刷新恢复。只做本地验收；真实 Google / GPU 调用与生产发布另依授权，不因新页面自动租机、复制预算或重新部署。

## 本次已确认和未确认

已确认的是上述本地代码能力和缺口。未检查实时线上健康、Google生成能力、GPU当前库存、账单或用户媒体；不把历史生产回执当实时可用。本次没有实现原生服务、修改生产或运行新任务。最终产品设计、统一数据契约及任务边界由主集成会话定稿后再开始代码。

## 对整体设计三文件的独立复核

复核对象：`../video-studio-design/QUICK-CHAT-PRODUCT-SYSTEM-DESIGN.zh-CN.md`、`QUICK-CHAT-API-CONTRACT.zh-CN.md`、`QUICK-CHAT-IMPLEMENTATION-BACKLOG.zh-CN.md` 在本轮初稿。**没有发现设计级 P0；原生权威、投影隔离、首次确认跨 actor 业务唯一性、未知不重投方向正确。** 以下是进入实现前必须定清的 P1 决策，不是在声称现有生产已发生这些漏洞。

1. **retry 的业务唯一性与 CAS**：首次 submission 已有 owner+revision 唯一约束，但同一个 `retry_of_execution_id` 可能被网页和多个 Agent 用不同 key 同时重试。除 HTTP 幂等外，需要 `(tenant, owner, item_id, retry_of_execution_id)` 唯一恢复记录，并锁定/CAS `item.current_execution`；所有崩溃恢复绑定同一次新 execution/job，不每个调用者生成一个。
2. **部分准入失败的恢复命令**：预算阻塞、rejected/no-job、expired planned item 不一定属于 failed/cancelled job。契约既承诺保留部分成功，又限制 retry 只接受已停止失败项、首次 submission 不可再创建，因而必须明确这些未执行项如何恢复：继续同一个未过期 planned job，或证明从未提交并处理原记录后新建 ItemExecution。需要命令/状态、fresh preflight 的 target item 绑定与预算保护，不能靠整批重投。
3. **执行状态与 timeline seq 的更新机制**：设计包含执行变化/结果 timeline、只读 seq 轮询，但没有指定 job/queue 谁推进事件。应明确选择持久 outbox→timeline 投影（源事件唯一、事务 checkpoint、重启补偿），或只持久创作事件并独立轮询 active submissions 的最新 job DTO。GET 不写；job 完成时 latest_seq 不变也不能让页面一直显示旧状态。
4. **三种快照边界**：CardRevision 保存即不可变，但派生素材/有效预设/policy 信息在 preflight 才确定。区分 revision 的 requested-input hash（原收据SHA、选段、显式参数）、preflight 的 resolved-execution hash（派生收据、有效参数、schema/model/policy）、既有投影 source_snapshot hash。派生 lineage 存 preflight，过期生成新预检，不补写旧 revision 或把不同结构的哈希当同一值。

另有一项 API 完整性需说明：旧上传要求 `client_project_id`，产品要求 Agent 直接使用 session 身份。可增加原生 `/sessions/{id}/assets` 薄适配，或规定公开 helper 在内部解析受保护 upload target；不能要求普通用户自己操作隐藏项目树。此项不要求另造上传/存储实现。

建议新增验收：不同 actor/不同 key 同源执行重试只建一个新 execution；rejected 与过期planned项有可解释恢复；后台完成/取消/失败后纯读页面发现真实变化，重启不重复timeline；预检创建派生素材后原revision hash完全不变。
