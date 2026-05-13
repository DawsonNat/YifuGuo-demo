from openai import OpenAI

# 1. 初始化客户端，替换为你的专属地址和 Token
client = OpenAI(
    api_key="sk-Jn4FOEeEptIe8JWKkyIO1g",
    # 注意：标准 OpenAI SDK 往往需要加上 /v1 后缀，具体视你们的 Nginx/网关配置而定
    base_url="https://litellm.local.lexmount.net/v1" 
)

def chat_with_litellm():
    try:
        # 2. 使用 with_raw_response 发起请求，这样才能拿到 Headers
        raw_response = client.chat.completions.with_raw_response.create(
            model="deepseek-v4-flash",
            messages=[
                {"role": "system", "content": "你是一个得力的 AI 助手。"},
                {"role": "user", "content": "你好，请做个自我介绍。"}
            ]
        )

        # 3. 从响应头中提取本次请求的计算成本 (Cost)
        cost = raw_response.headers.get('x-litellm-response-cost')
        
        # 4. 解析标准的 JSON 响应体获取内容
        parsed_json = raw_response.parse()
        reply_content = parsed_json.choices[0].message.content
        total_tokens = parsed_json.usage.total_tokens

        # 打印结果
        print("=== 请求结果 ===")
        print(f"回复内容: {reply_content}")
        print(f"消耗 Tokens: {total_tokens}")
        print(f"本次请求成本: ${cost if cost else '未返回(请检查服务端配置)'}")

    except Exception as e:
        print(f"请求发生错误: {e}")

if __name__ == "__main__":
    chat_with_litellm()