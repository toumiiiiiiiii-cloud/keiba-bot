import os
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
    PushMessageRequest,
    TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

app = Flask(__name__)

# 環境変数からLINEのキーを取得
CHANNEL_SECRET = os.getenv('LINE_CHANNEL_SECRET')
CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')

configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# ---------------------------------------------------------
# netkeibaから出馬表を取得して予想する関数
# ---------------------------------------------------------
def get_netkeiba_prediction(race_id="202505010811"):
    """
    netkeibaの出馬表ページからデータを取得し、簡易予想を行います。
    ※ race_id 例: 202505010811 (2025年 5回東京1日 8レース11番など)
    """
    url = f"https://race.netkeiba.com/race/shutuba.html?race_id={race_id}"
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    
    try:
        # アクセス過多防止のため安全に取得
        time.sleep(2)
        response = requests.get(url, headers=headers)
        response.encoding = 'EUC-JP'  # netkeibaの文字コード指定
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # レース名の取得
        race_name_tag = soup.find('div', class_='RaceName')
        race_name = race_name_tag.text.strip() if race_name_tag else "対象レース"
        
        # 出馬表テーブルから馬名とオッズを取得
        horses = []
        rows = soup.find_all('tr', class_='HorseList')
        
        for row in rows:
            horse_name_tag = row.find('span', class_='HorseName')
            odds_tag = row.find('span', class_='Odds')
            
            if horse_name_tag:
                name = horse_name_tag.text.strip()
                odds = odds_tag.text.strip() if odds_tag else "---"
                horses.append({'name': name, 'odds': odds})
        
        if not horses:
            # 取得できない場合のサンプル代替動作
            return f"【{race_name}】の出馬表が取得できませんでした（レースIDを確認してください）。"
        
        # --- AI/予想ロジックエリア ---
        # 本命（本命馬）や対抗の選定（ここでは先頭やオッズ上位から選定する例）
        honmei = horses[0]['name'] if len(horses) > 0 else "不明"
        taikou = horses[1]['name'] if len(horses) > 1 else "不明"
        anama  = horses[2]['name'] if len(horses) > 2 else "不明"
        
        # LINEに送るメッセージ文面の作成
        msg = f"🏇【AI競馬予想結果】🏇\n"
        msg += f"レース: {race_name}\n"
        msg += f"----------------------\n"
        msg += f"◎ 本命: {honmei}\n"
        msg += f"○ 対抗: {taikou}\n"
        msg += f"▲ 穴馬: {anama}\n"
        msg += f"----------------------\n"
        msg += f"※ netkeibaより自動取得して予想を行いました。"
        
        return msg

    except Exception as e:
        return f"データ取得時にエラーが発生しました: {str(e)}"

# ---------------------------------------------------------
# LINEからのメッセージ受信処理 (WebHook)
# ---------------------------------------------------------
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
    user_text = event.message.text
    
    # ユーザーが「予想」または「レースID（数字）」を送ったときに実行
    if "予想" in user_text or user_text.isdigit():
        # デフォルトまたは入力されたレースIDでnetkeibaを取得・予想
        race_id = user_text if user_text.isdigit() else "202505010811"
        reply_text = get_netkeiba_prediction(race_id)
    else:
        reply_text = "「予想」または「12桁のレースID（例: 202505010811）」と送信すると、netkeibaから出馬表を取得して予想します！"

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
