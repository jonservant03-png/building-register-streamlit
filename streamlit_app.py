import html
import io
import json
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, unquote, urlsplit, urlunsplit

import pandas as pd
import requests
import streamlit as st

PY_PER_SQM = 1 / 3.305785
BUILDING_API_BASE = "https://apis.data.go.kr/1613000/BldRgstHubService"
JUSO_API_URL = "https://business.juso.go.kr/addrlink/addrLinkApi.do"
KAKAO_SUBWAY_CATEGORY = "SW8"
KAKAO_COORD2ADDRESS_URL = "https://dapi.kakao.com/v2/local/geo/coord2address.json"


class ApiRequestError(RuntimeError):
    pass


@dataclass
class JusoResult:
    road_addr: str
    jibun_addr: str
    bd_mgt_sn: str
    adm_cd: str
    mt_yn: str
    lnbr_mnnm: str
    lnbr_slno: str


@dataclass
class BuildingResult:
    name: str
    address: str
    approval_year: str
    floors: str
    total_area_py: str
    collective_building: str = ""
    station: str = ""
    walk_time: str = ""


@dataclass
class UnitQuery:
    dong_nm: str
    ho_nm: str
    label: str


def clean_text(value: Any) -> str:
    return str(value or "").strip()


def html_text(value: Any) -> str:
    return html.escape(clean_text(value))


def sanitize_url(url: str) -> str:
    parts = urlsplit(url)
    safe_query_parts = []
    for part in parts.query.split("&"):
        if part.lower().startswith("servicekey=") or part.lower().startswith("confmkey="):
            key = part.split("=", 1)[0]
            safe_query_parts.append(f"{key}=***")
        else:
            safe_query_parts.append(part)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "&".join(safe_query_parts), parts.fragment))


def secret_value(name: str, default: str = "") -> str:
    try:
        return clean_text(st.secrets.get(name, default))
    except Exception:
        return default


def require_password() -> bool:
    app_password = secret_value("APP_PASSWORD")
    if not app_password:
        return True

    st.markdown("### 접속 확인")
    entered = st.text_input("비밀번호", type="password")
    if entered == app_password:
        return True
    if entered:
        st.error("비밀번호가 맞지 않습니다.")
    return False


def only_digits(value: str, width: int = 4) -> str:
    found = re.sub(r"\D", "", clean_text(value))
    return found.zfill(width) if found else "0000"


def format_year(use_apr_day: str) -> str:
    digits = re.sub(r"\D", "", clean_text(use_apr_day))
    return f"{digits[:4]}년" if len(digits) >= 4 else ""


def format_floors(ground: Any, underground: Any) -> str:
    ground_num = int(float(ground or 0))
    underground_num = int(float(underground or 0))
    ground_text = f"{ground_num}F" if ground_num else ""
    under_text = f"B{underground_num}" if underground_num else ""
    if ground_text and under_text:
        return f"{ground_text}/{under_text}"
    return ground_text or under_text


def format_py(area_sqm: Any) -> str:
    try:
        value = float(area_sqm or 0) * PY_PER_SQM
    except (TypeError, ValueError):
        return ""
    return f"{round(value):,} py" if value else ""


def area_float(area_sqm: Any) -> float:
    try:
        return float(area_sqm or 0)
    except (TypeError, ValueError):
        return 0.0


def area_py_value(area_sqm: Any) -> str:
    value = area_float(area_sqm) * PY_PER_SQM
    return f"{value:.1f}" if value else ""


def area_sqm_value(area_sqm: Any) -> str:
    value = area_float(area_sqm)
    return f"{value:.2f}" if value else ""


def display_address(juso: JusoResult) -> str:
    """'00구 00길 0' 형태로 간소화: 도/시 및 맨 뒤 (동) 표기 제거."""
    road = re.sub(r"\s*\(.*?\)\s*$", "", clean_text(juso.road_addr)).strip()
    tokens = road.split()
    # 도(경기도)·시(용인시/광역시/특별자치시) 레벨은 구/군/도로명이 나올 때까지 제거
    while tokens and (tokens[0].endswith("도") or tokens[0].endswith("시")):
        tokens.pop(0)
    return " ".join(tokens)


def request_json(url: str, params: dict[str, Any], headers: dict[str, str] | None = None) -> dict[str, Any]:
    try:
        response = requests.get(url, params=params, headers=headers, timeout=20)
    except requests.RequestException as exc:
        raise ApiRequestError(f"API 연결 실패: {exc}") from exc

    if response.status_code >= 400:
        safe_url = sanitize_url(response.url)
        body = clean_text(response.text)[:300]
        if "BldRgstService" in url and response.status_code >= 500:
            raise ApiRequestError(
                "건축물대장 API 서버 오류가 발생했습니다. "
                "공공데이터포털에서 '국토교통부_건축물대장정보 서비스' 활용신청이 승인됐는지, "
                "서비스키를 Decoding 키 또는 일반 인증키로 넣었는지 확인한 뒤 다시 시도하세요. "
                f"상태코드: {response.status_code}, URL: {safe_url}, 응답: {body}"
            )
        raise ApiRequestError(f"API 요청 실패. 상태코드: {response.status_code}, URL: {safe_url}, 응답: {body}")

    try:
        return response.json()
    except ValueError as exc:
        safe_url = sanitize_url(response.url)
        body = clean_text(response.text)[:300]
        raise ApiRequestError(f"JSON 응답을 읽지 못했습니다. URL: {safe_url}, 응답: {body}") from exc


def normalize_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    body = payload.get("response", {}).get("body", {})
    items = body.get("items", {})
    item = items.get("item", []) if isinstance(items, dict) else []
    if isinstance(item, dict):
        return [item]
    return item if isinstance(item, list) else []



def ensure_success_response(payload: dict[str, Any]) -> None:
    header = payload.get("response", {}).get("header", {})
    code = clean_text(header.get("resultCode"))
    message = clean_text(header.get("resultMsg"))
    if code and code not in {"00", "0", "NORMAL_CODE"}:
        raise ApiRequestError(f"공공데이터포털 응답 오류: {code} {message}")


def search_juso(keyword: str, juso_key: str) -> JusoResult | None:
    data = request_json(
        JUSO_API_URL,
        {
            "confmKey": juso_key,
            "currentPage": 1,
            "countPerPage": 5,
            "keyword": keyword,
            "resultType": "json",
        },
    )
    common = data.get("results", {}).get("common", {})
    if common.get("errorCode") != "0":
        raise RuntimeError(common.get("errorMessage", "도로명주소 API 조회 실패"))

    items = data.get("results", {}).get("juso", [])
    if not items:
        return None

    item = items[0]
    return JusoResult(
        road_addr=clean_text(item.get("roadAddr")),
        jibun_addr=clean_text(item.get("jibunAddr")),
        bd_mgt_sn=clean_text(item.get("bdMgtSn")),
        adm_cd=clean_text(item.get("admCd")),
        mt_yn=clean_text(item.get("mtYn")),
        lnbr_mnnm=clean_text(item.get("lnbrMnnm")),
        lnbr_slno=clean_text(item.get("lnbrSlno")),
    )


