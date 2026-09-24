import os
import re
import sys
import requests
from bs4 import BeautifulSoup
import pandas as pd
import joblib
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN) if LINE_CHANNEL_ACCESS_TOKEN else None
handler = WebhookHandler(LINE_CHANNEL_SECRET or 'dummy')

model_path = 'keiba_ai_model.pkl'
ai_model = joblib.load(model_path) if os.path.exists(model_path) else None

HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/124.0 Safari/537.36'),
    'Accept-Language': 'ja,en;q=0.8',
}
DEFAULT_ODDS = 50.0
DEFAULT_WEIGHT = 55.0
DUMMY_TIME = 100.0

# 馬の個別ページは /horse/ の後に10桁の数字。これで「お気に入り馬」等のメニューリンクを除外する
HORSE_HREF = re.compile(r'/horse/\d{10}')


# ─────────────────────────────
# 共通ユーティリティ
# ─────────────────────────────
def clean(text):
    return re.sub(r'\s+', '', text or '')


def to_float(text):
    """'57.0' '3.5' などを数値化。'---.-' や空欄は None"""
    if text is None:
        return None
    m = re.search(r'\d+(?:\.\d+)?', str(text))
    return float(m.group()) if m else None


def fetch_soup(url):
    """文字コードを自前で判定してデコードする（apparent_encoding に頼らない）"""
    res = requests.get(url, headers=HEADERS, timeout=10)
    res.raise_for_status()
    raw = res.content

    m = re.search(rb'charset=["\']?([\w-]+)', raw[:3000], re.I)
    enc = m.group(1).decode('ascii').lower() if m else 'euc-jp'

    if 'utf' in enc:
        html = raw.decode('utf-8', errors='replace')
    else:
        # netkeiba は EUC-JP。まず厳密に、ダメなら拡張漢字（髙など）対応の codec で
        try:
            html = raw.decode('euc_jp')
        except UnicodeDecodeError:
            html = raw.decode('euc_jis_2004', errors='replace')
    return BeautifulSoup(html, 'html.parser')


def parse_input(text):
    """LINEで送られた文字列から (race_id, ページ種別, ベースURL) を取り出す"""
    m = re.search(r'race_id=(\d{12})', text) or re.search(r'/race/(\d{12})', text)
    if not m:
        return None, None, None
    race_id = m.group(1)
    base = 'https://nar.netkeiba.com' if 'nar.' in text else 'https://race.netkeiba.com'
    # db.netkeiba.com/race/xxxx は結果ページ扱い
    is_result = ('result' in text) or ('db.netkeiba.com/race/' in text)
    return race_id, ('result' if is_result else 'shutuba'), base


# ─────────────────────────────
# 出馬表（shutuba.html）
# ─────────────────────────────
def fetch_win_odds(race_id, base):
    """出馬表のオッズはJavaScriptで後から読み込まれるため、HTMLには入っていない。
    netkeibaのオッズAPIから直接 {馬番: 単勝オッズ} を取る（JRAのみ）"""
    if 'nar.' in base:
        return {}
    url = ('https://race.netkeiba.com/api/api_get_jra_odds.html'
           f'?race_id={race_id}&type=1&action=update')
    try:
        js = requests.get(url, headers=HEADERS, timeout=10).json()
        data = js.get('data')
        if not isinstance(data, dict):
            return {}
        win = (data.get('odds') or {}).get('1') or {}
        result = {}
        for umaban, vals in win.items():
            val = to_float(vals[0]) if vals else None
            if val:
                result[int(umaban)] = val
        return result
    except Exception:
        return {}


def parse_shutuba(soup, race_id, base):
    rows = soup.select('table.Shutuba_Table tr.HorseList') or soup.select('tr.HorseList')
    api_odds = fetch_win_odds(race_id, base)
    horses = []

    for tr in rows:
        a = tr.select_one('.HorseName a') or tr.find('a', href=HORSE_HREF)
        if not a or not HORSE_HREF.search(a.get('href', '')):
            continue

        umaban_td = tr.select_one('td[class*="Umaban"]')
        umaban = int(to_float(umaban_td.get_text())) if umaban_td and to_float(umaban_td.get_text()) else None

        jockey_a = tr.select_one('td.Jockey a') or tr.find('a', href=re.compile(r'/jockey/'))
        jockey = clean(jockey_a.get_text()) if jockey_a else '不明'

        # 斤量は「性齢」セルの次のセル
        weight = None
        barei = tr.select_one('td.Barei')
        if barei:
            nxt = barei.find_next_sibling('td')
            if nxt:
                weight = to_float(nxt.get_text())
        if weight is None or not (45 <= weight <= 65):
            weight = None
            for td in tr.find_all('td'):
                t = clean(td.get_text())
                if re.fullmatch(r'[☆▲△★◇]?\d{2}\.\d', t):
                    weight = to_float(t)
                    break

        # オッズ：API → 無ければHTML内のspan（確定後などに入っている場合がある）
        odds = api_odds.get(umaban) if umaban else None
        if odds is None:
            span = tr.select_one('span[id^="odds-"]')
            odds = to_float(span.get_text()) if span else None

        row_text = tr.get_text()
        cancelled = 'Cancel' in tr.get('class', []) or '取消' in row_text or '除外' in row_text

        horses.append({'馬番': umaban, '馬名': clean(a.get_text()), '騎手': jockey,
                       '斤量': weight, '単勝オッズ': odds, '取消': cancelled})
    return horses


