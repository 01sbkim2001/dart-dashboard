"""DART 재무제표 raw data 대시보드 (Streamlit).

삼성전자 / SK하이닉스(또는 직접 검색한 회사)의 재무제표를 DART Open API에서 가져와
로컬 SQLite에 원본 그대로 누적 저장하고, 실제 재무제표 표/차트로 확인한다.
"""
from __future__ import annotations

import os

import pandas as pd
import plotly.express as px
import streamlit as st
from dotenv import load_dotenv

import db
from dart_client import DartApiError, DartClient, FS_DIVISIONS, REPORT_CODES, WATCHLIST
from statement_utils import (
    CUMULATIVE_SJ_NM,
    UNIT_FACTORS,
    add_derived_ratios,
    amount_to_number,
    build_comparison_table,
    build_key_metrics,
    build_quarterly_key_metrics,
    build_quarterly_pivot,
    build_statement_pivot,
    expand_with_audit_history,
    format_for_display,
    quarter_value_for_account,
    scale_for_chart,
)


FLOW_SJ_NM = {"손익계산서", "포괄손익계산서", "현금흐름표"}

# "회사 간 비교" 차트에서 회사별로 고정할 색상 (지정 안 된 회사는 Plotly 기본 팔레트가 자동 배정)
COMPANY_COLORS = {
    "SK하이닉스": "#FF6A13",  # 빨간기 살짝 있는 주황 (이전보다 주황쪽으로 조정)
    "삼성전자": "#0047AB",   # 코발트블루
}


def format_listing_date(raw: str | None) -> str | None:
    if not raw or len(raw) != 8:
        return None
    return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"


# 같은 연도 안에서 1분기 -> 반기 -> 3분기 -> 4분기(계산) -> 연간(사업보고서) 순으로 정렬하기 위한 순위표
REPRT_TYPE_ORDER = {
    "1분기보고서": 0,
    "1분기 잠정실적": 0,
    "반기보고서": 1,
    "2분기 잠정실적": 1,
    "3분기보고서": 2,
    "3분기 잠정실적": 2,
    "4분기(계산)": 3,
    "4분기 잠정실적": 3,
    "사업보고서": 4,
    "감사보고서": 4,
}


def reprt_sort_key(reprt_name: str) -> int:
    return REPRT_TYPE_ORDER.get(reprt_name, 99)


def chronological_order(values: list[str]) -> list[str]:
    """'YYYY 보고서명' 형태 라벨들을 연도 오름차순 -> 같은 연도 안에서는
    1분기->반기->3분기->4분기(계산)->사업보고서 순으로 정렬한다."""
    def key(v: str):
        year, _, reprt = v.partition(" ")
        return (year, reprt_sort_key(reprt))
    return sorted(values, key=key)


def add_listing_vline(fig, categories: list[str], listing_date_raw: str | None) -> None:
    """차트의 카테고리형 x축 위에, 상장일 이전/이후 경계에 점선을 그어준다.
    categories는 차트에 그려진 순서(연도 오름차순)의 '연도 보고서' 라벨 목록."""
    if not listing_date_raw:
        return
    listing_year = listing_date_raw[:4]
    boundary_idx = None
    for i, cat in enumerate(categories):
        if cat.split(" ")[0] >= listing_year:
            boundary_idx = i
            break
    if boundary_idx is None or boundary_idx == 0:
        return
    fig.add_vline(
        x=boundary_idx - 0.5, line_dash="dash", line_color="gray",
        annotation_text="상장", annotation_position="top",
    )

load_dotenv()
db.init_db()

st.set_page_config(page_title="DART 재무제표 대시보드", layout="wide")
st.title("📊 DART 재무제표 대시보드")


def get_secret(name: str) -> str:
    """Streamlit Cloud의 st.secrets를 우선 쓰고, 로컬 .env(os.environ)로 대체한다."""
    try:
        return str(st.secrets[name])
    except Exception:
        return os.getenv(name, "")


ADMIN_PASSWORD = get_secret("ADMIN_PASSWORD").strip()


def is_admin() -> bool:
    # ADMIN_PASSWORD가 설정 안 된 로컬 개발 환경에서는 전체 허용
    if not ADMIN_PASSWORD:
        return True
    return bool(st.session_state.get("is_admin", False))


