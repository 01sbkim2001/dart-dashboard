"""키워드별 뉴스(데이터센터/클라우드/CSP/아이폰 폴드/아이폰 울트라)를 모아 DB에 저장하고,
변경사항이 있으면 git commit/push까지 한다.

API 키가 필요 없는 keyless 소스 두 개를 함께 쓴다:
- Google 뉴스 RSS: 매번 안정적으로 응답하지만, 링크가 JS로만 풀리는 리다이렉트라 실제 기사
  요약을 못 주고 title/source만 준다. 이게 항상 채워지는 기본 소스다.
- Bing 뉴스 RSS: <description>에 진짜 기사 요약 스니펫을 주고 링크도 바로 꺼내 쓸 수 있어
  내용이 훨씬 풍부하지만, 스크립트로 요청하면 이따금(키워드에 따라) 빈 결과를 준다(봇 차단 추정).
  그래서 "있으면 좋고 없어도 그만"인 보너스로만 쓴다 — 실패해도 전체 실행은 계속된다.

매일 스케줄 작업이 이 스크립트 하나만 실행하면 수집 -> 저장 -> 배포 반영까지 한 번에 끝난다.
"""
from __future__ import annotations

import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))
import db  # noqa: E402

KEYWORDS = ["데이터센터", "클라우드", "CSP", "아이폰 폴드", "아이폰 울트라"]


def fetch_google_news(keyword: str) -> list[dict]:
    """항상 안정적으로 응답하는 기본 소스. 요약 없이 title/source만 준다."""
    resp = requests.get(
        "https://news.google.com/rss/search",
        params={"q": keyword, "hl": "ko", "gl": "KR", "ceid": "KR:ko"},
        timeout=20,
    )
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    items = []
    for item in root.findall("./channel/item"):
        source = item.findtext("source") or ""
        items.append({
            "title": item.findtext("title") or "",
            "link": item.findtext("link") or "",
            "description": f"출처: {source}" if source else "",
            "pub_date": item.findtext("pubDate") or "",
        })
    return items


def _local_tag(tag: str) -> str:
    """네임스페이스 접두사를 뗀 태그 이름. Bing의 News 네임스페이스 URI는 쿼리마다 달라져서
    (요청한 검색어가 URI 안에 그대로 박혀 있음) 고정된 네임스페이스로 찾을 수가 없다."""
    return tag.split("}")[-1] if "}" in tag else tag


def _resolve_bing_link(link: str) -> str:
    """Bing 뉴스 RSS의 link는 클릭 추적용 리다이렉트 URL이다. 그 안의 `url=` 쿼리파라미터에
    실제 기사 주소가 그대로 들어있어 꺼내 쓴다."""
    try:
        real = parse_qs(urlparse(link).query).get("url", [None])[0]
        return real or link
    except Exception:
        return link


def fetch_bing_news(keyword: str) -> list[dict]:
    """진짜 기사 요약이 담긴 보너스 소스. 실패하거나 빈 결과여도 호출부에서 그냥 넘어간다."""
    resp = requests.get(
        "https://www.bing.com/news/search",
        params={"q": keyword, "format": "RSS"},
        timeout=20,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    resp.raise_for_status()
    root = ET.fromstring(resp.content)  # ET는 XML 선언의 encoding="utf-8"을 그대로 존중한다

    items = []
    for item in root.findall("./channel/item"):
        source = ""
        for child in item:
            if _local_tag(child.tag) == "Source":
                source = child.text or ""
                break
        snippet = item.findtext("description") or ""
        description = f"{snippet} — {source}" if source else snippet
        items.append({
            "title": item.findtext("title") or "",
            "link": _resolve_bing_link(item.findtext("link") or ""),
            "description": description,
            "pub_date": item.findtext("pubDate") or "",
        })
    return items


def main() -> None:
    db.init_db()
    summary = []
    for kw in KEYWORDS:
        items = fetch_google_news(kw)
        try:
            items += fetch_bing_news(kw)
        except Exception as e:
            print(f"[{kw}] Bing 보너스 소스 실패, Google 결과만 사용: {e}")
        n = db.save_news_items(kw, items)
        summary.append(f"{kw} {n}건")
    print("신규 저장: " + ", ".join(summary))

    status = subprocess.run(
        ["git", "status", "--porcelain", "data/dart.db"],
        cwd=PROJECT_ROOT, capture_output=True, text=True,
    )
    if status.stdout.strip():
        subprocess.run(["git", "add", "data/dart.db"], cwd=PROJECT_ROOT, check=True)
        subprocess.run(
            ["git", "commit", "-m", f"Add keyword news: {date.today().isoformat()}"],
            cwd=PROJECT_ROOT, check=True,
        )
        subprocess.run(["git", "push"], cwd=PROJECT_ROOT, check=True)
        print("git push 완료")
    else:
        print("data/dart.db 변경사항 없음, 커밋 생략")


if __name__ == "__main__":
    main()
