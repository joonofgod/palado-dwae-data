#!/usr/bin/env python3
"""재료 시세를 받아 prices.json 으로 저장합니다 (팔아도돼 앱이 이 파일을 받아 씁니다).

GitHub Actions(.github/workflows/prices.yml)가 매주 실행합니다.
앱에는 API 키를 넣지 않고, 이 스크립트가 만든 파일만 받아 씁니다.

시세 출처 두 곳 (키가 없거나 받기에 실패한 출처는 지난번 값을 그대로 둡니다):
  KAMIS   농·수산물 전국 소매 평균 (양파, 배추, 고등어 ...)
          공공데이터포털 "한국농수산식품유통공사_최근일자 도,소매가격정보 조회"
          환경변수 DATA_GO_KR_KEY (참가격과 같은 키, 활용신청만 따로)
  참가격   한국소비자원 식품 판매가 (설탕, 식용유, 돼지고기, 계란 ...)
          환경변수 DATA_GO_KR_KEY (공공데이터포털 일반 인증키, Decoding 값)
          발급: data.go.kr 에서 "한국소비자원 생필품 가격 정보" 활용신청

사용:
  python3 tool/fetch_prices.py                    # 실제로 받기
  python3 tool/fetch_prices.py --fixture tool/fixtures --out /tmp/p.json
"""

import argparse
import datetime as dt
import json
import os
import statistics
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
OUT = os.path.join(ROOT, "prices.json")
UA = {"User-Agent": "palado-dwae-prices"}


def get(url, params, tries=3):
    """잠깐 끊기는 경우가 있어 세 번까지 다시 시도합니다."""
    req = urllib.request.Request(f"{url}?{urllib.parse.urlencode(params)}", headers=UA)
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=60) as res:
                return res.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            # 하루 호출 한도를 넘긴 것: 다시 해도 소용없으니 바로 알립니다.
            if e.code == 429 or attempt == tries - 1:
                raise
            time.sleep(2 * (attempt + 1))
        except Exception:
            if attempt == tries - 1:
                raise
            time.sleep(2 * (attempt + 1))


def recent_weekdays(today, weekday=None, count=7):
    """오늘 이전의 평일(또는 특정 요일)을 최근 것부터."""
    day = today - dt.timedelta(days=1)
    out = []
    while len(out) < count:
        if day.weekday() < 5 and (weekday is None or day.weekday() == weekday):
            out.append(day)
        day -= dt.timedelta(days=1)
    return out


# ---------------------------------------------------------------- KAMIS

# KAMIS(aT 농산물유통정보) 소매 가격을 공공데이터포털로 받습니다.
# "한국농수산식품유통공사_최근일자 도,소매가격정보 조회" (data.go.kr/data/15156063)
# 자동승인이라 참가격과 같은 DATA_GO_KR_KEY 하나로 됩니다.
KAMIS_API = "https://apis.data.go.kr/B552845/recent/price"
KAMIS_RETAIL = "01"  # 구분코드 01 소매, 02 중도매
KAMIS_FRESH_DAYS = 60  # 품목마다 '최근 조사일'이 달라서, 오래 조사 안 된 품목은 뺍니다.


def number(text):
    text = str(text or "").replace(",", "").strip()
    try:
        value = float(text)
    except ValueError:
        return None  # "-" 등 조사 안 된 값
    return value if value > 0 else None


def kamis_rows(payload):
    """응답 JSON에서 품목 목록."""
    try:
        item = payload["response"]["body"]["items"]["item"]
    except (KeyError, TypeError):
        return []
    if isinstance(item, dict):
        return [item]
    return item if isinstance(item, list) else []


def kamis_unit(row):
    unit = str(row.get("unit") or "").strip()
    unit = {"리터": "L"}.get(unit, unit)
    size = str(row.get("unit_sz") or "").strip() or "1"
    return f"{size}{unit}"