@st.cache_resource
def get_client() -> DartClient | None:
    api_key = get_secret("DART_API_KEY").strip()
    if not api_key:
        return None
    return DartClient(api_key)


client = get_client()


def period_picker(periods: list[str], key_prefix: str) -> list[str]:
    """기간(연도/보고서 등) 체크박스 선택기를 그려주고, 체크된 기간만 리스트로 반환한다."""
    col_all, col_none = st.columns(2)
    with col_all:
        if st.button("전체 선택", key=f"{key_prefix}_selall", use_container_width=True):
            for p in periods:
                st.session_state[f"{key_prefix}_{p}"] = True
    with col_none:
        if st.button("전체 해제", key=f"{key_prefix}_deselall", use_container_width=True):
            for p in periods:
                st.session_state[f"{key_prefix}_{p}"] = False

    selected = []
    for p in periods:
        if st.checkbox(p, value=True, key=f"{key_prefix}_{p}"):
            selected.append(p)
    return selected

# ---------------- 사이드바: 조회 및 저장 (관리자 전용) ----------------
with st.sidebar:
    if ADMIN_PASSWORD and not is_admin():
        st.subheader("🔒 관리자 로그인")
        pw = st.text_input("비밀번호", type="password", key="admin_pw_input")
        if st.button("로그인"):
            if pw == ADMIN_PASSWORD:
                st.session_state["is_admin"] = True
                st.rerun()
            else:
                st.error("비밀번호가 틀렸습니다.")
        st.divider()
        st.caption("이 대시보드는 읽기 전용으로 공개되어 있습니다. 데이터 수집/삭제는 관리자만 가능합니다.")
    else:
        if ADMIN_PASSWORD:
            st.success("🔓 관리자 모드")
        st.header("데이터 가져오기")

        if client is None:
            st.error("DART_API_KEY가 설정되지 않았습니다. secrets 또는 .env 파일에 키를 넣어주세요.")

        corp_choice = st.selectbox("회사", WATCHLIST + ["직접 검색"])
        if corp_choice == "직접 검색":
            corp_name = st.text_input("회사명 (DART 등록명과 정확히 일치해야 함)", "")
        else:
            corp_name = corp_choice

        years = st.multiselect(
            "사업연도",
            [str(y) for y in range(2015, 2028)],
            default=["2026", "2025", "2024"],
        )
        reprt_code = st.selectbox(
            "보고서 종류", list(REPORT_CODES.keys()), format_func=lambda k: REPORT_CODES[k], index=3
        )
        fs_div_input = st.selectbox("재무제표 구분", list(FS_DIVISIONS.keys()), format_func=lambda k: FS_DIVISIONS[k])

        fetch_clicked = st.button("DART에서 가져와 저장", type="primary", disabled=(client is None))

        if fetch_clicked:
            if not corp_name:
                st.warning("회사명을 입력해주세요.")
            elif not years:
                st.warning("사업연도를 하나 이상 선택해주세요.")
            else:
                try:
                    corp = client.find_corp(corp_name)
                    progress = st.progress(0.0)
                    total_rows = 0
                    for i, y in enumerate(years):
                        data = client.get_financial_statement(corp.corp_code, y, reprt_code, fs_div_input)
                        db.save_fetch(
                            corp.corp_code,
                            corp_name,
                            corp.stock_code,
                            y,
                            reprt_code,
                            REPORT_CODES[reprt_code],
                            fs_div_input,
                            data,
                        )
                        total_rows += len(data.get("list", []))
                        progress.progress((i + 1) / len(years))
                    st.success(f"{corp_name} {', '.join(years)}년 데이터 저장 완료 (총 {total_rows}개 계정과목)")
                    st.cache_data.clear()
                except DartApiError as e:
                    st.error(f"DART API 오류: {e}")
                except Exception as e:
                    st.error(f"오류 발생: {e}")

        st.divider()
        st.caption("보유 중인 API 키가 없다면 opendart.fss.or.kr 에서 무료로 발급받을 수 있습니다.")


