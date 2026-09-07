#!/usr/bin/env bash
# 서버 사용량 샘플러(2026-09-07, 소유자 질문 "최대 과부하 때와 평균 사용량") — 매분 한 줄을
# data/ops/usage.jsonl 에 남긴다: 부하평균, 메모리(사용/가용/스왑), 엔진·브리지·claude CLI·
# 리포트 프로세스의 RSS·CPU. 외부 패키지 없음(/proc + ps). 크론: `* * * * *`.
# 분석: `quant.apps.cli usage-report --days 7` (평균/피크/시간대별) — 없으면 jq/pandas 로 직접.
set -u
cd "$(dirname "$0")/../.."
mkdir -p data/ops
read -r l1 l5 l15 _ < /proc/loadavg
mem_total=$(awk '/MemTotal/{print $2}' /proc/meminfo)
mem_avail=$(awk '/MemAvailable/{print $2}' /proc/meminfo)
swap_total=$(awk '/SwapTotal/{print $2}' /proc/meminfo)
swap_free=$(awk '/SwapFree/{print $2}' /proc/meminfo)
# 프로세스별: 이름 매칭 → RSS(kB)·CPU% 합
proc_json() {  # $1=label $2=pattern
  local rss cpu n
  read -r rss cpu n < <(ps -eo rss=,pcpu=,args= | awk -v pat="$2" 'index($0, pat){r+=$1; c+=$2; n++} END{printf "%d %.1f %d\n", r+0, c+0, n+0}')
  printf '"%s":{"rss_mb":%d,"cpu_pct":%.1f,"n":%d}' "$1" $((rss/1024)) "$cpu" "$n"
}
# CPU 전체 사용률(1초 샘플, /proc/stat 델타)
cpu_pct() {
  local a b; read -r _ a1 a2 a3 a4 a5 a6 a7 _ < /proc/stat; sleep 1; read -r _ b1 b2 b3 b4 b5 b6 b7 _ < /proc/stat
  local idle=$((b4-a4)) total=$(( (b1+b2+b3+b4+b5+b6+b7) - (a1+a2+a3+a4+a5+a6+a7) ))
  [ "$total" -gt 0 ] && echo "scale=1; 100*($total-$idle)/$total" | bc || echo 0
}
cpu=$(cpu_pct)
printf '{"ts":"%s","load":[%s,%s,%s],"cpu_pct":%s,"mem_used_mb":%d,"mem_avail_mb":%d,"mem_total_mb":%d,"swap_used_mb":%d,%s,%s,%s,%s,%s}\n' \
  "$(date +%Y-%m-%dT%H:%M:%S%z)" "$l1" "$l5" "$l15" "$cpu" \
  $(( (mem_total-mem_avail)/1024 )) $((mem_avail/1024)) $((mem_total/1024)) $(( (swap_total-swap_free)/1024 )) \
  "$(proc_json engine 'quant.apps.cli paper')" \
  "$(proc_json bridge 'tg_bridge.py')" \
  "$(proc_json claude '.local/share/claude')" \
  "$(proc_json report 'quant.apps.report_cli')" \
  "$(proc_json other_py '.venv/bin/python')" >> data/ops/usage.jsonl