# ─────────────────────────────
# 結果ページ（result.html）
# ─────────────────────────────
def parse_result(soup):
    table = soup.select_one('table#All_Result_Table') or soup.select_one('table.RaceTable01')
    if not table:
        return []

    # 結果ページは見出しが文字なので、見出し名から列番号を決める
    header = table.select_one('tr.Header') or table.find('tr')
    cols = [clean(c.get_text()) for c in header.find_all(['th', 'td'])]

    def col(*keys):
        return next((i for i, c in enumerate(cols) if any(k in c for k in keys)), None)

    i_umaban, i_kin, i_odds = col('馬番'), col('斤量'), col('単勝', 'オッズ')

    def cell(tds, i):
        return tds[i].get_text() if i is not None and i < len(tds) else None

    horses = []
    for tr in table.select('tr.HorseList') or table.find_all('tr')[1:]:
        a = tr.find('a', href=HORSE_HREF)
        if not a:
            continue
        tds = tr.find_all('td')
        jockey_a = tr.find('a', href=re.compile(r'/jockey/'))
        umaban = to_float(cell(tds, i_umaban))
        rank_text = clean(tds[0].get_text()) if tds else ''

        horses.append({
            '馬番': int(umaban) if umaban else None,
            '馬名': clean(a.get_text()),
            '騎手': clean(jockey_a.get_text()) if jockey_a else '不明',
            '斤量': to_float(cell(tds, i_kin)),
            '単勝オッズ': to_float(cell(tds, i_odds)),
            '取消': not rank_text.isdigit() and rank_text in ('取消', '除外', '中止'),
        })
    return horses


# ─────────────────────────────
# まとめ
# ─────────────────────────────
def scrape_race(text):
    race_id, page, base = parse_input(text)
    if not race_id:
        return None, [], None

    url = f'{base}/race/{page}.html?race_id={race_id}'
    soup = fetch_soup(url)

    title = soup.select_one('.RaceName') or soup.select_one('h1')
    race_name = clean(title.get_text()) if title else '対象レース'

    horses = parse_shutuba(soup, race_id, base) if page == 'shutuba' else parse_result(soup)
    return race_name, horses, page


def generate_ai_prediction(text):
    if ai_model is None:
        return "エラー：AIモデルが読み込めませんでした。"
    try:
        race_name, horses, page = scrape_race(text)
        if race_name is None:
            return "URLからレースIDを読み取れませんでした。"

        horses = [h for h in horses if not h['取消']]
        if not horses:
            return "出馬表または結果のデータが見つかりませんでした。"

        df = pd.DataFrame(horses)
        no_odds = df['単勝オッズ'].isna().all()
        df['単勝オッズ'] = df['単勝オッズ'].fillna(DEFAULT_ODDS)
        df['斤量'] = df['斤量'].fillna(DEFAULT_WEIGHT)
        df['タイム_秒'] = DUMMY_TIME

        X = df[['単勝オッズ', '斤量', 'タイム_秒']]
        df['AI勝率'] = ai_model.predict_proba(X)[:, 1] * 100
        top = df.sort_values('AI勝率', ascending=False).head(5)

        reply = f"🟩 AI適性スコア予想 🟩\n{race_name}\n\n"
        ranks = ['【ランクS】1位', '【ランクA】2位', '【ランクB】3位', '【ランクB】4位', '【ランクC】5位']
        for i, (_, r) in enumerate(top.iterrows()):
            num = f"{int(r['馬番'])}番 " if pd.notna(r['馬番']) else ""
            reply += f"{ranks[i]}\n"
            reply += f"🐎 {num}{r['馬名']}\n"
            reply += f"👤 {r['騎手']}\n"
            reply += f"📊 ｵｯｽﾞ {r['単勝オッズ']}倍 / 斤量 {r['斤量']}kg\n"
            reply += f"📈 AI予測勝率 {r['AI勝率']:.1f}%\n\n"

        if no_odds:
            reply += "⚠ オッズ未発表のため仮オッズ(50.0倍)で計算しています。\n"
        reply += "※実際のオッズと斤量データを元にAIが算出しています。\n（走破タイムは仮数値を代入して計算）"
        return reply

    except requests.HTTPError as e:
        return f"netkeibaへのアクセスに失敗しました（{e.response.status_code}）。"
    except Exception as e:
        return f"予想中にエラーが発生しました。\n詳細: {str(e)}"


@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text.strip()
    if "netkeiba.com" in text:
        reply_text = generate_ai_prediction(text)
    else:
        reply_text = "netkeibaの出馬表、またはレース結果のURLを送信してください！"
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))


if __name__ == "__main__":
    # 動作確認用： python main.py <netkeibaのURL> でスクレイピング結果だけ表示
    if len(sys.argv) > 1:
        name, rows, page = scrape_race(sys.argv[1])
        print(name, page)
        for h in rows:
            print(h)
    else:
        port = int(os.environ.get("PORT", 5000))
        app.run(host="0.0.0.0", port=port)
