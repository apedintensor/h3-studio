# 远程 Linux 的中央凭据授权方案（设计，未启用）

2026-10-04。只读核对中央 `C:\Users\danmo\Desktop\AI-Registry\API_USAGE.md` 及下列官方资料。本轮没有创建凭据、代理、AWS/Cloudflare 资源，没有读取秘密，也没有调用供应商。

## 已确认边界

中央现有库用 Windows DPAPI 绑定原 Windows 用户；本机 WSL 通过 Windows Python 的受捕获进程管道取指定 profile。远程 Linux 无法使用这个 Windows 身份。`vault_bridge.py` 不是网络密钥导出服务。复制 DPAPI 文件、加载器源码或 Windows session 到 Lightsail 都不能视为安全接入。

因此当前公网包维持 `SIXNINE_STORAGE_PROVIDER=local`，生成和云创建关闭。`cloudflare-r2/cloudflare-r2--rig-root` 仍只是配置已知，不能声称在 Linux 或线上已通过验证。现有 Compose 的 `/run` secret 文件只是运行时挂载契约；正式加密来源和重启补给机制仍待配置。

## 推荐的中央契约

由中央注册表维护者扩展现有 `api_registry.load_api(service, profile=...)` 的授权后端；项目继续复用它，不另写解密器或秘密导出脚本。保留显式 `service/profile/base_url`，在非秘密元信息中登记允许的生产 workload、部署环境、权限和加密库引用。

可信 runtime helper 通过 mTLS 或其他已验证的工作负载身份申请指定权限。服务端按已登记的 workload → profile → endpoint → operation 映射授权；客户端不得自行选择任意 profile、bucket 或供应商地址。密钥或短期能力仅经受保护进程管道/Unix socket进入目标进程内存；禁止写入普通 JSON、`.env`、日志或命令参数。

应审计的字段仅有 workload ID、service/profile、权限、签发/到期时间、调用结果和无秘密关联 ID。短期能力到期或中央授权不可用时拒绝新操作，不能退回环境中的其他账户。续期必须沿用原账户、bucket和endpoint；不能借续期静默改变存储归属。

## R2 优先使用短期能力

[R2 官方临时凭据](https://developers.cloudflare.com/r2/api/s3/temporary-credentials/)支持 S3 access key、secret key、session token 三元组；可限定一个 bucket、对象/前缀和操作。父权限是上限；父令牌撤销后派生能力失效。按精确 S3 action 限权目前需可信方本地签发，不能假设远程 Temporary Credentials API 已支持同一字段。

建议初期由可信中央签发端保留父 profile，只给 Linux 应用短期、指定项目空间的存储操作；给 GPU worker 仅本任务输入的读取和既定输出对象的写入。浏览器优先获得单对象、单操作、短 TTL 的签名 URL，不持有父令牌。签名 URL 本身仍是秘密能力，不进入项目文稿或日志。

现有 `S3Credentials` 可显式接收 `session_token`，但不会自动续期。运行时签发/刷新、过期与重启测试尚未实现；不能仅注入一次短期值就宣布生产可用。长 multipart 会话必须在能力刷新后继续同一个 upload ID 和对象 key，未知完成状态只核对，不能重新创建另一笔上传。中央 broker 的可用性也需要明确：用户电脑休眠会影响依赖它的续期，故不能把临时本机桥接当作全天生产保证。

## 可选的托管加密来源

如果需要独立于个人电脑的服务，由中央维护者先审核生产 profile 进入托管秘密库的方案，并保留注册表作为入口。AWS Secrets Manager 可以配合工作负载 IAM 身份；官方也支持以 [IAM Roles Anywhere 访问秘密](https://docs.aws.amazon.com/secretsmanager/latest/userguide/auth-and-access-on-prem.html)，用于不能直接使用原生实例角色的工作负载。该方案需要受控证书、信任锚、轮换与最小权限，不能省略机器身份这一步。

[Lightsail IAM 文档](https://docs.aws.amazon.com/lightsail/latest/userguide/security_iam_service-with-iam.html)中“可用临时凭据调用 Lightsail API”不等于容器自动获得 EC2 instance profile。选 Lightsail 时必须另外证明所选 runtime 身份机制可用；不在项目创建长效 AWS access key来填补这个缺口。托管秘密库的新服务、费用及生产 profile 写入本轮均未发生。

## 上线前验收

- 假凭据测试错误 workload、未知 profile、错误 endpoint、超权限对象、过期和重放均被拒绝；日志、异常、repr和磁盘无秘密值。
- 应用和 worker 重启后能在无 `.env`、无个人 Windows session、无默认 AWS credential chain 的前提下获得已授权配置；中央不可用时有明确不可用状态。
- R2 续期不改变对象归属；multipart 的进行中/完成未知状态保留并核对。签名下载与上传不可扩大到其他用户或对象。
- 若允许正式验证，再单独授权最小对象的上传/读取/删除和撤销测试；这与不联网加载测试分开记录。
- PostgreSQL 管理员与应用 DSN 采用独立受控秘密，不写入 API profile冒充供应商Key；现有 `/run/secrets` 文件由经过审阅的供给机制生成，并按生产包权限要求挂载。
