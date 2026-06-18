import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

import pandas as pd
import requests
import streamlit as st


PY_PER_SQM = 1 / 3.305785
BUILDING_API_BASE = "https://apis.data.go.kr/1613000/BldRgstHubService"
JUSO_API_URL = "https://business.juso.go.kr/addrlink/addrLinkApi.do"
KAKAO_SUBWAY_CATEGORY = "SW8"


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
    station: str = ""
    walk_time: str = ""


def clean_text(value: Any) -> str:
    return str(value or "").strip()


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


def area_py_value(area_sqm: Any) -> str:
    try:
        value = float(area_sqm or 0) * PY_PER_SQM
    except (TypeError, ValueError):
        return ""
    return f"{value:.1f}" if value else ""


def display_address(juso: JusoResult) -> str:
    road = juso.road_addr
    prefixes = [
        "광주광역시 ",
        "서울특별시 ",
        "경기도 ",
        "수원시 ",
        "부산광역시 ",
        "대구광역시 ",
        "인천광역시 ",
        "대전광역시 ",
        "울산광역시 ",
        "세종특별자치시 ",
    ]
    for prefix in prefixes:
        road = road.replace(prefix, "")
    return road


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

    expos_records = []
    for row in expos_rows:
        area_sqm = row.get("area") or row.get("exposArea")
        expos_records.append(
            {
                "동명": clean_text(row.get("dongNm")),
                "호명": clean_text(row.get("hoNm")),
                "층": clean_text(row.get("flrNoNm")) or clean_text(row.get("flrNo")),
                "전유면적㎡": clean_text(area_sqm),
                "전유면적py": area_py_value(area_sqm),
                "용도": clean_text(row.get("mainPurpsCdNm")),
            }
        )

    pubuse_records = []
    for row in pubuse_rows:
        area_sqm = row.get("area")
        pubuse_records.append(
            {
                "동명": clean_text(row.get("dongNm")),
                "호명": clean_text(row.get("hoNm")),
                "구분": clean_text(row.get("exposPubuseGbCdNm")),
                "공용면적㎡": clean_text(area_sqm),
                "공용면적py": area_py_value(area_sqm),
            }
        )

    return pd.DataFrame(expos_records), pd.DataFrame(pubuse_records)


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


def find_nearest_landmark(x: float, y: float, kakao_key: str) -> str:
    """교통시설(터미널/기차역) > 관공서 > 공원·랜드마크 순으로 가장 가까운 시설명."""

    def nearest_keyword(keywords: list[str]) -> str:
        best_name, best_dist = "", None
        for kw in keywords:
            docs = kakao_local_search(
                "search/keyword.json",
                {"query": kw, "x": x, "y": y, "radius": 5000, "sort": "distance", "size": 1},
                kakao_key,
            )
            if docs:
                dist = int(docs[0].get("distance") or 0)
                if best_dist is None or dist < best_dist:
                    best_name, best_dist = clean_text(docs[0].get("place_name")), dist
        return best_name

    def nearest_category(code: str) -> str:
        docs = kakao_local_search(
            "search/category.json",
            {"category_group_code": code, "x": x, "y": y, "radius": 5000, "sort": "distance", "size": 1},
            kakao_key,
        )
        return clean_text(docs[0].get("place_name")) if docs else ""

    # 1) 교통시설
    name = nearest_keyword(["터미널", "기차역"])
    if name:
        return name
    # 2) 관공서(공공기관)
    name = nearest_category("PO3")
    if name:
        return name
    # 3) 공원·랜드마크(관광명소)
    return nearest_category("AT4")


def nearest_subway(address: str, kakao_key: str) -> tuple[str, str]:
    if not kakao_key:
        return "", ""
    point = geocode_kakao(address, kakao_key)
    if not point:
        return "", ""

    x, y = point
    subway = find_nearest_subway(x, y, kakao_key)
    if subway:
        place_name, distance_m = subway
        minutes = max(1, round(distance_m / 67))
        if minutes <= WALK_MINUTE_THRESHOLD:
            return format_station_name(place_name), f"도보 {minutes}분"

    landmark = find_nearest_landmark(x, y, kakao_key)
    if landmark:
        return f"{landmark} 인근", ""

    if subway:
        place_name, distance_m = subway
        minutes = max(1, round(distance_m / 67))
        return format_station_name(place_name), f"도보 {minutes}분"
    return "", ""


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
        station=station,
        walk_time=walk_time,
    )
    return juso, result, floors_df


def render_card(result: BuildingResult) -> None:
    transit_parts = [p for p in (result.station, result.walk_time) if p]
    transit_line = " ".join(transit_parts) if transit_parts else "역 정보 없음"
    st.markdown(
        f"""
        <table class="building-card">
          <tr><td class="card-title">{result.name}</td></tr>
          <tr><td class="card-empty"></td></tr>
          <tr><td class="card-row">{result.address}</td></tr>
          <tr><td class="card-row">{transit_line}</td></tr>
          <tr><td class="card-row">{result.approval_year}</td></tr>
          <tr><td class="card-row">{result.floors}</td></tr>
          <tr><td class="card-row">{result.total_area_py}</td></tr>
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

addresses_text = st.text_area(
    "매물 주소",
    height=140,
    placeholder="예: 경기도 수원시 영통구 효원로 400\n여러 건은 줄바꿈으로 입력",
)

with st.expander("집합건물 전유부 조회 옵션"):
    st.caption("집합건물은 주소만으로 전체 전유부가 여럿 나올 수 있습니다. 특정 호실이면 동명/호명을 입력하세요.")
    unit_dong = st.text_input("동명 (선택)", placeholder="예: 101동 또는 A동")
    unit_ho = st.text_input("호명 (선택)", placeholder="예: 301호")

run = st.button("조회", type="primary")

if run:
    addresses = [line.strip() for line in addresses_text.splitlines() if line.strip()]
    if not juso_key or not data_key:
        st.error("도로명주소 API 승인키와 공공데이터포털 서비스키를 입력해 주세요.")
    elif not addresses:
        st.error("주소를 입력해 주세요.")
    else:
        summary_rows: list[dict[str, str]] = []
        floor_tables: list[pd.DataFrame] = []
        expos_tables: list[pd.DataFrame] = []
        pubuse_tables: list[pd.DataFrame] = []

        tab_cards, tab_floors, tab_units = st.tabs(["카드 결과", "층별 정보", "집합건물 전유부"])

        with tab_cards:
            cols = st.columns(3)

        for index, address in enumerate(addresses):
            try:
                juso, result, floors_df = lookup(address, juso_key, data_key, kakao_key)

                with tab_cards:
                    with cols[index % 3]:
                        render_card(result)

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
                    }
                )

                if not floors_df.empty:
                    floors_df.insert(0, "입력주소", address)
                    floor_tables.append(floors_df)

                if unit_dong or unit_ho:
                    expos_df, pubuse_df = fetch_private_unit(juso, data_key, unit_dong, unit_ho)
                    if not expos_df.empty:
                        expos_df.insert(0, "입력주소", address)
                        expos_tables.append(expos_df)
                    if not pubuse_df.empty:
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
            if not (unit_dong or unit_ho):
                st.info("전유부 조회가 필요하면 동명 또는 호명을 입력하고 다시 조회하세요.")
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
            if (unit_dong or unit_ho) and not expos_tables and not pubuse_tables:
                st.info("해당 동/호 전유부 정보가 없거나 조회되지 않았습니다.")
