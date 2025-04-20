from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import httpx
import os
import json
import gspread
import matplotlib.pyplot as plt
from google.oauth2.service_account import Credentials
from datetime import datetime
from pathlib import Path
import collections
import time
import hmac
import hashlib

app = FastAPI()

Path("static").mkdir(parents=True, exist_ok=True)  # 📁 確保 static 資料夾存在
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

Path("log").mkdir(parents=True, exist_ok=True)
log_json_path = "log/log.json"
if not Path(log_json_path).exists():
    with open(log_json_path, "w") as f:
        json.dump([], f)

# ✅ 預先產生靜態圖，避免 logs_dashboard 載入時圖片 404
for fname in ["mdd_distribution.png", "equity_curve.png", "win_rate.png"]:
    fpath = Path(f"static/{fname}")
    if not fpath.exists():
        plt.figure(figsize=(3, 2))
        plt.text(0.5, 0.5, "No Data", fontsize=12, ha="center")
        plt.axis("off")
        plt.tight_layout()
        plt.savefig(fpath)
        plt.close()

SHEET_URL = os.getenv("GOOGLE_SHEET_URL")
sheet = None
try:
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_file("bybit-webhook-a203-logs-7a34c85019dd.json", scopes=scope)
    gs_client = gspread.authorize(creds)
    sheet = gs_client.open_by_url(SHEET_URL).worksheet("bybit_webhook logs")
except Exception as e:
    print(f"[⚠️ Google Sheets 初始化失敗]：{e}")

def write_to_gsheet(timestamp, strategy_id, event, equity=None, drawdown=None, order_action=None):
    try:
        if sheet:
            row = [timestamp, strategy_id, event, equity or '', drawdown or '', order_action or '']
            sheet.append_row(row)
            print("[✅ 已寫入 Google Sheets]")
    except Exception as e:
        print(f"[⚠️ Google Sheets 寫入失敗]：{e}")

async def push_line_message(msg: str):
    use_line = os.getenv("USE_LINE_NOTIFY", "false").lower() == "true"
    if not use_line:
        print("[⚠️] USE_LINE_NOTIFY 為 false，已略過 LINE 推送")
        return

    LINE_USER_ID = os.getenv("LINE_USER_ID")
    LINE_CHANNEL_TOKEN = os.getenv("LINE_CHANNEL_TOKEN")
    if not LINE_USER_ID or not LINE_CHANNEL_TOKEN:
        print("[⚠️] 未設定 LINE_USER_ID 或 LINE_CHANNEL_TOKEN")
        return
    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_TOKEN}",
        "Content-Type": "application/json"
    }
    body = {
        "to": LINE_USER_ID,
        "messages": [{"type": "text", "text": msg}]
    }
    async with httpx.AsyncClient() as client:
        r = await client.post("https://api.line.me/v2/bot/message/push", headers=headers, json=body)
        print("[LINE 回應]", r.status_code, await r.aread())

# ✅ Bybit 下單模組
async def place_order(symbol: str, side: str, qty: float):
    api_key = os.getenv("BYBIT_API_KEY")
    api_secret = os.getenv("BYBIT_API_SECRET")
    base_url = os.getenv("BYBIT_API_URL", "https://api-testnet.bybit.com")
    endpoint = f"{base_url}/v5/order/create"

    timestamp = str(int(time.time() * 1000))
    recv_window = "5000"
    payload = {
        "category": "linear",
        "symbol": symbol,
        "side": side,
        "orderType": "Market",
        "qty": str(qty),
        "timeInForce": "IOC"
    }
    payload_str = json.dumps(payload, separators=(",", ":"))
    sign_str = timestamp + api_key + recv_window + payload_str
    signature = hmac.new(api_secret.encode(), sign_str.encode(), hashlib.sha256).hexdigest()
    headers = {
        "X-BAPI-API-KEY": api_key,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-RECV-WINDOW": recv_window,
        "X-BAPI-SIGN": signature,
        "Content-Type": "application/json"
    }
    async with httpx.AsyncClient() as client:
        response = await client.post(endpoint, headers=headers, data=payload_str)
        print("[📤 Bybit 下單結果]", response.status_code, await response.aread())
        return response.json()

class WebhookPayloadData(BaseModel):
    action: str
    position_size: float

class WebhookPayload(BaseModel):
    strategy_id: str
    signal_type: str
    equity: float = None
    symbol: str = None
    order_type: str = None
    data: WebhookPayloadData = None
    secret: str = None

