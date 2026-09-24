import os
import re
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
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

model_path = 'keiba_ai_model.pkl'
ai_model = joblib.load(model_path) if os.path.exists(model_path) else None

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try: handler.handle(body, signature)
    except InvalidSignatureError: abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_text = event.message.text.strip()
    
    url = user_text
    m = re.search(r'race_id=(\d+)', url)
    if m:
        race_id = m.group(1)
        if "result" in url or "pid=race_result" in url:
            url = f"https://race.netkeiba.com/race/result.html?race_id={race_id}"
        else:
            url = f"https://race.netkeiba.com/race/shutuba.html?race_id={race_id}"
    elif "sp.netkeiba.com" in url:
        url = url.replace("sp.netkeiba.com", "netkeiba.com")
            
    if "netkeiba.com" in url:
        reply_text = generate_ai_prediction(url)
    else:
        reply_text = "netkeibaの出馬表、またはレース結果のURLを送信してください！"
        
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))

def generate_ai_prediction(url):
    if ai_model is None:
        return "エラー：AIモデルが読み込めませんでした。"
        
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        res = requests.get(url, headers=headers)
        
        # 【文字化け対策】余計な指定をせず、BeautifulSoupの自動解読に任せる（これで文字化けは直ります）
        soup = BeautifulSoup(res.content, 'html.parser')
        
        race_title_elem = soup.select_one('.RaceName, .Race_Name, h1')
        race_name = race_title_elem.text.strip() if race_title_elem else "対象レース"
        
        horses_data = []
        rows = soup.find_all('tr')
        
        for row in rows:
            # 馬名の取得（リンクURLから確実に探す）
            horse_name = None
            for a in row.find_all('a'):
                if 'horse' in a.get('href', ''):
                    horse_name = a.text.strip()
                    break
            
            if not horse_name:
                continue
            horse_name = re.sub(r'取消|除外|\s+', '', horse_name)
            if not horse_name:
                continue
                
            # 騎手の取得
            jockey = "不明"
            for a in row.find_all('a'):
                if 'jockey' in a.get('href', '') or 'recent' in a.get('href', ''):
                    jockey = a.text.strip()
                    break
            jockey = re.sub(r'\s+', '', jockey)
            
            weight = 55.0
            odds = 50.0
            
            for td in row.find_all(['td', 'th']):
                txt = td.text.strip()
                classes = td.get('class', [])
                
                # 馬体重のカッコや、タイムのコロンは除外
                if not txt or 'Weight' in classes or ':' in txt or '(' in txt or '---' in txt:
                    continue
                    
                try:
                    val = float(txt)
                    # 斤量は48〜65の数値（オッズ特有のTxt_Rクラスには入っていない）
                    if 48.0 <= val <= 65.0 and weight == 55.0 and 'Txt_R' not in classes:
                        weight = val
                    # オッズは必ず「Txt_R」というクラスに入っている
                    if 'Txt_R' in classes and 1.0 <= val <= 999.9:
                        odds = val
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
            return "出馬表または結果のデータが見つかりませんでした。"
            
        df = pd.DataFrame(horses_data)
        X = df[['単勝オッズ', '斤量', 'タイム_秒']]
        
        probabilities = ai_model.predict_proba(X)[:, 1]
        df['AI勝率'] = probabilities * 100
        df_sorted = df.sort_values('AI勝率', ascending=False).head(5)
        
        reply = f"🟩 AI適性スコア予想 🟩\n{race_name}\n\n"
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
