"""tg_bridge.py의 관심종목 명령(/watch, /unwatch, /watchlist) 테스트.

server/scripts/tg_bridge.py는 패키지가 아닌 독립 스크립트라 sys.path에 그 디렉토리를
얹어 직접 import한다. 텔레그램/Toss API는 전부 mock — 실 네트워크 호출 없음.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "server" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import tg_bridge  # noqa: E402


class FakeTossClient:
    """toss_client.stock_info를 흉내낸다. raises=True면 API 실패(IP 미등록 등)를 시뮬레이션."""

    def __init__(self, info: dict | None = None, raises: bool = False):
        self._info = info or {}
        self._raises = raises
        self.calls: list[str] = []

    def stock_info(self, symbol: str) -> dict:
        self.calls.append(symbol)
        if self._raises:
            raise Exception("네트워크 실패(IP 미등록)")
        return self._info


# ---------------------------------------------------------------------------
# normalize_symbol
# ---------------------------------------------------------------------------
def test_normalize_symbol_six_digit_is_kr():
    assert tg_bridge.normalize_symbol("005930") == "005930"


def test_normalize_symbol_non_kr_uppercased():
    assert tg_bridge.normalize_symbol("aapl") == "AAPL"


def test_normalize_symbol_strips_whitespace():
    assert tg_bridge.normalize_symbol("  aapl  ") == "AAPL"


# ---------------------------------------------------------------------------
# 명령 파싱 — handle_watchlist_command가 명령이 아니면 None을 반환하는지
# ---------------------------------------------------------------------------
def test_non_command_message_returns_none():
    toss = FakeTossClient(info={"name": "Apple Inc."})
    assert tg_bridge.handle_watchlist_command("그냥 안부 인사", toss) is None


def test_watch_without_args_returns_usage():
    toss = FakeTossClient()
    reply = tg_bridge.handle_watchlist_command("/watch", toss)
    assert "사용법" in reply
    assert toss.calls == []  # 인자 없으면 검증 API를 호출하지 않는다


def test_watch_accepts_multiple_symbols_at_once(tmp_path):
    """사용자 요구(2026-08-09): 폰에서 `/watch AAPL TSLA 005930`처럼 한 줄로 여러 개.

    종목별 독립 처리 — 하나가 중복이어도 나머지는 추가되고, 응답에 종목별 결과가
    전부 나열돼야 한다(무엇이 어떻게 됐는지 숨기지 않는다).
    """
    path = tmp_path / "watchlist.yaml"
    toss = FakeTossClient()
    reply = tg_bridge.handle_watch("AAPL tsla 005930", toss, path=path)

    saved = [e["symbol"] for e in tg_bridge.load_watchlist(path)]
    assert saved == ["AAPL", "TSLA", "005930"]
    assert reply.count("추가") == 3
    assert "다음 세션" in reply

    # 같은 명령을 다시 — 전부 "이미 있음"으로 응답하고 파일은 그대로여야 한다.
    reply2 = tg_bridge.handle_watch("AAPL TSLA", toss, path=path)
    assert reply2.count("이미 있음") == 2
    assert [e["symbol"] for e in tg_bridge.load_watchlist(path)] == ["AAPL", "TSLA", "005930"]


def test_watch_partial_duplicate_adds_the_rest(tmp_path):
    path = tmp_path / "watchlist.yaml"
    toss = FakeTossClient()
    tg_bridge.handle_watch("AAPL", toss, path=path)
    reply = tg_bridge.handle_watch("AAPL NVDA", toss, path=path)

    assert "이미 있음" in reply and "NVDA" in reply
    assert [e["symbol"] for e in tg_bridge.load_watchlist(path)] == ["AAPL", "NVDA"]


def test_watchlist_reset_clears_everything(tmp_path, monkeypatch):
    path = tmp_path / "watchlist.yaml"
    monkeypatch.setattr(tg_bridge, "WATCHLIST_PATH", path)
    toss = FakeTossClient()
    tg_bridge.handle_watch("AAPL TSLA", toss, path=path)

    reply = tg_bridge.handle_watchlist_command("/watchlist-reset", toss)
    assert "2개 전체 삭제" in reply
    assert "열린 포지션" in reply, "포지션에 영향 없다는 안내가 있어야 한다"
    assert tg_bridge.load_watchlist(path) == []

    # 이미 빈 상태에서 또 호출 — 멱등, 에러 없이 안내만.
    assert "비어 있음" in tg_bridge.handle_watchlist_command("/watchlist-reset", toss)


def test_unwatch_without_args_returns_usage():
    reply = tg_bridge.handle_unwatch("")
    assert "사용법" in reply


# ---------------------------------------------------------------------------
# /watch — 검증 성공
# ---------------------------------------------------------------------------
def test_watch_verified_adds_entry_with_name(tmp_path):
    path = tmp_path / "watchlist.yaml"
    toss = FakeTossClient(info={"symbol": "AAPL", "name": "Apple Inc."})

    reply = tg_bridge.handle_watch("aapl", toss, path=path)

    assert "✅" in reply
    assert "AAPL" in reply
    assert "Apple Inc." in reply

    entries = tg_bridge.load_watchlist(path)
    assert len(entries) == 1
    assert entries[0]["symbol"] == "AAPL"
    assert entries[0]["name"] == "Apple Inc."
    assert entries[0]["added_at"]


def test_watch_kr_symbol_normalized_and_verified(tmp_path):
    path = tmp_path / "watchlist.yaml"
    toss = FakeTossClient(info={"symbol": "005930", "name": "삼성전자"})

    reply = tg_bridge.handle_watch("005930", toss, path=path)

    assert "✅" in reply
    entries = tg_bridge.load_watchlist(path)
    assert entries[0]["symbol"] == "005930"
    assert entries[0]["name"] == "삼성전자"


# ---------------------------------------------------------------------------
# /watch — 검증 API 실패 시 미검증 추가로 폴백
# ---------------------------------------------------------------------------
class RaisingTossClient:
    def stock_info(self, symbol: str) -> dict:
        raise RuntimeError("403 IP 미등록")


def test_watch_verification_failure_adds_unverified(tmp_path):
    path = tmp_path / "watchlist.yaml"
    toss = RaisingTossClient()

    reply = tg_bridge.handle_watch("TSLA", toss, path=path)

    assert "미검증" in reply
    entries = tg_bridge.load_watchlist(path)
    assert len(entries) == 1
    assert entries[0]["symbol"] == "TSLA"
    assert entries[0]["name"] == ""


def test_watch_empty_stock_info_result_is_unverified(tmp_path):
    """stock_info가 빈 dict를 반환하는 경우(종목 미존재)도 미검증으로 취급."""
    path = tmp_path / "watchlist.yaml"
    toss = FakeTossClient(info={})

    reply = tg_bridge.handle_watch("ZZZZZ", toss, path=path)

    assert "미검증" in reply
    entries = tg_bridge.load_watchlist(path)
    assert entries[0]["name"] == ""


# ---------------------------------------------------------------------------
# /watch — 중복 추가
# ---------------------------------------------------------------------------
def test_watch_duplicate_reports_already_present_without_calling_api(tmp_path):
    path = tmp_path / "watchlist.yaml"
    tg_bridge.save_watchlist(
        [{"symbol": "AAPL", "name": "Apple Inc.", "added_at": "2026-01-01T00:00:00"}],
        path,
    )
    toss = FakeTossClient(info={"name": "Apple Inc."})

    reply = tg_bridge.handle_watch("AAPL", toss, path=path)

    assert "이미" in reply
    assert toss.calls == []  # 중복이면 검증 API를 아예 호출하지 않는다
    assert len(tg_bridge.load_watchlist(path)) == 1


# ---------------------------------------------------------------------------
# /unwatch
# ---------------------------------------------------------------------------
def test_unwatch_removes_existing_entry(tmp_path):
    path = tmp_path / "watchlist.yaml"
    tg_bridge.save_watchlist(
        [
            {"symbol": "AAPL", "name": "Apple Inc.", "added_at": "2026-01-01T00:00:00"},
            {"symbol": "TSLA", "name": "Tesla", "added_at": "2026-01-02T00:00:00"},
        ],
        path,
    )

    reply = tg_bridge.handle_unwatch("aapl", path=path)

    assert "제거" in reply
    entries = tg_bridge.load_watchlist(path)
    assert [e["symbol"] for e in entries] == ["TSLA"]


def test_unwatch_missing_symbol_reports_not_present(tmp_path):
    path = tmp_path / "watchlist.yaml"
    tg_bridge.save_watchlist([], path)

    reply = tg_bridge.handle_unwatch("AAPL", path=path)

    assert "없음" in reply
    assert tg_bridge.load_watchlist(path) == []


# ---------------------------------------------------------------------------
# /watchlist
# ---------------------------------------------------------------------------
def test_watchlist_empty_reports_none(tmp_path):
    path = tmp_path / "watchlist.yaml"
    assert "없음" in tg_bridge.format_watchlist(path)


def test_watchlist_lists_entries(tmp_path):
    path = tmp_path / "watchlist.yaml"
    tg_bridge.save_watchlist(
        [{"symbol": "AAPL", "name": "Apple Inc.", "added_at": "2026-01-01T00:00:00"}],
        path,
    )

    text = tg_bridge.format_watchlist(path)

    assert "AAPL" in text
    assert "Apple Inc." in text
    assert "2026-01-01T00:00:00" in text
    assert "다음 세션" in text


# ---------------------------------------------------------------------------
# 원자적 쓰기 + 파일 형식 — {symbols: [{symbol, name, added_at}]}
# ---------------------------------------------------------------------------
def test_save_watchlist_uses_atomic_tmp_write_then_replace(tmp_path):
    path = tmp_path / "watchlist.yaml"
    tg_bridge.save_watchlist(
        [{"symbol": "AAPL", "name": "Apple Inc.", "added_at": "2026-01-01T00:00:00"}],
        path,
    )

    tmp_file = path.with_suffix(".tmp")
    assert not tmp_file.exists()  # 쓰기 완료 후 tmp 파일이 남아있지 않다(replace 완료)
    assert path.exists()

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert "symbols" in raw
    assert raw["symbols"][0]["symbol"] == "AAPL"
    assert raw["symbols"][0]["name"] == "Apple Inc."
    assert raw["symbols"][0]["added_at"] == "2026-01-01T00:00:00"


def test_load_watchlist_missing_file_returns_empty_list(tmp_path):
    path = tmp_path / "does_not_exist.yaml"
    assert tg_bridge.load_watchlist(path) == []


def test_load_watchlist_corrupt_file_returns_empty_list(tmp_path):
    path = tmp_path / "watchlist.yaml"
    path.write_text("not: valid: yaml: [", encoding="utf-8")
    assert tg_bridge.load_watchlist(path) == []


# ---------------------------------------------------------------------------
# process_update: 비명령 메시지는 관심종목 로직을 건드리지 않고 그대로 통과
# ---------------------------------------------------------------------------
class RecordingTelegramClient:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []
        self.typed: list[int] = []

    def send_message(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))

    def send_typing(self, chat_id: int) -> None:
        self.typed.append(chat_id)


class FakeControl:
    def is_halted(self):
        return False


def test_process_update_non_command_falls_through_to_claude(tmp_path, monkeypatch):
    monkeypatch.setattr(tg_bridge, "run_claude", lambda prompt, cwd=None: (True, "안녕하세요"))
    tg = RecordingTelegramClient()
    limiter = tg_bridge.RateLimiter()
    control = FakeControl()
    toss = FakeTossClient(info={"name": "Apple Inc."})
    update = {"update_id": 1, "message": {"chat": {"id": 42}, "text": "오늘 시장 어때?"}}

    tg_bridge.process_update(tg, 42, limiter, control, toss, update)

    assert tg.sent == [(42, "안녕하세요")]
    assert toss.calls == []  # 관심종목 로직이 전혀 개입하지 않는다


def test_process_update_watch_command_bypasses_claude_and_rate_limit(tmp_path, monkeypatch):
    watch_path = tmp_path / "watchlist.yaml"
    monkeypatch.setattr(tg_bridge, "WATCHLIST_PATH", watch_path)

    def _boom(*args, **kwargs):
        raise AssertionError("관심종목 명령은 claude 서브프로세스를 호출하면 안 된다")

    monkeypatch.setattr(tg_bridge, "run_claude", _boom)
    tg = RecordingTelegramClient()
    limiter = tg_bridge.RateLimiter()
    control = FakeControl()
    toss = FakeTossClient(info={"name": "Apple Inc."})
    update = {"update_id": 1, "message": {"chat": {"id": 42}, "text": "/watch AAPL"}}

    tg_bridge.process_update(tg, 42, limiter, control, toss, update)

    assert len(tg.sent) == 1
    assert tg.sent[0][0] == 42
    assert "✅" in tg.sent[0][1]
    entries = tg_bridge.load_watchlist(watch_path)
    assert entries[0]["symbol"] == "AAPL"


def test_concurrent_watch_calls_lose_nothing(tmp_path, monkeypatch):
    """동시 쓰기 유실 회귀 가드 (2026-08-09 E2E 실측: 잠금 없던 시절 15건 중 11건 유실).

    원자적 tmp-replace는 파일 깨짐만 막지 유실은 못 막는다 — 두 쓰기가 같은
    스냅샷을 읽으면 나중 저장이 앞 저장을 덮는다. flock이 read-modify-write
    전체를 배타 구간으로 만들어야 한다.
    """
    import concurrent.futures as cf

    path = tmp_path / "watchlist.yaml"
    toss = FakeTossClient()
    # 시장별 상한(12)에 걸리지 않게 US 8 + KR 7로 나눈다 — 이 테스트의 의도는
    # 상한이 아니라 **동시 쓰기 유실 0**이다.
    targets = [f"SYM{i}" for i in range(8)] + [f"{i:06d}" for i in range(1, 8)]
    with cf.ThreadPoolExecutor(4) as ex:
        list(ex.map(lambda s: tg_bridge.handle_watch(s, toss, path=path), targets))

    saved = {e["symbol"] for e in tg_bridge.load_watchlist(path)}
    missing = set(targets) - saved
    assert not missing, f"동시 쓰기 유실: {sorted(missing)}"


# ---------------------------------------------------------------------------
# provenance(source) — 자동등록 vs 사용자등록 구분
# ---------------------------------------------------------------------------
def test_watch_default_source_is_user(tmp_path):
    path = tmp_path / "watchlist.yaml"
    toss = FakeTossClient()
    tg_bridge.handle_watch("AAPL", toss, path=path)

    entries = tg_bridge.load_watchlist(path)
    assert entries[0]["source"] == "user"


def test_watch_auto_source_is_recorded(tmp_path):
    path = tmp_path / "watchlist.yaml"
    toss = FakeTossClient()
    tg_bridge.handle_watch("AAPL", toss, path=path, source="auto")

    entries = tg_bridge.load_watchlist(path)
    assert entries[0]["source"] == "auto"


# ---------------------------------------------------------------------------
# TTL — 자동등록 3일 만료
# ---------------------------------------------------------------------------
def test_ttl_prunes_old_auto_entry_but_keeps_old_user_entry(tmp_path):
    old = (datetime.now() - timedelta(days=4)).isoformat(timespec="seconds")
    path = tmp_path / "watchlist.yaml"
    tg_bridge.save_watchlist(
        [
            {"symbol": "OLDAUTO", "name": "", "added_at": old, "source": "auto"},
            {"symbol": "OLDUSER", "name": "", "added_at": old, "source": "user"},
        ],
        path,
    )
    toss = FakeTossClient()

    reply = tg_bridge.handle_watch("NEWSYM", toss, path=path)

    assert "OLDAUTO" in reply and "만료 제거" in reply
    saved = [e["symbol"] for e in tg_bridge.load_watchlist(path)]
    assert "OLDAUTO" not in saved
    assert "OLDUSER" in saved
    assert "NEWSYM" in saved


def test_ttl_keeps_auto_entry_within_three_days(tmp_path):
    recent = (datetime.now() - timedelta(days=3)).isoformat(timespec="seconds")
    path = tmp_path / "watchlist.yaml"
    tg_bridge.save_watchlist(
        [{"symbol": "RECENTAUTO", "name": "", "added_at": recent, "source": "auto"}],
        path,
    )
    toss = FakeTossClient()

    tg_bridge.handle_watch("NEWSYM", toss, path=path)

    saved = [e["symbol"] for e in tg_bridge.load_watchlist(path)]
    assert "RECENTAUTO" in saved


def test_ttl_keeps_auto_entry_with_missing_or_unparseable_added_at(tmp_path):
    path = tmp_path / "watchlist.yaml"
    tg_bridge.save_watchlist(
        [
            {"symbol": "NOTIME", "name": "", "source": "auto"},
            {"symbol": "BADTIME", "name": "", "added_at": "not-a-date", "source": "auto"},
        ],
        path,
    )
    toss = FakeTossClient()

    tg_bridge.handle_watch("NEWSYM", toss, path=path)

    saved = [e["symbol"] for e in tg_bridge.load_watchlist(path)]
    assert "NOTIME" in saved
    assert "BADTIME" in saved


# ---------------------------------------------------------------------------
# 상한(20) — user/auto 공유
# ---------------------------------------------------------------------------
def test_cap_rejects_the_entry_past_the_market_limit_and_reports_it(tmp_path):
    """상한은 2026-08-11부터 **시장별**이다(전역 20 → 시장당 12). 초과분은 거부되고
    그 사실이 응답에 드러나야 한다 — 조용히 삼키면 왜 안 들어왔는지 알 수 없다."""
    cap = tg_bridge.WATCHLIST_CAP_PER_MARKET
    path = tmp_path / "watchlist.yaml"
    tg_bridge.save_watchlist(
        [{"symbol": f"SYM{i}", "name": "", "added_at": datetime.now().isoformat(), "source": "user"}
         for i in range(cap)],
        path,
    )
    toss = FakeTossClient()

    reply = tg_bridge.handle_watch("EXTRA", toss, path=path)

    assert f"시장별 상한({cap}종목) 초과로 미등록" in reply
    assert "EXTRA" in reply
    saved = [e["symbol"] for e in tg_bridge.load_watchlist(path)]
    assert "EXTRA" not in saved
    assert len(saved) == cap


# ---------------------------------------------------------------------------
# format_watchlist — [auto] 마커
# ---------------------------------------------------------------------------
def test_format_watchlist_marks_auto_entries(tmp_path):
    path = tmp_path / "watchlist.yaml"
    tg_bridge.save_watchlist(
        [
            {"symbol": "AAPL", "name": "Apple Inc.", "added_at": "2026-01-01T00:00:00", "source": "user"},
            {"symbol": "TSLA", "name": "Tesla", "added_at": "2026-01-02T00:00:00", "source": "auto"},
        ],
        path,
    )

    text = tg_bridge.format_watchlist(path)

    assert "TSLA" in text and "[auto]" in text
    tsla_line = next(line for line in text.splitlines() if "TSLA" in line)
    assert "[auto]" in tsla_line
    aapl_line = next(line for line in text.splitlines() if "AAPL" in line)
    assert "[auto]" not in aapl_line


# ---------------------------------------------------------------------------
# CLI — watch-add --source auto
# ---------------------------------------------------------------------------
def test_parse_watch_add_argv_defaults_to_user_source():
    # 2026-08-17 재기준: --tags-only 추가로 반환값이 (source, tags, arg) ->
    # (source, tags, tags_only, arg) 4튜플로 바뀌었다(서브프로젝트 T FRGN_EXIT
    # 경로) — 의도(기본값 파싱)는 그대로다.
    source, tags, tags_only, arg = tg_bridge._parse_watch_add_argv(["AAPL", "NVDA"])
    assert source == "user"
    assert tags == []
    assert tags_only is False
    assert arg == "AAPL NVDA"


def test_parse_watch_add_argv_accepts_source_auto_flag():
    source, tags, tags_only, arg = tg_bridge._parse_watch_add_argv(["--source", "auto", "AAPL", "NVDA"])
    assert source == "auto"
    assert tags == []
    assert arg == "AAPL NVDA"


def test_parse_watch_add_argv_accepts_tags_flag_anywhere():
    """--tags도 --source와 같은 파싱 방식 — 위치 무관, '+'로 다중 태그."""
    source, tags, tags_only, arg = tg_bridge._parse_watch_add_argv(["AAPL", "--tags", "EVENT", "NVDA"])
    assert source == "user"
    assert tags == ["EVENT"]
    assert arg == "AAPL NVDA"


def test_parse_watch_add_argv_tags_flag_supports_plus_combination():
    source, tags, tags_only, arg = tg_bridge._parse_watch_add_argv(["--tags", "EVENT+TREND", "AAPL"])
    assert tags == ["EVENT", "TREND"]
    assert arg == "AAPL"


def test_parse_watch_add_argv_accepts_tags_only_flag_anywhere():
    """--tags-only는 값 없는 플래그 — own_brief.sh의 FRGN_EXIT 경로 전용."""
    source, tags, tags_only, arg = tg_bridge._parse_watch_add_argv(
        ["AAPL", "--tags-only", "--tags", "FRGN_EXIT"]
    )
    assert tags_only is True
    assert tags == ["FRGN_EXIT"]
    assert arg == "AAPL"


def test_parse_watch_add_argv_tags_flag_without_value_is_a_usage_error():
    with pytest.raises(tg_bridge._WatchAddUsageError):
        tg_bridge._parse_watch_add_argv(["AAPL", "--tags"])


def test_parse_watch_add_argv_raises_usage_error_when_empty():
    with pytest.raises(tg_bridge._WatchAddUsageError):
        tg_bridge._parse_watch_add_argv([])


def test_parse_watch_add_argv_raises_usage_error_when_source_missing_value():
    with pytest.raises(tg_bridge._WatchAddUsageError):
        tg_bridge._parse_watch_add_argv(["--source"])


def test_watch_add_cli_path_with_auto_source_end_to_end(tmp_path):
    """daily_brief.sh가 실제로 거치는 경로: argv 파싱 -> handle_watch(source=...)."""
    path = tmp_path / "watchlist.yaml"
    toss = FakeTossClient()
    source, tags, tags_only, arg = tg_bridge._parse_watch_add_argv(["--source", "auto", "AAPL"])

    reply = tg_bridge.handle_watch(arg, toss, path=path, source=source, tags=tags, tags_only=tags_only)

    assert "✅" in reply or "⚠️" in reply
    entries = tg_bridge.load_watchlist(path)
    assert entries[0]["symbol"] == "AAPL"
    assert entries[0]["source"] == "auto"
    assert "tags" not in entries[0], "태그 없이 호출하면 tags 키 자체가 없어야 한다(하위호환)"


def test_watch_add_cli_path_with_event_tag_end_to_end(tmp_path):
    """daily_brief.sh가 EVENT 태그 그룹을 넘기는 경로: --tags EVENT가 저장된 항목에 남는다."""
    path = tmp_path / "watchlist.yaml"
    toss = FakeTossClient()
    source, tags, tags_only, arg = tg_bridge._parse_watch_add_argv(["--source", "auto", "--tags", "EVENT", "AAPL"])

    tg_bridge.handle_watch(arg, toss, path=path, source=source, tags=tags)

    entries = tg_bridge.load_watchlist(path)
    assert entries[0]["tags"] == ["EVENT"]


def test_watch_add_merges_tags_into_existing_entry(tmp_path):
    """2026-08-17 서브프로젝트 T — 재등록일에 새 태그(EVENT_SCALP 등)가 '이미
    있음'으로 조용히 버려지던 결함 수정. 기존 태그는 지우지 않고 합집합만 늘린다."""
    path = tmp_path / "watchlist.yaml"
    toss = FakeTossClient()
    tg_bridge.handle_watch("AAPL", toss, path=path, source="auto", tags=["EVENT"])

    reply = tg_bridge.handle_watch("AAPL", toss, path=path, source="auto", tags=["EVENT_SCALP"])

    assert "태그 갱신" in reply
    entries = tg_bridge.load_watchlist(path)
    assert entries[0]["tags"] == ["EVENT", "EVENT_SCALP"]