# ---------------- 메인 ----------------
tab_statement, tab_trend, tab_compare, tab_manage = st.tabs(
    ["📑 재무제표", "📈 추이 차트", "🆚 회사 간 비교", "🗂 데이터 관리"]
)

with tab_statement:
    col_a, col_b, col_d = st.columns([2, 1.5, 1])
    with col_a:
        stmt_corp = st.selectbox("회사", WATCHLIST, key="stmt_corp")

    items = db.get_all_line_items(stmt_corp)

    if items.empty:
        st.info("이 회사의 저장된 데이터가 없습니다. 왼쪽 사이드바에서 먼저 데이터를 가져와주세요.")
    else:
        items = expand_with_audit_history(items)

        listing_date_raw = db.get_company_meta(stmt_corp)
        listing_date_display = format_listing_date(listing_date_raw)
        if listing_date_display:
            st.caption(f"📌 상장일: {listing_date_display} (DART 증권발행실적보고서 기준) — 이전 연도는 상장 전 감사보고서의 비교재무제표에서 가져왔습니다.")

        available_fs = [f for f in FS_DIVISIONS if f in items["fs_div"].unique()]
        with col_b:
            stmt_fs_div = st.selectbox(
                "재무제표 구분", available_fs, format_func=lambda k: FS_DIVISIONS[k], key="stmt_fs_div"
            )
        with col_d:
            unit = st.selectbox("단위", list(UNIT_FACTORS.keys()), key="stmt_unit")

        # ---- 핵심 지표 (매출액 / 영업이익 / 당기순이익) 요약 ----
        key_metrics = build_key_metrics(items, stmt_fs_div)
        if not key_metrics.empty:
            st.subheader(f"💰 핵심 지표 (단위: {unit})")

            col_chart, col_pick = st.columns([4, 1])
            with col_pick:
                st.caption("비교할 기간 선택")
                km_periods = period_picker(key_metrics.columns.tolist(), key_prefix="km")

            with col_chart:
                if not km_periods:
                    st.info("왼쪽에서 비교할 기간을 하나 이상 선택해주세요.")
                else:
                    km_selected = key_metrics[km_periods]
                    st.dataframe(format_for_display(km_selected, unit), use_container_width=True)

                    chart_df = km_selected.reset_index().melt(id_vars="index", var_name="기간", value_name="금액")
                    chart_df = chart_df.rename(columns={"index": "지표"})
                    period_order = chronological_order(km_selected.columns.tolist())
                    chart_df["기간"] = pd.Categorical(chart_df["기간"], categories=period_order, ordered=True)
                    chart_df = chart_df.sort_values("기간")
                    chart_df, chart_unit = scale_for_chart(chart_df, "금액")
                    fig = px.bar(
                        chart_df, x="기간", y="금액", color="지표", barmode="group",
                        title=f"{stmt_corp} 매출액 · 영업이익 · 당기순이익 추이 (단위: {chart_unit})",
                    )
                    fig.update_yaxes(title=f"금액 ({chart_unit})")
                    add_listing_vline(fig, period_order, listing_date_raw)
                    st.plotly_chart(fig, use_container_width=True)
            st.divider()

        # ---- 분기별 단독 실적 (1~4분기, 연간에서 1~3분기를 뺀 4분기 포함) ----
        quarterly_key_metrics = build_quarterly_key_metrics(items, stmt_fs_div)
        if not quarterly_key_metrics.empty:
            st.subheader(f"📅 분기별 단독 실적 (단위: {unit})")
            st.caption(
                "1·2·3분기는 각 분기보고서 값 그대로, 4분기는 사업보고서(연간) 값에서 1·2·3분기 실적을 뺀 값입니다 "
                "(4분기만 별도로 공시되지 않기 때문)."
            )

            col_qchart, col_qpick = st.columns([4, 1])
            with col_qpick:
                st.caption("비교할 분기 선택")
                qkm_periods = period_picker(quarterly_key_metrics.columns.tolist(), key_prefix="qkm")

            with col_qchart:
                if not qkm_periods:
                    st.info("왼쪽에서 비교할 분기를 하나 이상 선택해주세요.")
                else:
                    qkm_selected = quarterly_key_metrics[qkm_periods]
                    st.dataframe(format_for_display(qkm_selected, unit), use_container_width=True)

                    q_chart_df = qkm_selected.reset_index().melt(id_vars="index", var_name="분기", value_name="금액")
                    q_chart_df = q_chart_df.rename(columns={"index": "지표"})
                    q_period_order = chronological_order(qkm_selected.columns.tolist())
                    q_chart_df["분기"] = pd.Categorical(q_chart_df["분기"], categories=q_period_order, ordered=True)
                    q_chart_df = q_chart_df.sort_values("분기")
                    q_chart_df, q_chart_unit = scale_for_chart(q_chart_df, "금액")
                    q_fig = px.bar(
                        q_chart_df, x="분기", y="금액", color="지표", barmode="group",
                        title=f"{stmt_corp} 분기별 매출액 · 영업이익 · 당기순이익 (단위: {q_chart_unit})",
                    )
                    q_fig.update_yaxes(title=f"금액 ({q_chart_unit})")
                    st.plotly_chart(q_fig, use_container_width=True)
            st.divider()

        # ---- 재무제표 종류별 전체 표 (재무상태표 / 손익계산서 / 현금흐름표 등 전부) ----
        available_sj = items.loc[items["fs_div"] == stmt_fs_div, "sj_nm"].dropna().unique().tolist()
        preferred_order = ["재무상태표", "손익계산서", "포괄손익계산서", "현금흐름표", "자본변동표"]
        available_sj = sorted(available_sj, key=lambda s: preferred_order.index(s) if s in preferred_order else 99)

        st.subheader(f"📄 {stmt_corp} · {FS_DIVISIONS[stmt_fs_div]} 전체 재무제표 (단위: {unit})")
        for sj_nm in available_sj:
            pivot = build_statement_pivot(items, stmt_fs_div, sj_nm)
            if pivot.empty:
                continue
            with st.expander(f"{sj_nm} ({len(pivot)}개 계정과목)", expanded=True):
                st.dataframe(format_for_display(pivot, unit), use_container_width=True)
                st.download_button(
                    "CSV로 내보내기 (원 단위)",
                    pivot.to_csv().encode("utf-8-sig"),
                    file_name=f"{stmt_corp}_{sj_nm}.csv",
                    mime="text/csv",
                    key=f"dl_{sj_nm}",
                )

                if sj_nm in FLOW_SJ_NM:
                    q_pivot = build_quarterly_pivot(items, stmt_fs_div, sj_nm)
                    if not q_pivot.empty:
                        cum_note = " (누적 보고서에서 직전 분기를 뺀 값)" if sj_nm in CUMULATIVE_SJ_NM else ""
                        st.markdown(f"**분기별 단독 실적{cum_note}**")
                        st.dataframe(format_for_display(q_pivot, unit), use_container_width=True)
                        st.download_button(
                            "분기별 표 CSV로 내보내기 (원 단위)",
                            q_pivot.to_csv().encode("utf-8-sig"),
                            file_name=f"{stmt_corp}_{sj_nm}_분기별.csv",
                            mime="text/csv",
                            key=f"dl_q_{sj_nm}",
                        )