def data_key_candidates(data_key: str) -> list[str]:
    raw_key = clean_text(data_key)
    decoded_key = unquote(raw_key)
    keys = [raw_key]
    if decoded_key and decoded_key != raw_key:
        keys.append(decoded_key)
    return keys


def building_params(juso: JusoResult, service_key: str, rows: int = 100) -> dict[str, Any]:
    if len(juso.adm_cd) < 10:
        raise RuntimeError("도로명주소 결과에서 법정동코드를 찾지 못했습니다.")

    return {
        "serviceKey": service_key,
        "sigunguCd": juso.adm_cd[:5],
        "bjdongCd": juso.adm_cd[5:10],
        "platGbCd": "1" if juso.mt_yn == "1" else "0",
        "bun": only_digits(juso.lnbr_mnnm),
        "ji": only_digits(juso.lnbr_slno),
        "numOfRows": rows,
        "pageNo": 1,
        "_type": "json",
    }


def fetch_building_api(endpoint: str, juso: JusoResult, data_key: str, rows: int = 100) -> list[dict[str, Any]]:
    errors: list[str] = []
    for index, service_key in enumerate(data_key_candidates(data_key), start=1):
        params = building_params(juso, service_key, rows=rows)
        try:
            data = request_json(f"{BUILDING_API_BASE}/{endpoint}", params)
            ensure_success_response(data)
            return normalize_items(data)
        except ApiRequestError as exc:
            errors.append(f"{index}차 시도 실패: {exc}")
            continue
    raise ApiRequestError("건축물대장 API 호출이 모두 실패했습니다. " + " / ".join(errors))


