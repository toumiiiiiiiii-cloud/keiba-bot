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
        
        # 文字化け対策（正常に動作している自動判定を維持）
        res.encoding = res.apparent_encoding
        soup = BeautifulSoup(res.text, 'html.parser')
        
        race_title_elem = soup.select_one('.RaceName, .Race_Name, h1')
        race_name = race_title_elem.text.strip() if race_title_elem else "対象レース"
        
        horses_data = []
        rows = soup.find_all('tr')
        
        for row in rows:
            horse_name = ""
            jockey = "不明"
            weight = 55.0
            odds = 50.0

            # 1. 馬名の取得（URLに "/horse/" を含むリンクだけを「本物の馬」として認識）
            name_a = row.select_one('a[href*="/horse/"]')
            if name_a:
                horse_name = name_a.text.strip()
            
            # 見つからなかったらスキップ（これで「お気に入り馬」等のメニューを完全に弾きます）
            if not horse_name:
                continue
            
            horse_name = re.sub(r'\s+', '', horse_name)
            horse_name = re.sub(r'取消|除外', '', horse_name)

            # 2. 騎手の取得（URLに "/jockey/" か "/recent/" を含むリンク）
            jockey_a = row.select_one('a[href*="/jockey/"], a[href*="/recent/"]')
            if jockey_a:
                jockey = jockey_a.text.strip()
            jockey = re.sub(r'\s+', '', jockey)

            # 3. 斤量とオッズの取得
            tds = row.find_all(['td', 'th'])
            td_texts = [td.text.strip() for td in tds]
            
            for td in tds:
                txt = td.text.strip()
                if not txt: continue
                
                # 数字（小数含む）のみのテキストを探す
                if re.match(r'^\d+(\.\d+)?$', txt):
                    val = float(txt)
                    
                    # 斤量の判定（48.0〜65.0の範囲）
                    if 48.0 <= val <= 65.0 and weight == 55.0:
                        weight = val
                        
                    # オッズの判定（Txt_R クラスが付いている数値）
                    if 'Txt_R' in td.get('class', []) or 'txt_r' in td.get('class', []):
                        if 1.0 <= val <= 999.9:
                            odds = val
            
            # クラス名が無くオッズが取れなかった場合、後ろの列からオッズらしい数字を探す
            if odds == 50.0:
                for txt in reversed(td_texts):
                    if re.match(r'^\d+(\.\d+)?$', txt):
                        val = float(txt)
                        if val != weight and 1.0 <= val <= 999.9:
                            odds = val
                            break
            
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