def kamis_kind(row):
    name = str(row.get("item_nm") or "").strip()
    kind = str(row.get("vrty_nm") or "").strip()
    return "" if kind == name else kind


def kamis_convert(rows):
    """소매 가격만, 최근 조사분만, 같은 품목·품종은 '상품' 등급 하나만 남깁니다."""
    dated = []
    for r in rows:
        if str(r.get("se_cd") or "") != KAMIS_RETAIL:
            continue
        try:
            day = dt.datetime.strptime(str(r.get("exmn_ymd")), "%Y%m%d").date()
        except ValueError:
            continue
        dated.append((day, r))
    if not dated:
        return []
    newest = max(d for d, _ in dated)
    best = {}
    for day, r in dated:
        if (newest - day).days > KAMIS_FRESH_DAYS:
            continue
        price = number(r.get("exmn_dd_prc"))
        name = str(r.get("item_nm") or "").strip()
        if not price or not name:
            continue
        kind = kamis_kind(r)
        rank = str(r.get("grd_nm") or "").strip()
        entry = {
            "name": name,
            "kind": kind,
            "rank": rank,
            "unit": kamis_unit(r),
            "price": price,
            "category": str(r.get("ctgry_nm") or "").strip(),
            "source": "KAMIS",
            "date": day.isoformat(),
        }
        # 1주·1달·1년 전 같은 단위 가격 (변동 표시용, 없으면 생략).
        for field, short in (("ww1_bfr_prc", "w1"), ("mm1_bfr_prc", "m1"), ("yy1_bfr_prc", "y1")):
            before = number(r.get(field))
            if before:
                entry[short] = before
        key = (name, kind)
        old = best.get(key)
        if (
            old is None
            or (rank == "상품" and old["rank"] != "상품")
            or (rank == old["rank"] and entry["date"] > old["date"])
        ):
            best[key] = entry
    return list(best.values())


def fetch_kamis(today, fixture):
    key = os.environ.get("DATA_GO_KR_KEY", "")
    if not fixture and not key:
        print("KAMIS: 키가 없어 건너뜁니다 (지난 값 유지).")
        return None
    try:
        if fixture:
            path = os.path.join(fixture, "kamis", "recent.json")
            payload = json.load(open(path, encoding="utf-8"))
        else:
            payload = json.loads(
                get(
                    KAMIS_API,
                    {
                        "serviceKey": key,
                        "pageNo": "1",
                        "numOfRows": "1000",
                        "returnType": "JSON",
                        "cond[se_cd::EQ]": KAMIS_RETAIL,
                    },
                )
            )
        header = payload.get("response", {}).get("header", {})
        if str(header.get("resultCode", "0")) not in ("0", "00"):
            raise RuntimeError(f"{header.get('resultCode')} {header.get('resultMsg')}")
        items = kamis_convert(kamis_rows(payload))
    except Exception as e:
        print(f"KAMIS 받기 실패: {e}", file=sys.stderr)
        return None
    if not items:
        print("KAMIS: 받은 시세가 없습니다 (지난 값 유지).", file=sys.stderr)
        return None
    newest = max(i["date"] for i in items)
    print(f"KAMIS: 최근 조사일 {newest}, 시세 {len(items)}개")
    return items


# ---------------------------------------------------------------- 참가격

# 공공데이터포털 "한국소비자원_생필품 가격 정보_GW" (data.go.kr/data/15158701/openapi.do)
PRICE_GO_KR = "https://apis.data.go.kr/B551919/ProductPriceInfoService"
DIV_UNIT = {"g": "g", "kg": "kg", "ml": "ml", "l": "L", "ea": "개", "pk": "개", "개": "개"}
# 상품 소분류 코드: 0301 신선식품, 0302 가공식품, 0303 생활용품(제외)
FOOD_CLASS_PREFIXES = ("0301", "0302")


