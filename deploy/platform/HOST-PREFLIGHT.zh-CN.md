# 发布前只读检查

`preflight_host.py` 由管理员审阅后，与 `release.py`、`check_config.py` 一起安装到受 root 控制的 `/opt/sixnine-release/`。它不创建目录、不读密码、不更改权限、不启动容器，也不连接 AWS。未准备好的主机应明确失败，不自动放宽权限继续发布。

```sh
# 仅在已获准的目标 Linux 主机运行；本轮没有在真实 Lightsail 执行。
sudo /usr/bin/python3 /opt/sixnine-release/preflight_host.py
```

检查器固定 `/srv/sixnine`、`/run/sixnine-secrets` 与 `/opt/sixnine-release/docker-config`，不接受命令行任意路径。`check_host()` 可供受信发布器/首次配置工具调用；失败抛出 `PreflightError`，消息仅是固定原因代码。纯函数 `validate_snapshot()` 用于假主机元信息回归。

| 检查 | 要求与原因 |
| --- | --- |
| 运行身份与父目录 | Linux root；所有父链均为root拥有、不可组/其他用户写的真实目录，拒绝symlink |
| 发布目录 | root拥有；只有incoming允许专用部署组写，不允许other写 |
| app/spool目录 | UID/GID10001:10001、0700；避免root建目录后容器无法写入 |
| PostgreSQL挂载父目录 | 0700，owner对应审阅过的官方17镜像postgres身份（Alpine70:70或Debian999:999）；实际digest必须另核验与该身份一致 |
| 秘密根目录 | root:root0700；管理员文件root:root0400/0600，应用DSN文件root:10001 0440/0640；常规单硬链接文件，1–16384字节 |
| 秘密物理挂载 | 读取`/proc/self/mountinfo`，最深匹配挂载必须是tmpfs；即使/run是tmpfs，若单个文件从ext4另行bind进来仍拒绝 |
| Docker配置 | `/opt/sixnine-release/docker-config` 为root:root0700空目录，不读取或继承root原来的登录/代理/context配置 |
| 本机daemon容量 | 仅`/usr/bin/docker --host unix:///var/run/docker.sock info`的固定格式元信息；Linux、至少2CPU、报告总内存至少6GiB。这不是当前空闲内存测量，8GiB主机仍要监控实际余量 |

Docker子进程使用固定`PATH=/usr/sbin:/usr/bin:/sbin:/bin`和上述空`DOCKER_CONFIG`。管理员需在此前单独安装Docker/Compose，并核验它们来自受控安装目录；运行时不下载plugin。使用发行版提供的Compose插件路径，不能仅将插件放在原用户的`~/.docker/cli-plugins`中。上述目录由管理员显式准备，检查器不会创建。

PostgreSQL的父目录约束来自本包`PGDATA=/var/lib/postgresql/data/pgdata`。官方入口只修正`$PGDATA`的owner；若其挂载父目录为root:root0700，切换postgres身份后无法穿过该父目录。本轮真实本机测试已使用与生产相同的PGDATA子目录结构和0700/70:70父目录，角色初始化及重复运行通过。[官方镜像入口源码](https://github.com/docker-library/postgres/blob/master/docker-entrypoint.sh)

预检没有验证秘密内容、正式秘密来源、PG身份密码一致性、账号就绪、备份恢复、80/443占用、Docker网段与VPN冲突、依赖镜像兼容、公网DNS或TLS；输出会保留这些未验收项。CPU阈值也不代表吞吐测试。app固定2核是为了兼容主机模板的2vCPU起点；Docker本身会拒绝超出daemon核数的NanoCPU配额。[Moby官方校验](https://github.com/moby/moby/blob/docker-v29.1.3/daemon/daemon_unix.go)

## 上传前核验实际镜像包

构建主机使用仓库内受信校验代码，而不是执行bundle中的脚本：

```sh
python tools/check_platform_bundle.py "$GITHUB_SHA" platform-release
```

这个命令核验完整commit、固定文件集、各文件哈希以及实际Docker保存的归档结构、镜像引用和ID。不会调用`docker load`或启动服务，也不会替主机独立批准来源；`provenance_approved`始终为false。CI应在构建打包后、上传artifact之前运行。主机仍需独立的root manifest批准与加载后image ID核验。

2026-10-04验证：9项新增假主机/归档测试；部署配置与真实本机Postgres集成亦通过。另用已有本机synthetic `sixnine-platform:ffffffffffffffffffffffffffffffffffffffff`镜像，真实`docker save`→压缩bundle→本校验器通过；未重新build/load/启动该镜像，临时产物已清除。此证据覆盖本机Docker29.8.1的OCI归档格式，不能替代最终CI与目标主机组合的真实交付验收。
