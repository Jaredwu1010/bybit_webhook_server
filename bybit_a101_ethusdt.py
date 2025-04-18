from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import httpx
import os
import time
import hmac
import hashlib
import json
import csv
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials
from collections import Counter
import matplotlib.pyplot as plt
import matplotlib
matplotlib.use('Agg')

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# === Webhook 資料結構定義 ===
class WebhookPayloadData(BaseModel):
    action: str = None
    position_size: float = 0

class WebhookPayload(BaseModel):
    strategy_id: str
    signal_type: str
    equity: float = None
    symbol: str = None
    order_type: str = None
    data: WebhookPayloadData = None
    secret: str = None

# === MDD 停單邏輯 ===
MAX_DRAWDOWN_PERCENT = float(os.getenv("MAX_DRAWDOWN", 10))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "abc123xyz")
LINE_NOTIFY_TOKEN = os.getenv("LINE_NOTIFY_TOKEN")
LINE_USER_ID = os.getenv("LINE_USER_ID")

max_equity = {}
strategy_status = {}
log_path_csv = "log/log.csv"
log_path_json = "log/log.json"

# === 初始化 log 資料夾與檔案 ===
os.makedirs("log", exist_ok=True)
if not os.path.exists(log_path_csv):
    with open(log_path_csv, mode="w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "strategy_id", "event", "equity", "drawdown", "order_action"])
if not os.path.exists(log_path_json):
    with open(log_path_json, mode="w") as f:
        json.dump([], f)

# === Google Sheets Logging 初始化 ===
SHEET_URL = os.getenv("GOOGLE_SHEET_URL")
creds = None
gs_client = None
sheet = None

try:
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_file("bybit-webhook-a203-logs-7a34c85019dd.json", scopes=scope)
    gs_client = gspread.authorize(creds)
    sheet = gs_client.open_by_url(SHEET_URL).worksheet("bybit_webhook logs")
except Exception as e:
    print(f"[⚠️ Google Sheets 初始化失敗]：{e}")

# === 寫入 log 函數 ===
def log_event(strategy_id, event, equity=None, drawdown=None, order_action=None):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    row = [timestamp, strategy_id, event, equity, drawdown, order_action]

    with open(log_path_csv, mode="a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(row)

    with open(log_path_json, mode="r+") as f:
        data = json.load(f)
        data.append({
            "timestamp": timestamp,
            "strategy_id": strategy_id,
            "event": event,
            "equity": equity,
            "drawdown": drawdown,
            "order_action": order_action
        })
        f.seek(0)
        json.dump(data, f, indent=2)

    try:
        if sheet:
            sheet.append_row(row)
    except Exception as e:
        print(f"[⚠️ Google Sheets 寫入失敗]：{e}")

# === LINE 通知功能 ===
async def push_line_message(message: str):
    headers = {
        "Authorization": f"Bearer {LINE_NOTIFY_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "to": LINE_USER_ID,
        "messages": [{
            "type": "text",
            "text": message
        }]
    }
    url = "https://api.line.me/v2/bot/message/push"
    async with httpx.AsyncClient() as client:
        res = await client.post(url, headers=headers, json=payload)
        print(f"[LINE 推播結果]：{res.status_code}, {res.text}", flush=True)

# === Bybit 下單函數 ===
async def place_order(symbol: str, side: str, qty: float):
    api_key = os.environ['BYBIT_API_KEY']
    api_secret = os.environ['BYBIT_API_SECRET']
    base_url = os.environ['BYBIT_API_URL']
    endpoint = f"{base_url}/v5/order/create"

    timestamp = str(int(time.time() * 1000))
    recv_window = "50000"
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
        return response.json()

# === Webhook 接收主邏輯 ===
@app.post("/webhook")
async def webhook_handler(payload: WebhookPayload):
    sid = payload.strategy_id

    if payload.secret != WEBHOOK_SECRET:
        log_event(sid, "invalid_secret")
        return {"status": "blocked", "reason": "invalid secret", "strategy_id": sid}

    if payload.signal_type == "equity_update":
        eq = float(payload.equity)
        max_eq = max_equity.get(sid, 0)
        if eq > max_eq:
            max_equity[sid] = eq
            strategy_status[sid] = {"paused": False}
            log_event(sid, "equity_update", equity=eq, drawdown=0.0)
            return {"status": "ok", "strategy_id": sid, "drawdown": 0.0}

        dd = (1 - eq / max_eq) * 100 if max_eq > 0 else 0
        if dd >= MAX_DRAWDOWN_PERCENT:
            strategy_status[sid] = {"paused": True}
            log_event(sid, "paused_by_mdd", equity=eq, drawdown=round(dd, 2))
            await push_line_message(f"⚠️ 策略 {sid} 已觸發 MDD 停損：{dd:.2f}%")
            return {"status": "paused", "reason": "MDD exceeded", "strategy_id": sid, "drawdown": round(dd, 2)}

        log_event(sid, "equity_update", equity=eq, drawdown=round(dd, 2))
        return {"status": "ok", "strategy_id": sid, "drawdown": round(dd, 2)}

    if payload.signal_type == "reset":
        strategy_status[sid] = {"paused": False}
        log_event(sid, "reset")
        return {"status": "reset", "strategy_id": sid}

    if strategy_status.get(sid, {}).get("paused", False):
        log_event(sid, "blocked_send")
        return {"status": "blocked", "reason": "MDD stop active", "strategy_id": sid}

    if payload.signal_type in ["entry_long", "entry_short"] and payload.data:
        action = payload.data.action
        size = float(payload.data.position_size or 0)
        symbol = payload.symbol
        order_type = payload.order_type

        if size == 0:
            log_event(sid, "skip_zero_order")
            return {"status": "ok", "message": "倉量為 0 不處理"}

        side = "Buy" if action == "buy" else "Sell"
        result = await place_order(symbol, side, size)
        log_event(sid, "order_sent", order_action=action)
        return {"status": "success", "bybit_response": result}

    log_event(sid, "unrecognized")
    return {"status": "ignored", "message": "無法處理的 webhook"}

# === 查詢目前策略狀態 ===
@app.get("/status")
async def get_status(strategy_id: str):
    max_eq = max_equity.get(strategy_id, 0)
    paused = strategy_status.get(strategy_id, {}).get("paused", False)
    return {
        "strategy_id": strategy_id,
        "max_equity": max_eq,
        "paused": paused
    }

# === 手動 reset 表單處理 ===
@app.post("/manual_reset")
async def manual_reset(request: Request, strategy_id: str = Form(...), secret: str = Form(...)):
    if secret != WEBHOOK_SECRET:
        return HTMLResponse("<h1>密碼錯誤</h1>", status_code=403)
    strategy_status[strategy_id] = {"paused": False}
    log_event(strategy_id, "manual_reset")
    return HTMLResponse(f"<h1>策略 {strategy_id} 已手動解除停損</h1><a href='/logs_dashboard'>返回面板</a>")

# === logs_dashboard 頁面 + 圖表統計 + 下載功能 ===
@app.get("/logs_dashboard", response_class=HTMLResponse)
async def logs_dashboard(request: Request):
    if not os.path.exists(log_path_json):
        return HTMLResponse(content="<h1>尚未產生任何 log.json 記錄</h1>", status_code=404)
    with open(log_path_json, "r") as f:
        records = json.load(f)

    # 圖表統計
    dd_values = [round(row["drawdown"], 2) for row in records if row.get("drawdown")]
    order_counts = Counter(row["strategy_id"] for row in records if row["event"] == "order_sent")
    equity_curve = [(row["timestamp"], row["equity"]) for row in records if row.get("equity")]

    # 畫圖：MDD 分佈
    plt.clf()
    plt.hist(dd_values, bins=20, edgecolor='black')
    plt.title("MDD 分佈")
    plt.xlabel("Drawdown (%)")
    plt.ylabel("策略數量")
    plt.savefig("log/mdd_hist.png")

    # 畫圖：下單次數統計
    plt.clf()
    plt.bar(order_counts.keys(), order_counts.values())
    plt.title("下單次數")
    plt.xticks(rotation=45)
    plt.savefig("log/order_count.png")

    # 畫圖：績效曲線
    plt.clf()
    x = [r[0] for r in equity_curve]
    y = [r[1] for r in equity_curve]
    plt.plot(x, y, marker="o")
    plt.title("總績效曲線")
    plt.xticks(rotation=45)
    plt.tight_layout()
    plt.savefig("log/equity_curve.png")

    return templates.TemplateResponse("logs_dashboard.html", {
        "request": request,
        "records": records
    })

# === JSON 檔案下載 ===
@app.get("/download/log.json")
async def download_json():
    return FileResponse(log_path_json, filename="log.json")

# === 測試 LINE 通知訊息 ===
@app.get("/test_line")
async def test_line():
    await push_line_message("📢 測試訊息：LINE 通知測試成功！")
    return {"status": "ok", "message": "LINE 測試已送出"}
