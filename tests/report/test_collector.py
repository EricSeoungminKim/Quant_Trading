"""뉴스 누적 수집기 — 리포트에서 수집을 떼어낸 층.

핵심 회귀: RSS 는 "최신 N건" 창이라 한 번만 긁으면 그 사이 밀려난 기사가
영구히 사라진다(2026-08-13 실측: 9시간 뒤 재수집에서 investing.com·SeekingAlpha·
Bloomberg·Yahoo 가 겹침 0). 그래서 여러 번 긁어 **쌓고**, 중복은 버리지 않고 센다.
"""
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from quant.collect.collector import (
    collect_once,
    load_store,
    normalize_link,
    prune,
    render_vault,
    store_path,
)

UTC = timezone.utc


# --- 링크 정규화 (중복 판정의 기준) ---

def test_tracking_params_do_not_create_duplicates():
    """같은 기사가 utm 파라미터만 달라 두 건으로 세지면 재보도 집계가 무의미해진다."""
    a = normalize_link("https://Example.com/news/1?utm_source=rss&utm_medium=feed")
    b = normalize_link("https://example.com/news/1")
    assert a == b


def test_google_news_oc_param_is_stripped():
    assert normalize_link("https://news.google.com/rss/articles/ABC?oc=5") == \
           normalize_link("https://news.google.com/rss/articles/ABC")


def test_path_case_is_preserved():
    """경로는 대소문자를 구분하는 서버가 있다 — 뭉개면 다른 기사를 같은 것으로 센다."""
    assert normalize_link("https://x.com/A") != normalize_link("https://x.com/a")


def test_meaningful_query_survives():
    """추적 파라미터만 벗긴다 — 기사 id 가 쿼리에 있는 매체가 있다."""
    assert "newsId=123" in normalize_link("https://edaily.co.kr/read?newsId=123&utm_source=x")


def test_garbage_url_does_not_crash():
    assert normalize_link("not a url") == "not a url"


# --- 누적과 중복 계수 ---

def _fake_feed(monkeypatch, items_by_feed):
    """`fetch_conditional`을 흉내낸다 — 캐시는 안 쓰고(빈 dict) 매번 "200" 취급이다.

    조건부 GET 자체(304/캐시 갱신)는 `test_feeds.py`가 `fetch_conditional`을
    직접 단위 테스트한다. 여기서는 `collect_once`가 그 결과를 어떻게 저장소에
    반영하는지만 본다.
    """
    from quant.collect import collector as mod
    monkeypatch.setattr(mod, "NEWS_FEEDS", {"KR": {n: f"http://feed/{n}" for n in items_by_feed}})
    monkeypatch.setattr(
        mod, "fetch_conditional",
        lambda url, cache: (items_by_feed[url.rsplit("/", 1)[-1]], {}, "ok"),
    )
    monkeypatch.setattr(mod, "resolve_outlet", lambda feed, item: item.get("outlet") or feed)


def _item(title, link, published="Wed, 13 Aug 2026 09:00:00 +0900"):
    return {"title": title, "link": link, "published": published, "outlet": ""}


def test_second_run_counts_duplicates_instead_of_discarding(tmp_path, monkeypatch):
    """중복은 잡음이 아니라 신호다 — 계속 재보도되는 기사는 다른 사건이다."""
    _fake_feed(monkeypatch, {"A": [_item("같은 기사", "https://x.com/1")]})
    now = datetime(2026, 8, 13, 0, 0, tzinfo=UTC)

    first = collect_once("KR", tmp_path, now=now)
    assert (first["new"], first["duplicate"]) == (1, 0)

    second = collect_once("KR", tmp_path, now=now + timedelta(hours=1))
    assert (second["new"], second["duplicate"], second["total"]) == (0, 1, 1)

    row = next(iter(load_store(store_path(tmp_path, "KR", date(2026, 8, 13))).values()))
    assert row["seen_count"] == 2
    assert row["first_seen"] != row["last_seen"]


