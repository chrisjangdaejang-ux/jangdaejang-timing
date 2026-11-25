#!/usr/bin/env python3
"""정기 실행 안전장치 — GitHub 가 정기 실행(schedule)을 빼먹거나 늦게 돌리는 날 대비.

  guard <이름>     워크플로 맨 앞 '확인' 작업. 정기 실행(schedule)일 때 오늘 이미 성공한 실행이 있으면 run=false(본 작업 건너뜀).
                   수동 실행(workflow_dispatch)은 언제나 run=true. API 가 안 되면 run=true(놓치는 것보다 한 번 더 도는 편이 안전).
  watchdog        감시(watchdog.yml): 오늘 해야 할 실행이 끝났는지 보고, 시간이 지났는데 없으면 REST 로 다시 실행(dispatch).
                   결과를 /tmp/health_today.json 에 쓰고, merge 가 data/schedule_health.json 에 합친다.
  merge <파일>     오늘 기록을 data/schedule_health.json 에 합침(최근 180일만).

시각은 모두 한국 시각(KST). 표준 라이브러리만 씀. 토큰: GH_TOKEN 또는 GITHUB_TOKEN.
"""
import os, sys, json, datetime, urllib.request, urllib.parse, urllib.error

KST = datetime.timezone(datetime.timedelta(hours=9))
REPO = os.environ.get('GITHUB_REPOSITORY', 'chrisjangdaejang-ux/jangdaejang-timing')
TOKEN = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN') or ''
HEALTH = os.environ.get('HEALTH_FILE', 'data/schedule_health.json')
BOT = 'github-actions[bot]'
MAX_DISPATCH = 2                       # 감시가 하루에 다시 실행시키는 최대 횟수(워크플로마다) — 진짜 고장이면 계속 돌리지 않게

# primary = 원래 정기 시각, backups = 예비 정기 시각(워크플로 cron 과 같게), due = 이 시각까지 성공이 없으면 감시가 다시 실행,
# since = 오늘 이 시각 이후 성공만 '오늘 것'으로 침('half' = 오전/오후 반나절), dow = 요일(1=월요일)만
WF = {
    'weekly':         dict(file='weekly.yml',         primary='03:43', backups=['04:03', '04:28', '05:13'], due='05:00', since='00:00', dow=1),
    'brief':          dict(file='brief.yml',          primary='05:07', backups=['05:27', '05:52', '06:37'], due='05:40', since='00:00'),
    'weekly_release': dict(file='weekly_release.yml', primary='07:00', backups=['07:20', '07:45', '08:30'], due='07:30', since='07:00', dow=1),
    'seo':            dict(file='seo.yml',            primary='07:37', backups=['07:57', '08:22', '09:07'], due='08:05', since='00:00'),
    'report':         dict(file='report.yml',         primary='05:40', backups=['06:05', '06:30', '07:52', '09:22'], due='07:30', since='05:00', dow=1),   # 7시 전 판은 07:00 발표 때 공개
    'facility':       dict(file='facility.yml',       primary='02:37', backups=['02:57', '03:22', '04:07'], due='05:00', since='half'),
}
# 몇 시간마다 도는 워크플로 — 최근 every_h + grace_h 시간 안에 시작된 실행(성공·도는 중·실패 모두)이 하나도 없으면 감시가 다시 실행(하루 최대 MAX_DISPATCH번).
# 2026-11-22 밤 GitHub 정기 실행이 몇 시간씩 밀리거나 빠져 update.yml 18:21 UTC 실행이 아예 없었음.
INTERVAL = {
    'update': dict(file='update.yml', every_h=6, grace_h=3),
}
# 매월 결과물(워크플로가 아니라 update.yml 안의 한 단계) — 공개 파일이 자료 끝 달(index.json dataTo)을 따라오는지 매일 확인해 기록만(다시 실행 안 함).
# behind 는 자료 끝 달이 바뀐 뒤 grace 일이 지나도 못 따라온 경우만 문제로 알림(새 달 자료가 들어온 날은 다음 update 실행이 만들 시간을 줌).
MONTHLY = {
    'trackrecord': dict(label='예측 적중 기록(매월)', path='data/trackrecord.json', key='end', grace_days=3),
}
PUB = os.environ.get('PUB_RAW', 'https://raw.githubusercontent.com/chrisjangdaejang-ux/jangdaejang-data/gh-pages/')
ACTIVE = {'queued', 'in_progress', 'waiting', 'requested', 'pending'}
NOT_REAL = ('· probe', '· compare')    # 시험 실행(run-name)은 '오늘 한 것'으로 치지 않음


