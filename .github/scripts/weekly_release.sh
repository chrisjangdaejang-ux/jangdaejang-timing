#!/usr/bin/env bash
# 주간 실거래 동향 발표 — weekly_release.yml(월요일 07:00 + 예비 07:20·07:45·08:30)과 weekly.yml(계산이 7시 넘어 끝난 날) 이 부름.
#  · 비공개 엔진 data/weekly 의 판 중 발표 시각(release, 한국 시각)이 지난 것만 공개 저장소(jangdaejang-data gh-pages) data/weekly/ 로(weekly.py publish --now).
#  · 모닝 브리핑의 '지난주 우리 지역은?' 칸(엔진 data/weekly/brief_wk)도 그 칸의 release 가 지났을 때만 data/brief/wk/ 로.
#  · 몇 번 실행해도 같은 판이면 결과가 같다: 바뀐 파일이 없거나 index.json 의 실행 시각(at)만 바뀌었으면 커밋하지 않음.
# 사용: bash .github/scripts/weekly_release.sh <엔진 폴더>   (PUBLISH_TOKEN 필요, DRY_RUN=1 이면 올리지 않고 바뀐 파일만 보여 줌)
set -euo pipefail
ENGINE="$(cd "${1:-engine}" && pwd)"
NOW="${RELEASE_NOW:-$(TZ=Asia/Seoul date +%Y-%m-%dT%H:%M)}"
PUB=/tmp/wkpub
[ -d "$ENGINE/data/weekly" ] || { echo "엔진 보관본 없음 → 직전 공개본 유지"; exit 1; }
echo "발표 기준 시각 $NOW (KST)"

# 이번 주 판이 공개돼 있는지 — 없으면 실패로 끝내 감시(watchdog)·예비 실행이 '발표 끝남'으로 잘못 치지 않게(2026-11-23: 계산이 안 돈 채 발표가 '바뀐 것 없음 · 성공'으로 끝남)
check_latest() {
  python3 - "$PUB/data/weekly/index.json" "$NOW" <<'PY'
import sys, json, datetime
idx, now = sys.argv[1], datetime.datetime.strptime(sys.argv[2][:16], '%Y-%m-%dT%H:%M')
mon = (now - datetime.timedelta(hours=7)).date(); mon -= datetime.timedelta(days=mon.weekday())   # 발표 시각(월 07:00)이 지난 가장 최근 월요일
y, w, _ = (mon - datetime.timedelta(days=7)).isocalendar(); want = f'{y}-{w:02d}'
try: got = json.load(open(idx, encoding='utf-8')).get('latest')
except Exception: got = None
if got and got >= want: print(f'이번 주 판 공개 확인: {got}'); raise SystemExit(0)
print(f'::error::이번 주 판({want})이 공개 저장소에 없음(최신 {got}) — 주간 계산(weekly.yml)이 끝났는지 확인'); raise SystemExit(1)
PY
}

for i in 1 2 3 4; do
  rm -rf "$PUB"
  git clone -q --depth 1 --filter=blob:none --sparse -b gh-pages "https://x-access-token:${PUBLISH_TOKEN}@github.com/chrisjangdaejang-ux/jangdaejang-data.git" "$PUB"
  git -C "$PUB" sparse-checkout set --no-cone /data/weekly/ /data/brief/wk/
  python3 "$ENGINE/scripts/weekly.py" publish --site "$PUB/data" --now "$NOW"
  python3 - "$ENGINE/data/weekly/brief_wk" "$PUB/data/brief/wk" "$NOW" <<'PY'
import sys, os, json, glob, shutil
src, dst, now = sys.argv[1:4]
fs = sorted(glob.glob(os.path.join(src, '*.json')))
if not fs: print('브리핑 주간 칸 없음 → 그대로'); raise SystemExit
rel = [json.load(open(f, encoding='utf-8')).get('release') for f in fs]
rel = [r for r in rel if r]
if rel and min(rel)[:16] > now[:16]: print(f'브리핑 주간 칸 발표 전({min(rel)[:16]}) → 올리지 않음'); raise SystemExit
shutil.rmtree(dst, ignore_errors=True); os.makedirs(dst)
for f in fs: shutil.copy(f, dst)
print(f'브리핑 주간 칸 {len(fs)}개 공개')
PY
  cd "$PUB"
  for p in data/weekly data/brief/wk; do if [ -e "$p" ]; then git add -A "$p"; fi; done
  if [ "$(git diff --cached --name-only)" = "data/weekly/index.json" ] && python3 - <<'PY'
import json, subprocess
a = json.loads(subprocess.run(['git', 'show', 'HEAD:data/weekly/index.json'], capture_output=True, text=True, check=True).stdout)
b = json.load(open('data/weekly/index.json', encoding='utf-8'))
a.pop('at', None); b.pop('at', None)
raise SystemExit(0 if a == b else 1)
PY
  then git reset -q && git checkout -- data/weekly/index.json; fi
  if git diff --cached --quiet; then echo "바뀐 것 없음"; check_latest; exit $?; fi
  git diff --cached --stat | tail -5
  python3 "$ENGINE/scripts/public_guard.py" "$PUB/data" --models "$ENGINE/data" || { echo "공개 자료 검사 실패 → 올리지 않음"; exit 1; }
  [ -n "${DRY_RUN:-}" ] && { echo "DRY_RUN → 올리지 않음"; exit 0; }
  git -c user.name=jangdaejang-bot -c user.email=actions@users.noreply.github.com commit -qm "주간 실거래 동향 발표 $(TZ=Asia/Seoul date +%F)"
  git push -q && { echo "공개 저장소로 내보냄"; check_latest; exit $?; }
  cd /; sleep 20
done
echo "올리지 못함 — 다음 예비 실행·감시가 다시 시도"; exit 1
