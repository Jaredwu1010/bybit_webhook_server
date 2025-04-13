from fastapi import FastAPI
from pydantic import BaseModel
import httpx
import os

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

# === Bybit 下單函數（帶簽名） ===
async def place_order(symbol: str, side: str, qty: float):
    endpoint = f"{os.environ['BYBIT_API_URL']}/v5/order/create"
    headers = {
        "X-BYBIT-API-KEY": os.environ['BYBIT_API_KEY'],
        "Content-Type": "application/json"
    }
    
    payload = {
        "category": "linear",
        "symbol": symbol,
        "side": side,
        "orderType": "Market",
        "qty": qty,
        "timeInForce": "IOC"
    }

    print(f"[Bybit] 下單請求：{payload}")

    async with httpx.AsyncClient() as client:
        response = await client.post(endpoint, headers=headers, json=payload)
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
