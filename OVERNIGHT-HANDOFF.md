# 映序与H3十小时实现交接

用户授权：2026-10-04悉尼，要求研究Hippius SN75、反复审查架构并实现功能，持续工作10小时；问题最后集中询问。先保留原作品/配置，尽量完成独立部分。

- 开始：2026-10-03T16:27:42Z。
- 截止：2026-10-04T02:27:42Z（悉尼2026-10-04 13:27:42）。到时写报告，停止新增任务并暂停对应自动跟进；未完成事项如实报告。
- Goal已创建，禁止误当全部完成。旧h3 GPU测试自动化PAUSED，不能恢复。
- 工作根：`C:/Users/danmo/Desktop/inference`。H3独立git repo：`h3-studio`；映序：`video-studio-design/studio-app`。
- 用户网站：HTTPS `www.sixnine.art`；拟`h3.sixnine.art`为H3工具。域名现Namecheap停车页，DNS尚未改。
- AWS MCP STS成功但为root；不作为应用/CI身份。无已批准CPU/GPU具体费用上限。云创建/GPU仍关闭，autoscale dry-run/max0；完成代码、IaC、离线与本地验证不需要等待。
- 中央配置必须复用AI-Registry。R2指定profile本机加载/官方主机类型核对通过，不是在线读写验证。Hippius尚待研究，不凭SN75名称猜接口/凭据。
- 本机旧H3数据、映序localStorage/IndexedDB/素材均不能覆盖。新平台使用独立测试DATA和版本化API，旧接口保留。

## 初始基线

- H3现有单进程SQLite+Comfy本地隧道，GPU已销毁；Docker/密码模式/CPU CI已有。
- 映序现有React/ReactFlow本地创作原型，章节/场景/角色/镜头/素材/候选流程可复用；无云项目和真实任务。
- `LAUNCH-SCALE-NOVEL-PLAN.zh-CN.md`、`VIDEO-API-V1-DRAFT.zh-CN.md`、`SIXNINE-READINESS.zh-CN.md`为设计资料，不是已实现声明。
- git初始已有`SCALING.zh-CN.md`修改，保留；现有默认拒绝.gitignore需按新代码精确增白名单，禁止把凭据/用户数据/模型加入Git。

## 工作分工与进度

准备分三条并行线：存储/Hippius；任务账本/调度；映序云端UX。root负责架构契约、API/auth集成、部署与总体验收。每次换任务先确认文件所有权，避免覆盖。

- /root/h3_user_tests_docs：studio_platform/repository.py、queue.py、autoscale.py及对应tests、QUEUE-CONTRACT.md。
- /root/lightsail_packaging：studio_platform/storage*.py、test_platform_storage.py、Hippius研究文档。
- /root/lightsail_deploy_audit：映序studio-app/src和vite proxy，云模式与H3 UI。
- root：settings/auth/capabilities/project_validation/media/assets/platform_app和集成测试/部署。所有新API独立数据目录，不导入有旧库副作用的server模块。
- SQLAlchemy2.0.54、psycopg3.3.6、boto3 1.43.108及依赖用uv装入已有项目.venv，requirements已更新。
- 复用本地已有postgres:17-alpine镜像新建隔离测试容器sixnine-platform-test-db-20261004，127.0.0.1:55469，tmpfs测试数据，1GB内存/2CPU限制。数据库sixnine_test/user同名；口令为明确虚假的本地测试字符串，仅该容器使用。不得连接/停止已有crypto-explore-wallet-db等其他容器。结束时只清理本任务测试容器并记录。
- 心跳自动跟进ID：h3-2，每30分钟，截止时暂停。

## 2026-10-03 18:09Z 进度增补（仍在工作，远未到十小时截止）

