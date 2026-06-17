# 건축물대장 자동 채우기 Streamlit 배포본

주소를 입력하면 도로명주소 API와 건축물대장 표제부 API로 아래 항목을 채웁니다.

- 건물명
- 표시 주소
- 가까운 지하철역
- 도보시간
- 사용승인년도
- 층수
- 연면적(py)
- 층별 정보
- 집합건물 전유부 및 전유공용면적

## Streamlit Community Cloud 배포

1. 이 폴더의 파일 3개를 GitHub 저장소에 올립니다.
   - `streamlit_app.py`
   - `requirements.txt`
   - `secrets.toml.example`
2. Streamlit Community Cloud에서 `Create app`을 누릅니다.
3. GitHub 저장소, 브랜치, 메인 파일 `streamlit_app.py`를 선택합니다.
4. Advanced settings의 Secrets에 아래 형식으로 입력합니다.

```toml
JUSO_API_KEY = "도로명주소 API 승인키"
DATA_GO_API_KEY = "공공데이터포털 서비스키"
KAKAO_REST_API_KEY = "Kakao REST API 키, 없으면 빈 문자열"
APP_PASSWORD = "회사에서 사용할 접속 비밀번호"
```

5. Deploy를 누릅니다.

## 주의

- `secrets.toml.example`은 예시 파일입니다. 실제 API 키를 GitHub에 올리지 마세요.
- Kakao 키가 없으면 지하철역과 도보시간은 빈칸으로 표시됩니다.
- 앱 URL을 아는 사람이 접근할 수 있으므로 `APP_PASSWORD`를 꼭 설정하세요.
- 집합건물 전유부는 동명 또는 호명을 입력했을 때 조회합니다. 주소만 입력하면 해당 지번의 전체 전유부가 너무 많을 수 있습니다.