with tab_trend:
    trend_corp = st.selectbox("회사", WATCHLIST, key="trend_corp")
    all_items = db.get_all_line_items(trend_corp)

    if all_items.empty:
        st.info("이 회사의 저장된 데이터가 없습니다.")
    else:
        all_items = expand_with_audit_history(all_items)

        trend_listing_raw = db.get_company_meta(trend_corp)
        trend_listing_display = format_listing_date(trend_listing_raw)
        if trend_listing_display:
            st.caption(f"📌 상장일: {trend_listing_display} (DART 증권발행실적보고서 기준) — 이전 연도는 상장 전 감사보고서의 비교재무제표에서 가져왔습니다.")

        account_options = sorted(all_items["account_nm"].dropna().unique().tolist())
        default_idx = account_options.index("매출액") if "매출액" in account_options else 0
        account = st.selectbox("계정과목", account_options, index=default_idx)

        def _reprt_sort_col(col: pd.Series) -> pd.Series:
            if col.name == "reprt_name":
                return col.map(reprt_sort_key)
            return col

        subset = all_items[all_items["account_nm"] == account].copy()
        subset["금액"] = subset["thstrm_amount"].apply(amount_to_number)
        subset = subset.sort_values(["bsns_year", "reprt_name"], key=_reprt_sort_col)
        subset["연도/보고서"] = subset["bsns_year"] + " " + subset["reprt_name"]

        # ---- 4분기(계산값): 사업보고서(연간)에서 1·2·3분기를 뺀 값을 별도 옵션으로 추가 ----
        q4_rows = []
        for (fs_val, sj_val), grp in subset.groupby(["fs_div", "sj_nm"]):
            if sj_val not in FLOW_SJ_NM:
                continue
            amounts = {(r["bsns_year"], r["reprt_name"]): r["금액"] for _, r in grp.iterrows()}
            years_here = sorted(grp["bsns_year"].unique())
            q_result = quarter_value_for_account(amounts, years_here, cumulative=sj_val in CUMULATIVE_SJ_NM)
            for period_label, val in q_result.items():
                if not period_label.endswith("4분기") or pd.isna(val):
                    continue
                year = period_label.split(" ")[0]
                q4_rows.append({
                    "fs_div": fs_val, "sj_nm": sj_val, "account_nm": account,
                    "bsns_year": year, "reprt_name": "4분기(계산)", "금액": val,
                    "연도/보고서": f"{year} 4분기(계산)",
                })
        if q4_rows:
            subset = pd.concat([subset, pd.DataFrame(q4_rows)], ignore_index=True)
            subset = subset.sort_values(["bsns_year", "reprt_name"], key=_reprt_sort_col)

        trend_periods_all = subset["연도/보고서"].drop_duplicates().tolist()

        col_tchart, col_tpick = st.columns([4, 1])
        with col_tpick:
            st.caption("비교할 기간 선택")
            trend_periods = period_picker(trend_periods_all, key_prefix=f"trend_{trend_corp}")

        with col_tchart:
            if not trend_periods:
                st.info("오른쪽에서 비교할 기간을 하나 이상 선택해주세요.")
            else:
                filtered_subset = subset[subset["연도/보고서"].isin(trend_periods)]
                filtered_periods_order = filtered_subset["연도/보고서"].drop_duplicates().tolist()
                chart_subset, trend_unit = scale_for_chart(filtered_subset, "금액")
                fig = px.bar(
                    chart_subset, x="연도/보고서", y="금액", color="sj_nm",
                    title=f"{trend_corp} - {account} (단위: {trend_unit})",
                )
                fig.update_yaxes(title=f"금액 ({trend_unit})")
                add_listing_vline(fig, filtered_periods_order, trend_listing_raw)
                st.plotly_chart(fig, use_container_width=True)
                st.dataframe(
                    filtered_subset[["bsns_year", "reprt_name", "sj_nm", "account_nm", "금액"]],
                    use_container_width=True,
                    hide_index=True,
                )

