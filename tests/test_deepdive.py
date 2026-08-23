"""오케스트레이터 — 모든 I/O 를 주입해 밀폐로 검증한다.
핵심 계약: LLM 이 죽으면(narrate→None) 신규 발견만 멈추고 기존 스냅샷은 보존."""
import json
from quant.apps.deepdive import run_deepdive


class _FakeNarrator:
    def __init__(self, text):
        self._t = text

    def narrate(self, prompt):
        return self._t


def _seed_news(root, market, day, titles):
    d = root / "data" / "news" / market
    d.mkdir(parents=True, exist_ok=True)  # 여러 날짜를 같은 market 아래 순차로 심는다
    rows = [{"key": f"k{i}", "title": t, "link": f"http://n/{i}",
             "outlet": "테스트", "feed": "f", "published": None,
             "published_known": False, "first_seen": f"{day}T09:00:00",
             "last_seen": f"{day}T09:00:00", "seen_count": 1}
            for i, t in enumerate(titles)]
    (d / f"{day}.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows))


def test_llm_down_preserves_existing_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr("quant.apps.deepdive._name_maps",
                        lambda root, market: ({"삼성전자": "005930"}, {"005930": "삼성전자"}))
    led = tmp_path / "data" / "ledger"
    led.mkdir(parents=True)
    (led / "relations.json").write_text(json.dumps({"005930": [{"dst": "042700"}]}))
    _seed_news(tmp_path, "KR", "2026-08-15", ["삼성전자 HBM4 뉴스"])
    # 본문 fetch 는 성공해야 한다(I1a: 본문 실패면 그 소스가 LLM 호출 없이
    # 스킵되므로, 이 테스트가 확인하려는 "narrator 자체가 죽었다"와 안 섞이려면
    # getter 는 유효한 본문을 돌려줘야 한다).
    stats = run_deepdive("KR", tmp_path, _FakeNarrator(None),
                         getter=lambda url: "<p>" + "본문" * 20 + "</p>", today="2026-08-15")
    assert stats["skipped_llm"] is True
    kept = json.loads((led / "relations.json").read_text())
    assert "005930" in kept  # 어제 사전이 지워지지 않았다


def test_deepdive_does_not_treat_substring_name_as_top_source(tmp_path, monkeypatch):
    """실측 회귀(2026-08-15) — '이닉스'(452400)가 'SK하이닉스'(000660)의
    부분문자열이라, 마스킹 없이는 SK하이닉스 헤드라인 5건을 전부 이닉스
    언급으로 잘못 세 소스 선정 1위가 됐다. relations.json 에
    이닉스→SK하이닉스[경쟁사] 가 들어갔고 reason 문장조차 SK하이닉스
    서술이었다(LLM 이 애초에 SK하이닉스 기사를 읽었다는 뜻)."""
    monkeypatch.setattr(
        "quant.apps.deepdive._name_maps",
        lambda root, market: (
            {"이닉스": "452400", "SK하이닉스": "000660"},
            {"452400": "이닉스", "000660": "SK하이닉스"},
        ),
    )
    _seed_news(tmp_path, "KR", "2026-08-15",
              ["SK하이닉스 급등", "SK하이닉스 실적 발표", "SK하이닉스 신고가",
               "SK하이닉스 목표가 상향", "SK하이닉스 HBM 증설"])
    prompts: list[str] = []

    class _RecordingNarrator:
        def narrate(self, prompt):
            prompts.append(prompt)
            return None

    stats = run_deepdive("KR", tmp_path, _RecordingNarrator(),
                         getter=lambda url: "<p>" + "본문" * 20 + "</p>",
                         today="2026-08-15")
    # 소스는 SK하이닉스 하나뿐이어야 한다 — 이닉스는 이 코퍼스에 한 번도
    # 안 나온다(마스킹 전 코드였다면 5건으로 세어져 소스가 2개였을 것이다).
    assert stats["sources"] == 1
    assert len(prompts) == 1
    assert "SK하이닉스" in prompts[0]
    assert "이닉스" not in prompts[0].replace("SK하이닉스", "")


def test_src_name_falls_back_to_matching_table_outside_display_domain(tmp_path, monkeypatch):
    """I5 잔여 결함 — US 매칭 표(name_to_code = load_us_table)는 S&P500 밖
    티커(이 확장이 노리던 CoreWeave 류)도 포함하는데, 표시 표(code_to_name =
    load_name_map)는 S&P500 뿐이다. 그런 티커가 소스로 뽑히면 `code_to_name.
    get(src, src)` 가 원시 티커로 떨어지고, 제목에 티커가 그대로 나올 리
    없어 arts 가 조용히 0건이 된다. src_name 은 매칭 표(name_to_code 의
    역변환)로 폴백해야 한다."""
    monkeypatch.setattr(
        "quant.apps.deepdive._name_maps",
        lambda root, market: (
            {"CoreWeave": "CRWV"},  # 매칭 표 — S&P500 밖 티커도 포함
            {},                     # 표시 표 — S&P500 전용이라 CRWV 가 없다
        ),
    )
    _seed_news(tmp_path, "US", "2026-08-15",
              ["CoreWeave announces new datacenter deal"])
    fetched: list[str] = []

    def getter(url):
        fetched.append(url)
        return "<p>" + "body text here " * 10 + "</p>"

    stats = run_deepdive("US", tmp_path, _FakeNarrator(None),
                         getter=getter, today="2026-08-15")
    # 티커 폴백이었다면 "CRWV" 는 제목 문자열에 없어 fetch_body 가 한 번도
    # 안 불렸을 것이다 — 회사명("CoreWeave")으로 풀려 본문을 가져왔는지로 검증.
    assert fetched, "src_name 이 회사명으로 풀리지 않아 제목 매칭이 실패했다"
    assert stats["bodies_ok"] >= 1


def test_accepts_only_evidence_backed(tmp_path, monkeypatch):
    _seed_news(tmp_path, "KR", "2026-08-15",
               ["삼성전자 HBM4 조기 양산", "삼성전자·한미반도체 협력",
                "한미반도체 수주 확대", "한미반도체 장비 증설"])
    # name_map 로딩을 밀폐 — entities 캐시 대신 고정 사전
    monkeypatch.setattr("quant.apps.deepdive._name_maps",
                        lambda root, market: ({"한미반도체": "042700",
                                               "삼성전자": "005930"},
                                              {"005930": "삼성전자"}))
    llm = _FakeNarrator("한미반도체 | 수혜주 | HBM 본딩 장비\n"
                        "존재안함 | 수혜주 | 근거없음\n")
    stats = run_deepdive("KR", tmp_path, llm,
                         getter=lambda url: "<p>" + "삼성전자 HBM 본문" * 10 + "</p>",
                         today="2026-08-15")
    snap = json.loads((tmp_path / "data" / "ledger" / "relations.json").read_text())
    assert stats["accepted"] >= 1
    assert any(r["dst"] == "042700" for r in snap.get("005930", []))


def test_evidence_corpus_spans_seven_days(tmp_path, monkeypatch):
    """증거 코퍼스는 오늘 하루가 아니라 최근 7일 전체다(Task 3 계약).

    오늘 뉴스만으로는 '삼성전자' 언급이 0건이라 결정론 채점이 임계(50)를 못
    넘는다(오늘 하루치 코퍼스면 n=0 → 0점) — 지난 사흘치를 합쳐야 3건(기본
    40점) + 동시출현(+30) = 70으로 임계를 넘긴다. 하루치만 셌다면(수정 전
    코드) 이 후보는 거부됐어야 한다.
    """
    monkeypatch.setattr("quant.apps.deepdive._name_maps",
                        lambda root, market: ({"한미반도체": "042700",
                                               "삼성전자": "005930"},
                                              {"005930": "삼성전자",
                                               "042700": "한미반도체"}))
    # 오늘: 소스(한미반도체)만 언급 — 삼성전자는 오늘 뉴스에 없다.
    _seed_news(tmp_path, "KR", "2026-08-15", ["한미반도체 수주 확대"])
    # 이전 사흘: 삼성전자 단독 언급 2건 + 동시출현 1건 → 코퍼스 합산 3건.
    _seed_news(tmp_path, "KR", "2026-08-14", ["삼성전자 파운드리 투자 확대"])
    _seed_news(tmp_path, "KR", "2026-08-13", ["삼성전자 HBM4 조기 양산"])
    _seed_news(tmp_path, "KR", "2026-08-12", ["삼성전자·한미반도체 실적 협력"])
    llm = _FakeNarrator("삼성전자 | 수혜주 | HBM 협력\n")
    stats = run_deepdive("KR", tmp_path, llm,
                         getter=lambda url: "<p>" + "본문" * 20 + "</p>",
                         today="2026-08-15")
    snap = json.loads((tmp_path / "data" / "ledger" / "relations.json").read_text())
    assert stats["accepted"] == 1
    assert any(r["dst"] == "005930" and r["evidence_score"] == 70
              for r in snap.get("042700", []))


def _theme_fixture():
    """정유 테마 1개, 종목 2개 — 각자 다른 종목의 "수혜주 후보"가 되도록."""
    return {
        "10": {
            "name": "정유",
            "symbols": [
                {"code": "096770", "name": "SK이노베이션",
                 "reason": "종합에너지기업으로 정유사업을 영위", "change_pct": 1.0,
                 "value_traded": 900000},
                {"code": "078930", "name": "GS",
                 "reason": "정유업체 GS칼텍스를 손자회사로 보유", "change_pct": 2.0,
                 "value_traded": 500000},
            ],
        }
    }


def test_theme_artifact_produces_naver_theme_relation_with_via_theme(tmp_path, monkeypatch):
    """F Step1 ① — 테마 아티팩트(themes.json)가 있으면 source="naver_theme"
    관계가 생기고 via_theme(테마명)가 채워진다. reason 은 네이버 편입사유
    그대로다(LLM 문장이 아니다)."""
    monkeypatch.setattr(
        "quant.apps.deepdive._name_maps",
        lambda root, market: (
            {"SK이노베이션": "096770", "GS": "078930"},
            {"096770": "SK이노베이션", "078930": "GS"},
        ),
    )
    led = tmp_path / "data" / "ledger"
    led.mkdir(parents=True)
    (led / "themes.json").write_text(json.dumps(_theme_fixture(), ensure_ascii=False))
    # theme_search.hot_themes 자격 요건(2026-08-16 EC2 e2e 결함 수정: 매칭
    # 종목 수 ≥ 2, 우산 지수 테마의 절대 수 독식 방지)을 만족하려면 정유 테마
    # 2종목이 모두 뉴스에 매칭돼야 한다 — SK이노베이션 단독 언급만으로는
    # 테마 자체가 후보에서 탈락한다.
    _seed_news(tmp_path, "KR", "2026-08-15",
              ["SK이노베이션 실적 발표", "SK이노베이션 GS 협력"])
    run_deepdive("KR", tmp_path, _FakeNarrator(None),
                getter=lambda url: "<p>" + "본문" * 20 + "</p>",
                today="2026-08-15")
    snap = json.loads((led / "relations.json").read_text())
    theme_rows = [r for rs in snap.values() for r in rs if r["source"] == "naver_theme"]
    assert theme_rows, "테마 기반 관계가 하나도 생기지 않았다"
    assert all(r["via_theme"] == "정유" for r in theme_rows)
    reasons = {r["reason"] for r in theme_rows}
    assert reasons <= {"종합에너지기업으로 정유사업을 영위", "정유업체 GS칼텍스를 손자회사로 보유"}


def test_no_theme_artifact_falls_back_to_llm_path(tmp_path, monkeypatch):
    """F Step1 ② — 테마 수집·읽기가 모두 실패하면(themes.json 없음, 수집도
    실패) 기존 LLM 경로로 폴백해 여전히 관계가 나온다. 그 관계의 source 는
    "llm" 이어야 한다(테마가 만든 게 아니다)."""
    monkeypatch.setattr("quant.apps.deepdive._name_maps",
                        lambda root, market: ({"한미반도체": "042700",
                                               "삼성전자": "005930"},
                                              {"005930": "삼성전자",
                                               "042700": "한미반도체"}))
    _seed_news(tmp_path, "KR", "2026-08-15",
              ["삼성전자 HBM4 조기 양산", "삼성전자·한미반도체 협력",
               "한미반도체 수주 확대", "한미반도체 장비 증설"])
    llm = _FakeNarrator("한미반도체 | 수혜주 | HBM 본딩 장비\n")
    stats = run_deepdive("KR", tmp_path, llm,
                         getter=lambda url: "<p>" + "삼성전자 HBM 본문" * 10 + "</p>",
                         today="2026-08-15")
    snap = json.loads((tmp_path / "data" / "ledger" / "relations.json").read_text())
    assert stats["accepted"] >= 1
    assert any(r["dst"] == "042700" and r["source"] == "llm"
              for r in snap.get("005930", []))


def test_us_market_never_touches_theme_artifact(tmp_path, monkeypatch):
    """F Step1 ③ — US 는 테마를 보지 않는다. themes.json 이 있어도 절대 읽지
    않는다 — `_load_or_fetch_themes` 호출 자체가 없어야 한다(호출되면 즉시
    AssertionError)."""
    monkeypatch.setattr("quant.apps.deepdive._name_maps",
                        lambda root, market: ({"CoreWeave": "CRWV"}, {"CRWV": "CoreWeave"}))
    led = tmp_path / "data" / "ledger"
    led.mkdir(parents=True)
    (led / "themes.json").write_text(json.dumps(_theme_fixture(), ensure_ascii=False))

    def _boom(root, getter=None):
        raise AssertionError("US market 에서 테마 아티팩트를 읽으면 안 된다")

    monkeypatch.setattr("quant.apps.deepdive._load_or_fetch_themes", _boom)
    _seed_news(tmp_path, "US", "2026-08-15", ["CoreWeave announces new datacenter deal"])
    stats = run_deepdive("US", tmp_path, _FakeNarrator(None),
                         getter=lambda url: "<p>" + "body text here " * 10 + "</p>",
                         today="2026-08-15")
    assert stats["sources"] == 1  # 예외 없이 기존 언급 수 상위 경로로 진행됐다


def test_theme_relation_wins_over_llm_for_same_pair(tmp_path, monkeypatch):
    """컨트롤러 결정 4 — 같은 (src,dst) 를 테마와 LLM 이 둘 다 냈다면
    naver_theme 이 이긴다(팩트 우선). LLM 이유가 최종본에 남으면 안 된다."""
    monkeypatch.setattr(
        "quant.apps.deepdive._name_maps",
        lambda root, market: (
            {"SK이노베이션": "096770", "GS": "078930"},
            {"096770": "SK이노베이션", "078930": "GS"},
        ),
    )
    led = tmp_path / "data" / "ledger"
    led.mkdir(parents=True)
    (led / "themes.json").write_text(json.dumps(_theme_fixture(), ensure_ascii=False))
    # 두 번째 제목이 SK이노베이션·GS 를 동시 언급 — GS 의 evidence_score 가
    # MIN_EVIDENCE(50) 를 넘어야 LLM 후보가 실제로 채택되고, 그래야 이 테스트가
    # "채택된 LLM 관계를 테마가 덮어쓰는지"를 검증할 수 있다.
    _seed_news(tmp_path, "KR", "2026-08-15",
              ["SK이노베이션 실적 발표", "SK이노베이션 GS 협력"])
    llm = _FakeNarrator("GS | 수혜주 | LLM 이 지어낸 이유\n")
    run_deepdive("KR", tmp_path, llm,
                getter=lambda url: "<p>" + "SK이노베이션 GS 본문" * 10 + "</p>",
                today="2026-08-15")
    snap = json.loads((led / "relations.json").read_text())
    row = next(r for r in snap["096770"] if r["dst"] == "078930")
    assert row["source"] == "naver_theme"
    assert row["reason"] != "LLM 이 지어낸 이유"


def test_theme_relation_survives_llm_across_days_when_theme_absent_today(tmp_path, monkeypatch):
    """2026-08-16 리뷰 I2 — 팩트 우선은 이번 실행 안에서만이 아니라 날짜를
    걸쳐서도 성립해야 한다. 어제 source="naver_theme" 로 확정된 (src,dst) 가
    오늘은 테마 데이터 자체가 없어(수집·읽기 모두 실패) 기존 LLM 경로로
    폴백했는데, 그 LLM 이 같은 쌍을 지목해도 어제 확정된 팩트 행이 "llm" 으로
    강등되면 안 된다."""
    monkeypatch.setattr(
        "quant.apps.deepdive._name_maps",
        lambda root, market: (
            {"SK이노베이션": "096770", "GS": "078930"},
            {"096770": "SK이노베이션", "078930": "GS"},
        ),
    )
    led = tmp_path / "data" / "ledger"
    led.mkdir(parents=True)
    # 어제 테마 기반으로 확정된 관계 — 오늘은 themes.json 자체가 없다(수집도
    # 실패한다고 가정, getter 가 테마 HTML 을 못 준다) — 완전히 기존 LLM
    # 경로로 폴백하는 시나리오.
    yesterday_row = {
        "src": "096770", "dst": "078930", "kind": "beneficiary",
        "reason": "정유업체 GS칼텍스를 손자회사로 보유", "evidence_score": 0,
        "first_seen": "2026-08-14", "last_verified": "2026-08-14",
        "via_theme": "정유", "source": "naver_theme",
    }
    (led / "relations.json").write_text(
        json.dumps({"096770": [yesterday_row]}, ensure_ascii=False))
    # GS 의 evidence_score 가 MIN_EVIDENCE(50) 를 넘어야 LLM 후보가 실제로
    # 채택 시도된다 — 그래야 "채택 시도된 LLM 관계가 어제 팩트 행을 못
    # 덮어쓰는지"를 검증할 수 있다.
    _seed_news(tmp_path, "KR", "2026-08-15",
              ["SK이노베이션 실적 발표", "SK이노베이션 GS 협력"])
    llm = _FakeNarrator("GS | 수혜주 | 오늘 LLM 이 지어낸 새 이유\n")
    run_deepdive("KR", tmp_path, llm,
                getter=lambda url: "<p>" + "SK이노베이션 GS 본문" * 10 + "</p>",
                today="2026-08-15")
    snap = json.loads((led / "relations.json").read_text())
    row = next(r for r in snap["096770"] if r["dst"] == "078930")
    assert row["source"] == "naver_theme"
    assert row["reason"] == "정유업체 GS칼텍스를 손자회사로 보유"
    assert row["first_seen"] == "2026-08-14"  # 강등은커녕 갱신조차 안 됐다


def test_theme_collection_failure_is_logged_to_stderr(tmp_path, monkeypatch, capsys):
    """2026-08-16 리뷰 I3 — 테마 수집 실패가 무로그로 넘어가면 안 된다."""
    monkeypatch.setattr("quant.apps.deepdive._name_maps",
                        lambda root, market: ({}, {}))

    def _boom(getter=None):
        raise RuntimeError("네트워크 끊김")

    monkeypatch.setattr("quant.apps.deepdive.fetch_themes", _boom)
    run_deepdive("KR", tmp_path, _FakeNarrator(None),
                getter=lambda url: None, today="2026-08-15")
    assert "테마 수집 실패" in capsys.readouterr().err


def test_market_leaders_failure_is_logged_to_stderr(tmp_path, monkeypatch, capsys):
    """2026-08-16 리뷰 I3 — 거래상위 수집 실패가 무로그로 넘어가면 안 된다."""
    monkeypatch.setattr("quant.apps.deepdive._name_maps",
                        lambda root, market: ({}, {}))
    monkeypatch.setattr("quant.apps.deepdive._load_or_fetch_themes",
                        lambda root, getter=None: _theme_fixture())

    def _boom(getter=None):
        raise RuntimeError("네트워크 끊김")

    monkeypatch.setattr("quant.apps.deepdive.fetch_quant_top", _boom)
    run_deepdive("KR", tmp_path, _FakeNarrator(None),
                getter=lambda url: None, today="2026-08-15")
    assert "거래상위" in capsys.readouterr().err


class _FakeConn:
    """`relation_store.upsert_relations` 가 기대하는 최소 계약만 흉내."""

    def __init__(self):
        self.executed: list[tuple] = []
        self.committed = False

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def executemany(self, sql, tuples):
        self.executed.extend(tuples)

    def commit(self):
        self.committed = True


def test_ingest_snapshot_flattens_and_upserts():
    """스냅샷(`{src: [행...]}`) → upsert 행 변환이 핵심 — fake conn 으로 DB 없이 검증."""
    from quant.apps.deepdive import ingest_snapshot

    snapshot = {
        "005930": [{"src": "005930", "dst": "042700", "kind": "beneficiary",
                    "reason": "이유", "evidence_score": 70,
                    "first_seen": "2026-08-10", "last_verified": "2026-08-15"}],
        "000660": [],
    }
    conn = _FakeConn()
    n = ingest_snapshot(conn, snapshot)
    assert n == 1
    assert conn.committed is True
    assert conn.executed[0][:3] == ("005930", "042700", "beneficiary")


def test_sector_map_preserved_when_fetch_returns_empty(tmp_path, monkeypatch):
    """`fetch_sector_map` 이 빈 dict 를 돌려줘도(실패) 어제 sector_map.json 은 그대로."""
    monkeypatch.setattr("quant.apps.deepdive._name_maps",
                        lambda root, market: ({}, {}))
    led = tmp_path / "data" / "ledger"
    led.mkdir(parents=True)
    (led / "sector_map.json").write_text(
        json.dumps({"005930": "반도체"}, ensure_ascii=False))
    run_deepdive("KR", tmp_path, _FakeNarrator("x"),
                getter=lambda url: None, today="2026-08-15")
    kept = json.loads((led / "sector_map.json").read_text())
    assert kept == {"005930": "반도체"}


def test_sector_members_artifact_written_from_fetch_sector_data(tmp_path, monkeypatch):
    """H-1a — fetch_sector_data 가 낸 sector_members 가
    data/ledger/sector_members.json 에 그대로 아티팩트로 남는다(무선통신서비스
    사용자 재현 케이스: 4종목·보합·코스닥 별표 포함)."""
    monkeypatch.setattr("quant.apps.deepdive._name_maps",
                        lambda root, market: ({}, {}))
    members = {
        "무선통신서비스": [
            {"code": "017670", "name": "SK텔레콤", "change_pct": 10.32},
            {"code": "032640", "name": "LG유플러스", "change_pct": 2.45},
            {"code": "006490", "name": "프리티", "change_pct": 0.0},
            {"code": "065530", "name": "와이어블", "change_pct": -1.03},
        ]
    }
    monkeypatch.setattr(
        "quant.apps.deepdive.fetch_sector_data",
        lambda getter=None: ({"017670": "무선통신서비스"}, members),
    )
    run_deepdive("KR", tmp_path, _FakeNarrator("x"),
                getter=lambda url: None, today="2026-08-15")
    led = tmp_path / "data" / "ledger"
    saved = json.loads((led / "sector_members.json").read_text())
    assert saved == members
    assert len(saved["무선통신서비스"]) == 4


def test_sector_members_preserved_when_fetch_returns_empty(tmp_path, monkeypatch):
    """`fetch_sector_data` 가 빈 sector_members 를 돌려줘도(실패) 어제
    sector_members.json 은 그대로 — sector_map.json 과 같은 규칙."""
    monkeypatch.setattr("quant.apps.deepdive._name_maps",
                        lambda root, market: ({}, {}))
    led = tmp_path / "data" / "ledger"
    led.mkdir(parents=True)
    (led / "sector_members.json").write_text(
        json.dumps({"반도체": [{"code": "005930", "name": "삼성전자", "change_pct": 1.0}]},
                   ensure_ascii=False))
    run_deepdive("KR", tmp_path, _FakeNarrator("x"),
                getter=lambda url: None, today="2026-08-15")
    kept = json.loads((led / "sector_members.json").read_text())
    assert kept == {"반도체": [{"code": "005930", "name": "삼성전자", "change_pct": 1.0}]}


def test_deadline_exceeded_preserves_snapshot(tmp_path, monkeypatch):
    """`deadline_min=-1` — 전 소스가 처리 전에 마감. 기존 스냅샷은 손상 없이 그대로.

    `0`이 아니라 `-1`을 쓴다: `deadline_min=0`은 "0초 안에 처리"라 마감 판정이
    `time.monotonic()`의 미세한 타이밍(0.0 초과 여부)에 기대는 형태였다. `-1`은
    항상 음수 상한이라 어떤 타이밍에서도 첫 반복에 확실히 걸린다.
    """
    monkeypatch.setattr("quant.apps.deepdive._name_maps",
                        lambda root, market: ({"한미반도체": "042700",
                                               "삼성전자": "005930"},
                                              {"005930": "삼성전자",
                                               "042700": "한미반도체"}))
    led = tmp_path / "data" / "ledger"
    led.mkdir(parents=True)
    existing = {"005930": [{"src": "005930", "dst": "042700", "kind": "beneficiary",
                            "reason": "기존", "evidence_score": 99,
                            "first_seen": "2026-08-01", "last_verified": "2026-08-14"}]}
    (led / "relations.json").write_text(json.dumps(existing, ensure_ascii=False))
    _seed_news(tmp_path, "KR", "2026-08-15",
               ["삼성전자·한미반도체 협력", "한미반도체 수주 확대", "한미반도체 장비 증설"])
    llm = _FakeNarrator("한미반도체 | 수혜주 | HBM 본딩 장비\n")
    stats = run_deepdive("KR", tmp_path, llm,
                         getter=lambda url: "<p>" + "본문" * 20 + "</p>",
                         today="2026-08-15", deadline_min=-1)
    assert stats["accepted"] == 0
    assert stats["sources"] >= 1  # 소스는 뽑혔지만 처리 전에 마감으로 끊겼다
    kept = json.loads((led / "relations.json").read_text())
    assert kept == existing


def test_body_fetch_failure_excludes_article_and_skips_llm_without_bodies(tmp_path, monkeypatch):
    """fetch_body 실패(None)는 빈 본문으로 위장되지 않는다(I1a) — 그 기사는 arts 에서
    빠지고, 그 소스의 기사가 전부 실패하면 LLM 호출 자체를 건너뛴다."""
    monkeypatch.setattr("quant.apps.deepdive._name_maps",
                        lambda root, market: ({"한미반도체": "042700"},
                                              {"042700": "한미반도체"}))
    _seed_news(tmp_path, "KR", "2026-08-15", ["한미반도체 수주 확대"])
    calls: list[str] = []

    class _RecordingNarrator:
        def narrate(self, prompt):
            calls.append(prompt)
            return None

    stats = run_deepdive("KR", tmp_path, _RecordingNarrator(),
                         getter=lambda url: None,  # 본문 fetch 항상 실패
                         today="2026-08-15")
    assert stats["bodies_failed"] >= 1
    assert stats["bodies_ok"] == 0
    assert calls == []  # 본문을 하나도 못 얻은 소스는 LLM 을 부르지 않는다
    assert stats["skipped_llm"] is False  # LLM 이 죽은 게 아니라 애초에 안 불렀다


def test_corrupt_relations_json_backed_up_not_crashed(tmp_path, monkeypatch):
    """relations.json 이 파손돼 있어도(예: 이전 실행이 쓰다 죽음) 크래시하지 않고
    증거를 `.corrupt` 로 남긴 뒤 빈 사전에서 다시 시작한다(I3)."""
    monkeypatch.setattr("quant.apps.deepdive._name_maps",
                        lambda root, market: ({}, {}))
    led = tmp_path / "data" / "ledger"
    led.mkdir(parents=True)
    (led / "relations.json").write_text("{이것은 json 이 아니다")
    stats = run_deepdive("KR", tmp_path, _FakeNarrator(None),
                         getter=lambda url: None, today="2026-08-15")
    assert (led / "relations.json.corrupt").read_text() == "{이것은 json 이 아니다"
    assert json.loads((led / "relations.json").read_text()) == {}
    assert stats["sources"] == 0
