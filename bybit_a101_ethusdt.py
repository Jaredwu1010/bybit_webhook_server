from fastapi import FastAPI
from pydantic import BaseModel
from typing import Optional
import httpx
import os
import time
import hmac
import hashlib
import json

app = FastAPI()

# === 策略狀態紀錄（MDD 控制） ===
strategy_status = {}
MDD_LIMIT = 10.0  # 最大回撤百分比

# === 型別定義 ===
class WebhookPayloadData(BaseModel):
    action: Optional[str] = None
    position_size: Optional[float] = 0.0

class WebhookPayload(BaseModel):
    strategy_id: Optional[str] = None
    signal_type: Optional[str] = None
    equity: Optional[float] = None
    timestamp: Optional[str] = None
    data: Optional[WebhookPayloadData] = None
    price: Optional[float] = None
    order_type: Optional[str] = None
    symbol: Optional[str] = None
    time: Optional[str] = None

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
    signature = hmac.new(
        api_secret.encode("utf-8"),
        sign_str.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

    headers = httpx.Headers({
        "X-BAPI-API-KEY": api_key,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-RECV-WINDOW": recv_window,
        "X-BAPI-SIGN": signature,
        "Content-Type": "application/json"
    })

    print(f"[Bybit] 下單請求：{payload}")
    print(f"[Bybit] HTTP headers：{headers}")

    async with httpx.AsyncClient() as client:
        response = await client.post(endpoint, headers=headers, data=payload_str)
        print(f"[Bybit] 回應：{response.status_code} | {response.text}")
        return response.json()

# === Webhook 接收入口 ===
@app.post("/webhook")
async def webhook_handler(payload: WebhookPayload):
    sid = payload.strategy_id or payload.symbol or "unknown"
    signal = payload.signal_type

    # === MDD 控制邏輯 ===
    if signal == "equity_update" and payload.equity is not None:
        equity = payload.equity

        if sid not in strategy_status:
            strategy_status[sid] = {
                "max_equity": equity,
                "last_equity": equity,
                "paused": False
            }

        strategy_status[sid]["max_equity"] = max(strategy_status[sid]["max_equity"], equity)
        strategy_status[sid]["last_equity"] = equity

        max_eq = strategy_status[sid]["max_equity"]
        dd = (max_eq - equity) / max_eq * 100

        if dd >= MDD_LIMIT:
            strategy_status[sid]["paused"] = True
            return {
                "status": "paused",
                "reason": "MDD exceeded",
                "strategy_id": sid,
                "drawdown": round(dd, 2)
            }

        return {
            "status": "ok",
            "strategy_id": sid,
            "drawdown": round(dd, 2)
        }

    # === 若已達停單狀態，則阻擋下單 ===
    if strategy_status.get(sid, {}).get("paused", False):
        print(f"[MDD STOP] 拒絕下單：{sid} 已觸發停單保護")
        return {"status": "blocked", "reason": "MDD stop active", "strategy_id": sid}

    # === 處理下單 webhook ===
    if payload.data:
        action = payload.data.action
        size = float(payload.data.position_size or 0)
        symbol = payload.symbol
        order_type = payload.order_type

        print(f"[Webhook] 接收到訊號：{order_type} | {action} | {symbol} | size={size}")

        if size == 0:
            print("⚠️ 倉量為 0，忽略下單請求")
            return {"status": "ok", "message": "倉量為 0 不處理"}

        side = "Buy" if action == "buy" else "Sell"
        result = await place_order(symbol, side, size)
        print(f"[Webhook] 完成下單：{result}")

        return {"status": "success", "bybit_response": result}

    return {"status": "ignored", "message": "無法處理的 webhook"}
