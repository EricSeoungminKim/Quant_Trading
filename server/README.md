# EC2 배포 가이드

quant_trading_kiwoom 엔진을 AWS EC2에 상시 구동시키는 절차. 전작(stock-algo-trade)에서
검증된 배포 패턴(deploy/aws-setup.md)을 이식.

## 0. 필수 전제 — IP 허용목록

Toss Open API는 **등록된 IP만 허용 (403 otherwise)**. EC2에 **Elastic IP**(고정 IP)를
연결하고, 그 IP를 토스증권 앱 → 설정 → Open API → IP 허용목록에 등록할 것.
**이거 없으면 엔진은 뜨지만 주문/조회가 전부 403으로 실패한다.**

## 1. AWS 콘솔 (최초 1회)

1. 리전 **Asia Pacific (Seoul) ap-northeast-2** 선택.
2. EC2 → Key pairs → **Import key pair** → 로컬 `~/.ssh/id_ed25519.pub` 내용 붙여넣기.
3. EC2 → **Launch instance**:
   - AMI: **Ubuntu Server 24.04 LTS (64-bit Arm)**
   - Instance type: **t4g.small**
   - Key pair: 방금 import한 키
   - Network: SSH는 **My IP**만 허용 (Anywhere 금지)
   - Storage: 20 GiB gp3
4. EC2 → **Elastic IPs → Allocate → Associate**로 인스턴스에 고정 IP 연결.
5. 위 Elastic IP를 §0에 따라 Toss 허용목록에 등록.

## 2. 서버 최초 셋업

```bash
ssh ubuntu@<ElasticIP>
git clone https://github.com/EricSeoungminKim/quant_trading_kiwoom.git
cd quant_trading_kiwoom
bash server/scripts/setup_ec2.sh
```

`setup_ec2.sh`가 하는 일 (idempotent — 재실행 안전):
- 타임존을 Asia/Seoul로 설정
- git, uv 설치
- 레포 clone/pull + `uv sync`
- systemd 유닛(`quant-engine`, `tg-bridge`) 설치 및 활성화
- crontab 설치

## 3. 시크릿 전달 (.env.local)

`.env.local`은 git에 올라가지 않는다(gitignore). 로컬 Mac에서 scp로 전달:

```bash
scp .env.local ubuntu@<ElasticIP>:~/quant_trading_kiwoom/
ssh ubuntu@<ElasticIP> "sudo systemctl restart quant-engine tg-bridge"
```

## 4. 확인

```bash
sudo systemctl status quant-engine tg-bridge
journalctl -u quant-engine -f      # 엔진 로그
journalctl -u tg-bridge -f         # 텔레그램 브리지 로그
crontab -l                         # 크론 설치 확인
```

## 5. 이후 배포 (코드 변경 반영)

로컬 Mac에서:

```bash
QT_SSH_HOST=ubuntu@<ElasticIP> ./server/scripts/deploy.sh
```

`deploy.sh`는 `git push` 후 서버에 ssh로 접속해 `git pull && uv sync &&
systemctl restart quant-engine`을 실행한다. `settings.yaml` 파라미터 변경은 엔진의
핫 리로드로 재시작 없이 반영되지만, **코드 변경은 재시작 없이는 반영되지 않는다.**

## 참고 — 전작에서 이식한 교훈

- **systemd/cron은 venv python 바이너리를 직접 호출한다** (`uv run` 아님). 데몬/크론
  환경은 로그인 셸이 아니라 PATH가 비어 있어 `uv`가 조용히 실패하는 사례가 있었다.
  `.venv/bin/python`을 직접 가리키면 이 문제를 완전히 피한다.
- **TZ=Asia/Seoul을 모든 곳에 명시한다** — 서버 타임존, systemd 유닛의 Environment,
  crontab 주석의 시각 기준까지 전부 KST로 통일.
- **SIGTERM(exit 143)은 정상 종료로 취급한다** (`SuccessExitStatus=143`) — 재배포
  때마다 재시작이 'Failed'로 오경보되는 것을 방지.
- **tg-bridge는 MemoryMax=400M, CPUQuota=50%로 격리한다** — 브리지가 클로드
  서브프로세스를 띄우다 폭주해도 엔진(quant-engine)의 자원을 빼앗지 않도록.
- **Toss API는 등록된 IP만 허용한다** — Elastic IP를 반드시 허용목록에 등록해야
  주문/조회가 동작한다 (§0).