def test_rolled_off_articles_are_kept(tmp_path, monkeypatch):
    """이게 이 모듈의 존재 이유다 — 창에서 밀려난 기사가 저장소에는 남는다."""
    now = datetime(2026, 8, 13, 0, 0, tzinfo=UTC)
    _fake_feed(monkeypatch, {"A": [_item("1회차 기사", "https://x.com/1")]})
    collect_once("KR", tmp_path, now=now)

    # 피드 창이 통째로 굴러갔다(겹침 0) — 실측에서 9시간 만에 실제로 그랬다
    _fake_feed(monkeypatch, {"A": [_item("2회차 기사", "https://x.com/2")]})
    stat = collect_once("KR", tmp_path, now=now + timedelta(hours=9))

    assert stat["total"] == 2, "밀려난 기사가 사라지면 안 된다"
    titles = {r["title"] for r in load_store(store_path(tmp_path, "KR", date(2026, 8, 13))).values()}
    assert titles == {"1회차 기사", "2회차 기사"}


def test_dead_feed_is_reported_not_silently_zero(tmp_path, monkeypatch):
    """죽은 피드와 조용한 피드는 다르다 — 0건을 정상으로 넘기지 않는다."""
    _fake_feed(monkeypatch, {"살아있음": [_item("a", "https://x.com/1")], "죽음": []})
    stat = collect_once("KR", tmp_path, now=datetime(2026, 8, 13, tzinfo=UTC))
    assert stat["dead_feeds"] == ["죽음"]


def test_alive_feeds_are_named_not_just_counted(tmp_path, monkeypatch):
    """감시가 **이름**을 필요로 한다 (Phase 5.3).

    `opstate.record_feed_health()` 는 죽은 피드를 지우지 않고 "마지막 성공 시각이
    낡아가는 것"으로 표현한다 — 그러려면 살아있는 쪽 이름이 있어야 한다. 개수만으로는
    "원래 없던 피드"와 "오늘 죽은 피드"를 구분할 수 없다.
    """
    _fake_feed(monkeypatch, {"살아있음": [_item("a", "https://x.com/1")], "죽음": []})

    stat = collect_once("KR", tmp_path, now=datetime(2026, 8, 13, tzinfo=UTC))

    assert stat["alive_feeds"] == ["살아있음"]
    assert stat["dead_feeds"] == ["죽음"]
    assert stat["feeds"] == 2   # 개수는 살아있는 것만이 아니라 전체다


def test_item_without_link_or_title_is_skipped(tmp_path, monkeypatch):
    _fake_feed(monkeypatch, {"A": [
        {"title": "", "link": "https://x.com/1", "published": None},
        {"title": "제목만", "link": "", "published": None},
    ]})
    assert collect_once("KR", tmp_path, now=datetime(2026, 8, 13, tzinfo=UTC))["total"] == 0


# --- 발행시각 미상 처리 ---

def test_undated_article_records_collection_time_separately(tmp_path, monkeypatch):
    """'발행시각 미상'과 '수집시각 X'를 섞지 않는다 — 섞으면 어느 근거로 시간 창을
    통과했는지 나중에 알 수 없다."""
    _fake_feed(monkeypatch, {"A": [_item("날짜없음", "https://x.com/1", published=None)]})
    collect_once("KR", tmp_path, now=datetime(2026, 8, 13, 5, 0, tzinfo=UTC))
    row = next(iter(load_store(store_path(tmp_path, "KR", date(2026, 8, 13))).values()))
    assert row["published"] is None
    assert row["published_known"] is False
    assert row["first_seen"].startswith("2026-08-13T05:00")


# --- 보존 ---

def test_prune_removes_only_old_days(tmp_path, monkeypatch):
    _fake_feed(monkeypatch, {"A": [_item("a", "https://x.com/1")]})
    for d in (date(2026, 8, 1), date(2026, 8, 12), date(2026, 8, 13)):
        collect_once("KR", tmp_path,
                     now=datetime(d.year, d.month, d.day, 3, 0, tzinfo=UTC))
    removed = prune(tmp_path, "KR", date(2026, 8, 13), keep_days=7)
    assert removed == 1
    assert not store_path(tmp_path, "KR", date(2026, 8, 1)).exists()
    assert store_path(tmp_path, "KR", date(2026, 8, 13)).exists()


# --- 볼트 렌더 ---

