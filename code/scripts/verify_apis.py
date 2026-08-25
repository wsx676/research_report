# -*- coding: utf-8 -*-
"""D1 遗留验收：文献检索 API 连通性真实验证（arXiv + Semantic Scholar）。

D2 SurveyAgent 的前置排雷脚本：验证两个检索源可用并打印样例结果。
用法：python code/scripts/verify_apis.py
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

QUERY = "context engineering LLM agents"


def check_arxiv() -> bool:
    print("=== arXiv API (export.arxiv.org) ===")
    r = httpx.get(
        "https://export.arxiv.org/api/query",
        params={
            "search_query": f"all:{QUERY}",
            "max_results": 3,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        },
        timeout=30,
        follow_redirects=True,
    )
    print(f"HTTP {r.status_code}")
    titles = re.findall(r"<title>(.*?)</title>", r.text, re.S)[1:]
    for t in titles:
        print(" -", " ".join(t.split())[:90])
    ok = r.status_code == 200 and titles
    print("result:", "OK" if ok else "FAIL")
    return ok


def check_semantic_scholar() -> bool:
    print("\n=== Semantic Scholar API (graph/v1) ===")
    import time

    ok = False
    for attempt in range(3):  # 公共池限流（429）：退避重试
        if attempt:
            wait = 10 * attempt
            print(f"retry #{attempt} after {wait}s ...")
            time.sleep(wait)
        r = httpx.get(
            "https://api.semanticscholar.org/graph/v1/paper/search",
            params={"query": QUERY, "fields": "title,year,citationCount",
                    "limit": 3},
            timeout=30,
        )
        print(f"HTTP {r.status_code}")
        if r.status_code == 200:
            for p in r.json().get("data", []):
                print(f" - {p['title'][:80]} ({p.get('year')}, "
                      f"cites={p.get('citationCount')})")
            ok = bool(r.json().get("data"))
            break
        if r.status_code != 429:
            print(" body:", r.text[:200])
            break
    print("result:", "OK" if ok else "FAIL")
    return ok


if __name__ == "__main__":
    a = check_arxiv()
    s = check_semantic_scholar()
    print("\n=== summary ===")
    print(f"arXiv: {'PASS' if a else 'FAIL'} | Semantic Scholar: "
          f"{'PASS' if s else 'FAIL'}")
    sys.exit(0 if (a or s) else 1)  # 至少一源可用即不阻塞 D2
