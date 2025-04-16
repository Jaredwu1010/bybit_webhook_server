from fastapi import FastAPI, Request
from pydantic import BaseModel
import httpx
import os
import time
import hmac
import hashlib
import json

app = FastAPI()

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

max_equity = {}
strategy_status = {}

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

    # Secret Key 驗證
    if payload.secret != WEBHOOK_SECRET:
        return {"status": "blocked", "reason": "invalid secret", "strategy_id": sid}

    # 處理 equity 更新
    if payload.signal_type == "equity_update":
        eq = float(payload.equity)
        max_eq = max_equity.get(sid, 0)

        if eq > max_eq:
            max_equity[sid] = eq
            strategy_status[sid] = {"paused": False}
            return {"status": "ok", "strategy_id": sid, "drawdown": 0.0}

        dd = (1 - eq / max_eq) * 100 if max_eq > 0 else 0
        if dd >= MAX_DRAWDOWN_PERCENT:
            strategy_status[sid] = {"paused": True}
            return {"status": "paused", "reason": "MDD exceeded", "strategy_id": sid, "drawdown": round(dd, 2)}

        return {"status": "ok", "strategy_id": sid, "drawdown": round(dd, 2)}

    # 處理 reset
    if payload.signal_type == "reset":
        strategy_status[sid] = {"paused": False}
        return {"status": "reset", "strategy_id": sid}

    # 如果已經停單則不處理
    if strategy_status.get(sid, {}).get("paused", False):
        return {"status": "blocked", "reason": "MDD stop active", "strategy_id": sid}

    # 處理進場下單
    if payload.signal_type in ["entry_long", "entry_short"] and payload.data:
        action = payload.data.action
        size = float(payload.data.position_size or 0)
        symbol = payload.symbol
        order_type = payload.order_type

        if size == 0:
            return {"status": "ok", "message": "倉量為 0 不處理"}

        side = "Buy" if action == "buy" else "Sell"
        result = await place_order(symbol, side, size)
        return {"status": "success", "bybit_response": result}

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
