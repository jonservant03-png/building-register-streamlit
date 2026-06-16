import re
from dataclasses import dataclass
from typing import Any

import pandas as pd
import requests
import streamlit as st


PY_PER_SQM = 1 / 3.305785
KAKAO_SUBWAY_CATEGORY = "SW8"


@dataclass
class JusoResult:
    road_addr: str
    jibun_addr: str
    bd_mgt_sn: str
    adm_cd: str
    mt_yn: str
    lnbr_mnnm: str
    lnbr_slno: str
    si_nm: str
    sgg_nm: str


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
    if len(digits) >= 4:
        return f"{digits[:4]}년"
    return ""


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
    except ValueError:
        return ""
    if not value:
        return ""
    return f"{round(value):,} py"


def display_address(juso: JusoResult) -> str:
    road = juso.road_addr
    for prefix in ("광주광역시 ", "서울특별시 ", "경기도 ", "수원시 "):
        road = road.replace(prefix, "")
    return road


def request_json(url: str, params: dict[str, Any], headers: dict[str, str] | None = None) -> dict[str, Any]:
    response = requests.get(url, params=params, headers=headers, timeout=15)
    response.raise_for_status()
    return response.json()


def search_juso(keyword: str, juso_key: str) -> JusoResult | None:
    data = request_json(
        "https://business.juso.go.kr/addrlink/addrLinkApi.do",
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
        si_nm=clean_text(item.get("siNm")),
        sgg_nm=clean_text(item.get("sggNm")),
    )


def normalize_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    body = payload.get("response", {}).get("body", {})
    items = body.get("items", {})
    item = items.get("item", []) if isinstance(items, dict) else []
    if isinstance(item, dict):
        return [item]
    return item if isinstance(item, list) else []


def fetch_building_title(juso: JusoResult, data_key: str) -> dict[str, Any] | None:
    if len(juso.adm_cd) < 10:
        return None

    sigungu_cd = juso.adm_cd[:5]
    bjdong_cd = juso.adm_cd[5:10]
    plat_gb_cd = "1" if juso.mt_yn == "1" else "0"
    bun = only_digits(juso.lnbr_mnnm)
    ji = only_digits(juso.lnbr_slno)

    data = request_json(
        "https://apis.data.go.kr/1613000/BldRgstService_v2/getBrTitleInfo",
        {
            "serviceKey": data_key,
            "sigunguCd": sigungu_cd,
            "bjdongCd": bjdong_cd,
            "platGbCd": plat_gb_cd,
            "bun": bun,
            "ji": ji,
            "numOfRows": 20,
            "pageNo": 1,
            "_type": "json",
        },
    )
    items = normalize_items(data)
    if not items:
        return None

    def score(item: dict[str, Any]) -> tuple[int, float]:
        name_score = 1 if clean_text(item.get("bldNm")) else 0
        try:
            area = float(item.get("totArea") or 0)
        except ValueError:
            area = 0
        return name_score, area

    return sorted(items, key=score, reverse=True)[0]


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


def nearest_subway(address: str, kakao_key: str) -> tuple[str, str]:
    if not kakao_key:
        return "", ""
    point = geocode_kakao(address, kakao_key)
    if not point:
        return "", ""

    x, y = point
    data = request_json(
        "https://dapi.kakao.com/v2/local/search/category.json",
        {
            "category_group_code": KAKAO_SUBWAY_CATEGORY,
            "x": x,
            "y": y,
            "radius": 2000,
            "sort": "distance",
            "size": 1,
        },
        {"Authorization": f"KakaoAK {kakao_key}"},
    )
    docs = data.get("documents", [])
    if not docs:
        return "", ""
    station = docs[0].get("place_name", "")
    distance_m = int(docs[0].get("distance") or 0)
    minutes = max(1, round(distance_m / 67))
    return station, f"도보 {minutes}분"


def lookup(address: str, juso_key: str, data_key: str, kakao_key: str) -> BuildingResult:
    juso = search_juso(address, juso_key)
    if not juso:
        raise RuntimeError("주소를 찾지 못했습니다.")

    building = fetch_building_title(juso, data_key)
    if not building:
        raise RuntimeError("건축물대장 표제부를 찾지 못했습니다.")

    station, walk_time = nearest_subway(juso.road_addr, kakao_key) if kakao_key else ("", "")
    return BuildingResult(
        name=clean_text(building.get("bldNm")) or "건물명 없음",
        address=display_address(juso),
        approval_year=format_year(clean_text(building.get("useAprDay"))),
        floors=format_floors(building.get("grndFlrCnt"), building.get("ugrndFlrCnt")),
        total_area_py=format_py(building.get("totArea")),
        station=station,
        walk_time=walk_time,
    )


def render_card(result: BuildingResult) -> None:
    station_line = result.station or "역 정보 없음"
    walk_line = result.walk_time or "도보시간 없음"
    st.markdown(
        f"""
        <div class="building-card">
          <div class="card-title">{result.name}</div>
          <div class="card-row">{result.address}</div>
          <div class="card-row">{station_line}<br>{walk_line}</div>
          <div class="card-row">{result.approval_year}</div>
          <div class="card-row">{result.floors}</div>
          <div class="card-row">{result.total_area_py}</div>
        </div>
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
        display: flex;
        align-items: center;
        justify-content: center;
        padding: 7px 8px;
        font-size: 24px;
        line-height: 1.45;
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
    st.caption("Kakao 키가 없으면 역/도보시간은 비워둡니다.")

addresses_text = st.text_area(
    "매물 주소",
    height=140,
    placeholder="예: 경기도 수원시 영통구 효원로 400\n여러 건은 줄바꿈으로 입력",
)

run = st.button("조회", type="primary")

if run:
    addresses = [line.strip() for line in addresses_text.splitlines() if line.strip()]
    if not juso_key or not data_key:
        st.error("도로명주소 API 승인키와 공공데이터포털 서비스키를 입력해 주세요.")
    elif not addresses:
        st.error("주소를 입력해 주세요.")
    else:
        results: list[dict[str, str]] = []
        cols = st.columns(3)
        for index, address in enumerate(addresses):
            try:
                result = lookup(address, juso_key, data_key, kakao_key)
                with cols[index % 3]:
                    render_card(result)
                results.append(
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
            except Exception as exc:
                st.warning(f"{address}: {exc}")

        if results:
            df = pd.DataFrame(results)
            st.subheader("표 형식 결과")
            st.dataframe(df, use_container_width=True, hide_index=True)
            st.download_button(
                "CSV 다운로드",
                data=df.to_csv(index=False).encode("utf-8-sig"),
                file_name="building_register_results.csv",
                mime="text/csv",
            )
