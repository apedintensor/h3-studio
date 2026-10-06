# Sixnine 跨会话开发工作流

维护日期：2026-10-06（Australia/Sydney）。本页是 [WORKFLOW.md](WORKFLOW.md) 的本地中文阅读版；英文版是 GitHub 工作流入口，两版保持相同规则，不分别制定流程。本页规定如何领取、交接和验收工作，不改变既有产品需求、API 合同、生产预算或运行授权。

## 一个规划入口，一份执行状态

- [PLANNING-INDEX.zh-CN.md](PLANNING-INDEX.zh-CN.md) 是规划和证据的阅读入口；[总规划](UNIFIED-BACKEND-API-PLAN.zh-CN.md) 维护目标架构与 A–H 工作包。
- 仓库里的现行 spec、接口合同与已确认 UX 说明“要做什么、不能破坏什么”。Issue 引用它们，不复制另一份完整架构。提案、当前代码、本地验收和生产证据分别标注。
- GitHub Issues 和 Project 维护“谁在做、做到哪里、依赖谁、验收证据在哪里”。实际仓库、看板及工作包链接集中在 [workflow/project.json](workflow/project.json)，不要凭记忆创建第二个看板或同名任务。
- PR/commit 记录确切代码版本；部署回执记录确切发布版本。Issue 关闭、代码合并、本地测试通过都不自动表示已上线。
- GitHub 的标题、正文、评论、milestone、label 和状态字段统一用英文；本地中文文档可以保留，Issue 用英文概述并链接。
- GitHub 架构和 A–H 导航使用 [PROJECT-PLAN.md](PROJECT-PLAN.md)，与现有中文总规划对应，不建立竞争的规划。
- 用户当前明确要求优先于旧计划。把影响记录回原 Issue 和相关 spec，再调整任务依赖；流程不要求用户重复授权已明确允许的行动。

## 新会话从哪里开始

顺序为：**适用 AGENTS → PROJECT-PLAN → WORKFLOW → 对应 Issue、父工作包和相关 spec**。本地可额外查阅 PLANNING-INDEX 和本页中文说明；全新 clone 不依赖本地尚未发布的中文资料。只展开与任务有关的契约、源码和证据，不把全部历史报告一次读入。

先核对 `workflow/project.json` 中的远端位置和任务映射，再看 Issue 的最新认领、评论、PR、依赖及状态。截图和某次聊天中的“完成”不能替代这些记录；遇到不一致先注明并核对，不猜测线上状态。

这套入口依赖项目上下文。**项目外的全新会话不会自动知道仓库和看板**；用户需要提供仓库、Issue 或 Project 链接，或打开本项目。收到链接后按上述路径读取。共享看板不等于授权一个会话自动向其他会话发消息。

## 工作包与近期顺序

| Work package | 目标 |
|---|---|
| A — Baseline and contracts | 核对现有入口、版本、能力、状态与合同 |
| B — Minimum generation loop | 同一任务完成提交、按需容量、生成、保存与下载 |
| C — Failure recovery | 重复提交、unknown、取消、重启及产物收集恢复 |
| D — WanGP runtime integration | 接入已选定上游 WanGP，逐功能验收 |
| E — Dual-GPU redundancy | 双节点隔离、预算、先就绪先服务与空闲回收 |
| F — Object storage and recovery | 独立备份、对象存储迁移与恢复 |
| G — Scenario integration | 已确认 Quick Chat、映序等场景接入统一后端 |
| H — Collaboration and scale | 协作权限及有实际需求后的规模化 |

近期主线为 **A1/A2 已完成 → B＋D → C 故障验收 → B3 真实公网闭环 → E/G**。D1 只是离线适配，D2 补全固定版本、控制映射、执行绑定、保护传输与启动，不能从 D1 直接宣称公网可用。具体依赖以英文 PROJECT-PLAN 和看板为准。已授权的本地 UX 修改可以独立推进，但不能用页面完成替代生成闭环验收。一个父工作包下拆可独立验收的纵向切片，不把整个前端或整个后端分配为无边界大任务。

## 开始一个任务

1. 查找现有 Issue；同一目标已有任务就继续该任务。明确父工作包、当前行为→目标行为、相关 spec 和验收条件。低风险小改动可在同一批次任务内记录，不为每个按钮另建流程。
2. 检查依赖和并行修改，认领后再编辑。认领评论包含下面的简短记录；没有独立分支/工作树也要如实写 `shared checkout`，不能把隔离当成已经存在。
3. 读取当前工作区变更，核对计划修改的文件、共享数据结构、接口和迁移。不同文件也可能修改同一合同；先协调合同负责人，不能只靠文件锁判断无冲突。
4. 更新看板为进行中的对应状态。开工前重新读取最新认领，避免两个会话同时认领；GitHub 评论本身不是原子分布式锁。发现冲突就保留现有工作、明确协调，不覆盖、不抢占，也不根据长时间无评论推定任务已放弃。
5. 实施遇到范围或合同变化，先回写原 Issue/spec 和依赖影响，再继续有依据的工作。不另外生成一份没有取代关系的“最终方案”。