def fetch_building_api_with_extra(
    endpoint: str,
    juso: JusoResult,
    data_key: str,
    rows: int = 100,
    extra_params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    errors: list[str] = []
    for index, service_key in enumerate(data_key_candidates(data_key), start=1):
        params = building_params(juso, service_key, rows=rows)
        if extra_params:
            params.update(extra_params)
        try:
            data = request_json(f"{BUILDING_API_BASE}/{endpoint}", params)
            ensure_success_response(data)
            return normalize_items(data)
        except ApiRequestError as exc:
            errors.append(f"{index}차 시도 실패: {exc}")
            continue
    raise ApiRequestError("건축물대장 API 호출이 모두 실패했습니다. " + " / ".join(errors))



def fetch_building_title(juso: JusoResult, data_key: str) -> dict[str, Any] | None:
    items = fetch_building_api("getBrTitleInfo", juso, data_key, rows=30)
    if not items:
        return None

    def score(item: dict[str, Any]) -> tuple[int, float]:
        name_score = 1 if clean_text(item.get("bldNm")) else 0
        try:
            area = float(item.get("totArea") or 0)
        except (TypeError, ValueError):
            area = 0
        return name_score, area

    return sorted(items, key=score, reverse=True)[0]


def fetch_floor_outline(juso: JusoResult, data_key: str) -> pd.DataFrame:
    rows = fetch_building_api("getBrFlrOulnInfo", juso, data_key, rows=300)
    records: list[dict[str, str]] = []
    for row in rows:
        area_sqm = row.get("area")
        records.append(
            {
                "동명": clean_text(row.get("dongNm")),
                "층구분": clean_text(row.get("flrGbCdNm")),
                "층": clean_text(row.get("flrNoNm")) or clean_text(row.get("flrNo")),
                "구조": clean_text(row.get("strctCdNm")),
                "용도": clean_text(row.get("mainPurpsCdNm")),
                "면적㎡": clean_text(area_sqm),
                "면적py": area_py_value(area_sqm),
            }
        )
    return pd.DataFrame(records)


def filter_by_dong_ho(rows: list[dict[str, Any]], dong_nm: str, ho_nm: str) -> list[dict[str, Any]]:
    dong_key = clean_text(dong_nm).replace("동", "")
    ho_key = clean_text(ho_nm).replace("호", "")
    filtered = []
    for row in rows:
        row_dong = clean_text(row.get("dongNm")).replace("동", "")
        row_ho = clean_text(row.get("hoNm")).replace("호", "")
        if dong_key and dong_key not in row_dong:
            continue
        if ho_key and ho_key not in row_ho:
            continue
        filtered.append(row)
    return filtered


def unit_key(value: Any, suffix: str) -> str:
    text = clean_text(value).replace(suffix, "").replace(" ", "")
    return str(int(text)) if text.isdigit() else text


def first_clean(row: dict[str, Any] | pd.Series, keys: list[str]) -> str:
    for key in keys:
        value = clean_text(row.get(key))
        if value:
            return value
    return ""


def is_private_area_row(row: dict[str, Any]) -> bool:
    gb_code = clean_text(row.get("exposPubuseGbCd"))
    gb_name = clean_text(row.get("exposPubuseGbCdNm"))
    return gb_code == "1" or gb_name == "전유"



def format_collective_building(building: dict[str, Any]) -> str:
    register_type = clean_text(building.get("regstrGbCdNm"))
    if not register_type:
        return ""
    return "예" if "집합" in register_type else "아니오"



def fetch_private_unit(juso: JusoResult, data_key: str, dong_nm: str, ho_nm: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    extra_params = {}
    if dong_nm:
        extra_params["dongNm"] = dong_nm
    if ho_nm:
        extra_params["hoNm"] = ho_nm

    expos_rows = fetch_building_api_with_extra("getBrExposInfo", juso, data_key, rows=300, extra_params=extra_params)
    pubuse_rows = fetch_building_api_with_extra(
        "getBrExposPubuseAreaInfo", juso, data_key, rows=300, extra_params=extra_params
    )

    if dong_nm or ho_nm:
        expos_rows = filter_by_dong_ho(expos_rows, dong_nm, ho_nm)
        pubuse_rows = filter_by_dong_ho(pubuse_rows, dong_nm, ho_nm)

    expos_by_unit = {
        (unit_key(row.get("dongNm"), "동"), unit_key(row.get("hoNm"), "호")): row for row in expos_rows
    }
    common_area_by_unit: dict[tuple[str, str], float] = {}
    for row in pubuse_rows:
        if is_private_area_row(row):
            continue
        key = (unit_key(row.get("dongNm"), "동"), unit_key(row.get("hoNm"), "호"))
        common_area_by_unit[key] = common_area_by_unit.get(key, 0.0) + area_float(
            first_clean(row, ["area", "exposArea", "exposAreaSum", "totArea"])
        )

    expos_records = []
    for row in pubuse_rows:
        if not is_private_area_row(row):
            continue
        key = (unit_key(row.get("dongNm"), "동"), unit_key(row.get("hoNm"), "호"))
        detail = expos_by_unit.get(key, {})
        area_sqm = first_clean(row, ["area", "exposArea", "exposAreaSum", "totArea"])
        common_area_sqm = common_area_by_unit.get(key, 0.0)
        purpose = first_clean(row, ["mainPurpsCdNm", "etcPurps"]) or first_clean(detail, ["mainPurpsCdNm", "etcPurps"])
        expos_records.append(
            {
                "동명": clean_text(row.get("dongNm")),
                "호명": clean_text(row.get("hoNm")),
                "층": first_clean(row, ["flrNoNm", "flrNo"]) or first_clean(detail, ["flrNoNm", "flrNo"]),
                "전유면적㎡": clean_text(area_sqm),
                "전유면적py": area_py_value(area_sqm),
                "전유공용면적합계㎡": area_sqm_value(common_area_sqm),
                "전유공용면적합계py": area_py_value(common_area_sqm),
                "용도": purpose,
            }
        )

    return pd.DataFrame(expos_records), pd.DataFrame()


def parse_unit_queries(text: str, fallback_dong: str = "", fallback_ho: str = "") -> list[UnitQuery]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    queries: list[UnitQuery] = []

    for line in lines:
        tokens = [token.strip() for token in re.split(r"[,/\t|]+", line) if token.strip()]
        dong_nm = ""
        ho_nm = ""

        if len(tokens) >= 2:
            dong_nm, ho_nm = tokens[0], tokens[1]
        else:
            dong_match = re.search(r"([A-Za-z0-9가-힣-]+동)", line)
            ho_match = re.search(r"([A-Za-z0-9가-힣-]+호)", line)
            if dong_match:
                dong_nm = dong_match.group(1)
            if ho_match:
                ho_nm = ho_match.group(1)
            if not dong_nm and not ho_nm:
                ho_nm = line

        label = " ".join([part for part in (dong_nm, ho_nm) if part]) or "전체"
        queries.append(UnitQuery(dong_nm=dong_nm, ho_nm=ho_nm, label=label))

    if not queries and (fallback_dong or fallback_ho):
        label = " ".join([part for part in (fallback_dong, fallback_ho) if part])
        queries.append(UnitQuery(dong_nm=fallback_dong, ho_nm=fallback_ho, label=label))

    return queries



def geocode_kakao(address: str, kakao_key: str) -> tuple[float, float] | None:
    data = request_json(
        "https://dapi.kakao.com/v2/local/search/address.json",
        {"query": address},
        {"Authorization": f"KakaoAK {kakao_key}"},
    )
    docs = data.get("documents", [])
    if not docs:
        return None
    return float(docs[0]["x"]), float(docs[0]["y"])


WALK_MINUTE_THRESHOLD = 20
WALK_METERS_PER_MINUTE = 67


def format_station_name(place_name: str) -> str:
    """Kakao place_name '역이름 노선' -> '노선 역이름' 순서로 재정렬."""
    text = clean_text(place_name)
    parts = text.rsplit(" ", 1)
    if len(parts) == 2:
        station, line = parts
        return f"{line} {station}"
    return text


def kakao_local_search(path: str, params: dict[str, Any], kakao_key: str) -> list[dict[str, Any]]:
    data = request_json(
        f"https://dapi.kakao.com/v2/local/{path}",
        params,
        {"Authorization": f"KakaoAK {kakao_key}"},
    )
    return data.get("documents", [])


def find_nearest_subway(x: float, y: float, kakao_key: str) -> tuple[str, int] | None:
    docs = kakao_local_search(
        "search/category.json",
        {
            "category_group_code": KAKAO_SUBWAY_CATEGORY,
            "x": x,
            "y": y,
            "radius": 2000,
            "sort": "distance",
            "size": 1,
        },
        kakao_key,
    )
    if not docs:
        return None
    return docs[0].get("place_name", ""), int(docs[0].get("distance") or 0)


LANDMARK_RADIUS = 3000  # '인근'으로 부를 수 있는 최대 거리(m)


LANDMARK_NAME_EXCLUDE = ("화물",)  # "양재화물터미널" 등 랜드마크로 부적절한 명칭 제외
PRIORITY_PUBLIC_QUERIES = (
    "구청",
    "시청",
    "도청",
    "검찰청",
    "법원",
    "공기업 본사",
    "공기업 본부",
    "공단 본사",
    "공단 본부",
    "공사 본사",
    "공사 본부",
    "국민연금공단 본사",
    "국민연금공단 본부",
)
PRIORITY_GOV_KEYWORDS = ("구청", "시청", "도청", "검찰청", "법원")
GENERIC_PUBLIC_CORP_KEYWORDS = ("공기업", "공단", "공사")
SPECIFIC_PUBLIC_CORP_KEYWORDS = (
    "국민연금공단",
    "국민건강보험공단",
    "근로복지공단",
    "한국전력공사",
    "한국토지주택공사",
    "한국도로공사",
    "한국수자원공사",
    "한국가스공사",
    "한국철도공사",
)
HQ_KEYWORDS = ("본사", "본부")
PUBLIC_CATEGORY_KEYWORDS = ("공공", "사회기관", "행정", "관공서")
TRANSIT_CATEGORY_KEYWORDS = ("교통", "수송", "지하철", "전철", "버스", "터미널", "기차", "철도", "공항")
OTHER_TRANSIT_SEARCHES = (
    ({"query": "버스터미널"}, ("터미널",)),
    ({"query": "터미널"}, ("터미널",)),
    ({"query": "기차역"}, ("기차", "철도")),
    ({"query": "철도역"}, ("기차", "철도")),
    ({"query": "공항"}, ("공항",)),
)


def walk_minutes(distance_m: int) -> int:
    return max(1, round(distance_m / WALK_METERS_PER_MINUTE))


def subway_label(subway: tuple[str, int]) -> tuple[str, str]:
    """지하철 (역명, 거리m) -> ('노선 역이름', '도보 NN분')."""
    place_name, distance_m = subway
    return format_station_name(place_name), f"도보 {walk_minutes(distance_m)}분"


def landmark_label(name: str, distance_m: int | None = None) -> tuple[str, str]:
    if distance_m is None:
        return f"{name} 인근", ""
    minutes = walk_minutes(distance_m)
    if minutes <= WALK_MINUTE_THRESHOLD:
        return name, f"도보 {minutes}분"
    return f"{name} 인근", ""


def is_priority_public_place(name: str, category: str) -> bool:
    if any(word in category for word in TRANSIT_CATEGORY_KEYWORDS):
        return False
    if any(word in name for word in PRIORITY_GOV_KEYWORDS):
        return True
    is_hq = any(word in name for word in HQ_KEYWORDS)
    if not is_hq:
        return False
    if any(word in name for word in SPECIFIC_PUBLIC_CORP_KEYWORDS):
        return True
    is_generic_public_corp = any(word in name for word in GENERIC_PUBLIC_CORP_KEYWORDS)
    is_public_category = any(word in category for word in PUBLIC_CATEGORY_KEYWORDS)
    return is_generic_public_corp and is_public_category


def find_nearest_landmark(
    x: float, y: float, kakao_key: str, subway: tuple[str, int] | None = None
) -> tuple[str, str]:
    """주요 공공기관 > 교통시설 > 관공서 > 백화점 > 대학교 > IC 순으로 표기 반환."""

    def nearest(
        path: str,
        queries: list[dict[str, Any]],
        require: tuple[str, ...] | None = None,
        require_cat: tuple[str, ...] | None = None,
        predicate: Callable[[str, str], bool] | None = None,
    ) -> tuple[str, int | None]:
        best_name, best_dist = "", None
        for extra in queries:
            params = {"x": x, "y": y, "radius": LANDMARK_RADIUS, "sort": "distance", "size": 15}
            params.update(extra)
            for doc in kakao_local_search(path, params, kakao_key):
                name = clean_text(doc.get("place_name"))
                category = clean_text(doc.get("category_name"))
                dist = int(doc.get("distance") or 0)
                if not dist or dist > LANDMARK_RADIUS:
                    continue
                if any(word in name for word in LANDMARK_NAME_EXCLUDE):
                    continue
                if require and not any(word in name for word in require):
                    continue
                if require_cat and not any(word in category for word in require_cat):
                    continue
                if predicate and not predicate(name, category):
                    continue
                if best_dist is None or dist < best_dist:
                    best_name, best_dist = name, dist
        return best_name, best_dist

    name, _ = nearest(
        "search/keyword.json",
        [{"query": query} for query in PRIORITY_PUBLIC_QUERIES],
        predicate=is_priority_public_place,
    )
    if name:
        return f"{name} 인근", ""

    transit: list[tuple[str, int]] = []
    for query, categories in OTHER_TRANSIT_SEARCHES:
        name, dist = nearest("search/keyword.json", [query], require_cat=categories)
        if name and dist is not None:
            transit.append((name, dist))
    if transit:
        name, dist = min(transit, key=lambda t: t[1])
        return landmark_label(name, dist)

    name, _ = nearest("search/category.json", [{"category_group_code": "PO3"}])
    if name:
        return f"{name} 인근", ""
    name, _ = nearest("search/keyword.json", [{"query": "백화점"}], require_cat=("백화점",))
    if name:
        return f"{name} 인근", ""
    name, _ = nearest("search/keyword.json", [{"query": "대학교"}], require_cat=("대학교",))
    if name:
        return f"{name} 인근", ""
    name, _ = nearest(
        "search/keyword.json", [{"query": "IC"}, {"query": "나들목"}], require=("IC", "JC", "나들목")
    )
    if name:
        return f"{name} 인근", ""
    return "", ""


def nearest_subway(address: str, kakao_key: str) -> tuple[str, str]:
    if not kakao_key:
        return "", ""
    point = geocode_kakao(address, kakao_key)
    if not point:
        return "", ""

    x, y = point
    subway = find_nearest_subway(x, y, kakao_key)
    if subway:
        minutes = walk_minutes(subway[1])
        if minutes <= WALK_MINUTE_THRESHOLD:
            return subway_label(subway)

    station, walk = find_nearest_landmark(x, y, kakao_key, subway=subway)
    if station:
        return station, walk

    if subway:
        return subway_label(subway)
    return "", ""


def parse_naver_request(raw_input: str) -> tuple[str, dict[str, str]]:
    text = clean_text(raw_input)
    if not text:
        return "", {}
    if text.startswith("http"):
        return text, {}

    headers: dict[str, str] = {}
    url = ""
    try:
        parts = shlex.split(text.replace("^\n", " ").replace("`\n", " "))
    except ValueError:
        parts = text.split()

    index = 0
    while index < len(parts):
        token = parts[index]
        lower = token.lower()
        if token.startswith("http"):
            url = token
        elif lower in {"-h", "--header"} and index + 1 < len(parts):
            header = parts[index + 1]
            if ":" in header:
                key, value = header.split(":", 1)
                headers[key.strip()] = value.strip()
            index += 1
        elif lower in {"-b", "--cookie", "--cookie-jar"} and index + 1 < len(parts):
            headers["Cookie"] = parts[index + 1]
            index += 1
        index += 1
    return url, headers


def url_with_page(url: str, page: int) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["page"] = str(page)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query, doseq=True), parts.fragment))


