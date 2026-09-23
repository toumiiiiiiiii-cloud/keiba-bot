import os
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi, ReplyMessageRequest,
    FlexMessage, FlexContainer
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

app = Flask(__name__)

# サーバーの設定から鍵を読み込みます
CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '')
CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET', '')

configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_msg = event.message.text.strip()
    
    # LINEに送信される黒背景のAI予想カード（デザインデータ）
    flex_json = {
      "type": "bubble",
      "styles": {
        "header": {"backgroundColor": "#1E1E1E"},
        "body": {"backgroundColor": "#2A2A2A"}
      },
      "header": {
        "type": "box",
        "layout": "vertical",
        "contents": [
          {
            "type": "text",
            "text": "【全自動AI予想】 " + user_msg,
            "weight": "bold",
            "color": "#00FF66",
            "size": "sm"
          }
        ]
      },
      "body": {
        "type": "box",
        "layout": "vertical",
        "spacing": "md",
        "contents": [
          {
            "type": "box",
            "layout": "horizontal",
            "contents": [
              {"type": "text", "text": "◎ 本命", "color": "#FF5555", "weight": "bold", "flex": 2},
              {"type": "text", "text": "5番", "color": "#FFFFFF", "flex": 1},
              {"type": "text", "text": "ソールオリエンス", "color": "#FFFFFF", "weight": "bold", "flex": 5}
            ]
          },
          {
            "type": "box",
            "layout": "horizontal",
            "contents": [
              {"type": "text", "text": "○ 対抗", "color": "#FFAA00", "weight": "bold", "flex": 2},
              {"type": "text", "text": "3番", "color": "#FFFFFF", "flex": 1},
              {"type": "text", "text": "ジャスティンパレス", "color": "#FFFFFF", "flex": 5}
            ]
          },
          {
            "type": "box",
            "layout": "horizontal",
            "contents": [
              {"type": "text", "text": "▲ 単穴", "color": "#FFFF00", "weight": "bold", "flex": 2},
              {"type": "text", "text": "4番", "color": "#FFFFFF", "flex": 1},
              {"type": "text", "text": "スターズオンアース", "color": "#FFFFFF", "flex": 5}
            ]
          }
        ]
      }
    }

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[FlexMessage(alt_text="AI競馬予想結果", contents=FlexContainer.from_dict(flex_json))]
            )
        )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
