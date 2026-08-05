import os
import json
import pika
# import transformers, torch, peft 等机器学习相关库 (此处隐去实际引入)

# ==========================================
# 1. 辅助工具函数 (内部核心实现已隐去)
# ==========================================

def fix_lora_weights(peft_model, adapter_path):
    """
    [关键实现已隐去]
    修复 LoRA 权重：处理 safetensors 映射等特定逻辑。
    """
    # 实际实现中包含读取 safetensors 并修正 key 的逻辑
    print(f"Applying LoRA weights fix for {adapter_path}...")
    return peft_model

def extract_text_from_pdf(file_obj):
    """
    [关键实现已隐去]
    从 PDF 或其他文件中提取文本。
    """
    return "[Extracted Document Content]"

def compose_prompt(input_list):
    """
    [关键实现已隐去]
    核心 Prompt 工程逻辑。
    根据前端传来的 input_list（历史对话记录、拒答反馈等），组装最终的 Prompt 模型输入。
    """
    # 样例默认仅返回最后一条 user 消息
    for msg in reversed(input_list):
        if msg["role"] == "user":
            return msg["content"]
    return ""

def extract_model_answer(text):
    """
    [关键实现已隐去]
    后处理逻辑，提取模型生成的干净文本，去除 special tokens。
    """
    return text.strip()

def parse_input(message, is_vision_model=False): 
    """
    解析前端传入的消息结构，处理文件下载/读取，并拼接最终输入。
    """
    input_list = message.get("input", [])
    file_path = message.get("file_url", None)
    prompt_text = compose_prompt(input_list)
    
    if is_vision_model:
        if not file_path:
            raise ValueError("No image file provided for vision model.")
        # 返回多模态特定的输入格式
        return [
            {
                "role": "user",
                "content": [
                    {"type": "image", "url": file_path},
                    {"type": "text", "text": prompt_text}
                ]
            }
        ]
    else:
        # [文件解析逻辑已简化] 
        file_text = ""
        if file_path:
            print(f"Processing file from: {file_path}")
            file_text = "\n" + extract_text_from_pdf(None)
            
        return prompt_text + file_text


# ==========================================
# 2. 模型推理 Handler 抽象基类及实现
# ==========================================

class PipelineHandler:
    def __init__(self, base_model_path, device, adapter_path=None):
        """[关键实现已隐去] 初始化基础/微调文本模型"""
        self.base_model_path = base_model_path
        self.adapter_path = adapter_path
        self.device = device
        # 实际代码: 加载 AutoModelForCausalLM, Tokenizer 等

    @property
    def default_generation_args(self):
        return {"max_new_tokens": 512, "temperature": 0.4}

    def infer(self, prompt, **generation_args):
        """[关键实现已隐去] 执行模型前向传播，返回生成结果"""
        print(f"[PipelineHandler] Running inference on {self.device}...")
        return "This is a mocked response from PipelineHandler."


class ChatHandler:
    def __init__(self, base_model_path, device, adapter_path=None):
        """[关键实现已隐去] 初始化对话特定模型 (Apply Chat Template)"""
        self.base_model_path = base_model_path
        self.adapter_path = adapter_path
        self.device = device

    @property
    def default_generation_args(self):
        return {"max_new_tokens": 512}

    def infer(self, prompt, **generation_args):
        """[关键实现已隐去] 包含 apply_chat_template 及流式/非流式生成逻辑"""
        print(f"[ChatHandler] Running inference on {self.device}...")
        return "This is a mocked response from ChatHandler."


class VisionHandler:
    def __init__(self, base_model_path, device):
        """[关键实现已隐去] 初始化视觉多模态模型 (Processor & VisionModel)"""
        self.base_model_path = base_model_path
        self.device = device

    @property
    def default_generation_args(self):
        return {"max_new_tokens": 100}

    def infer(self, prompt, **generation_args):
        """[关键实现已隐去] 处理图像和文本的联合特征并生成结果"""
        print(f"[VisionHandler] Running inference on {self.device}...")
        return "This is a mocked response from VisionHandler."


