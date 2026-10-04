# 映序 Agent API 与公网发布验收

日期：2026-10-04，Australia/Sydney。此文件在本轮验收过程中更新；下列每项按实测范围记录，尚未完成的部分不视为已上线。

## 公网站点与账户

- 网址：https://www.sixnine.art 。Namecheap `@` A 记录为 `18.136.57.227`，`www` CNAME 指向 `sixnine.art`；公网 TLS 正常。
- 正式密码账户：`superdan`、`supervan`。两账户作品、素材、任务和 Key 按 owner 隔离。没有公开免密码登录、自注册或团队共享权限。
- 首次密码由服务器生成，保存在 AWS 新加坡区 Secrets Manager `/sixnine/platform/bootstrap-accounts`；在用户自己的 AWS 控制台查看。登录网站后可修改密码；修改后会撤销该账户旧会话和 Key，AWS 条目仍只代表初始密码。密码不写在本文、GitHub、项目环境文件或聊天中。
- 网页右上角「登录云工作室」→账户界面→API Key：创建、设置范围/到期日、仅一次查看/复制、撤销。可以授权全部现有及未来故事，或只选指定故事。
- 网站仍支持本机草稿；本机草稿不会自动成为云项目。先登录并选择云故事，再让 Agent 使用该项目 ID。Agent 更新后网页提示有新版本，由用户明确载入，避免覆盖未保存编辑。

## Agent 可以做什么

已提供真实 API：多故事创建与列表、章节/场景/角色/镜头等实体编辑、关联素材、画布位置与连线、创作流程设置、参考文件上传/读取、生成计划与任务、结果下载与采用、镜头裁切、字幕和声音编排、JSON/CSV/SRT 导出。完整字段通过 `/v1/guided-schema` 提供；`/v1/agent-guide` 给调用顺序。

机器 Key 不可管理 Key 或密码。修改支持版本冲突检查；创建和付费任务支持幂等键。已实现的 H3 接口保留模型控制参数，执行时还须符合当前 GPU 池实际验收范围和预算，不能把 UI 中存在选项当作线上已验收。

Agent Skill：仓库 `skills/sixnine-yingxu/`，网站授权下载 `/v1/agent-skill.zip`；本机也已安装到 `C:/Users/danmo/.codex/skills/sixnine-yingxu`。让 Agent 使用用户在网站创建的 Key，不使用供应商 Lium/Engy Key 作为网站登录凭据。Key 只进进程环境或中央加密库，不贴进提示词或普通配置。

尚未实现的应用能力：自动 LLM 剧本写作、未配置的图片/音乐/Marble 生成、跨账户团队成员协作、服务端媒体打包 ZIP。章节 CPU 粗剪代码有独立接口，但本轮初期公网主机尚未开放粗剪 worker。

## 已完成的公网 API 验收

首次成功版本：`4b8b5e97c202f0bb56a192cf2796f89531a4c522`；GitHub 完整 CI `37176927083` 成功。首次批准 manifest SHA256：`5da4d5956da30fc55abad7bd4f240f41e16732e44aa286c54d9f33aa52ce7b6c`。

2026-10-04 04:42:39 UTC，真正公开 HTTPS 请求通过：

1. 未登录访问受保护 API 返回 401。
2. 两账户密码登录、各自创建临时限权 PAT。
3. PAT 创建故事，重复相同幂等请求返回同一结果；创建章节/场景/镜头。
4. 浏览器会话 API 与 Agent API 读到相同持久文档；过期编辑版本返回 409。
5. JSON 导出、仅含两个受审查文件的 Skill ZIP 下载。
6. 跨账户故事读取返回 404；机器 Key 管理账户 Key 返回 403。
7. 验收临时 Key 全撤销、会话退出，撤销后 PAT 返回 401。

证据：`.platform-agent-e2e/public-api-verification.json`、`public-login.png`。保留的示例云故事：superdan `project-68117e8fcc3253fb8c05b6791149d4f5`；supervan `project-5e695aa8ce7058aab0c1ed9e4acf0238`。

## CI/CD 与服务器

私有仓库：https://github.com/apedintensor/h3-studio 。main push 先执行 Python/前端、PostgreSQL 并发、非 root Linux 容器及发布包校验，再以 GitHub OIDC 将同一测试过的镜像/manifest 发布到私有 S3。镜像身份同时验证经典 Docker config digest 与 containerd/OCI manifest/index，不能仅放松标签检查。

正式站点运行 AWS 新加坡 EC2 `i-03d81d2d153b5e2fd`，2 vCPU / 4 GiB，PostgreSQL、API、Caddy；数据在加密 EBS 上。只有 HTTPS/HTTP 公开；管理使用 SSM，没有公网 SSH/数据库/ComfyUI 端口。凭据在 AWS Secrets Manager，容器读 `/run` secret 文件。

常规发布为明确选择已批准 commit 的 GitHub workflow：独立 OIDC 身份只能调用固定 SSM Document，不能执行任意 shell、自批 manifest、读取密码或租 GPU。主机独立核对 manifest SHA。GPU 验收期间常规 CD 被阻止，避免发布误停仍在生成的 worker。2026-10-04 05:04 UTC，GitHub workflow `37178771109` 的 `deploy-approved-aws` 成功：OIDC → 固定 SSM Document → 主机批准摘要 → 已批准版本 `4b8b5e9` 的部署及健康检查全部实际执行，SSM 收据 `65ab78c9-2cfb-4bfb-8e6a-e20c7c7f7f69`。这不是每次 push 自动无审批替换生产。

本轮没有把用户素材迁到 S3/R2/Hippius。S3 当前存的是发布包；保留 EBS 和代码回滚不等于异地数据库/素材备份。

## GPU 验收中的范围

本轮 GPU 总预算上限 US$50，不自动充值；实测机器为 Lium 两台单卡 RTX PRO 6000 Blackwell Server 96GB，每台报价 $1.29/h，保守接受上限 $1.40/h，设置供应商 TTL。这不是本轮 RTX5090、B200 或单任务多卡张量并行成绩。

已实测首台完整 BF16 FL2VA，CPU encoder、tiled video VAE、原版 H3：50 步、1344×768、请求 5 秒、音频开启，无 OOM 或静默降精度。Comfy 执行 554.50 秒，提交至本地下载/完整解码校验 559.91 秒。原生结果约 5.167 秒 MP4 + 5.175 秒 FLAC；约 $0.20/段仅为该段推理时间按租价换算，非包含冷启动/闲置的最终账单。

本地隔离业务 API 已提交两账户共 8 个真实任务，队列触发第二台自动创建、安装、smoke 和 worker 注册；两物理 GPU 已同时忙碌。完整结果核验、自动缩容、Ref 多模态 smoke、正式公网 job→GPU→artifact 回写和最终费用/销毁证据仍待完成。

最终报告将分别标出隔离两机扩容与正式生产单机交接，不能把前者称为已部署持续生产自动租赁服务。原生命周期控制器是唯一供应商管理者，交接只移交推理队列。测试结束恢复关闭新生成并销毁测试 GPU，网站与作品保留。
