#!/usr/bin/env bash
# 내 집 성적표 자료 — report.yml(월요일 05:40 + 예비)이 부름. 7시 전이면 비공개 엔진 data/report_next/ 에만 두고 weekly_release.sh(07:00)가 공개, 7시 뒤면 바로 공개.
#  · 공개 저장소(jangdaejang-data gh-pages)의 price/{구}.json · today/g · weekly(최신 판) · 직전 report/ 를 읽어 data/report/ 를 새로 씀(엔진 home_report.py build).
#  · 같은 날 다시 돌려도 결과가 같다(그날의 '지난 성적표'를 파일 안에 남김). 바뀐 것이 없으면 커밋하지 않음.
#  · 내보내기 전 public_guard(노하우 유출 검사). 실패하면 올리지 않음 → 직전 공개본 그대로.
# 사용: bash .github/scripts/report_build.sh <엔진 폴더>   (PUBLISH_TOKEN 필요, DRY_RUN=1 이면 올리지 않음, REPORT_NOW=YYYY-MM-DDTHH:MM 시험용)
set -euo pipefail
ENGINE="$(cd "${1:-engine}" && pwd)"
NOW="${REPORT_NOW:-$(TZ=Asia/Seoul date +%Y-%m-%dT%H:%M)}"
PUB=/tmp/rppub
for i in 1 2 3 4; do
  rm -rf "$PUB"
  git clone -q --depth 1 --filter=blob:none --sparse -b gh-pages "https://x-access-token:${PUBLISH_TOKEN}@github.com/chrisjangdaejang-ux/jangdaejang-data.git" "$PUB"
  git -C "$PUB" sparse-checkout set --no-cone '/data/price/[0-9]*.json' /data/today/g/ '/data/weekly/*.json' /data/report/
  DAY="${NOW:0:10}"; DOW=$(date -d "$DAY" +%u); HM="${NOW:11:5}"
  EARLY=""; [ "$DOW" = "1" ] && [[ "$HM" < "07:00" ]] && EARLY=1
  if [ -n "$EARLY" ]; then
    # 7시 전: 이번 주 주간 동향 판을 '7시 발표 상태'로 임시 폴더에만 펼쳐 성적표에 넣는다(공개 저장소에는 올리지 않음)
    python3 "$ENGINE/scripts/weekly.py" publish --site "$PUB/data" --now "${DAY}T07:00"
  fi
  python3 "$ENGINE/scripts/home_report.py" build "$PUB/data" --now "$NOW"
  if [ -n "$EARLY" ]; then
    python3 "$ENGINE/scripts/public_guard.py" "$PUB/data/report" --models "$ENGINE/data" || { echo "공개 자료 검사 실패 → 저장하지 않음"; exit 1; }
    [ -n "${DRY_RUN:-}" ] && { echo "DRY_RUN → 저장하지 않음"; exit 0; }
    rm -rf "$ENGINE/data/report_next" && mkdir -p "$ENGINE/data/report_next" && cp -r "$PUB/data/report/." "$ENGINE/data/report_next/"
    cd "$ENGINE"
    git add -A data/report_next
    if git diff --cached --quiet; then echo "바뀐 것 없음(미리 만든 판 그대로)"; exit 0; fi
    git -c user.name=jangdaejang-bot -c user.email=actions@users.noreply.github.com commit -qm "내 집 성적표 미리 만든 판 $DAY (07:00 발표 때 공개)"
    for j in 1 2 3 4; do git pull -q --rebase -X theirs && git push -q && { echo "7시 발표 대기(엔진 data/report_next)"; exit 0; }; sleep 10; done
    echo "엔진에 저장하지 못함"; exit 1
  fi
  cd "$PUB"
  git add -A data/report
  if git diff --cached --quiet; then echo "바뀐 것 없음"; exit 0; fi
  git diff --cached --stat | tail -3
  python3 "$ENGINE/scripts/public_guard.py" "$PUB/data/report" --models "$ENGINE/data" || { echo "공개 자료 검사 실패 → 올리지 않음"; exit 1; }
  [ -n "${DRY_RUN:-}" ] && { echo "DRY_RUN → 올리지 않음"; exit 0; }
  git -c user.name=jangdaejang-bot -c user.email=actions@users.noreply.github.com commit -qm "내 집 성적표 자료 $(TZ=Asia/Seoul date +%F)"
  git push -q && { echo "공개 저장소로 내보냄"; exit 0; }
  cd /; sleep 20
done
echo "올리지 못함 — 다음 예비 실행·감시가 다시 시도"; exit 1
