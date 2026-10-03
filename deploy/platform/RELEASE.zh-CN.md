# 映序与 H3 统一平台发布

本文件描述已实现的发布代码与所需主机准备，不表示 AWS 实例、DNS、正式身份或真实生成已经上线。

## 发布边界

- GitHub 的普通 push/PR 只执行测试。`workflow_dispatch` 明确选择 deploy、main 分支测试通过，再进入 production environment。
- production environment 应限制到 main 并配置审核人与分支保护；代码中的 environment 字段不等于这些 GitHub 账户设置已经建立。
- 使用 `Dockerfile.platform` 构建包含映序与 `/freestyle` 的 CPU 镜像。旧 `Dockerfile` 仍做兼容验证，但不再作为此部署工作流的发布镜像。
- 测试后的同一个镜像保存进 bundle，附 commit 与每个发布文件的 SHA-256。SSH 主机密钥固定，不使用自动接受未知主机。
- 目标机器必须预先安装受 root 控制的 `/opt/sixnine-release/release.py` 与同目录 `check_config.py`。不能把 incoming 内脚本直接交给 sudo。
- 控制器只接受完整 40 位 commit。主机根固定 `/srv/sixnine`，只操作本地 Docker socket；不会调用 AWS、改 DNS 或租 GPU。
- 镜像 label 与同包附带的 checksum 不能证明来源可信。主机额外要求独立 root 管理的 `approved-releases/<commit>.sha256`，其内容是经另一可信渠道审核过的 CI release-manifest 文件摘要；部署身份无权写此目录，也不能通过本工作流自行批准。**当前是有审批门槛的持续交付，不是无人确认的生产自动部署。**

## 首次主机准备

由已授权的主机操作员在目标 Linux 机器准备，当前仓库不包含密码或私钥：

1. 安装 Docker/Compose 与 Python 3，Docker受信可执行文件位置固定 `/usr/bin/docker`，拉取并核验固定摘要的 PostgreSQL 17 和 Caddy 依赖镜像。依赖镜像 `pull_policy: never`，发布不会临时拉取未知版本。建立 `/opt/sixnine-release/docker-config` 为root:root0700空目录，发布进程固定使用它，不读取root已有登录/context文件。
2. 建立 `/srv/sixnine`、`releases`、`approved-releases` 为 root 所有且不可组/其他用户写；`incoming` 为 root 所有、只向专用部署组开放写权限。部署身份不加入 docker 组。
3. 按 Compose 的 UID/权限准备 `platform-data`、`upload-spool`、`postgres` 持久目录，不复用旧 H3 数据目录。已有数据的迁移必须另做备份和核验。
4. 通过受保护的运行时秘密来源提供 `/run/sixnine-secrets/db_admin_password` 与 `app_database_url`。后者是限制权限的应用数据库 DSN，使用 `sixnine_app` 角色；不要将内容放进 site.env、CI 输出或仓库。文件须 root 控制、无其他用户权限；应用文件需允许容器 UID/GID 10001 读取。若 /run 重启后消失，须有经过审阅的恢复机制。
5. 从 `site.env.example` 建立 root 所有的 `/srv/sixnine/site.env`，只写镜像摘要和上述秘密文件路径。应用镜像在每次发布中被固定为已测试 commit。
6. 安装并审阅root控制器、`check_config.py`、`preflight_host.py`和管理员专用`bootstrap.py`，目录与源码全部root控制。sudoers只允许专用部署身份执行固定`release.py`；它仍仅接受SHA。**不得把bootstrap.py、python解释器或任意manage命令授权给部署身份**。配置变更通过单独的主机维护发布，不能让部署身份覆盖控制器。
7. 配置经审阅的固定出口或私网部署runner，在仓库变量 `SIXNINE_DEPLOY_RUNNER` 中指定其label；未设置时仅产出测试bundle，不进行生产传输。默认GitHub托管runner的动态IP不在主机管理员 `/32` 中，不能用开放SSH到全网解决。GitHub production 配置 `LIGHTSAIL_HOST`、`LIGHTSAIL_SSH_USER` 变量和既有专用 `LIGHTSAIL_SSH_KEY`、`LIGHTSAIL_KNOWN_HOSTS` secrets。云身份/应用供应商 key 不放入发布 bundle。
8. 首次由管理员通过自己的root终端执行 `/opt/sixnine-release/bootstrap.py <已批准commit>`。它先核对独立批准与bundle，加载同一测试镜像，启动私有db、db-init和固定init-db，再在交互终端为缺少的superdan/supervan分别设置隐藏密码；已有有效账户不会被重置。中途断开保留pending，可用同commit继续。它不启动app/caddy，不自称上线；账户齐备后再调用普通release入口。页面只输入用户名的测试模式禁止公网部署。

