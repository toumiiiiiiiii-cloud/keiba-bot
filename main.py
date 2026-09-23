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
        if "result" in url:
            url = f"https://race.netkeiba.com/race/result.html?race_id={race_id}"
        else:
            url = f"https://race.netkeiba.com/race/shutuba.html?race_id={race_id}"
    elif "db.sp.netkeiba.com" in url:
        url = url.replace("db.sp.netkeiba.com", "db.netkeiba.com")
            
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
        
        # レース名の取得
        race_title_elem = soup.select_one('.RaceName, .Race_Name, .race_name, .RaceList_Item02 .DataTitle, h1')
        race_name = race_title_elem.text.strip() if race_title_elem else "対象レース"
        
        # 1. 馬データが含まれるテーブルを探す
        target_table = None
        for tbl in soup.find_all('table'):
            if '馬名' in tbl.text:
                target_table = tbl
                break
                
        if not target_table:
            return "出馬表または結果のデータが見つかりませんでした。"
            
        # 2. 列番号（インデックス）を自動特定する（最強の対策）
        header_row = None
        for row in target_table.find_all('tr'):
            if '馬名' in row.text:
                header_row = row
                break
                
        if not header_row:
            return "テーブル内にヘッダーが見つかりませんでした。"
            
        col_map = {}
        for i, cell in enumerate(header_row.find_all(['th', 'td'])):
            txt = cell.text.strip()
            if '馬名' in txt: col_map['name'] = i
            elif '騎手' in txt: col_map['jockey'] = i
            elif '斤量' in txt: col_map['weight'] = i
            elif 'オッズ' in txt or '単勝' in txt: col_map['odds'] = i
            
        horses_data = []
        
        # 3. データ行から抽出
        for row in target_table.find_all('tr'):
            tds = row.find_all('td')
            # 必要な列数がない行（見出しなど）はスキップ
            if not tds or len(tds) <= max(col_map.values(), default=0):
                continue
                
            # 馬名の抽出
            name_cell = tds[col_map.get('name', 0)]
            a_tag_name = name_cell.find('a')
            horse_name = a_tag_name.text.strip() if a_tag_name else name_cell.text.strip()
            horse_name = re.sub(r'取消|除外', '', horse_name).strip()
            
            if not horse_name:
                continue

            # 騎手の抽出
            jockey = "不明"
            if 'jockey' in col_map:
                jockey_cell = tds[col_map['jockey']]
                a_tag_jockey = jockey_cell.find('a')
                jockey = a_tag_jockey.text.strip() if a_tag_jockey else jockey_cell.text.strip()
            
            weight_str = tds[col_map.get('weight', 0)].text.strip() if 'weight' in col_map else ""
            odds_str = tds[col_map.get('odds', 0)].text.strip() if 'odds' in col_map else ""
            
            weight = 55.0
            try:
                m_w = re.search(r'(\d+\.\d+|\d+)', weight_str)
                if m_w: weight = float(m_w.group(1))
            except Exception:
                pass
                
            odds = 50.0
            try:
                m_o = re.search(r'(\d+\.\d+|\d+)', odds_str)
                if m_o: odds = float(m_o.group(1))
            except Exception:
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
