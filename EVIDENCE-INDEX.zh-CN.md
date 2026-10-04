# 本轮可核对的验收索引

记录截至2026-10-04 01:00 UTC，十小时工作尚未结束。最新修复候选为 `027039a55c74e3c25d9fa0369d9e11870263a2bc`；下表中明确标45d3的记录属于历史基线，不能代替新版本验收。下列点号目录是本机验收产物，不随Git源码推送；这些路径的存在不代表离机备份。

## 先看哪份

- 用户入口：[五分钟走查](START-HERE.zh-CN.md)、[交付报告](DELIVERY-REPORT.zh-CN.md)。
- 设计和范围：[架构](ARCHITECTURE.zh-CN.md)、[迭代故事](ITERATIONS.zh-CN.md)、[API契约](API-USAGE.zh-CN.md)。
- 上线条件：[生产就绪复核](deploy/platform/READINESS-REVIEW.zh-CN.md)、[秘密供给](deploy/platform/RUNTIME-SECRETS.zh-CN.md)。

## 原始记录

| 证明什么 | 本机相对路径/外部运行 | 不证明什么 |
|---|---|---|
| 45d3 Windows完整检查 | `.platform-preview-v2/regression-20261004-final-hardening-confirmed.log` | Windows没有Linux地址空间限额 |
| 45d3 隔离PG专项 | `.platform-preview-v2/regression-pg-20261004-final-hardening.log` | 未访问生产数据 |
| 45d3 Linux CI及前端构建 | [GitHub运行37163860227](https://github.com/apedintensor/h3-studio/actions/runs/37163860227) | push不会自动部署 |
| 不可变镜像、HTTPS/PG、最大素材及发布包 | `.platform-final-release-20261004/45d3cccb7d32f91db18dfbbb1efe09c1e8c91c4b/FINAL-RECEIPT.json`，同级 `REPORT.zh-CN.md` | 没有核心代码overlay；仍非公网、GPU或云存储验收 |
| 六小时滚动CPU模拟 | `.platform-soak-overnight-20261004/status.json` | 36/36、5次重启；不是单一commit六小时 |
| 固定源码两小时CPU模拟 | `.platform-fixed-soak-20261004/manifest.json`；`source/.platform-soak-frozen-2h/status.json` | 运行中；最终状态必须读 `status` 与 `completed_at`，不能只看计数 |
| 浏览器下载实际文件 | `.platform-preview-v2/browser-download-verification.json` | Chrome工具下载；不是操作系统另存为弹窗 |
| 视频选段 | `.platform-preview-v2/trim-browser-verification.json` | 262帧CPU成片；不是H3新推理 |
| 下载并发/慢连接 | `.platform-network-capacity-20261004/REPORT.zh-CN.md` | 旧25525ff镜像本地45秒测试，不是最终版公网负载SLA |
| 媒体内存边界修正过程 | `.platform-media-capacity-20261004/RESULTS.zh-CN.md` | 源码覆盖实验与最终不可变镜像验证分别记录 |
| 工作台状态与成片入口 | `.platform-preview-v2/studio-guide-final.jpg` | 截图不能替代附件哈希/实际解码 |
| 原GPU已销毁 | `gpu-shutdown-receipt.json` | 历史API核验时间，不是当前最终账单 |

整个最终发布包恰好六个文件，报告、日志和用户文件不能塞进 `bundle/`。镜像只含代码、依赖和许可字体；没有模型、媒体、数据库、中央库、SSH私钥或会话文件。

## 最新修复候选与失败证据

- 027039a Windows：`.platform-preview-v2/regression-final-race-timeline.log`，735项/723通过/12跳过。
- 027039a PG+媒体：`.platform-preview-v2/regression-pg-final-race-timeline.log`，453项/451通过/2跳过。媒体测试明确使用SQLite；MPU新增25项实际使用PG随机schema。
- 027039a GitHub：[运行37166508489](https://github.com/apedintensor/h3-studio/actions/runs/37166508489)，截至本段时间仍在运行。
- 027039a固定75分钟：`.platform-fixed-release-soak-20261004/manifest.json`，`source/.platform-soak-release-75m/status.json`；00:58:05開始，预计02:13:05结束，未提前声明通过。
- b879失败基线：[CI37165888551](https://github.com/apedintensor/h3-studio/actions/runs/37165888551)，并发ListParts遇另一请求已完成MPU；容器169项之前的边界失败记录在`.platform-final-release-20261004/b879864ede96d89c01a7a5d92069217fae81b833/INTERIM-RECEIPT.json`。后来的overlay验证不是原镜像全通过。
- b879固定75分钟：`.platform-fixed-final-soak-20261004/manifest.json`，`source/.platform-soak-final-75m/status.json`；仅证明CPU模拟路径，不能覆盖该版本已知MPU/短镜头问题。
- 发布前替包与工作目录满重试：`.platform-review-render-cache-20261004/REVIEW.zh-CN.md`及源码测试；修复在b879进入代码。
- 机器身份与对象下载审查：`.platform-review-machine-download/check.py`；仅合成身份和fake云传输，不代表已验证真实存储账户。

## 复核原则

生成来源、真实CPU媒体处理和GPU推理是三种证据。当前网页中的CPU模拟水印不应去掉；真实H3历史样片及费用记录保留各自时间和配置。汇总通过项不能覆盖明确跳过项，过往版本的负载结果不能自动归给新版本。目录同盘、Git可重建代码或一次成功恢复，都不能替代用户资产的独立备份。
