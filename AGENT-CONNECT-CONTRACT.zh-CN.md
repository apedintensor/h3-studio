# 连接 Codex：一次性授权契约与安全审查

日期：2026-10-05 Australia/Sydney。状态：**本地实现与离线验收，尚未上线**。用户已授权继续实施；`studio_platform/agent_connect.py` / `agent_connect_routes.py` 已接本地统一应用，沿用既有Auth/PAT。没有联网调用供应商、启动GPU、部署或改变生产账户、任务、预算。临时SQLite与Windows临时目录仅使用测试随机材料。

## 用户路径

1. 侧栏底部「连接 Codex」打开账户面板；未登录先走现有正式身份登录。
2. 面板显示当前账户、授权范围「创作、上传与生成，本人现有和未来项目」、有效期90天。新设计拟另增 `assistant:run`，允许明确发起可能消费额度的文本助手调用，必须在广泛创作授权面板单独说明。旧PAT和旧连接码不会因此自动获权；文本助手默认disabled，真实线上验证仍需另授权。生成仍受现有账户预算、执行能力和任务确认约束；不授予密钥管理、账户管理、GPU租赁或基础设施操作。
3. 点击「生成连接说明」产生5分钟一次码。说明包括公开 `/for-agents` 文档地址、明确账户、一次码及安全步骤。**一次码本身是短期凭据**，分享即允许兑换；不放到 URL/query/hash、脚本参数或浏览器持久存储。页面只在这次响应期间暂存一次码，复制到用户选定 Agent 的授权流程。
4. Agent 阅读公开文档和本地连接 helper，先在用户自己的 OS 凭据存储保存一个新高熵 PAT 和恢复 verifier，再用 TLS 提交 PAT 的 SHA256及一次码，服务端回传公开连接元信息。PAT不进入聊天、日志、URL或服务器交换响应。
5. 网页显示「已连接 Codex」、最近使用、权限与撤销。Agent创建/编辑通过同一套账户、素材、项目、任务 API，网页显示同一份结果。页面轮询到服务器真实连接状态，不能用定时器伪造成功。
6. 「撤销」停用这条连接及其 PAT；不取消已经接收的任务，不删除素材或历史。手动创建 PAT 是高级备用，不能继续成为默认教程。

用户不需要内部 AI Registry。开发调用供应商仍遵守 Registry规则；公众连接凭据与供应商凭据分开。

## 真实复用项与缺口

| 项目 | 当前依据 | 接入方式 |
|---|---|---|
| 账户与浏览器会话 | `studio_platform/auth.py` 的 accounts、sessions、Principal；`api.py` 的密码登录/HttpOnly Cookie | 发码/列表/撤销必须现有浏览器会话，拒绝机器Key继续发Key |
| PAT鉴权、权限、撤销 | `personal_keys`、`Auth.personal_bearer/list_keys/revoke_key` | 写同一张 PAT 表，只保存 token hash；沿用既有 scopes/所有权检查 |
| 密码更新/账户停用 | PAT记录 auth_mode/password_version | 未兑换码与恢复请求必须检查原授权版本；既有 PAT 验证逻辑继续生效 |
| 公共发现 | `/for-agents`、`/llms.txt`、guide.json、Skill 下载 | 更新旧手动教程，公布一次码协议与 helper 下载/摘要；不给私有数据 |
| 同源写入保护 | `api.py` middleware 的 Origin / Sec-Fetch-Site 检查 | 新发码/撤销沿用且明确要求 JSON；匿名交换不放开跨站浏览器来源 |
| 素材、项目、任务、预算与 GPU | 现有 API、队列和 controller | 全部复用；连接码兑换只做凭据事务，绝不排队或租GPU |
| 一次码、兑换恢复与审计 | `agent_connect.py`的新独立SQL表，已离线验证 | 原子消费、固定授权快照、幂等发码收据与短恢复，不用内存状态 |
| 公共本地安全保存 | canonical `skills/sixnine-yingxu/scripts/connect.py`，`tools/agent_connect.py`薄入口 | Windows DPAPI；Linux Secret Service（需secret-tool与DBus会话）；没有 OS 安全存储停止，不写明文 JSON 兜底。macOS适配尚缺 |

