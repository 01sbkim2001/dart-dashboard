"""재무제표 raw data를 표로 가공하는 순수 함수 모음 (Streamlit/DART API에 의존하지 않음).

이 모듈은 부작용(사이드 이펙트)이 전혀 없다 — 다른 스크립트에서 안전하게 import해서
테스트하거나 재사용할 수 있다. UI/사이드바 코드가 섞여 있는 app.py를 직접 import하면
Streamlit이 그 파일의 최상위 코드를 전부 실행해버려 실제 API 호출까지 트리거될 수 있으니
피할 것.
"""
from __future__ import annotations

import pandas as pd

UNIT_FACTORS = {"원": 1, "억원": 1e8, "조원": 1e12}

# XBRL account_id 기준 (한글 계정명이 연도마다 바뀌어도 안정적으로 매칭됨)
KEY_METRIC_IDS = {
    "매출액": "ifrs-full_Revenue",
    "영업이익": "dart_OperatingIncomeLoss",
    "당기순이익": "ifrs-full_ProfitLoss",
}

# account_id가 "-표준계정코드 미사용-"으로 찍히는 경우(특히 작은/신생 상장사)를 대비한 계정명 대체 후보
KEY_METRIC_FALLBACK_NAMES = {
    "매출액": ["매출액", "영업수익", "수익(매출액)"],
    "영업이익": ["영업이익", "영업이익(손실)", "영업손실", "영업이익(손실율)"],
    "당기순이익": ["당기순이익", "당기순이익(손실)", "분기순이익", "분기순손실", "당기순손실", "반기순이익", "반기순손실"],
}


def amount_to_number(s: str | None) -> float | None:
    if s is None or s == "":
        return None
    try:
        return float(str(s).replace(",", ""))
    except ValueError:
        return None


def expand_with_audit_history(items: pd.DataFrame) -> pd.DataFrame:
    """사업보고서에는 전기(frmtrm)·전전기(bfefrmtrm) 비교재무제표가 함께 실린다.
    이 비교 수치는 그 해의 감사보고서를 바탕으로 한 값이라, 상장 전이라 별도로 수집하지 못한
    과거 연도를 이걸로 보완한다 (이미 별도로 수집된 연도는 건드리지 않는다)."""
    if items.empty:
        return items
    annual = items[items["reprt_name"] == "사업보고서"].copy()
    if annual.empty:
        return items

    existing_years = set(items["bsns_year"].unique())

    extra_frames = []
    for offset, amt_col in [(1, "frmtrm_amount"), (2, "bfefrmtrm_amount")]:
        candidate = annual.copy()
        candidate["bsns_year"] = (candidate["bsns_year"].astype(int) - offset).astype(str)
        candidate = candidate[~candidate["bsns_year"].isin(existing_years)]
        candidate = candidate[candidate[amt_col].notna() & (candidate[amt_col] != "")]
        if candidate.empty:
            continue
        candidate["thstrm_amount"] = candidate[amt_col]
        candidate["reprt_name"] = "감사보고서"
        extra_frames.append(candidate)

    if not extra_frames:
        return items
    return pd.concat([items] + extra_frames, ignore_index=True)


def filter_to_reported_period(df: pd.DataFrame) -> pd.DataFrame:
    """DART가 사업/반기/분기 보고서 조회 시 다른 기간의 비교값을 같은 계정에 섞어 보내는 경우가 있어,
    실제로 요청한 보고서 기간(thstrm_nm)에 해당하는 행만 남긴다."""
    period_hint = {
        "반기보고서": "반기",
        "1분기보고서": "1분기",
        "3분기보고서": "3분기",
    }

    def keep(row) -> bool:
        t = row.get("thstrm_nm") or ""
        if row["reprt_name"] == "사업보고서":
            # "제 N 기" (분기/반기 접미사 없음)만 인정
            return ("분기" not in t) and ("반기" not in t)
        hint = period_hint.get(row["reprt_name"])
        if hint:
            return hint in t
        return True

    return df[df.apply(keep, axis=1)]