def naver_source_meta(url: str) -> dict[str, str]:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    return {
        "API법정동코드": query.get("cortarNo", ""),
        "API부동산유형": query.get("realEstateType", ""),
        "API거래유형": query.get("tradeType", ""),
        "API페이지": query.get("page", ""),
    }


def fetch_naver_json(url: str, headers: dict[str, str]) -> dict[str, Any]:
    request_headers = {
        "accept": "application/json, text/plain, */*",
        "user-agent": "Mozilla/5.0",
        "referer": "https://new.land.naver.com/",
    }
    request_headers.update({key: value for key, value in headers.items() if value})
    return request_json(url, {}, request_headers)


def naver_read(obj: dict[str, Any], names: list[str]) -> str:
    return clean_text(readOwn(obj, names))


def naver_trade(value: str) -> str:
    return {"A1": "매매", "B1": "전세", "B2": "월세", "B3": "단기임대"}.get(value, value)


def naver_kind(value: str) -> str:
    return {"SG": "상가", "SMS": "사무실", "GM": "건물", "TJ": "토지", "APT": "아파트", "OPST": "오피스텔"}.get(value, value)


def korea_lat_lng(lat: float, lng: float) -> bool:
    return 32 <= lat <= 39.5 and 124 <= lng <= 132.5


def normalize_naver_coordinate(first: Any, second: Any) -> tuple[str, str]:
    try:
        a = float(str(first).replace(",", "").strip())
        b = float(str(second).replace(",", "").strip())
    except (TypeError, ValueError):
        return "", ""
    candidates = [(a, b), (b, a)]
    scaled = []
    for x, y in candidates:
        for div in (1, 10, 100, 1000, 10000, 100000, 1000000, 10000000):
            scaled.append((x / div, y / div))
    for lat, lng in scaled:
        if korea_lat_lng(lat, lng):
            return f"{lat:.7f}", f"{lng:.7f}"
    return "", ""


def extract_naver_coordinate(obj: dict[str, Any]) -> tuple[str, str, str]:
    pairs = [
        (["latitude", "lat", "markerLat", "yLat"], ["longitude", "lng", "lon", "markerLng", "xLng"]),
        (["ycoordinate", "yCoordinate", "ypos", "yPos", "mapY"], ["xcoordinate", "xCoordinate", "xpos", "xPos", "mapX"]),
        (["y"], ["x"]),
    ]
    for lat_keys, lng_keys in pairs:
        lat, lng = normalize_naver_coordinate(readOwn(obj, lat_keys), readOwn(obj, lng_keys))
        if lat and lng:
            return lat, lng, f"{lat},{lng}"

    geocode = naver_read(obj, ["geocode", "geoCode", "coordinate", "coordinates"])
    numbers = re.findall(r"\d+(?:\.\d+)?", geocode)
    if len(numbers) >= 2:
        lat, lng = normalize_naver_coordinate(numbers[0], numbers[1])
        if lat and lng:
            return lat, lng, f"{lat},{lng}"
    return "", "", geocode


