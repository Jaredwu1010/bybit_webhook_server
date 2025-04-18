from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
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
app.mount("/static", StaticFiles(directory="log"), name="static")

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

# === Webhook 接收主邏輯 ===
# ...略（保留原 webhook_handler）

# === logs_dashboard 頁面 + 圖表統計 + 下載功能 ===
@app.get("/logs_dashboard", response_class=HTMLResponse)
async def logs_dashboard(request: Request):
    if not os.path.exists(log_path_json):
        return HTMLResponse(content="<h1>尚未產生任何 log.json 記錄</h1>", status_code=404)
    with open(log_path_json, "r") as f:
        records = json.load(f)

    dd_values = [round(row["drawdown"], 2) for row in records if row.get("drawdown")]
    order_counts = Counter(row["strategy_id"] for row in records if row["event"] == "order_sent")
    equity_curve = [(row["timestamp"], row["equity"]) for row in records if row.get("equity")]

    win_map = {}
    for r in records:
        sid = r["strategy_id"]
        if sid not in win_map:
            win_map[sid] = {"order": 0, "tp": 0}
        if r["event"] == "order_sent":
            win_map[sid]["order"] += 1
        if r["event"] == "take_profit":
            win_map[sid]["tp"] += 1

    win_rates = {k: round((v["tp"] / v["order"])*100, 2) if v["order"] > 0 else 0 for k, v in win_map.items()}
    plt.clf()
    plt.bar(win_rates.keys(), win_rates.values())
    plt.xticks(rotation=45)
    plt.title("Win Rate (%)")
    plt.tight_layout()
    plt.savefig("log/win_rate.png")

    return templates.TemplateResponse("logs_dashboard.html", {
        "request": request,
        "records": records
    })
