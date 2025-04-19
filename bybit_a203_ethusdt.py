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

SHEET_URL = os.getenv("GOOGLE_SHEET_URL")
sheet = None
try:
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_file("bybit-webhook-a203-logs-7a34c85019dd.json", scopes=scope)
    gs_client = gspread.authorize(creds)
    sheet = gs_client.open_by_url(SHEET_URL).worksheet("bybit_webhook logs")
except Exception as e:
    print(f"[⚠️ Google Sheets 初始化失敗]：{e}")

def write_to_gsheet(timestamp, strategy_id, event, equity=None, drawdown=None, order_action=None, trigger_type=None, comment=None, order_id=None):
    try:
        if sheet:
            row = [timestamp, strategy_id, event, equity or '', drawdown or '', order_action or '', trigger_type or '', comment or '', order_id or '']
            sheet.append_row(row)
            print("[✅ 已寫入 Google Sheets]")
    except Exception as e:
        print(f"[⚠️ Google Sheets 寫入失敗]：{e}")

async def push_line_message(msg: str):
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

class WebhookPayload(BaseModel):
    strategy_id: str
    signal_type: str
    equity: float = None
    symbol: str = None
    order_type: str = None
    price: float = None
    action: str = None
    capital_percent: float = None
    trigger_type: str = None
    comment: str = None
    order_id: str = None
    secret: str = None

@app.post("/webhook")
async def webhook_handler(payload: WebhookPayload):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sid = payload.strategy_id
    event = payload.signal_type
    equity = payload.equity
    drawdown = None
    action = payload.action or ""
    symbol = payload.symbol or "ETHUSDT"
    qty = 0.0

    if payload.price and payload.capital_percent:
        qty = round((equity * payload.capital_percent / 100) / payload.price, 3)

    try:
        with open(log_json_path, "r+") as f:
            logs = json.load(f)

            # ✅ 防重複單邏輯（根據 order_id）
            if any(log.get("order_id") == payload.order_id for log in logs if payload.order_id):
                print(f"[⚠️ 重複訊號] 已處理過的 order_id：{payload.order_id}")
                return {"status": "duplicate", "order_id": payload.order_id}

            logs.append({
                "timestamp": timestamp,
                "strategy_id": sid,
                "event": event,
                "equity": equity,
                "drawdown": drawdown,
                "order_action": action,
                "trigger_type": payload.trigger_type,
                "comment": payload.comment,
                "order_id": payload.order_id
            })
            f.seek(0)
            json.dump(logs, f, indent=2)
        print(f"[📥 已寫入 log.json] {sid} {event}")
    except Exception as e:
        print(f"[⚠️ log.json 寫入失敗]：{e}")

    write_to_gsheet(timestamp, sid, event, equity, drawdown, action, payload.trigger_type, payload.comment, payload.order_id)

    if event in ["entry_long", "entry_short"] and qty > 0:
        side = "Buy" if action == "buy" else "Sell"
        try:
            result = await place_order(symbol, side, qty)
            print("[✅ 已執行下單]", result)
        except Exception as e:
            print("[⚠️ 下單失敗]", e)

    await push_line_message(f"✅ 策略 {sid} 收到訊號：{event}，動作：{action}\n{payload.comment or ''}")
    return {"status": "ok", "strategy_id": sid}
