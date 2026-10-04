# H3 Studio

2026-10-04 十小时实现与本轮验收已完成：新的统一平台入口是 `platform_app.py` / `studio_platform/`，使用独立数据库与对象根，不覆盖旧 `data/`。映序 canonical 源码在 `../video-studio-design/studio-app`，`yingxu/` 是由 `tools/sync_yingxu_source.py` 产生的只读发布快照。当前架构见 `ARCHITECTURE.zh-CN.md`；新生产包在 `deploy/platform/`，旧 `deploy/` 只保留兼容。不能把下文初始“只有设计”的状态当作新代码未实现，也不能把本地代码/模拟验证当作公网或GPU已上线。

新平台已实现 PostgreSQL/SQLite 持久任务、计划/预算、worker/fleet、受限私有存储、云项目/素材及CPU章节粗剪；运行证据见本轮验收记录。真实GPU默认关、云创建上限0、scaler默认dry-run；FakeProvider和模拟视频不是线上模型验收。Hippius只为实验adapter，不能替换缺少条件创建保证的AssetService。远端Linux中央凭据加载尚未完成，不复制DPAPI库或生成项目.env。

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