def looks_like_naver_article(obj: Any) -> bool:
    if not isinstance(obj, dict):
        return False
    if readOwn(obj, ["articleNo", "atclNo", "articleId"]):
        return True
    return bool(re.search(r"atcl|article|rletTp|tradTp|dealOrWarrant|rentPrc|floorInfo", " ".join(obj.keys()), re.I))


def walk_objects(value: Any, depth: int = 0) -> list[dict[str, Any]]:
    if depth > 10:
        return []
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if looks_like_naver_article(value):
            found.append(value)
        for child in value.values():
            found.extend(walk_objects(child, depth + 1))
    elif isinstance(value, list):
        for child in value:
            found.extend(walk_objects(child, depth + 1))
    return found


def naver_article_row(obj: dict[str, Any], source_url: str) -> dict[str, str]:
    lat, lng, geocode = extract_naver_coordinate(obj)
    article_name = naver_read(obj, ["articleName", "atclNm", "name", "title"])
    feature = naver_read(obj, ["articleFeatureDesc", "atclFetrDesc", "featureDesc", "summary"])
    meta = naver_source_meta(source_url)
    row = {
        "매물번호": naver_read(obj, ["articleNo", "atclNo", "articleId"]),
        "동": naver_read(obj, ["dongName", "emdNm", "cortarName", "lawdNm", "regionName", "address", "roadAddress"]),
        "종류": naver_kind(naver_read(obj, ["realEstateTypeName", "rletTpNm", "realEstateTypeCode", "rletTpCd"])),
        "거래방식": naver_trade(naver_read(obj, ["tradeTypeName", "tradTpNm", "tradeTypeCode", "tradTpCd"])),
        "보증금": naver_read(obj, ["dealOrWarrantPrc", "warrantPrice", "deposit", "price", "prc"]),
        "임대료": naver_read(obj, ["rentPrc", "rentPrice", "monthlyRent", "rent"]),
        "공급면적": naver_read(obj, ["area1", "spc1", "supplyArea"]),
        "전용/임대면적": naver_read(obj, ["area2", "spc2", "exclusiveArea", "leaseArea"]),
        "층": naver_read(obj, ["floorInfo", "flrInfo", "floor"]),
        "방향": naver_read(obj, ["direction", "directionName", "directionBaseTypeName"]),
        "중개사무소": naver_read(obj, ["realtorName", "realtorOfficeName", "rltrNm", "cpName", "brokerageName"]),
        "위도": lat,
        "경도": lng,
        "geocode": geocode,
        "매물명": article_name,
        "요약": feature,
        "원본API": source_url,
    }
    row.update(meta)
    return row


def reverse_geocode_kakao(latitude: str, longitude: str, kakao_key: str) -> dict[str, str]:
    if not kakao_key or not latitude or not longitude:
        return {"도로명주소": "", "지번주소": "", "주소변환상태": "좌표 또는 Kakao 키 없음"}
    data = request_json(
        KAKAO_COORD2ADDRESS_URL,
        {"x": longitude, "y": latitude, "input_coord": "WGS84"},
        {"Authorization": f"KakaoAK {kakao_key}"},
    )
    docs = data.get("documents", [])
    if not docs:
        return {"도로명주소": "", "지번주소": "", "주소변환상태": "주소 없음"}
    doc = docs[0]
    road = doc.get("road_address") or {}
    jibun = doc.get("address") or {}
    return {
        "도로명주소": clean_text(road.get("address_name")),
        "지번주소": clean_text(jibun.get("address_name")),
        "주소변환상태": "성공",
    }