def test_watch_add_existing_entry_without_tags_still_says_already_here(tmp_path):
    """태그 없이(tags=None) 재호출하면 기존 동작(이미 있음, 변경 없음) 그대로다."""
    path = tmp_path / "watchlist.yaml"
    toss = FakeTossClient()
    tg_bridge.handle_watch("AAPL", toss, path=path, source="auto", tags=["EVENT"])

    reply = tg_bridge.handle_watch("AAPL", toss, path=path)

    assert "이미 있음" in reply
    entries = tg_bridge.load_watchlist(path)
    assert entries[0]["tags"] == ["EVENT"], "태그 없이 부르면 기존 태그가 그대로 남아야 한다"


def test_watch_add_tags_only_updates_existing_but_skips_new_registration(tmp_path):
    """own_brief.sh의 FRGN_EXIT 경로 — 이미 등록된 종목만 태그가 갱신되고, 미등록
    종목은 신규 등록되지 않는다."""
    path = tmp_path / "watchlist.yaml"
    toss = FakeTossClient()
    tg_bridge.handle_watch("005930", toss, path=path, source="auto", tags=["FRGN"])

    reply = tg_bridge.handle_watch(
        "005930 000660", toss, path=path, source="auto", tags=["FRGN_EXIT"], tags_only=True,
    )

    entries = {e["symbol"]: e for e in tg_bridge.load_watchlist(path)}
    assert entries["005930"]["tags"] == ["FRGN", "FRGN_EXIT"]
    assert "000660" not in entries, "미등록 종목은 --tags-only로 신규 등록되면 안 된다"
    assert "000660" in reply and "아님" in reply  # "태그 전용 갱신 대상 아님" 안내