def build_statement_pivot(items: pd.DataFrame, fs_div: str, sj_nm: str) -> pd.DataFrame:
    """계정과목(행) x 연도/보고서(열) 형태의 실제 재무제표 표를 만든다.

    같은 항목(XBRL account_id)이라도 연도마다 한글 계정명이 달라질 수 있어(예: '매출액' vs '영업수익'),
    account_id를 기준으로 묶고 표시용 이름은 가장 최근 연도의 account_nm을 사용한다.
    """
    subset = items[(items["fs_div"] == fs_div) & (items["sj_nm"] == sj_nm)].copy()
    if subset.empty:
        return subset

    subset = filter_to_reported_period(subset)
    subset["금액"] = subset["thstrm_amount"].apply(amount_to_number)
    subset["ord_num"] = pd.to_numeric(subset["ord"], errors="coerce")
    subset["기간"] = subset["bsns_year"] + " " + subset["reprt_name"]
    # account_id가 없는 경우를 대비해 account_nm으로 대체
    subset["key"] = subset["account_id"].where(
        subset["account_id"].notna() & (subset["account_id"] != ""), subset["account_nm"]
    )

    # 표시용 이름: 가장 최근 연도에 쓰인 한글 계정명을 사용
    label_map = subset.sort_values("bsns_year").groupby("key")["account_nm"].last()
    # 같은 계정과목이 연도마다 순서(ord)가 다를 수 있어 가장 이른 순서를 기준으로 정렬
    account_order = subset.groupby("key")["ord_num"].min().sort_values()

    pivot = subset.pivot_table(index="key", columns="기간", values="금액", aggfunc="first")
    pivot = pivot.reindex(account_order.index)
    pivot.index = pivot.index.map(label_map)
    pivot.index.name = "account_nm"
    pivot = pivot[sorted(pivot.columns, reverse=True)]
    return pivot


def format_for_display(pivot: pd.DataFrame, unit: str) -> pd.DataFrame:
    factor = UNIT_FACTORS[unit]
    scaled = pivot / factor if factor != 1 else pivot
    decimals = 0 if unit == "원" else 1
    return scaled.map(lambda v: "" if pd.isna(v) else f"{v:,.{decimals}f}")


def pick_chart_unit(max_abs_value: float | None) -> str:
    """차트에 그릴 값의 규모에 맞는 단위를 고른다 (조원 -> 억원 -> 원)."""
    if max_abs_value is None or pd.isna(max_abs_value):
        return "원"
    if abs(max_abs_value) >= UNIT_FACTORS["조원"]:
        return "조원"
    if abs(max_abs_value) >= UNIT_FACTORS["억원"]:
        return "억원"
    return "원"


def scale_for_chart(df: pd.DataFrame, value_col: str) -> tuple[pd.DataFrame, str]:
    """value_col을 규모에 맞는 단위(원/억원/조원)로 스케일링한 사본과 단위 이름을 반환한다."""
    unit = pick_chart_unit(df[value_col].abs().max() if not df.empty else None)
    factor = UNIT_FACTORS[unit]
    out = df.copy()
    if factor != 1:
        out[value_col] = out[value_col] / factor
    return out, unit


def build_key_metrics(items: pd.DataFrame, fs_div: str) -> pd.DataFrame:
    """손익계산서/포괄손익계산서 어느 쪽에 있든 매출액·영업이익·당기순이익을 찾아 한 표로 모은다."""
    sub = items[(items["fs_div"] == fs_div) & (items["sj_nm"].isin(["손익계산서", "포괄손익계산서"]))].copy()
    if sub.empty:
        return pd.DataFrame()
    sub = filter_to_reported_period(sub)
    sub["금액"] = sub["thstrm_amount"].apply(amount_to_number)
    sub["기간"] = sub["bsns_year"] + " " + sub["reprt_name"]

    rows = {}
    for label, account_id in KEY_METRIC_IDS.items():
        fallback_names = KEY_METRIC_FALLBACK_NAMES.get(label, [])
        mask = (sub["account_id"] == account_id) | (sub["account_nm"].isin(fallback_names))
        match = sub[mask]
        if not match.empty:
            rows[label] = match.groupby("기간")["금액"].first()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).T
    return df[sorted(df.columns, reverse=True)]