- 新平台：`studio_platform/` 独立持久库，FastAPI `/v1`，两用户密码/服务身份、私有素材、H3全控制编译、不可变计划、任务/批次、预算、幂等、source snapshot、输出校验已实现并做本地测试。旧data与映序本机草稿未覆盖。
- UI：映序云模式与`/freestyle`共用同一store/项目；章节场景镜头、素材分区、首尾帧/锚点/选段、预检、mock候选与审核均有真实浏览器证据。ReactFlow画布懒加载，正在继续10故事整体UX走查。不是所有图像/音乐/剧本AI配方已接通。
- Worker/队列91项SQLite+真实隔离PG通过：提交不明不重投、重收集、费用pending、物理卡跨池独占、drain持久化、idle心跳、旧表无损ALTER。新fleet supervisor由agent开发，CPU Runner控制私有Comfy，GPU不拿DB/主存储key。
- root新增 `execution_policy.py`：操作员受信配置绑定模型/config/pool/验收/控制范围/预算；到期或缺策略拒绝新生成。9测试通过。worker即将接新提交前guard，已在跑的任务仍核对。`SIXNINE_DATABASE_URL_FILE`安全加载已实现。
- 批次4个故障窗口测试通过：先建planned再链接再排队；崩溃/预算竞争保留job ID与逐项原因，重试不会创建第二次生成。
- 存储：Local/R2/S3，durable receipts+multipart+恢复/配额；Hippius实验adapter因缺conditional create不能接AssetService，未做在线读写。真实浏览器发现client_asset_id冒号误校验，已修并原ID复测通过。agent正在修derive预先预留配额并独立安全review。
- `deploy/platform/`已做9项本地部署验证：私网PG17、非root app、受限role、Caddy域名路由，正式密码/secret来源/DNS/实例尚无。新Docker镜像曾构建与无网Linux完整链路检查通过，但后续新改动需重建。CI目前仍有旧legacy部署段，root还需改成明确平台release。
- 源码快照：映序canonical仍`video-studio-design/studio-app`；`h3-studio/yingxu`只读源码snapshot，49文件、LF规范化、manifest哈希、6检查通过；不要直接编辑snapshot，完成UI后用sync工具刷新。无素材/node_modules复制。
- 预览：`http://localhost:8850` Vite PID43980，后端`127.0.0.1:8845`wrapper PID70776（实际Python子PID另查），DATA `.platform-preview-v2`，local-test+mock明确模拟；实际PID记录在该目录processes.json。CPU mock wrapperPID67784/child29228，已修idle续心跳；不操作旧8843或原data。
- 六小时真实时长CPU稳定性测试已启动：wrapperPID69636，`.platform-soak-overnight-20261004/status.json`，每20分钟两用户各1个4秒mock，真实文件读回校验，每小时只重启自身idle子worker。严格6小时上限，无云/GPU请求。失败先读status类型与该目录error日志，不重复启动。启动前54秒smoke4任务均通过，早先两个smoke脚本问题已修，保留证据。
- 本机PG测试容器仍只有本任务`sixnine-platform-test-db-20261004`127.0.0.1:55469；不要停其他crypto容器。部署agent临时sixnine-deploy-test资源已清完。
- 新文件默认被gitignore拒绝；正式提交前核对allowlist（control、execution policy、deploy/platform需纳入）与secret/data排除，不git add整个未审目录。当前尚未commit/push此轮。

分工现状：root owns API/auth/settings/media/source/batches/policy/CI；storage agent owns storage/assets/deploy/platform及独立安全review；worker agent owns repo/queue/control/worker/fleet；frontend agent ownsstudio-app/src与生成snapshot。修改前协调避免覆盖。

## 待集中报告的问题

AWS区域/正式费用上限、正式用户密码初始化、远端中央秘密加载方式、Hippius/R2目标账号桶与授权验证、GPU真实验收费用。不要为缺这些信息停掉可独立进行的开发，不要在用户睡觉时反复提问。

## 2026-10-03 19:26Z 继续工作交接

