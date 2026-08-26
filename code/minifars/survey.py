# -*- coding: utf-8 -*-
"""SurveyAgent（设计文档 §5.1）：文献检索与"研究空白清单"生成。

职责：
1. 按 topic.yaml 的 sub_directions 调 arXiv API（主源）与 Semantic Scholar
   API（辅源，限流时静默降级），产出近 N 个月文献摘要卡片；
2. 卡片落盘 proposals/survey/survey_cards.json（供 HypothesisAgent 与
   Writing 阶段引用，含被淘汰论文的审计痕迹）；
3. light 档 LLM 基于卡片生成 research_gaps.md（每方向空白点 + 跨方向观察）。

约束：
- 摘要卡片截断到 CARD_ABSTRACT_LIMIT 字符（上下文压缩红线，§6.2）；
- 检索本身不调 LLM，只有空白清单生成走 LLMClient（计量红线不绕过）。
"""
from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from .llm import LLMClient

ARXIV_API = "https://export.arxiv.org/api/query"
S2_API = "https://api.semanticscholar.org/graph/v1/paper/search"


class S2DegradedError(RuntimeError):
    """Semantic Scholar 辅源不可用（429 限流/网络失败重试耗尽）。

    与"成功但无命中"区分：合法空检索不应触发后续方向的跳过。"""


#: 每子方向从每个源最多取多少篇
PER_DIRECTION_LIMIT = 8
#: 卡片摘要截断（上下文压缩红线）
CARD_ABSTRACT_LIMIT = 600
#: S2 429 限流退避
S2_BACKOFF_SECONDS = (5, 10)
ATOM_NS = "{http://www.w3.org/2005/Atom}"
ARXIV_NS = "{http://arxiv.org/schemas/atom}"


@dataclass
class Paper:
    """一张文献摘要卡片。"""

    paper_id: str            # "arxiv:2401.12345" / "s2:<sha>"
    title: str
    abstract: str
    authors: List[str] = field(default_factory=list)
    published: str = ""      # ISO 日期
    url: str = ""
    source: str = ""         # arxiv | semantic_scholar
    sub_direction: str = ""
    citation_count: Optional[int] = None


# ------------------------------------------------------------------ parsers
def parse_arxiv_atom(xml_text: str, sub_direction: str = "") -> List[Paper]:
    """解析 arXiv Atom feed 为卡片列表（纯函数，便于单测）。"""
    root = ET.fromstring(xml_text)
    papers: List[Paper] = []
    for entry in root.findall(f"{ATOM_NS}entry"):
        raw_id = (entry.findtext(f"{ATOM_NS}id") or "").strip()
        arxiv_id = raw_id.rsplit("/abs/", 1)[-1] if "/abs/" in raw_id else raw_id
        authors = [a.findtext(f"{ATOM_NS}name") or ""
                   for a in entry.findall(f"{ATOM_NS}author")]
        abstract = " ".join((entry.findtext(f"{ATOM_NS}summary") or "").split())
        papers.append(Paper(
            paper_id=f"arxiv:{arxiv_id}",
            title=" ".join((entry.findtext(f"{ATOM_NS}title") or "").split()),
            abstract=abstract[:CARD_ABSTRACT_LIMIT],
            authors=[a for a in authors if a],
            published=(entry.findtext(f"{ATOM_NS}published") or "")[:10],
            url=raw_id,
            source="arxiv",
            sub_direction=sub_direction,
        ))
    return papers


def filter_recent(papers: List[Paper], months_back: int,
                  now: Optional[datetime] = None) -> List[Paper]:
    """按 published >= now - months_back 过滤；无日期的条目保留（S2 老条目）。"""
    if months_back <= 0:
        return papers
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=30 * months_back)
    kept: List[Paper] = []
    for p in papers:
        try:
            if datetime.fromisoformat(p.published).replace(tzinfo=timezone.utc) < cutoff:
                continue
        except ValueError:
            pass  # 日期缺失/非法：保留
        kept.append(p)
    return kept


