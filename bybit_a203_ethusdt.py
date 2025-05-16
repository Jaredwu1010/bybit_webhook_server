from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import httpx
import os
import json
from pydantic import BaseModel 
import asyncio
import gspread
import matplotlib.pyplot as plt
from google.oauth2.service_account import Credentials
from datetime import datetime, timedelta, timezone
from pathlib import Path
import collections
import time
import hmac
import hashlib
import math

# ——— 小工具：空字串/None 時回傳預設值 0.0 ———
def safe_float(val: str | float | int | None, default: float = 0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default
        
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

def write_to_gsheet(
         pine_time, server_time,
         strategy_id, event,
         equity=None, drawdown=None,
         order_action=None, trigger_type=None,
         comment=None, contracts=None,
         ret_code=None, ret_msg=None,
         pnl=None, price=None, qty=None):
    try:
        if sheet:
            expected_headers = [
                "pine_time", "server_time",
                "strategy_id", "event", "equity", "drawdown",
                "order_action", "trigger_type", "comment", "contracts",
                "ret_code", "ret_msg", "pnl", "price", "qty"
            ]
            headers = sheet.row_values(1)
            if headers != expected_headers:
                sheet.update("A1:O1", [expected_headers])
            row = [
                pine_time, server_time,
                strategy_id, event,
                equity, drawdown,
                order_action, trigger_type,
                comment, contracts,
                ret_code, ret_msg,
                pnl, price, qty
            ]
            print(f"[📝 準備寫入資料] {row}")
            sheet.append_row(row)
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
async def place_order(symbol: str, side: str, qty: float, reduce_only: bool = False):
    api_key = os.getenv("BYBIT_API_KEY")
    api_secret = os.getenv("BYBIT_API_SECRET")
    base_url = os.getenv("BYBIT_API_URL", "https://api-testnet.bybit.com")
    endpoint = f"{base_url}/v5/order/create"

    timestamp = str(int(time.time() * 1000))
    recv_window = "5000"
    # 市价单不需要 timeInForce，也不要带 price
    payload = {
        "category": "linear",
        "symbol": symbol,
        "side": side,
        "orderType": "Market",
        "qty": str(qty)
    }
    # 如果是減倉單，帶上 reduce_only 參數
    if reduce_only:
        payload["reduce_only"] = True
        
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
    time: str
    trigger_type: str = None 
    equity: float = None
    symbol: str = None
    order_type: str = None
    data: WebhookPayloadData = None
    secret: str = None

# 🧠 根據 order_id 精準推斷動作方向與用途
def infer_action_from_order_id(order_id: str) -> str:
    if order_id.startswith("entry_long"):
        return "多單建倉"
    elif order_id.startswith("entry_short"):
        return "空單建倉"
    elif order_id.startswith("tp1_long"):
        return "多單止盈"
    elif order_id.startswith("tp1_short"):
        return "空單止盈"
    elif order_id.startswith("trail_long"):
        return "多單移動止損"
    elif order_id.startswith("trail_short"):
        return "空單移動止損"
    elif order_id.startswith("stop_loss_long"):
        return "多單止損"
    elif order_id.startswith("stop_loss_short"):
        return "空單止損"
    elif order_id.startswith("breakeven_long"):
        return "多單套保"
    elif order_id.startswith("breakeven_short"):
        return "空單套保"
    elif order_id.startswith("residual_close_long"):
        return "多單清殘倉"
    elif order_id.startswith("residual_close_short"):
        return "空單清殘倉"
    elif order_id.startswith("close_long_for_short"):
        return "多單反手轉空"
    elif order_id.startswith("close_short_for_long"):
        return "空單反手轉多"
    return "unknown"

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
@app.get("/equity_status")
async def equity_status():
    try:
        api_key = os.getenv("BYBIT_API_KEY")
        api_secret = os.getenv("BYBIT_API_SECRET")
        base_url = os.getenv("BYBIT_API_URL", "https://api-testnet.bybit.com")
        endpoint = f"{base_url}/v5/account/wallet-balance?accountType=UNIFIED"

        timestamp = str(int(time.time() * 1000))
        recv_window = "5000"
        sign_str = timestamp + api_key + recv_window
        signature = hmac.new(api_secret.encode(), sign_str.encode(), hashlib.sha256).hexdigest()
        headers = {
            "X-BAPI-API-KEY": api_key,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": recv_window,
            "X-BAPI-SIGN": signature
        }

        async with httpx.AsyncClient() as client:
            response = await client.get(endpoint, headers=headers)
            data = response.json()
            raw_equity   = data["result"]["list"][0].get("totalEquity")
            usdt_balance = safe_float(raw_equity)      # ← 自動處理空字串 / None
            return {"status": "ok", "equity": usdt_balance}
    except Exception as e:
        fallback = safe_float(os.getenv("EQUITY_FALLBACK", "100"))
        return {"status": "fallback", "equity": fallback, "error": str(e)}

@app.post("/tv_webhook")
async def tv_webhook(request: Request):
    try:
        payload = await request.json()
        if payload.get("secret", "") != os.getenv("WEBHOOK_SECRET", "letmein"):
            return {"status": "unauthorized"}

        strategy_id  = payload.get("strategy_id", "")
        order_id     = payload.get("order_id", "")
        trigger_type = payload.get("trigger_type", "")
        comment      = payload.get("comment", "")
        # 不再使用 TV payload 的 contracts
        # contracts    = payload.get("contracts", None)
        symbol       = payload.get("symbol", "")
        if symbol.endswith(".P"): symbol = symbol[:-2]
        price           = safe_float(payload.get("price"), 0.0)
        capital_percent = safe_float(payload.get("capital_percent"), 0.0)
        event        = order_id
        order_action = infer_action_from_order_id(order_id)

        pine_time   = payload.get("time", "")
        tz_tw       = timezone(timedelta(hours=8))
        server_time = datetime.now(tz=tz_tw).strftime("%Y-%m-%d %H:%M:%S")

        # 取餘額
        api_key    = os.getenv("BYBIT_API_KEY")
        api_secret = os.getenv("BYBIT_API_SECRET")
        base_url   = os.getenv("BYBIT_API_URL", "https://api-testnet.bybit.com")
        endpoint   = f"{base_url}/v5/account/wallet-balance?accountType=UNIFIED"

        timestamp    = str(int(time.time() * 1000))
        recv_window  = "5000"
        query_string = "accountType=UNIFIED"
        sign_str     = timestamp + api_key + recv_window + query_string
        signature    = hmac.new(api_secret.encode(), sign_str.encode(), hashlib.sha256).hexdigest()
        headers      = {
            "X-BAPI-API-KEY": api_key,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": recv_window,
            "X-BAPI-SIGN": signature
        }
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(endpoint, headers=headers)
                data = response.json()
                print("[📦 Bybit API 回傳]", data)

            usdt_info = next((c for c in data["result"]["list"][0]["coin"] if c["coin"] == "USDT"), None)
            if usdt_info:
                equity_str = (
                    usdt_info.get("totalAvailableBalance")
                    or usdt_info.get("availableToWithdraw")
                    or usdt_info.get("equity")
                )
                equity = safe_float(equity_str, default=safe_float(os.getenv("EQUITY_FALLBACK", "100")))
            else:
                equity = safe_float(os.getenv("EQUITY_FALLBACK", "100"))
        except Exception as e:
            print("[⚠️ 無法取得 Bybit 賬戶餘額]", e)
            equity = safe_float(os.getenv("EQUITY_FALLBACK", "100"))

        # —— 先把TV傳的contracts讀回來（供 exit 下單參考）——
        contracts = safe_float(payload.get("contracts"), 0.0)
        # 拆解 order_id → action, direction, is_long（忽略 "rev" 後綴）
        parts     = order_id.split("_")
        action    = parts[0]
        # parts 可能像 ["entry","long","rev"] 或 ["stop","loss","long"]
        # 先看 parts[1]，若不是 long/short 再看 parts[2]
        if len(parts) > 1 and parts[1] in ("long", "short"):
            direction = parts[1]
        elif len(parts) > 2 and parts[2] in ("long", "short"):
            direction = parts[2]
        else:
            direction = ""
        is_long = (direction == "long")

        # 根據 action 分流：entry 開倉，exit 類型減倉，其它不動
        # 进到 tv_webhook 的 action 分流处，替换 entry 分支为：
        if action == "entry" and price > 0 and capital_percent > 0:
            # 修正最小下單單位為 0.01 ETH
            avail    = equity
            raw_qty  = (avail * capital_percent / 100) / price
            min_unit = 0.01
            qty      = math.floor(raw_qty / min_unit) * min_unit
            if qty < min_unit:
                order_result = {"retCode": None, "retMsg": "qty too small", "result": {}}
            else:
                side         = "Buy" if is_long else "Sell"
                order_result = await place_order(symbol, side, qty)
        
        elif action in ("tp1", "stop", "trail", "breakeven", "residual"):
            # 1) 查目前持倉
            position_endpoint = f"{base_url}/v5/position/list?category=linear&symbol={symbol}"
            ts           = str(int(time.time()*1000))
            recv_window  = "5000"
            qstr         = f"category=linear&symbol={symbol}"
            sig          = hmac.new(api_secret.encode(), (ts+api_key+recv_window+qstr).encode(), hashlib.sha256).hexdigest()
            pos_headers  = {"X-BAPI-API-KEY": api_key, "X-BAPI-TIMESTAMP": ts,
                            "X-BAPI-RECV-WINDOW": recv_window, "X-BAPI-SIGN": sig}
            async with httpx.AsyncClient() as client:
                pos_data = (await client.get(position_endpoint, headers=pos_headers)).json()
        
            # 2) 取得正確方向倉位大小
            pos_side   = "Buy" if is_long else "Sell"
            pos_size   = 0.0
            for p in pos_data.get("result", {}).get("list", []):
                if p.get("side") == pos_side:
                    pos_size = safe_float(p.get("size"), 0.0)
                    break

            min_unit = 0.01
            if pos_size < min_unit:                # 沒倉位：直接跳過、不寫 Sheet / Log
                return {"status": "skip_no_position"}

            # 3) 決定本次要平多少
            close_qty = pos_size
            if action == "tp1":                         # 只平 50%
                close_qty = max(min_unit,
                                round(pos_size * 0.5, 2))

            # 4) 下平倉 Market 減倉單
            side        = "Sell" if is_long else "Buy"
            exit_result = await place_order(symbol, side, close_qty, reduce_only=True)

            order_result = {
                "retCode": exit_result.get("retCode"),
                "retMsg":  exit_result.get("retMsg"),
                "result":  exit_result.get("result", {})
            }
            executed_qty = safe_float(
                exit_result["result"].get("cumExecQty")
                or exit_result["result"].get("execQty")
                or exit_result["result"].get("qty"), 0.0)
            contracts = executed_qty
            qty       = executed_qty

        else:
            # 不符合下單條件
            qty = 0.0
            reason = "invalid price/cap_percent" if action == "entry" else "not entry"
            order_result = {"retCode": None, "retMsg": reason, "result": {}}

        
        # —— 共用：解析下單回傳 —— 
        ret_code = order_result.get("retCode")
        ret_msg  = order_result.get("retMsg")
        pnl      = order_result.get("result", {}).get("cumRealisedPnl", None)

        # 如果是 Exit 分支，再解析並覆寫成交量（Entry 不受影響）
        if action in ("tp1", "stop", "trail", "breakeven", "residual"):
            executed_qty = safe_float(
                order_result.get("result", {}).get("cumExecQty")
                or order_result.get("result", {}).get("execQty")
                or order_result.get("result", {}).get("qty"),
                0.0
            )
            # 僅在 Exit 時用 Bybit 回傳量覆寫
            contracts = executed_qty
            qty       = executed_qty
        # ——————————————————————————————————————————————————————————————

        # 寫入 log.json
        with open(log_json_path, "r+") as f:
            logs = json.load(f)
            logs.append({
                "pine_time": pine_time,
                "server_time": server_time,
                "strategy_id": strategy_id,
                "event": event,
                "trigger_type": trigger_type,
                "comment": comment,
                "contracts": contracts,
                "equity": equity,
                "order_action": order_action,
                "ret_code": ret_code,
                "ret_msg": ret_msg,
                "pnl": pnl,
                "price": price,
                "qty": qty
            })
            f.seek(0); json.dump(logs, f, indent=2)

        # 寫入 Google Sheets
        if sheet:
            write_to_gsheet(
                pine_time, server_time,
                strategy_id, event,
                equity, None,            # drawdown
                order_action, trigger_type,
                comment, contracts,
                ret_code, ret_msg,
                pnl, price, qty
            )

        return {"status": "ok"}
    except Exception as e:
        print(f"[⚠️ TV Webhook 錯誤]：{e}")
        return {"status": "error", "message": str(e)}
        
@app.post("/tv_webhook_test")
async def tv_webhook_test(request: Request):
    try:
        payload = await request.json()
        if payload.get("secret", "") != os.getenv("WEBHOOK_SECRET", "letmein"):
            return {"status": "unauthorized"}

        strategy_id  = payload.get("strategy_id", "")
        order_id     = payload.get("order_id", "")
        action       = "Buy" if "long" in order_id else "Sell"
        symbol       = payload.get("symbol", "")
        price        = safe_float(payload.get("price"))
        trigger_type = payload.get("trigger_type", "")

        pine_time   = payload.get("time", "")
        tz_tw       = timezone(timedelta(hours=8))
        server_time = datetime.now(tz=tz_tw).strftime("%Y-%m-%d %H:%M:%S")

        await place_order(symbol, action, 0.01)

        # log.json
        with open(log_json_path, "r+") as f:
            logs = json.load(f)
            logs.append({
                "pine_time": pine_time,
                "server_time": server_time,
                "strategy_id": strategy_id,
                "event": order_id + "_test",
                "equity": None,
                "drawdown": None,
                "order_action": action,
                "trigger_type": trigger_type,
                "comment": None,
                "contracts": None,
                "ret_code": None,
                "ret_msg": None,
                "pnl": None,
                "price": price,
                "qty": 0.01
            })
            f.seek(0); json.dump(logs, f, indent=2)

        # Google Sheets
        if sheet:
            write_to_gsheet(
                pine_time, server_time,
                strategy_id, order_id + "_test",
                None, None,           # equity, drawdown
                action, trigger_type,
                None, None,           # comment, contracts
                None, None, None,     # ret_code, ret_msg, pnl
                price, 0.01
            )

        return {"status": "ok"}
    except Exception as e:
        print(f"[⚠️ TV 測試 webhook 錯誤]：{e}")
        return {"status": "error", "message": str(e)}


# 🗂️ 15 列完整版 /webhook
@app.post("/webhook")
async def webhook_handler(payload: WebhookPayload):
    # 0. Pine 時間（假設前端已傳 "time" 欄位，格式同 tv_webhook）
    pine_time   = payload.__dict__.get("time", "")  
    # 1. Server 接收時戳（台北時區）
    tz_tw       = timezone(timedelta(hours=8))
    server_time = datetime.now(tz=tz_tw).strftime("%Y-%m-%d %H:%M:%S")

    # 2. 其它欄位解包
    strategy_id  = payload.strategy_id
    event        = payload.signal_type
    equity       = payload.equity
    drawdown     = None
    order_action = payload.data.action       if payload.data else ""
    trigger_type = payload.signal_type
    comment      = None
    contracts    = payload.data.position_size if payload.data else None

    # 3. 這裡沒訂單回報，所以留空
    ret_code = None
    ret_msg  = None
    pnl      = None
    price    = None
    qty      = None

    # 4. 寫入 Google Sheets（15 列）
    write_to_gsheet(
        pine_time,  server_time,
        strategy_id, event,
        equity,     drawdown,
        order_action, trigger_type,
        comment,    contracts,
        ret_code,   ret_msg,
        pnl,        price,
        qty
    )

    return {"status": "ok", "strategy_id": strategy_id}


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
