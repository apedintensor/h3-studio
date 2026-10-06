# Sixnine · 映序与 H3 Studio

**New development session:** read [PROJECT-PLAN.md](PROJECT-PLAN.md), then [WORKFLOW.md](WORKFLOW.md), and claim an existing task on the [Sixnine Platform Delivery board](https://github.com/users/apedintensor/projects/2). [workflow/project.json](workflow/project.json) links the actual work packages and fields. Use English on GitHub; code completion, verification and production release are separate states.

开发与架构先读 [规划与实现索引](PLANNING-INDEX.zh-CN.md)：包含目标设计、[现有代码复用与模块化决策](REUSE-AND-MIGRATION-DECISION.zh-CN.md)、当前接口契约及历史证据的分工。规划未等于实现或上线；以下早期走查入口不能证明今天的服务状态。

当前主线是映序引导制作、ReactFlow画布、`/freestyle`共用项目、私有素材和版本化`/v1` API的平台。代码入口为 `platform_app.py` / `studio_platform/`，新部署包位于 `deploy/platform/`。

先按[五分钟检查入口](START-HERE.zh-CN.md)查看本机工作室。本轮功能、测试与待上线事项见[交付报告](DELIVERY-REPORT.zh-CN.md)；报告中的执行状态与最终验证时间分开记录。

- [当前架构与实现边界](ARCHITECTURE.zh-CN.md)
- [实际v1 API接入方式](API-USAGE.zh-CN.md)
- [统一平台部署与首次账户初始化](deploy/platform/README.zh-CN.md)
- [Lightsail主机模板与费用观察](deploy/platform/INFRASTRUCTURE.zh-CN.md)
- [sixnine.art域名观察与切换方案](deploy/platform/DNS-CUTOVER.zh-CN.md)
- [发布、回滚与CI](deploy/platform/RELEASE.zh-CN.md)
- [部署和恢复的实际就绪复核](deploy/platform/READINESS-REVIEW.zh-CN.md)
- [备份恢复](BACKUP-RECOVERY.zh-CN.md)、[运行诊断](OPERATIONS.zh-CN.md)、[迭代证据](ITERATIONS.zh-CN.md)

前端canonical源码在同级`../video-studio-design/studio-app`；本仓库`yingxu/`是经哈希核对的发布源码快照，由`tools/sync_yingxu_source.py`维护。不要直接编辑快照或将本机草稿/素材放进镜像。

以下为保留的早期独立H3工具背景；`server.py`、`web/`和旧`deploy/`仍保留兼容测试，统一平台以以上新入口为准。

MiniMax H3 multimodal workbench with separate image/video/audio references,
timeline guides, native workflow controls, owned assets, jobs and downloads.

The current deployment package runs the **CPU website only**. Generation is
disabled; it does not rent a GPU, carry provider credentials, or download weights.
Public deployment uses password authentication for `superdan` and `supervan`.

- [Lightsail and GitHub Actions deployment](LIGHTSAIL.md)
- [Multi-GPU / API routing design and remaining work](SCALING.zh-CN.md)
- [Generation controls and measured limits](CONTROLS.zh-CN.md)

## Tests

Python 3.12, Node.js and FFmpeg/ffprobe are required:

```sh
python -m pip install -r requirements.lock.txt
python -m unittest discover -s . -p 'test_*.py' -v
node --check web/app.js
```

Tests use disposable data and fake requests. Do not run live generation scripts
as a CI test. Database, media, cloud state, SSH files, model weights, credentials,
logs and local experiments are deliberately excluded from Git and Docker.

GitHub Actions runs CI on push/PR. CPU deployment is an explicit main-branch
workflow dispatch after destination configuration and account provisioning.
The tested image is passed to deployment unchanged. This repository alone does
not create an AWS account, Lightsail instance, domain, GPU, or API subscription.