def now_kst():
    s = os.environ.get('SCHED_NOW')    # 시험용: 'YYYY-MM-DDTHH:MM'
    return datetime.datetime.fromisoformat(s).replace(tzinfo=KST) if s else datetime.datetime.now(KST)


def at(day, hhmm):
    h, m = map(int, hhmm.split(':'))
    return datetime.datetime.combine(day, datetime.time(h, m), KST)


def window_start(cfg, now):
    s = cfg['since']
    if s == 'half': s = '12:00' if now.hour >= 12 else '00:00'
    return at(now.date(), s)


def api(path, method='GET', body=None):
    url = path if path.startswith('http') else f'https://api.github.com{path}'
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None)
    req.add_header('Accept', 'application/vnd.github+json')
    req.add_header('X-GitHub-Api-Version', '2022-11-28')
    if TOKEN: req.add_header('Authorization', f'Bearer {TOKEN}')
    if body is not None: req.add_header('Content-Type', 'application/json')
    with urllib.request.urlopen(req, timeout=30) as r:
        t = r.read()
        return json.loads(t) if t else {}


def runs_since(file, since):
    q = urllib.parse.quote('>=' + since.astimezone(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))
    out, page = [], 1
    while True:
        j = api(f'/repos/{REPO}/actions/workflows/{file}/runs?per_page=100&page={page}&created={q}')
        rs = j.get('workflow_runs') or []
        out += rs
        if len(rs) < 100 or page >= 5: break
        page += 1
    for r in out: r['_t'] = datetime.datetime.fromisoformat(r['created_at'].replace('Z', '+00:00')).astimezone(KST)
    return sorted(out, key=lambda r: r['_t'])


def real(r):
    return not any(x in (r.get('display_title') or '') for x in NOT_REAL)


def kst(t): return t.strftime('%H:%M')


def gh_out(**kv):
    p = os.environ.get('GITHUB_OUTPUT')
    lines = ''.join(f'{k}={v}\n' for k, v in kv.items())
    if p:
        with open(p, 'a') as f: f.write(lines)
    print(lines, end='')


def summary(md):
    p = os.environ.get('GITHUB_STEP_SUMMARY')
    if p:
        with open(p, 'a') as f: f.write(md + '\n')


# ---------- guard ----------
def cmd_guard(name):
    cfg, now = WF[name], now_kst()
    ev, me = os.environ.get('GITHUB_EVENT_NAME', ''), str(os.environ.get('GITHUB_RUN_ID', ''))
    if ev != 'schedule':
        print(f'{ev or "수동"} 실행 → 확인 없이 진행'); return gh_out(run='true')
    since = window_start(cfg, now)
    try:
        done = [r for r in runs_since(cfg['file'], since)
                if str(r['id']) != me and r.get('status') == 'completed' and r.get('conclusion') == 'success' and real(r)]
    except Exception as e:
        print(f'::warning::지난 실행 확인 실패({type(e).__name__}: {e}) → 그냥 진행')
        return gh_out(run='true')
    if done:
        r = done[0]
        msg = f'오늘({since:%m-%d %H:%M} KST 이후) 이미 성공한 실행이 있음 → 건너뜀: {kst(r["_t"])} {r["event"]} #{r["run_number"]} {r["html_url"]}'
        print(msg); summary(msg)
        return gh_out(run='false')
    print(f'오늘({since:%m-%d %H:%M} KST 이후) 성공한 실행 없음 → 진행')
    gh_out(run='true')


# ---------- watchdog ----------
def output_check(name, today):
    """공개 결과물로 한 번 더 확인(있는 것만) — 모닝 브리핑: 공개 저장소 data/brief/market.json, 내 집 성적표: data/report/index.json 의 date 가 오늘인지"""
    path = {'brief': 'data/brief/market.json', 'report': 'data/report/index.json'}.get(name)
    if not path: return None
    try:
        req = urllib.request.Request('https://raw.githubusercontent.com/chrisjangdaejang-ux/jangdaejang-data/gh-pages/' + path,
                                     headers={'Cache-Control': 'no-cache'})
        with urllib.request.urlopen(req, timeout=30) as r: d = json.loads(r.read()).get('date')
        return {'date': d, 'ok': d == today.isoformat()}
    except Exception as e:
        return {'error': type(e).__name__}


