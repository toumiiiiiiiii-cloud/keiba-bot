# -*- coding: utf-8 -*-
"""
シェイクユアハート：単勝オッズの記録（前日・朝・発走前…と、時間ごとのオッズを残す）

なぜ：今のバックテストは「最終オッズ」で計算しているが、実際にBotで予想を聞くのは発走の少し前。
      オッズは発走直前まで動くので、「予想を聞いた時点のオッズ」で期待値を計算したら成績がどう変わるかを、
      あとで確かめられるように、今から記録しておく。

・GitHub Actions（odds_snapshot.yml）が、土日月の 9:00〜16:40 に20分ごと、金土日の夜（前日オッズ）に動かす
・今日と明日の中央競馬の全レースについて、まだ発走していないレースの単勝オッズを読む
・Cloudflare R2 の odds/日付.json に貯める（動画と同じ置き場。1日1ファイル）
  形：{"レースID": {"name": レース名, "time": "15:40", "snaps": [["2026-10-11 14:20", 80, {"1": 3.4, ...}], ...]}}
       snaps の2番目の数字＝発走まであと何分
・開催の無い日はすぐ終わる
"""
import datetime
import json
import os
import re
import sys
import time

os.environ.setdefault('SYH_BATCH', '1')     # netkeibaへゆっくりアクセスする
os.environ.setdefault('SYH_NOFONT', '1')
import boto3                                 # noqa: E402
import main as M                             # noqa: E402

BUCKET = os.environ['R2_BUCKET']
def r2_endpoint():
    """R2の接続先。Account ID だけでなく、URLを丸ごと入れた場合や、前後に空白・改行が入った場合も読めるようにする"""
    raw = os.environ['R2_ACCOUNT_ID'].strip()
    m = re.search(r'[0-9a-fA-F]{32}', raw)
    acct = m.group(0).lower() if m else re.sub(r'^https?://|\.r2\.cloudflarestorage\.com.*$', '', raw).strip('/ ')
    same_as_key = acct == os.environ.get('R2_ACCESS_KEY_ID', '').strip().lower()
    print(f"[r2] Account ID：{len(acct)}文字・{'32桁の英数字でOK' if m else '32桁の英数字が見つからない（Cloudflareの右側にある Account ID を入れてください）'}"
          + ('・※Access Key ID と同じ値になっています（Account ID を入れてください）' if same_as_key else ''), flush=True)
    return f"https://{acct}.r2.cloudflarestorage.com"


s3 = boto3.client('s3', endpoint_url=r2_endpoint(),
                  aws_access_key_id=os.environ['R2_ACCESS_KEY_ID'],
                  aws_secret_access_key=os.environ['R2_SECRET_ACCESS_KEY'], region_name='auto')
MAX_SEC = float(os.environ.get('MAX_MINUTES', 12)) * 60
T0 = time.time()


def jst_now():
    return datetime.datetime.utcnow() + datetime.timedelta(hours=9)


def load_day(date):
    try:
        return json.loads(s3.get_object(Bucket=BUCKET, Key=f'odds/{date:%Y-%m-%d}.json')['Body'].read())
    except Exception:
        return {}


def save_day(date, data):
    s3.put_object(Bucket=BUCKET, Key=f'odds/{date:%Y-%m-%d}.json',
                  Body=json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode('utf-8'),
                  ContentType='application/json')


def race_times(date):
    """開催一覧のページから、レースID → 発走時刻"""
    url = f"https://race.netkeiba.com/top/race_list_sub.html?kaisai_date={date:%Y%m%d}"
    html = M.http_get(url, timeout=15).text
    out = {}
    ids = [(m.start(), m.group(1)) for m in re.finditer(r'race_id=(\d{12})', html)]
    for k, (pos, rid) in enumerate(ids):
        end = ids[k + 1][0] if k + 1 < len(ids) else pos + 3000
        tm = re.search(r'(\d{1,2}):(\d{2})', html[pos:end])
        if rid not in out or (tm and not out[rid]):
            out[rid] = f"{int(tm.group(1)):02d}:{tm.group(2)}" if tm else ''
    return out


def post_time(rid, known):
    """一覧で時刻が読めなかったレースは、出馬表から読む（1日1回だけ）"""
    if known:
        return known, ''
    try:
        soup = M.fetch_soup(f"https://race.netkeiba.com/race/shutuba.html?race_id={rid}")
        info = M.parse_race_info(soup, rid, 'https://race.netkeiba.com')
        return info.get('time', ''), info.get('name', '')
    except Exception:
        return '', ''


def minutes_to(date, hhmm, now):
    try:
        h, m = map(int, hhmm.split(':'))
    except Exception:
        return None
    return int((datetime.datetime(date.year, date.month, date.day, h, m) - now).total_seconds() // 60)


def snap_day(date, now):
    try:
        times = race_times(date)
    except Exception as e:
        print(f"{date} 開催一覧を読めず：{e}", flush=True)
        return 0
    if not times:
        print(f"{date} 中央競馬の開催なし", flush=True)
        return 0
    data = load_day(date)
    stamp = now.strftime('%Y-%m-%d %H:%M')
    got = 0
    for rid in sorted(times):
        if time.time() - T0 > MAX_SEC:
            print('時間切れ。続きは次の回に', flush=True)
            break
        e = data.setdefault(rid, {'name': '', 'time': '', 'snaps': []})
        if not e.get('time'):
            e['time'], nm = post_time(rid, times.get(rid))
            e['name'] = e.get('name') or nm
        mins = minutes_to(date, e['time'], now)
        if mins is not None and mins < -2:
            continue                          # 発走した（最後の記録は発走直前のもの）
        if e['snaps'] and e['snaps'][-1][0] == stamp:
            continue
        odds = M.fetch_win_odds(rid, 'https://race.netkeiba.com')
        if not odds:
            continue                          # まだ発売前
        e['snaps'].append([stamp, mins, {str(n): o for n, o in sorted(odds.items())}])
        got += 1
    save_day(date, data)
    print(f"{date}：{len(times)}レース中 {got}レースのオッズを記録", flush=True)
    return got


def main():
    now = jst_now()
    total = 0
    for k in (0, 1):                          # 今日と明日（明日の分＝前日オッズ）
        total += snap_day(now.date() + datetime.timedelta(days=k), now)
    print(f"完了：{total}レース分（{(time.time() - T0) / 60:.1f}分）", flush=True)


if __name__ == '__main__':
    main()
