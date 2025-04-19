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
        print("[📩 LINE Callback 收到資料]", json.dumps(payload, indent=2))
    except Exception as e:
        print("[⚠️ LINE Callback 處理失敗]", e)
    return {"status": "received"}
    
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
    keys = [
        "USE_LINE_NOTIFY",
        "LINE_USER_ID",
        "LINE_CHANNEL_TOKEN",
        "RESET_SECRET",
        "GOOGLE_SHEET_URL",
        "BYBIT_API_KEY",
        "BYBIT_API_SECRET",
    ]
    rows = []
    for key in keys:
        val = os.getenv(key)
        if key == "RESET_SECRET" and val:
            val_display = "••••••••"  # 隱藏敏感密碼
        elif val:
            val_display = val
        else:
            val_display = ""

        status = "✅ 設定完成" if val else "❌ 缺失"
        rows.append(f"<tr><td>{key}</td><td>{status}</td><td><code>{val_display}</code></td></tr>")

    html = f"""
    <html>
    <head>
        <title>Settings Dashboard</title>
        <style>
            body {{ font-family: Arial; padding: 20px; }}
            table {{ border-collapse: collapse; width: 100%; }}
            th, td {{ border: 1px solid #ccc; padding: 8px; }}
            th {{ background-color: #f4f4f4; }}
        </style>
    </head>
    <body>
        <h2>📋 Webhook Server 設定狀態</h2>
        <table>
            <tr><th>環境變數</th><th>狀態</th><th>內容</th></tr>
            {''.join(rows)}
        </table>
    </body>
    </html>
    """
    return HTMLResponse(content=html)