def test_vault_shows_local_time_not_utc(tmp_path, monkeypatch):
    """한국 리포트에 "03:00"이 찍히면 12:00 인지 03:00 인지 읽는 쪽이 알 수 없다."""
    _fake_feed(monkeypatch, {"A": [_item("정오 기사", "https://x.com/1",
                                         "Wed, 13 Aug 2026 12:00:00 +0900")]})
    collect_once("KR", tmp_path, now=datetime(2026, 8, 13, 4, 0, tzinfo=UTC))
    text = render_vault(tmp_path, "KR", date(2026, 8, 13)).read_text(encoding="utf-8")
    assert "`12:00`" in text and "`03:00`" not in text


def test_vault_has_obsidian_frontmatter(tmp_path, monkeypatch):
    _fake_feed(monkeypatch, {"A": [_item("a", "https://x.com/1")]})
    collect_once("KR", tmp_path, now=datetime(2026, 8, 13, tzinfo=UTC))
    text = render_vault(tmp_path, "KR", date(2026, 8, 13)).read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert "market: KR" in text and "tags: [news/scrape" in text


def test_vault_groups_by_symbol_when_extractor_given(tmp_path, monkeypatch):
    _fake_feed(monkeypatch, {"A": [
        _item("삼성전자 신고가", "https://x.com/1"),
        _item("삼성전자 또 상승", "https://x.com/2"),
    ]})
    collect_once("KR", tmp_path, now=datetime(2026, 8, 13, tzinfo=UTC))
    text = render_vault(tmp_path, "KR", date(2026, 8, 13),
                        symbols_of=lambda t: ["005930"] if "삼성전자" in t else []
                        ).read_text(encoding="utf-8")
    assert "### 005930 (2건)" in text


def test_vault_marks_undated_articles(tmp_path, monkeypatch):
    _fake_feed(monkeypatch, {"A": [_item("날짜없음", "https://x.com/1", published=None)]})
    collect_once("KR", tmp_path, now=datetime(2026, 8, 13, tzinfo=UTC))
    text = render_vault(tmp_path, "KR", date(2026, 8, 13)).read_text(encoding="utf-8")
    assert "발행시각미상" in text


# --- RSS 조건부 GET 캐시 (data/cache/feed_headers.json, H-2 Task 2) ---


def test_collect_once_works_without_a_pre_existing_cache_file(tmp_path, monkeypatch):
    """하위호환: 캐시 파일이 아예 없어도 기존과 동일하게 동작해야 한다."""
    from quant.collect.collector import feed_headers_cache_path

    _fake_feed(monkeypatch, {"A": [_item("첫 기사", "https://x.com/1")]})
    assert not feed_headers_cache_path(tmp_path).exists()

    stat = collect_once("KR", tmp_path, now=datetime(2026, 8, 13, tzinfo=UTC))

    assert (stat["new"], stat["duplicate"], stat["total"]) == (1, 0, 1)


def test_collect_once_persists_feed_headers_after_run(tmp_path, monkeypatch):
    """`fetch_conditional`이 돌려준 (etag/last_modified) 헤더가 다음 실행을 위해
    디스크에 남아야 한다."""
    from quant.collect import collector as mod
    from quant.collect.collector import feed_headers_cache_path

    monkeypatch.setattr(mod, "NEWS_FEEDS", {"KR": {"A": "http://feed/A"}})
    monkeypatch.setattr(
        mod, "fetch_conditional",
        lambda url, cache: ([_item("기사", "https://x.com/1")], {"etag": '"abc"'}, "ok"),
    )
    monkeypatch.setattr(mod, "resolve_outlet", lambda feed, item: item.get("outlet") or feed)

    collect_once("KR", tmp_path, now=datetime(2026, 8, 13, tzinfo=UTC))

    saved = json.loads(feed_headers_cache_path(tmp_path).read_text(encoding="utf-8"))
    assert saved == {"http://feed/A": {"etag": '"abc"'}}


