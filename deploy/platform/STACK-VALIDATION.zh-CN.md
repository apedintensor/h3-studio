# 独立本地 HTTPS / PostgreSQL / Local 全链路验收

时间：2026-10-04 Australia/Sydney（最终完成 2026-10-03 22:27 UTC）。这是本地 Linux Docker 实际联调；没有创建 Lightsail、改 DNS、请求 ACME、开启 GPU 或调用供应商。

入口：[tools/check_platform_stack.py](../../tools/check_platform_stack.py)。完整非秘密结果：[stack-validation-20261004-final.json](stack-validation-20261004-final.json)。测试对象是已经存在的镜像，不是在宿主进程中用 TestClient 替代网络。

| 已存在镜像 | 实际 image ID |
|---|---|
| sixnine-platform:captions-precommit-20261004 | sha256:536bb0a318828945138eec69a71e62746a3f75f4c1821493dcb8dbdd124adcfb |
| postgres:17-alpine | sha256:b0f9560a2de083e2cc7382e75f808c7381a32852a7ec49117deedb300e552b24 |
| caddy:2.10.2-alpine | sha256:4c6e91c6ed0e2fa03efd5b44747b625fec79bc9cd06ac5235a779726618e530d |

## 实际拓扑及秘密边界

每次新建一个随机前缀及唯一 label 下的 6 个容器、2 个 internal 网络和 4 个独立卷。数据库、媒体、证书均为新测试数据，不挂载原 `data/`、预览目录、既有测试数据库或其他项目。没有任何宿主端口，客户端只在测试网络中连接 `https://stack.test` 的 Caddy 网络别名。

Caddy 使用 `tls internal` 和 `skip_install_trust`。新普通客户端确实报证书验证失败；只有本次 Python 客户端的 SSLContext 加入临时 CA **公钥**后才连通，仍执行主机名与证书校验。没有把 CA 安装到 Windows、Docker 宿主或任何持久客户端信任库。TLS 私钥仅在专用 Caddy 卷，验收结束一同删除。

测试每次在内存产生随机 DB 管理员密码、不同的应用密码及两个用户密码，全部经 `docker exec` 标准输入传入。只有数据库 fixture 在临时 tmpfs secret 卷中按生产权限契约保存；没有放进 Docker argv、容器配置环境、镜像、报告或日志。账户口令不落临时配置文件，会话 Cookie 只留测试客户端进程。所有测试容器禁用日志驱动，程序异常只输出静态原因代码。

真实 `init_database.py` 初始化 PostgreSQL 17 的 `sixnine_app/sixnine`，实际连接验证角色无 superuser/createdb/createrole/replication/bypassrls。应用 UID 10001 能读取应用 DSN 文件，不能读取 DB 管理员密码文件。单 Uvicorn worker 只信任 Caddy 的本次固定内部 IP；Caddy 覆写 X-Forwarded-For，不传递客户端伪造来源。

## 已通过的实际请求

- HTTPS 首页、`/freestyle` 与健康检查；生成、粗剪、云创建全部关闭，execution backend 为 disabled。
- 匿名访问项目返回 401；两账户通过真实 bcrypt 密码登录，Cookie 带 Secure/HttpOnly/SameSite=Lax；注销后原会话不可用。
- 实际保存章节/场景/镜头项目，上传合成 PNG，经 CPU 归一化成为 ready 素材；业务素材 ID 含冒号的前端格式可正常工作。
- 下载字节与原图完全一致；Range 返回正确 206/Content-Range/对应字节，HEAD 长度正确且无正文，越界 Range 为 416，附件下载有 attachment 头。
- supervan 看不到 superdan 的项目、素材及任务，单对象和 Range 均返回 404；跨 Origin 写入被拒，旧标签页预期账户不一致返回 409。
- 创建 H3 计划及任务仍明确 blocked；重复同幂等请求返回同一 job。真实 PG 查询最终 attempt 数为 0，没有偷偷投递推理。
- 真正重启 PostgreSQL 容器和应用容器后，原登录会话、账户新登录、项目、素材原字节及 blocked job 均保留。
- 客户端 A 的合法登录加错误登录达到每源 5 次限额；不断替换伪造 XFF/X-Real-IP 仍得到 429。另一固定 IP 的客户端 B 仍可登录，证明此拓扑没有将所有用户压成代理 IP。
- 客户端 B 绕过 Caddy，直接向内部应用发送伪造 XFF/X-Forwarded-Proto；Uvicorn 不信该来源。最终 PG 登录来源 hash 集合恰好对应两个真实客户端地址，没有 Caddy 或任一伪造地址。

