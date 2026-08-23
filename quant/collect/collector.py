"""뉴스 누적 수집기 — 리포트 빌드에서 뉴스 수집을 떼어낸다.

## 왜 분리하나 (2026-08-13 실측)

RSS 는 **"최신 N건" 스냅샷**이다. 상한을 300 으로 올려도 investing.com 은 항상
10건만 준다 — 그건 그쪽 페이지 크기다. 문제는 그 창이 계속 굴러간다는 것:

    02:20 → 11:20 (9시간) 재수집 결과
    Investing_Com      겹침 0/10   전량 교체
    SeekingAlpha       겹침 0/7    전량 교체
    Bloomberg_Markets  겹침 0/10   전량 교체
    Yahoo_Finance      겹침 0/10   전량 교체
    Motley_Fool        겹침 1/50   49건 회전
    → 한 번 더 받았을 뿐인데 새 기사 153건 (당시 보유 67건)

즉 08:00 에 한 번 긁으면 **전날 20:00 이후 나왔다가 창 밖으로 밀려난 기사는
영구히 사라진다.** 천천히 받는다고 해결되지 않는다(레이트 리밋 문제가 아니다).
**여러 번 받아 쌓아야** 한다.

## 저장 구조 — JSONL 이 진실, md 는 뷰

    data/news/{market}/YYYY-MM-DD.jsonl   ← 기계가 읽는 원본 (append-only)
    data/vault/news/{market}/YYYY-MM-DD.md ← 사람·AI 가 읽는 뷰 (매번 재생성)

스냅샷=JSON / HTML=뷰 와 같은 패턴이다. md 를 파싱해 되읽지 않는다 — 마크다운
파싱은 깨지기 쉽고, 그 깨짐이 조용하다.

## 중복은 버리는 게 아니라 세는 것

같은 기사가 여러 매체·여러 수집 회차에 나오는 것은 **잡음이 아니라 신호**다
(사용자 지시 2026-08-13). 링크로 중복을 판정하되 버리지 않고 `seen_count` 와
`first_seen`/`last_seen` 을 올린다 — "3시간째 계속 재보도되는 기사"와 "한 번
나오고 만 기사"는 다른 사건이다.
"""
from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from quant.collect.sources.feeds import (
    NEWS_FEEDS,
    fetch_conditional,
    parse_published,
    resolve_outlet,
)

# 보존 기간. 세션 창(전일 마감~개장) 밖 기사는 리포트가 안 쓰지만, 금요일→월요일
# 3일 공백과 "며칠째 재보도" 판정을 위해 여유를 둔다.
RETENTION_DAYS = 7

# 링크에 붙는 추적 파라미터. 같은 기사가 파라미터만 달라 중복으로 세지 않게 벗긴다.
_TRACKING_PARAMS = re.compile(
    r"(?:^|&)(?:utm_[^&]*|oc|ocid|ref|referrer|fbclid|gclid|mc_cid|mc_eid|_gl)=[^&]*"
)


def normalize_link(url: str) -> str:
    """중복 판정용 링크 정규화 — 추적 파라미터·프래그먼트·말미 슬래시 제거.

    호스트는 소문자로 맞추되 **경로 대소문자는 건드리지 않는다** — 경로는
    대소문자를 구분하는 서버가 있어 다른 기사를 같은 것으로 뭉갤 수 있다.
    """
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    query = _TRACKING_PARAMS.sub("", parts.query).lstrip("&")
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, query, ""))


# 시장별 표시 타임존. 볼트는 사람과 AI 가 읽는 뷰라 **현지 시각**으로 보여준다 —
# 저장은 UTC(ISO)로 하되 표시만 바꾼다. 한국 리포트에 "03:00"이 찍히면 그게
# 12:00 인지 03:00 인지 읽는 쪽이 알 수 없다.
_DISPLAY_TZ = {"KR": timezone(timedelta(hours=9)), "US": timezone(timedelta(hours=-4))}


def _local_hhmm(raw: str | None, market: str) -> str:
    """ISO 문자열 → 시장 현지 HH:MM. 못 읽으면 원문 앞부분을 그대로 준다."""
    if not raw:
        return "??:??"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return raw[11:16] or "??:??"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_DISPLAY_TZ.get(market, timezone.utc)).strftime("%H:%M")


def store_path(root: Path, market: str, day: date) -> Path:
    return root / "data" / "news" / market / f"{day.isoformat()}.jsonl"


def vault_path(root: Path, market: str, day: date) -> Path:
    return root / "data" / "vault" / "news" / market / f"{day.isoformat()}.md"


def load_store(path: Path) -> dict[str, dict]:
    """정규화 링크 → 기사. 깨진 줄은 건너뛴다."""
    out: dict[str, dict] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        key = row.get("key")
        if key:
            out[key] = row
    return out


def feed_headers_cache_path(root: Path) -> Path:
    return root / "data" / "cache" / "feed_headers.json"


