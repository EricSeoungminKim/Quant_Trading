# ADR-0002: 웹 프레임워크 없음 (FastAPI/Django 기각)

- Status: Accepted
- Date: 2026-07-28

## Context

이 시스템에 인바운드 HTTP 요청이 없다. 유저 인터페이스는 Telegram bot(아웃바운드
polling), 트리거는 시장 데이터와 스케줄러다. 웹 프레임워크는 순수 오버헤드다.

## Decision

asyncio 기반 이벤트 루프 단일 프로세스로 간다. FastAPI/Django는 채택하지 않는다.

## Consequences

- 추후 대시보드가 필요하면 그때 읽기 전용 FastAPI를 어댑터로 붙인다
  (코어 수정 없이 가능한 구조 — ADR-0003의 Ports & Adapters 경계 덕분).
