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

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# 掛載 static 資料夾作為靜態資源路徑
app.mount("/static", StaticFiles(directory="static"), name="static")

# === 初始化 log 資料夾與檔案（如不存在則建立） ===
Path("log").mkdir(parents=True, exist_ok=True)
Path("static").mkdir(parents=True, exist_ok=True)  # 確保 static 資料夾存在
log_json_path = "log/log.json"
if not Path(log_json_path).exists():
    with open(log_json_path, "w") as f:
        json.dump([], f)

# === Google Sheets 初始化 ===
SHEET_URL = os.getenv("GOOGLE_SHEET_URL")
sheet = None
try:
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_file("bybit-webhook-a203-logs-7a34c85019dd.json", scopes=scope)
    gs_client = gspread.authorize(creds)
    sheet = gs_client.open_by_url(SHEET_URL).worksheet("bybit_webhook logs")
except Exception as e:
    print(f"[⚠️ Google Sheets 初始化失敗]：{e}")

# === 寫入 log 至 Google Sheets ===
def write_to_gsheet(timestamp, strategy_id, event, equity=None, drawdown=None, order_action=None):
    try:
        if sheet:
            row = [timestamp, strategy_id, event, equity or '', drawdown or '', order_action or '']
            sheet.append_row(row)
            print("[✅ 已寫入 Google Sheets]")
    except Exception as e:
        print(f"[⚠️ Google Sheets 寫入失敗]：{e}")

# === LINE 通知函式 ===
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

# === Webhook Payload 定義 ===
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

# === Webhook 主邏輯 ===
@app.post("/webhook")
async def webhook_handler(payload: WebhookPayload):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sid = payload.strategy_id
    event = payload.signal_type
    equity = payload.equity
    drawdown = None
    action = payload.data.action if payload.data else ""

    # === 寫入 log.json ===
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

    # === 寫入 Google Sheets ===
    write_to_gsheet(timestamp, sid, event, equity, drawdown, action)

    # === LINE 通知（選用） ===
    await push_line_message(f"✅ 策略 {sid} 收到訊號：{event}，動作：{action}")

    return {"status": "ok", "strategy_id": sid}

# === 測試 LINE 是否成功通知 ===
@app.get("/test_line")
async def test_line():
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    strategy_id = "TEST_LINE"
    event = "test_line_triggered"
    write_to_gsheet(timestamp, strategy_id, event)
    await push_line_message("📢 測試訊息：LINE 通知測試成功！")
    return {"status": "ok"}

# === logs dashboard HTML 頁面 ===
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

        plt.figure(figsize=(4, 3))
        plt.hist(mdd_list, bins=10)
        plt.title("MDD 分佈圖")
        plt.tight_layout()
        plt.savefig("static/mdd_distribution.png")

        plt.figure(figsize=(4, 3))
        plt.plot(equity_list)
        plt.title("Equity 曲線")
        plt.tight_layout()
        plt.savefig("static/equity_curve.png")

        plt.figure(figsize=(3, 3))
        plt.bar(["Win Rate"], [win_rate])
        plt.title(f"Win Rate: {win_rate:.1f}%")
        plt.ylim(0, 100)
        plt.tight_layout()
        plt.savefig("static/win_rate.png")
    except Exception as e:
        print("[⚠️ 圖表產生失敗]", e)

    return templates.TemplateResponse("logs_dashboard.html", {"request": request, "records": records, "seen_ids": []})

# === log.json 下載 ===
@app.get("/download/log.json")
def download_log():
    return FileResponse("log/log.json", media_type="application/json", filename="log.json")

# === Reset 選定策略（POST） ===
@app.post("/reset_strategy")
async def reset_strategy(strategy_id: str = Form(...), reset_secret: str = Form(...)):
    expected_secret = os.getenv("RESET_SECRET", "letmein")
    if reset_secret != expected_secret:
        return HTMLResponse(content="<h1>密碼錯誤，請重新輸入。</h1>", status_code=403)

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    write_to_gsheet(timestamp, strategy_id, "manual_reset")
    await push_line_message(f"🔁 手動重置策略：{strategy_id}")
    return RedirectResponse(url="/logs_dashboard", status_code=302)
