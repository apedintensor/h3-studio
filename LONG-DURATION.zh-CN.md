# 解除公网统一5秒输出限制

2026-10-05 17:58，Sydney。用户明确要求解除当前约5.1667秒采样上限。本批已上线：完整CI37271780834、受保护发布73ca224、新监督器sequence005/admission open与公网GET验收通过。具体回执见`.platform-demand-live/NEW-START-STRATEGY-RESULT.zh-CN.md`和`final-duration-public-verification.json`。允许4–15秒请求，不代表15秒生成性能已经实测。

## 最终行为

- 允许当前工作流4–15秒请求；原生采样按17n+5帧、24fps向上对齐。14秒请求采样345帧/14.375秒，15秒采样362帧/15.0833秒；成片按请求时长导出，不能把采样上限写成整数15而再次拦住15秒请求。
- 控制器原来除运营5.1667秒policy之外还有6秒硬边界。新范围只对显式`queued-task-first-v1`开放，保持`runtime_required`；旧FL50和三轮测试profile的历史边界不变。
- 新GPU按此前用户要求做固定源码、权重、硬件和runtime检查后执行队列真实任务，成功直接交付。没有额外测试视频，也不能把运行环境就绪宣称为已验证15秒。
- 其他开放范围保持：最多768P像素面积、50步、现有首尾帧/参考输入和CPU编码器/tiled视频解码；参考素材时长与输出时长是两种限制，本次不一起扩大。
- 两台按需策略仍处于设计阶段，这批不顺带启用双机。映序新媒体页面仍由用户本地确认后再发布。

## 时间与额度预留

`reservation.duration_reference_seconds` 是可选的明确运营配置。不存在时保持旧policy行为；启用时按`max(1, actual_native_duration / reference_duration)`派生向上取整的任务运行时间与额度预留，不修改原policy或其SHA。

本次运营基线沿用124/24秒、1800秒运行额度、US$0.80费用预留，长时长只上调，不因480P或少步自动下调。14秒请求约5009秒/US$2.225807，15秒约5255秒/US$2.335484。这是运营估算与预留，不是已测长视频耗时、最终收费或性能必然线性增长；API返回`estimate_basis: operator_allowance`与`performance_scaling_verified:false`。

预检、账户预算、任务保存、热机与冷启动截止检查、worker剩余TTL检查使用同一派生运行时间。冷启动另占准备窗口；长任务可能因物理实例剩余TTL不足被拒绝，这不是恢复旧5秒限制。用户另授权原服务窗口增加5小时，截止延至2026-10-05 23:45 Sydney；created_at、累计账本、已预留款和序号保持，显式authorization_extension_s=18000。现有余额足够，无充值。服务窗口延期不等于当前供应商实例TTL已经延长。

## 安全切换与已接任务

运营policy、config、capacity approval、worker与host marker都绑定身份。不能只覆盖JSON；旧审批与已提交任务保留原策略。

原sequence004真实任务`f5508e4a-3e50-4558-803a-f824c795b62d`在升级检查期间已经开始并完成，单次attempt，旧策略结果完整保留。用户随后明确要求关机升级再开，实际采用旧GPU销毁及租赁对账完成后的finished-empty切换，未执行live SQL adoption，也没有重新提交原任务。

独立helper保留原job/request/plan/幂等键、六项终态任务待结算预留、预算与历史租赁；旧control和operator归档，新sequence从004继续到005。hard_deadline只增加明确授权18000秒，未充值或重置账本。公网后端及按需控制器已恢复，新真实任务触发下一次GPU启动；当前空队列没有额外租GPU。此前准备的同pod迁移代码只有离线证据，不是本轮实际执行路径。GPU销毁后的新实例仍须准备环境和模型，不宣称保留了旧pod缓存。

## 验收与证据

关键离线检查覆盖原生补帧上限、旧profile不扩、派生quote不改policy身份、精确额度边界、cold/runtime原截止、真实首任务一次提交、unknown只对账与重启恢复。PostgreSQL CI合并检查这批最终范围，不为每个参数单独发布。

线上只进行已授权的读取与无生成预检核对；这次不额外创建收费测试任务。新增长时长的实际速度、显存和成片质量由后续真实用户任务记录，不把离线通过写成15秒在线成功。

相关文件：`studio_platform/production_scaler.py`、`execution_policy.py`、`queued_task_runner.py`、`test_platform_long_duration_policy.py`、`test_platform_duration_quote.py`及`QUEUED-TASK-START.zh-CN.md`。确切CI、发布、host切换与公网检查回执保存于`.platform-demand-live/`。