def collect_naver_articles(
    request_text: str,
    cookie_text: str,
    start_page: int,
    end_page: int,
    kakao_key: str,
    convert_address: bool,
) -> pd.DataFrame:
    url, headers = parse_naver_request(request_text)
    if not url:
        raise RuntimeError("네이버 /api/articles URL 또는 cURL을 입력해 주세요.")
    if cookie_text.strip():
        headers["Cookie"] = cookie_text.strip()

    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for page in range(start_page, end_page + 1):
        page_url = url_with_page(url, page)
        data = fetch_naver_json(page_url, headers)
        for obj in walk_objects(data):
            row = naver_article_row(obj, page_url)
            key = row.get("매물번호") or json.dumps(row, ensure_ascii=False, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            if convert_address:
                try:
                    row.update(reverse_geocode_kakao(row.get("위도", ""), row.get("경도", ""), kakao_key))
                except Exception as exc:
                    row.update({"도로명주소": "", "지번주소": "", "주소변환상태": f"실패: {exc}"})
            rows.append(row)
    return pd.DataFrame(rows)


def find_column(df: pd.DataFrame, candidates: list[str]) -> str:
    normalized = {re.sub(r"\s+", "", str(col)).lower(): col for col in df.columns}
    for candidate in candidates:
        key = re.sub(r"\s+", "", candidate).lower()
        if key in normalized:
            return normalized[key]
    for key, col in normalized.items():
        if any(re.sub(r"\s+", "", candidate).lower() in key for candidate in candidates):
            return col
    return ""


def parse_pasted_listing_table(text: str) -> pd.DataFrame:
    raw = text.strip("\ufeff\n\r\t ")
    if not raw:
        return pd.DataFrame()

    for separator in ("\t", ","):
        try:
            df = pd.read_csv(io.StringIO(raw), sep=separator, dtype=str).fillna("")
        except Exception:
            continue
        if len(df.columns) > 1:
            df.columns = [clean_text(col) for col in df.columns]
            return df

    lines = [line for line in raw.splitlines() if line.strip()]
    rows = [re.split(r"\s{2,}|\t", line.strip()) for line in lines]
    if len(rows) >= 2 and len(rows[0]) > 1:
        return pd.DataFrame(rows[1:], columns=[clean_text(col) for col in rows[0]]).fillna("")
    raise RuntimeError("표를 읽지 못했습니다. 엑셀/콘솔에서 헤더 포함 TSV 형식으로 복사해 붙여넣어 주세요.")


def coordinate_from_row(row: pd.Series, lat_col: str, lng_col: str, geocode_col: str) -> tuple[str, str, str]:
    if lat_col and lng_col:
        lat, lng = normalize_naver_coordinate(row.get(lat_col, ""), row.get(lng_col, ""))
        if lat and lng:
            return lat, lng, f"{lat},{lng}"

    geocode = clean_text(row.get(geocode_col, "")) if geocode_col else ""
    numbers = re.findall(r"\d+(?:\.\d+)?", geocode)
    if len(numbers) >= 2:
        lat, lng = normalize_naver_coordinate(numbers[0], numbers[1])
        if lat and lng:
            return lat, lng, f"{lat},{lng}"
    return "", "", geocode


def building_summary_from_address(address: str, juso_key: str, data_key: str) -> dict[str, str]:
    if not address:
        return {"건물명": "", "연면적": "", "층수": "", "준공연도": "", "건축물조회상태": "주소 없음"}
    if not juso_key or not data_key:
        return {"건물명": "", "연면적": "", "층수": "", "준공연도": "", "건축물조회상태": "API 키 없음"}

    try:
        juso = search_juso(address, juso_key)
        if not juso:
            return {"건물명": "", "연면적": "", "층수": "", "준공연도": "", "건축물조회상태": "주소 검색 실패"}
        building = fetch_building_title(juso, data_key)
        if not building:
            return {"건물명": "", "연면적": "", "층수": "", "준공연도": "", "건축물조회상태": "표제부 없음"}
        return {
            "건물명": clean_text(building.get("bldNm")) or "건물명 없음",
            "연면적": format_py(building.get("totArea")),
            "층수": format_floors(building.get("grndFlrCnt"), building.get("ugrndFlrCnt")),
            "준공연도": format_year(clean_text(building.get("useAprDay"))),
            "건축물조회상태": "성공",
        }
    except Exception as exc:
        return {"건물명": "", "연면적": "", "층수": "", "준공연도": "", "건축물조회상태": f"실패: {exc}"}

def enrich_pasted_listings_with_address(text: str, kakao_key: str, juso_key: str = "", data_key: str = "", include_building: bool = True) -> pd.DataFrame:
    df = parse_pasted_listing_table(text)
    if df.empty:
        return df

    lat_col = find_column(df, ["위도", "latitude", "lat", "y"])
    lng_col = find_column(df, ["경도", "longitude", "lng", "lon", "x"])
    geocode_col = find_column(df, ["geocode", "geo", "좌표", "coordinate"])

    if not ((lat_col and lng_col) or geocode_col):
        raise RuntimeError("위도/경도 또는 geocode 열을 찾지 못했습니다. 콘솔 복사 결과에 좌표 열이 포함되어야 합니다.")

    cache: dict[tuple[str, str], dict[str, str]] = {}
    building_cache: dict[str, dict[str, str]] = {}
    records: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        record = row.to_dict()
        lat, lng, geocode = coordinate_from_row(row, lat_col, lng_col, geocode_col)
        record["위도"] = lat
        record["경도"] = lng
        record["geocode"] = geocode

        key = (lat, lng)
        if not lat or not lng:
            record.update({"도로명주소": "", "지번주소": "", "주소변환상태": "좌표 없음"})
        elif key in cache:
            record.update(cache[key])
        else:
            try:
                converted = reverse_geocode_kakao(lat, lng, kakao_key)
            except Exception as exc:
                converted = {"도로명주소": "", "지번주소": "", "주소변환상태": f"실패: {exc}"}
            cache[key] = converted
            record.update(converted)
        if include_building:
            lookup_address = clean_text(record.get("도로명주소")) or clean_text(record.get("지번주소"))
            if lookup_address in building_cache:
                record.update(building_cache[lookup_address])
            else:
                summary = building_summary_from_address(lookup_address, juso_key, data_key)
                building_cache[lookup_address] = summary
                record.update(summary)
        records.append(record)

    return add_duplicate_tags(pd.DataFrame(records))


def normalize_duplicate_floor(value: Any) -> str:
    text = clean_text(value)
    if not text:
        return ""
    return re.sub(r"\s+", "", text)


def add_duplicate_tags(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    result = df.copy()
    floor_col = find_column(result, ["사용층", "층", "floor", "usefloor"])
    road_col = find_column(result, ["도로명주소", "road_address", "roadaddr"])
    jibun_col = find_column(result, ["지번주소", "jibun_address", "jibunaddr"])
    if not floor_col or not (road_col or jibun_col):
        result["중복태그"] = ""
        return result

    seen: set[tuple[str, str]] = set()
    tags: list[str] = []
    for _, row in result.iterrows():
        address = clean_text(row.get(road_col)) if road_col else ""
        if not address and jibun_col:
            address = clean_text(row.get(jibun_col))
        floor = normalize_duplicate_floor(row.get(floor_col))
        if not address or not floor:
            tags.append("")
            continue
        key = (re.sub(r"\s+", "", address), floor)
        if key in seen:
            tags.append("중복")
        else:
            seen.add(key)
            tags.append("")
    result["중복태그"] = tags
    return result

def dataframe_to_xlsx_bytes(df: pd.DataFrame) -> bytes:
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="naver_land")
    return output.getvalue()


def render_naver_land_api_tab(kakao_key: str, juso_key: str, data_key: str) -> None:
    st.subheader("네이버 부동산 매물 주소 변환")
    st.caption("크롬 콘솔에서 엑셀 복사된 매물 표를 붙여넣으면 위도/경도를 도로명주소로 변환해 엑셀 파일로 저장합니다.")

    pasted_text = st.text_area(
        "콘솔/엑셀 복사 결과 붙여넣기",
        height=260,
        placeholder="No\t매물번호\t...\tgeocode\t위도\t경도\n1\t...",
    )
    st.caption("헤더 행이 포함된 TSV/CSV를 붙여넣어 주세요. 열 이름은 `위도`/`경도` 또는 `geocode`를 인식합니다.")
    include_building = st.checkbox("도로명주소로 건물명/연면적/층수/준공연도 조회", value=True)

    if not kakao_key:
        st.warning("Kakao REST API 키가 없어 도로명주소 변환을 할 수 없습니다. Secrets 또는 사이드바에 Kakao 키를 넣어주세요.")
    if include_building and (not juso_key or not data_key):
        st.warning("건물명/연면적/층수/준공연도 조회에는 도로명주소 API 키와 공공데이터포털 키가 필요합니다.")

    if st.button("붙여넣은 매물 주소 변환", type="primary"):
        if not kakao_key:
            st.error("Kakao REST API 키를 먼저 입력해 주세요.")
            return
        try:
            with st.spinner("좌표를 도로명주소로 변환 중..."):
                df = enrich_pasted_listings_with_address(pasted_text, kakao_key, juso_key, data_key, include_building)
        except Exception as exc:
            st.error(str(exc))
            return

        if df.empty:
            st.warning("변환할 데이터가 없습니다.")
            return

        success_count = int((df.get("주소변환상태", pd.Series(dtype=str)) == "성공").sum())
        building_count = int((df.get("건축물조회상태", pd.Series(dtype=str)) == "성공").sum())
        st.success(f"{len(df):,}건 처리 완료 · 주소 변환 성공 {success_count:,}건 · 건축물 조회 성공 {building_count:,}건")
        st.dataframe(df, use_container_width=True, hide_index=True)
        st.download_button(
            "주소 변환 엑셀 다운로드",
            data=dataframe_to_xlsx_bytes(df),
            file_name="naver_land_articles_with_address.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        st.download_button(
            "주소 변환 CSV 다운로드",
            data=df.to_csv(index=False).encode("utf-8-sig"),
            file_name="naver_land_articles_with_address.csv",
            mime="text/csv",
        )

    with st.expander("실험 기능: 네이버 API URL 직접 수집"):
        st.caption("네이버 서버 응답이 느리거나 차단될 수 있어 기본 흐름은 콘솔 복사 결과 붙여넣기입니다.")
        request_text = st.text_area(
            "네이버 /api/articles URL 또는 cURL",
            height=120,
            placeholder="https://new.land.naver.com/api/articles?cortarNo=... 또는 curl 'https://new.land.naver.com/api/articles?...' ...",
            key="naver_api_request_text",
        )
        cookie_text = st.text_area(
            "Cookie 헤더 (필요할 때만)",
            height=70,
            placeholder="Network 요청에 Cookie가 필요할 때만 붙여넣기",
            key="naver_api_cookie_text",
        )
        col1, col2 = st.columns(2)
        with col1:
            start_page = st.number_input("시작 페이지", min_value=1, value=1, step=1, key="naver_api_start_page")
        with col2:
            end_page = st.number_input("끝 페이지", min_value=1, value=1, step=1, key="naver_api_end_page")

        if st.button("API URL 직접 수집", key="naver_api_collect"):
            try:
                with st.spinner("네이버 API 수집 및 주소 변환 중..."):
                    df = collect_naver_articles(
                        request_text=request_text,
                        cookie_text=cookie_text,
                        start_page=int(start_page),
                        end_page=int(end_page),
                        kakao_key=kakao_key,
                        convert_address=bool(kakao_key),
                    )
            except Exception as exc:
                st.error(str(exc))
                return

            if df.empty:
                st.warning("수집된 매물이 없습니다. Network의 /api/articles URL과 page/cortarNo 값을 확인해 주세요.")
                return

            st.success(f"{len(df):,}건 수집 완료")
            st.dataframe(df, use_container_width=True, hide_index=True)
            st.download_button(
                "API 수집 엑셀 다운로드",
                data=dataframe_to_xlsx_bytes(df),
                file_name="naver_land_articles.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )

def lookup(address: str, juso_key: str, data_key: str, kakao_key: str) -> tuple[JusoResult, BuildingResult, pd.DataFrame]:
    juso = search_juso(address, juso_key)
    if not juso:
        raise RuntimeError("주소를 찾지 못했습니다.")

    building = fetch_building_title(juso, data_key)
    if not building:
        raise RuntimeError("건축물대장 표제부를 찾지 못했습니다.")

    floors_df = fetch_floor_outline(juso, data_key)
    try:
        station, walk_time = nearest_subway(juso.road_addr, kakao_key) if kakao_key else ("", "")
    except Exception:
        station, walk_time = "", ""
    result = BuildingResult(
        name=clean_text(building.get("bldNm")) or "건물명 없음",
        address=display_address(juso),
        approval_year=format_year(clean_text(building.get("useAprDay"))),
        floors=format_floors(building.get("grndFlrCnt"), building.get("ugrndFlrCnt")),
        total_area_py=format_py(building.get("totArea")),
        collective_building=format_collective_building(building),
        station=station,
        walk_time=walk_time,
    )
    return juso, result, floors_df


def render_debug(address: str, kakao_key: str) -> None:
    """임시 진단: 좌표 + 랜드마크 후보 검색 결과(이름/거리)를 화면에 표시."""
    st.markdown(f"#### 🔧 디버그: {address}")
    if not kakao_key:
        st.info("Kakao 키가 없어 디버그를 건너뜁니다.")
        return
    point = geocode_kakao(address, kakao_key)
    st.write("좌표 (x=경도, y=위도):", point)
    if not point:
        st.warning("좌표를 못 찾음 → geocode 문제")
        return
    x, y = point
    searches = [
        ("지하철 SW8", "search/category.json", {"category_group_code": "SW8"}),
        ("터미널 (keyword)", "search/keyword.json", {"query": "터미널"}),
        ("기차역 (keyword)", "search/keyword.json", {"query": "기차역"}),
        ("공공기관 PO3", "search/category.json", {"category_group_code": "PO3"}),
        ("백화점 (keyword)", "search/keyword.json", {"query": "백화점"}),
        ("대학교 (keyword)", "search/keyword.json", {"query": "대학교"}),
        ("IC (keyword)", "search/keyword.json", {"query": "IC"}),
    ]
    for label, path, extra in searches:
        params = {"x": x, "y": y, "radius": 3000, "sort": "distance", "size": 5}
        params.update(extra)
        try:
            docs = kakao_local_search(path, params, kakao_key)
            rows = [
                {
                    "place_name": d.get("place_name"),
                    "distance": d.get("distance"),
                    "category": d.get("category_name"),
                }
                for d in docs
            ]
            st.write(f"[{label}] ({len(rows)}건)", rows or "결과 없음")
        except Exception as exc:
            st.write(f"[{label}] 오류:", str(exc))


def render_card(result: BuildingResult) -> None:
    transit_parts = [p for p in (result.station, result.walk_time) if p]
    transit_line = " ".join(transit_parts) if transit_parts else "역 정보 없음"
    st.markdown(
        f"""
        <table class="building-card">
          <tr><td class="card-title">{html_text(result.name)}</td></tr>
          <tr><td class="card-empty"></td></tr>
          <tr><td class="card-row">{html_text(result.address)}</td></tr>
          <tr><td class="card-row">{html_text(transit_line)}</td></tr>
          <tr><td class="card-row">{html_text(result.approval_year)}</td></tr>
          <tr><td class="card-row">{html_text(result.floors)}</td></tr>
          <tr><td class="card-row">{html_text(result.total_area_py)}</td></tr>
        </table>
        """,
        unsafe_allow_html=True,
    )


st.set_page_config(page_title="건축물대장 자동 채우기", layout="wide")

st.markdown(
    """
    <style>
      .block-container { padding-top: 2rem; max-width: 1180px; }
      .building-card {
        width: 325px;
        border: 1px solid #222;
        background: #fff;
        text-align: center;
        font-family: 'Malgun Gothic', sans-serif;
        border-collapse: collapse;
      }
      .building-card td {
        vertical-align: middle;
        text-align: center;
        padding: 7px 8px;
      }
      .card-title {
        background: #22577f;
        color: #fff;
        font-size: 28px;
        font-weight: 700;
        padding: 24px 8px;
      }
      .card-row {
        min-height: 42px;
        border-top: 1px dotted #777;
        font-size: 24px;
        line-height: 1.45;
      }
      .card-empty {
        height: 18px;
        border-top: 1px dotted #777;
      }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("건축물대장 자동 채우기")

if not require_password():
    st.stop()

juso_secret = secret_value("JUSO_API_KEY")
data_secret = secret_value("DATA_GO_API_KEY")
kakao_secret = secret_value("KAKAO_REST_API_KEY")

with st.sidebar:
    st.header("API 키")
    if juso_secret and data_secret:
        st.success("Secrets에 저장된 API 키를 사용합니다.")
        juso_key = juso_secret
        data_key = data_secret
        kakao_key = kakao_secret
    else:
        st.info("Secrets가 없으면 여기서 직접 입력할 수 있습니다.")
        juso_key = st.text_input("도로명주소 API 승인키", value=juso_secret, type="password")
        data_key = st.text_input("공공데이터포털 서비스키", value=data_secret, type="password")
        kakao_key = st.text_input("Kakao REST API 키 (선택)", value=kakao_secret, type="password")
    st.caption("Kakao 키가 없으면 역/도보시간은 비워집니다.")
    debug_mode = st.checkbox("🔧 디버그 표시 (임시)", value=False)

tab_register, tab_naver = st.tabs(["건축물대장", "네이버 부동산 API 수집"])

with tab_register:
    addresses_text = st.text_area(
        "매물 주소",
        height=140,
        placeholder="예: 경기도 수원시 영통구 효원로 400\n여러 건은 줄바꿈으로 입력",
    )
    render_cards = st.checkbox("카드 결과 생성", value=False)

    with st.expander("집합건물 전유부 조회 옵션"):
        st.caption("여러 전유부는 줄바꿈으로 입력하세요. 예: 101동 301호 / 101동,302호 / 303호")
        fetch_all_units = st.checkbox("주소별 전체 전유부 조회", value=False)
        unit_lines_text = st.text_area(
            "여러 전유부 동/호 (선택)",
            height=90,
            placeholder="101동 301호\n101동,302호\n303호",
        )
        unit_dong = st.text_input("동명 단일 입력 (선택)", placeholder="예: 101동 또는 A동")
        unit_ho = st.text_input("호명 단일 입력 (선택)", placeholder="예: 301호")

    run = st.button("조회", type="primary")

    if run:
        addresses = [line.strip() for line in addresses_text.splitlines() if line.strip()]
        if not juso_key or not data_key:
            st.error("도로명주소 API 승인키와 공공데이터포털 서비스키를 입력해 주세요.")
        elif not addresses:
            st.error("주소를 입력해 주세요.")
        else:
            unit_queries = parse_unit_queries(unit_lines_text, unit_dong, unit_ho)
            fetch_units = fetch_all_units or bool(unit_queries)
            queries_to_fetch = [UnitQuery("", "", "전체")] if fetch_all_units else unit_queries
            summary_rows: list[dict[str, str]] = []
            floor_tables: list[pd.DataFrame] = []
            expos_tables: list[pd.DataFrame] = []
            pubuse_tables: list[pd.DataFrame] = []

            first_tab_label = "카드 결과" if render_cards else "요약 결과"
            tab_cards, tab_floors, tab_units = st.tabs([first_tab_label, "층별 정보", "집합건물 전유부"])

            if render_cards:
                with tab_cards:
                    cols = st.columns(3)

            for index, address in enumerate(addresses):
                try:
                    juso, result, floors_df = lookup(address, juso_key, data_key, kakao_key)

                    if render_cards or debug_mode:
                        with tab_cards:
                            if render_cards:
                                with cols[index % 3]:
                                    render_card(result)
                            if debug_mode:
                                render_debug(juso.road_addr, kakao_key)

                    summary_rows.append(
                        {
                            "입력주소": address,
                            "건물명": result.name,
                            "표시주소": result.address,
                            "가까운역": result.station,
                            "도보시간": result.walk_time,
                            "사용승인년도": result.approval_year,
                            "층수": result.floors,
                            "연면적": result.total_area_py,
                            "집합건물여부": result.collective_building,
                        }
                    )

                    if not floors_df.empty:
                        floors_df.insert(0, "입력주소", address)
                        floor_tables.append(floors_df)

                    if fetch_units:
                        for unit_query in queries_to_fetch:
                            expos_df, pubuse_df = fetch_private_unit(juso, data_key, unit_query.dong_nm, unit_query.ho_nm)
                            if not expos_df.empty:
                                expos_df.insert(0, "조회동호", unit_query.label)
                                expos_df.insert(0, "입력주소", address)
                                expos_tables.append(expos_df)
                            if not pubuse_df.empty:
                                pubuse_df.insert(0, "조회동호", unit_query.label)
                                pubuse_df.insert(0, "입력주소", address)
                                pubuse_tables.append(pubuse_df)

                except Exception as exc:
                    st.warning(f"{address}: {exc}")

            if summary_rows:
                summary_df = pd.DataFrame(summary_rows)
                with tab_cards:
                    st.subheader("표 형식 결과")
                    st.dataframe(summary_df, use_container_width=True, hide_index=True)
                    st.download_button(
                        "요약 CSV 다운로드",
                        data=summary_df.to_csv(index=False).encode("utf-8-sig"),
                        file_name="building_register_summary.csv",
                        mime="text/csv",
                    )

            with tab_floors:
                if floor_tables:
                    floor_df = pd.concat(floor_tables, ignore_index=True)
                    st.dataframe(floor_df, use_container_width=True, hide_index=True)
                    st.download_button(
                        "층별 정보 CSV 다운로드",
                        data=floor_df.to_csv(index=False).encode("utf-8-sig"),
                        file_name="building_floor_outline.csv",
                        mime="text/csv",
                    )
                else:
                    st.info("층별 정보가 없습니다.")

            with tab_units:
                if not fetch_units:
                    st.info("전유부 조회가 필요하면 전체 전유부 조회를 선택하거나 동/호를 입력하고 다시 조회하세요.")
                if expos_tables:
                    expos_df = pd.concat(expos_tables, ignore_index=True)
                    st.subheader("전유부")
                    st.dataframe(expos_df, use_container_width=True, hide_index=True)
                    st.download_button(
                        "전유부 CSV 다운로드",
                        data=expos_df.to_csv(index=False).encode("utf-8-sig"),
                        file_name="building_private_unit.csv",
                        mime="text/csv",
                    )
                if pubuse_tables:
                    pubuse_df = pd.concat(pubuse_tables, ignore_index=True)
                    st.subheader("전유공용면적")
                    st.dataframe(pubuse_df, use_container_width=True, hide_index=True)
                    st.download_button(
                        "전유공용면적 CSV 다운로드",
                        data=pubuse_df.to_csv(index=False).encode("utf-8-sig"),
                        file_name="building_private_common_area.csv",
                        mime="text/csv",
                    )
                if fetch_units and not expos_tables and not pubuse_tables:
                    st.info("해당 전유부 정보가 없거나 조회되지 않았습니다.")





with tab_naver:
    render_naver_land_api_tab(kakao_key, juso_key, data_key)