def test_parse_watch_add_argv_and_handle_watch_tags_only_cli_path_end_to_end(tmp_path):
    """CLI 경로: argv 파싱 -> handle_watch(tags_only=...)."""
    path = tmp_path / "watchlist.yaml"
    toss = FakeTossClient()
    tg_bridge.handle_watch("005930", toss, path=path, source="auto", tags=["FRGN"])

    source, tags, tags_only, arg = tg_bridge._parse_watch_add_argv(
        ["--tags-only", "--tags", "FRGN_EXIT", "005930"]
    )
    tg_bridge.handle_watch(arg, toss, path=path, source=source, tags=tags, tags_only=tags_only)

    entries = tg_bridge.load_watchlist(path)
    assert entries[0]["tags"] == ["FRGN", "FRGN_EXIT"]


# ---------------------------------------------------------------------------
# 즉시 조회 명령 (/balance, /scoreboard, /help) — 2026-08-10
# ---------------------------------------------------------------------------
def test_query_command_routes_and_help_lists_all_commands():
    import tg_bridge

    class _NullToss:
        pass

    assert tg_bridge.handle_query_command("아무 문장", _NullToss()) is None
    help_text = tg_bridge.handle_query_command("/help", _NullToss())
    for cmd in ("/balance", "/scoreboard", "/watch", "/halt", "/flatten"):
        assert cmd in help_text
    assert tg_bridge.handle_query_command("/명령어", _NullToss()) == help_text


