from fastapi import FastAPI
from pydantic import BaseModel
import httpx
import os
import time
import hmac
import hashlib
import json

app = FastAPI()

# === 型別定義 ===
class WebhookPayloadData(BaseModel):
    action: str
    position_size: float

class WebhookPayload(BaseModel):
    data: WebhookPayloadData
    price: float
    signal_type: str
    order_type: str
    symbol: str
    time: str

# === Bybit v5 下單函數（修正版）===
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
        "qty": str(qty),  # Bybit 要求 qty 為 string
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
    action = payload.data.action
    size = float(payload.data.position_size)
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