def classify(cfg, runs, now):
    """오늘 실행 기록 → 상태. succeeded = 원래 정기 실행이 성공 / recovered = 예비 정기 실행·감시·수동 실행이 채움"""
    day = now.date()
    first_backup = at(day, cfg['backups'][0])
    ok = [r for r in runs if r.get('conclusion') == 'success' and real(r)]
    sched = [r for r in runs if r['event'] == 'schedule']
    disp = [r for r in runs if r['event'] == 'workflow_dispatch']
    o = {'primary': cfg['primary'],
         'runs': {'schedule': len(sched), 'dispatch': len(disp),
                  'failed': sum(1 for r in runs if r.get('conclusion') in ('failure', 'timed_out')),
                  'cancelled': sum(1 for r in runs if r.get('conclusion') == 'cancelled')},
         'on_time': any(r['_t'] < first_backup for r in sched)}
    if ok:
        r = ok[0]
        if r['event'] == 'schedule':
            by = 'schedule' if r['_t'] < first_backup else 'schedule-late-or-backup'
        else:
            by = 'watchdog' if (r.get('triggering_actor') or r.get('actor') or {}).get('login') == BOT else 'manual'
        o.update(status='succeeded' if by == 'schedule' else 'recovered', by=by, at=kst(r['_t']), run=r['id'])
        o['skipped'] = sum(1 for x in ok[1:] if x['event'] == 'schedule')       # 이미 끝난 뒤 들어와 확인 단계에서 건너뛴 예비 실행
    elif any(r.get('status') in ACTIVE for r in runs):
        o['status'] = 'running'
    else:
        o['status'] = 'missing' if now >= at(day, cfg['due']) else 'not_due'
    return o


def _pub_json(path):
    req = urllib.request.Request(PUB + path, headers={'Cache-Control': 'no-cache'})
    with urllib.request.urlopen(req, timeout=30) as r: return json.loads(r.read())


def monthly_check(now, get=None):
    """MONTHLY 마다 {status: ok|behind|missing|error, end, dataTo, built} — get(path) 는 시험용(기본: 공개 저장소에서 읽음)"""
    get = get or _pub_json
    out = {}
    try: data_to = (get('data/index.json') or {}).get('dataTo')
    except Exception as e: return {k: {'status': 'error', 'note': f'index.json {type(e).__name__}'} for k in MONTHLY}
    for k, c in MONTHLY.items():
        try: d = get(c['path']) or {}
        except urllib.error.HTTPError as e:
            out[k] = {'status': 'missing' if e.code == 404 else 'error', 'dataTo': data_to}; continue
        except Exception as e:
            out[k] = {'status': 'error', 'note': type(e).__name__, 'dataTo': data_to}; continue
        o = {'end': d.get(c['key']), 'dataTo': data_to, 'built': d.get('built')}
        o['status'] = 'ok' if o['end'] == data_to else 'behind'
        if o['status'] == 'behind' and now.day <= c['grace_days']: o['status'] = 'pending'     # 새 달 자료가 막 들어옴 — 며칠 안에 따라오면 정상
        out[k] = o
    return out


# ---------- 자료 원천 상태(수집기가 실행마다 남긴 data/source_status/*.json) ----------
SRC_DIRS = [os.environ.get('SOURCE_STATUS_DIR') or 'data/source_status', os.environ.get('ENGINE_STATUS_DIR') or '/tmp/eng/data/source_status']
SRC_BAD = ('failed', 'quota', 'notRegistered', 'waiting')
SRC_KO = {'ok': '정상', 'partial': '일부(이어 받는 중)', 'quota': '하루 한도', 'notRegistered': '신청·키 필요', 'waiting': '승인 연결 대기',
          'failed': '실패', 'skipped': '건너뜀(할 일 없음)', 'stale': '오래 안 돎'}