def test_balance_renders_positions_with_quotes(tmp_path, monkeypatch):
    import json

    import tg_bridge

    portfolio = {
        "cash": 8_000_000.0,
        "positions": {
            "229200": {"qty": 88, "avg_cost": 13960.49,
                        "meta": {"stop": 13609.5}},
            "TQQQ": {"qty": 10, "avg_cost": 70.0, "meta": {}},
        },
    }
    p = tmp_path / "portfolio.json"
    p.write_text(json.dumps(portfolio), encoding="utf-8")
    monkeypatch.setattr(tg_bridge, "PORTFOLIO_STATE_PATH", p)
    monkeypatch.setattr(tg_bridge, "WATCHLIST_PATH", tmp_path / "none.yaml")

    class _Toss:
        def prices(self, symbols):
            return [{"symbol": "229200", "price": 14200.0},
                    {"symbol": "TQQQ", "price": 75.0}]

        def usd_krw(self):
            return 1000.0

    text = tg_bridge.handle_balance(_Toss())
    assert "평단가 13,960.49원" in text
    assert "+1.72%" in text, "229200 수익률 (14200/13960.49 - 1)"
    assert "손절가 13,609.50원" in text
    # 총자산 = 현금 8,000,000 + 88x14,200 + 10x75x1,000 = 9,999,600
    assert "9,999,600원" in text