def merge_papers(groups: List[List[Paper]]) -> List[Paper]:
    """多源去重合并：arXiv id 优先（S2 条目带 arXiv external id 时归并）。"""
    by_id: Dict[str, Paper] = {}
    for papers in groups:
        for p in papers:
            key = p.paper_id.split(":", 1)[1]
            if key in by_id:
                # S2 补充引用数到已有 arXiv 卡片
                if p.citation_count is not None and by_id[key].citation_count is None:
                    by_id[key].citation_count = p.citation_count
                continue
            by_id[key] = p
    return list(by_id.values())


# ------------------------------------------------------------------ fetchers
def search_arxiv(query: str, max_results: int = PER_DIRECTION_LIMIT,
                 timeout: float = 30.0) -> List[Paper]:
    r = httpx.get(ARXIV_API, params={
        "search_query": f"all:{query}",
        "max_results": max_results,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }, timeout=timeout, follow_redirects=True)
    r.raise_for_status()
    return parse_arxiv_atom(r.text, sub_direction=query)


def search_semantic_scholar(query: str, limit: int = PER_DIRECTION_LIMIT,
                            api_key: Optional[str] = None,
                            timeout: float = 30.0) -> List[Paper]:
    """S2 检索：429/网络异常重试耗尽时抛 S2DegradedError（与"合法空结果"
    区分），成功但无命中返回空列表。"""
    headers = {"x-api-key": api_key} if api_key else {}
    for attempt, backoff in enumerate(S2_BACKOFF_SECONDS + (0,)):
        try:
            r = httpx.get(S2_API, params={
                "query": query, "limit": limit,
                "fields": "title,abstract,year,publicationDate,authors,"
                          "citationCount,externalIds,url",
            }, headers=headers, timeout=timeout)
            if r.status_code == 429 and backoff:
                time.sleep(backoff)
                continue
            r.raise_for_status()
            break
        except httpx.HTTPError as exc:
            if backoff:
                time.sleep(backoff)
                continue
            raise S2DegradedError(f"S2 检索失败: {exc}") from exc
    else:
        raise S2DegradedError("S2 持续 429 限流，重试耗尽")
    papers: List[Paper] = []
    for item in r.json().get("data", []):
        ext = item.get("externalIds") or {}
        arxiv_id = ext.get("ArXiv")
        paper = Paper(
            paper_id=f"arxiv:{arxiv_id}" if arxiv_id else f"s2:{item.get('paperId', '')}",
            title=item.get("title") or "",
            abstract=(item.get("abstract") or "")[:CARD_ABSTRACT_LIMIT],
            authors=[a.get("name") or "" for a in item.get("authors", [])],
            published=item.get("publicationDate") or str(item.get("year") or ""),
            url=item.get("url") or "",
            source="semantic_scholar",
            sub_direction=query,
            citation_count=item.get("citationCount"),
        )
        if paper.title:
            papers.append(paper)
    return papers


# ------------------------------------------------------------------ agent
GAPS_SYSTEM = (
    "You are a survey analyst. Based ONLY on the provided paper cards, "
    "identify concrete research gaps. Be specific and falsifiable; never "
    "invent papers. Output concise Markdown in Chinese."
)


