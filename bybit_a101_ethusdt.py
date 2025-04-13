from fastapi import FastAPI, Request
from pydantic import BaseModel
import httpx
import os
import time

app = FastAPI()

# ✅ 讀取 Bybit API 金鑰（來自 Render 環境變數）
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")
BYBIT_API_URL = "https://api.bybit.com"  # 實盤用 URL；測試網用 https://api-testnet.bybit.com

# 📥 TradingView 傳來的 webhook 格式對應
class WebhookPayload(BaseModel):
    data: dict
    price: float
    signal_type: str
    order_type: str
    symbol: str
    time: str

# 🧠 Bybit 下單函式（市價單）
async def place_order(symbol: str, side: str, qty: float):
    endpoint = f"{BYBIT_API_URL}/v5/order/create"
    headers = {
        "X-BYBIT-API-KEY": BYBIT_API_KEY,
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
    async with httpx.AsyncClient() as client:
        response = await client.post(endpoint, headers=headers, json=payload)
        return response.json()

# 🚀 Webhook 接收入口
@app.post("/webhook")
async def webhook_handler(payload: WebhookPayload):
    action = payload.data.get("action")
    size = float(payload.data.get("position_size", 0))
    symbol = payload.symbol
    order_type = payload.order_type
    print(f"[Webhook] 接收到訊號：{order_type} | {action} {size} {symbol}")

    if size == 0:
        print("⚠️ 這是平倉訊號，尚未實作處理邏輯。")
        return {"status": "ok", "message": "Received flat close command."}

    side = "Buy" if action == "buy" else "Sell"
    result = await place_order(symbol, side, size)
    print(f"[Bybit] 下單結果：{result}")
    return {"status": "success", "bybit_response": result}
