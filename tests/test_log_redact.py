"""로그 시크릿 마스킹 — 실제 유출 사례를 회귀로 고정한다.

2026-08-13 실측: `journalctl -u quant-engine` 에 텔레그램 봇 토큰이 이틀간 381번
평문으로 남아 있었다. httpx 가 요청 URL 을 INFO 로 찍는데 텔레그램은 토큰을
경로에 담기 때문이다. 같은 측정에서 키움·토스 앱키는 0건이었다.
"""
import logging

from quant.core.log_redact import (
    REDACTED,
    SecretRedactingFilter,
    install,
    known_secrets,
    redact,
)

_REAL_SHAPE_TOKEN = "1234567890:AAfakeShapeOnlyTokenForRedactTest000000"


# --- 형태 기반 (런타임 발급 토큰은 값 목록에 없다) ---

def test_telegram_token_in_url_is_masked():
    """실제 유출 사례 그대로."""
    line = (f'HTTP Request: POST https://api.telegram.org/bot{_REAL_SHAPE_TOKEN}'
            f'/sendMessage "HTTP/1.1 200 OK"')
    out = redact(line, secrets=[])
    assert _REAL_SHAPE_TOKEN not in out
    assert "api.telegram.org/bot" + REDACTED in out
    assert "200 OK" in out, "진단에 필요한 나머지는 남아야 한다"


def test_bearer_token_is_masked():
    out = redact("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abcdefghijklmnop", secrets=[])
    assert "eyJhbGci" not in out and REDACTED in out


def test_appkey_in_body_is_masked():
    out = redact('{"appkey": "ABCDEFGHIJKLMNOP1234", "x": 1}', secrets=[])
    assert "ABCDEFGHIJKLMNOP1234" not in out


def test_ordinary_request_logs_survive_untouched():
    """로그를 끄는 게 아니라 값만 가린다 — 진단 능력을 잃으면 안 된다."""
    line = 'HTTP Request: POST https://api.kiwoom.com/api/dostk/stkinfo "HTTP/1.1 200 OK"'
    assert redact(line, secrets=[]) == line


# --- 값 기반 (형태를 몰라도 잡는다) ---

def test_known_secret_value_is_masked_anywhere():
    secret = "SUPERSECRETAPPKEY12345"
    out = redact(f"키움 클라이언트 생성 실패: key={secret} 확인", secrets=[secret])
    assert secret not in out and REDACTED in out


def test_secrets_are_collected_by_env_name():
    env = {
        "TELEGRAM_BOT_TOKEN": "abcdefghijklmnop",
        "KIWOOM_APP_KEY": "qrstuvwxyz012345",
        "TOSS_CLIENT_SECRET": "secretsecretsecret",
        "KIWOOM_BASE_URL": "https://api.kiwoom.com",  # 시크릿 아님
        "MODE": "paper",
    }
    got = set(known_secrets(env))
    assert got == {"abcdefghijklmnop", "qrstuvwxyz012345", "secretsecretsecret"}


def test_short_values_are_not_masked():
    """짧은 값까지 치환하면 로그가 망가진다 — TOKEN 이름의 설정값이 있을 수 있다."""
    assert known_secrets({"SOME_TOKEN": "N"}) == []


def test_longest_secret_is_replaced_first():
    """짧은 시크릿이 긴 시크릿의 부분문자열이면 조각이 남는다."""
    short, long = "ABCDEFGHIJKL", "ABCDEFGHIJKLMNOPQRST"
    out = redact(f"key={long}", secrets=known_secrets({"A_TOKEN": short, "B_TOKEN": long}))
    assert "MNOPQRST" not in out


# --- 로깅 배선 ---

def test_filter_is_installed_on_handlers_not_loggers(caplog):
    """유출이 httpx 로거였다 — 핸들러에 걸어야 전파된 레코드까지 잡는다."""
    logger = logging.getLogger("test_redact_wiring")
    handler = logging.StreamHandler()
    logger.addHandler(handler)
    try:
        assert install(logger) == 1
        assert any(isinstance(f, SecretRedactingFilter) for f in handler.filters)
        assert install(logger) == 0, "중복 설치되면 안 된다"
    finally:
        logger.removeHandler(handler)


def test_filter_rewrites_the_record_message():
    f = SecretRedactingFilter()
    rec = logging.LogRecord(
        "httpx", logging.INFO, __file__, 1,
        "HTTP Request: POST https://api.telegram.org/bot%s/sendMessage",
        (_REAL_SHAPE_TOKEN,), None,
    )
    assert f.filter(rec) is True
    assert _REAL_SHAPE_TOKEN not in rec.getMessage()


