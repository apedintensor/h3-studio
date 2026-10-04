# 有限生产 GPU 扩容验收

这是独立 root 操作入口，不是常驻自动付费开关。默认 CPU 发布配置仍关闭生成；普通 CD 不获得 Lium 权限。只有已授权、已建预算和容量审批的一个固定故事可进入这轮控制器。文件存在、测试通过和配置导入均不代表公网 GPU 验收已完成。

## 安装与准备

由 root 从已审查版本安装 `gpu_scaler.py`、`gpu_acceptance.py`、`deploy_approved.py` 及它们现有的受信 release/check_config 依赖到 `/opt/sixnine-release/`。同时安装本项目的 `studio_platform/__init__.py`、`lium_identity.py`、`lium_runtime_aws.py` 到该目录下同名 Python 包，保持 root 所有且禁止 group/other 写。复用同一个运行时加载器；不复制中央 Windows 加载器、不创建 `.env`。host 使用已安装 boto3 和仅能读取确切 secret ARN 的 EC2 role；容器不需要 boto3、AWS 凭据或 IMDS。

当前健康发布必须是独立 root 批准 manifest 的确切 commit，且镜像包含 `production_scaler`、`production_scaler_boot`、`drain_safe_runner`。helper 同时校验 archive 的 Classic/OCI 身份与本机实际 image ID。安装新 helper 是独立操作员动作；普通 CI 不允许覆盖它。

只准备下面这些位置；本 helper 不创建私钥、预算、容量审批、GPU 或初始化数据库。

| 主机位置 | 容器位置 | 权限与用途 |
|---|---|---|
| `/srv/sixnine/gpu-scaler/operator/scaler.json` | `/control-config/scaler.json` | root 所有、不可被 group/other 写；无密钥值 |
| `operator/execution-policy.json` | `/control-config/execution-policy.json` | root 只读策略；app 与 controller 同一文件 |
| `public-source/bootstrap_cloud.py`、`public-source/model_manifest.json` | `/bootstrap-source/` | 仅两个公开部署来源；逐文件 SHA-256 绑定 config |
| `identity/key` | `/worker-identity/key` | 专用 SSH 私钥现有安全位置；uid 10001、0400；不读取到会话，不写入镜像 |
| `control/` | `/control` | uid 10001、0700，首次只允许预置 known_hosts 或空目录；账本恢复标记、共享 collection-lock |
| `tmp/` | `/tmp` | uid 10001、0700 |
| `/srv/sixnine/platform-data` | `/data` | 与 app 相同对象存储；controller 不另建数据库 |
| 已有 app_database_url secret | `/run/secrets/app_database_url` | 与 app 相同生产 PostgreSQL |
| `/srv/sixnine/lium-runtime-import.json` | 不挂载 | root 保护的 ARN/version/service/profile 元数据，无凭据值 |

上述根目录、operator、identity、public-source 目录必须为 root 所有、不可被 group/other 写，且不能是链接。读取私钥只发生在 worker 的 SSH 客户端中。已存在的模型、环境、项目资料和原凭据不搬迁、不删除。

`scaler.json` 采用 `FiniteConfig` 全字段格式，包括显式 `interval_s`。必须精确使用 tenant `sixnine`、owner `superdan`、独立 project/pool/configuration/cycle/approval ID，及表中的容器路径；`known_hosts_file=/control/known_hosts`。可预置经独立核验的 SSH 主机公钥文件，uid 10001、0600、非链接，并保持 `trust_first_host_key=false`。新供应商主机采用 TOFU 需要 config 显式 `trust_first_host_key=true`，首次观察保存后拒绝换钥；不得把此机制描述成预先核验过的主机身份。

`execution_policy_sha256` 是 `repository.request_hash(policy)` 的规范 JSON 摘要，**不是**策略文件字节 SHA。`source_sha256` 则是两个公开文件的实际字节摘要。ARN/version 必须与已授权 metadata 一致，加载器进一步严格检查 `lium/lium--rig-root/https://lium.io/api`。profile 名不是上游 model ID。

必须先在网站创建故事，再由操作员配置它已有的生产 PG 预算/容量审批。保留历史 spent/reserved；本轮所有供应商费用累计受原 US$50 授权约束，之前实验费用必须扣除，未知账单保守预留。helper 不自动增加预算或补款。单卡 launch、精确 offer/template、报价上限、最多两台、TTL 不超过四小时、全局 hard deadline 都写入审批；不能用下载成功代替 FL50 资格。