def test_balance_survives_quote_failure(tmp_path, monkeypatch):
    import json

    import tg_bridge

    p = tmp_path / "portfolio.json"
    p.write_text(json.dumps({"cash": 100.0, "positions": {
        "069500": {"qty": 1, "avg_cost": 10000.0, "meta": {}}}}), encoding="utf-8")
    monkeypatch.setattr(tg_bridge, "PORTFOLIO_STATE_PATH", p)
    monkeypatch.setattr(tg_bridge, "WATCHLIST_PATH", tmp_path / "none.yaml")

    class _Broken:
        def prices(self, symbols):
            raise RuntimeError("api down")

        def usd_krw(self):
            raise RuntimeError("api down")

    text = tg_bridge.handle_balance(_Broken())
    assert "평단가로 근사" in text
    assert "총 추정자산 10,100원" in text


# ---------------------------------------------------------------------------
# 오늘의 전적 (/status add-on) — 2026-08-10
# ---------------------------------------------------------------------------
def _write_ledger(tmp_path, rows):
    import json
    p = tmp_path / "trades.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return p


def test_daily_record_realized_and_unrealized(tmp_path, monkeypatch):
    import json
    from datetime import datetime, timedelta, timezone

    import tg_bridge

    now = datetime.now(timezone.utc)
    yesterday = now - timedelta(days=2)
    ledger = _write_ledger(tmp_path, [
        # 오늘 매수 — 실현 손익에 포함되면 안 됨
        {"ts": now.isoformat(), "strategy_id": "intraday_scan", "symbol": "042660",
         "side": "buy", "qty": 4, "price": 92323.0, "fee": 55.4, "realized_pnl": 0.0},
        # 오늘 매도 (KR 손실): -12,000 - 수수료 340 = -12,340원
        {"ts": now.isoformat(), "strategy_id": "intraday_scan", "symbol": "196170",
         "side": "sell", "qty": 10, "price": 350000.0, "fee": 340.0, "realized_pnl": -12000.0},
        # 오늘 매도 (US 이익): +10.00달러 → 환율 1,000원 기준 +10,000원
        {"ts": now.isoformat(), "strategy_id": "donchian", "symbol": "TQQQ",
         "side": "sell", "qty": 5, "price": 80.0, "fee": 0.0, "realized_pnl": 10.0},
        # 이틀 전 매도 — 오늘의 전적에서 제외
        {"ts": yesterday.isoformat(), "strategy_id": "orb_scan", "symbol": "005930",
         "side": "sell", "qty": 1, "price": 70000.0, "fee": 10.0, "realized_pnl": 999999.0},
    ])
    monkeypatch.setattr(tg_bridge, "LEDGER_PATH", ledger)

    portfolio = {"cash": 1_000_000.0, "positions": {
        "088350": {"qty": 100, "avg_cost": 4600.0, "meta": {}},
    }}
    p = tmp_path / "portfolio.json"
    p.write_text(json.dumps(portfolio), encoding="utf-8")
    monkeypatch.setattr(tg_bridge, "PORTFOLIO_STATE_PATH", p)
    monkeypatch.setattr(tg_bridge, "WATCHLIST_PATH", tmp_path / "none.yaml")

    class _Toss:
        def prices(self, symbols):
            return [{"symbol": "088350", "price": 4700.0}]

        def usd_krw(self):
            return 1000.0

    text = tg_bridge.handle_daily_record(_Toss())
    assert "오늘의 전적" in text
    assert "-12,340원" in text, "KR 매도: realized -12,000 - fee 340"
    assert "장중 신고가 돌파" in text, "전략 라벨 표기"
    assert "+10.00달러" in text, "US 매도는 달러로 표기"
    assert "005930" not in text, "이틀 전 매도는 제외"
    # 보유 평가: (4700-4600)x100 = +10,000원
    assert "+10,000원 (+2.17%)" in text
    # 실현 합계 = -12,340 + 10x1,000 = -2,340 / 평가 +10,000 / 합계 +7,660
    assert "실현 합계 -2,340원" in text
    assert "평가 합계 +10,000원" in text
    assert "+7,660원" in text


def test_daily_record_empty_day(tmp_path, monkeypatch):
    import tg_bridge

    monkeypatch.setattr(tg_bridge, "LEDGER_PATH", tmp_path / "no_ledger.jsonl")
    monkeypatch.setattr(tg_bridge, "PORTFOLIO_STATE_PATH", tmp_path / "no_portfolio.json")
    monkeypatch.setattr(tg_bridge, "WATCHLIST_PATH", tmp_path / "none.yaml")

    class _Toss:
        def prices(self, symbols):
            return []

        def usd_krw(self):
            return 1000.0

    text = tg_bridge.handle_daily_record(_Toss())
    assert "오늘 청산한 종목 없음" in text
    assert "보유 종목 없음" in text
    assert "오늘 합계" in text