最终运行 ID 为 `9ed02a47240c4680ab8203d32f2d939e`。工具按“本次记录名 + 唯一 label”逐项删除自己的资源，之后分别按 label 查询容器、网络、卷都为空。另一次独立查询该工具 label 的全部测试资源也为空，没有操作既有容器或执行 prune。

## 复现与失败行为

```powershell
# 只在授权的本地 Docker 环境执行。缺少任一已存在镜像就失败，不 pull。
.venv\Scripts\python.exe tools/check_platform_stack.py --image sixnine-platform:captions-precommit-20261004 --report C:/绝对路径/全新的测试结果.json

# 纯假对象回归，不运行 Docker。
.venv\Scripts\python.exe -m unittest test_platform_stack -q
```

Windows 固定使用本地 Docker Desktop Linux named pipe；Linux 使用本地 Unix socket，不继承远程 DOCKER_HOST。Docker CLI 使用本次空配置目录，避免复用其他项目登录/context。工具检查输入报告是新绝对路径，不能覆盖旧报告。

工具现要求显式 `--image`，没有默认历史镜像；最终版本使用 `sixnine-platform:<完整40位commit>`，并在创建资源前检查 revision label 一致。报告分开记录 `release_commit` 与 `precommit_evidence`；历史 JSON 保留原证据，不重写为新版本成功。执行前必须在对应 commit 的已审阅源码目录中运行，因为工具会只读挂载当前 `init_database.py`；新报告附该文件 SHA-256。

6 项工具安全回归通过：错误 label 在删除前拒绝、只删除登记的本次资源且复查为空、容器创建成功后即使启动失败仍会纳入清理、子进程异常不输出合成秘密，以及显式镜像选择/commit标签不一致拒绝。开发阶段 fixture 曾先 chown 再 chmod 导致权限失败，程序失败并清理；调整顺序后，两次完整 HTTPS 链路通过。这里修的是新测试工具，不是绕过生产权限要求。新增 commit 参数只做了离线回归，等待最终镜像再跑整栈。

程序正常失败会进入 finally 清理；进程被强制杀死或宿主断电时无法保证 finally 已运行。应按失败报告的唯一 `art.sixnine.isolated-stack-check=<run_id>` label 核查，不能按宽泛容器名称或 prune 删除别的项目。

## 尚未证明的部分

本次临时 Caddy 配置只验证 TLS 内部 CA 与代理信任；不是正式 production Compose 的完全复制，也没有执行 root 主机发布器、真实加密秘密供给、GitHub SSH 发布、公众 CA、外网 DNS 或跨公网网络。测试镜像是上表固定预提交版本，后续最终 commit 镜像仍需自己的验收。

没有验证实际吞吐/压力、长视频、大规模数据库、离机备份、生产 PostgreSQL 恢复、R2/Hippius/S3、供应商模型或 GPU。Local 两容器重启证明持久性，不是系统断电恢复或异机冗余证明。公网和运行秘密等剩余条件继续见 [READINESS-REVIEW.zh-CN.md](READINESS-REVIEW.zh-CN.md)。

## 最终 commit 镜像的验证与归档次序

以下是待最终 commit 镜像已经构建完成后的建议命令，本轮冻结复核没有执行它们。先确认当前 checkout 是该 commit，且部署源码没有未提交修改；不能把旧 precommit 镜像重标一个新 SHA 就声称代码已重建、验收。`org.opencontainers.image.revision` label 是一致性标记，不是来源签名。

