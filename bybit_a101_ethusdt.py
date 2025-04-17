from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
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
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")  # ✅ 使用動態變數，避免硬編碼

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

# === Web Dashboard Route ===
@app.get("/logs_dashboard", response_class=HTMLResponse)
async def show_logs_dashboard(request: Request):
    with open(log_path_json, "r") as f:
        raw_data = json.load(f)

    def clean_strategy_id(sid):
        return re.sub(r"_\d+$", "", sid)

    simplified_data = [
        {
            **r,
            "strategy_id": clean_strategy_id(r.get("strategy_id", ""))
        }
        for r in raw_data
    ]

    return templates.TemplateResponse("logs_dashboard.html", {
        "request": request,
        "records": simplified_data
    })