def test_status_appends_daily_record_when_toss_available(tmp_path, monkeypatch):
    import tg_bridge
    from quant.trade.control import TradingControl

    monkeypatch.setattr(tg_bridge, "LEDGER_PATH", tmp_path / "no_ledger.jsonl")
    monkeypatch.setattr(tg_bridge, "PORTFOLIO_STATE_PATH", tmp_path / "no_portfolio.json")
    monkeypatch.setattr(tg_bridge, "WATCHLIST_PATH", tmp_path / "none.yaml")
    control = TradingControl(state_path=tmp_path / "control.json")

    class _Toss:
        def prices(self, symbols):
            return []

        def usd_krw(self):
            return 1000.0

    with_toss = tg_bridge.handle_control_command("/status", control, _Toss())
    assert "오늘의 전적" in with_toss

    # toss_client 없이 호출(하위 호환) — 전적 섹션 없이 기존 형식 유지
    without = tg_bridge.handle_control_command("/status", control)
    assert "거래상태" in without
    assert "오늘의 전적" not in without


def test_parse_quote_rows_accepts_real_server_lastprice_shape():
    """실서버 Toss 응답은 lastPrice(문자열) — 2026-08-10 실측 회귀 가드."""
    import tg_bridge

    rows = [
        {"symbol": "005380", "lastPrice": "406500", "currency": "KRW"},
        {"symbol": "229200", "price": 14670.0},
        {"symbol": "TQQQ", "close": 75.5},
        {"symbol": "BAD", "lastPrice": "not-a-number"},
        {"noSymbol": True},
    ]
    quotes = tg_bridge._parse_quote_rows(rows)
    assert quotes == {"005380": 406500.0, "229200": 14670.0, "TQQQ": 75.5}


def test_daily_record_treats_kr_open_to_us_close_as_one_day(tmp_path, monkeypatch):
    """거래일 = KR 개장 ~ 다음날 US 마감. 달력 자정으로 쪼개지면 안 된다
    (2026-08-11 사용자 정의)."""
    import json
    from datetime import datetime, timezone

    import tg_bridge

    # 2026-08-10(월) KR 세션 매도 = 06:19 UTC = 15:19 KST 월요일
    kr_sell = "2026-08-10T06:19:59+00:00"
    # 같은 거래일의 US 세션 매도 = 19:58 UTC 월 = 04:58 KST 화요일 (달력상 다음날)
    us_sell = "2026-08-10T19:58:56+00:00"
    # 다음 거래일 KR 세션 매도 = 2026-08-11 01:22 UTC = 10:22 KST 화요일
    next_day_sell = "2026-08-11T01:22:46+00:00"

    ledger = _write_ledger(tmp_path, [
        {"ts": kr_sell, "strategy_id": "orb_scan", "symbol": "229200",
         "side": "sell", "qty": 88, "price": 14796.3, "fee": 195.31, "realized_pnl": 73551.35},
        {"ts": us_sell, "strategy_id": "intraday_scan", "symbol": "GLD",
         "side": "sell", "qty": 1.8, "price": 402.5, "fee": 0.72, "realized_pnl": 4.98},
        {"ts": next_day_sell, "strategy_id": "intraday_scan", "symbol": "058610",
         "side": "sell", "qty": 1, "price": 114471.0, "fee": 0.17, "realized_pnl": -7.36},
    ])
    monkeypatch.setattr(tg_bridge, "LEDGER_PATH", ledger)
    monkeypatch.setattr(tg_bridge, "PORTFOLIO_STATE_PATH", tmp_path / "none.json")
    monkeypatch.setattr(tg_bridge, "WATCHLIST_PATH", tmp_path / "none.yaml")

    class _Toss:
        def prices(self, symbols):
            return []

        def usd_krw(self):
            return 1000.0

    # 화요일 새벽 04:00 KST에 조회 — 아직 월요일 거래일이다
    now = datetime(2026, 8, 11, 4, 0, tzinfo=timezone(tg_bridge.KST.utcoffset(None)))
    text = tg_bridge.handle_daily_record(_Toss(), now=now)
    assert "229200" in text, "월요일 KR 매도가 같은 거래일에 있어야 한다"
    assert "GLD" in text, "화요일 새벽 US 매도도 같은 거래일이다"
    assert "058610" not in text, "화요일 오전 KR 매도는 다음 거래일"


def test_trading_day_boundary_is_kst_0800():
    from datetime import datetime, timedelta, timezone

    from quant.core.models import trading_day

    kst = timezone(timedelta(hours=9))
    # 월 09:00 개장 → 월
    assert trading_day(datetime(2026, 8, 10, 9, 0, tzinfo=kst)).isoformat() == "2026-08-10"
    # 화 05:00 US 마감 → 여전히 월
    assert trading_day(datetime(2026, 8, 11, 5, 0, tzinfo=kst)).isoformat() == "2026-08-10"
    # 화 07:59 → 아직 월
    assert trading_day(datetime(2026, 8, 11, 7, 59, tzinfo=kst)).isoformat() == "2026-08-10"
    # 화 08:00 → 새 거래일
    assert trading_day(datetime(2026, 8, 11, 8, 0, tzinfo=kst)).isoformat() == "2026-08-11"


# ============================ 시장별 상한 (2026-08-11) — US 자동 편입이 막히던 문제

def test_watchlist_cap_is_per_market_so_us_is_not_blocked_by_kr_entries(tmp_path):
    """실측 배경: 전역 상한 20에서 KR 7 + US 12 = 19가 차서 US 자동 편입이
    1종목만 들어가고 나머지가 막혔다. 두 시장은 거래 시간이 달라 같은 예산을 놓고
    경쟁할 이유가 없다."""
    import tg_bridge

    path = tmp_path / "watchlist.yaml"
    cap = tg_bridge.WATCHLIST_CAP_PER_MARKET

    class _Toss:
        def stock_info(self, symbol):
            return {"name": f"이름{symbol}"}

    # KR을 상한까지 채운다
    kr = " ".join(f"{i:06d}" for i in range(1, cap + 1))
    tg_bridge._handle_watch_unlocked(kr, _Toss(), path)
    entries = tg_bridge.load_watchlist(path)
    assert len(entries) == cap

    # KR이 꽉 찼어도 US는 그대로 들어가야 한다
    out = tg_bridge._handle_watch_unlocked("NVDA TSLA", _Toss(), path)
    symbols = {e["symbol"] for e in tg_bridge.load_watchlist(path)}
    assert {"NVDA", "TSLA"} <= symbols, out
    assert "초과로 미등록" not in out

    # KR 추가분은 상한에 막힌다
    blocked = tg_bridge._handle_watch_unlocked("999999", _Toss(), path)
    assert "초과로 미등록" in blocked and "999999" in blocked


def test_us_cap_blocks_only_us_and_reports_the_market_limit(tmp_path):
    import tg_bridge

    path = tmp_path / "watchlist.yaml"
    cap = tg_bridge.WATCHLIST_CAP_PER_MARKET

    class _Toss:
        def stock_info(self, symbol):
            return {"name": ""}

    us = " ".join(f"SYM{i}" for i in range(cap))
    tg_bridge._handle_watch_unlocked(us, _Toss(), path)
    out = tg_bridge._handle_watch_unlocked("AAPL 005930", _Toss(), path)

    symbols = {e["symbol"] for e in tg_bridge.load_watchlist(path)}
    assert "005930" in symbols, "US가 꽉 차도 KR은 들어가야 한다"
    assert "AAPL" not in symbols
    assert f"시장별 상한({cap}종목)" in out