## 本地接口（不代表已上线）

| 接口 | 身份/输入 | 返回/约束 |
|---|---|---|
| `POST /v1/account/agent-connections` | 浏览器登录与Origin；name，默认Codex；authorization_profile_id/version；Idempotency-Key | `{connection,code,code_available,replayed}`；**code仅首次响应返回**，同操作重放返回同记录但code=null；固定owner/授权快照，最多5个pending |
| `GET /v1/account/agent-connections?limit=50&offset=0` | 浏览器登录；分页最大100 | 公共连接记录，无码、PAT、hash或verifier |
| `GET /v1/account/agent-connections/{id}` | 原账户浏览器登录 | pending/connected/expired/revoked/unavailable；可返回既有 public_key 元信息 |
| `DELETE /v1/account/agent-connections/{id}` | 原账户浏览器登录 | 幂等撤销，停用已兑换PAT；不触任务 |
| `POST /v1/agent-connect/exchange` | 匿名TLS + JSON；code、client_challenge、token_hash、key_prefix，可选recovery_verifier | connection + public key metadata + recovered；**没有api_key字段** |

既有 scopes 为 projects:read/create/write、assets:read/write、jobs:read/write。新设计的文本调用拟扩 `assistant:run`，要最小改造Auth的允许集，但不得从允许集自动生成授权。每次发码持久保存 `authorization_profile_id/version`、**确切scopes列表**、owner/tenant、all_projects/project_ids、PAT期限及授权fingerprint；兑换仅按这份不可变快照注册，不读取部署后最新的scope全集，也不自动加入未来权限。已发码的scope不能靠一次部署变化扩大；新profile需要用户新的明确授权。

兑换入口不接受可修改owner、项目范围、scope、权限、TTL或模型参数的字段；额外字段应422拒绝，不忽略后悄悄执行。可以接受 `expected_authorization_fingerprint` 这样的只读一致性断言，匹配后才消费码。成功兑换/恢复返回原授权账户与profile快照，helper核对复制说明中的账户/域名和快照后才标连接成功；失败响应不得反射账户、fingerprint或秘密。

本地公开发现包含 `/for-agents/connect.py` 及 `/for-agents/connect-manifest.json`，提供canonical helper的同源SHA256摘要。禁止远程脚本直接管道执行；Agent先阅读 helper，使用用户指定 origin。helper拒绝重定向、认证参数URL、非HTTPS（隔离loopback预览除外）；不能默认为另一服务或将凭据转送第三方。生产仍是原版本，不声称新入口已上线。

## 兑换与不确定结果

- 浏览器码：`sxc_` + 32随机字节的URL-safe编码，服务端仅保存SHA256；不是可枚举的6位数字。5分钟TTL不能延续。
- Agent先生成 `sxp_` + 32随机字节的 PAT及独立随机 verifier；在安全存储中先保存pending记录，再发交换请求。只发送PAT摘要和其12字符前缀，服务端从未见到完整PAT。
- 首次交换绑定 `client_challenge=SHA256(verifier)`、PAT hash和key_id，同一事务原子消费授权并创建**一条**既有PAT记录。SQLite写事务用BEGIN IMMEDIATE，PostgreSQL锁账户/授权行；唯一token hash约束兜底。失败事务回滚，不消费码。
- 同一码的另一个challenge/token hash，即使并发也不能拿到第二条Key；返回409，不提示密钥内容。
- 响应丢失时保留原本地PAT/verifier/请求，使用相同码与hash + recovery_verifier恢复**同一**已发PAT元信息。允许兑换后5分钟恢复；不增加TTL，不重建、不轮转。若显示not_exchanged可重试原首次交换；必须先判断恢复结果，不能换随机Key重投。
- 已过恢复窗口且本地保存完整，可先用该PAT执行不联网之外的只读 `/api/auth/me`（需要用户允许联网验证）；如不能确认，用户撤销原连接并重新明确授权。helper绝不默默产生多个Key。
- 新授权是用户重新点击发码，旧pending可显式撤销；不是通过旧code/PAT签发或加权。

