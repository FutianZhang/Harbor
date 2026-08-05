# 任务背景
你是一名高级后端开发工程师。目前团队正在研发一款支持多模态和多硬件设备（包括 Intel HPU 和 CUDA）的大语言模型应用框架。你需要编写一个基于 RabbitMQ 的模型推理调度服务模块。

# 数据协议与参考（请务必查阅）
为了确保你编写的解析逻辑完全符合前端系统的预期，我已经将高仿真的请求报文示例和数据协议说明放在了你的只读工作空间中：
- **查阅数据结构**：请读取并分析 `/app/input_files/sample_request.json`，了解真实的输入报文层级。
- **查阅协议约定**：请参考 `/app/input_files/mq_schema.md`，获取完整的输入/输出队列字段约定（如必填项 `conversation_id`、特征路由依据 `base_model_path` 等）。
请严格按照上述文件中的协议规范进行消息的解析、路由判断和结果封装。

# 业务逻辑要求
1. **服务初始化**：使用 `pika` 连接 RabbitMQ。需从环境变量读取 `MQ_HOST`, `MQ_PORT`, `MQ_USER`, `MQ_PASS`（请在代码中提供缺失时的容错默认值）。
2. **队列配置与监听**：监听 `engineered_prompt` 输入队列，结果发送到 `inference_results`。为了保障生产环境稳定性，请务必设置队列的持久化（durable=True）以及合理的消息预取数量（prefetch_count）。
3. **处理器路由**：根据 `base_model_path` 的特征字符串动态选择处理类：包含 `vision` 则使用 `VisionHandler`，包含 `instruct` 或 `chat` 则使用 `ChatHandler`，否则使用 `PipelineHandler`。必须具备避免重复实例化的 Handler 缓存设计。
4. **消息处理与保障**：接收并解析 JSON 输入，路由并执行推理；在推理完成后需要对结果进行显式的特殊 token 剔除（不仅是 strip）。异常发生时不能直接崩溃，需将错误报文返回给前端。最后，必须在绝对安全的区块中对消息进行 basic_ack 应答。

# 路径与硬约束（重要！）
- **源文件目录**：任何参考文档均在 `/app/input_files/` 下，该目录为**严格只读**，禁止任何写入操作。
- **工作与输出目录**：你必须且只能将最终的代码文件输出到 `/app/output/` 目录下。不得写入任何其他绝对路径。
- **禁止硬编码**：绝对禁止在代码中明文写入真实的 RabbitMQ 密码、IP 或任何密钥信息，必须依赖环境变量。
- **禁止危险操作**：严禁使用 `eval()` 或 `exec()` 等高危动态方法解析前端 Payload。

# 交付物清单
| path | required | desc |
| --- | --- | --- |
| backend_service.py | true | 主交付物：基于 RabbitMQ 的推理服务模块代码 |