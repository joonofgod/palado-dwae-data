# 팔아도돼 재료 시세 데이터

[팔아도돼](https://github.com/joonofgod/palado-dwae) 앱이 받아 쓰는 재료 시세 파일(`prices.json`)을 두는 공개 저장소예요.
앱 코드는 비공개 저장소에 있고, 이 저장소에는 공공데이터만 있어요.

- 매주 화요일 오전 10시(한국 시간)에 `.github/workflows/prices.yml`이 `tool/fetch_prices.py`를 실행해 `prices.json`을 갱신해요. Actions 탭에서 Run workflow로 바로 실행할 수도 있어요.
- 품목마다 `history`(최근 약 3개월의 [날짜, 가격])와, KAMIS는 `w1`·`m1`·`y1`(1주·1달·1년 전 가격)도 들어 있어요.
- 매일 새벽 3시에는 `.github/workflows/backfill.yml`이 그래프용 지난 시세(최근 3개월)를 채워요. 다 채우면 아무것도 바꾸지 않아요. 3개월(92일)이 지난 값은 지워요.
- 앱은 `https://raw.githubusercontent.com/joonofgod/palado-dwae-data/main/prices.json`을 받아요.

| 출처 | 무엇 |
|---|---|
| KAMIS (한국농수산식품유통공사, 공공데이터포털 "최근일자 도,소매가격정보 조회") | 농·수산물 전국 소매 평균 (양파, 배추, 고등어 등) |
| 참가격 (한국소비자원, 공공데이터포털 "생필품 가격 정보") | 식품 매장 판매가 (설탕, 식용유, 돼지고기, 계란 등), 매장별 가격의 중간값 |

키는 저장소 Secret `DATA_GO_KR_KEY` 하나(공공데이터포털 일반 인증키)만 써요. 키가 없거나 받기에 실패한 출처는 지난 값을 그대로 둬요.

`tool/fixtures/`는 형식 시험용 예시 응답이에요: `python3 tool/fetch_prices.py --fixture tool/fixtures --out /tmp/p.json`

출처: 한국농수산식품유통공사, 한국소비자원 (공공누리, 공공데이터포털)