## 边界验收与风险

| 场景 | 必须结果 |
|---|---|
| 码过期 | 410，未新增PAT；浏览器明确「重新连接」 |
| 同码并发不同客户端 | 恰好一条PAT，其他409；审核真实存储计数，不看客户端动画 |
| 同码重复/无verifier恢复 | 不重签，409 |
| 丢响应且verifier正确 | 返回原key_id，数据库数量/有效期不变 |
| verifier错误、token hash变更 | 不恢复、不签发；静态错误，无秘密 |
| 匿名暴力请求/限流 | 按可信源地址哈希每5分钟10次，429+Retry-After；不信任客户端自行提供X-Forwarded-For |
| 恶意Origin、cross-site浏览器POST | 403，未创建/撤销/消费；不启用宽松CORS |
| PAT尝试发码/查看连接/撤销 | 403；PAT无管理凭据能力 |
| wrong owner/tenant | 404，不透露是否存在；连接列表只返回当前账户 |
| 用户注销后兑换尚未使用的码 | 授权会话失效，410；不会留下可离线升级的凭据 |
| 密码轮转/账户停用后兑换或恢复 | fail closed，原Key鉴权也无效 |
| 发码后新增assistant:run/其他scope或调整PAT期限 | 原授权快照不变化；旧PAT和旧码没有新增权限，新profile须用户重新授权 |
| assistant_mode=assist/discuss但无assistant:run | 拒绝外部文本调用且不消费文本额度；none仅存文字/手写卡仍可用；propose为助手输出intent，不是默认强制出卡模式 |
| helper预期账户/origin/profile与兑换授权不符 | 拒绝标connected并保留可撤销状态，不能把错误账户Key用于后续创作 |
| 网页撤销或既有PAT管理撤销 | 新访问/恢复失败；原已接收任务和素材不受影响 |
| 第50条有效PAT边界 | 拒绝新Key，授权不被消费；用户先管理现有Key |
| 数据库异常 | 503安全信息；不返回SQL/DSN/请求内容；状态未知先恢复原操作 |
| helper无OS安全存储 | 不兑换，给明确本地配置缺口；不用普通JSON/.env兜底 |
| 公共文档/授权说明 | 明确一次码是凭据，不声称链接本身授权或 GPU 在线 |

连接审计记录 issued/exchanged/recovered/revoked、owner、connection_id、UTC时间；不记录code、hash、verifier、PAT、提示词或用户媒体。生产访问日志只记方法/固定路由/状态，不记body。匿名失败计数是安全运行指标，不能记录原输入。公开连接状态与 audit不得反推私有作品内容。

## 实施前待核实

- root维护路由、middleware公开POST豁免和整体API契约；此模块只负责SQL授权事务，不负责重新设计认证体系。
- 已修复设计阶段动态scope缺口：profile=`creator-full`/version1为显式8项创作权限，发码即保存scopes、origin/owner/tenant/id、PAT绝对到期与fingerprint。后续常量/允许集变化不扩大旧码；兑换不能修改授权。
- 反向代理/ASGI的可信源地址设置应基于当前真实部署核对。默认用request.client.host限流，不能自行把全部代理标成可信；若所有用户都被当作代理IP，需要明确修复部署配置后上线。
- 发码按钮双击可由前端pending锁防止；若需要网络超时发码幂等恢复，**不能服务器回显已持久保存的码**。最简单是状态不明先列表撤销该pending，再明确重新发码，不能无限积累码。下一阶段可增加浏览器-held issuance challenge，但不应为方便存储明文。
- 90天为建议默认期限；固定范围是用户明确要求的广泛创作，不能误写成基础设施管理员。
- 普通公众的macOS本地安全存储缺口应如实显示，不把Windows成功当全平台完成。
- 已完成临时SQLite和in-process HTTP验收，未运行临时PostgreSQL；PostgreSQL锁/表/唯一约束走兼容SQLAlchemy实现，需后续可用隔离PG验收。离线连接测试不代表GPU、Google模型或生产TLS入口已实测；用户确认后才发布。

