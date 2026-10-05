# 解除公网统一5秒输出限制

2026-10-05，Sydney。用户明确要求解除当前约5.1667秒采样上限。本批状态：本地修改与发布准备；实际启用必须有公网和控制器回执，本文本身不是上线证明。

## 最终行为

- 允许当前工作流4–15秒请求；原生采样按17n+5帧、24fps向上对齐。14秒请求采样345帧/14.375秒，15秒采样362帧/15.0833秒；成片按请求时长导出，不能把采样上限写成整数15而再次拦住15秒请求。
- 控制器原来除运营5.1667秒policy之外还有6秒硬边界。新范围只对显式`queued-task-first-v1`开放，保持`runtime_required`；旧FL50和三轮测试profile的历史边界不变。
- 新GPU按此前用户要求做固定源码、权重、硬件和runtime检查后执行队列真实任务，成功直接交付。没有额外测试视频，也不能把运行环境就绪宣称为已验证15秒。
- 其他开放范围保持：最多768P像素面积、50步、现有首尾帧/参考输入和CPU编码器/tiled视频解码；参考素材时长与输出时长是两种限制，本次不一起扩大。
- 两台按需策略仍处于设计阶段，这批不顺带启用双机。映序新媒体页面仍由用户本地确认后再发布。

## 时间与额度预留

`reservation.duration_reference_seconds` 是可选的明确运营配置。不存在时保持旧policy行为；启用时按`max(1, actual_native_duration / reference_duration)`派生向上取整的任务运行时间与额度预留，不修改原policy或其SHA。

本次运营基线沿用124/24秒、1800秒运行额度、US$0.80费用预留，长时长只上调，不因480P或少步自动下调。14秒请求约5009秒/US$2.225807，15秒约5255秒/US$2.335484。这是运营估算与预留，不是已测长视频耗时、最终收费或性能必然线性增长；API返回`estimate_basis: operator_allowance`与`performance_scaling_verified:false`。

预检、账户预算、任务保存、热机与冷启动截止检查、worker剩余TTL检查使用同一派生运行时间。冷启动另占准备窗口；长任务可能因原服务剩余时间不足被拒绝，这不是恢复旧5秒限制。原GPU累计US$50、原2026-10-05 18:45 Sydney截止和供应商TTL不延期、不重置、不充值。

## 安全切换与已接任务

运营policy、config、capacity approval、worker与host marker都绑定身份。不能只覆盖JSON；旧审批与已提交任务保留原策略。

本轮只读诊断确认原sequence004出现真实任务`f5508e4a-3e50-4558-803a-f824c795b62d`，GPU已开始准备。新批次先完成代码与发布包，不能用空池迁移打断它。必须等业务结束、原实例销毁及费用结算后，再通过独立一次性helper确认并切换；有未结束业务、unknown或未结账时拒绝切换。

新helper保留租赁与费用历史、原budget IDs和余额、created_at、hard_deadline、max_cycles及累计已消耗sequence。未租的当前等待周期延续序号；已经完成租赁的周期依据真实状态推进，不能回到001。未准入草稿保留数据，旧预检过期后重新预检；不得将已接任务当成草稿清除。

## 验收与证据

关键离线检查覆盖原生补帧上限、旧profile不扩、派生quote不改policy身份、精确额度边界、cold/runtime原截止、真实首任务一次提交、unknown只对账与重启恢复。PostgreSQL CI合并检查这批最终范围，不为每个参数单独发布。

线上只进行已授权的读取与无生成预检核对；这次不额外创建收费测试任务。新增长时长的实际速度、显存和成片质量由后续真实用户任务记录，不把离线通过写成15秒在线成功。

相关文件：`studio_platform/production_scaler.py`、`execution_policy.py`、`queued_task_runner.py`、`test_platform_long_duration_policy.py`、`test_platform_duration_quote.py`及`QUEUED-TASK-START.zh-CN.md`。确切CI、发布、host切换与公网检查回执保存于`.platform-demand-live/`。
