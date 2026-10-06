# 生产 GPU 启动显存门槛适配

日期：2026-10-05。仅本地代码与离线测试，未租赁、部署或宣称 H100 已完成真实推理。

`ProductionBoot` 不再对所有候选硬套 90 GiB。存在显式硬件过滤时，按该实例意图的持久 `scaler_actions.launch_spec`，匹配当前批准配置中唯一的 launch / Lium manifest，然后将该 manifest 的 `minimum_vram_mib` 转换为字节传给 `BootConfig`。不能用第一项、全列表最小值或用户任务字段降低门槛；缺失、歧义、错配均拒绝。

旧无过滤 manifest (`minimum_vram_mib=0`) 继续使用原 90 GiB 默认；某个新候选有较低批准门槛也不会改变另一旧候选的门槛。`BootConfig` 既有最低 30 GiB 底线保留；没有修改历史 `lium_bootstrap.py` 或单机 `production_worker.py`。

显存筛选只允许机器进入启动资格测试。原 FL50 → 首尾帧 4 步 → REF 4 步流程、模型 revision、文件检查、同一实例证据、排空与采集保护不变；全部输出实际核验成功后才能建立 fleet / 注册 worker。没有把 80GB 的报价、候选名称或 runtime memory 报告当成生成资格。

修改：`studio_platform/production_scaler_boot.py`；新增独立测试 `test_platform_production_boot_memory.py`。

离线验证：新增 8 个测试通过。此前新增 6 个与现有 `ProductionBootTests`、`BootTests` 合并 32 个通过；最后两项验证 30 GiB 边界及 MiB → byte 精确转换。其他覆盖包括：旧 90 GiB拒绝80 GiB、exact manifest允许80 GiB进入尚未通过的资格流程、其他候选不能借用较低门槛、持久动作缺失 / 错配拒绝、实际显存不足拒绝、80 GiB假主机全部三阶段成功前没有注册 worker。

使用现有 `.venv/Scripts/python.exe -m unittest`，全部主机 / backend / provider 为 fake；此证据是控制流程测试，不是 H100 运行速度或画质 benchmark。生产候选选择与批准配置验证由主任务继续完成，真实实例仍须独立验收。