def test_collect_once_passes_loaded_cache_into_fetch_conditional(tmp_path, monkeypatch):
    """디스크에 있던 캐시가 다음 실행에서 `fetch_conditional`로 그대로 들어가야 한다."""
    from quant.collect import collector as mod
    from quant.collect.collector import feed_headers_cache_path

    cache_path = feed_headers_cache_path(tmp_path)
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text(json.dumps({"http://feed/A": {"etag": '"prev"'}}), encoding="utf-8")

    seen_cache = {}
    monkeypatch.setattr(mod, "NEWS_FEEDS", {"KR": {"A": "http://feed/A"}})

    def fake_fetch_conditional(url, cache):
        seen_cache.update(cache)
        return [], cache.get(url, {}), "not_modified"

    monkeypatch.setattr(mod, "fetch_conditional", fake_fetch_conditional)
    monkeypatch.setattr(mod, "resolve_outlet", lambda feed, item: item.get("outlet") or feed)

    collect_once("KR", tmp_path, now=datetime(2026, 8, 13, tzinfo=UTC))

    assert seen_cache == {"http://feed/A": {"etag": '"prev"'}}


def test_corrupt_cache_file_restarts_from_empty_dict(tmp_path, monkeypatch):
    """깨진 캐시 파일이 수집 자체를 막으면 안 된다 — 성능 최적화일 뿐이다."""
    from quant.collect.collector import feed_headers_cache_path

    cache_path = feed_headers_cache_path(tmp_path)
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text("{이건 json이 아니다", encoding="utf-8")

    _fake_feed(monkeypatch, {"A": [_item("기사", "https://x.com/1")]})
    stat = collect_once("KR", tmp_path, now=datetime(2026, 8, 13, tzinfo=UTC))

    assert stat["new"] == 1
    # 재수집 후에는 유효한 JSON으로 다시 써져 있어야 한다.
    assert json.loads(cache_path.read_text(encoding="utf-8")) == {}


def test_headerless_feed_entry_is_not_persisted_in_cache(tmp_path, monkeypatch):
    """ETag/Last-Modified가 없는 서버는 캐시에 남기지 않는다 — 이전 항목이 있었어도
    지워야 다음 실행이 존재하지 않는 조건부 헤더로 계속 우기지 않는다."""
    from quant.collect import collector as mod
    from quant.collect.collector import feed_headers_cache_path

    cache_path = feed_headers_cache_path(tmp_path)
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text(json.dumps({"http://feed/A": {"etag": '"stale"'}}), encoding="utf-8")

    monkeypatch.setattr(mod, "NEWS_FEEDS", {"KR": {"A": "http://feed/A"}})
    monkeypatch.setattr(mod, "fetch_conditional",
                        lambda url, cache: ([_item("기사", "https://x.com/1")], {}, "ok"))
    monkeypatch.setattr(mod, "resolve_outlet", lambda feed, item: item.get("outlet") or feed)

    collect_once("KR", tmp_path, now=datetime(2026, 8, 13, tzinfo=UTC))

    assert json.loads(cache_path.read_text(encoding="utf-8")) == {}


# --- 건강도 판정: 304 는 성공이다 (fix, 2026-08-16) ---
#
# 조건부 GET 도입 직후 실측: 304(변경 없음)를 item 개수 0으로만 보면 매 주기
# 정상 피드가 dead_feeds 에 잡혀 opstate.record_feed_health 의 last_ok 가 절대
# 갱신되지 않고, stale_feeds 감시가 건강한 피드를 계속 경보한다(양치기 소년).
# status 로 판정해야 한다 — "변경 없음"과 "죽음"은 다른 사건이다.


def test_not_modified_feed_is_alive_not_dead(tmp_path, monkeypatch):
    """304 는 서버가 응답했다는 뜻이다 — 신규 0건이어도 죽은 게 아니다."""
    from quant.collect import collector as mod

    monkeypatch.setattr(mod, "NEWS_FEEDS", {"KR": {"변경없음": "http://feed/A"}})
    monkeypatch.setattr(mod, "fetch_conditional",
                        lambda url, cache: ([], {"etag": '"v1"'}, "not_modified"))
    monkeypatch.setattr(mod, "resolve_outlet", lambda feed, item: item.get("outlet") or feed)

    stat = collect_once("KR", tmp_path, now=datetime(2026, 8, 13, tzinfo=UTC))

    assert stat["alive_feeds"] == ["변경없음"]
    assert stat["dead_feeds"] == []


