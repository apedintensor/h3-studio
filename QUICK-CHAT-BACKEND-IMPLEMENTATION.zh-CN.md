# Quick Chat 后端本地实现与验证

时间：2026-10-05。此记录描述已写入本地代码与离线测试，不是生产部署、Google模型或GPU生成验收。

## 实现文件与连接点

- `studio_platform/quick_chat.py`：会话、材料绑定、轮次、助手运行、不可变卡片版本、预检、提交/每份执行、单项重试、原项准入恢复、结果导入、稳定timeline。
- `studio_platform/quick_chat_routes.py`：原生 `/v1/quick-chat/` HTTP接口与公共schema；复用账户鉴权、资源读取和受限上传解析器。
- `studio_platform/quick_chat_assistant.py`：受限Google inline多媒体adapter与严格结构化建议；复用原 `google_chat.GoogleChatClient` 中央凭据入口和准确模型ID。
- `QuickChatHooks` 依赖注入：`preflight / create_planned / enqueue / public_job / cancel / refresh_planned`。统一 `GenerationAdmission`、队列、预算、资产、存储的集成由根会话维护。
- 元数据导出 `studio_platform.quick_chat.metadata`；数据库表为 `platform_quick_chat_objects`、`platform_quick_chat_operations`、`platform_quick_chat_events`。备份恢复集成由根会话维护。

## 数据与安全边界

Chat原生数据是创作权威。每个会话隐藏的freestyle project与每份独立shot只作执行投影；调用者不用提交隐藏project ID。材料catalog保留原`enabled`选择；`effective_enabled`才是结合当前recipe后的真实参与，灰色项以`mode_incompatible`记录。Session input_refs与turn/助手冻结一致，排除清单持久，不能把灰色素材实际送给模型。卡片明确选同会话、同owner/project的已验证素材收据，独立版本不修改下一轮材料选择；其输入严格互斥校验，不暗中丢弃冲突项。卡片保存与投影更新同事务，版本与seed固定；投影发生额外修改则阻止预检。

普通讨论、保存文本、素材上传与卡片建议均不提交视频任务。助手默认关闭；打开schema不请求Google。机器身份调用助手需要显式 `assistant:run`，旧Key不会自动扩权。真实模型调用在SQL事务之外，媒体/上下文manifest在请求前持久。准备payload不冒充上游已收到；超时保留unknown、禁止自动重发和换模型。SQL-only `recover_assistant_runs` 对180秒以上的过期运行加fence，并写真实恢复审计事件；根会话集成的 `quick_chat_recovery.py` 在应用生命周期中每30秒执行此SQL核对，不发起上游请求或启动云实例。

每个revision在不同actor、不同Idempotency-Key下仍只有一个submission；每份原execution的retry仍只有一个后继execution。原确认重放只核对持久收据；准入失败需要新预检和明确的resume命令，不自动重复排队。单项取消在job创建两侧复查，已链接任务的取消交给既有队列CAS与真实停止状态。未知状态、restore `recovery_hold`、缺少attempt停止证据或仍有lease均拒绝恢复/重试。

结果导入读取同owner且获授权的真实已验证artifact，拒绝mock占位；按已验证 `content_type`/MIME选择扩展并核对大小/hash。固定client_asset_id用于丢失响应后的收据对账，不重传已存在成片。保留原asset与选段intent，实际片段派生在卡片参考预检；不自动启用下一轮参考，不覆盖原成片。

timeline只记录创作命令，单调session序号支持前向after_cursor与向前翻历史。异步执行通过submission GET读取真实队列状态，不通过伪造进度/反复追加事件表示轮询。

## 离线验收

命令：`.venv/Scripts/python.exe -X utf8 -m unittest test_platform_quick_chat test_platform_quick_chat_assistant -q`

本模块本地36项全部通过；同次合并运行根会话的3项恢复生命周期测试，总计39项通过。临时SQLite数据库、实际CPU解码PNG/JPEG/WebP、fake Google HTTP和mock执行计划用于测试，未读取凭据或发起真实生成。

- 28项持久/API/竞争边界：owner与scope隔离、只读Agent不能创建助手选段派生、幂等参数冲突、版本CAS、旧版本不可变/固定seed、上传与独立卡片素材、recipe有效参与/排除清单、讨论与disabled助手不创建job、完整相关卡片/control继承、unknown manifest与确认、新轮次上下文上限、pending恢复、跨actor/并发唯一提交、原任务丢失响应对账、job未链接与enqueue期间两种取消竞争、单份唯一retry、部分准入原job恢复、停止证据/restore hold、稳定timeline、受限上传与旧project保护、JPEG/WebP导入与原上传收据对账。
- 8项adapter边界：准确模型路径、图片/视频/音频REST payload、Gemma不支持输入不回退、媒体hash/大小/长片段拒绝、unknown不宣称送达、schema实现与线上验证分离、非法结构化输出拒绝、实际PNG收据与请求前SQL manifest。

## 未验证与限制

- 本轮并发验证使用SQLite真实多线程事务；未启用新的PostgreSQL服务器做实际竞争测试。实现使用既有Repository事务/行锁和PG创建advisory lock，不能把SQLite通过当作PG运行证据。
- `gemini-3.8-flash`、`gemma-4-31b-it` 保留旧准确ID和 `gemini/gemini--user-supplied` 配置。schema明确 `verified=false`；通用REST payload与Gemma31B文本/图片支持以官方文档为依据，但未验证这两个hosted ID、额度、费率或真实多媒体质量。助手inline单份6MiB、总计12MiB、最多12个参与媒体；视频/音频须经既有资产校验或明确2–15秒选段。限制不是上游模型的全部能力声明。
- HTTP助手请求目前同步返回完成或unknown；长调用可能超过浏览器等待时长，原turn/manifest仍持久，可通过timeline/turn读取核对，不能自动重发。后台异步运行需要额外显式运行/恢复设计；本轮未启动额外服务。
- 素材inventory沿用既有AssetService的500条上限，native接口提供limit/offset/client_asset_id；未宣称无限规模库存。
- 未启动GPU、云实例、线上LLM或重复部署，未改变原任务/预算。上线及真实推理验收仍需用户确认和对应授权。

官方接口依据：<https://ai.google.dev/api/generate-content>、<https://ai.google.dev/gemma/docs/core/model_card_4>。
