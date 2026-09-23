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

def get_netkeiba_data(user_text, race_id):
    """中央競馬・地方競馬・スマホ版URLの全てに対応したデータ取得関数"""
    
    # ユーザーが送ってきたURLから中央/地方を判定、無ければIDで判定
    is_nar = 'nar.sp.netkeiba.com' in user_text or 'nar.netkeiba.com' in user_text
    
    # URLの組み立て
    if is_nar:
        urls = [
            f"https://nar.sp.netkeiba.com/race/shutuba.html?race_id={race_id}",
            f"https://nar.netkeiba.com/race/shutuba.html?race_id={race_id}"
        ]
    else:
        urls = [
            f"https://race.sp.netkeiba.com/race/shutuba.html?race_id={race_id}",
            f"https://race.netkeiba.com/race/shutuba.html?race_id={race_id}"
        ]

    headers = {
        'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Mobile/15E148 Safari/604.1'
    }

    for url in urls:
        try:
            res = requests.get(url, headers=headers, timeout=8)
            
            # 文字コードの判定（UTF-8 または EUC-JP）
            try:
                html = res.content.decode('utf-8')
            except UnicodeDecodeError:
                html = res.content.decode('euc-jp', errors='ignore')
                
            soup = BeautifulSoup(html, 'html.parser')
            horses = []

            # 抽出パターン1 (スマホ版出走表)
            items = soup.select('.HorseList, .RaceList_DataItem, tr.HorseList')
            for item in items:
                umaban_elem = item.select_one('.Umaban, .Umaban_Num, .td_umaban')
                name_elem = item.select_one('.HorseName, .Horse_Name, .Horse_Info .Name')
                
                if umaban_elem and name_elem:
                    u_txt = re.sub(r'\D', '', umaban_elem.text)
                    n_txt = name_elem.text.strip().replace('\n', '')
                    if u_txt and n_txt:
                        horses.append({'umaban': u_txt, 'name': n_txt})

            # 抽出パターン2 (汎用フォールバック)
            if not horses:
                name_elems = soup.select('.HorseName a, .Horse_Name a, span.HorseName')
                for idx, name_elem in enumerate(name_elems, start=1):
                    n_txt = name_elem.text.strip()
                    if n_txt:
                        horses.append({'umaban': str(idx), 'name': n_txt})

            if horses:
                return horses

        except Exception as e:
            print(f"取得試行エラー ({url}): {e}")
            continue

    return []

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_text = event.message.text

    # レースIDの抽出（12桁の数字）
    match = re.search(r'race_id=(\d{12})', user_text) or re.search(r'\b(\d{12})\b', user_text)

    if match:
        race_id = match.group(1)
        horses = get_netkeiba_data(user_text, race_id)

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
