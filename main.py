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
    user_text = event.message.text.strip()
    
    # URLの自動変換（スマホ版 -> PC版、出馬表・結果を両方サポート）
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
        
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_text)
    )

def generate_ai_prediction(url):
    if ai_model is None:
        return "エラー：AIモデル（keiba_ai_model.pkl）が読み込めませんでした。"
        
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        res = requests.get(url, headers=headers)
        
        # 【最重要修正】自動推測を廃止し、netkeibaの文字コード（EUC-JP）に完全固定
        res.encoding = 'euc-jp'
        soup = BeautifulSoup(res.text, 'html.parser')
        
        race_title_elem = soup.select_one('.RaceName, .Race_Name, .race_name, h1')
        race_name = race_title_elem.text.strip() if race_title_elem else "対象レース"
        
        # 1. どのテーブルにデータがあるか探し、各項目の「列番号」を確実に見つける
        target_table = None
        col_name = col_weight = col_odds = col_jockey = -1
        
        for tbl in soup.find_all('table'):
            first_row = tbl.find('tr')
            if not first_row: 
                continue
                
            header_texts = [h.text.strip() for h in first_row.find_all(['th', 'td'])]
            
            if any('馬名' in txt for txt in header_texts):
                target_table = tbl
                for i, txt in enumerate(header_texts):
                    if '馬名' in txt: col_name = i
                    elif '騎手' in txt: col_jockey = i
                    elif '斤量' in txt: col_weight = i
                    elif '単勝' in txt or 'オッズ' in txt: col_odds = i
                break
                
        if not target_table:
            return "出馬表または結果のデータが見つかりませんでした。"
            
        horses_data = []
        for row in target_table.find_all('tr')[1:]:
            cells = row.find_all(['td', 'th'])
            # 必要な列数がない行はスキップ
            if len(cells) <= max(col_name, col_weight, col_odds):
                continue
                
            # --- 馬名 ---
            horse_name = cells[col_name].text.strip()
            horse_name = re.sub(r'\s+', '', horse_name)
            horse_name = re.sub(r'取消|除外', '', horse_name)
            if not horse_name: 
                continue
                
            # --- 騎手 ---
            jockey = cells[col_jockey].text.strip() if col_jockey != -1 else "不明"
            jockey = re.sub(r'\s+', '', jockey)
                
            # --- 斤量 ---
            weight = 55.0
            if col_weight != -1:
                m_w = re.search(r'(\d+\.\d+|\d+)', cells[col_weight].text.strip())
                if m_w: weight = float(m_w.group(1))
                
            # --- オッズ ---
            odds = 50.0
            if col_odds != -1:
                odds_txt = cells[col_odds].text.strip()
                m_o = re.search(r'(\d+\.\d+|\d+)', odds_txt)
                if m_o: 
                    parsed_odds = float(m_o.group(1))
                    # 馬体重などと間違えないよう制限をかける
                    if 1.0 <= parsed_odds <= 500.0:
                        odds = parsed_odds
                        
            horses_data.append({
                '馬名': horse_name,
                '騎手': jockey,
                '単勝オッズ': odds,
                '斤量': weight,
                'タイム_秒': 100.0 
            })
            
        if not horses_data:
            return "データが正しく取得できませんでした。URLを確認してください。"
            
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
