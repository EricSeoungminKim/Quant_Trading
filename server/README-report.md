# 배포 — market_report

거래 엔진(`quant_trading_kiwoom`)과 **같은 EC2**에 나란히 올린다. 별도 디렉토리,
별도 venv, 별도 systemd 유닛으로 격리되며 **거래 엔진의 파일·크론·유닛은 건드리지
않는다.**

## 왜 같은 박스인가

- 토스 Open API 가 이 박스의 **고정 Elastic IP 만 화이트리스트**한다. 다른 곳에서는
  403 이다(로컬 개발기는 통신사 NAT 라 출발지 IP 가 요청마다 바뀐다 — 실측 확인).
- 공개 포트를 **하나도 열지 않는다.** 정적 서빙은 tailnet 전용이고, 외부 공유가
  필요하면 `out/` 만 정적 호스팅으로 밀어낸다. 방어할 표면을 만들지 않는 게
  방어를 잘하는 것보다 낫다.
- ADR-0002 가 이미 리포팅 레이어 프로세스를 이 박스에 허용한다(`daily_brief.sh`,
  `tg_bridge`). 리포트는 그 계층에 하나 더 붙는 것이다.

## 자원

실측 최대 RSS **231MB**(KR) / 214MB(US), 빌드 **32초**. 박스는 2코어·1.8GB 라
`MemoryMax=500M`(2배 여유)·`CPUQuota=50%`·`Nice=10`·`IOSchedulingClass=idle` 로
격리한다. **거래 엔진이 항상 우선이다.**

## 절차

```bash
# 1) 배포 (커밋된 상태여야 진행된다)
QT_SSH_HOST=ubuntu@100.87.129.113 ./server/deploy.sh

# 2) 시크릿 전달 — 스크립트는 .env.local 을 전송하지 않는다
scp .env.local ubuntu@100.87.129.113:/home/ubuntu/quant_trading_kiwoom/.env.local
ssh ubuntu@100.87.129.113 'chmod 600 /home/ubuntu/quant_trading_kiwoom/.env.local'

# 3) 수동 1회 확인
ssh ubuntu@100.87.129.113 'sudo systemctl start market-report@KR.service'
ssh ubuntu@100.87.129.113 'tail -20 /home/ubuntu/quant_trading_kiwoom/data/report.log'
```

## 발행 시각

| 시장 | 개장 | 발행 | 타이머 |
|---|---|---|---|
| KR | 09:00 KST | 08:00 KST | 07:50 기상 후 대기 |
| US | 09:30 ET | 07:00 ET (서머타임 20:00 / 표준시 21:00 KST) | 19:50 기상 후 대기 |

**타이머는 이른 시각에 걸고 정확한 발행 시각까지는 `run_report.sh` 가 대기한다.**
systemd `OnCalendar` 는 고정 시각이라 US 서머타임 전환을 못 따라간다. DST 계산은
테스트로 고정된 `quant/analyze/clock.py` 에 맡긴다.

US 리드가 150분인 이유는 엔진의 **21:40 US 관심종목 초기화** 크론 회피다.
개장이 KST 로 한 시간 움직이므로 양쪽 체제를 모두 피해야 한다:

| 리드 | 서머타임 | 표준시 | |
|---|---|---|---|
| 65분 | 21:25 | 22:25 | ⚠ 서머타임에 15분 차 |
| 120분 | 20:30 | 21:30 | ⚠ 표준시에 10분 차 — '2시간'이 오히려 나쁘다 |
| **150분** | **20:00** | **21:00** | ✅ 양쪽 40분 이상 여유 |

부수 효과: DST 를 추종하는 리드는 **항상 미 동부 07:00 에 실행**된다는 뜻이라
계절과 무관하게 시장 기준 같은 시각에 데이터를 모은다.

## 열람

tailnet 전용으로 서빙된다 — 공개 포트 없음.

```
http://100.87.129.113:8899/2026/08/12/KR_report.html
```

`out/` 만 루트로 서빙한다. **저장소 루트를 서빙하면 `.env.local` 이 그대로 노출된다.**

## 확인

```bash
systemctl list-timers 'market-report*'
systemctl status market-report-web.service
journalctl -u market-report@KR.service -n 50
```
