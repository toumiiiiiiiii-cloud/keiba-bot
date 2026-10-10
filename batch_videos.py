# -*- coding: utf-8 -*-
"""
展開動画の作り置き（GitHub Actions から1日数回動かす）

  1. 今日から3日先までの中央競馬の開催を netkeiba で確認（祝日の月曜開催も自動で入る）
  2. 枠順が出ているレースのうち、動画が無い・顔ぶれ（取消）や馬場が変わったものだけ作る
  3. 1000回シミュレーション → 代表レースの動画（mp4）とプレビュー画像を Cloudflare R2 へ
  4. 一覧 index.json を更新（Bot はこれを見て動画を送る）。1週間より古い分は消す

必要な環境変数（GitHub の Secrets）:
  R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET
  R2_PUBLIC_URL  … 公開URL（例 https://pub-xxxx.r2.dev）。末尾の / は不要
任意:
  MAX_MINUTES   … この分数を超えたら次の回に回す（既定 75）
  DAYS_AHEAD    … 何日先まで見るか（既定 3）
"""
import os
import re
import sys
import json
import time
import hashlib
import datetime
import traceback

os.environ.setdefault('SYH_BATCH', '1')     # netkeibaへゆっくりアクセスする
os.environ.setdefault('SYH_NOFONT', '1')    # main.py のフォント準備は不要（動画は sim_video が自分で探す）
import boto3                                 # noqa: E402
import main as M                             # noqa: E402
import sim_video as SV                       # noqa: E402

PUBLIC = os.environ['R2_PUBLIC_URL'].rstrip('/')
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
T0 = time.time()
MAX_SEC = float(os.environ.get('MAX_MINUTES', 75)) * 60
DAYS_AHEAD = int(os.environ.get('DAYS_AHEAD', 3))
CACHE_FILE = '.cache/careers.json'


# ── 馬の全成績はディスクにも保存（Actions のキャッシュで次の回へ持ち越し、netkeibaへのアクセスを減らす）──
def _load_disk():
    try:
        with open(CACHE_FILE, encoding='utf-8') as f:
            d = json.load(f)
        now = time.time()
        return {k: v for k, v in d.items() if now - v[0] < 43200}
    except Exception:
        return {}


DISK = _load_disk()
_orig_fetch_career = M.fetch_career


def fetch_career_disk(hid):
    hit = DISK.get(hid)
    if hit:
        return hit[1]
    v = _orig_fetch_career(hid)
    if v:
        DISK[hid] = [time.time(), v]
    return v


M.fetch_career = fetch_career_disk


