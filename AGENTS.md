# H3 Studio

## 当前授权与部署进展（2026-10-04，本段优先于下方历史状态）

快速创作修复：方式选择移到素材区上方，接口按 `execution_support` 展示真实开放范围。可选 `fl50-firstlast4-ref4-v1` 仅允许受控排队；策略 `runtime_required` 不能写成已完成GPU实测。每台新GPU完成FL50及4步首尾帧/Ref兼容性检查后才注册工作机；仍由真实用户确认任务触发租赁，业务空闲600秒停机。控制与保守输入限制、验收边界见 `FREESTYLE-ADMISSION.zh-CN.md`；部署回执在 `.platform-demand-live/`，不能用本地代码存在代替线上状态。旧池有任何已接收任务都不得执行空池换版；预算与原窗口不得在升级时重置。

最新用户调整：不保留常驻 GPU；只有真实用户确认生成任务后才允许启动，队列与生成/结果收集全部结束后连续空闲 600 秒才关闭。准备常驻时未发送租赁请求。两账户所有故事共用按需单卡池，新增/实验扩容不可打断已接收任务。GPU 累计 US$50 原预算仍有效，旧最终账单 US$3.490345，运行期不能自动充值或提高额度。按需实现与实际部署/验证状态分别见 `ON-DEMAND-GPU.zh-CN.md`，不能把离线测试写成新 GPU 线上实测。

新增前台接入任务：用户要求分享网站给 Codex 即可发现 Skill/API，并在网页查看 Agent 的创作进展、定位与调整。实现和验收见 `AGENT-ONBOARDING.zh-CN.md`。这次仅发布网站/API 接入改进；GPU 暂停状态不变。公共发现入口不授予私有故事访问权；Key 仍由账户所有者在网页显式创建。

最新用户指令：先收尾后端，暂停进一步扩容实测，完成 front-facing / onboarding 后再继续。两台旧测试 GPU 在随后“留一个”的消息到达前均已销毁，未重租；网站仍运行，生成已关闭。新增有限 controller 代码默认禁用、尚未部署，交接见 `BACKEND-HANDOFF-20261004.zh-CN.md`。恢复租赁/实测前需要新的继续指令，不能据下方历史预算自动重开。

用户已新授权：完成映序 Agent API、账户自助 API Key、部署公开网站，并实际验证多 GPU 队列与自动扩容。GPU 测试累计上限 US$50，不自动充值；其他设施初期预算目标低于 US$50/月。测试 GPU 完成后销毁，网站保留运行。历史关闭 GPU 指令不禁止此次已授权测试；不把测试额度视为无限持续租用授权。

已创建新加坡 AWS EC2 CPU 控制主机 `i-03d81d2d153b5e2fd`，固定 IP `18.136.57.227`，六九域名 DNS 已切换；资源记录见 `deploy/platform/ec2/`。`https://www.sixnine.art` 的密码登录、账户隔离、PAT、故事编辑及结果回写已实际验收。GitHub OIDC 发布测试后的 S3 release bundle，独立部署身份通过固定 SSM Document 执行确切版本；主机仍须独立批准确切 manifest SHA。`fc91fc7` 的 CI/CD 均成功。密码与数据库凭据留 AWS Secrets Manager，站点容器通过 `/run` 文件读取；不得读出值到会话/日志。生产初始账号通过服务器内部安全管道建立。

原 GPU 隔离测试控制器状态见 `.platform-gpu-live`：两台单卡 PRO6000 96GB 的 8 个 FL50 任务成功、Ref50 多模态输入成功；正式生产单机交接作业 `1816736a-49f2-4be4-89a3-0f1ad1e7bfb2` 已通过公网生成/下载/采用/会话一致验收，随后安全恢复 CPU 模式。原控制器负责该两台实例销毁；新生产有限扩容控制器须在旧周期全部销毁并完成持久账本核验后才可单独启动。两个单卡任务并发不等于单个任务张量并行；本轮未测 5090/B200。

2026-10-04 十小时实现与本轮验收已完成：新的统一平台入口是 `platform_app.py` / `studio_platform/`，使用独立数据库与对象根，不覆盖旧 `data/`。映序 canonical 源码在 `../video-studio-design/studio-app`，`yingxu/` 是由 `tools/sync_yingxu_source.py` 产生的只读发布快照。当前架构见 `ARCHITECTURE.zh-CN.md`；新生产包在 `deploy/platform/`，旧 `deploy/` 只保留兼容。不能把下文初始“只有设计”的状态当作新代码未实现，也不能把本地代码/模拟验证当作公网或GPU已上线。