# ==================== 시장별 초기화 (2026-08-11: 개장 전 정기 청소)

def _seed(path, symbols):
    import tg_bridge
    tg_bridge.save_watchlist(
        [{"symbol": s, "name": "", "added_at": datetime.now().isoformat(), "source": "user"}
         for s in symbols],
        path,
    )


def test_market_reset_removes_only_that_market(tmp_path):
    """KR 개장 전 청소가 사용자가 넣어둔 US 종목을 날리면 안 된다 — 두 시장은
    개장 시각이 다르므로 청소 시각도 달라야 한다."""
    import tg_bridge

    path = tmp_path / "watchlist.yaml"
    _seed(path, ["005930", "069500", "NVDA", "TQQQ"])

    out = tg_bridge.handle_watchlist_reset(path=path, market="KR")
    left = [e["symbol"] for e in tg_bridge.load_watchlist(path)]
    assert left == ["NVDA", "TQQQ"], out
    assert "한국" in out and "2개 삭제" in out
    assert "미국" not in out.split("\n")[0]

    out2 = tg_bridge.handle_watchlist_reset(path=path, market="US")
    assert tg_bridge.load_watchlist(path) == []
    assert "미국" in out2


def test_market_reset_is_idempotent_and_says_so(tmp_path):
    import tg_bridge

    path = tmp_path / "watchlist.yaml"
    _seed(path, ["NVDA"])
    tg_bridge.handle_watchlist_reset(path=path, market="KR")
    again = tg_bridge.handle_watchlist_reset(path=path, market="KR")
    assert "이미 비어" in again
    assert [e["symbol"] for e in tg_bridge.load_watchlist(path)] == ["NVDA"], "US는 그대로"


def test_global_reset_still_clears_everything(tmp_path):
    """market 미지정 = 기존 동작(전체 삭제) 보존."""
    import tg_bridge

    path = tmp_path / "watchlist.yaml"
    _seed(path, ["005930", "NVDA"])
    tg_bridge.handle_watchlist_reset(path=path)
    assert tg_bridge.load_watchlist(path) == []


def test_market_reset_preserves_accumulation_tags(tmp_path):
    """시장별 리셋이 FRGN/FRGN_EXIT 태그 항목을 보존한다 (2026-08-18 P1).

    왜: 08:05 크론 리셋이 태그 불문 전삭제면 08:12 own_brief 의 FRGN_EXIT
    갱신(--tags-only, 이미 등록된 종목만)이 항상 빈 목록을 만나 적립 전략의
    이탈 신호가 유실된다 — 2단계 분할 청산은 태그가 연속으로 보여야 작동한다.
    """
    from datetime import datetime

    import tg_bridge

    path = tmp_path / "watchlist.yaml"
    now = datetime.now().isoformat()
    tg_bridge.save_watchlist(
        [
            {"symbol": "005930", "name": "", "added_at": now, "source": "auto",
             "tags": ["FRGN"]},
            {"symbol": "000660", "name": "", "added_at": now, "source": "auto",
             "tags": ["FRGN_EXIT"]},
            {"symbol": "069500", "name": "", "added_at": now, "source": "auto",
             "tags": ["EVENT_SCALP"]},
            {"symbol": "035420", "name": "", "added_at": now, "source": "user"},
            {"symbol": "NVDA", "name": "", "added_at": now, "source": "user"},
        ],
        path,
    )

    out = tg_bridge.handle_watchlist_reset(path=path, market="KR")
    left = {e["symbol"] for e in tg_bridge.load_watchlist(path)}
    # 적립 태그 2종 + 타 시장(US)만 남는다 — 단타 태그(EVENT_SCALP)·무태그는 청소된다.
    assert left == {"005930", "000660", "NVDA"}, out
    assert "적립 태그 보존 2개" in out

    # 전체 수동 리셋은 여전히 전삭제(명시적 사용자 명령의 무게 유지).
    tg_bridge.handle_watchlist_reset(path=path)
    assert tg_bridge.load_watchlist(path) == []


def test_market_reset_expires_stale_accumulation_tags(tmp_path):
    """ACCUM_TAG_TTL_DAYS(7일)를 넘게 tags_updated_at 갱신이 없으면 적립 태그도
    청소된다 -- own_brief가 매일 살아있는 FRGN 신호에 태그를 다시 붙이므로,
    갱신이 끊겼다는 것은 신호가 끝났다는 뜻이다(2026-08-18 P1)."""
    import tg_bridge

    path = tmp_path / "watchlist.yaml"
    now = datetime.now()
    fresh = (now - timedelta(days=1)).isoformat(timespec="seconds")
    stale = (now - timedelta(days=tg_bridge.ACCUM_TAG_TTL_DAYS + 1)).isoformat(timespec="seconds")
    tg_bridge.save_watchlist(
        [
            {"symbol": "005930", "name": "", "added_at": stale, "source": "auto",
             "tags": ["FRGN"], "tags_updated_at": fresh},
            {"symbol": "000660", "name": "", "added_at": stale, "source": "auto",
             "tags": ["FRGN"], "tags_updated_at": stale},
        ],
        path,
    )

    out = tg_bridge.handle_watchlist_reset(path=path, market="KR")
    left = {e["symbol"] for e in tg_bridge.load_watchlist(path)}
    assert left == {"005930"}, out
    assert "적립 태그 보존 1개" in out


def test_market_reset_preserve_cap_keeps_most_recent_and_reports_cull(tmp_path):
    """ACCUM_PRESERVE_MAX를 넘는 적립 태그는 tags_updated_at 최신순으로만 남기고
    나머지는 청소하며, 절삭 개수를 메시지에 표시한다(조용히 버리지 않는다)."""
    import tg_bridge

    path = tmp_path / "watchlist.yaml"
    now = datetime.now()
    cap = tg_bridge.ACCUM_PRESERVE_MAX
    extra = 3
    total = cap + extra
    entries = []
    for i in range(total):
        entries.append({
            "symbol": f"{100000 + i:06d}",
            "name": "",
            "added_at": now.isoformat(timespec="seconds"),
            "source": "auto",
            "tags": ["FRGN"],
            # i=0이 가장 최신(now), i가 클수록 더 과거 -- 전부 TTL 이내로 신선.
            "tags_updated_at": (now - timedelta(minutes=i)).isoformat(timespec="seconds"),
        })
    tg_bridge.save_watchlist(entries, path)

    out = tg_bridge.handle_watchlist_reset(path=path, market="KR")
    left = tg_bridge.load_watchlist(path)
    left_symbols = {e["symbol"] for e in left}
    # 가장 최신 cap개(i=0..cap-1)만 남는다.
    expected = {f"{100000 + i:06d}" for i in range(cap)}
    assert left_symbols == expected, out
    assert len(left) == cap
    assert f"보존 상한 초과 {extra}개 절삭" in out


