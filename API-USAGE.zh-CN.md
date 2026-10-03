# 映序与 H3 的实际 v1 API

这份文档对应 `studio_platform/api.py` 的实现。目标公网入口是 `https://www.sixnine.art/v1`；当前公网尚未部署。本机预览经 `http://localhost:8850/v1` 代理到8845，仅使用明确标注的CPU模拟与CPU粗剪。真实H3执行/云租赁默认关闭。

映序网页直接使用这一套API与项目文稿。其他小说或制作服务可以作为受限服务客户端接入；不需要把ComfyUI暴露到公网，也不需要拥有GPU云账号或对象存储主密钥。

## 身份与项目归属

网页使用HttpOnly会话Cookie；正式模式必须密码登录。`superdan`、`supervan`的项目、素材、任务和下载按服务端认证身份隔离。网页向所有`/v1`请求加`X-Expected-Account`，服务端发现Cookie实际账号改变会在操作前返回409、`code=account_context_changed`；前端应保存旧账号草稿并重新核对身份。响应头`X-Authenticated-Account`是非秘密账号名，可核对迟到响应。

机器客户端使用`Authorization: Bearer ...`，绑定一个owner、明确的项目ID列表及scopes。由操作员在受保护终端运行`python -m studio_platform.manage register-client --help`查看入口；token只通过隐藏交互输入、存储哈希，不作为命令参数或文档内容。它不是用户密码，也不是供应商API key。注册/轮换权限不开放给网页或CI部署身份。

可授予：`projects:read`、`assets:read`、`assets:write`、`jobs:read`、`jobs:write`。机器客户端不能创建未授权项目、覆盖项目文稿或查询整个账户配额。先由创作者建立项目，再绑定该项目；不要为方便把superdan会话Cookie发给别的服务。机器token只存放在受信第三方服务端/BFF，不嵌入公开网页JavaScript或浏览器本地配置。

本项目供应商配置仍按中央 `C:/Users/danmo/Desktop/AI-Registry/API_USAGE.md` 加载。上述外部客户端token没有被本轮自动创建或导入中央profile；不要把资源ID当model ID或凭空写一个可用profile。

## 已实现端点

| 操作 | 路径 | 主要约束 |
|---|---|---|
| 查看模型控制与输入限制 | GET `/v1/capabilities` | 用准确recipe/model/能力版本，不假定所有执行池可用 |
| 列出项目 | GET `/v1/projects?limit=100&offset=0` | limit 1–100；授权过滤发生在SQL分页之前 |
| 读文稿 | GET `/v1/projects/{project_id}` | v4文稿、服务端version与更新时间 |
| 创建/保存文稿 | POST `/v1/projects`；PUT `/v1/projects/{project_id}` | 仅网页登录；PUT携带expected_version，冲突不会覆盖 |
| 上传素材 | POST `/v1/assets` | multipart file、client_project_id、可选稳定client_asset_id；实际解码/校验 |
| 读素材列表/状态 | GET `/v1/assets?client_project_id=...`；GET `/v1/assets/{id}` | 只在ready后可用于计划 |
| 创建音视频选段 | POST `/v1/assets/{id}/derivatives` | JSON start/end秒；保留原件，不在生成请求里偷偷裁剪 |
| 恢复已接收素材处理 | POST `/v1/assets/{id}/resume` | 同一asset；不等于浏览器断线按字节续传 |
| 创建H3预检计划 | POST `/v1/generation-plans` | 输出实际参数/尺寸/片长、blockers、期限及预算性质 |
| 创建章节粗剪计划 | POST `/v1/render-plans` | 从服务端文稿编译已采用视频/音轨时间线 |
| 确认任务 | POST `/v1/jobs` | body只有plan_id；必须带稳定Idempotency-Key |
| 查任务/列表 | GET `/v1/jobs/{id}`；GET `/v1/jobs` | 可按client_project_id；limit 1–100，offset非负 |
| 取消 | POST `/v1/jobs/{id}/cancel` | 已提交的任务需确认上游取消，不能立即算免费/释放槽 |
| 批次 | POST/GET `/v1/batches`；GET `/v1/batches/{id}` | 列表limit 1–10、offset，返回逐项状态摘要及has_more/next_offset；单批详情含job结果；重复请求不重做已接受项 |
| 批次恢复/取消 | 恢复重发POST `/v1/batches`；取消POST `/v1/batches/{id}/cancel` | 恢复必须保持原body与Idempotency-Key，只有可恢复步骤继续 |
| 项目任务总览 | GET `/v1/activity-summary?client_project_id=...` | 全部状态聚合，与列表已加载页数分开 |
| 结果清单 | GET `/v1/jobs/{id}/artifacts` | 类型、SHA256、大小、私有content/download URL |
| 下载结果/素材 | GET或HEAD `/v1/artifacts/{id}/content`、`/v1/assets/{id}/content` | `?download=1`附件；Local支持Range；始终鉴权 |

Swagger/OpenAPI由服务生成，访问仍需本账户身份。表中的模型支持是H3版本化配方的控制契约，当前未接通的Engy/Boyesir、图像生成、音乐生成、Marble不会因为有下拉框就自动可用。

## H3请求与确认

现有两个配方：`h3-base-fl2va-v1`首尾帧/文生音视频、`h3-base-ref2va-v1`参考生成。准确上游模型标识是`MiniMax-H3-Base-BF16`；它不等于配方ID，也没有被静默替换成VDN或供应商小模型。

