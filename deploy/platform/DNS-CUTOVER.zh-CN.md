# sixnine.art 的上线与 DNS 切换记录

2026-10-04 悉尼时间，本轮只查询公开DNS，没有修改Namecheap、nameserver、邮箱或证书。观察文件位于项目内 `.platform-preview-v2/dns-observation-20261004.json`。这是递归DNS观察，不能代替已登录控制台中的完整记录与转发设置。

| 查询 | 本次观察 | 处理原则 |
|---|---|---|
| NS | dns1.registrar-servers.com、dns2.registrar-servers.com | 保留Namecheap DNS；使用R2不要求改用Cloudflare nameserver |
| apex A | 162.255.119.157 | 这是当前公开地址；控制台可能以URL转发表示，切换前记录实际设置 |
| www CNAME | parkingpage.namecheap.com | 正式切换时替换为已验收主机的静态IPv4 A记录 |
| apex MX | eforward1/2/3.registrar-servers.com 优先级10；eforward4优先级15；eforward5优先级20 | 保持原邮箱转发记录，不整区替换 |
| apex AAAA | 本次无Answer | 模板目前没有IPv6，不新增AAAA |
| h3 A / CAA | 未取得可用回答 | 保持“待核实”，不把查询失败解释成记录一定不存在 |

目标主入口为 `https://www.sixnine.art`，单次创作为 `/freestyle`。apex重定向到www，`h3.sixnine.art`重定向到`https://www.sixnine.art/freestyle`。H3任务API只在主站 `/v1`，Comfy/GPU端口不加入公网DNS。

## 切换次序

1. 先确认地区、主机费用与非root AWS身份，创建受限CPU主机和固定IP；本轮未执行此项。
2. 私下验证同一个已测试镜像、正式登录、superdan/supervan隔离、数据库、备份和下载。应用内部端口不公开；GPU/自动租机仍保持关闭。
3. 在已登录Namecheap控制台核对并保存现有 `@` 的真实记录/转发目标、www、h3，以及MX/TXT/CAA等。用户会话和Cookie不导出到文件。
4. 仅修改站点需要的记录：www指向目标静态IPv4；apex同IP供Caddy重定向；h3可用指向www的CNAME。删除冲突旧www CNAME前明确记录原值；不替换整区、不改变nameserver或邮箱。具体值必须来自实际已验收实例，本文件不放虚构IP。
5. 先观察DNS传播，再由Caddy签发/续期TLS并从外网核验三个域名、登录和私有下载。Caddy本地配置验证不等于真实证书已签发。
6. 如失败，按保存的原记录恢复www/apex/h3；保留应用和数据库的故障证据。DNS回退和应用镜像回滚分别执行，不能由一次Git回滚推断DNS已恢复。

对象存储桶继续私有。R2签名地址保持供应商端点，不直接把Host换成`media.sixnine.art`；若未来需要自有媒体域名，单独部署鉴权和Range支持后再接入。

尚缺实际生产静态IP、控制台内完整DNS/转发快照、TLS签发结果、正式登录与外网下载证据。该文件是可审核切换方案，不是公网已上线声明。