def test_error_feed_is_dead(tmp_path, monkeypatch):
    """네트워크 예외·4xx/5xx(status="error")는 여전히 죽은 것으로 잡혀야 한다."""
    from quant.collect import collector as mod

    monkeypatch.setattr(mod, "NEWS_FEEDS", {"KR": {"고장남": "http://feed/A"}})
    monkeypatch.setattr(mod, "fetch_conditional",
                        lambda url, cache: ([], {}, "error"))
    monkeypatch.setattr(mod, "resolve_outlet", lambda feed, item: item.get("outlet") or feed)

    stat = collect_once("KR", tmp_path, now=datetime(2026, 8, 13, tzinfo=UTC))

    assert stat["dead_feeds"] == ["고장남"]
    assert stat["alive_feeds"] == []


def test_empty_200_feed_stays_dead(tmp_path, monkeypatch):
    """200인데 item 이 0건(진짜 빈 문서)인 경우는 기존과 동일하게 죽은 것으로
    잡아야 한다 — status="ok"만으로 자동 생존 처리하면 안 된다."""
    from quant.collect import collector as mod

    monkeypatch.setattr(mod, "NEWS_FEEDS", {"KR": {"빈문서": "http://feed/A"}})
    monkeypatch.setattr(mod, "fetch_conditional",
                        lambda url, cache: ([], {}, "ok"))
    monkeypatch.setattr(mod, "resolve_outlet", lambda feed, item: item.get("outlet") or feed)

    stat = collect_once("KR", tmp_path, now=datetime(2026, 8, 13, tzinfo=UTC))

    assert stat["dead_feeds"] == ["빈문서"]
    assert stat["alive_feeds"] == []


def test_mixed_feed_statuses_split_correctly_between_alive_and_dead(tmp_path, monkeypatch):
    """한 번의 collect_once 에 세 상태가 섞여도 각자 맞는 쪽으로 갈라져야 한다."""
    from quant.collect import collector as mod

    monkeypatch.setattr(mod, "NEWS_FEEDS", {"KR": {
        "정상": "http://feed/A", "변경없음": "http://feed/B", "고장남": "http://feed/C",
    }})

    def fake_fetch_conditional(url, cache):
        if url.endswith("/A"):
            return [_item("기사", "https://x.com/1")], {}, "ok"
        if url.endswith("/B"):
            return [], {"etag": '"v1"'}, "not_modified"
        return [], {}, "error"

    monkeypatch.setattr(mod, "fetch_conditional", fake_fetch_conditional)
    monkeypatch.setattr(mod, "resolve_outlet", lambda feed, item: item.get("outlet") or feed)

    stat = collect_once("KR", tmp_path, now=datetime(2026, 8, 13, tzinfo=UTC))

    assert sorted(stat["alive_feeds"]) == ["변경없음", "정상"]
    assert stat["dead_feeds"] == ["고장남"]


def test_vault_is_regenerated_not_appended(tmp_path, monkeypatch):
    """md 는 뷰다 — 두 번 렌더해도 내용이 두 배가 되면 안 된다."""
    _fake_feed(monkeypatch, {"A": [_item("a", "https://x.com/1")]})
    collect_once("KR", tmp_path, now=datetime(2026, 8, 13, tzinfo=UTC))
    first = render_vault(tmp_path, "KR", date(2026, 8, 13)).read_text(encoding="utf-8")
    second = render_vault(tmp_path, "KR", date(2026, 8, 13)).read_text(encoding="utf-8")
    assert first == second


# ============ 선정 원장: 깨진 첫 기록이 그날을 영구히 오염시켰다 (2026-08-14)
#
# `selections.append()` 는 (날짜,시장,종목) 중복을 건너뛴다 — 하루에 리포트를 두 번
# 돌려도 표본이 중복되지 않게 하는 의도된 설계다. 그런데 그 때문에 **첫 기록이 깨져
# 있으면 정정한 재빌드가 버려진다.**
#
# 실측: API 키가 깨진 상태로 빌드가 돌아 `close=None` 인 123행이 먼저 들어갔고, 키를
# 고친 뒤 재빌드한 행은 전부 중복으로 스킵됐다. 기준가가 없으면 전방 수익률을 영원히
# 계산할 수 없으므로 그날 표본이 통째로 죽는다.
#
# 규칙: **결측 → 존재로만 승격한다.** 좋은 값을 덮어쓰지 않으므로 소급 변경 위험이 없다.

