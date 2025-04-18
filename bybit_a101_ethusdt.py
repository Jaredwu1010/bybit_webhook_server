from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
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
import re
from collections import defaultdict

app = FastAPI()
templates = Jinja2Templates(directory="templates")

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

MAX_DRAWDOWN_PERCENT = float(os.getenv("MAX_DRAWDOWN", 10))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
LINE_CHANNEL_TOKEN = os.getenv("LINE_CHANNEL_TOKEN")
LINE_USER_ID = os.getenv("LINE_USER_ID")

max_equity = {}
strategy_status = {}
log_path_csv = "log/log.csv"
log_path_json = "log/log.json"

os.makedirs("log", exist_ok=True)
if not os.path.exists(log_path_csv):
    with open(log_path_csv, mode="w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "strategy_id", "event", "equity", "drawdown", "order_action"])
if not os.path.exists(log_path_json):
    with open(log_path_json, mode="w") as f:
        json.dump([], f)

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

async def push_line_message(message: str):
    print(f"[DEBUG] LINE_CHANNEL_TOKEN: {LINE_CHANNEL_TOKEN[:8]}...", flush=True)
    print(f"[DEBUG] LINE_USER_ID: {LINE_USER_ID}", flush=True)
    if not LINE_CHANNEL_TOKEN or not LINE_USER_ID:
        print("[❌ 缺少 LINE TOKEN 或 USER ID]", flush=True)
        return
    try:
        headers = {
            "Authorization": f"Bearer {LINE_CHANNEL_TOKEN}",
            "Content-Type": "application/json"
        }
        body = {
            "to": LINE_USER_ID,
            "messages": [{"type": "text", "text": message}]
        }
        async with httpx.AsyncClient() as client:
            resp = await client.post("https://api.line.me/v2/bot/message/push", headers=headers, json=body)
            print(f"[✅ LINE 發送回應] {resp.status_code} - {resp.text}", flush=True)
    except Exception as e:
        print(f"[⚠️ LINE 推播失敗]：{e}", flush=True)

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
            await push_line_message(f"⚠️ 策略 {sid} 已觸發最大回撤停單，DD = {round(dd, 2)}%")
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

@app.get("/status")
async def get_status(strategy_id: str):
    max_eq = max_equity.get(strategy_id, 0)
    paused = strategy_status.get(strategy_id, {}).get("paused", False)
    return {
        "strategy_id": strategy_id,
        "max_equity": max_eq,
        "paused": paused
    }

@app.post("/line_callback")
async def line_callback(request: Request):
    return {"status": "ok"}

@app.get("/test_line")
async def test_line():
    await push_line_message("📢 測試訊息：LINE 通知測試成功！")
    return {"status": "sent"}
