# -*- coding: utf-8 -*-
"""LLM 客户端：Anthropic 协议（MiniMax TokenPlanPlus 订阅 Key）+ 计量中间件。

D1 最小版：同步 httpx 调用 /v1/messages，每次调用经 MeteringMiddleware 落流水。
D2 起各 Swarm 子 Agent 统一经 LLMClient.chat() 访问模型，禁止绕过（计量红线）。
后续将提供 openjiuwen rail 适配器，把框架内调用纳入同一流水（PR2）。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import httpx

from .config import TierModel
from .metering import MeteringMiddleware

#: 可重试的服务端状态码（限流 + 瞬时故障）
RETRYABLE_STATUS = (429, 500, 502, 503)
#: 每次重试前的退避秒数（订阅制端点限流是常态，退避后通常恢复）
RETRY_BACKOFFS = (5.0, 15.0)


class LLMError(RuntimeError):
    """携带服务端 base_resp/错误体的调用失败。"""


class LLMClient:
    """单模型客户端；工厂方法 from_env() 按分级路由产出实例。"""

    def __init__(self, api_base: str, api_key: str, model: TierModel,
                 metering: MeteringMiddleware, stage: str = "unscheduled",
                 agent: str = "unknown", timeout: float = 120.0):
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.metering = metering
        self.stage = stage
        self.agent = agent
        self.timeout = timeout

    def bind(self, stage: str, agent: str) -> "LLMClient":
        """返回绑定阶段/Agent 身份的浅拷贝（流水归因用）。"""
        return LLMClient(self.api_base, self.api_key, self.model, self.metering,
                         stage=stage, agent=agent, timeout=self.timeout)

    def chat(self, prompt: str, system: Optional[str] = None,
             max_tokens: Optional[int] = None,
             history: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """单轮/多轮调用，返回原始响应 dict（含 usage）。"""
        messages = list(history or []) + [{"role": "user", "content": prompt}]
        payload: Dict[str, Any] = {
            "model": self.model.name,
            "max_tokens": max_tokens or self.model.max_tokens,
            "temperature": self.model.temperature,
            "messages": messages,
        }
        if system:
            payload["system"] = system

        def _call() -> Dict[str, Any]:
            """单次调用，对限流/瞬时故障做有限次退避重试（RETRY_BACKOFFS）：
            一次 429 不应作废整段流水线的既有 token 消耗。"""
            for attempt in range(len(RETRY_BACKOFFS) + 1):
                try:
                    resp = httpx.post(
                        f"{self.api_base}/v1/messages",
                        headers={"x-api-key": self.api_key,
                                 "anthropic-version": "2023-06-01",
                                 "content-type": "application/json"},
                        json=payload, timeout=self.timeout,
                    )
                except httpx.TimeoutException as exc:
                    if attempt < len(RETRY_BACKOFFS):
                        time.sleep(RETRY_BACKOFFS[attempt])
                        continue
                    raise LLMError(f"超时（重试耗尽）: {exc}") from exc
                if (resp.status_code in RETRYABLE_STATUS
                        and attempt < len(RETRY_BACKOFFS)):
                    time.sleep(RETRY_BACKOFFS[attempt])
                    continue
                if resp.status_code != 200:
                    raise LLMError(f"HTTP {resp.status_code}: {resp.text[:300]}")
                data = resp.json()
                base = data.get("base_resp") or {}
                if base.get("status_code") not in (0, None):
                    raise LLMError(f"base_resp {base.get('status_code')}: "
                                   f"{base.get('status_msg')}")
                return data
            raise LLMError("unreachable")  # for 循环必然 return/raise

        wrapped = self.metering.wrap(
            _call, stage=self.stage, agent=self.agent, model=self.model.name)
        return wrapped()

    @staticmethod
    def text_of(response: Dict[str, Any]) -> str:
        """提取响应正文文本。

        注意：MiniMax-M2 会先产出 thinking 块，thinking 与 text 共享
        max_tokens 预算；预算过小时可能只有 thinking 而无 text。
        """
        texts = [b.get("text", "") for b in response.get("content", [])
                 if b.get("type") == "text"]
        body = "".join(texts)
        if not body:  # 兜底：仅 thinking 块时返回思考内容并标注
            thinking = "".join(b.get("thinking", "") for b in response.get("content", [])
                               if b.get("type") == "thinking")
            if thinking:
                return f"[only thinking, no text; 建议增大 max_tokens] {thinking}"
        return body


def build_client(env: Dict[str, str], tier: TierModel, metering: MeteringMiddleware,
                 stage: str = "unscheduled", agent: str = "unknown") -> LLMClient:
    """从 .env 四件套构造客户端；MiniMax 订阅 Key 固定走 Anthropic 协议。"""
    if tier.provider != "Anthropic":
        raise NotImplementedError(f"D1 仅实现 Anthropic 协议路由，收到 provider={tier.provider}")
    api_base, api_key = env.get("API_BASE"), env.get("API_KEY")
    if not api_base or not api_key:
        raise LLMError(".env 缺少 API_BASE/API_KEY（检查 ~/.jiuwenswarm/.env）")
    return LLMClient(api_base, api_key, tier, metering, stage=stage, agent=agent)
