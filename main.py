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
    
    # 対策1：スマホ版の「出馬表」「レース結果」URLを、AIが読めるPC版へ完全自動変換
    if "sp.netkeiba.com" in user_text:
        m = re.search(r'race_id=(\d+)', user_text)
        if m:
            race_id = m.group(1)
            # レース結果ページの場合
            if "pid=race_result" in user_text or "race_result" in user_text:
                user_text = f"https://race.netkeiba.com/race/result.html?race_id={race_id}"
            else:
                user_text = f"https://race.netkeiba.com/race/shutuba.html?race_id={race_id}"
        else:
            user_text = user_text.replace("race.sp.netkeiba.com", "race.netkeiba.com")
            
    if "netkeiba.com" in user_text:
        reply_text = generate_ai_prediction(user_text)
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
        # 対策2：アクセスブロック回避
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
        res = requests.get(url, headers=headers)
        
        # 対策3：文字化けの完全防止
        res.encoding = res.apparent_encoding
        soup = BeautifulSoup(res.text, 'html.parser')
        
        race_title_elem = soup.select_one('.RaceName, .Race_Name, .race_name, .RaceList_Item02 .DataTitle')
        race_name = race_title_elem.text.strip() if race_title_elem else "対象レース"
        
        # 対策4：「出馬表」と「結果ページ」両方のテーブルに対応
        rows = soup.select('tr.HorseList, table.RaceTable01 tr, table.ResultTable tr')
        if not rows:
            rows = soup.find_all('tr')
            
        horses_data = []
        for row in rows:
            horse_name_elem = row.select_one('.HorseName a, .Horse_Name a, .Horse_Info a')
            if not horse_name_elem:
                continue
            horse_name = horse_name_elem.text.strip()
            
            jockey_elem = row.select_one('.Jockey a')
            jockey = jockey_elem.text.strip() if jockey_elem else "不明"
            
            tds = row.find_all('td')
            td_texts = [td.text.strip() for td in tds]
            
            weight = 55.0
            odds = 50.0
            
            # 斤量の自動特定（48.0〜65.0kgの数値を自動で探す）
            for txt in td_texts:
                try:
                    val = float(txt)
                    if 48.0 <= val <= 65.0 and weight == 55.0:
                        weight = val
                except ValueError:
                    pass
            
            # オッズの自動特定（右側から探し、オッズ特有のクラスを検知）
            odds_found = False
            for td in reversed(tds):
                txt = td.text.strip()
                if not txt or txt == '---':
                    continue
                if 'Txt_R' in td.get('class', []):
                    try:
                        odds = float(txt)
                        odds_found = True
                        break
                    except ValueError:
                        pass
            
            # クラスで見つからなかった場合のフォールバック
            if not odds_found:
                if len(td_texts) >= 13: # 結果ページ用
                    try: odds = float(td_texts[12])
                    except ValueError: pass
                elif len(td_texts) >= 10: # 出馬表ページ用
                    try: odds = float(td_texts[9])
                    except ValueError: pass
            
            horses_data.append({
                '馬名': horse_name,
                '騎手': jockey,
                '単勝オッズ': odds,
                '斤量': weight,
                'タイム_秒': 100.0 
            })
            
        if not horses_data:
            return "データが見つかりませんでした。正しい出馬表かレース結果のURLか確認してください。"
            
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