class SurveyAgent:
    """检索 → 卡片 → 研究空白清单（§5.1 SurveyAgent）。"""

    def __init__(self, topic: Dict[str, Any], llm_light: Optional[LLMClient],
                 out_dir: Path, metering: Any = None,
                 s2_api_key: Optional[str] = None):
        self.topic = topic
        self.llm = llm_light
        self.out_dir = Path(out_dir)
        self.metering = metering
        self.s2_api_key = s2_api_key

    def run(self) -> Dict[str, str]:
        directions = self.topic.get("sub_directions") or [self.topic.get("name", "")]
        filters = self.topic.get("search_filters") or {}
        months_back = int(filters.get("months_back", 0))
        min_citations = int(filters.get("min_citations", 0))

        cards: List[Paper] = []
        s2_degraded = False
        for direction in directions:
            t0 = time.perf_counter()
            group: List[Paper] = []
            try:
                group = search_arxiv(direction)
            except httpx.HTTPError as exc:
                print(f"[survey] arXiv '{direction}' 失败: {exc}")
            # 无 key 且已确认限流：跳过后续方向的 S2 重试（省退避等待）。
            # 降级信号来自 S2DegradedError，合法空结果不触发跳过。
            if s2_degraded and not self.s2_api_key:
                s2_hits: List[Paper] = []
            else:
                try:
                    s2_hits = search_semantic_scholar(direction,
                                                      api_key=self.s2_api_key)
                except S2DegradedError as exc:
                    s2_hits = []
                    s2_degraded = True
                    if not self.s2_api_key:
                        print(f"[survey] S2 降级（{exc}），后续方向跳过 S2")
            merged = merge_papers([group, s2_hits])
            merged = filter_recent(merged, months_back)
            if min_citations > 0:
                merged = [p for p in merged
                          if (p.citation_count or 0) >= min_citations]
            cards.extend(merged)
            self._record(direction, len(merged), t0)
        print(f"[survey] {len(cards)} 张卡片（S2{'限流降级' if s2_degraded else '正常'}）")

        cards_path = self.out_dir / "survey_cards.json"
        cards_path.parent.mkdir(parents=True, exist_ok=True)
        cards_path.write_text(
            json_dump({"schema": "v0", "topic": self.topic.get("name"),
                       "generated_at": now_iso(), "cards": [asdict(c) for c in cards]}),
            encoding="utf-8")

        gaps_path = self.out_dir / "research_gaps.md"
        gaps_path.write_text(self._generate_gaps(cards), encoding="utf-8")
        return {"survey_cards": str(cards_path), "research_gaps": str(gaps_path)}

    # ------------------------------------------------------------ internals
    def _generate_gaps(self, cards: List[Paper]) -> str:
        if not cards:
            return "# 研究空白清单\n\n（检索无结果，无法生成空白清单）\n"
        if self.llm is None:
            return self._gaps_placeholder(cards)
        lines = ["# 文献卡片（输入）", ""]
        for c in cards[:24]:  # 最多 24 张进 prompt（上下文压缩）
            cites = f", cites={c.citation_count}" if c.citation_count is not None else ""
            lines.append(f"- [{c.paper_id}] {c.title} ({c.published}{cites}) "
                         f"方向: {c.sub_direction}")
            lines.append(f"  摘要: {c.abstract[:200]}")
        prompt = (
            f"研究主题：{self.topic.get('title') or self.topic.get('name')}\n\n"
            + "\n".join(lines)
            + "\n\n请输出研究空白清单：每个子方向 2~3 个具体空白点"
            "（现状一句 + 缺口一句），最后给 1~2 条跨方向观察。"
        )
        cli = self.llm.bind("ideation", "survey_agent")
        resp = cli.chat(prompt, system=GAPS_SYSTEM, max_tokens=2048)
        text = LLMClient.text_of(resp).strip()
        header = (f"# 研究空白清单\n\n> 生成自 {len(cards)} 张卡片"
                  f"（SurveyAgent, light 档）；卡片全量见 survey_cards.json\n\n")
        return header + text + "\n"

    def _gaps_placeholder(self, cards: List[Paper]) -> str:
        by_dir: Dict[str, List[Paper]] = {}
        for c in cards:
            by_dir.setdefault(c.sub_direction, []).append(c)
        lines = ["# 研究空白清单（未接 LLM 的离线占位）", ""]
        for d, papers in by_dir.items():
            lines.append(f"## {d}（{len(papers)} 篇）")
            lines.extend(f"- {p.title} [{p.paper_id}]" for p in papers[:5])
            lines.append("")
        return "\n".join(lines)

    def _record(self, query: str, hits: int, t0: float) -> None:
        if self.metering is None:
            return
        self.metering.record(stage="ideation", agent="survey_agent",
                             model="arxiv+s2-api",
                             latency_ms=int((time.perf_counter() - t0) * 1000),
                             extra={"query": query, "hits": hits})


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def json_dump(obj: Any) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False, indent=2)
