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
        res.encoding = res.apparent_encoding
        soup = BeautifulSoup(res.text, 'html.parser')
        
        race_title_elem = soup.select_one('.RaceName, .Race_Name, .race_name, .RaceList_Item02 .DataTitle, h1')
        race_name = race_title_elem.text.strip() if race_title_elem else "対象レース"
        
        rows = soup.select('tr.HorseList, table.RaceTable01 tr, table.ResultTable tr')
        if not rows:
            rows = soup.find_all('tr')
            
        horses_data = []
        for row in rows:
            horse_name_elem = row.select_one('.HorseName a, .Horse_Name a, .Horse_Info a')
            if not horse_name_elem:
                continue
            horse_name = horse_name_elem.text.strip()
            horse_name = re.sub(r'取消|除外', '', horse_name).strip()
            
            jockey_elem = row.select_one('.Jockey a')
            jockey = jockey_elem.text.strip() if jockey_elem else "不明"
            
            tds = row.find_all('td')
            if not tds:
                continue
                
            weight = 55.0
            odds = 50.0
            
            # --- 斤量の取得 ---
            for td in tds:
                txt = td.text.strip()
                try:
                    val = float(txt)
                    if 48.0 <= val <= 65.0 and weight == 55.0:
                        weight = val
                except ValueError:
                    pass
            
            # --- オッズの取得（絶対外さないピンポイント指定） ---
            # tdの数で「結果ページ(15列以上)」か「出馬表ページ(14列以下)」かを判定します
            if len(tds) >= 15:
                # 結果ページは左から13番目（プログラム上は12）が単勝オッズです
                try:
                    odds = float(tds[12].text.strip())
                except ValueError:
                    pass
            else:
                # 出馬表ページは 'Txt_R' クラスが付いている列がオッズです
                for td in tds:
                    if 'Txt_R' in td.get('class', []):
                        txt = td.text.strip()
                        # 馬体重（括弧付き）を除外します
                        if '(' not in txt:
                            try:
                                odds = float(txt)
                                break
                            except ValueError:
                                pass
                                
            # もし上記で取れなかった場合の最終バックアップ
            if odds == 50.0:
                for td in reversed(tds):
                    txt = td.text.strip()
                    if '.' in txt and '(' not in txt and ':' not in txt:
                        try:
                            val = float(txt)
                            if val != weight and 1.0 <= val <= 500.0:
                                odds = val
                                break
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
            return "データが見つかりませんでした。正しいURLか確認してください。"
            
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