def test_rebuild_upgrades_a_row_that_was_missing_its_base_price(tmp_path):
    from quant.control import selections

    path = tmp_path / "selections.jsonl"
    broken = {"schema": 1, "date": "2026-08-14", "market": "KR", "symbol": "005930",
              "close": None, "trending_score100": 50, "is_candidate": True}
    assert selections.append([broken], path) == 1

    fixed = {**broken, "close": 71000.0}
    added = selections.append([fixed], path)

    rows = selections.load(path)
    assert len(rows) == 1, "행이 두 배가 되면 표본이 중복된다"
    assert rows[0]["close"] == 71000.0
    assert added == 1


def test_rebuild_never_overwrites_a_row_that_already_has_a_base_price(tmp_path):
    """**좋은 데이터를 덮지 않는다.** 그날의 판단 근거가 소급 변경되면 채점이
    무의미해진다 — 승격은 결측→존재 한 방향뿐이다."""
    from quant.control import selections

    path = tmp_path / "selections.jsonl"
    good = {"schema": 1, "date": "2026-08-14", "market": "KR", "symbol": "005930",
            "close": 71000.0, "trending_score100": 68, "is_candidate": True}
    selections.append([good], path)

    selections.append([{**good, "close": 99999.0, "trending_score100": 1}], path)

    rows = selections.load(path)
    assert len(rows) == 1
    assert rows[0]["close"] == 71000.0
    assert rows[0]["trending_score100"] == 68


def test_outcomes_already_filled_are_preserved_on_upgrade(tmp_path):
    """승격이 사후에 채운 수익률을 지우면 안 된다."""
    from quant.control import selections

    path = tmp_path / "selections.jsonl"
    row = {"schema": 1, "date": "2026-08-14", "market": "KR", "symbol": "005930",
           "close": None, "trending_score100": 50, "is_candidate": True,
           "outcome_d1_bps": 120.0}
    selections.append([row], path)

    selections.append([{**row, "close": 71000.0, "outcome_d1_bps": None}], path)

    rows = selections.load(path)
    assert rows[0]["close"] == 71000.0
    assert rows[0]["outcome_d1_bps"] == 120.0


# --- 저장소 창 읽기 (`load_window`, 2026-09-03) ---
#
# 리포트 빌드가 실시간 RSS 만 읽으면 30분마다 쌓인 누적분의 극히 일부만 본다
# (2026-09-02 EC2 실측: KR 21%, US 26%). `load_window`는 build_sources의 "news"
# 소스가 실시간 결과와 유니언할 수 있게, 저장소에서 발행창에 맞는 기사를 피드별로
# 묶어 돌려준다(`fetch_news`의 `feeds` 값과 같은 모양).

from quant.collect.collector import load_window


def test_load_window_returns_feeds_shape_grouped_by_feed(tmp_path, monkeypatch):
    _fake_feed(monkeypatch, {
        "A": [_item("기사1", "https://x.com/1", "Wed, 13 Aug 2026 09:00:00 +0900")],
        "B": [_item("기사2", "https://x.com/2", "Wed, 13 Aug 2026 09:00:00 +0900")],
    })
    collect_once("KR", tmp_path, now=datetime(2026, 8, 13, 0, 0, tzinfo=UTC))

    out = load_window(tmp_path, "KR", since=datetime(2026, 8, 12, tzinfo=UTC))

    assert set(out.keys()) == {"A", "B"}
    assert out["A"][0]["title"] == "기사1"
    assert out["A"][0]["link"] == "https://x.com/1"
    assert "outlet" in out["A"][0]


def test_load_window_filters_out_of_range_articles(tmp_path, monkeypatch):
    _fake_feed(monkeypatch, {"A": [
        _item("이전 기사", "https://x.com/1", "Tue, 12 Aug 2026 09:00:00 +0900"),
        _item("창 안 기사", "https://x.com/2", "Wed, 13 Aug 2026 09:00:00 +0900"),
    ]})
    collect_once("KR", tmp_path, now=datetime(2026, 8, 13, 1, 0, tzinfo=UTC))

    out = load_window(
        tmp_path, "KR",
        since=datetime(2026, 8, 13, 0, 0, tzinfo=UTC),
        until=datetime(2026, 8, 13, 2, 0, tzinfo=UTC),
    )
    titles = {it["title"] for items in out.values() for it in items}
    assert titles == {"창 안 기사"}