- 距10小时截止仍约7小时；不可当已完成而提前结束。h3-2仍ACTIVE，旧h3保持PAUSED。
- `ARCHITECTURE.zh-CN.md`、`ITERATIONS.zh-CN.md`是本轮实际架构和验收记录。域名规划更新为`www.sixnine.art`统一站点，`/freestyle`单次创作，`h3.sixnine.art`重定向；DNS仍未修改。
- CPU章节粗剪已实现，独立于GPU生成开关：`render_plans.py`服务器编译不可变镜头/音轨快照；`render_backend.py`CPU FFmpeg；`render_cli.py`/fleet支持独立CPU槽。480P/720P，24fps，最多50镜头/600秒/32音轨；仅明确音轨混入，不默默拿源视频音轨；源短阻止，模拟来源永久水印。无转场/字幕，真实AI视频生成仍未启用。
- 浏览器新项目“十故事验收 · 雨夜信箱”，supervan，章节chapter-1efda152-893e-420f-8ac6-f0f2b2869e42。静音粗剪74123e48-205c-41f9-8e00-2890dc772d12完整播放；有声42c192d7-afb0-4118-a16a-d32bda958fb4成功12秒720×1280+FLAC。root真实decode音频RMS：0–6.9秒0，7.1–8.9秒0.0388349，9.1–12秒0。MP4/FLAC附件HTTP SHA匹配，跨superdan访问404。Chrome CUA下载事件仍超时，未证明浏览器保存落盘。
- 前端119测试+build；删除图库/造型/角色解绑+一次undo恢复引用，云项目版本15；66文件snapshot已验证，NOVICE-STORIES-AC按单文档白名单纳入。frontend agent继续按章节/状态筛选批量生产、逐镜返修/审片故事。
- ArtifactWriter输出receipt、共享owner10GiB/tenant40GiB配额、>100MiB持久MPU、丢写响应后不fetch/encode/submit恢复已实现。独立审查修复：收集前先持久stage reservation（不是写大文件后才quota）；统一storage_schema与Repository同PG advisory/SQLite immediate DDL锁。storage agent最新148相关测试过。它继续Local SQLite私有备份恢复，禁止备份auth secrets/session与恢复后重投任务。
- queue/control候选只取标量并SQL exact backend/model/config/recipe过滤，锁单job再复核；109SQLite+PG、26workerSQLite+PG已过。queue agent继续list_jobs摘要投影+批量artifacts避免N+1，同时接storage initdb统一表初始化。root API list已加summary=True，相应repo改动正在写入，整套最终回归需代码稳定后跑。
- scaler.py已实现leader/fence/one-create/unknown reconciliation/TTL drain/confirmed destroy与actual账单分离；默认DisabledProvider，没接真实云租赁。fleet可多slot但不自己租GPU。零卡自动冷启动尚未启用；现执行策略需要可用/忙碌的已验收worker。
- release controller `deploy/platform/release.py`已修独立root批准manifest（部署用户不能自签）、exact image archive/root index、旧包重载、同commit重试保留previous、proxy稳定与pending状态；14契约测试过。真实Docker29导出268MB OCI archive检查通过，container.Image与manifest image_id一致。`f...f`40位tag是明确本地未提交源码测试标识，不是Git commit/生产发布。
- 新Docker `sixnine-platform:ffffffffffffffffffffffffffffffffffffffff`非root10001、network none/read-only、1GB/2CPU验证通过：两真实CPU素材+显式音轨→HTTP plan/job→worker→MP4/FLAC private附件→重启持久化；无GPU/云请求。代码仍在改，最终提交后需重建真实commit镜像。
- API当前wrapperPID36344（.platform-preview-v2/processes.json为准）；UI43980。CPU render wrapper28800/workdir .platform-preview-v2/cpu-render-worker与mock wrapper67784仍旧代码，待所有worker/schema改动稳定且idle后定向重载，不杀其他Python。root维护restart-api.ps1可定向重载API。
- 六小时soak仍运行，开始18:07:21Z，结束00:07:21Z；wrapper69636，status.json只读选summary字段避免打印全部samples。19:17检查4批/8任务全验证、1次worker重启、无failure。它每小时滚动加载新worker代码，因此不能声称同一commit连续6小时稳定。
- 本机PG测试容器sixnine-platform-test-db-20261004仍可用（127.0.0.1:55469，synthetic fixture），其他crypto容器不要操作。尚未commit/push；CI只workflow_dispatch deploy=true才部署，normalpush不创建云。最终需中央inbox独立变更记录与源码allowlist检查，不能覆写正式中央目录。

## 验证与迭代

每轮记录假执行/本地真实文件/远端真实GPU的证据等级。重点：跨用户拒绝、幂等、未知提交不重发、下载失败不重生成、并发领取/预算/租机去重、旧镜头结果不覆盖、引导/画布共用状态、重启与断网恢复、私有下载。

## 2026-10-03 21:25Z checkpoint — continued development

