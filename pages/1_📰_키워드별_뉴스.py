"""데이터센터 · 클라우드 · CSP · 아이폰 폴드 · 아이폰 울트라 키워드 뉴스.

DART 재무제표 대시보드(app.py)와는 무관한 별도 페이지. 매일 아침 8시 스케줄 작업(fetch_keyword_news.py)이
Google 뉴스 · Bing 뉴스에서 키워드별 최신 기사를 모아 keyword_news 테이블에 저장하면, 여기서 보여준다.
두 소스 모두에 잡힌 같은 기사는 요약이 더 풍부한 쪽만 남기고 화면에서 걸러낸다.
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

import db

NEWS_KEYWORDS = ["데이터센터", "클라우드", "CSP", "아이폰 폴드", "아이폰 울트라"]

st.set_page_config(page_title="키워드별 뉴스", page_icon="📰", layout="wide")
db.init_db()

st.title("📰 키워드별 뉴스")
st.caption(
    "DART 재무 데이터와는 무관한 별도 기능입니다. 매일 아침 8시, "
    f"{' · '.join(NEWS_KEYWORDS)} 키워드가 들어간 최신 뉴스를 Google · Bing 뉴스에서 자동으로 모아옵니다."
)

news_keyword = st.radio("키워드", ["전체"] + NEWS_KEYWORDS, horizontal=True, key="news_keyword")
news_df = db.get_news(None if news_keyword == "전체" else news_keyword)

if news_df.empty:
    st.info("아직 수집된 뉴스가 없습니다. 매일 아침 8시 자동 수집을 기다려주세요.")
else:
    news_df["_dt"] = pd.to_datetime(news_df["pub_date"], errors="coerce", utc=True).dt.tz_convert("Asia/Seoul")
    news_df["표시일시"] = news_df["_dt"].dt.strftime("%Y-%m-%d %H:%M")

    # 같은 기사가 Google/Bing 양쪽에 다 잡히면 링크는 달라도 제목 앞부분이 같다.
    # 요약이 더 긴(더 풍부한) 쪽만 남기고 화면에서 중복을 걸러낸다.
    news_df["_dedup_key"] = news_df["keyword"] + news_df["title"].str[:20]
    news_df["_desc_len"] = news_df["description"].str.len()
    news_df = news_df.sort_values("_desc_len", ascending=False).drop_duplicates(subset="_dedup_key", keep="first")

    news_df = news_df.sort_values("_dt", ascending=False)
    for _, row in news_df.iterrows():
        st.markdown(f"**[{row['title']}]({row['link']})**")
        meta = f"`{row['keyword']}`"
        if row["표시일시"] and row["표시일시"] != "NaT":
            meta += f" · {row['표시일시']}"
        st.caption(meta)
        if row["description"]:
            st.write(row["description"])
        st.divider()
