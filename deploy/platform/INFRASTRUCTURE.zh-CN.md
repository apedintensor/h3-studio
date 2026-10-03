# Lightsail 主机模板与当前验证范围

`lightsail-host.json` 已写好 CPU 主机、静态 IPv4、防火墙和可选每日磁盘快照。它不创建 GPU，不安装应用，不设置任何密码/供应商 key，也不改 Namecheap DNS。2026-10-04 通过官方 AWS MCP 调用 CloudFormation `ValidateTemplate` 成功；没有调用 CreateStack、创建 change set 或部署实例。

这只是结构验证，不能证明镜像兼容、区域有货、IAM 权限、启动结果或公网可用。本机没有 cfn-lint/cfn-guard，本轮未安装或声称运行过它们；执行部署前应按固定版本补齐，并审阅实际 change set。

## 主机规格与费用依据

2026-10-04 官方 Lightsail `GetBundles` 分别查询新加坡和悉尼返回以下值。报价是该时点 API 的月价，不包括税、额外传输、快照、R2/GPU/API调用；不是已产生账单。

| 模板选项 | 内存/CPU/磁盘 | 新加坡 bundle / 月价 | 悉尼 bundle / 月价 | 用途 |
|---|---|---|---|---|
| control8gb | 8 GiB / 2 vCPU / 160 GB | large_3_0 / $44 | large_3_2 / $44 | 两人控制台、数据库、小规模媒体处理起点；不在此承诺多路粗剪吞吐 |
| cpu16gb | 16 GiB / 4 vCPU / 320 GB | xlarge_3_0 / $84 | xlarge_3_2 / $84 | 给 CPU 工作进程和缓存留空间；仍须实测 |

新加坡两个规格包含的传输额度分别是5120/6144 GB，悉尼是2560/3072 GB。此处没有把传输额度当作永久免费出站；地区规则和超额计费须在购买时再核对。原先“4vCPU、8GiB”的建议不是标准通用 bundle 的已确认低价配置，因此模板采用上述实际查询到的通用规格。

新加坡 `GetBlueprints` 返回 `ubuntu_24_04`（OS、Ubuntu 24.04 LTS、active）。新加坡 `GetRegions(includeAvailabilityZones=true)` 返回1a/1b/1c可用；本次该调用没有返回悉尼AZ详情，所以悉尼必须在该区域重新查询，不能把空数组解释为整个区域不可用。以上为库存与元信息观察，不是已购买容量。

## 输入与部署约束

准备独立非秘密 JSON，恰好包含：`region`、`InstanceName`、`AvailabilityZone`、`Capacity`、`KeyPairName`、`AdminIpv4Cidr`、`DailySnapshot`。区域和容量没有默认值；KeyPairName 是已登记的公钥对名称，不是私钥。管理员地址必须是确认归你或部署 runner 所有的公网 IPv4 `/32`。

```text
python tools/check_lightsail_plan.py ABSOLUTE_PATH_TO_REVIEWED_PLAN.json
```

此命令只验证结构并输出 CloudFormation 参数，不调用 AWS。它拒绝跨区AZ、开放SSH网段、私网/文档示例IP和多余秘密字段；不能验证IP归属、当前价格或区域key是否存在。

模板仅开放公网80/443 TCP、443 UDP；22仅管理员 `/32`，IPv6列表为空。应用8845、Postgres5432、Comfy8188不开放。正式DNS仅在主机、登录和备份就绪后设置www/apex/h3的A记录；不要凭模板写AAAA。是否开放IPv6另行验证。

**当前 GitHub 默认托管 runner 使用动态来源IP，不能直接穿过管理员 `/32` 的SSH规则。** 首次发布可由管理员从该地址取已经测试的bundle并调用受限控制器；要让GitHub自动送包，需配置明确的固定出口runner或经审阅的私有通道。不要用开放22到全网“修好CI”。工作流已留显式 runner 选择；未设置时不尝试生产传输。主机可信manifest批准仍独立于CI部署身份，详见 RELEASE 文档。

实例和静态IP使用Retain/UpdateReplacePolicy Retain，避免删除stack就丢失本地项目。**删除stack不会停止它们计费**；替换可能同时留下旧主机/IP。维护时先列清实际实例、备份、域名指向和费用，再由有授权者处理，不能靠删除本地项目收尾。

可选快照默认disabled，启用后每天16:00 UTC请求自动磁盘快照，另有费用。运行中的磁盘快照不等于PostgreSQL与媒体的应用一致性备份，也不替代加密离机备份。模板没有夸称解决灾难恢复。

## 后续执行次序

1. 选择区域/容量/费用范围，核对非root AWS身份、可用AZ、独立公钥对和管理员固定IP。
2. 补齐cfn-lint/cfn-guard，审阅实际变更及费用，再创建主机。当前尚未进行到这一步。
3. 依 README 配置受限Docker服务、加密秘密来源、正式账户、离机备份与恢复演练。
4. 安装root控制的发布器，传递已测试的同一个image bundle；人工批准来源manifest。
5. 私下验证站点后再改DNS并签发TLS，核验外网登录、两用户隔离和下载。
6. R2、CPU worker、H3 GPU池逐项启用与独立验收，不随网站上线一起打开未知收费链路。

官方依据：[Lightsail实例CloudFormation](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-lightsail-instance.html)、[静态IP](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-lightsail-staticip.html)、[自动快照](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-properties-lightsail-instance-autosnapshotaddon.html)、[Lightsail价格](https://aws.amazon.com/lightsail/pricing/)。API观察发生于悉尼2026-10-04；不会自动追新价。
