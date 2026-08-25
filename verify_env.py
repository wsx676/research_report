# -*- coding: utf-8 -*-
"""环境与 API 连通性验证脚本（仅用标准库）。

验证项：
1. MiniMax TokenPlanPlus（Anthropic 协议）主模型调用
2. 智谱 embedding-3 向量化调用
"""
import json
import os
import urllib.request

ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def load_env(path):
    env = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"')
    return env


def post_json(url, payload, headers, timeout=60):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def test_minimax(env):
    """MiniMax 主模型：Anthropic 协议 /v1/messages"""
    url = env["API_BASE"].rstrip("/") + "/v1/messages"
    headers = {
        "x-api-key": env["API_KEY"],
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": env["MODEL_NAME"],
        "max_tokens": 16,
        "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
    }
    status, data = post_json(url, payload, headers)
    text = ""
    for block in data.get("content", []):
        if block.get("type") == "text":
            text += block.get("text", "")
    usage = data.get("usage", {})
    print(f"[MiniMax] HTTP {status} | model={data.get('model')} | "
          f"reply={text.strip()!r} | usage={usage}")
    return True


def test_zhipu_embedding(env):
    """智谱 embedding-3：OpenAI 兼容 /embeddings"""
    url = "https://open.bigmodel.cn/api/paas/v4/embeddings"
    headers = {
        "Authorization": f"Bearer {env['ZHIPU_API_KEY']}",
        "content-type": "application/json",
    }
    payload = {"model": "embedding-3", "input": ["FARS 全自动科研系统连通性测试"]}
    status, data = post_json(url, payload, headers)
    dim = len(data["data"][0]["embedding"])
    print(f"[Zhipu] HTTP {status} | model={data.get('model')} | dim={dim}")
    return True


if __name__ == "__main__":
    env = load_env(ENV_PATH)
    results = {}
    for name, fn in [("MiniMax-M2 (Anthropic 协议)", test_minimax),
                     ("Zhipu embedding-3", test_zhipu_embedding)]:
        try:
            results[name] = fn(env)
        except Exception as e:  # noqa: BLE001
            print(f"[{name}] FAILED: {e}")
            results[name] = False
    print("=" * 50)
    print("结果汇总：" + " | ".join(f"{k}: {'PASS' if v else 'FAIL'}" for k, v in results.items()))