```powershell
$releaseCommit = (git rev-parse HEAD).Trim()
if ($releaseCommit -notmatch '^[0-9a-f]{40}$') { throw 'Expected full commit' }
$runStamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ')
$stackReport = Join-Path (Get-Location) "stack-$releaseCommit-$runStamp.json"
$releaseBundle = Join-Path (Get-Location) "platform-release-$releaseCommit-$runStamp"

# 仅使用已有镜像；实际创建本机临时容器，完成后清理。
.venv\Scripts\python.exe tools/check_platform_stack.py --image "sixnine-platform:$releaseCommit" --report $stackReport
if ($LASTEXITCODE -ne 0) { throw 'Stack check failed; do not package as verified' }

# 保存同一个已测试镜像；不重新build、不pull、不启动发布服务。
.venv\Scripts\python.exe tools/build_platform_release.py $releaseCommit $releaseBundle
if ($LASTEXITCODE -ne 0) { throw 'Bundle build failed; keep incomplete directory for review' }
.venv\Scripts\python.exe tools/check_platform_bundle.py $releaseCommit $releaseBundle
if ($LASTEXITCODE -ne 0) { throw 'Bundle verification failed' }

$stackEvidence = Get-Content -Raw -LiteralPath $stackReport | ConvertFrom-Json
$bundleEvidence = Get-Content -Raw -LiteralPath (Join-Path $releaseBundle 'release-manifest.json') | ConvertFrom-Json
$testedImageId = $stackEvidence.images.PSObject.Properties["sixnine-platform:$releaseCommit"].Value
if ($testedImageId -ne $bundleEvidence.image_id) { throw 'Packaged image differs from tested image' }
```

预计额外预留：整栈 1–3 分钟；docker save、压缩及逐层流式验包 2–8 分钟，取决于本机磁盘与镜像大小。这是安排时间的估计，不是已测出的最终版本耗时。需要重建镜像、安装依赖或获取缺失镜像时另计；这些步骤可能联网，不能放进“整栈无外网”结论。

| 归档类别 | 应包含 | 刻意不包含及原因 |
|---|---|---|
| 普通发布 bundle | 恰好 6 文件：image.tar.gz、compose.yaml、Caddyfile、init_database.py、check_config.py、release-manifest.json；与当前 builder/validator/CI发送清单一致 | 不含数据库、媒体、秘密、site.env、测试CA/证书、root控制脚本；不能自行加一个说明文件后仍期待严格验包通过 |
| 管理员首次安装材料 | 同一已审阅源码版本的 release.py、check_config.py、preflight_host.py、bootstrap.py，以及安装权限/批准流程说明 | 这四个 root 程序必须独立安装审核，不由 incoming bundle 或部署账户升级；bundle内check_config不作为root执行代码 |
| 镜像内容 | 平台代码、锁定Python依赖、映序构建产物、Comfy工作流定义、Linux字体及其发行包授权 | 不含模型权重、GPU、中央库、用户 `.env`、SSH、媒体/账户库；验证工具从外部只读挂载，不必进运行镜像 |
| 验收证据 | 最终commit整栈JSON、同镜像ID、bundle manifest摘要、测试结果及对应源码版本；历史precommit结果单独保留 | JSON不能补填成从未跑过的最终结果；镜像ID也不能当作注册表manifest digest填入依赖固定引用 |
| 灾难恢复材料 | 独立加密的DB/对象一致备份、备份manifest与恢复证明，由专门流程维护 | 不打进GitHub发布artifact；运行秘密及Caddy私钥的管理另行安排，本工具不导出它们 |

`check_platform_bundle.py` 只读归档、从本checkout导入受信验证器，不从bundle执行代码，不docker load、不联网。builder不会pull，但保留不完整目标目录便于失败核查，不会替使用者删除它。host release会启动站点和Caddy，正式Caddy可能联网申请/续期证书；这与这里的无外网临时internal TLS测试不同。