新平台已实现 PostgreSQL/SQLite 持久任务、计划/预算、worker/fleet、受限私有存储、云项目/素材及CPU章节粗剪；运行证据见本轮验收记录。常规 CPU 发布默认关闭真实生成和云创建；有限 GPU 验收必须独立启用并阻止并行 CD。FakeProvider不算线上验收。Hippius只为实验adapter，不能替换缺少条件创建保证的AssetService。Linux使用已明确授权的AWS Secrets Manager `/sixnine/platform/lium` 固定版本作为同一 `lium/lium--rig-root` 的加密运行时来源；host复用 `AwsLiumLoader`，一次内存stdin交付controller，不复制DPAPI库、不建项目.env、不打印秘密。导入通过不等于新生产租赁已验收。

`SIXNINE_RENDER_ENABLED` 单独控制CPU粗剪；不通过开启它恢复GPU。正式站点必须密码认证，`local-test`和mock仅隔离loopback测试使用。公网发布仅使用测试后的 `sixnine-platform:<commit>`；主机要求独立root批准manifest摘要，部署身份不得覆盖host controller或自批准。处理项目删除/模型迁移仍需先读中央HOUSEKEEPING并核实云账单与唯一资产。

2026-10-04 早期独立服务部署记录（以下段落不描述新的统一平台）：GitHub CI 与 Lightsail CPU 发布配置见 `LIGHTSAIL.md`。公网容器固定 `H3_AUTH_MODE=password`、`H3_GENERATION_ENABLED=0`，不包含中央 DPAPI 库、GPU 控制器、SSH 文件或用户数据。正式密码仅由目标服务器 `tools/manage_users.py` 交互建立；不要写入聊天、代码或普通配置。本机默认 username-test 仅兼容历史测试，不能当正式认证。多 GPU / API 路由当前仅设计，见 `SCALING.zh-CN.md`，不得写成已运行自动扩容。现有 default AWS CLI 指向第三方存储，不能当 Lightsail 账户使用。

本目录是用户授权部署的 MiniMax H3 多模态参考工作台和异步 API。
中央资源入口及凭据加载规则继承父目录 AGENTS.md；Lium 使用 lium/lium--rig-root，加载父目录 h3-benchmark/central_api.py，不复制凭据。
自部署使用非量化 BF16 FL2VA/Ref2VA、BF16编码器文件（Comfy运行时加载FP16）、FP16视频VAE和FP32音频VAE；公开Base checkpoint本身经过CFG蒸馏。不能冒充尚未开源的官方 Context-IR / 2K regeneration。
最新状态（2026-10-04 00:22:24 悉尼时间）：用户明确要求关闭GPU，已销毁本轮 Lium pod `f14b5efb-0050-481e-91e5-267ccfe4be22`，DELETE成功后再次查询供应商pod列表，目标实例已不存在。`cloud-state.json`为DESTROYED，证据见`gpu-shutdown-receipt.json`。该指令取代先前继续运行的要求；未经新的用户授权，不重启、重租、续租或充值。保留本地网页、账户、SQLite、素材和成片；GPU生成当前不可用，不能把本地网页可打开写成推理在线。停机前费用快照约$6.41，不是最终账单。
历史：首轮云端为单卡、初设3小时TTL；2026-10-03用户曾要求“服务器不要停，我们没做完”，当时取消自动销毁并核验RUNNING，按$1.46/小时持续计费。原初始测试预算、TTL与继续运行规则仅保留为历史，不构成当前运行或重建授权。
UI和API仅绑定loopback，通过SSH隧道访问云端ComfyUI，不公开无鉴权GPU控制接口。媒体/任务/输出属于项目专属资产；删除目录不会停止云端计费。
每种输入必须经服务端实际解码并检查时长、尺寸、数量，不能仅信浏览器或文件扩展名；不能宣称未完成的输入推理测试已通过。

2026-10-03新增双用户测试：superdan/supervan，仅用户名登录，HttpOnly会话和SQLite owner隔离；旧素材/任务归superdan。免密码不验证身份，不可称为生产认证。所有素材/任务/参考/锚点/取消/下载端点必须从会话取得owner，不能信客户端owner，也不能用匿名默认superdan绕过。内部worker可以用可信owner读取；测试导入server必须指向临时DATA，不触碰生产SQLite或调度GPU。
当前网页仍本机入口，公网研究建议AWS EC2常驻CPU应用+现有Lium GPU，未创建AWS/Vercel资源。原生ComfyUI仅管理用途，保持loopback，不给其他用户开放。已有API脚本需要先登录并保留进程内Cookie容器，不打印session值。更改API版本/资源须向中央inbox提交独立记录，用户名账户不是中央服务商API profile。
