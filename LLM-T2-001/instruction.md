# 任务背景
团队正在研发一款支持多模态和多硬件的大语言模型应用框架。你需要编写一个基于 RabbitMQ 的 Python 推理调度服务模块。

# 数据协议与参考
请查阅只读工作空间中的文件以对齐前端系统：
- 输入结构示例：`/app/input_files/sample_request.json`
- 队列与字段协议约定：`/app/input_files/mq_schema.md`

# 业务逻辑要求
1. **服务初始化**：使用 `pika` 连接 RabbitMQ，监听 `engineered_prompt` 队列，并将最终结果投递至 `inference_results` 队列。
2. **请求路由**：解析前端发来的请求报文，提取必要的模型路径及硬件参数。根据模型类型（如视觉模型、对话模型或基础管道）将其分发给对应的 Handler（VisionHandler / ChatHandler / PipelineHandler）进行处理。
3. **结果流转**：获取大模型的原始推理结果后，将其封装为协议要求的响应报文投递到输出队列，并完成消息确认。
4. **异常容错**：在处理单条消息时，若遇到解析错误或大模型显存溢出等异常，服务主进程绝对不能崩溃，需保持对后续消息的持续监听。

# 路径与硬约束
- **读取路径**：参考文档均在 `/app/input_files/`（严格只读）。
- **输出路径**：代码必须保存至 `/app/output/backend_service.py`。
- **安全规范**：严禁明文硬编码任何中间件的账密或 IP 地址；必须以安全的方式反序列化前端 Payload。