def load_sources():
    """두 곳(공개 수집 저장소 · 비공개 엔진 저장소)의 상태 파일 → 원천마다 가장 최근 것"""
    import glob
    out = {}
    for d in SRC_DIRS:
        for p in glob.glob(os.path.join(d, '*.json')):
            try: r = json.load(open(p, encoding='utf-8'))
            except Exception: continue
            k = r.get('k') or os.path.basename(p)[:-5]
            if k not in out or str(r.get('at', '')) > str(out[k].get('at', '')): out[k] = r
    return out


def summarize_sources(now, S=None):
    """원천별 상태 → 그날 기록용 요약. stale = 정해 둔 주기(every_h)의 2배 + 6시간 넘게 기록이 없음(수집 단계가 아예 안 돎)"""
    S = load_sources() if S is None else S
    detail, groups, problems = {}, {}, []
    for k, r in sorted(S.items()):
        st = r.get('status', 'failed')
        try: at = datetime.datetime.fromisoformat(r['at']).replace(tzinfo=KST)
        except Exception: at = None
        every = float(r.get('every_h') or 24)
        if at and (now - at).total_seconds() / 3600 > every * 2 + 6: st = 'stale'
        detail[k] = [st, r.get('at', ''), r.get('reason', '')]
        groups.setdefault(st, []).append(k)
        if st in SRC_BAD or st == 'stale':
            problems.append(f"{r.get('label') or k}: {SRC_KO.get(st, st)}" + (f" — {r['reason']}" if r.get('reason') else '')
                            + (f" (마지막 기록 {r.get('at', '')[5:16].replace('T', ' ')})" if st == 'stale' else ''))
    summary = {'n': len(S), 'ok': len(groups.get('ok', [])), **{g: sorted(v) for g, v in groups.items() if g != 'ok'}}
    return {'summary': summary, 'problems': problems, 'detail': detail}