## 启动和状态

无参数只显示禁用状态，不连接提供商：

```sh
/usr/bin/python3 /opt/sixnine-release/gpu_scaler.py
```

在 root 受信通道、已批准 config 和预算下，使用持久系统服务托管一次启动，unit 名应与本次 cycle 对应：

```sh
systemd-run --unit=sixnine-finite-APPROVED-CYCLE --property=Type=exec \
  --property=KillMode=process --property=TimeoutStopSec=infinity \
  /usr/bin/python3 /opt/sixnine-release/gpu_scaler.py start
```

不要把占位 unit 名当作现有实例，也不要重复 start。helper 会在任何付费能力启动前写 root `active.json`，并拒绝既有 controller 容器或未清算的 control 目录。提供商认证只在 host 内存加载一次，经 `docker compose run -T --name ... --no-deps` 的 stdin 一次交付并 EOF；不进入 argv/env/key 文件或输出。controller 使用 database+edge 网络、无端口、无 Docker socket，uid 10001、只读文件系统、1 GiB、0.75 CPU。两个 GPU 可同时生成，CPU 下载/校验/入库使用同一 `/control/collection-lock` 串行化。

GPU 不持有生产数据库凭据或用户认证。CPU 保存 DB/对象，GPU 只运行 Comfy；访问经私有 SSH 隧道。新节点下载与 FL50 资格测试本身会消耗时间/费用，不能保证每次都会在剩余 TTL 内产生可用容量。

`AWS_EC2_METADATA_DISABLED=true` 只禁止 SDK 的 metadata 查询，不是网络防火墙。操作员须核实当前 EC2 metadata response hop limit 为 1 或已有同等容器隔离；本 helper 不修改 IMDS、IAM 或防火墙，也不以环境变量替代该核验。公开部署来源及 SSH 私钥各自单文件只读挂载，不连带挂载旁边的文件。

容器 `--status` 是无 provider/SSH 的独立 PG 查询，不能单凭它停止 controller。主机 `active.json` 为 root 写入，`control/status.json` 仅是观察记录；未知、过期、缺失、快照和异常均不能当作完成证据。

## 收尾与失败

```sh
/usr/bin/python3 /opt/sixnine-release/gpu_scaler.py restore-cpu
```

helper 先恢复 CPU app，关闭新生成准入；再请求 drain。既有任务继续收集/对账，已有租赁继续由唯一 controller 处理销毁。它只在原 controller **自然退出 0、未 OOM**，以及独立新鲜 PG 证明精确 cycle 全实例 destroyed、无活跃作业/attempt 后才清除 active barrier。不会调用 Docker stop/kill/down，不会把 TTL 经过当作销毁证明。等待超时保留容器和 barrier；对账后重试同一个 restore，不能重建控制器绕过未知状态。

host launcher 正常生命周期结束同样先关闭网站新生成准入，再验证销毁并清 barrier，已完成网站作品仍可读取。未知退出也会尝试关闭准入并保留 barrier。等待阶段收到 TERM/INT 只请求关闭准入和 drain，不代理信号给 Docker 容器。不要用强制终止 systemd/unit、删除 marker 或重跑 Compose 绕过这些规则。

`billing_pending` 非零表示计费尚未最终清算，不能称为零费用或直接开始新预算周期。销毁记录与最终供应商账单由这轮控制器/操作员保留。恢复完成不删除对象、故事、成片、账本、私钥或历史容器；归档和下一轮目录准备是另一个明确操作。

普通 `deploy_approved.py` 会同时检查单机验收与 scaler 的 active barrier，以及运行中的 `gpu-worker` / `gpu-controller`；任何不确定状态都阻止常规 CD。原 CPU Compose checker 不放宽，只由新 checker 严格剥离本轮固定 delta 后再次验证。

## 离线验证范围

`test_platform_gpu_scaler.py` 使用实际 Compose config 渲染与假进程/假加载器；检查不可增权限/网络/端口/挂载、stdin 唯一交付、未知交付不重试、自然退出和新鲜账本同时成立、自动结束关闭准入。它不访问 AWS/Lium，不租机、不生成；生产任务→GPU→网站作品仍需单独实测。
