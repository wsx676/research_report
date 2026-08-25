# -*- coding: utf-8 -*-
"""配置加载：config.yaml（分级路由）+ .env（密钥四件套）。

加载顺序（后者不覆盖前者已有值）：
1. 进程环境变量
2. <repo>/.env（工作区根目录）
3. ~/.jiuwenswarm/.env（JiuwenSwarm 官方配置目录）

分级路由（§6.2）：strong = 假设生成/写作；light = 摘要/格式校验。
当前两档都指向 MiniMax-M2（订阅制内免费额度），后续可换轻量模型省额度。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "code" / "config.yaml"

ENV_KEYS = ("MODEL_PROVIDER", "MODEL_NAME", "API_BASE", "API_KEY",
            "EMBED_API_BASE", "EMBED_API_KEY", "EMBED_MODEL",
            "S2_API_KEY")  # Semantic Scholar 免费 key（可选，缓解 429 限流）


def _parse_env_file(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"')
    return out


def load_env(repo_root: Optional[Path] = None) -> Dict[str, str]:
    """合并三级 env 来源，只保留 ENV_KEYS 白名单。"""
    root = Path(repo_root) if repo_root else REPO_ROOT
    merged: Dict[str, str] = {}
    # 优先级低的先写，高的后覆盖
    for src in (Path.home() / ".jiuwenswarm" / ".env", root / ".env"):
        merged.update(_parse_env_file(src))
    for k in ENV_KEYS:
        if os.environ.get(k):
            merged[k] = os.environ[k]
    return merged


@dataclass
class TierModel:
    tier: str            # strong | light
    provider: str        # Anthropic | OpenAI
    name: str
    max_tokens: int = 2048
    temperature: float = 0.7


@dataclass
class PipelineConfig:
    tiers: Dict[str, TierModel] = field(default_factory=dict)
    prices: Dict[str, Dict[str, float]] = field(default_factory=dict)
    workspace_root: Path = REPO_ROOT / "workspace"
    raw: Dict = field(default_factory=dict)


def load_config(path: Optional[Path | str] = None) -> PipelineConfig:
    path = Path(path) if path else DEFAULT_CONFIG
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    tiers = {}
    for tier, m in (data.get("models") or {}).items():
        tiers[tier] = TierModel(tier=tier, provider=m.get("provider", "Anthropic"),
                                name=m["name"], max_tokens=m.get("max_tokens", 2048),
                                temperature=m.get("temperature", 0.7))
    ws = data.get("workspace_root")
    return PipelineConfig(
        tiers=tiers,
        prices=(data.get("metering") or {}).get("prices_usd_per_mtok") or {},
        workspace_root=Path(ws) if ws else REPO_ROOT / "workspace",
        raw=data,
    )
