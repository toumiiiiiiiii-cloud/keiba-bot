import os
import re
import requests
from bs4 import BeautifulSoup
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

@app.route("/", methods=['GET'])
def index():
    return "OK"

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature')
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return 'OK'

def get_real_netkeiba_data(race_id):
    """PC版とスマホ版の両方に対応し、文字化けを自動で防ぐ関数"""
    urls = [
        f"https://race.netkeiba.com/race/shutuba.html?race_id={race_id}",
        f"https://race.sp.netkeiba.com/race/shutuba.html?race_id={race_id}",
        f"https://nar.netkeiba.com/race/shutuba.html?race_id={race_id}"
    ]

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }

    for url in urls:
        try:
            res = requests.get(url, headers=headers, timeout=10)
            if res.status_code != 200:
                continue

            # ★文字化け対策の決定版★
            # apparent_encodingを使用して、サイトの文字コードを自動判定させる
            res.encoding = res.apparent_encoding
            html = res.text
            soup = BeautifulSoup(html, 'html.parser')
            
            horses = []

            # --------------------------------------------------
            # 優先1: PC版出馬表（最もデータが正確）
            # --------------------------------------------------
            rows = soup.find_all('tr', class_=re.compile(r'HorseList'))
            if rows:
                for row in rows:
                    umaban_td = row.select_one('td.Umaban')
                    name_td = row.select_one('span.HorseName a') or row.select_one('.HorseName')
                    jockey_td = row.select_one('td.Jockey a') or row.select_one('.Jockey')

                    if name_td:
                        name = name_td.text.strip()
                        umaban_txt = umaban_td.text.strip() if umaban_td else ""
                        jockey = jockey_td.text.strip() if jockey_td else "未定"

                        name = re.sub(r'\s+', '', name)
                        umaban = re.sub(r'\D', '', umaban_txt)

                        # "馬名"というヘッダー行を除外
                        if name and name != "馬名":
                            horses.append({
                                'umaban': umaban if umaban else "未定",
                                'name': name,
                                'jockey': jockey
                            })
                
                if horses:
                    return horses

            # --------------------------------------------------
            # 優先2: スマホ版出馬表 または 特別登録馬
            # --------------------------------------------------
            name_tags = soup.select('.HorseName a, .Horse_Name a, .HorseName, .Horse_Info .Name')
            if name_tags:
                for tag in name_tags:
                    name = tag.text.strip()
                    name = re.sub(r'\s+', '', name)

                    # 既に追加済みの馬（重複）やヘッダー文字を除外
                    if name and name != "馬名" and name not in [h['name'] for h in horses]:
                        horses.append({
                            'umaban': "枠順未定",
                            'name': name,
                            'jockey': "確認中"
                        })
                
                if horses:
                    return horses

        except Exception as e:
            print(f"Scraping Error ({url}): {e}")
            continue

    return []

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_text = event.message.text

    match = re.search(r'race_id=(\d{12})', user_text) or re.search(r'\b(\d{12})\b', user_text)

    if match:
        race_id = match.group(1)
        horses = get_real_netkeiba_data(race_id)

        if horses:
            total_count = len(horses)
            is_confirmed = horses[0]['umaban'] not in ["未定", "枠順未定"]
            status_text = "【枠順確定済み】" if is_confirmed else "【枠順未確定（特別登録馬）】"

            # 表示用にリストを作成（上位5頭と最後の大外馬）
            display_lines = []
            for i, h in enumerate(horses[:5]):
                display_lines.append(f"{h['umaban']}番: {h['name']} (騎手: {h['jockey']})")
            
            if total_count > 5:
                display_lines.append("...")
                last_h = horses[-1]
                display_lines.append(f"大外: {last_h['name']} (騎手: {last_h['jockey']})")
            
            sample_text = "\n".join(display_lines)

            reply_text = (
                f"✅ データを正しく取得しました\n"
                f"📍 レースID: {race_id}\n"
                f"状態: {status_text}\n"
                f"出走/登録頭数: {total_count}頭\n"
                f"--------------------\n"
                f"【取得馬名データ（一部）】\n"
                f"{sample_text}\n"
                f"--------------------\n"
                f"※本物の出走データを文字化けなしで取得できました。"
            )
        else:
            reply_text = f"レースID: {race_id} のデータを取得できませんでした。"
    else:
        reply_text = "netkeibaの出馬表URLを送信してください。"

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_text)
    )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
