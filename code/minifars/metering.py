# -*- coding: utf-8 -*-
"""MeteringMiddleware 最小版（设计文档 §6.2，对应 framework PR2 候选组件）。

职责：每次 LLM 调用记录 {阶段, agent, 模型, in_tokens, out_tokens, latency, cost}，
逐条追加写入 workspace/<project>/metering/calls.jsonl；
resource_report.md（D5）将直接由该流水自动生成，保证零人工誊抄。

设计要点：
- 与具体 LLM SDK 解耦：只要求调用方在 after 钩子提供 usage 字典；
- 同步/异步调用均可用 wrap/awrap 包裹计时；
- 阶段级"编排流水"也走同一 record()，保证流水口径统一。
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

# 价格表：USD / 百万 token。未列出的模型按 0 计费并在 extra 标记 price_unknown。
# MiniMax-M2 订阅制（TokenPlanPlus）无按量单价，此处记 0，成本维度以 token 数为准。
DEFAULT_PRICES: Dict[str, Dict[str, float]] = {
    "MiniMax-M2": {"input": 0.0, "output": 0.0},
    "embedding-3": {"input": 0.0, "output": 0.0},
}


@dataclass
class CallRecord:
    """单条计量流水（与 calls.jsonl 每行一一对应）。"""

    ts: str
    stage: str
    agent: str
    model: str
    in_tokens: int = 0
    out_tokens: int = 0
    latency_ms: int = 0
    cost_usd: float = 0.0
    status: str = "ok"
    error: Optional[str] = None
    record_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    extra: Dict[str, Any] = field(default_factory=dict)


class MeteringMiddleware:
    """token/时长/成本统一计量中间件。

    Args:
        metering_dir: 流水目录（workspace/<project>/metering/）
        prices: 价格表覆盖，结构同 DEFAULT_PRICES
    """

    FILENAME = "calls.jsonl"

    def __init__(self, metering_dir: Path | str, prices: Optional[Dict[str, Dict[str, float]]] = None):
        self.dir = Path(metering_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.prices = {**DEFAULT_PRICES, **(prices or {})}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ core
    def _cost(self, model: str, in_tokens: int, out_tokens: int) -> tuple[float, bool]:
        p = self.prices.get(model)
        if p is None:
            return 0.0, True
        return (in_tokens * p["input"] + out_tokens * p["output"]) / 1e6, False

    def record(
        self,
        stage: str,
        agent: str,
        model: str,
        in_tokens: int = 0,
        out_tokens: int = 0,
        latency_ms: int = 0,
        status: str = "ok",
        error: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> CallRecord:
        """写入一条流水并原样返回（调用方可用于断言/日志）。"""
        cost, price_unknown = self._cost(model, in_tokens, out_tokens)
        rec = CallRecord(
            ts=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            stage=stage,
            agent=agent,
            model=model,
            in_tokens=int(in_tokens),
            out_tokens=int(out_tokens),
            latency_ms=int(latency_ms),
            cost_usd=round(cost, 6),
            status=status,
            error=error,
            extra={**(extra or {}), **({"price_unknown": True} if price_unknown else {})},
        )
        line = json.dumps(asdict(rec), ensure_ascii=False)
        with self._lock:
            with open(self.dir / self.FILENAME, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        return rec

    # ------------------------------------------------------------- wrappers
    @staticmethod
    def _usage_from(result: Any) -> tuple[int, int]:
        """从常见响应结构中尽力提取 (in_tokens, out_tokens)。"""
        usage = getattr(result, "usage", None) or (result.get("usage") if isinstance(result, dict) else None)
        if not usage:
            return 0, 0
        get = usage.get if isinstance(usage, dict) else lambda k, d=0: getattr(usage, k, d)  # noqa: E731
        in_t = get("input_tokens", 0) or get("prompt_tokens", 0)
        out_t = get("output_tokens", 0) or get("completion_tokens", 0)
        return int(in_t or 0), int(out_t or 0)

    def wrap(
        self,
        fn: Callable[..., Any],
        *,
        stage: str,
        agent: str,
        model: str,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Callable[..., Any]:
        """同步包裹：自动计时 + 提取 usage + 落流水。"""

        def wrapped(*args: Any, **kwargs: Any) -> Any:
            t0 = time.perf_counter()
            try:
                result = fn(*args, **kwargs)
            except Exception as e:  # noqa: BLE001
                self.record(stage, agent, model,
                            latency_ms=int((time.perf_counter() - t0) * 1000),
                            status="error", error=f"{type(e).__name__}: {e}", extra=extra)
                raise
            in_t, out_t = self._usage_from(result)
            self.record(stage, agent, model, in_t, out_t,
                        latency_ms=int((time.perf_counter() - t0) * 1000), extra=extra)
            return result

        return wrapped

    def awrap(
        self,
        fn: Callable[..., Awaitable[Any]],
        *,
        stage: str,
        agent: str,
        model: str,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Callable[..., Awaitable[Any]]:
        """异步包裹：语义同 wrap。"""

        async def wrapped(*args: Any, **kwargs: Any) -> Any:
            t0 = time.perf_counter()
            try:
                result = await fn(*args, **kwargs)
            except Exception as e:  # noqa: BLE001
                self.record(stage, agent, model,
                            latency_ms=int((time.perf_counter() - t0) * 1000),
                            status="error", error=f"{type(e).__name__}: {e}", extra=extra)
                raise
            in_t, out_t = self._usage_from(result)
            self.record(stage, agent, model, in_t, out_t,
                        latency_ms=int((time.perf_counter() - t0) * 1000), extra=extra)
            return result

        return wrapped

    # -------------------------------------------------------------- summary
    def load(self) -> list[CallRecord]:
        path = self.dir / self.FILENAME
        if not path.exists():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(CallRecord(**json.loads(line)))
        return out

    def summarize(self) -> Dict[str, Any]:
        """阶段 × agent 汇总，供 resource_report.md 生成器使用（D5）。"""
        records = self.load()
        agg: Dict[tuple, Dict[str, float]] = {}
        for r in records:
            key = (r.stage, r.agent)
            slot = agg.setdefault(key, {"calls": 0, "in_tokens": 0, "out_tokens": 0,
                                        "latency_ms": 0, "cost_usd": 0.0, "errors": 0})
            slot["calls"] += 1
            slot["in_tokens"] += r.in_tokens
            slot["out_tokens"] += r.out_tokens
            slot["latency_ms"] += r.latency_ms
            slot["cost_usd"] += r.cost_usd
            slot["errors"] += int(r.status != "ok")
        total = {
            "calls": len(records),
            "in_tokens": sum(r.in_tokens for r in records),
            "out_tokens": sum(r.out_tokens for r in records),
            "cost_usd": round(sum(r.cost_usd for r in records), 6),
        }
        return {"by_stage_agent": {f"{k[0]}/{k[1]}": v for k, v in sorted(agg.items())},
                "total": total}
