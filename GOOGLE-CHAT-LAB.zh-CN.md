# Google 创作助手试聊（本地，未做生成测试）

入口：http://127.0.0.1:8864/ 。独立比较页，不修改现有快速创作流程，不部署到公网。

可选择 `gemma-4-31b-it`、`gemini-3.8-flash`，也可同时发送比较；每个模型分别保留当前标签页的文本上下文。刷新清空，可导出。当前仅文字，不上传图片、音频、视频，不调用 H3 或 GPU。

启动：在本目录运行 `.venv/Scripts/python.exe tools/chat_model_lab.py`。监听 127.0.0.1:8864，仅允许本机同源提交。打开页面不向 Google 请求；点击发送才调用 generateContent，可能消耗账户额度。不会自动重试或替换模型。

凭据：复用中央 `api_registry.load_api('gemini', profile='gemini--user-supplied')`，严格保留 `https://generativelanguage.googleapis.com`。不新增凭据文件，不在浏览器传递密钥。

2026-10-05：两个准确 ID 的模型目录 GET 曾返回成功；用户随后要求不测试，未执行 generateContent、未做页面或集成测试。实际回答、速度与额度留待用户试用。这里只完成配置和调用适配，不代表生产验证通过。

每轮输出上限2048 tokens，最多20轮、60000字上下文。本地服务有同请求ID进程内去重，不提供重启后的持久幂等保障。网络中断时不要假定上游未执行。