with tab_compare:
    st.caption("여러 회사의 연간(사업보고서/감사보고서) 실적을 한 화면에서 비교합니다. 분기 데이터는 회사마다 보유 현황이 달라 제외했습니다.")

    col_cc, col_cfs = st.columns([3, 1])
    with col_cc:
        compare_corps = st.multiselect("비교할 회사", WATCHLIST, default=WATCHLIST, key="compare_corps")
    with col_cfs:
        compare_fs_div = st.selectbox(
            "재무제표 구분", list(FS_DIVISIONS.keys()), format_func=lambda k: FS_DIVISIONS[k], key="compare_fs_div"
        )

    if not compare_corps:
        st.info("비교할 회사를 하나 이상 선택해주세요.")
    else:
        items_by_corp = {
            c: expand_with_audit_history(db.get_all_line_items(c)) for c in compare_corps
        }
        items_by_corp = {c: v for c, v in items_by_corp.items() if not v.empty}
        cmp_df = build_comparison_table(items_by_corp, compare_fs_div)

        if cmp_df.empty:
            st.info(f"선택한 회사들의 {FS_DIVISIONS[compare_fs_div]} 연간 데이터가 없습니다.")
        else:
            cmp_df = add_derived_ratios(cmp_df)

            col_metric, col_view = st.columns(2)
            with col_metric:
                cmp_metric = st.radio("지표", ["매출액", "영업이익", "당기순이익"], horizontal=True, key="cmp_metric")
            with col_view:
                cmp_view = st.radio(
                    "보기 방식", ["금액", "영업이익률(%)", "전년대비 성장률(%)"], horizontal=True, key="cmp_view"
                )

            if cmp_view == "금액":
                plot_df = cmp_df[["회사", "연도", cmp_metric]].dropna().rename(columns={cmp_metric: "값"})
                plot_df, cmp_unit = scale_for_chart(plot_df, "값")
                y_title = f"{cmp_metric} ({cmp_unit})"
                chart_title = f"회사별 {cmp_metric} 비교 (단위: {cmp_unit})"
            elif cmp_view == "영업이익률(%)":
                plot_df = cmp_df[["회사", "연도", "영업이익률(%)"]].dropna().rename(columns={"영업이익률(%)": "값"})
                y_title = "영업이익률 (%)"
                chart_title = "회사별 영업이익률 비교"
            else:
                col_name = f"{cmp_metric} 성장률(%)"
                plot_df = cmp_df[["회사", "연도", col_name]].dropna().rename(columns={col_name: "값"})
                y_title = f"{cmp_metric} 전년대비 성장률 (%)"
                chart_title = f"회사별 {cmp_metric} 성장률 비교"

            if plot_df.empty:
                st.info("표시할 데이터가 없습니다 (성장률은 최소 2개 연도가 있어야 계산됩니다).")
            else:
                cmp_fig = px.bar(
                    plot_df, x="연도", y="값", color="회사", barmode="group", title=chart_title,
                    color_discrete_map=COMPANY_COLORS,
                )
                cmp_fig.update_yaxes(title=y_title)
                st.plotly_chart(cmp_fig, use_container_width=True)

            st.dataframe(
                cmp_df[["회사", "연도", "매출액", "영업이익", "당기순이익", "영업이익률(%)"]]
                .style.format({
                    "매출액": "{:,.0f}", "영업이익": "{:,.0f}", "당기순이익": "{:,.0f}", "영업이익률(%)": "{:.1f}",
                }),
                use_container_width=True,
                hide_index=True,
            )