def cmd_watchdog(dry=False):
    now = now_kst(); today = now.date()
    rep = {'date': today.isoformat(), 'wf': {}}
    dispatched = []
    lines = [f'감시 {now:%Y-%m-%d %H:%M} KST' + (' (dry-run)' if dry else '')]
    for name, cfg in WF.items():
        if cfg.get('dow') and today.isoweekday() != cfg['dow']: continue
        if now < at(today, cfg['primary']):                     # 아직 원래 시각 전
            continue
        since = window_start(cfg, at(today, cfg['primary']))
        try:
            runs = runs_since(cfg['file'], since)
        except Exception as e:
            lines.append(f'- {name}: 실행 기록 확인 실패 {type(e).__name__}'); continue
        o = classify(cfg, runs, now)
        oc = output_check(name, today)
        if oc is not None: o['output'] = oc
        if o['status'] == 'missing':
            mine = [r for r in runs if r['event'] == 'workflow_dispatch' and (r.get('triggering_actor') or {}).get('login') == BOT]
            if len(mine) >= MAX_DISPATCH:
                o['note'] = f'감시가 이미 {len(mine)}번 다시 실행 — 더 하지 않음(실패 원인 확인 필요)'
                lines.append(f'- {name}: 없음 · {o["note"]}')
            elif dry:
                lines.append(f'- {name}: 없음 → (dry-run) 다시 실행하지 않음')
            else:
                try:
                    api(f'/repos/{REPO}/actions/workflows/{cfg["file"]}/dispatches', 'POST', {'ref': 'main'})
                    o['status'] = 'dispatched'; o['dispatched_at'] = kst(now); dispatched.append(name)
                    lines.append(f'- {name}: 없음 → 다시 실행 요청함(dispatch)')
                except urllib.error.HTTPError as e:
                    o['note'] = f'dispatch 실패 HTTP {e.code}'
                    lines.append(f'- {name}: 없음 → ::error::다시 실행 요청 실패 HTTP {e.code}')
        elif o['status'] in ('succeeded', 'recovered'):
            lines.append(f'- {name}: 이미 끝남(already done) · {o["status"]} by {o["by"]} {o["at"]}' + (f' · 공개 결과 date={oc.get("date")}' if oc else ''))
        else:
            lines.append(f'- {name}: {o["status"]}')
        if name in ('brief', 'report') and oc and oc.get('ok') is False and o['status'] in ('succeeded', 'recovered'):
            lines.append(f'  ::warning::{name} 실행은 성공했지만 공개 결과 date={oc.get("date")} (오늘 {today})')
        rep['wf'][name] = o
    for name, cfg in INTERVAL.items():
        try:
            runs = runs_since(cfg['file'], now - datetime.timedelta(hours=cfg['every_h'] + cfg['grace_h']))
            mine = [r for r in runs_since(cfg['file'], at(today, '00:00'))
                    if r['event'] == 'workflow_dispatch' and (r.get('triggering_actor') or {}).get('login') == BOT]
        except Exception as e:
            lines.append(f'- {name}: 실행 기록 확인 실패 {type(e).__name__}'); continue
        o = {'every_h': cfg['every_h'], 'recent': len(runs)}
        if runs:
            r = runs[-1]; o.update(status='ok', last=kst(r['_t']), last_event=r['event'], last_state=r.get('conclusion') or r.get('status'))
            lines.append(f'- {name}: 최근 실행 {o["last"]} {r["event"]} ({o["last_state"]})')
        elif len(mine) >= MAX_DISPATCH:
            o.update(status='missing', note=f'감시가 오늘 이미 {len(mine)}번 다시 실행 — 더 하지 않음')
            lines.append(f'- {name}: {cfg["every_h"] + cfg["grace_h"]}시간 동안 실행 없음 · {o["note"]}')
        elif dry:
            o['status'] = 'missing'; lines.append(f'- {name}: {cfg["every_h"] + cfg["grace_h"]}시간 동안 실행 없음 → (dry-run) 다시 실행하지 않음')
        else:
            try:
                api(f'/repos/{REPO}/actions/workflows/{cfg["file"]}/dispatches', 'POST', {'ref': 'main'})
                o.update(status='dispatched', dispatched_at=kst(now)); dispatched.append(name)
                lines.append(f'- {name}: {cfg["every_h"] + cfg["grace_h"]}시간 동안 실행 없음 → 다시 실행 요청함(dispatch)')
            except urllib.error.HTTPError as e:
                o.update(status='missing', note=f'dispatch 실패 HTTP {e.code}')
                lines.append(f'- {name}: ::error::다시 실행 요청 실패 HTTP {e.code}')
        rep['wf'][name] = o
    try:
        wd = [r for r in runs_since('watchdog.yml', at(today, '00:00')) if r['event'] == 'schedule']
        rep['watchdog_scheduled_runs'] = len(wd)
    except Exception:
        pass
    try:
        rep['monthly'] = monthly_check(now)
        for k, o in rep['monthly'].items():
            lab = MONTHLY[k]['label']
            if o['status'] in ('ok', 'pending'): lines.append(f"- {lab}: {o['status']} (자료 끝 {o.get('end')}, 공개 자료 {o.get('dataTo')})")
            else: lines.append(f"- {lab}: ::warning::{o['status']} (공개 파일 {o.get('end')} / 자료 끝 {o.get('dataTo')}) — update.yml '예측 적중 기록' 단계·logs/trackrecord.log 확인")
    except Exception as e:
        lines.append(f'- 매월 결과물 확인 실패 {type(e).__name__}')
    try:
        rep['sources'] = summarize_sources(now)
        for k, o in (rep.get('monthly') or {}).items():            # 매월 결과물이 못 따라오면 대표 보고 줄에도
            if o.get('status') in ('behind', 'missing', 'error'):
                rep['sources']['problems'].append(f"{MONTHLY[k]['label']}: {o['status']} — 공개 파일 {o.get('end')} / 자료 끝 {o.get('dataTo')}")
        sm = rep['sources']['summary']
        lines.append(f"자료 원천 {sm['n']}개: 정상 {sm['ok']}" + ''.join(f" · {SRC_KO.get(g, g)} {len(v)}" for g, v in sm.items() if g not in ('n', 'ok')))
        lines += [f'  - {p}' for p in rep['sources']['problems']]
    except Exception as e:
        lines.append(f'- 자료 원천 상태 읽기 실패 {type(e).__name__}')
    if os.environ.get('GITHUB_EVENT_NAME') == 'workflow_run':
        wr = ((json.load(open(os.environ['GITHUB_EVENT_PATH'])) if os.environ.get('GITHUB_EVENT_PATH') else {}).get('workflow_run') or {})
        lines.insert(1, f"(다른 워크플로 끝남으로 깨어남: {wr.get('name', '?')} {wr.get('event', '')} {wr.get('conclusion', '')})")
    print('\n'.join(lines)); summary('\n'.join(lines))
    gh_out(dispatched=','.join(dispatched))
    with open('/tmp/health_today.json', 'w', encoding='utf-8') as f: json.dump(rep, f, ensure_ascii=False)