## 本地实现与验收记录

- 服务：`studio_platform/agent_connect.py`；路由hook：`agent_connect_routes.register_routes(app)`，只注册账户管理与唯一匿名exchange。root统一middleware负责精确匿名豁免/公共文档，已用真实create_app进行in-process验收。
- SQL表：platform_agent_connections、platform_agent_exchange_limits、platform_agent_connection_audit。由业务backup的Auth排除集保护；不能把授权hash/状态导出成普通业务备份。
- helper：canonical `skills/sixnine-yingxu/scripts/connect.py`；标准库，无Registry、依赖安装或import时副作用。connect/resume/whoami/request；JSON request支持method/path/input与Idempotency-Key，不输出PAT。multipart和流式下载复用原Sixnine SDK的受保护入口，由统一集成负责人适配，不在本helper重复造存储逻辑。
- Windows采用用户DPAPI密文blob，临时文件也只写密文，原子创建避免覆盖另一个客户端的token；Linux用Secret Service且有非秘密锁文件串行化写入，无存储即停止。macOS尚未支持。
- 20项SQL授权测试 + 5项路由测试 + 11项helper测试 + 2项实际app middleware测试，共**38项离线通过**。覆盖并发仅一Key、并发发码仅一次返回、丢响应恢复原key、源/连接限流、50Key上限、固定snapshot、TTL、密码轮转/退出/撤销、跨账户/tenant、机器不能管Key、严格Origin与字段、公众发现、真实PAT鉴权、旧PAT不扩assistant权限。
- Windows DPAPI只对临时测试假值做密文/往返验证，没有读取系统既有凭据；Linux无存储停止是模拟环境验证，未连接真实Linux Secret Service。没有线上API/模型、GPU、收费或发布验收。

## 对整体设计的独立安全审查（2026-10-05）

检查对象：`video-studio-design/QUICK-CHAT-PRODUCT-SYSTEM-DESIGN.zh-CN.md`、`QUICK-CHAT-API-CONTRACT.zh-CN.md`、`QUICK-CHAT-IMPLEMENTATION-BACKLOG.zh-CN.md`。没有执行API、模型、助手、GPU或生产操作。

未发现推翻本地生成PAT/hash注册方案的P0。设计已明确无明文Key响应、既有Auth复用、未知恢复、不扩大旧PAT、文本调用默认禁用、独立文本限额及上线前离线验收。

需要在整体契约落实的P1：

1. **授权时持久冻结scope/profile/期限**。仅写“固定profile”不足以保证未来部署不会将新scope加入尚未兑换码。本文已补确切快照与版本；不能以当前API_SCOPES全集代替。
2. **每个付费助手入口明确权限**。最终统一契约中默认assist/discuss = projects:read/write + assistant:run，none = projects:read/write；运行前/恢复后重复验证身份、scope、开关、账户文本限额，不把LLM建议当调用授权。GET/轮询/acknowledge-unknown不得隐式发新请求。

统一产品/API契约以 `../video-studio-design/QUICK-CHAT-PRODUCT-SYSTEM-DESIGN.zh-CN.md` 和 `QUICK-CHAT-API-CONTRACT.zh-CN.md` 为准；本审查推荐条目不是已存在端点。当前草稿尚不满足授权快照/fingerprint/新scope，不可直接接入生产。
3. **客户端可检查授权账户**。交换成功元信息需要授权账户/profile快照，说明和helper的预期账户/origin必须匹配；只返回不含owner的旧public_key不能可靠识别连错账户。错误响应保持匿名安全。

实施验收还应明确：码发放、匿名兑换及恢复都有源级与已知连接级限流（不能以无效ID建立无限状态）；不要仅复用登录限流。发码/撤销要求同源浏览器会话，匿名兑换只精确开放POST，拒绝恶意Origin且不放开其他私有API。发码响应丢失不存明文码补发；列表检查/撤销原pending后，用户明确发新码。