秘密加载的远端限制另见 [RUNTIME-SECRETS.zh-CN.md](RUNTIME-SECRETS.zh-CN.md)。本机 Windows DPAPI 加密库不能直接复制到 Linux 使用。

## 一次发布会做什么

控制器先加主机发布锁，校验 incoming 的固定文件集合与哈希，再复制到私有临时目录。复制中断或输入变化不会留下一个看似完整的 release。复核完成后才原子发布 commit 目录。

独立发布批准要先于 root 复制大文件。主机还检查镜像 archive 只有一个预期镜像/tag，并绑定 Docker image ID；拒绝额外镜像、重复路径、链接和超出解包限额。运行容器的实际 image ID 与健康状态也须匹配。当前 Linux/Docker archive 的真实格式需要随最终镜像再次验证，不能只依赖伪造 tar 单元测试。

可在今后用受信构建签名或 GitHub artifact attestations 取代手动批准。官方文档说明私有/内部仓库使用该能力需要 GitHub Enterprise Cloud；本仓库账户资格尚未确认，所以本轮不设置一个可能无法工作的签名步骤，更不让 incoming 自己声称“已签名”。[GitHub 官方说明](https://docs.github.com/en/actions/how-tos/secure-your-work/use-artifact-attestations/use-artifact-attestations)

随后验证 Compose 的端口、网络、UID、权限、资源限额、精确挂载路径、秘密引用及镜像身份；加载测试镜像并核对 ID/revision 标签。启动私有数据库、运行幂等角色初始化、启动应用并等待健康，再更新 Caddy，检查代理保持运行且没有连续重启。状态保存到 `/srv/sixnine/release-state.json`；`current` 是上次确认版本，`pending` 是进行中的版本，不能把中断发布当作成功。

发布状态固定记录首次批准的Postgres/Caddy镜像digest。普通应用发布必须保持这两个digest相同，修改site.env依赖会在启动前拒绝；升级依赖走独立维护流程。这样不会在应用回退时继续使用一次悄悄升级的坏Caddy镜像，也不自动降级Postgres。旧发布状态若缺依赖身份，须管理员核对后补记录，不能猜。

如应用启动失败且存在上一版，回退上一版应用与Caddy配置，Caddy二进制保持上述固定digest；不回滚数据库、不删除素材、不执行旧的数据库代码降级。数据库schema变更因此必须保持至少前一版应用兼容；无法兼容的迁移需要独立维护方案。

回退前从旧的已批准 bundle 重新加载并核验 image ID，不能依赖可能被重标的旧 tag。丢失 SSH 响应后重试同一个已就绪 commit，只核对实际容器并保持历史 previous，不把 previous 改成自身。首次失败或回退失败会记录待核对状态；同一 pending commit 可恢复，切换另一 commit 前须由主机操作员核对并处理未决状态。

首次部署没有可回退的上一版，失败时保留数据并报告未就绪。应用健康不代表域名解析、TLS、R2 CORS、真实 GPU 或第三方模型已经验收，须分别记录证据。

## 本轮验证

发布文件测试覆盖：commit/文件集合、篡改、硬链接、复制中断再试、原子发布、不复制 incoming 控制脚本、子进程不继承云凭据环境，以及 CI 发布目标。Compose 和隔离 Linux 数据库验证由 `test_platform_deployment.py` 覆盖。

尚未执行真实主机发布；没有依据上述文件创建云资源、设置密码或修改域名。版本留存、旧镜像清理与备份期限待确定，发布程序不自行删除旧版或用户数据。