def test_legacy_accum_entry_without_tags_updated_at_falls_back_to_added_at(tmp_path):
    """tags_updated_at 필드가 없는 구버전 항목은 added_at으로 신선도를 판정한다
    (하위호환 -- 마이그레이션 스크립트 없이 이 필드 도입 이전 항목도 처리돼야 한다)."""
    import tg_bridge

    path = tmp_path / "watchlist.yaml"
    now = datetime.now()
    fresh_added = (now - timedelta(days=1)).isoformat(timespec="seconds")
    stale_added = (now - timedelta(days=tg_bridge.ACCUM_TAG_TTL_DAYS + 1)).isoformat(timespec="seconds")
    tg_bridge.save_watchlist(
        [
            {"symbol": "005930", "name": "", "added_at": fresh_added, "source": "auto",
             "tags": ["FRGN"]},  # tags_updated_at 없음 -- added_at으로 신선 판정돼야 보존
            {"symbol": "000660", "name": "", "added_at": stale_added, "source": "auto",
             "tags": ["FRGN"]},  # tags_updated_at 없음 -- added_at으로 만료 판정돼야 청소
        ],
        path,
    )

    out = tg_bridge.handle_watchlist_reset(path=path, market="KR")
    left = {e["symbol"] for e in tg_bridge.load_watchlist(path)}
    assert left == {"005930"}, out


def test_watch_add_stamps_tags_updated_at_on_new_registration_and_merge(tmp_path):
    """watch-add가 태그를 병합하거나 신규 등록할 때 tags_updated_at을 남긴다 --
    시장별 리셋의 적립 태그 보존이 이 값으로 신선도를 판정하므로, 도장이 안 찍히면
    own_brief의 매일 재태깅이 신선도를 못 갱신해 보존이 무력화된다."""
    import time

    import tg_bridge

    class _Toss:
        def stock_info(self, symbol):
            return {"name": ""}

    path = tmp_path / "watchlist.yaml"

    # 신규 등록 시 도장
    tg_bridge._handle_watch_unlocked("005930", _Toss(), path, source="auto", tags=["FRGN"])
    entries = {e["symbol"]: e for e in tg_bridge.load_watchlist(path)}
    assert entries["005930"].get("tags_updated_at"), "신규 등록에 tags_updated_at이 없다"
    first_stamp = entries["005930"]["tags_updated_at"]

    # 이미 있는 심볼에 같은 태그를 재부여(병합)해도 도장은 갱신돼야 한다 -- own_brief가
    # 매일 살아있는 신호에 같은 태그를 다시 붙이는 것 자체가 "신호 생존"의 근거다.
    time.sleep(1.1)  # timespec="seconds" 해상도라 확실히 다른 초를 보장
    tg_bridge._handle_watch_unlocked("005930", _Toss(), path, source="auto", tags=["FRGN"])
    entries = {e["symbol"]: e for e in tg_bridge.load_watchlist(path)}
    second_stamp = entries["005930"]["tags_updated_at"]
    assert second_stamp != first_stamp, "동일 태그 재적용도 신선도 갱신을 위해 도장을 다시 찍어야 한다"

    # 다른 태그 병합 시에도 도장 + 태그 합집합 갱신
    time.sleep(1.1)
    tg_bridge._handle_watch_unlocked("005930", _Toss(), path, source="auto", tags=["FRGN_EXIT"])
    entries = {e["symbol"]: e for e in tg_bridge.load_watchlist(path)}
    assert set(entries["005930"]["tags"]) == {"FRGN", "FRGN_EXIT"}
    assert entries["005930"]["tags_updated_at"] != second_stamp


def test_telegram_market_reset_commands_route(tmp_path, monkeypatch):
    import tg_bridge

    path = tmp_path / "watchlist.yaml"
    monkeypatch.setattr(tg_bridge, "WATCHLIST_PATH", path)
    _seed(path, ["005930", "NVDA"])

    class _Toss:
        pass

    tg_bridge.handle_watchlist_command("/watchlist-reset-kr", _Toss())
    assert [e["symbol"] for e in tg_bridge.load_watchlist(path)] == ["NVDA"]
    tg_bridge.handle_watchlist_command("/watchlist-reset-us", _Toss())
    assert tg_bridge.load_watchlist(path) == []


def test_per_market_cap_is_twenty_each(tmp_path):
    """2026-08-11 사용자 지시: 한국 20 / 미국 20 각각."""
    import tg_bridge

    assert tg_bridge.WATCHLIST_CAP_PER_MARKET == 20
    path = tmp_path / "watchlist.yaml"

    class _Toss:
        def stock_info(self, symbol):
            return {"name": ""}

    kr = " ".join(f"{i:06d}" for i in range(1, 21))
    us = " ".join(f"S{i}" for i in range(20))
    tg_bridge._handle_watch_unlocked(kr, _Toss(), path)
    tg_bridge._handle_watch_unlocked(us, _Toss(), path)
    assert len(tg_bridge.load_watchlist(path)) == 40, "시장별 20 = 합계 40까지 허용"


# ============ watch-add 인자 파싱 회귀 (2026-08-11 실운영 사고)

def test_source_flag_is_accepted_after_symbols_not_swallowed_as_one():
    """us_watch_discover.sh가 `watch-add INTC --source auto` 형태로 불렀는데
    파서가 `--source`를 앞자리에서만 인식해 `--SOURCE`와 `AUTO`가 **심볼로 등록**됐다.
    `AUTO`는 실재 종목(오토웹)이라 파싱 버그 하나로 엉뚱한 종목을 거래할 뻔했다."""
    import tg_bridge

    source, tags, tags_only, arg = tg_bridge._parse_watch_add_argv(["INTC", "--source", "auto"])
    assert source == "auto"
    assert arg == "INTC", "--source/auto가 심볼 목록에 섞이면 안 된다"


def test_source_flag_still_works_in_leading_position():
    import tg_bridge

    source, tags, tags_only, arg = tg_bridge._parse_watch_add_argv(["--source", "auto", "NVDA", "QQQ"])
    assert source == "auto"
    assert arg == "NVDA QQQ"


def test_unknown_double_dash_option_is_rejected_not_treated_as_symbol():
    """모르는 `--` 인자를 심볼로 삼키면 같은 부류의 사고가 반복된다."""
    import tg_bridge
    import pytest as _pytest

    with _pytest.raises(tg_bridge._WatchAddUsageError):
        tg_bridge._parse_watch_add_argv(["NVDA", "--bogus"])


def test_source_flag_without_value_is_a_usage_error():
    import tg_bridge
    import pytest as _pytest

    with _pytest.raises(tg_bridge._WatchAddUsageError):
        tg_bridge._parse_watch_add_argv(["NVDA", "--source"])