def xml_records(text):
    """참가격 XML 응답에서 레코드(자식 태그 → 값)를 모두 꺼냅니다.

    상품·가격 레코드는 모두 goodId 를 가지고 있어서, 감싸는 태그 이름에
    상관없이 goodId 자식이 있는 요소를 레코드로 봅니다.
    """
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        # 상품명 등에 이스케이프되지 않은 & 가 섞여 오는 경우가 있습니다.
        root = ET.fromstring(re.sub(r"&(?!#?\w+;)", "&amp;", text))
    out = []
    for el in root.iter():
        kids = {c.tag: (c.text or "").strip() for c in el}
        if "goodId" in kids:
            out.append(kids)
    return out


def xml_error(text):
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return text[:200]
    for tag in ("returnAuthMsg", "errMsg", "resultMsg", "returnReasonCode"):
        el = root.find(f".//{tag}")
        if el is not None and (el.text or "").strip() not in ("", "NORMAL SERVICE.", "NORMAL_SERVICE"):
            return el.text.strip()
    return None


def chamgagyeok_convert(products, prices, day):
    """상품별로 여러 매장 판매가의 중앙값을 씁니다 (행사가 하나에 흔들리지 않게).

    1+1 행사 가격은 한 개 값이 아니라서 뺍니다.
    """
    by_good = {}
    for p in prices:
        price = number(p.get("goodPrice"))
        if price and p.get("plusoneYn", "N") != "Y":
            by_good.setdefault(p.get("goodId"), []).append(price)
    items = []
    for prod in products:
        gid = prod.get("goodId")
        name = prod.get("goodName", "").strip()
        values = by_good.get(gid)
        total = number(prod.get("goodTotalCnt"))
        unit = DIV_UNIT.get(prod.get("goodTotalDivCode", "").strip().lower())
        if not values or not name or not total or not unit:
            continue
        qty = int(total) if total == int(total) else total
        items.append(
            {
                "name": name,
                "unit": f"{qty}{unit}",
                "price": round(statistics.median(values)),
                "category": "신선식품" if prod.get("goodSmlclsCode", "").startswith("0301") else "가공식품",
                "source": "참가격",
                "date": day.isoformat(),
                "stores": len(values),
            }
        )
    return items


def is_food(prod):
    return prod.get("goodSmlclsCode", "").startswith(FOOD_CLASS_PREFIXES)


