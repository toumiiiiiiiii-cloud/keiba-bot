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

def get_netkeiba_data(race_id):
    """文字化けを直して馬名・馬番を綺麗に取得する関数"""
    urls = [
        f"https://race.netkeiba.com/race/shutuba.html?race_id={race_id}",
        f"https://nar.netkeiba.com/race/shutuba.html?race_id={race_id}"
    ]
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }

    for url in urls:
        try:
            res = requests.get(url, headers=headers, timeout=10)
            
            # EUC-JPで直接デコードして文字化けを解消
            html = res.content.decode('euc-jp', errors='replace')
            soup = BeautifulSoup(html, 'html.parser')
            
            horses = []
            rows = soup.select('tr.HorseList')
            
            for row in rows:
                umaban_elem = row.select_one('.Umaban')
                # 馬名はリンクタグ内から正確に取得
                name_elem = row.select_one('.HorseName a') or row.select_one('.HorseName')
                
                if umaban_elem and name_elem:
                    umaban = umaban_elem.text.strip()
                    name = name_elem.text.strip()
                    if umaban and name:
                        horses.append({'umaban': umaban, 'name': name})
            
            if horses:
                return horses

        except Exception as e:
            print(f"取得エラー ({url}): {e}")
            continue

    return []

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_text = event.message.text

    # レースID（12桁の数字）を読み取る
    match = re.search(r'race_id=(\d{12})', user_text) or re.search(r'\b(\d{12})\b', user_text)

    if match:
        race_id = match.group(1)
        horses = get_netkeiba_data(race_id)

        if horses:
            total_count = len(horses)
            
            honmei = horses[0] if len(horses) > 0 else {"umaban": "-", "name": "不明"}
            taikou = horses[1] if len(horses) > 1 else {"umaban": "-", "name": "不明"}
            anama  = horses[2] if len(horses) > 2 else {"umaban": "-", "name": "不明"}

            reply_text = (
                f"🏇【AI予想結果】🏇\n"
                f"📍 レースID: {race_id}\n"
                f"--------------------\n"
                f"◎ 本命: {honmei['umaban']}番 {honmei['name']}\n"
                f"◯ 対抗: {taikou['umaban']}番 {taikou['name']}\n"
                f"▲ 穴馬: {anama['umaban']}番 {anama['name']}\n"
                f"--------------------\n"
                f"出走頭数: {total_count}頭\n"
                f"※ netkeibaから自動取得しました。"
            )
        else:
            reply_text = f"レースID: {race_id} の出走表データが取得できませんでした。"
    else:
        reply_text = "netkeibaのレースURLを送信してください！"

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_text)
    )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