with tab_manage:
    st.caption("가져온 원본 데이터(raw data) 자체를 조회/삭제/내보내기 하는 관리 화면입니다.")
    filter_corp = st.selectbox("회사 필터", ["전체"] + WATCHLIST, key="browse_filter")
    fetches = db.list_fetches(None if filter_corp == "전체" else filter_corp)

    if fetches.empty:
        st.info("아직 저장된 데이터가 없습니다.")
    else:
        st.dataframe(fetches, use_container_width=True, hide_index=True)

        selected_id = st.selectbox(
            "상세히 볼 fetch 선택",
            fetches["id"],
            format_func=lambda i: f"#{i} - "
            + fetches.loc[fetches['id'] == i, 'corp_name'].values[0]
            + " "
            + fetches.loc[fetches['id'] == i, 'bsns_year'].values[0]
            + " "
            + fetches.loc[fetches['id'] == i, 'reprt_name'].values[0],
        )

        items_view = db.get_line_items(int(selected_id))
        col1, col2 = st.columns(2)
        with col1:
            st.download_button(
                "이 fetch 전체 라인아이템 CSV로 내보내기",
                items_view.to_csv(index=False).encode("utf-8-sig"),
                file_name=f"fetch_{int(selected_id)}.csv",
                mime="text/csv",
            )
        with col2:
            if is_admin():
                if st.button("이 기록 삭제", key="delete_btn"):
                    db.delete_fetch(int(selected_id))
                    st.success("삭제했습니다. 새로고침해주세요.")
                    st.cache_data.clear()
            else:
                st.caption("삭제는 관리자만 가능합니다.")

        st.dataframe(items_view, use_container_width=True, hide_index=True)

        with st.expander("원본 JSON 보기"):
            conn = db.get_connection()
            raw_json = pd.read_sql_query(
                "SELECT raw_json FROM fetch_log WHERE id = ?", conn, params=(int(selected_id),)
            )
            conn.close()
            if not raw_json.empty:
                st.json(raw_json.iloc[0]["raw_json"])
