"""
階段二：幣安 USDT 本位合約 Testnet 下單 API 連通性測試。

只測「水管通不通」——簽名（HMAC SHA256）、開倉、設 STOP_MARKET/TAKE_PROFIT_MARKET、
查詢持倉/掛單、平倉、撤單——完全不測策略邏輯（那件事已經由 PaperEngine + 真實行情
驗證過，Testnet 的價格是別的測試機器人亂打的，看訊號有沒有觸發沒有意義）。

事前準備：
1. 到 https://testnet.binancefuture.com 用獨立帳號登入（跟正式幣安帳號分開），
   在 API Management 申請一組 Testnet 專用 API Key/Secret。
2. 設定環境變數（不要寫進程式碼或commit）：
     Windows PowerShell:  $env:BINANCE_TESTNET_API_KEY="..."; $env:BINANCE_TESTNET_API_SECRET="..."
     bash:                export BINANCE_TESTNET_API_KEY=...  BINANCE_TESTNET_API_SECRET=...
3. Testnet 帳戶預設會有模擬 USDT 餘額，若沒有可以在網站上用水龍頭（faucet）領取。

執行方式：在 backend/ 目錄下 `python experiment/scripts/2026-07-02_testnet_order_api_check.py`

跑完之後人工核對每一段 `<<< body=` 印出來的欄位是否符合預期（成交價/數量、SL/TP掛單
是否真的出現在 openOrders、平倉後 openOrders 是否清空），這份腳本本身只負責跑流程、
印原始回應，不做正確性斷言（斷言什麼算「正確」需要人判斷 Binance 文件定義的欄位）。
"""
import hashlib
import hmac
import os
import sys
import time
import urllib.parse

import httpx

BASE_URL = "https://testnet.binancefuture.com"
API_KEY = os.environ.get("BINANCE_TESTNET_API_KEY")
API_SECRET = os.environ.get("BINANCE_TESTNET_API_SECRET")
SYMBOL = "BTCUSDT"
TARGET_NOTIONAL = 150.0  # 略高於一般最低名目金額（BTCUSDT期貨通常要求>=100 USDT）

_CLIENT_KW = dict(verify=False)  # 同 market.py：Windows 有時缺中繼憑證


def _sign(params: dict) -> dict:
    params = {**params, "timestamp": int(time.time() * 1000), "recvWindow": 5000}
    query = urllib.parse.urlencode(params)
    sig = hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    params["signature"] = sig
    return params


def _request(method: str, path: str, params: dict | None = None, signed: bool = True):
    params = dict(params or {})
    if signed:
        params = _sign(params)
    headers = {"X-MBX-APIKEY": API_KEY} if API_KEY else {}
    url = f"{BASE_URL}{path}"
    with httpx.Client(timeout=10, **_CLIENT_KW) as client:
        res = client.request(method, url, params=params, headers=headers)
    shown = {k: v for k, v in params.items() if k != "signature"}
    print(f"\n>>> {method} {path} params={shown}")
    print(f"<<< status={res.status_code}")
    try:
        data = res.json()
    except Exception:
        data = res.text
    print(f"<<< body={data}")
    if res.status_code >= 400:
        raise RuntimeError(f"{path} failed: {data}")
    return data


def get_mark_price(symbol: str) -> float:
    data = _request("GET", "/fapi/v1/ticker/price", {"symbol": symbol}, signed=False)
    return float(data["price"])


def main():
    if not API_KEY or not API_SECRET:
        sys.exit(
            "請先設定環境變數 BINANCE_TESTNET_API_KEY / BINANCE_TESTNET_API_SECRET\n"
            "（到 https://testnet.binancefuture.com 申請 Testnet 專用 API Key，"
            "跟正式帳號的 Key 是分開的）"
        )

    print("=== 階段二：Testnet 下單 API 連通性測試 ===")

    # 1) 帳戶資訊：驗證簽名／API Key本身正確
    _request("GET", "/fapi/v2/account")

    # 2) 設定槓桿：驗證「設定類」signed POST也正常
    _request("POST", "/fapi/v1/leverage", {"symbol": SYMBOL, "leverage": 5})

    # 3) 市價開多倉
    price = get_mark_price(SYMBOL)
    qty = round(max(TARGET_NOTIONAL / price, 0.002), 3)
    print(f"\n目前 {SYMBOL} 市價 ≈ {price}，測試單數量 = {qty}")
    _request("POST", "/fapi/v1/order", {
        "symbol": SYMBOL, "side": "BUY", "type": "MARKET", "quantity": qty,
    })

    # 4) 查詢持倉，確認真的成交、均價/數量合理
    _request("GET", "/fapi/v2/positionRisk", {"symbol": SYMBOL})

    # 5) 設 STOP_MARKET（止損）與 TAKE_PROFIT_MARKET（止盈）
    #    closePosition=true 時不能帶 quantity，會直接平掉當時的全部持倉
    sl_price = round(price * 0.97, 1)
    tp_price = round(price * 1.03, 1)
    _request("POST", "/fapi/v1/order", {
        "symbol": SYMBOL, "side": "SELL", "type": "STOP_MARKET",
        "stopPrice": sl_price, "closePosition": "true",
    })
    _request("POST", "/fapi/v1/order", {
        "symbol": SYMBOL, "side": "SELL", "type": "TAKE_PROFIT_MARKET",
        "stopPrice": tp_price, "closePosition": "true",
    })

    # 6) 查詢掛單，確認SL/TP兩張都真的掛上去了
    _request("GET", "/fapi/v1/openOrders", {"symbol": SYMBOL})

    # 7) 市價平倉（reduceOnly，確保只平倉不會反手開新倉）
    _request("POST", "/fapi/v1/order", {
        "symbol": SYMBOL, "side": "SELL", "type": "MARKET",
        "quantity": qty, "reduceOnly": "true",
    })

    # 8) 撤銷剩餘的SL/TP掛單——平倉後這兩張條件單還會留著，呼應
    #    chan_lun_audit_report.md 陷阱4.4「強平後未撤掛單導致二次幽靈開倉」的教訓，
    #    這裡驗證的是「手動平倉後，你自己的執行模組有沒有記得撤單」這個下單流程本身。
    _request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": SYMBOL})

    # 9) 再查一次持倉+掛單，確認乾淨收尾（應該都是空的）
    _request("GET", "/fapi/v2/positionRisk", {"symbol": SYMBOL})
    _request("GET", "/fapi/v1/openOrders", {"symbol": SYMBOL})

    print("\n=== 全部步驟完成，請逐一核對上面每段 <<< body 的欄位是否符合預期 ===")


if __name__ == "__main__":
    main()
