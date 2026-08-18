"""키워드별 뉴스(데이터센터/클라우드/CSP/아이폰 폴드/아이폰 울트라)를 Google 뉴스 RSS에서 가져와
DB에 저장하고, 변경사항이 있으면 git commit/push까지 한다.

API 키가 필요 없는 keyless RSS 엔드포인트를 쓴다 (네이버 검색 API는 신규 발급을 받지 않아 대체).
매일 스케줄 작업이 이 스크립트 하나만 실행하면 수집 -> 저장 -> 배포 반영까지 한 번에 끝난다.
"""
from __future__ import annotations

import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))
import db  # noqa: E402

KEYWORDS = ["데이터센터", "클라우드", "CSP", "아이폰 폴드", "아이폰 울트라"]


def fetch_google_news(keyword: str) -> list[dict]:
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


def main() -> None:
    db.init_db()
    summary = []
    for kw in KEYWORDS:
        items = fetch_google_news(kw)
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