def build_comparison_table(items_by_corp: dict[str, pd.DataFrame], fs_div: str) -> pd.DataFrame:
    """여러 회사의 연간(사업보고서/감사보고서) 핵심 지표를 하나의 long-format 표로 합친다.
    분기 데이터는 회사마다 보유 현황이 들쭉날쭉해서, 모든 회사가 공통으로 가진 연간 데이터만 비교한다."""
    rows = []
    for corp_name, items in items_by_corp.items():
        km = build_key_metrics(items, fs_div)
        if km.empty:
            continue
        for period in km.columns:
            year, _, reprt = period.partition(" ")
            if reprt not in ("사업보고서", "감사보고서"):
                continue
            row = {"회사": corp_name, "연도": year}
            for metric in km.index:
                row[metric] = km.loc[metric, period]
            rows.append(row)
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values(["회사", "연도"]).reset_index(drop=True)


def add_derived_ratios(df: pd.DataFrame) -> pd.DataFrame:
    """영업이익률과 전년대비 성장률(%)을 계산해 컬럼으로 추가한다. df는 [회사, 연도, 매출액, 영업이익, 당기순이익] 형태."""
    df = df.copy()
    if "매출액" in df and "영업이익" in df:
        df["영업이익률(%)"] = df["영업이익"] / df["매출액"] * 100
    for metric in ("매출액", "영업이익", "당기순이익"):
        if metric in df:
            df[f"{metric} 성장률(%)"] = df.groupby("회사")[metric].pct_change() * 100
    return df


# DART가 보고서 종류별로 주는 thstrm_amount의 성격이 통계 종류마다 다르다 (실측으로 확인됨):
#   - 손익계산서/포괄손익계산서: 1분기·반기·3분기보고서 값 자체가 이미 "그 분기만의" 단독 값이다
#     (예: 삼성전자 2025 매출액 1분기 79.1조 / 반기 74.6조 / 3분기 86.1조 — 누적이라면 반기가 1분기보다
#     작을 수 없다). 그래서 4분기만 사업보고서(연간)에서 1~3분기를 빼서 계산해야 한다.
#   - 현금흐름표: 반대로 진짜 연초 누적치다 (1분기 16.6조 -> 반기 33.9조 -> 3분기 56.5조로 계속 증가).
#     그래서 분기 단독값을 얻으려면 직전 누적 보고서를 빼야 한다.
CUMULATIVE_SJ_NM = {"현금흐름표"}


def _standalone_quarter_columns(cum: dict[str, float], years: list[str]) -> dict[str, float]:
    """손익계산서류: 보고서 값 자체가 분기 단독값. 4분기만 연간 - (1~3분기)로 역산한다."""
    row: dict[str, float] = {}
    for y in years:
        q1, q2, q3 = cum.get((y, "1분기보고서")), cum.get((y, "반기보고서")), cum.get((y, "3분기보고서"))
        annual = cum.get((y, "사업보고서"))
        if q1 is not None:
            row[f"{y} 1분기"] = q1
        if q2 is not None:
            row[f"{y} 2분기"] = q2
        if q3 is not None:
            row[f"{y} 3분기"] = q3
        if None not in (q1, q2, q3, annual):
            row[f"{y} 4분기"] = annual - q1 - q2 - q3
    return row


