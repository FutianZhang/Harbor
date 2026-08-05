# 消息队列数据协议说明 (v1.0)

## 1. 输入队列: `engineered_prompt`
- 格式: JSON
- 字段说明:
  - `conversation_id` (String): 必填，全局唯一的会话 ID。
  - `base_model_path` (String): 必填，基础模型路径。如果包含 `vision` 则需路由至多模态处理器；包含 `instruct` 或 `chat` 则路由至对话处理器。
  - `adapter_path` (String): 可选，LoRA 等微调权重路径。
  - `file_url` (String): 可选，多模态任务所需的本地文件绝对路径。
  - `input` (List[Dict]): 必填，标准的 OpenAI 风格角色对话列表。
  - `generation_args` (Dict): 可选，推理参数。若缺省则使用 Handler 自带的默认值。

## 2. 输出队列: `inference_results`
- 格式: JSON
- 字段说明:
  - `conversation_id` (String): 必填，原样返回输入时的会话 ID，以便前端回调。
  - `result` (String): 必填，模型生成的最终文本。如果是异常报错，请将错误信息填入此字段。