import os
import requests
from bs4 import BeautifulSoup
import pandas as pd
import joblib
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

# 環境変数からLINEのキーを取得
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# AIモデルの読み込み
model_path = 'keiba_ai_model.pkl'
if os.path.exists(model_path):
    ai_model = joblib.load(model_path)
else:
    ai_model = None

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
    user_text = event.message.text
    
    # 対策1：スマホ用URL(sp.)をPC用に自動変換
    if "race.sp.netkeiba.com" in user_text:
        user_text = user_text.replace("race.sp.netkeiba.com", "race.netkeiba.com")
    
    if "netkeiba.com" in user_text:
        reply_text = generate_ai_prediction(user_text)
    else:
        reply_text = "netkeibaの出馬表URLを送信してください！\n例: https://race.netkeiba.com/race/shutuba.html?race_id=..."
        
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_text)
    )

def generate_ai_prediction(url):
    if ai_model is None:
        return "エラー：AIモデル（keiba_ai_model.pkl）が読み込めませんでした。"
        
    try:
        # 対策2：アクセス拒否（Bot弾き）を防ぐUser-Agent偽装
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
        
        res = requests.get(url, headers=headers)
        
        # 対策3：完全な文字化け対策（生データからEUC-JPとして強制デコード）
        html = res.content.decode('euc-jp', 'replace')
        soup = BeautifulSoup(html, 'html.parser')
        
        # レース名の取得
        race_title_elem = soup.select_one('.RaceName')
        race_name = race_title_elem.text.strip() if race_title_elem else "対象レース"
        
        horses_data = []
        rows = soup.select('.HorseList')
        
        for row in rows:
            horse_name_elem = row.select_one('.HorseName a')
            if not horse_name_elem:
                continue
            horse_name = horse_name_elem.text.strip()
            
            jockey_elem = row.select_one('.Jockey a')
            jockey = jockey_elem.text.strip() if jockey_elem else "不明"
            
            weight = 55.0 
            jockey_td = row.select_one('.Jockey')
            if jockey_td:
                text_parts = jockey_td.get_text(separator='|').split('|')
                for part in text_parts:
                    try:
                        weight = float(part.strip())
                        break
                    except ValueError:
                        continue
            
            odds = 50.0 
            odds_td = row.select_one('.Txt_R') or row.select_one('.Popular')
            if odds_td:
                try:
                    odds_str = odds_td.text.strip()
                    if odds_str and odds_str != '---':
                        odds = float(odds_str)
                except ValueError:
                    pass
            
            horses_data.append({
                '馬名': horse_name,
                '騎手': jockey,
                '単勝オッズ': odds,
                '斤量': weight,
                'タイム_秒': 100.0 
            })
            
        if not horses_data:
            return "出馬表データが見つかりませんでした。正しいURLか確認してください。"
            
        df = pd.DataFrame(horses_data)
        X = df[['単勝オッズ', '斤量', 'タイム_秒']]
        
        probabilities = ai_model.predict_proba(X)[:, 1]
        df['AI勝率'] = probabilities * 100
        df_sorted = df.sort_values('AI勝率', ascending=False).head(5)
        
        reply = f"🟩 AI適性スコア予想（本番モデル） 🟩\n{race_name}\n\n"
        ranks = ['【ランクS】1位', '【ランクA】2位', '【ランクB】3位', '【ランクB】4位', '【ランクC】5位']
        
        for i, (_, row) in enumerate(df_sorted.iterrows()):
            reply += f"{ranks[i]}\n"
            reply += f"🐎 {row['馬名']}\n"
            reply += f"👤 {row['騎手']}\n"
            reply += f"📊 ｵｯｽﾞ {row['単勝オッズ']}倍 / 斤量 {row['斤量']}kg\n"
            reply += f"📈 AI予測勝率 {row['AI勝率']:.1f}%\n\n"
            
        reply += "※実際のオッズと斤量データを元にAIが算出しています。\n（走破タイムは仮数値を代入して計算）"
        return reply
        
    except Exception as e:
        return f"予想中にエラーが発生しました。\n詳細: {str(e)}"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
