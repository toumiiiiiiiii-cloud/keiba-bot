import os
import re
import time
import requests
from bs4 import BeautifulSoup
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

app = Flask(__name__)

CHANNEL_SECRET = os.getenv('LINE_CHANNEL_SECRET')
CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')

configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# 12桁のレースIDをテキストやURLから抽出する関数
def extract_race_id(text):
    match = re.search(r'\d{12}', text)
    if match:
        return match.group(0)
    return None

# netkeibaから該当レースを取得して予想する関数
def get_netkeiba_prediction(race_id):
    url = f"https://race.netkeiba.com/race/shutuba.html?race_id={race_id}"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    
    try:
        time.sleep(1)
        response = requests.get(url, headers=headers)
        response.encoding = 'EUC-JP'
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # レース名
        race_name_tag = soup.find('div', class_='RaceName')
        race_name = race_name_tag.text.strip() if race_name_tag else f"レースID: {race_id}"
        
        # 馬名・馬番・オッズの取得
        horses = []
        rows = soup.find_all('tr', class_='HorseList')
        for row in rows:
            horse_name_tag = row.find('span', class_='HorseName')
            umaban_tag = row.find('td', class_='Umaban')
            odds_tag = row.find('span', class_='Odds')
            
            if horse_name_tag:
                name = horse_name_tag.text.strip()
                umaban = umaban_tag.text.strip() if umaban_tag else "?"
                odds = odds_tag.text.strip() if odds_tag else "---"
                horses.append({'umaban': umaban, 'name': name, 'odds': odds})
        
        if not horses:
            return f"⚠️ レースID[{race_id}]の出馬表を取得できませんでした。\nURLが正しいか、出馬表が発表されているかご確認ください。"
        
        # 予想ロジック（上位出走馬から選定）
        honmei = horses[0] if len(horses) > 0 else {"umaban": "-", "name": "不明"}
        taikou = horses[1] if len(horses) > 1 else {"umaban": "-", "name": "不明"}
        anama  = horses[2] if len(horses) > 2 else {"umaban": "-", "name": "不明"}
        
        msg = f"🏇【AI予想結果】🏇\n"
        msg += f"📍 {race_name}\n"
        msg += f"----------------------\n"
        msg += f"◎ 本命: {honmei['umaban']}番 {honmei['name']}\n"
        msg += f"○ 対抗: {taikou['umaban']}番 {taikou['name']}\n"
        msg += f"▲ 穴馬: {anama['umaban']}番 {anama['name']}\n"
        msg += f"----------------------\n"
        msg += f"出走頭数: {len(horses)}頭\n"
        msg += f"※ netkeibaから自動取得しました。"
        return msg

    except Exception as e:
        return f"エラーが発生しました: {str(e)}"

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_text = event.message.text.strip()
    
    # URLやテキストの中から12桁のレースIDを探す
    race_id = extract_race_id(user_text)
    
    if race_id:
        reply_text = get_netkeiba_prediction(race_id)
    else:
        reply_text = (
            "【使い方】\n"
            "netkeibaの出馬表ページの「URL」か「12桁のレースID」を送信してください！\n\n"
            "例:\n"
            "https://race.netkeiba.com/race/shutuba.html?race_id=202405010811"
        )

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply_text)]
            )
        )

if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
