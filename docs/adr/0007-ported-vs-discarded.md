# ADR-0007: 전작에서 이식하는 것 / 버리는 것

- Status: Accepted
- Date: 2026-07-28

## Context

`stock-algo-trade`(전작)에 검증된 로직과 반면교사로 삼아야 할 실수가 함께 있다.
클린 슬레이트로 새로 시작하되, 검증된 부품은 재이식하고 교훈은 반복하지 않는다.

## Decision

| 이식 (검증됨)                                                  | 버리는 것 (교훈)                                  |
| -------------------------------------------------------------- | ------------------------------------------------- |
| danta.py의 Donchian 진입/청산 로직                             | config 별칭 다중 인스턴스 (donchian_apex_vm17...) |
| Toss 클라이언트 (rate limiter, 토큰 캐시, 401 재발급)          | 죽은 KR 유니버스 코드                             |
| Telegram notifier (circuit breaker, 4096 truncate)             | 80줄짜리 운영 히스토리 docstring                  |
| tg_bridge (Claude 브릿지, systemd 분리)                        | 체크인된 .venv                                    |
| systemd/cron 배포 패턴 (venv 직접 바이너리, KST 교훈)          | REST 폴링 전용 설계 (WS 추가)                     |
| 1m→15m 리샘플링 (`closed="left", label="left"`, 미완성봉 drop) |                                                   |

## Consequences

- 새 코드에 config 별칭으로 같은 전략 클래스를 다중 인스턴스화하는 패턴을 다시
  들이지 않는다 — 전략 1개 = 파일 1개 원칙을 지킨다.
- `.venv`는 절대 git에 커밋하지 않는다.