# 路由判断逻辑
def needs_chat_handler(base_model_path):
    return "instruct" in base_model_path.lower() or "chat" in base_model_path.lower()

def is_vision_handler(base_model_path):
    return "vision" in base_model_path.lower()


# ==========================================
# 3. 消息队列服务 (与前端的交互核心)
# ==========================================

def main():
    # 配置信息 (敏感信息已替换为环境变量或占位符)
    rabbitmq_host = os.environ.get("MQ_HOST", "127.0.0.1")
    rabbitmq_port = int(os.environ.get("MQ_PORT", 5672))
    rabbitmq_user = os.environ.get("MQ_USER", "guest")
    rabbitmq_pass = os.environ.get("MQ_PASS", "guest")
    input_queue = "engineered_prompt"    # 接收前端请求的队列
    output_queue = "inference_results"   # 发送推理结果到前端的队列

    # 连接 RabbitMQ
    credentials = pika.PlainCredentials(rabbitmq_user, rabbitmq_pass)
    connection = pika.BlockingConnection(
        pika.ConnectionParameters(host=rabbitmq_host, port=rabbitmq_port, credentials=credentials)
    )
    channel = connection.channel()
    channel.queue_declare(queue=input_queue, durable=True)
    channel.queue_declare(queue=output_queue, durable=True)
    channel.basic_qos(prefetch_count=1)

    handler_cache = {}

    def get_handler(base_model_path, device, adapter_path=None):
        """模型处理器的工厂模式 + 内存缓存"""
        key = (base_model_path, adapter_path, device)
        if key not in handler_cache:
            if is_vision_handler(base_model_path):
                handler_cache[key] = VisionHandler(base_model_path, device)
            elif needs_chat_handler(base_model_path):
                handler_cache[key] = ChatHandler(base_model_path, device, adapter_path)
            else:
                handler_cache[key] = PipelineHandler(base_model_path, device, adapter_path)
        return handler_cache[key]

    def on_message(ch, method, properties, body):
        """消费来自前端的 JSON 请求，处理并发布回传"""
        try:
            # 1. 协议解析
            message = json.loads(body)
            base_model_path = message.get("base_model_path")
            adapter_path = message.get("adapter_path")
            device = "cuda" # 或者 hpu / cpu
            conversation_id = message.get("conversation_id")

            if not base_model_path:
                print("Error: Invalid message format.")
                ch.basic_ack(delivery_tag=method.delivery_tag)
                return
            
            # 2. 路由到具体的 Handler
            handler = get_handler(base_model_path, device, adapter_path)

            # 3. 提取与组装上下文
            try:
                is_vision = is_vision_handler(base_model_path)
                parsed_prompt = parse_input(message, is_vision_model=is_vision)
            except ValueError as ve:
                # 异常处理，例如用户未上传图片但请求了视觉模型
                response = {"conversation_id": conversation_id, "result": str(ve)}
                ch.basic_publish(exchange="", routing_key=output_queue, body=json.dumps(response))
                ch.basic_ack(delivery_tag=method.delivery_tag)
                return 
            
            # 4. 执行推理
            generation_args = message.get("generation_args", handler.default_generation_args)
            raw_result = handler.infer(parsed_prompt, **generation_args)
            final_result = extract_model_answer(raw_result)

            # 5. 打包结果推入返回队列
            response = {
                "conversation_id": conversation_id,
                "result": final_result,
            }
            ch.basic_publish(
                exchange="",
                routing_key=output_queue,
                body=json.dumps(response)
            )
            print(f"Processed conversation {conversation_id} successfully.")

        except Exception as e:
            print(f"System Error: {e}")
        finally:
            # 确保消息被正确 Ack，防止队列堆积
            ch.basic_ack(delivery_tag=method.delivery_tag)

    # 启动监听
    channel.basic_consume(queue=input_queue, on_message_callback=on_message)
    print("AI Inference Service initialized. Waiting for requests...")
    channel.start_consuming()

if __name__ == "__main__":
    main()