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
    """レースIDから中央/地方を自動判定し、PC版の正確なデータを取得する"""
    
    # IDの5〜6桁目で場所を判定（01〜10が中央競馬の競馬場）
    try:
        venue_code = int(race_id[4:6])
        if venue_code <= 10:
            url = f"https://race.netkeiba.com/race/shutuba.html?race_id={race_id}"
        else:
            url = f"https://nar.netkeiba.com/race/shutuba.html?race_id={race_id}"
    except ValueError:
        return []

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }

    try:
        res = requests.get(url, headers=headers, timeout=10)
        # netkeiba固有の文字化け対策
        html = res.content.decode('euc-jp', errors='replace')
        soup = BeautifulSoup(html, 'html.parser')
        
        horses = []
        
        # PC版の出馬表（HorseList）から正確に抽出
        rows = soup.find_all('tr', class_=re.compile(r'HorseList'))
        
        for row in rows:
            # 馬番
            umaban_td = row.select_one('td.Umaban')
            # 馬名
            name_td = row.select_one('span.HorseName a') or row.select_one('.HorseName')
            # 騎手（データが本物か確認するため追加）
            jockey_td = row.select_one('td.Jockey a') or row.select_one('.Jockey')
            
            if umaban_td and name_td:
                # 余計な文字を省き、純粋な数字と文字列だけにする
                umaban = re.sub(r'\D', '', umaban_td.text.strip())
                name = name_td.text.strip()
                jockey = jockey_td.text.strip() if jockey_td else "不明"
                
                if umaban and name:
                    horses.append({
                        'umaban': int(umaban), # 数値として扱う
                        'name': name,
                        'jockey': jockey
                    })
        
        # 念のため馬番順に並び替え
        horses = sorted(horses, key=lambda x: x['umaban'])
        return horses
        
    except Exception as e:
        print(f"Scraping Error: {e}")
        return []

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_text = event.message.text

    # URLやテキストから12桁の数字（race_id）を確実に抜き出す
    match = re.search(r'race_id=(\d{12})', user_text) or re.search(r'\b(\d{12})\b', user_text)

    if match:
        race_id = match.group(1)
        horses = get_real_netkeiba_data(race_id)

        if horses:
            # 現在は「データ収集の正確性」をテストするため、実際のリストの一部をそのまま返します。
            # ※ここに後日、本格的なAI予想ロジックが組み込まれます。
            
            total_count = len(horses)
            
            reply_text = (
                f"✅ データの正確な取得に成功しました\n"
                f"📍 レースID: {race_id}\n"
                f"出走頭数: {total_count}頭\n"
                f"--------------------\n"
                f"【取得データ サンプル】\n"
                f"1番: {horses[0]['name']} (騎手: {horses[0]['jockey']})\n"
                f"2番: {horses[1]['name']} (騎手: {horses[1]['jockey']})\n"
                f"...\n"
                f"大外: {horses[-1]['name']} (騎手: {horses[-1]['jockey']})\n"
                f"--------------------\n"
                f"※収集フェーズ完了。次はこのデータを使って過去分析を実装します。"
            )
        else:
            reply_text = f"レースID: {race_id} の出馬表が見つかりません。過去のレース結果URLや、無効なURLの可能性があります。"
    else:
        reply_text = "netkeibaの出馬表URL（race_idが含まれるもの）を送信してください。"

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_text)
    )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