# ✅ 新增 LINE Callback 接收模組（放在 /webhook 前面）
@app.post("/line_callback")
async def line_callback(request: Request):
    try:
        payload = await request.json()
        events = payload.get("events", [])

        for event in events:
            event_type = event.get("type", "")
            source = event.get("source", {})
            user_type = source.get("type", "")
            user_id = source.get("userId", "")
            group_id = source.get("groupId", "")
            message = event.get("message", {})
            msg_type = message.get("type", "")

            print(f"[📩 LINE] 類型: {event_type}, 訊息類型: {msg_type}, 來源: {user_type}, userId: {user_id}{' | groupId: ' + group_id if group_id else ''}")
    except Exception as e:
        print("[⚠️ LINE Callback 處理失敗]", e)
    return {"status": "received"}

# ✅ 新增 TradingView Webhook+Secret 專用入口
@app.post("/tv_webhook")
async def tv_webhook(request: Request):
    try:
        payload = await request.json()

        # ✅ Webhook secret 驗證（來自 .env 或 Render secret）
        expected_secret = os.getenv("WEBHOOK_SECRET", "letmein")
        received_secret = payload.get("secret", "")
        if received_secret != expected_secret:
            print("❌ Webhook secret 驗證失敗")
            return {"status": "unauthorized", "message": "invalid secret"}

        # ✅ 正常流程處理
        strategy_id = payload.get("strategy_id")
        order_id = payload.get("order_id")
        action = infer_action_from_order_id(order_id)
        symbol = payload.get("symbol")
        price = float(payload.get("price"))
        capital_percent = float(payload.get("capital_percent"))
        trigger_type = payload.get("trigger_type")
        time = payload.get("time")

        print(f"✅ [TV Webhook] {strategy_id} | {order_id} | {symbol}@{price} | {capital_percent}%")

        await place_order(symbol, action.upper(), capital_percent)

        with open(log_json_path, "r+") as f:
            logs = json.load(f)
            logs.append({
                "timestamp": time,
                "strategy_id": strategy_id,
                "event": order_id,
                "equity": None,
                "drawdown": None,
                "order_action": action
            })
            f.seek(0)
            json.dump(logs, f, indent=2)

        write_to_gsheet(time, strategy_id, order_id, None, None, action)

        return {"status": "ok", "message": "tv webhook received"}
    
    except Exception as e:
        print(f"[⚠️ TV Webhook 錯誤]：{e}")
        return {"status": "error", "message": str(e)}

@app.post("/tv_webhook_test")
async def tv_webhook_test(request: Request):
    try:
        payload = await request.json()

        # ✅ Secret 驗證
        expected_secret = os.getenv("WEBHOOK_SECRET", "letmein")
        received_secret = payload.get("secret", "")
        if received_secret != expected_secret:
            print("❌ Webhook secret 驗證失敗")
            return {"status": "unauthorized", "message": "invalid secret"}

        # ✅ 正常 webhook 資訊提取
        strategy_id = payload.get("strategy_id")
        order_id = payload.get("order_id")
        action = "Buy" if "long" in order_id else "Sell"
        symbol = payload.get("symbol")
        price = float(payload.get("price"))
        trigger_type = payload.get("trigger_type")
        time = payload.get("time")

        print(f"🧪 [TV 測試 Webhook] {strategy_id} | {order_id} | {symbol}@{price} | qty=0.01")

        # ✅ 固定 0.01 下單
        await place_order(symbol, action, 0.01)

        # ✅ log.json 記錄
        with open(log_json_path, "r+") as f:
            logs = json.load(f)
            logs.append({
                "timestamp": time,
                "strategy_id": strategy_id,
                "event": order_id + "_test",
                "equity": None,
                "drawdown": None,
                "order_action": action
            })
            f.seek(0)
            json.dump(logs, f, indent=2)

        # ✅ Google Sheets
        write_to_gsheet(time, strategy_id, order_id + "_test", None, None, action)

        return {"status": "ok", "message": "tv_webhook_test received"}
    
    except Exception as e:
        print(f"[⚠️ TV 測試 webhook 錯誤]：{e}")
        return {"status": "error", "message": str(e)}

# 🧠 判斷動作方向
def infer_action_from_order_id(order_id: str):
    if "long" in order_id:
        return "Buy"
    elif "short" in order_id:
        return "Sell"
    return "unknown"

