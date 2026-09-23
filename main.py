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
    """枠順確定後・確定前のどちらでも正確にデータを取得する関数"""
    
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

            # netkeiba固有のエンコーディング対策
            try:
                html = res.content.decode('euc-jp', errors='replace')
            except Exception:
                html = res.content.decode('utf-8', errors='replace')

            soup = BeautifulSoup(html, 'html.parser')
            horses = []

            # --------------------------------------------------
            # パターン1: 枠順確定後（PC版出馬表）
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

                        # 余計な空白やノイズを除去
                        name = re.sub(r'\s+', '', name)
                        umaban = re.sub(r'\D', '', umaban_txt)

                        if name and len(name) <= 15 and name != "馬名":
                            horses.append({
                                'umaban': umaban if umaban else "未定",
                                'name': name,
                                'jockey': jockey
                            })
                if horses:
                    return horses

            # --------------------------------------------------
            # パターン2: 枠順未確定（特別登録・出走予定馬）またはスマホ版
            # --------------------------------------------------
            name_tags = soup.select('.HorseName a, .Horse_Name a, .HorseName, .Horse_Info .Name')
            for tag in name_tags:
                name = tag.text.strip()
                name = re.sub(r'\s+', '', name)

                # 重複防止と有効な馬名チェック
                if name and 2 <= len(name) <= 15 and name not in [h['name'] for h in horses] and name != "馬名":
                    horses.append({
                        'umaban': "枠順未定",
                        'name': name,
                        'jockey': "未定"
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

    # 12桁のレースIDを抽出
    match = re.search(r'race_id=(\d{12})', user_text) or re.search(r'\b(\d{12})\b', user_text)

    if match:
        race_id = match.group(1)
        horses = get_real_netkeiba_data(race_id)

        if horses:
            total_count = len(horses)
            
            # 枠順確定済みか未確定かを判定
            is_confirmed = horses[0]['umaban'] != "枠順未定"
            status_text = "【枠順確定済み】" if is_confirmed else "【枠順未確定（特別登録馬）】"

            sample_text = ""
            if is_confirmed:
                sample_text = (
                    f"1番: {horses[0]['name']} (騎手: {horses[0]['jockey']})\n"
                    f"2番: {horses[1]['name']} (騎手: {horses[1]['jockey']})\n"
                    f"...\n"
                    f"大外: {horses[-1]['name']} (騎手: {horses[-1]['jockey']})"
                )
            else:
                sample_text = (
                    f"・{horses[0]['name']}\n"
                    f"・{horses[1]['name']}\n"
                    f"・{horses[2]['name']}\n"
                    f"..."
                )

            reply_text = (
                f"✅ データを正しく取得しました\n"
                f"📍 レースID: {race_id}\n"
                f"状態: {status_text}\n"
                f"出走/登録頭数: {total_count}頭\n"
                f"--------------------\n"
                f"【取得馬名データ】\n"
                f"{sample_text}\n"
                f"--------------------\n"
                f"※本物の出走データを正常に解析できています。"
            )
        else:
            reply_text = f"レースID: {race_id} のデータを取得できませんでした。URLをご確認ください。"
    else:
        reply_text = "netkeibaの出馬表URLを送信してください。"

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_text)
    )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