def test_broken_format_does_not_kill_logging():
    """포맷 실패가 로깅을 죽이면 안 된다 — 로그가 사라지는 게 더 위험하다."""
    f = SecretRedactingFilter()
    rec = logging.LogRecord("x", logging.INFO, __file__, 1, "%d 개", ("숫자아님",), None)
    assert f.filter(rec) is True


# --- .env.local 전용 시크릿 (os.environ을 채우지 않는 get_key() 경로) ---


def test_dart_api_key_query_string_is_masked_by_shape():
    """DART `crtfc_key`는 값 목록 없이도(형태 기반) 잡혀야 한다 — 실제 유출 사례
    (httpx INFO 로그의 `?crtfc_key=<평문>`)를 값을 몰라도 막는 두 번째 방어선."""
    fake_key = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"
    line = (
        f"HTTP Request: GET https://opendart.fss.or.kr/api/list.json"
        f"?crtfc_key={fake_key}&bgn_de=20260101 \"HTTP/1.1 200 OK\""
    )
    out = redact(line, secrets=[])
    assert fake_key not in out
    assert "crtfc_key=" + REDACTED in out
    assert "bgn_de=20260101" in out, "다른 쿼리 파라미터는 진단용으로 남아야 한다"


def test_env_local_only_secret_is_masked_when_injected_as_extra():
    """`quant.adapters.env.get_key()`는 `.env.local`만 읽고 `os.environ`을 채우지
    않는다 — 이런 값은 `known_secrets(env=...)`로 뽑아 `install(extra_secrets=...)`로
    주입해야 마스킹된다는 배선 계약을 고정한다."""
    dotenv_only_secret = "DEADBEEFCAFEDEADBEEFCAFE12345"
    dotenv_values = {"DART_API_KEY": dotenv_only_secret}
    extra = known_secrets(env=dotenv_values)
    assert extra == [dotenv_only_secret]

    logger = logging.getLogger("test_redact_env_local_only")
    handler = logging.StreamHandler()
    logger.addHandler(handler)
    try:
        install(logger, extra_secrets=extra)
        rec = logging.LogRecord(
            "httpx", logging.INFO, __file__, 1,
            f"GET https://opendart.fss.or.kr/api/list.json?crtfc_key={dotenv_only_secret}",
            (), None,
        )
        f = next(fl for fl in handler.filters if isinstance(fl, SecretRedactingFilter))
        assert f.filter(rec) is True
        assert dotenv_only_secret not in rec.getMessage()
        assert REDACTED in rec.getMessage()
    finally:
        logger.removeHandler(handler)


def test_filter_picks_up_os_environ_secrets_added_after_install(monkeypatch):
    """예전 구현은 생성 시점에 시크릿을 캐시해, `install()`이 로깅 초기화 직후(값
    로드 전) 한 번 불리면 이후 `os.environ`이 채워져도 영영 못 잡았다 — "설치는
    됐지만 배선은 안 된" 상태를 회귀로 고정한다."""
    monkeypatch.delenv("TEST_LATE_TOKEN", raising=False)
    f = SecretRedactingFilter()  # os.environ에 아직 시크릿이 없을 때 생성

    late_secret = "LATELOADEDVALUE1234567890"
    monkeypatch.setenv("TEST_LATE_TOKEN", late_secret)

    rec = logging.LogRecord(
        "x", logging.INFO, __file__, 1, f"key={late_secret}", (), None,
    )
    assert f.filter(rec) is True
    assert late_secret not in rec.getMessage()


def test_install_merges_extra_secrets_into_already_installed_filter():
    """두 번째 `install()` 호출이 no-op으로 무시되더라도, `extra_secrets`를 주면
    기존 필터에 병합돼야 한다(중복 설치 방지와 시크릿 갱신은 별개 관심사)."""
    logger = logging.getLogger("test_redact_merge")
    handler = logging.StreamHandler()
    logger.addHandler(handler)
    try:
        assert install(logger) == 1
        secret = "MERGEDSECRETVALUE1234567"
        assert install(logger, extra_secrets=[secret]) == 0, "재설치는 여전히 0"
        f = next(fl for fl in handler.filters if isinstance(fl, SecretRedactingFilter))
        rec = logging.LogRecord("x", logging.INFO, __file__, 1, f"v={secret}", (), None)
        f.filter(rec)
        assert secret not in rec.getMessage()
    finally:
        logger.removeHandler(handler)