def test_load_window_keeps_undated_articles(tmp_path, monkeypatch):
    """발행시각을 못 읽은 기사는 (수집일 기준으로 읽히는 파일 안에서는) 발행시각
    필터로 걸러지지 않는다(`feeds.filter_since`와 같은 원칙) — 저장 파일 자체가
    수집일(KST) 단위라 `since`/`until` 을 벗어난 날짜의 파일은 애초에 열리지
    않는다는 전제 위에서다."""
    _fake_feed(monkeypatch, {"A": [_item("날짜없음", "https://x.com/1", published=None)]})
    collect_once("KR", tmp_path, now=datetime(2026, 8, 13, 1, 0, tzinfo=UTC))  # KST day 8/13

    out = load_window(
        tmp_path, "KR",
        since=datetime(2026, 8, 13, 0, 0, tzinfo=UTC),
        until=datetime(2026, 8, 13, 2, 0, tzinfo=UTC),
    )
    titles = {it["title"] for items in out.values() for it in items}
    assert titles == {"날짜없음"}


def test_load_window_spans_multiple_kst_day_files(tmp_path, monkeypatch):
    """저장 파일은 **수집이 돈 날**(KST) 단위다(`collect_once`의 `day` 계산) —
    창이 그 경계를 걸치면 두 파일 다 읽어야 한다."""
    _fake_feed(monkeypatch, {"A": [_item("첫날", "https://x.com/1", "Wed, 12 Aug 2026 19:00:00 +0900")]})
    collect_once("KR", tmp_path, now=datetime(2026, 8, 12, 10, 0, tzinfo=UTC))  # KST 8/12 19:00 → day 8/12
    _fake_feed(monkeypatch, {"A": [_item("둘째날", "https://x.com/2", "Thu, 13 Aug 2026 05:00:00 +0900")]})
    collect_once("KR", tmp_path, now=datetime(2026, 8, 12, 20, 0, tzinfo=UTC))  # KST 8/13 05:00 → day 8/13

    assert store_path(tmp_path, "KR", date(2026, 8, 12)).exists()
    assert store_path(tmp_path, "KR", date(2026, 8, 13)).exists()

    out = load_window(
        tmp_path, "KR",
        since=datetime(2026, 8, 12, 9, 0, tzinfo=UTC),
        until=datetime(2026, 8, 12, 21, 0, tzinfo=UTC),
    )
    titles = {it["title"] for items in out.values() for it in items}
    assert titles == {"첫날", "둘째날"}


def test_load_window_missing_store_returns_empty(tmp_path):
    assert load_window(tmp_path, "KR", since=datetime(2026, 8, 13, tzinfo=UTC)) == {}


# --- 상한 절단 신호(FEED_LIMIT, 2026-09-03) — collect_once 경로 ---

def test_collect_once_warns_when_feed_hits_the_cap(tmp_path, monkeypatch, caplog):
    from quant.collect.sources.feeds import FEED_LIMIT

    capped = [_item(f"t{i}", f"https://x.com/{i}") for i in range(FEED_LIMIT)]
    _fake_feed(monkeypatch, {"꽉찬매체": capped})

    with caplog.at_level("WARNING", logger="quant.collect.collector"):
        collect_once("KR", tmp_path, now=datetime(2026, 8, 13, tzinfo=UTC))

    assert any("꽉찬매체" in r.message for r in caplog.records)


def test_collect_once_does_not_warn_below_the_cap(tmp_path, monkeypatch, caplog):
    _fake_feed(monkeypatch, {"정상매체": [_item("a", "https://x.com/1")]})

    with caplog.at_level("WARNING", logger="quant.collect.collector"):
        collect_once("KR", tmp_path, now=datetime(2026, 8, 13, tzinfo=UTC))

    assert not any("정상매체" in r.message for r in caplog.records)