- Deadline remains 2026-10-04 02:27:42Z; about five hours remain. Goal and h3-2 heartbeat active; no new GPU/cloud resources.
- Cold-start approval/cycle/waiter ledger + capacity_cli implemented and tested SQLite/isolated PG; CLI defaults disabled, dry-run read-only, advance does not construct provider/scaler. Lium adapter default disabled and independent Boyesir adapter integration_ready=False. Neither is a running production route.
- CPU render v2 supports source-bound 24fps in/out; v1 remains explicit legacy drain. New browser copy job f4de51c0-fee0-4a94-8786-7c7f8714ac6d independently verified 262 frames / 720x1280 / 24fps; FLAC349333 samples at32k, energy only intended5.9167–7.9167s. Both SHA match; sources uploaded mock copies, not real H3 generation. Evidence .platform-preview-v2/trim-browser-verification.json.
- Runtime metadata: API wrapper37992, CPUv2 wrapper42716/workerpreview-cpu-render-v2, instancepreview-cpu-local. Old CPUv1 drained and retired; current files in .platform-preview-v2 are authoritative. Vite8850 wrapper43980; built preview8851 wrapper61436. Do not restart during an active job.
- Frontend159 tests/build +84-file source snapshot passed at trim checkpoint. Next work is one-click same-job standalone FLAC reuse: frontend agent owns canonical UI; packaging agent now owns render_plans/render_backend/project_validation and tests+render docs; root API adds trusted source_job_id/artifact_id and capability, but preview API has not yet reloaded these unfinished changes.
- Queue agent fixed invoice-resolver exceptions wrongly blocking verified artifacts; SQLite/PG targeted tests pass, budget stayspending and later settlement idempotent. Agent now independently reviews API/machine-client/slow-request CPU resource boundaries; root owns API/auth/http_limits modifications.
- PostgreSQL control-plane stress24concurrent/1200requests all200,137.99req/s,p95199.17ms; only inprocess ASGI read workload. Report .platform-stress-20261004-pg-c24/report.json.
- Sixhour soak20/20verified,10bursts,3workerrestarts,failures[] at21:16Z; completes00:07Z. Changes are rolling, not one-commit continuous proof.
- Aligned allowlist/CI for capacity_cli, backup_cli, Boyesir adapter/contract; still no commit/push this run. Pre-existing SCALING.zh-CN.md modification excluded from our staging. Full suite required after current agent changes stable; previous548 run had two stale-loaded deployment errors, affected34 passed/1skip after stabilization, do not claim548allpass.
- Source docs updated distinguish oldserver vsnewplatform and independent Boyesir transport. Central Boyesir-only inbox record exists h3-studio-boyesir-offline-adapter-20261003T205754Z-a57c.md; overall platform record still pending.

## 2026-10-03 22:40Z checkpoint — final-source gate, work continues

- Deadline 02:27:42Z is still about 3h48 away; do not end the ten-hour task early. Goal and h3-2 active; GPU/cloud/DNS untouched.
- Core and UI feature freeze. Latest full Python 655 collected/648 passed/7 skipped; PostgreSQL 361/359 passed/2 skipped. Logs `.platform-preview-v2/regression-20261004-final-source.log` and `regression-pg-20261004-final-source.log`. Stack tool subsequently gained explicit image/revision checks; its six offline tests passed separately. Frontend 200 tests/build; 92-file source snapshot synchronized including final AC document.
- Actual fixes since last checkpoint: same-job generated audio binding, v3 fixed-preset Chinese subtitles, bounded full-ASGI admission/media limits, explicit stream handle cleanup, short-body abort, project/batch SQL scalar summaries, missing/submitted attempt cancellation hold, control slot proof, remote backup TLS fail-closed, and workspace/upload race guards. Canonical frontend remains sibling studio-app; do not edit snapshot.
- Local preview latest API wrapper63796, CPUv3 wrapper50632, mock wrapper67704. Own two workers drained and checked idle before identity-checked reload; data/IDs retained. API8845, Vite8850, built8851 still active. CPUv3 uses explicit WindowsYaHei; Docker uses packaged NotoCJK. Both are CPU-only.
- Caption browser result job f9f48e57-ed0f-4444-a8ca-31424c8cac36: 94frames/720x1280/24fps, cues exactly frames[1,47),[49,91), expected blank boundaries independently decoded; output SHA checked. Source mock, not H3 quality. Other trim/sound receipts remain `.platform-preview-v2`.
- Actual isolated Caddy internal TLS -> password app -> PostgreSQL17 restricted role -> Local network drill passed on precommit image536bb0a318828945138eec69a71e62746a3f75f4c1821493dcb8dbdd124adcfb. Full report deploy/platform/STACK-VALIDATION.zh-CN.md and stack-validation-20261004-final.json. All own labeled test resources removed. CLI now requires --image and commit label match; final committed image needs fresh drill.
- No commit yet. Source allowlist audit258files found only14 explicit test-fixture patterns. Preexisting SCALING.zh-CN.md still MUST NOT stage. Next: commit exact reviewed source, source-only archive/build, final-image Linux/stack/bundle check, privateGitHubpush/CI. CI push is test-only; deployment needs explicit dispatch+runner.
- Six-hour soak remains independent wrapper69636, ends00:07:21Z, 28/28 verified at22:30, nofailure. Read compact status fields only; rolling source means not one-commit six-hour proof. Do not restart it.
- DELIVERY-REPORT.zh-CN.md has product matrix and limits; still draft awaiting commit/CI/soak/deadline. Central own inbox file h3-studio-sixnine-platform-20261003T2140Z-01a0a8bc.md currently stale about v3, update before handoff. Do not change formalregistry/otherinboxes.