def _cumulative_quarter_columns(cum: dict[str, float], years: list[str]) -> dict[str, float]:
    """현금흐름표류: 보고서 값이 연초부터의 누적치. 직전 누적을 빼서 분기 단독값을 계산한다."""
    steps: list[tuple[str, str, str | None]] = [
        ("1분기보고서", "1분기", None),
        ("반기보고서", "2분기", "1분기보고서"),
        ("3분기보고서", "3분기", "반기보고서"),
        ("사업보고서", "4분기", "3분기보고서"),
    ]
    row: dict[str, float] = {}
    for y in years:
        for reprt_name, qlabel, prev_name in steps:
            cur = cum.get((y, reprt_name))
            if cur is None:
                continue
            if prev_name is None:
                row[f"{y} {qlabel}"] = cur
                continue
            prev = cum.get((y, prev_name))
            if prev is not None:
                row[f"{y} {qlabel}"] = cur - prev
    return row


def _sort_quarter_columns(columns) -> list[str]:
    # "2025 4분기" -> ("2025", "4분기") 로 연도desc, 분기desc 정렬
    return sorted(columns, key=lambda c: tuple(c.rsplit(" ", 1)), reverse=True)


def build_quarterly_pivot(items: pd.DataFrame, fs_div: str, sj_nm: str) -> pd.DataFrame:
    """계정과목(행) x 분기(열) 형태로, 각 분기 '단독' 실적을 계산해 보여준다.
    재무상태표/자본변동표처럼 특정 시점 값(누적 개념이 없는 계정)에는 쓰지 않는다."""
    subset = items[(items["fs_div"] == fs_div) & (items["sj_nm"] == sj_nm)].copy()
    if subset.empty:
        return pd.DataFrame()

    subset = filter_to_reported_period(subset)
    subset["금액"] = subset["thstrm_amount"].apply(amount_to_number)
    subset["ord_num"] = pd.to_numeric(subset["ord"], errors="coerce")
    subset["key"] = subset["account_id"].where(
        subset["account_id"].notna() & (subset["account_id"] != ""), subset["account_nm"]
    )

    label_map = subset.sort_values("bsns_year").groupby("key")["account_nm"].last()
    account_order = subset.groupby("key")["ord_num"].min().sort_values()
    years = sorted(subset["bsns_year"].unique())
    col_builder = _cumulative_quarter_columns if sj_nm in CUMULATIVE_SJ_NM else _standalone_quarter_columns

    result: dict[str, dict[str, float]] = {}
    for key, grp in subset.groupby("key"):
        # 같은 계정이 같은 보고서 안에 중복으로 잡히는 경우가 있어(예: ProfitLoss가 자본변동표에도 잡힘) first()로 정리
        cum = grp.groupby(["bsns_year", "reprt_name"])["금액"].first()
        cum = {k: v for k, v in cum.items()}
        row = col_builder(cum, years)
        if row:
            result[key] = row

    if not result:
        return pd.DataFrame()
    pivot = pd.DataFrame(result).T
    pivot = pivot.reindex(account_order.index)
    pivot.index = pivot.index.map(label_map)
    pivot.index.name = "account_nm"
    pivot = pivot[_sort_quarter_columns(pivot.columns)]
    return pivot


def build_quarterly_key_metrics(items: pd.DataFrame, fs_div: str) -> pd.DataFrame:
    """매출액·영업이익·당기순이익의 분기별 단독 실적 (4분기 포함, 연간 - 1~3분기로 역산)."""
    sub = items[(items["fs_div"] == fs_div) & (items["sj_nm"].isin(["손익계산서", "포괄손익계산서"]))].copy()
    if sub.empty:
        return pd.DataFrame()
    sub = filter_to_reported_period(sub)
    sub["금액"] = sub["thstrm_amount"].apply(amount_to_number)
    years = sorted(sub["bsns_year"].unique())

    rows: dict[str, dict[str, float]] = {}
    for label, account_id in KEY_METRIC_IDS.items():
        fallback_names = KEY_METRIC_FALLBACK_NAMES.get(label, [])
        mask = (sub["account_id"] == account_id) | (sub["account_nm"].isin(fallback_names))
        match = sub[mask]
        if match.empty:
            continue
        cum = match.groupby(["bsns_year", "reprt_name"])["금액"].first()
        row = _standalone_quarter_columns(dict(cum.items()), years)
        if row:
            rows[label] = row

    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).T
    return df[_sort_quarter_columns(df.columns)]