def save_disk():
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    with open(CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(DISK, f, ensure_ascii=False)


# ── R2 ──
def load_index():
    try:
        return json.loads(s3.get_object(Bucket=BUCKET, Key='index.json')['Body'].read())
    except Exception:
        return {}


def save_index(idx):
    s3.put_object(Bucket=BUCKET, Key='index.json', Body=json.dumps(idx, ensure_ascii=False).encode('utf-8'),
                  ContentType='application/json', CacheControl='max-age=60')


def put_file(path, key, ctype):
    with open(path, 'rb') as f:
        s3.put_object(Bucket=BUCKET, Key=key, Body=f, ContentType=ctype, CacheControl='max-age=604800')
    return f"{PUBLIC}/{key}"


def delete_keys(*keys):
    for k in keys:
        try:
            s3.delete_object(Bucket=BUCKET, Key=k)
        except Exception:
            pass


# ── レースごとの判定 ──
def quick_state(rid):
    """出馬表だけ読んで、枠順が出ているか・顔ぶれ・馬場を返す（全成績はまだ読まない）"""
    soup = M.fetch_soup(f"https://race.netkeiba.com/race/shutuba_past.html?race_id={rid}")
    info = M.parse_race_info(soup, rid, 'https://race.netkeiba.com')
    hs = [h for h in M.parse_shutuba_past(soup) if not h['cancelled']]
    if not hs or not all(h.get('n') and h.get('w') for h in hs):
        return None                                   # 枠順がまだ
    going = info['going'] if info.get('going_known') else '?'
    return {'horses': '-'.join(str(n) for n in sorted(h['n'] for h in hs)), 'going': going,
            'time': info.get('time', ''), 'name': info.get('name', '')}


def started(date, hhmm):
    now = datetime.datetime.utcnow() + datetime.timedelta(hours=9)
    try:
        h, m = map(int, hhmm.split(':'))
    except Exception:
        return date < now.date()
    return datetime.datetime(date.year, date.month, date.day, h, m) <= now


def build(rid, date, idx):
    race_arr, err = M.analyze(rid)
    if err:
        print(f"  {rid} 予想できず: {err}", flush=True)
        return False
    race, arr = race_arr
    sim = SV.simulate(race, arr)
    if not sim:
        return False
    sig = M.horse_sig(arr)
    going = race['going'] if race.get('going_known') else '?'
    tag = hashlib.md5(f"{sig}|{going}|{time.time()}".encode()).hexdigest()[:8]
    mp4, jpg = f"/tmp/{rid}.mp4", f"/tmp/{rid}.jpg"
    SV.make_video(race, arr, sim, mp4, jpg)
    old = idx.get(rid)
    e = {'mp4': put_file(mp4, f"videos/{rid}_{tag}.mp4", 'video/mp4'),
         'jpg': put_file(jpg, f"videos/{rid}_{tag}.jpg", 'image/jpeg'),
         'horses': sig, 'going': going, 'date': date.isoformat(), 'shape': M.pred_shape(race),
         'name': race.get('name', ''), 'summary': SV.summary_line(sim),
         'made': (datetime.datetime.utcnow() + datetime.timedelta(hours=9)).strftime('%Y-%m-%d %H:%M')}
    idx[rid] = e
    save_index(idx)
    if old:   # 前の版は消す
        delete_keys(*(old[k].replace(PUBLIC + '/', '') for k in ('mp4', 'jpg') if old.get(k)))
    for p in (mp4, jpg):
        try:
            os.remove(p)
        except Exception:
            pass
    print(f"  {rid} {race.get('venue')}{race.get('R')}R {race.get('name')} → 作成（{e['summary']}）", flush=True)
    return True


def cleanup(idx, today):
    limit = (today - datetime.timedelta(days=7)).isoformat()
    gone = [rid for rid, e in idx.items() if e.get('date', '') < limit]
    for rid in gone:
        e = idx.pop(rid)
        delete_keys(*(e[k].replace(PUBLIC + '/', '') for k in ('mp4', 'jpg') if e.get(k)))
    if gone:
        save_index(idx)
        print(f"古い動画を削除：{len(gone)}件", flush=True)


def main():
    today = (datetime.datetime.utcnow() + datetime.timedelta(hours=9)).date()
    idx = load_index()
    cleanup(idx, today)
    made = skipped = waiting = failed = 0
    for k in range(DAYS_AHEAD + 1):
        date = today + datetime.timedelta(days=k)
        try:
            ids = M.race_ids_on(date)
        except Exception as e:
            print(f"{date} 開催一覧を読めず: {e}", flush=True)
            continue
        if not ids:
            continue
        print(f"{date}：{len(ids)}レース", flush=True)
        for rid in ids:
            if time.time() - T0 > MAX_SEC:
                print('時間切れ。残りは次の回に作ります', flush=True)
                save_disk()
                return
            try:
                st = quick_state(rid)
                if not st:
                    waiting += 1
                    continue
                if started(date, st['time']):
                    continue
                e = idx.get(rid)
                if e and e.get('horses') == st['horses'] and e.get('going') == st['going']:
                    skipped += 1
                    continue
                if build(rid, date, idx):
                    made += 1
                else:
                    failed += 1
            except Exception:
                failed += 1
                traceback.print_exc()
        save_disk()
    save_disk()
    print(f"完了：作成{made}・作成済み{skipped}・枠順待ち{waiting}・失敗{failed}（{(time.time() - T0) / 60:.1f}分）", flush=True)
    if failed and not made:
        sys.exit(1)


if __name__ == '__main__':
    main()
