# 固定 SSM Document 的常规 CD

2026-10-04：`Sixnine-DeployApprovedRelease` v1 和 `sixnine-github-deploy` 已创建，permissions boundary 已用 IAM 模拟验证固定文档/实例可调用、任意 shell/Secrets Manager/PassRole 被拒绝。GitHub 变量 `SIXNINE_AWS_DEPLOY_ROLE` 已配置；工作流实跑结果另见上线验收记录，不能仅凭角色存在判定 CD 成功。

首发仍由 root 独立批准、`aws_bootstrap.py` 初始化账户、`release.py` 发布。常规 CI 不能执行初始密码设置、写 approval、任意 shell、Secrets Manager、IAM 或 EC2 操作。

操作员安装经过 review 的 `deploy_approved.py` 到 `/opt/sixnine-release`（root-owned0644），并以 `ssm-deploy-document.json` 创建 **`Sixnine-DeployApprovedRelease` v1**。只有 `Commit` 参数：40位小写hex，SSM `ENV_VAR` 安全插值，无 `{{...}}` 拼接和旧Agent回退；已装Agent3.3.4793.0支持此能力。参数再次在Python校验。

root entry 先验证已有正式站点current，再在同一host lock内执行受批准的S3下载和`release.apply_locked`。未知pending版本需操作员对账，不能以另一个SHA绕过；失败后沿原release状态/rollback流程恢复。传入旧但曾批准的版本也属于明确回滚，操作员须自行决定是否继续保留旧approval。

`.github/workflows/ci.yml` 已包含以下路径：

1. 独立 `workflow_dispatch` 输入 `approved_commit` 与可选 `resume_command`。checkout可信main上的tool，不能checkout外部随意传入的代码。
2. `permissions: contents: read, id-token: write`，显式 `concurrency: sixnine-production-deploy, cancel-in-progress: false`，timeout35min。
3. 用独立deploy OIDC role，保留已确认的GitHub不可变repo ID / main或专用environment trust；不要复用publish role来扩大S3写身份权限。
4. 固定AWS region/account，经OIDC短期身份执行 `python tools/deploy_aws_release.py "$APPROVED_COMMIT"`，恢复时加 `--resume-command "$COMMAND_ID"`。不能把输入嵌进shell代码文本，使用环境变量双引号参数。
5. 根会话先通过独立可信通道写对应manifest approval；CI即使能publish和dispatch也不能自己批准。

IAM 操作范围（根会话已按 SDK 代码运行 IAM Policy Autopilot 0.3.0，并用明确的 boundary 收窄生成策略后创建）：

- `ssm:SendCommand` 仅两个精确ARN：`arn:aws:ssm:ap-southeast-1:829135631045:document/Sixnine-DeployApprovedRelease`、`arn:aws:ec2:ap-southeast-1:829135631045:instance/i-03d81d2d153b5e2fd`。不用AWS-RunShellScript权限，不授UpdateDocument/PassRole/StartSession。
- 等待完成需要 `ssm:GetCommandInvocation`。**AWS不支持该操作资源级权限**，需`Resource:"*"`，可限`aws:RequestedRegion=ap-southeast-1`；不授ListCommands/ListCommandInvocations。不能声称此读取权限仅覆盖本实例或本次命令。若不接受此只读范围，先仅触发再由操作员核验，后续另做专属无秘密状态回执方案。
- 没有SM、IAM、EC2变更、S3写、审批文件能力。用permissions boundary约束相同操作，以免后续误附宽策略。

`SendCommand` 没有幂等token，runner禁止SDK自动重试提交；丢响应时显示“结果未知”，不会重新发新任务。成功响应立即输出无秘密command ID；可凭此恢复轮询，只有固定receipt中的commit相符才成功。到轮询时限不取消远端正在进行的发布，不打印SSM任意stdout/stderr。服务器自身的commit/锁/approval/原版本机制承担重复安全，不把HTTP超时说成未执行。

参考官方：[SSM安全参数插值](https://docs.aws.amazon.com/systems-manager/latest/userguide/documents-schemas-features.html)、[SSM权限表](https://docs.aws.amazon.com/service-authorization/latest/reference/list_ssm.html)、[GetCommandInvocation的最终一致性](https://docs.aws.amazon.com/systems-manager/latest/APIReference/API_GetCommandInvocation.html)。