def fetch_chamgagyeok(today, fixture):
    key = os.environ.get("DATA_GO_KR_KEY", "")
    if not fixture and not key:
        print("참가격: 키가 없어 건너뜁니다 (지난 값 유지).")
        return None

    def products_xml():
        if fixture:
            return open(os.path.join(fixture, "chamgagyeok", "getProductInfoSvc.xml"), encoding="utf-8").read()
        return get(f"{PRICE_GO_KR}/getProductInfoSvc.do", {"ServiceKey": key})

    def prices_xml(day, good_id):
        if fixture:
            return open(
                os.path.join(fixture, "chamgagyeok", "getProductPriceInfoSvc.xml"), encoding="utf-8"
            ).read()
        # 가격 조회는 상품아이디나 업체아이디 중 하나가 꼭 있어야 합니다.
        return get(
            f"{PRICE_GO_KR}/getProductPriceInfoSvc",
            {"serviceKey": key, "goodInspectDay": day.strftime("%Y%m%d"), "goodId": good_id},
        )

    try:
        text = products_xml()
        products = [p for p in xml_records(text) if is_food(p)]
        if not products:
            print(f"참가격: 상품 목록이 비었습니다: {xml_error(text)}", file=sys.stderr)
            return None

        # 조사일(금요일, 보통 격주)은 자료가 올라온 가장 최근 날을 한 상품으로 찾습니다.
        probe = products[0]["goodId"]
        day = None
        for friday in recent_weekdays(today, weekday=4, count=8):
            if xml_records(prices_xml(friday, probe)):
                day = friday
                break
            if fixture:
                day = friday
                break
        if day is None:
            print("참가격: 최근 8주 안에 올라온 조사 자료가 없습니다 (지난 값 유지).", file=sys.stderr)
            return None

        prices = []
        if fixture:
            prices = xml_records(prices_xml(day, None))
        else:
            for i, prod in enumerate(products):
                try:
                    prices += xml_records(prices_xml(day, prod["goodId"]))
                except Exception as e:
                    print(f"참가격 {prod.get('goodName')} 받기 실패: {e}", file=sys.stderr)
                if i % 50 == 49:
                    print(f"참가격: {i + 1}/{len(products)} 상품")
        items = chamgagyeok_convert(products, prices, day)
        print(f"참가격: {day} 조사분, 식품 {len(items)}개 (가격 {len(prices)}건)")
        return items or None
    except Exception as e:
        print(f"참가격 받기 실패: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------- 변동 이력

HISTORY_DAYS = 92  # 최근 3개월만 두고, 그보다 오래된 값은 지웁니다.


def item_key(i):
    return (i.get("source", "KAMIS"), i["name"], i.get("kind", ""), i.get("unit", ""))


def add_history(items, old_items):
    """지난번 파일의 이력에 이번 가격을 붙입니다. [[날짜, 가격], ...] 오래된 것부터."""
    old = {item_key(i): i.get("history") or [] for i in old_items}
    for i in items:
        date = i.get("date")
        hist = [list(h) for h in old.get(item_key(i), []) if isinstance(h, list) and len(h) == 2]
        if not hist and date:
            # 처음 받는 품목은 KAMIS가 주는 1달·1주 전 가격으로 시작점을 채웁니다.
            day = dt.date.fromisoformat(date)
            for back, field in ((30, "m1"), (7, "w1")):
                if i.get(field):
                    hist.append([(day - dt.timedelta(days=back)).isoformat(), i[field]])
        if date and i.get("price"):
            hist = [h for h in hist if h[0] != date] + [[date, i["price"]]]
        hist.sort(key=lambda h: h[0])
        if hist:
            i["history"] = hist
    trim_history(items)


def trim_history(items):
    """품목 조사일 기준 3개월(HISTORY_DAYS)이 지난 값은 지웁니다."""
    for i in items:
        hist = i.get("history")
        if not hist or not i.get("date"):
            continue
        cutoff = (dt.date.fromisoformat(i["date"]) - dt.timedelta(days=HISTORY_DAYS)).isoformat()
        i["history"] = [h for h in hist if h[0] >= cutoff]
        if not i["history"]:
            del i["history"]


# ---------------------------------------------------------------- 지난 시세 채우기

# 공공데이터포털 "한국농수산식품유통공사_가격 추이 정보 조회" (data.go.kr/data/15156069)
# 조사일 하나를 넣으면 그날과 1~4주 전 평균 가격을 함께 줍니다.
KAMIS_TREND_API = "https://apis.data.go.kr/B552845/priceSequel/info"
TREND_FIELDS = (
    (0, "exmn_dd_avg_prc"),
    (7, "ww1_bfr_avg_prc"),
    (14, "ww2_bfr_avg_prc"),
    (21, "ww3_bfr_avg_prc"),
    (28, "ww4_bfr_avg_prc"),
)


def merge_points(items, points, near_days=0):
    """points: {item_key: [[날짜, 가격], ...]} 를 각 품목 history 에 더합니다.

    이미 있는 날짜는 그대로 둡니다. [near_days]가 있으면 새 점과 그 일수 안으로
    가까운 기존 점(1주·1달 전 값으로 대신 채워 둔 점)은 새 점으로 바꿉니다.
    """
    added = 0
    for i in items:
        new = points.get(item_key(i))
        if not new:
            continue
        hist = [list(h) for h in i.get("history") or []]
        if near_days:
            new_days = [dt.date.fromisoformat(d) for d, _ in new]
            hist = [
                h
                for h in hist
                if h[0] == i.get("date")
                or all(abs((dt.date.fromisoformat(h[0]) - nd).days) > near_days for nd in new_days)
            ]
        have = {h[0] for h in hist}
        for d, v in new:
            if d not in have:
                hist.append([d, v])
                have.add(d)
                added += 1
        hist.sort(key=lambda h: h[0])
        i["history"] = hist
    return added


def backfill_kamis(items, key, fixture):
    """가격 추이 API로 KAMIS 품목의 지난 주별 가격을 채웁니다. 몇 번만 부르면 됩니다."""
    kamis = [i for i in items if i.get("source") == "KAMIS" and i.get("date")]
    if not kamis:
        return 0
    newest = max(dt.date.fromisoformat(i["date"]) for i in kamis)
    points = {}
    # 한 번에 5주치(그날 + 1~4주 전)가 오므로 5주 간격으로 세 번 부르면 약 3개월입니다.
    for back in (0, 35, 70):
        day = newest - dt.timedelta(days=back)
        try:
            if fixture:
                path = os.path.join(fixture, "kamis", "trend.json")
                payload = json.load(open(path, encoding="utf-8"))
            else:
                payload = json.loads(
                    get(
                        KAMIS_TREND_API,
                        {
                            "serviceKey": key,
                            "pageNo": "1",
                            "numOfRows": "1000",
                            "returnType": "JSON",
                            "cond[exmn_ymd::EQ]": day.strftime("%Y%m%d"),
                            "cond[se_cd::EQ]": KAMIS_RETAIL,
                        },
                    )
                )
        except Exception as e:
            print(f"KAMIS 추이 {day} 받기 실패: {e}", file=sys.stderr)
            continue
        rows = kamis_rows(payload)
        if rows and back == 0:
            print("KAMIS 추이 항목:", ", ".join(sorted(rows[0].keys())))
        for r in rows:
            if str(r.get("se_cd") or KAMIS_RETAIL) != KAMIS_RETAIL:
                continue
            name = str(r.get("item_nm") or "").strip()
            k = ("KAMIS", name, kamis_kind(r), kamis_unit(r))
            for days, field in TREND_FIELDS:
                v = number(r.get(field))
                if v:
                    d = (day - dt.timedelta(days=days)).isoformat()
                    points.setdefault(k, []).append([d, v])
        if fixture:
            break
    n = merge_points(items, points, near_days=3)
    print(f"KAMIS 지난 시세: {n}개 점 추가")
    return n


def backfill_chamgagyeok(items, key, today, fixture, max_days):
    """참가격 지난 조사일 가격을 하루 한도(2,000건) 안에서 [max_days]회차씩 채웁니다."""
    cham = [i for i in items if i.get("source") == "참가격"]
    if not cham or max_days <= 0:
        return 0
    have = {}
    for i in cham:
        for d, _ in i.get("history") or []:
            have[d] = have.get(d, 0) + 1
    # 품목 절반 넘게 들어 있는 날짜는 이미 채운 회차로 봅니다.
    done = {d for d, n in have.items() if n * 2 >= len(cham)}
    cutoff = today - dt.timedelta(days=HISTORY_DAYS)
    fridays = [d for d in recent_weekdays(today, weekday=4, count=14) if d >= cutoff]
    todo = [f for f in fridays if f.isoformat() not in done]
    if not todo:
        print("참가격 지난 시세: 이미 다 채웠어요.")
        return 0

    def call(svc, params):
        if fixture:
            name = "getProductInfoSvc.xml" if "Info" in svc and "Price" not in svc else "getProductPriceInfoSvc.xml"
            return open(os.path.join(fixture, "chamgagyeok", name), encoding="utf-8").read()
        return get(f"{PRICE_GO_KR}/{svc}", params)

    try:
        products = [p for p in xml_records(call("getProductInfoSvc.do", {"ServiceKey": key})) if is_food(p)]
    except Exception as e:
        print(f"참가격 상품 목록 받기 실패: {e}", file=sys.stderr)
        return 0
    if not products:
        return 0
    probe = products[0]["goodId"]
    added = 0
    filled = 0
    for day in todo:
        if filled >= max_days:
            break
        params = {"serviceKey": key, "goodInspectDay": day.strftime("%Y%m%d")}
        prices = []
        stop = False
        try:
            if not fixture and not xml_records(call("getProductPriceInfoSvc", {**params, "goodId": probe})):
                continue  # 조사가 없던 금요일 (격주)
            if fixture:
                prices = xml_records(call("getProductPriceInfoSvc", params))
            else:
                for prod in products:
                    try:
                        prices += xml_records(call("getProductPriceInfoSvc", {**params, "goodId": prod["goodId"]}))
                    except urllib.error.HTTPError as e:
                        if e.code == 429:
                            raise
                        print(f"참가격 {day} {prod.get('goodName')} 실패: {e}", file=sys.stderr)
                    except Exception as e:
                        print(f"참가격 {day} {prod.get('goodName')} 실패: {e}", file=sys.stderr)
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise
            print("참가격 오늘 호출 한도를 다 써서 여기까지 하고 내일 이어서 받아요.")
            stop = True
        except Exception as e:
            print(f"참가격 {day} 받기 실패: {e}", file=sys.stderr)
            continue
        if prices:
            past = chamgagyeok_convert(products, prices, day)
            points = {item_key(i): [[i["date"], i["price"]]] for i in past}
            n = merge_points(items, points)
            print(f"참가격 지난 시세: {day} 조사분 {n}개 점 추가")
            added += n
        filled += 1
        if stop:
            break
    return added


def backfill(path, today, fixture, max_cham_days):
    try:
        data = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        print("시세 파일이 없어 채울 수 없습니다.", file=sys.stderr)
        return 1
    key = os.environ.get("DATA_GO_KR_KEY", "")
    if not fixture and not key:
        print("키가 없어 건너뜁니다.")
        return 0
    items = data.get("items", [])
    added = backfill_kamis(items, key, fixture)
    added += backfill_chamgagyeok(items, key, today, fixture, max_cham_days)
    trim_history(items)
    if not added:
        print("새로 채운 값이 없어 파일을 바꾸지 않습니다.")
        return 0
    write(path, data)
    print(f"저장: 지난 시세 {added}개 점 추가 → {path}")
    return 0


# ---------------------------------------------------------------- 저장


def write(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.write("\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture", help="실제 API 대신 읽을 예시 응답 폴더")
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--today", help="기준일 YYYY-MM-DD (시험용)")
    ap.add_argument(
        "--backfill",
        type=int,
        metavar="N",
        help="새로 받지 않고, 지난 3개월 시세를 채웁니다 (참가격은 한 번에 N회차까지)",
    )
    args = ap.parse_args()
    today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()
    if args.backfill is not None:
        return backfill(args.out, today, args.fixture, args.backfill)

    try:
        old = json.load(open(args.out, encoding="utf-8"))
    except (OSError, ValueError):
        old = {}
    old_items = old.get("items", []) if isinstance(old, dict) else []

    def previous(source):
        return [i for i in old_items if i.get("source", "KAMIS") == source]

    kamis = fetch_kamis(today, args.fixture)
    cham = fetch_chamgagyeok(today, args.fixture)
    items = (kamis if kamis is not None else previous("KAMIS")) + (
        cham if cham is not None else previous("참가격")
    )
    if kamis is None and cham is None:
        print("새로 받은 시세가 없어 파일을 바꾸지 않습니다.")
        return 0

    add_history(items, old_items)
    items.sort(key=lambda i: (i.get("source", ""), i.get("category", ""), i["name"], i.get("kind", "")))
    dates = sorted({i["date"] for i in items if i.get("date")})
    out = {
        "source": "KAMIS·참가격",
        "date": dates[-1] if dates else None,
        "items": items,
    }
    write(args.out, out)
    print(f"저장: 시세 {len(items)}개 → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