@app.post("/webhook")
async def webhook_handler(payload: WebhookPayload):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sid = payload.strategy_id
    event = payload.signal_type
    equity = payload.equity
    drawdown = None
    action = payload.data.action if payload.data else ""

    try:
        with open(log_json_path, "r+") as f:
            logs = json.load(f)
            logs.append({
                "timestamp": timestamp,
                "strategy_id": sid,
                "event": event,
                "equity": equity,
                "drawdown": drawdown,
                "order_action": action
            })
            f.seek(0)
            json.dump(logs, f, indent=2)
        print(f"[📥 已寫入 log.json] {sid} {event}")
    except Exception as e:
        print(f"[⚠️ log.json 寫入失敗]：{e}")

    write_to_gsheet(timestamp, sid, event, equity, drawdown, action)

    # ✅ 自動下單
    if event in ["entry_long", "entry_short"] and payload.data:
        if action and payload.symbol and payload.data.position_size > 0:
            side = "Buy" if action == "buy" else "Sell"
            try:
                result = await place_order(payload.symbol, side, payload.data.position_size)
                print("[✅ 已執行下單]", result)
            except Exception as e:
                print("[⚠️ 下單失敗]", e)

    await push_line_message(f"✅ 策略 {sid} 收到訊號：{event}，動作：{action}")
    return {"status": "ok", "strategy_id": sid}

# ✅ 健康檢查路由，支援 GET 與 HEAD 請求（避免 405 錯誤）
# 📌 給 UptimeRobot 使用，保持 Render Server 醒著
# 📌 不寫入 log、不發 LINE 通知、不與 TV webhook 混用

@app.api_route("/healthcheck", methods=["GET", "HEAD"])
async def healthcheck():
    return {"status": "server is running"}

@app.get("/test_line")
async def test_line():
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    strategy_id = "TEST_LINE"
    event = "test_line_triggered"
    write_to_gsheet(timestamp, strategy_id, event)
    await push_line_message("📢 測試訊息：LINE 通知測試成功！")
    return {"status": "ok"}

@app.get("/line_status")
async def line_status():
    use_line = os.getenv("USE_LINE_NOTIFY", "false").lower() == "true"
    return {"line_notify_enabled": use_line}

# ✅ 新增 /status 查詢策略狀態 API
@app.get("/status")
async def check_strategy_status(strategy_id: str):
    try:
        with open("log/log.json", "r") as f:
            records = json.load(f)
        matched = [r for r in records if r.get("strategy_id") == strategy_id]
        if matched:
            return {"status": "found", "count": len(matched)}
        else:
            return {"status": "not found"}
    except Exception as e:
        return {"status": "error", "error": str(e)}

@app.get("/logs_dashboard", response_class=HTMLResponse)
async def show_logs_dashboard(request: Request):
    try:
        with open("log/log.json", "r") as f:
            records = json.load(f)
    except Exception as e:
        print(f"[⚠️ log.json 載入失敗]：{e}")
        records = []

    try:
        strategy_counts = collections.Counter(r["strategy_id"].split("_")[0] + "_" + r["strategy_id"].split("_")[1] for r in records)
        win_count = sum(1 for r in records if r["event"] == "order_sent")
        total_orders = sum(1 for r in records if r["event"] in ["entry_long", "entry_short"])
        win_rate = (win_count / total_orders * 100) if total_orders else 0

        mdd_list = [r["drawdown"] for r in records if r["drawdown"] is not None]
        equity_list = [r["equity"] for r in records if r["equity"] is not None]

        if mdd_list:
            plt.figure(figsize=(4, 3))
            plt.hist(mdd_list, bins=10)
            plt.title("MDD 分佈圖")
            plt.tight_layout()
            plt.savefig("static/mdd_distribution.png")
        else:
            print("[⚠️ MDD 無資料]")

        if equity_list:
            plt.figure(figsize=(4, 3))
            plt.plot(equity_list)
            plt.title("Equity 曲線")
            plt.tight_layout()
            plt.savefig("static/equity_curve.png")
        else:
            print("[⚠️ Equity 無資料]")

        plt.figure(figsize=(3, 3))
        plt.bar(["Win Rate"], [win_rate])
        plt.title(f"Win Rate: {win_rate:.1f}%")
        plt.ylim(0, 100)
        plt.tight_layout()
        plt.savefig("static/win_rate.png")
    except Exception as e:
        print("[⚠️ 圖表產生失敗]", e)

    return templates.TemplateResponse("logs_dashboard.html", {"request": request, "records": records, "seen_ids": []})

@app.get("/download/log.json")
def download_log():
    return FileResponse("log/log.json", media_type="application/json", filename="log.json")

@app.post("/reset_strategy")
async def reset_strategy(request: Request):
    expected_secret = os.getenv("RESET_SECRET", "letmein")

    try:
        if request.headers.get("content-type", "").startswith("application/json"):
            data = await request.json()
            strategy_id = data.get("strategy_id")
            reset_secret = data.get("reset_secret")
        else:
            form = await request.form()
            strategy_id = form.get("strategy_id")
            reset_secret = form.get("reset_secret")

        if reset_secret != expected_secret:
            return HTMLResponse(content="<h1>密碼錯誤，請重新輸入。</h1>", status_code=403)

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        write_to_gsheet(timestamp, strategy_id, "manual_reset")
        await push_line_message(f"🔁 手動重置策略：{strategy_id}")
        return RedirectResponse(url="/logs_dashboard", status_code=302)
    except Exception as e:
        print("[⚠️ Reset Strategy 處理失敗]", e)
        return HTMLResponse(content="<h1>內部錯誤</h1>", status_code=500)

