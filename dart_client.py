"""DART(전자공시시스템) Open API 클라이언트.

- corpCode.xml 마스터 목록을 받아 회사명 -> corp_code 매핑을 만든다.
- fnlttSinglAcntAll(단일회사 전체 재무제표) API로 재무제표 원본 데이터를 가져온다.
- 정식 분기보고서가 나오기 전, 회사가 먼저 내는 '영업(잠정)실적(공정공시)'도 파싱해서 가져온다.
"""
from __future__ import annotations

import io
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from datetime import datetime as _dt
from pathlib import Path

import pandas as pd
import requests

BASE_URL = "https://opendart.fss.or.kr/api"
CORP_CODE_CACHE = Path(__file__).parent / "data" / "corpCode.xml"

# 사업보고서 종류
REPORT_CODES = {
    "11013": "1분기보고서",
    "11012": "반기보고서",
    "11014": "3분기보고서",
    "11011": "사업보고서",
}

# 재무제표 구분
FS_DIVISIONS = {
    "CFS": "연결재무제표",
    "OFS": "개별재무제표",
}

# 관심 기업 (필요하면 여기에 더 추가)
# 참고: DART 등록명 기준. LS ELECTRIC은 DART에 "엘에스일렉트릭"(한글 표기)으로 등록되어 있음
WATCHLIST = ["삼성전자", "SK하이닉스", "리브스메드", "리센스메디컬", "엘에스일렉트릭"]


class DartApiError(RuntimeError):
    pass


@dataclass
class CorpInfo:
    corp_code: str
    corp_name: str
    stock_code: str