def load_feed_headers_cache(path: Path) -> dict:
    """RSS 조건부 GET용 ETag/Last-Modified 캐시를 읽는다.

    **이 캐시는 순수 성능 최적화다 — 정확성은 여기에 기대지 않는다.** 파일이
    없거나 깨졌으면 그냥 빈 dict로 다시 시작한다. 그러면 다음 수집이 조건부
    헤더 없이 전체를 다시 받을 뿐이고(304 최적화 한 번 놓치는 것), 기사가
    새는 일은 없다 — 안전망은 collector의 링크 기반 중복 판정(저장소가
    append-only)이 이미 쥐고 있다.
    """
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}


def save_feed_headers_cache(path: Path, cache: dict) -> None:
    """tmp에 쓰고 os.replace로 갈아치운다 — 쓰다 만 파일이 다음 로드를 깨지 않게."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def collect_once(market: str, root: Path, now: datetime | None = None) -> dict:
    """피드 전체를 한 번 긁어 누적 저장소에 반영한다.

    **append-only 가 아니라 전체 재기록**이다 — `seen_count`/`last_seen` 을
    갱신해야 하므로 한 줄을 고쳐 쓸 수 없다. 하루치라 파일이 작아 문제없다.
    """
    now = now or datetime.now(timezone.utc)
    day = now.astimezone(timezone(timedelta(hours=9))).date()  # KST 기준 하루
    path = store_path(root, market, day)
    store = load_store(path)

    headers_path = feed_headers_cache_path(root)
    headers_cache = load_feed_headers_cache(headers_path)

    stamp = now.isoformat()
    new = dup = 0
    per_feed: dict[str, int] = {}
    feed_status: dict[str, str] = {}

    for feed_name, url in NEWS_FEEDS.get(market, {}).items():
        items, entry, status = fetch_conditional(url, headers_cache)
        feed_status[feed_name] = status
        if entry:
            headers_cache[url] = entry
        else:
            headers_cache.pop(url, None)  # 헤더 없는 서버는 캐시에 남기지 않는다
        per_feed[feed_name] = len(items)
        for it in items:
            link = it.get("link") or ""
            title = (it.get("title") or "").strip()
            if not link or not title:
                continue
            key = normalize_link(link)
            pub = parse_published(it.get("published"))
            row = store.get(key)
            if row is None:
                store[key] = {
                    "key": key,
                    "title": title,
                    "link": link,
                    "outlet": resolve_outlet(feed_name, it),
                    "feed": feed_name,
                    # 발행시각을 모르면 None 으로 남기고, 수집시각을 대체로 쓴다.
                    # "발행시각 미상"과 "수집시각 X"를 섞지 않는다 — 섞으면 나중에
                    # 어느 쪽 근거로 시간 창을 통과했는지 알 수 없다.
                    "published": pub.isoformat() if pub else None,
                    "published_known": pub is not None,
                    "first_seen": stamp,
                    "last_seen": stamp,
                    "seen_count": 1,
                }
                new += 1
            else:
                # 중복은 버리지 않고 센다 — 계속 재보도되는 기사는 다른 사건이다.
                row["last_seen"] = stamp
                row["seen_count"] = int(row.get("seen_count", 1)) + 1
                dup += 1

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in sorted(store.values(), key=lambda r: r["first_seen"]):
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    save_feed_headers_cache(headers_path, headers_cache)

    # 304 는 성공이다 — "변경 없음"을 "죽음"으로 읽으면 감시가 양치기 소년이 된다.
    # item 개수만으로 죽음을 판정하면 조건부 GET 도입 후 정상 피드가 매번 dead로
    # 잡혀 opstate.record_feed_health 의 last_ok 가 안 갱신되고, stale_feeds 가
    # 건강한 피드를 계속 경보한다. status 로 판정한다 — error 는 죽음, ok인데
    # 0건(빈 문서 응답)도 기존과 동일하게 죽음, not_modified 는 0건이어도 생존.
    dead = [n for n in per_feed
            if feed_status[n] == "error" or (feed_status[n] == "ok" and per_feed[n] == 0)]
    return {
        "market": market, "date": day.isoformat(), "collected_at": stamp,
        "new": new, "duplicate": dup, "total": len(store),
        "feeds": len(per_feed), "dead_feeds": dead,
        # 살아있는 피드 **이름**도 돌려준다. 감시(`opstate.record_feed_health`)는
        # 죽은 피드를 지우지 않고 "마지막 성공 시각이 낡아가는 것"으로 표현하므로
        # 양쪽 목록이 다 필요하다 — 개수만으로는 "원래 없던 피드"와 "오늘 죽은
        # 피드"를 구분할 수 없다.
        "alive_feeds": [n for n in per_feed if n not in dead],
    }


def window_articles(root: Path, market: str, since: datetime,
                    until: datetime | None = None, days_back: int = 4) -> list[dict]:
    """세션 창 안의 기사. 리포트 빌드가 이걸 읽는다(피드를 다시 긁지 않는다).

    발행시각을 모르는 기사는 **수집시각으로 판정**한다 — 버리지 않되, 어느 근거로
    통과했는지 `published_known` 으로 구분해 남긴다.
    """
    until = until or datetime.now(timezone.utc)
    start_day = since.astimezone(timezone(timedelta(hours=9))).date()
    out: list[dict] = []
    for back in range(days_back + 1):
        day = start_day + timedelta(days=back)
        for row in load_store(store_path(root, market, day)).values():
            raw = row.get("published") if row.get("published_known") else row.get("first_seen")
            try:
                ts = datetime.fromisoformat(raw)
            except (TypeError, ValueError):
                continue
            if since <= ts <= until:
                out.append(row)
    return sorted(out, key=lambda r: r.get("published") or r["first_seen"], reverse=True)


def prune(root: Path, market: str, today: date, keep_days: int = RETENTION_DAYS) -> int:
    """보존 기간이 지난 일자 파일을 지운다. 지운 파일 수 반환."""
    base = root / "data" / "news" / market
    if not base.exists():
        return 0
    cutoff = today - timedelta(days=keep_days)
    removed = 0
    for p in base.glob("*.jsonl"):
        try:
            if date.fromisoformat(p.stem) < cutoff:
                p.unlink()
                removed += 1
        except ValueError:
            continue
    return removed


def render_vault(root: Path, market: str, day: date, symbols_of=None) -> Path:
    """누적분을 Obsidian 마크다운으로 렌더한다. **매번 새로 쓴다**(뷰이므로).

    `symbols_of(title) -> list[str]`: 제목에서 종목을 뽑는 함수(선택). 넘기면
    종목별 섹션을 만든다 — 지식화 세션이 "이 종목에 대해 오늘 무슨 말이 나왔나"를
    한 번에 읽게 하려는 것이다.
    """
    rows = list(load_store(store_path(root, market, day)).values())
    rows.sort(key=lambda r: r.get("published") or r["first_seen"], reverse=True)

    outlets: dict[str, int] = {}
    for r in rows:
        outlets[r.get("outlet") or r.get("feed") or "?"] = outlets.get(
            r.get("outlet") or r.get("feed") or "?", 0) + 1

    label = {"KR": "한국장", "US": "미국장"}.get(market, market)
    lines = [
        "---",
        f"date: {day.isoformat()}",
        f"market: {market}",
        f"articles: {len(rows)}",
        f"outlets: {len(outlets)}",
        "tags: [news/scrape, market/" + market.lower() + "]",
        "---",
        "",
        f"# {label} 뉴스 수집 · {day.isoformat()}",
        "",
        f"기사 **{len(rows)}건** · 매체 **{len(outlets)}곳**",
        "",
    ]

    if outlets:
        top = sorted(outlets.items(), key=lambda kv: -kv[1])[:8]
        lines += ["## 매체별 건수", ""]
        lines += [f"- {name} — {n}건" for name, n in top]
        lines.append("")

    # 여러 번 관측된 기사 = 계속 재보도되는 기사. 버리지 않고 따로 보여준다.
    repeated = [r for r in rows if int(r.get("seen_count", 1)) >= 3]
    if repeated:
        lines += ["## 반복 관측 (재보도 지속)", ""]
        for r in sorted(repeated, key=lambda x: -int(x.get("seen_count", 1)))[:15]:
            lines.append(f"- **{r['title']}** — {r.get('outlet','?')} · "
                         f"{r['seen_count']}회 관측 · 최초 "
                         f"{_local_hhmm(r['first_seen'], market)}")
        lines.append("")

    if symbols_of is not None:
        by_symbol: dict[str, list[dict]] = {}
        for r in rows:
            for sym in symbols_of(r["title"]):
                by_symbol.setdefault(sym, []).append(r)
        if by_symbol:
            lines += ["## 종목별", ""]
            for sym, items in sorted(by_symbol.items(), key=lambda kv: -len(kv[1]))[:30]:
                lines.append(f"### {sym} ({len(items)}건)")
                for r in items[:8]:
                    when = _local_hhmm(r.get("published") or r["first_seen"], market)
                    lines.append(f"- {when} [{r['title']}]({r['link']}) — {r.get('outlet','?')}")
                lines.append("")

    lines += ["## 전체 기사", ""]
    for r in rows:
        when = _local_hhmm(r.get("published") or r["first_seen"], market)
        mark = "" if r.get("published_known") else " ⏱발행시각미상"
        rep = f" ×{r['seen_count']}" if int(r.get("seen_count", 1)) > 1 else ""
        lines.append(f"- `{when}`{mark}{rep} [{r['title']}]({r['link']}) — {r.get('outlet','?')}")

    path = vault_path(root, market, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
