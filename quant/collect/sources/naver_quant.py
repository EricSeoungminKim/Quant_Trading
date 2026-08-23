"""네이버 거래상위(sise_quant) → 시장 전체 거래대금 상위 종목. 테마 밖의 대장주를
놓치지 않는 보완 신호 — Task 2 의 select_sources 가 테마 대장주와의 합집합으로 쓴다.

수집 예절·파싱 관례는 naver_sector.py/naver_theme.py 와 같다: _http_get 패턴 재사용,
Referer 지정, 깨진 행은 0 채움 대신 스킵 + 스킵 건수 stderr 로그.

이 페이지는 실측(2026-08-15, `?page=1`과 `?page=2` 응답이 완전히 동일함을 확인)
결과 페이지네이션이 없다 — 항상 거래대금 상위 100종목을 한 번에 보여준다.
`pages` 파라미터는 naver_theme 관례를 따라 열어두지만, 오늘 기준 2페이지 이상
요청해도 같은 데이터가 반복될 뿐이다(코드 기준 dedup 으로 중복 적재는 막는다).

## PER/ROE/시가총액 (2026-08-19, 장타 팩터 전략 전제 데이터)

이 페이지엔 시가총액·PER·ROE 컬럼이 이미 오지만(아래 주석), 오랫동안 파싱만
하고 버려왔다. 실측(2026-08-19 EC2, 실응답)으로 확인: 일반 종목(예: 005930
삼성전자 PER 21.70/ROE 10.85)은 값이 오고, ETF/ETN(레버리지·인버스 등 —
오늘 거래상위 상위권 다수)은 PER·ROE 칸이 항상 문자열 `"N/A"` 다(수익·자기자본
개념이 없는 상품이라 당연하다). `parse_quant_rows`는 `"N/A"`를 **명시적으로
`None`으로 남긴다** — 0으로 채우지 않는다. 시가총액은 관측 범위에서 항상
숫자였다(단위: 억원, 콤마 포함).
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from quant.adapters.http import client

INDEX_URL = "https://finance.naver.com/sise/sise_quant.naver"

_KST = timezone(timedelta(hours=9))

# 행 단위 파싱. <th> 헤더 실측(2026-08-15 EC2): N 종목명 현재가 전일비 등락률
# 거래량 거래대금 매수호가 매도호가 시가총액 PER ROE. class="number" td 는 행당
# 10개, 순서대로 현재가/전일비/등락률/거래량/거래대금/매수호가/매도호가/
# 시가총액/PER/ROE — 등락률=index 2, 거래대금=index 4, 시가총액=index 7,
# PER=index 8, ROE=index 9. 매수호가·매도호가(index 5/6)는 확신은 있으나
# 브리프 범위 밖이라 파싱하지 않는다.
_ROW_RE = re.compile(r"<tr>(.*?)</tr>", re.S)
# 코드는 6자리 숫자 종목뿐 아니라 영숫자 혼합(ETN 등, 예: 0183J0)도 있다 —
# 2026-08-15 실측 100종목 중 19종목이 영숫자 코드였다.
_CODE_NAME_RE = re.compile(
    r'<a href="/item/main\.naver\?code=([0-9A-Za-z]{6})" class="tltle">([^<]+)</a>'
)
_NUMBER_TD_RE = re.compile(r'<td class="number"[^>]*>(.*?)</td>', re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def _parse_optional_number(text: str | None, cast):
    """`"N/A"`/빈 문자열/결측 인덱스는 `None` — 0으로 위장하지 않는다.

    파싱 자체가 실패하면(예상 못 한 형식) 그것도 `None`이지만, 이 경우는
    "결측"과 구분되지 않아 흔치 않은 형식 변화를 조용히 삼킬 수 있다 —
    호출부(`parse_quant_rows`)가 스킵 카운트로 이미 깨진 행 전체를 로그하므로
    개별 필드 단위의 조용한 실패는 여기서는 허용한다(행 자체는 버리지 않는다).
    """
    if not text or text == "N/A":
        return None
    try:
        return cast(text.replace(",", ""))
    except ValueError:
        return None


def parse_quant_rows(html_text: str) -> list[dict]:
    out: list[dict] = []
    total = 0
    for row in _ROW_RE.findall(html_text or ""):
        code_name = _CODE_NAME_RE.search(row)
        if not code_name:
            continue  # 헤더/구분 행 — 애초에 종목 행이 아니다
        total += 1
        numbers = [_TAG_RE.sub("", cell).strip() for cell in _NUMBER_TD_RE.findall(row)]
        if len(numbers) < 5:
            continue  # 예상 컬럼 수 미달 — 깨진 행
        try:
            change_pct = float(numbers[2].replace("%", ""))
            value_traded = int(numbers[4].replace(",", ""))  # 단위: 백만원
        except ValueError:
            continue  # 등락률/거래대금 파싱 실패 — 깨진 행
        code, name = code_name.group(1), code_name.group(2).strip()
        # 시가총액(억원)/PER/ROE — index 7/8/9. 컬럼 자체가 없는(더 깨진) 행도
        # 있을 수 있어 인덱스 존재 여부부터 확인한다.
        market_cap = _parse_optional_number(numbers[7] if len(numbers) > 7 else None, int)
        per = _parse_optional_number(numbers[8] if len(numbers) > 8 else None, float)
        roe = _parse_optional_number(numbers[9] if len(numbers) > 9 else None, float)
        out.append(
            {
                "code": code,
                "name": name,
                "change_pct": change_pct,
                "value_traded": value_traded,
                "market_cap": market_cap,
                "per": per,
                "roe": roe,
            }
        )
    skipped = total - len(out)
    if skipped:
        print(f"거래상위 파싱 {len(out)}/{total}건 (건너뜀 {skipped}건)", file=sys.stderr)
    return out


def _http_get(url: str) -> str | None:
    with client() as c:
        resp = c.get(url, headers={"Referer": "https://finance.naver.com/"})
        resp.raise_for_status()
    return resp.text


def fetch_quant_top(getter=None, pages: int = 1) -> list[dict]:
    get = getter or _http_get
    seen: set[str] = set()
    out: list[dict] = []
    for page in range(1, pages + 1):
        url = INDEX_URL if page == 1 else f"{INDEX_URL}?page={page}"
        try:
            html = get(url)
        except Exception:  # noqa: BLE001 — 페이지 하나 실패, 여기까지 모은 걸로 종료
            break
        for row in parse_quant_rows(html or ""):
            if row["code"] in seen:
                continue
            seen.add(row["code"])
            out.append(row)
        if page < pages:
            time.sleep(0.3)
    return out


def append_ledger(rows: list[dict], path: Path) -> int:
    """`(date, code)` 기준 중복 제거 후 append. 반환값은 실제로 추가된 건수.

    `dart.append_ledger`/`naver_research.append_ledger` 와 같은 원자적-append
    패턴이다. 하루 여러 번 돌려도 그날의 **첫 관측값**만 남는다(장중 변동으로
    PER/시가총액이 계속 바뀌므로, 같은 날 재적재로 뒤 값이 앞 값을 덮어쓰지
    않게 한다) — 팩터 백테스트는 "그날 어떤 값을 볼 수 있었나"가 중요하지
    "장중 몇 시 값이었나"는 이 원장의 관심사가 아니다.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    seen: set[tuple] = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            seen.add((row.get("date"), row.get("code")))

    to_write: list[dict] = []
    for row in rows:
        key = (row.get("date"), row.get("code"))
        if key in seen:
            continue
        seen.add(key)
        to_write.append(row)

    if to_write:
        with path.open("a", encoding="utf-8") as f:
            for row in to_write:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    return len(to_write)


def fetch_and_persist(root: Path, getter=None, now: datetime | None = None) -> dict:
    """거래상위 스냅샷을 수집해 펀더멘털(시가총액/PER/ROE) 원장에 적재한다.

    관측일은 KST 기준 날짜로 기록한다(리포트 발행·다른 원장들과 같은 관례) —
    이게 없으면 나중에 "그 값을 언제 알 수 있었나"를 복원할 수 없어
    look-ahead를 피할 수 없다. 네트워크 실패는 여기서 삼키지 않는다(호출부가
    원장 원칙대로 "실패"와 "빈 원장"을 구분해야 한다) — `fetch_quant_top`
    자체가 이미 페이지 실패를 빈 리스트로 흡수하므로 여기선 그 결과를 그대로
    기록할 뿐이다.
    """
    now = now or datetime.now(timezone.utc)
    observed = now.astimezone(_KST).date()
    rows = fetch_quant_top(getter=getter)
    ledger_rows = [
        {**row, "date": observed.isoformat(), "source": "naver_quant"} for row in rows
    ]
    path = root / "data" / "ledger" / "fundamentals_naver.jsonl"
    added = append_ledger(ledger_rows, path)
    return {"fetched": len(rows), "added": added, "date": observed.isoformat()}
