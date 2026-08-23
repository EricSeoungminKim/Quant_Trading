# Runbook: EC2 배포

코드 변경을 상시 구동 중인 EC2 인스턴스에 반영하는 절차. 최초 셋업(인스턴스 생성,
Elastic IP, systemd/crontab 설치)은 이 문서 범위 밖 — `server/README.md` §1-2 참고.

## 사전 조건

- [ ] 로컬에서 `git status` 클린, 커밋 완료
- [ ] `uv run pytest` 로컬에서 통과 확인
- [ ] `QT_SSH_HOST` 환경변수 설정: `export QT_SSH_HOST=ubuntu@<ElasticIP>`

## 절차

```bash
QT_SSH_HOST=ubuntu@<ElasticIP> ./server/scripts/deploy.sh
# 또는: QT_SSH_HOST=ubuntu@<ElasticIP> make deploy
```

이 스크립트가 하는 일:

1. 로컬에서 `git push`
2. `ssh $QT_SSH_HOST`로 접속해 `cd quant_trading_kiwoom && git pull && uv sync && sudo systemctl restart quant-engine`

## 코드 변경 vs 설정 변경

- **`config/settings.yaml` 변경**: 엔진이 핫 리로드하므로 재시작 없이 반영된다.
  `deploy.sh` 없이 `scp config/settings.yaml`만 해도 된다.
- **코드(.py) 변경**: 재시작 없이는 반영되지 않는다. 반드시 위 `deploy.sh` 절차를
  거친다.
- **시크릿(`.env.local`) 변경**: `deploy.sh`가 다루지 않는다 —
  [`secrets-rotation.md`](secrets-rotation.md) 참고.

## 확인

```bash
ssh $QT_SSH_HOST "sudo systemctl status quant-engine tg-bridge"
ssh $QT_SSH_HOST "journalctl -u quant-engine -n 50"
```

`Active: active (running)`이고 최근 로그에 에러가 없으면 배포 완료.

## 롤백

배포 후 문제가 생기면:

```bash
ssh $QT_SSH_HOST "cd quant_trading_kiwoom && git log --oneline -5"
ssh $QT_SSH_HOST "cd quant_trading_kiwoom && git checkout <이전_커밋> && uv sync && sudo systemctl restart quant-engine"
```