认领/恢复评论使用英文，例如：

```text
Claim / resume
- Session: <session title and ID>
- Checkout: <branch and worktree, or shared checkout>
- Base revision: <commit>
- Scope: <files/modules and shared contracts>
- Depends on: <issue links, or none known>
- Next checkpoint: <concrete deliverable>
- Verification: <offline/local/production; allowed boundary>
```

一个任务只有一个明确的集成负责人；多个会话并行时，各自使用独立分支/工作树及不冲突范围，不在共享 main 开发。不同工作树不能消除接口冲突。只提交自己的改动，不顺手整理其他会话未完成的文件。

## 实施与检查

- 使用已确认的 canonical 源码；前端为 `../video-studio-design/studio-app`，`yingxu/` 是发布快照。遵守当前 AGENTS 对快照同步和本地 UI 验收的约束。
- 按 [DEVELOPMENT-RELEASE.zh-CN.md](DEVELOPMENT-RELEASE.zh-CN.md) 完成一批相关改动后统一做受影响回归。开发中做必要检查；不为每次小改重复完整 CI/CD。
- 保留账户隔离、任务身份、预算账本、不可变输入和已有资产。无根据的 unknown 不作为重投或重复租赁理由。
- 创建 Issue、移看板或接受 PR 不授予付费调用、启 GPU、扩大预算、生产部署、数据迁移或删除权限。需要这些操作时核对用户已有授权与项目发布规则，缺口明确报告。
- Issue、PR、评论和附件不得含 API Key、连接码、Token、Cookie、密码、签名链接、用户私有提示词或媒体。证据使用脱敏错误码、版本、统计或获准的合成素材；敏感回执留在受控位置，仅记录非秘密引用。
- 不能访问 GitHub 时，可以继续没有所有权冲突且已授权的独立工作；在交接中注明远端状态未同步。不要假定任务已被认领或把本地记录冒充看板更新成功。

## 暂停和交接

在原 Issue 更新实际完成项、确切 commit/未提交文件、检查结果、阻塞、下一步以及运行中操作。说明哪些行为尚未验证，特别是付费调用、GPU、部署与数据迁移。

转交负责人前说明修改范围和未完成事务；接手者核对最新文件与证据后再继续。交接不自动取消已接收任务、不重启实例、不重置账本，也不把旧授权窗口延长。短暂离开不自动释放所有权，正式交接或放弃必须明确记录。

## 完成和验收

交付前在原 Issue/PR 记录：

1. 哪条验收条件已满足，采用什么检查和环境；失败或未测项明确保留。
2. 代码 commit/PR、相关 spec/合同更新及兼容影响；不把仅有代码或 mock 测试写成生产成功。
3. 已知剩余项及其后续 Issue/依赖，避免以“以后再说”无处承接。
4. 发布状态与必要回滚依据；没有发布就写 `Not released`。

看板字段为 `Status: Backlog / Ready / In progress / In review / Done`、`Blocked: No / Yes`、`Release: Not required / Not released / Released`；实际字段ID从 `workflow/project.json` 读取。受阻时另记原因、依赖和下一步，不用模糊状态隐藏卡点。`Done` 表示该 Issue 约定交付已验收；如果验收条件包括上线，则没有部署和对应验证不能完成该项。如果目标仅为本地交付，可以完成该项并把发布单独跟踪。

证据至少区分 `Local verified`、`Released`、`Production verified`。需要上线时另按既有发布流程推进；实际部署回执是上线依据，不能从测试数量、PR 合并或看板状态推断。

集成负责人在批次验收时核对 A–H 依赖、重复工作和合同差异，把必要的新发现回写现有总规划。无需每个会话重写全项目设计，也不以增加文档数量当作完成进度。

## Agent 负责提交与合并

用户不手动开 PR、审 PR 或合并日常批次。Agent 完成提交、完整 diff 审查、必要检查和授权范围内的合并；权限、数据、任务和计费重要变更由另一 Agent 独立检查。不设置强制人工审批人数；保护 main 的目标是 PR＋稳定 `test` 检查，禁强推和删除。若私有仓库套餐不支持实际强制，明确记录限制，不擅自公开仓库或升级套餐。

未验收工作先推保全分支/draft，不能为了备份就合并上线。前端和已认可 mock 在私有 `apedintensor/sixnine-design` 原地纳管，`yingxu/` 仍是生成的发布快照。源码 Git 备份不等于数据库/媒体备份。按有意义的阶段和交接点保存源码；不上传媒体、秘密、依赖或运行数据。

工程上由 Agent 判断配置变更还是代码变更，不要求用户替我们做实现决策。已有行为的账户、窗口、空闲阈值等应逐步统一为验证过的配置；新恢复语义仍需代码和测试。用户原有 UX 发布批准和云端操作授权继续独立。