# ✅ 新增根目錄首頁，避免 Render 預設 GET / 回傳 404
@app.get("/")
async def root():
    return {"message": "Webhook Server is live"}

@app.get("/settings_dashboard", response_class=HTMLResponse)
async def settings_dashboard():
    use_line = os.getenv("USE_LINE_NOTIFY", "false").lower() == "true"
    use_line_status = "✅ 已啟用" if use_line else "❌ 未啟用"

    html = f"""
    <html>
    <head>
        <title>Settings Dashboard</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body {{
                font-family: Arial, sans-serif;
                padding: 20px;
                background: #f5f7fa;
                color: #333;
            }}
            h2 {{
                margin-top: 2em;
                color: #222;
            }}
            .status-box {{
                margin-top: 10px;
                font-weight: bold;
            }}
            .box {{
                background: #fff;
                padding: 16px;
                border-radius: 8px;
                box-shadow: 0 0 8px rgba(0,0,0,0.05);
                margin-bottom: 30px;
            }}
            .button-group {{
                display: flex;
                flex-wrap: wrap;
                gap: 12px;
                margin-bottom: 20px;
            }}
            button {{
                background-color: #007bff;
                color: white;
                border: none;
                padding: 10px 18px;
                border-radius: 6px;
                cursor: pointer;
                font-size: 15px;
            }}
            button:hover {{
                background-color: #0056b3;
            }}
            @media (max-width: 600px) {{
                .button-group {{
                    flex-direction: column;
                }}
                button {{
                    width: 100%;
                }}
            }}
        </style>
    </head>
    <body>
        <div class="box">
            <h2>🔔 LINE 通知狀態</h2>
            <p>USE_LINE_NOTIFY：<strong>{use_line_status}</strong></p>
        </div>

        <div class="box">
            <h2>🧪 測試功能</h2>
            <div class="button-group">
                <button onclick="testLine()">📲 測試 LINE 通知</button>
                <button onclick="testWebhook()">📩 模擬 webhook 下單</button>
                <button onclick="testReset()">🔁 測試重置策略</button>
            </div>
            <div id="result" class="status-box">📡 等待測試中…</div>
        </div>

        <script>
            async function testLine() {{
                document.getElementById('result').innerText = "⏳ 傳送 LINE 測試中...";
                const res = await fetch("/test_line");
                const data = await res.json();
                document.getElementById('result').innerText = "✅ LINE 測試完成：" + JSON.stringify(data);
            }}

            async function testWebhook() {{
                document.getElementById('result').innerText = "📩 發送 webhook 測試中...";
                const res = await fetch("/webhook", {{
                    method: "POST",
                    headers: {{
                        "Content-Type": "application/json"
                    }},
                    body: JSON.stringify({{
                        strategy_id: "TEST_WEBHOOK",
                        signal_type: "entry_long",
                        equity: 9999,
                        symbol: "ETHUSDT",
                        order_type: "market",
                        data: {{
                            action: "buy",
                            position_size: 0.01
                        }},
                        secret: ""
                    }})
                }});
                const data = await res.json();
                document.getElementById('result').innerText = "✅ webhook 測試完成：" + JSON.stringify(data);
            }}

            async function testReset() {{
                document.getElementById('result').innerText = "🔁 傳送重置指令中...";
                const res = await fetch("/trigger_reset", {{ method: "POST" }});
                const data = await res.json();
                if (data.status === "success") {{
                    document.getElementById('result').innerText = "✅ 策略重置成功！";
                }} else {{
                    document.getElementById('result').innerText = "❌ 重置失敗：" + JSON.stringify(data);
                }}
            }}
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html)

@app.post("/trigger_reset")
async def trigger_reset():
    strategy_id = "TEST_STRATEGY"
    reset_secret = os.getenv("RESET_SECRET", "letmein")

    form = {
        "strategy_id": strategy_id,
        "reset_secret": reset_secret,
    }

    try:
        async with httpx.AsyncClient() as client:
            res = await client.post("http://localhost:10000/reset_strategy", data=form)
            if res.status_code == 302 or "logs_dashboard" in res.text:
                return {"status": "success"}
            else:
                return {"status": "failed", "detail": res.text}
    except Exception as e:
        return {"status": "error", "error": str(e)}