class DartClient:
    def __init__(self, api_key: str):
        if not api_key:
            raise ValueError("DART_API_KEY가 비어 있습니다.")
        self.api_key = api_key

    def _get(self, path: str, **params) -> dict:
        params["crtfc_key"] = self.api_key
        resp = requests.get(f"{BASE_URL}/{path}", params=params, timeout=30)
        resp.raise_for_status()
        return resp

    def load_corp_codes(self, force_refresh: bool = False) -> list[CorpInfo]:
        """상장/비상장 전체 회사의 corp_code 매핑을 로드한다 (로컬 캐시 사용)."""
        if force_refresh or not CORP_CODE_CACHE.exists():
            resp = self._get("corpCode.xml")
            zf = zipfile.ZipFile(io.BytesIO(resp.content))
            xml_bytes = zf.read("CORPCODE.xml")
            CORP_CODE_CACHE.parent.mkdir(parents=True, exist_ok=True)
            CORP_CODE_CACHE.write_bytes(xml_bytes)
        else:
            xml_bytes = CORP_CODE_CACHE.read_bytes()

        root = ET.fromstring(xml_bytes)
        corps = []
        for el in root.findall("list"):
            corps.append(
                CorpInfo(
                    corp_code=(el.findtext("corp_code") or "").strip(),
                    corp_name=(el.findtext("corp_name") or "").strip(),
                    stock_code=(el.findtext("stock_code") or "").strip(),
                )
            )
        return corps

    def find_corp(self, corp_name: str) -> CorpInfo:
        """회사명으로 상장사(stock_code 존재)를 우선 검색한다."""
        corps = self.load_corp_codes()
        candidates = [c for c in corps if c.corp_name == corp_name]
        listed = [c for c in candidates if c.stock_code]
        if listed:
            return listed[0]
        if candidates:
            return candidates[0]
        raise DartApiError(f"'{corp_name}'에 해당하는 corp_code를 찾지 못했습니다.")

    def get_financial_statement(
        self, corp_code: str, bsns_year: str, reprt_code: str, fs_div: str = "CFS"
    ) -> dict:
        """fnlttSinglAcntAll: 단일회사 전체 재무제표 원본 데이터."""
        resp = self._get(
            "fnlttSinglAcntAll.json",
            corp_code=corp_code,
            bsns_year=bsns_year,
            reprt_code=reprt_code,
            fs_div=fs_div,
        )
        data = resp.json()
        if data.get("status") != "000":
            raise DartApiError(f"DART API 오류 [{data.get('status')}]: {data.get('message')}")
        return data

    def find_listing_date(self, corp_code: str) -> str | None:
        """공시 이력에서 '증권발행실적보고서'(IPO 직후 제출)를 찾아 상장일의 근사치로 사용한다.
        이미 오래전부터 정기공시(사업/분기/반기보고서)를 내고 있던 회사라면, 그 '증권발행실적보고서'는
        IPO가 아니라 나중의 회사채·유상증자 등일 가능성이 높으므로 무시한다.
        DART 전자공시가 시작되기 훨씬 전에 상장한 오래된 회사는 어차피 찾지 못한다 (None 반환)."""
        resp = self._get(
            "list.json", corp_code=corp_code, bgn_de="19990101", page_count="100", page_no="1"
        )
        data = resp.json()
        if data.get("status") != "000":
            return None
        candidates = [
            item for item in data.get("list", [])
            if "증권발행실적보고서" in item.get("report_nm", "")
        ]
        if not candidates:
            return None
        earliest_issuance = min(candidates, key=lambda item: item.get("rcept_dt", "99999999"))
        issuance_date = earliest_issuance.get("rcept_dt")

        # 정기공시 이력 중 가장 오래된 날짜와 비교해, 시기가 비슷할 때만(=신규 상장) 신뢰한다.
        resp2 = self._get(
            "list.json", corp_code=corp_code, bgn_de="19990101", pblntf_ty="A", page_count="100", page_no="1"
        )
        data2 = resp2.json()
        periodic_dates = [item.get("rcept_dt") for item in data2.get("list", []) if item.get("rcept_dt")]
        if not periodic_dates:
            return None
        earliest_periodic = min(periodic_dates)

        try:
            gap_days = abs((_dt.strptime(issuance_date, "%Y%m%d") - _dt.strptime(earliest_periodic, "%Y%m%d")).days)
        except (ValueError, TypeError):
            return None
        if gap_days > 400:
            return None
        return issuance_date

    def find_preliminary_earnings(self, corp_code: str, bgn_de: str | None = None) -> dict | None:
        """가장 최근의 '영업(잠정)실적(공정공시)' 공시를 찾는다. 정식 분기/반기보고서가 나오기
        1~3주 전에 회사가 먼저 발표하는 매출액·영업이익 잠정치다.
        '공정공시'는 DART 분류상 거래소공시(pblntf_ty=I)에 속해서, 임원 지분보고 등 다른 공시가
        많은 대형주라도 검색 결과가 크게 줄어든다."""
        params = {"corp_code": corp_code, "pblntf_ty": "I", "page_count": "100"}
        if bgn_de:
            params["bgn_de"] = bgn_de
        resp = self._get("list.json", **params)
        data = resp.json()
        if data.get("status") != "000":
            return None
        candidates = [
            item for item in data.get("list", []) if "영업(잠정)실적" in item.get("report_nm", "")
        ]
        if not candidates:
            return None
        latest = max(candidates, key=lambda item: item.get("rcept_dt", ""))
        return {"rcept_no": latest.get("rcept_no"), "rcept_dt": latest.get("rcept_dt")}

    def get_preliminary_earnings(self, rcept_no: str) -> dict:
        """'연결재무제표기준 영업(잠정)실적' 공정공시 문서를 내려받아 매출액/영업이익/당기순이익의
        당해(그 분기만의) 실적을 원 단위로 환산해 반환한다.
        문서는 <meta charset=euc-kr>이라고 되어 있지만 실제로는 UTF-8로 인코딩되어 있다 (실측 확인됨)."""
        resp = self._get("document.xml", rcept_no=rcept_no)
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        fn = zf.namelist()[0]
        content = zf.read(fn).decode("utf-8", errors="replace")

        try:
            tables = pd.read_html(io.StringIO(content))
        except ValueError as e:
            raise DartApiError(f"잠정실적 문서에서 표를 찾지 못했습니다: {e}") from e
        if len(tables) < 2:
            raise DartApiError("잠정실적 문서 형식을 인식하지 못했습니다 (표 2개 필요).")

        period_table, main = tables[0], tables[1]
        thstrm_start = str(period_table.iloc[1, 1]).strip()
        thstrm_end = str(period_table.iloc[1, 3]).strip()

        unit_text = str(main.iloc[1, 4])
        if "조원" in unit_text:
            unit = 1e12
        elif "억원" in unit_text:
            unit = 1e8
        elif "백만원" in unit_text:
            unit = 1e6
        elif "천원" in unit_text:
            unit = 1e3
        else:
            unit = 1

        def parse_num(v) -> float | None:
            s = str(v).replace(",", "").strip()
            if s in ("", "-", "nan", "None"):
                return None
            try:
                return float(s)
            except ValueError:
                return None

        label_to_account = {
            "매출액": ("ifrs-full_Revenue", "매출액"),
            "영업이익": ("dart_OperatingIncomeLoss", "영업이익"),
            "당기순이익": ("ifrs-full_ProfitLoss", "당기순이익"),
        }

        # 표는 계정과목마다 "당해실적"(그 분기만의 값) 행과 "누계실적"(연초 누적) 행이 쌍으로 나온다.
        # 당해실적 행만 취한다.
        metrics: dict[str, dict] = {}
        for i in range(len(main)):
            label = str(main.iloc[i, 0]).strip()
            basis = str(main.iloc[i, 1]).strip()
            if basis != "당해실적" or label not in label_to_account:
                continue
            thstrm = parse_num(main.iloc[i, 2])
            if thstrm is None:
                continue
            account_id, account_nm = label_to_account[label]
            metrics[account_id] = {"account_nm": account_nm, "amount": thstrm * unit}

        if not metrics:
            raise DartApiError("잠정실적 문서에서 매출액/영업이익 수치를 찾지 못했습니다.")

        return {"thstrm_start": thstrm_start, "thstrm_end": thstrm_end, "metrics": metrics}


def quarter_of(month: int) -> int:
    return (month - 1) // 3 + 1


def build_preliminary_raw_data(prelim: dict, rcept_no: str) -> dict:
    """get_preliminary_earnings() 결과를, db.save_fetch()가 기대하는 DART 응답 형태로 감싼다."""
    rows = []
    for i, (account_id, info) in enumerate(prelim["metrics"].items()):
        rows.append({
            "rcept_no": rcept_no,
            "sj_div": "IS",
            "sj_nm": "포괄손익계산서",
            "account_id": account_id,
            "account_nm": info["account_nm"],
            "thstrm_nm": f"잠정 {prelim['thstrm_start']}~{prelim['thstrm_end']}",
            "thstrm_amount": str(int(info["amount"])),
            "frmtrm_amount": "",
            "bfefrmtrm_amount": "",
            "ord": str(i + 1),
            "currency": "KRW",
        })
    return {"status": "000", "message": "정상 (DART 잠정실적 공정공시 기반)", "list": rows}
