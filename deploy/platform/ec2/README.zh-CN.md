# 初期 EC2 控制主机（2026-10-04）

此目录是实际创建的两账户 CPU 控制主机记录，取代此前仅研究的 Lightsail 计划；没有创建 GPU。实例 `i-03d81d2d153b5e2fd`，新加坡 `ap-southeast-1`，固定公网地址 `18.136.57.227`。完整非秘密 ID 见 `resource-receipt.json`，启动参数见 `launch-spec.json`。不要直接重跑创建来更新网站，应复用此实例和已有独立批准的发布流程。

## 容量与费用边界

- `t3a.medium`：2 vCPU / 4 GiB，CPU credit **Standard**，用尽 burst credits 后吞吐会下降，不会产生 Unlimited surplus 费用。
- 官方 Pricing API 当次 Linux shared On-Demand 为 $0.0472/h，按 730 h 约 $34.46；40 GiB gp3 估计 $3.84，单公网 IPv4 约 $3.65，固定部分约 **$41.95/月**。Secrets Manager、S3、请求和出网另计；$50 是初期预算目标，不是 AWS 自动硬限额，也没有购买承诺或充值。
- 初期 steady 容器限额：API 2 GiB、PostgreSQL 512 MiB、Caddy 128 MiB，总 2.625 GiB；db-init 另 256 MiB，仅初始化。1 个 Uvicorn 进程，generation/render/cloud creation 默认关闭。
- 本机真实 Linux 容器验证（冻结 b588 镜像、无网络、UID10001、2 GiB/2 CPU）：5760×5760 / 15 s 合成视频 HTTP 上传，两个账户并发 5760² PNG，两个账户各 51 个项目成功；内存峰值 1,409,675,264 B，OOM 0、failcnt 0。报告原件 `.platform-ec2-capacity-20261004/ec2-2g-capacity.json`。合成低复杂度素材不等于全部编码器/恶意素材/用户规模的最坏上限，不是 GPU 吞吐测试。
- 独立 512 MiB PostgreSQL 17 容器（network none、临时 tmpfs、仅合成数据）初始化、1 万行、两个并发事务各更新 5 千行成功，峰值 92,631,040 B、OOM/failcnt 0。它验证小规模启动和基本事务，不能代替真实应用 PG 负载验收。
- 服务器 Docker 实报 Linux、2 CPU、可用 RAM 4,031,000,576 B，根卷 38 GiB 文件系统、基座安装后约 36 GiB 空闲。后续用户素材和镜像会消耗空间。
- CPU 粗剪 worker 不在此预算内，未在主机启动。需要单独评估内存/CPU或独立主机后再开放；GPU worker 在独立实例部署。

## 基座与身份

Canonical 官方 Ubuntu 24.04 AMI owner `099720109477`；AMI 已通过 DescribeImages 核验。独立安全组仅开放 TCP 80/443 与 UDP443，无 SSH、数据库或应用直连端口。主机通过 SSM 管理，不创建 SSH key。IMDSv2 必需、hop limit=1；Web 容器无云身份访问，不能把主机 role 凭据传进去。

`bootstrap-host.sh` 只装 Docker 官方 apt 包、python3-boto3、SSM，并创建保护目录；不启动应用，不创建/打印账户密码。实际安装：Docker 29.8.2、Compose 5.6.0、containerd 2.3.6、Ubuntu python3-boto3 1.34.46；包版本写入主机 `/opt/sixnine-release/host-bootstrap.json`。未来重建需重新核对 apt 版本，脚本并非历史包镜像锁。

主机 IAM role `sixnine-platform-ec2`：AWS managed SSM core；另仅两个精确 Secrets Manager ARN 的 Get/Describe 和本项目 release bucket `releases/*` GetObject。首次空条目初始化时短暂授予两个精确 ARN 的 PutSecretValue，成功后已移除。无 GitHub credential、无 S3 写权限。

Secrets Manager 名称：`/sixnine/platform/database`、`/sixnine/platform/bootstrap-accounts`。不在此记录值。服务器内部生成初始高熵密码，bootstrap 条目仅是初始密码；用户网站改密后此条目不会跟随更新。用户可在自己的 AWS 控制台安全查看。网页与 Agent API 不公开自注册。

## 重启与首发

`sixnine-secrets.service` 在 Docker 启动前，从 SM 只读恢复 `/run` tmpfs 文件；失败会阻止 Docker 启动。secret-root 是 root:root 0700，admin 文件 root:root0400，app DSN 文件 root:10001 0440。仅缺少的空文件会写入；已存在内容相同时不截断、不换 inode，不同则拒绝自动轮换。运行时不把秘密写入磁盘 `.env`、SSM output 或进程参数。`site.env` 只包含非秘密镜像/路径引用。

root-owned `/opt/sixnine-release` 已安装 review 过的 controller、校验器、SM hydration、S3 fetch 和 `aws_bootstrap.py`。`deploy` 是 nologin 用户，无 Docker group 或任意 sudo，incoming 可写不等于有发布授权。

首发由操作员通过独立可信 SSM 通道执行，`COMMIT` 和 `MANIFEST_SHA` 必须来自已核对的 CI artifact，不能信任下载内容自称的 digest：

```sh
# 以下两个变量均非秘密；填入独立核对的完整 40/64 位十六进制值。
printf '%s\n' "$MANIFEST_SHA" > "/srv/sixnine/approved-releases/$COMMIT.sha256"
chmod 0644 "/srv/sixnine/approved-releases/$COMMIT.sha256"
python3 /opt/sixnine-release/fetch_release_s3.py "$COMMIT"
python3 /opt/sixnine-release/aws_bootstrap.py "$COMMIT"
python3 /opt/sixnine-release/release.py "$COMMIT"
```

fetch 先验证独立批准，再下载镜像，限大小、逐文件验 SHA、不执行下载脚本。bootstrap 只用于未发布首发，仅补缺账号，SM→进程 stdin 私有管道→`Auth.set_password`；不会覆盖已有账号，不公开应用。release 再核验 manifest、单镜像身份、配置与健康，最后开 Caddy。应用 source bundle 和 host controller 的可信边界独立，不弱化旧 release approval。

正式依赖已按服务器实际拉取的 RepoDigest 固定：

- `postgres@sha256:b0f9560a2de083e2cc7382e75f808c7381a32852a7ec49117deedb300e552b24`
- `caddy@sha256:4c6e91c6ed0e2fa03efd5b44747b625fec79bc9cd06ac5235a779726618e530d`

**本记录的完成界限是主机准备，不代表网站、TLS、两账户公网使用或实际 H3 推理已验收。** 首发与公网实测由根任务继续记录。

## 删除及恢复影响

termination protection 已启用；根 EBS `vol-05d7deeef75eef365` 加密且 `DeleteOnTermination=false`，防误终止同时删数据库/素材。停止或终止 EC2 **不会**自动删除保留卷、EIP、SM、S3，也不会停止它们各自的费用。现在网站数据在本机 `/srv/sixnine`，不是自动 R2/S3 双读或迁移；发布 rollback 也不是数据库/素材备份。备份/跨存储迁移需另按现有方案显式实施，不能因有保留卷就称已异地备份。
