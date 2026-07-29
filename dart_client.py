"""DART(전자공시시스템) Open API 클라이언트.

- corpCode.xml 마스터 목록을 받아 회사명 -> corp_code 매핑을 만든다.
- fnlttSinglAcntAll(단일회사 전체 재무제표) API로 재무제표 원본 데이터를 가져온다.
"""
from __future__ import annotations

import io
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path

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

        from datetime import datetime as _dt
        try:
            gap_days = abs((_dt.strptime(issuance_date, "%Y%m%d") - _dt.strptime(earliest_periodic, "%Y%m%d")).days)
        except (ValueError, TypeError):
            return None
        if gap_days > 400:
            return None
        return issuance_date