def cmd_merge(path):
    rep = json.load(open(path, encoding='utf-8'))
    H = {'about': '정기 실행 기록(감시 watchdog.yml 이 매일 05~09시 KST 에 씀). status: succeeded=원래 정기 실행 성공, '
                  'recovered=예비 정기 실행·감시(watchdog)·수동(manual) 실행이 채움, dispatched=감시가 다시 실행 요청함, '
                  'missing=끝까지 없음. on_time=false 이면 원래 정기 실행이 제때(첫 예비 시각 전) 안 들어온 날(빠짐 또는 늦음). skipped=이미 끝나 건너뛴 예비 실행 수. '
                  'monthly=매월 결과물(예측 적중 기록 trackrecord): ok=공개 파일이 자료 끝 달을 따라옴, pending=새 달 자료가 막 들어옴(3일 안), behind=못 따라옴, missing=파일 없음. '
                  'sources=자료 원천(외부 데이터)별 마지막 실행 결과: summary(상태별 이름 — ok 는 개수만), problems(대표에게 보일 한 줄씩), '
                  'detail(원천: [상태, 시각 KST, 이유]). 상태: partial=일부(이어 받는 중), quota=포털 하루 한도, notRegistered=활용신청·키 필요, '
                  'waiting=승인 직후 연결 대기, failed=실패, skipped=할 일 없음, stale=정해 둔 주기의 2배 넘게 기록 없음(수집 단계가 안 돎).',
         'days': {}}
    if os.path.exists(HEALTH):
        try: H.update({k: v for k, v in json.load(open(HEALTH, encoding='utf-8')).items() if k != 'about'})
        except Exception: pass
    day = H['days'].setdefault(rep['date'], {})
    for k, v in rep['wf'].items():
        prev = day.get(k) or {}
        if prev.get('status') == 'dispatched' and v['status'] == 'running': v = dict(v, status='dispatched', dispatched_at=prev.get('dispatched_at'))
        if prev.get('dispatched_at') and v.get('status') in ('succeeded', 'recovered'): v['watchdog_dispatched_at'] = prev['dispatched_at']
        day[k] = v
    if 'watchdog_scheduled_runs' in rep: day['watchdog_scheduled_runs'] = rep['watchdog_scheduled_runs']
    if rep.get('monthly'):
        # 매월 결과물(예측 적중 기록 등): {status: ok|pending|behind|missing|error, end, dataTo, built}
        day['monthly'] = rep['monthly']
    if rep.get('sources'):
        # 대표 09:07 보고용: summary(상태별 원천 이름) · problems(사람이 읽는 한 줄씩) · detail(원천: [상태, 시각, 이유])
        day['sources'] = rep['sources']
    H['days'] = dict(sorted(H['days'].items())[-180:])
    os.makedirs(os.path.dirname(HEALTH) or '.', exist_ok=True)
    j = lambda o: json.dumps(o, ensure_ascii=False, separators=(',', ':'))
    out = ['{', f' "about": {j(H["about"])},', ' "days": {']             # 하루·워크플로마다 한 줄
    ds = list(H['days'].items())
    for i, (d, wfs) in enumerate(ds):
        out.append(f'  {j(d)}: {{')
        ks = list(wfs.items())
        out += [f'   {j(k)}: {j(v)}' + (',' if n < len(ks) - 1 else '') for n, (k, v) in enumerate(ks)]
        out.append('  }' + (',' if i < len(ds) - 1 else ''))
    out += [' }', '}']
    with open(HEALTH, 'w', encoding='utf-8') as f: f.write('\n'.join(out) + '\n')


if __name__ == '__main__':
    a = sys.argv[1:]
    if not a: raise SystemExit(__doc__)
    if a[0] == 'guard': cmd_guard(a[1])
    elif a[0] == 'watchdog': cmd_watchdog(dry='--dry-run' in a)
    elif a[0] == 'merge': cmd_merge(a[1])
    elif a[0] == 'sources':
        r = summarize_sources(now_kst()); print(json.dumps(r['summary'], ensure_ascii=False)); print('\n'.join(r['problems']))
    else: raise SystemExit(__doc__)