下面是请求结构示例，ID必须换成当前账号真实已有的项目、镜头和ready素材。这里没有执行生成，也没有给出凭据值。

```json
{
  "client_ref": {"project_id": "existing-project", "shot_id": "existing-shot", "shot_version": 1},
  "recipe_id": "h3-base-ref2va-v1",
  "prompt": "描述当前镜头中的人物、动作、环境与镜头运动",
  "inputs": {
    "images": ["ready-image-id"],
    "videos": [{"asset_id": "ready-video-segment-id", "include_audio": false}],
    "audios": ["ready-audio-segment-id"]
  },
  "controls": {
    "duration": 5,
    "resolution": "480P",
    "aspect_ratio": "16:9",
    "seed": "18446744073709551615",
    "steps": 50,
    "generate_audio": true
  }
}
```

先POST计划并展示返回的effective_request/output_spec/estimate/blockers。预算预留额不等于最终供应商账单。`status=ready`表示可接受确认；`execution.admission_state=waiting_capacity`表示确认后先等开机和模型验收，不保证马上生成。无已批准策略、报价、预算或能力时会blocked。

用户确认后POST `/v1/jobs`，body为`{"plan_id":"returned-plan-id"}`，Idempotency-Key由客户端生成并持久保存，例如一个新UUID。网络超时重试必须保持**同一plan与同一key**；不能每点一次生成新key。若修改文稿/角色/参考，先获取新计划，旧结果保留为旧版本候选。

当前参考上限9图/3视频/3音频、普通参考总数12；每份音视频选段2–15秒，视频选段合计与音频选段合计分别不超过15秒；时间锚点最多8个。上传单文件默认512MiB。首尾帧、锚点、原声开关和更多控制以能力接口为准；音频VAE分块当前不可用，会明确拒绝而非忽略。

## 状态、恢复和下载

章节粗剪使用`POST /v1/render-plans`，请求包含`client_ref.project_id/chapter_id`、`resolution`（480P或720P），可选`aspect`（16:9、9:16、1:1、4:3、3:4，缺省沿用项目画幅）及`burn_subtitles`（默认false）。镜头选段、声音和字幕正文从当前账户已保存的项目文稿读取；不能把任意FFmpeg/ASS/字体路径传给接口。开启烧录前须人工确认这一版字幕与时间线，返回的`timeline.subtitles`给出实际24fps帧/秒范围。新计划为render v3，需要对应配置的合格CPUworker；历史v1/v2不能被静默改写。详细限制见[字幕契约](CAPTION-BURN-IN-CONTRACT.zh-CN.md)。

典型路径：waiting_capacity → queued → claimed → submitting → running → collecting → succeeded。未知提交为submission_unknown；此时继续查同一个job，不新投。cancel_requested需要上游核对。recovery_hold表示灾难恢复或状态与attempt证据矛盾/缺失，需操作员核对；不允许从网页恢复成新的付费执行。

任务succeeded后取artifacts，使用同身份访问content_url或download_url。MP4与实际返回的独立音频artifact各有大小/哈希；只有清单包含FLAC时才承诺独立声音文件，H3有声任务或CPU粗剪都不应仅凭开关猜文件存在。`result.billing_status=pending`时已校验的成片仍可下载；`actual_cost=null`代表费用未确认，不代表免费。保存后核对SHA256；不要把临时签名URL作为作品长期ID。如果对象存储返回重定向，客户端不应将Authorization转发给别的主机；签名查询串不得写日志。API下载通过的证据与浏览器最终保存到磁盘是两件事。

轮询建议从2秒开始，持续运行时降到5–10秒；429遵守Retry-After，401重新鉴权，409核对版本/账号/计划，503保留本地稿后重试。错误详情不应被当成可重放的供应商请求。当前没有已部署webhook，不用假定job callback已经可用。

单进程入口同时最多32个HTTP请求；鉴权数据库访问并发8、密码登录并发2，每个owner最多8个请求，私有下载每owner2个/全局4个，上传每owner2个/全局4个。多个机器客户端共享其owner额度；这些是保护应用的初始限制，不是GPU任务并发。流式下载占用到ASGI传输结束或断连才释放，超限返回429及Retry-After。跨进程/多主机入口需要共享限流或反向代理配置，不能把每个进程的上限当作整个集群上限。

映序客户端将GET读取限制为总4并发、其中媒体2并发；只对429按Retry-After有限重试，最多额外2次，账户切换会中止队列和旧响应。POST/PUT等写入、上传和生成确认仍不自动重试。第三方客户端应采用同等节流和持久幂等策略，不能以大量HTTP请求增加GPU吞吐。

登录正文接收总期限15秒；每次响应写入空闲期限30秒，私有媒体响应总期限300秒。超时若尚未发送响应头则504，已发送部分媒体时中止连接，不在视频中拼接错误JSON，也不重新生成。请求结束显式关闭流式文件句柄；声明长度与实际输出不符时拒绝发送假完成标记。已启动的同步磁盘读取必须等线程归还，超时不能安全强杀线程，等待期间仍保留名额和句柄。客户端可重新鉴权读取原文件，Local支持Range继续下载。这是单进程CPU入口的资源保护，不是任务生成耗时上限。

## 验证范围

本轮本地HTTP契约覆盖双用户、受限机器scope、分页、同名项目跨账号操作阻挡、版本冲突、幂等批次、私有Range/附件与哈希。SQLite和隔离PostgreSQL均有测试；CPU粗剪实际编码输出MP4/FLAC。真实H3新GPU池、公网域名、供应商API与云存储在线读写需要分别验收。
