# -*- coding: utf-8 -*-
"""PeerAgent / 同行质询者（设计文档 §5.1）：候选假设的新颖性查重与可行性质询。

职责：
1. 本地查重线索：候选假设与 survey_cards.json 的词重叠比对（不调 LLM，
   便宜且可单测），每个假设给出最相似的 top-k 文献标题；
2. strong 档 LLM 做一轮同行评审：新颖性担忧 / 可行性打分（0~1）/
   主要弱点 / 修改建议，落盘 peer_review_r{n}.json；
3. 质询意见交给 HypothesisAgent.refine（Lead）做辩论精炼（1~2 轮，
   由 pipeline.stage_ideation 编排）。

约束：假设字段进 prompt 前截断（上下文压缩红线，§6.2）。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .artifacts import load_cards, load_hypotheses
from .hypothesis import extract_json_array
from .llm import LLMClient

#: 每假设字段进 prompt 的截断（上下文压缩红线）
FIELD_LIMIT = 200
#: 查重线索返回的最相似卡片数
HINT_TOP_K = 2

STOPWORDS = {"the", "a", "an", "of", "for", "and", "to", "in", "on", "with",
             "via", "using", "based", "model", "models"}

PEER_SYSTEM = (
    "You are a critical peer reviewer for research hypotheses in an "
    "automated science pipeline. Be harsh on novelty overlap with prior "
    "work and on feasibility, but constructive in suggested fixes. "
    "Output ONLY a JSON array."
)


def _tokens(text: str) -> set:
    """中英混合分词：英文取 >=3 字母词，中文取字符 bigram（去停用词）。"""
    text = (text or "").lower()
    words = set(re.findall(r"[a-z]{3,}", text))
    for seg in re.findall(r"[\u4e00-\u9fff]+", text):
        if len(seg) == 1:
            words.add(seg)
        else:
            words.update(seg[i:i + 2] for i in range(len(seg) - 1))
    return words - STOPWORDS


def novelty_hints(hypotheses: List[Dict[str, Any]],
                  cards: List[Dict[str, Any]],
                  top_k: int = HINT_TOP_K) -> Dict[str, List[Dict[str, Any]]]:
    """每个假设 × 卡片（标题+摘要前缀）的词重叠 top-k（纯函数，供质询与审计）。"""
    card_tokens = [
        (str(c.get("title", "")),
         _tokens(str(c.get("title", "")) + " " + str(c.get("abstract", ""))[:200]))
        for c in cards
    ]
    hints: Dict[str, List[Dict[str, Any]]] = {}
    for h in hypotheses:
        ht = _tokens(str(h.get("title", "")) + " " + str(h.get("testable_prediction", "")))
        scored = [(len(ht & ct), t) for t, ct in card_tokens]
        scored = [(n, t) for n, t in scored if n > 0]
        scored.sort(key=lambda x: (-x[0], x[1]))  # 同分按标题序保证确定性
        hints[str(h.get("id", ""))] = [{"overlap": n, "title": t}
                                       for n, t in scored[:top_k]]
    return hints


def _clamp01(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.5  # 非法分数按"未知"处理


def _match_ids(items: List[Dict[str, Any]],
               reviews: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按 items 顺序对齐 LLM 质询结果；缺评的候选补占位（不静默丢失）。"""
    by_id = {str(r.get("id")): r for r in reviews if r.get("id")}
    out: List[Dict[str, Any]] = []
    for it in items:
        hid = str(it.get("id", ""))
        r = dict(by_id.get(hid) or {})
        r["id"] = hid
        r.setdefault("novelty_concern", "（质询缺失：占位）")
        r.setdefault("major_weakness", "占位")
        r.setdefault("suggested_fix", "占位")
        r["feasibility_score"] = _clamp01(r.get("feasibility_score", 0.5))
        out.append(r)
    return out


class PeerAgent:
    """同行质询者：新颖性查重 + 可行性打分（§5.1 PeerAgent）。"""

    def __init__(self, topic: Dict[str, Any], llm_strong: Optional[LLMClient],
                 proposals_dir: Path):
        self.topic = topic
        self.llm = llm_strong
        self.proposals_dir = Path(proposals_dir)

    def review(self, hypotheses_path: Path | str, cards_path: Path | str,
               round_no: int = 1) -> Dict[str, Any]:
        """质询当前候选假设，落盘 peer_review_r{n}.json，返回意见列表。"""
        items = load_hypotheses(hypotheses_path)
        cards = load_cards(cards_path)
        hints = novelty_hints(items, cards)
        if self.llm is None:
            reviews = self._placeholder_reviews(items)
        else:
            reviews = self._generate(items, hints)
        reviews = _match_ids(items, reviews)

        payload = {
            "schema": "v0",
            "round": round_no,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "novelty_hints": hints,
            "reviews": reviews,
        }
        self.proposals_dir.mkdir(parents=True, exist_ok=True)
        path = self.proposals_dir / f"peer_review_r{round_no}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        print(f"[peer] round {round_no} 质询 {len(reviews)} 条假设"
              f"（{'LLM' if self.llm else '占位'}）")
        return {"review": str(path), "items": reviews}

    # ------------------------------------------------------------ internals
    def _generate(self, items: List[Dict[str, Any]],
                  hints: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
        hypo_lines: List[str] = []
        for it in items:
            hid = str(it.get("id", ""))
            sim = "; ".join(h["title"] for h in hints.get(hid, [])) or "无"
            hypo_lines.append(
                f"- id={hid}\n"
                f"  标题：{it.get('title', '')}\n"
                f"  预测：{str(it.get('testable_prediction', ''))[:FIELD_LIMIT]}\n"
                f"  实验：{str(it.get('minimal_experiment', ''))[:FIELD_LIMIT]}\n"
                f"  本地查重（最相似文献）：{sim}")
        prompt = (
            f"研究主题：{self.topic.get('title') or self.topic.get('name')}\n"
            f"算力预算（硬约束）：{json.dumps(self.topic.get('budget') or {}, ensure_ascii=False)}\n\n"
            "候选假设：\n" + "\n".join(hypo_lines) + "\n\n"
            "请以严苛同行评审身份质询每条假设，重点：与已有工作的重叠（新颖性）、"
            "预测可验证性、实验在预算内的可行性。输出 JSON 数组，每个元素：\n"
            '{"id": "H1", "novelty_concern": "与已有工作的重叠担忧（结合本地查重线索）", '
            '"feasibility_score": 0.0, "major_weakness": "最主要弱点", '
            '"suggested_fix": "具体修改建议"}\n'
            "只输出 JSON 数组，不要其他文字。"
        )
        cli = self.llm.bind("ideation", "peer_reviewer")
        resp = cli.chat(prompt, system=PEER_SYSTEM, max_tokens=4096)
        return extract_json_array(LLMClient.text_of(resp))

    @staticmethod
    def _placeholder_reviews(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [{
            "id": str(it.get("id", "")),
            "novelty_concern": "（离线占位：未接 LLM，未做新颖性质询）",
            "feasibility_score": 0.5,
            "major_weakness": "占位",
            "suggested_fix": "占位",
        } for it in items]
