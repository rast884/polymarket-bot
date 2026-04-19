"""
BTC 5MIN Polymarket Bot — без py-clob-client
Работает напрямую через Polymarket HTTP API
"""

import os, time, json, logging, asyncio, hmac, hashlib, base64
from datetime import datetime, timezone
from typing import Optional
import httpx
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("btc5m")

API_KEY        = os.getenv("POLYMARKET_API_KEY", "")
API_SECRET     = os.getenv("POLYMARKET_API_SECRET", "")
API_PASSPHRASE = os.getenv("POLYMARKET_PASSPHRASE", "")
PRIVATE_KEY    = os.getenv("POLYMARKET_PRIVATE_KEY", "")

BET_USD            = float(os.getenv("BET_USD",        "2.0"))
MAX_DAILY_LOSS_USD = float(os.getenv("MAX_DAILY_LOSS", "10.0"))
MIN_BALANCE_USD    = float(os.getenv("MIN_BALANCE",    "5.0"))
MIN_SCORE          = float(os.getenv("MIN_SCORE",      "3.5"))
WIN_MULT           = float(os.getenv("WIN_MULT",       "0.9"))
START_BALANCE      = float(os.getenv("START_BALANCE",  "100.0"))

ROUND_SEC = 300
TICK_SEC  = 5
CLOB_HOST  = "https://clob.polymarket.com"
GAMMA_HOST = "https://gamma-api.polymarket.com"

state = {
    "balance":    START_BALANCE,
    "pnl":        0.0,
    "wins":       0,
    "losses":     0,
    "daily_loss": 0.0,
    "daily_date": "",
    "pending":    None,
}

def round_id(ts=None):
    t = ts or time.time()
    return int(t // ROUND_SEC) * ROUND_SEC

def round_remain():
    return (round_id() + ROUND_SEC) - time.time()

def fmt_window(rid):
    s = datetime.fromtimestamp(rid, tz=timezone.utc)
    e = datetime.fromtimestamp(rid + ROUND_SEC, tz=timezone.utc)
    return f"{s:%H:%M}-{e:%H:%M} UTC"

def reset_daily():
    today = datetime.utcnow().strftime("%Y-%m-%d")
    if state["daily_date"] != today:
        state["daily_date"] = today
        state["daily_loss"] = 0.0
        log.info("New day - daily limit reset")

def save_state():
    try:
        with open("state.json", "w") as f:
            json.dump(state, f, ensure_ascii=False, indent=2, default=str)
    except Exception as e:
        log.warning(f"Save error: {e}")

def load_state():
    try:
        with open("state.json") as f:
            state.update(json.load(f))
        log.info(f"Loaded: balance=${state['balance']:.2f} PnL=${state['pnl']:.2f}")
    except FileNotFoundError:
        log.info("Starting fresh")

def make_auth_headers(method, path, body=""):
    timestamp = str(int(time.time()))
    message   = timestamp + method.upper() + path + body
    try:
        secret  = base64.b64decode(API_SECRET + "==")
    except Exception:
        secret  = API_SECRET.encode()
    signature = hmac.new(secret, message.encode(), hashlib.sha256).digest()
    sig_b64   = base64.b64encode(signature).decode()
    return {
        "POLY-API-KEY":    API_KEY,
        "POLY-SIGNATURE":  sig_b64,
        "POLY-TIMESTAMP":  timestamp,
        "POLY-PASSPHRASE": API_PASSPHRASE,
        "Content-Type":    "application/json",
    }

async def get_price():
    async with httpx.AsyncClient(timeout=6) as client:
        try:
            r = await client.get("https://api.bybit.com/v5/market/tickers?category=spot&symbol=BTCUSDT")
            d = r.json()["result"]["list"][0]
            return {"price": float(d["lastPrice"]), "change": float(d["price24hPcnt"])*100, "source": "Bybit"}
        except Exception:
            pass
        try:
            r = await client.get("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd&include_24hr_change=true")
            d = r.json()["bitcoin"]
            return {"price": float(d["usd"]), "change": float(d.get("usd_24h_change", 0)), "source": "CoinGecko"}
        except Exception:
            pass
    return {"price": 0, "change": 0, "source": "NONE"}

async def get_klines(limit=10):
    async with httpx.AsyncClient(timeout=6) as client:
        try:
            r = await client.get("https://api.bybit.com/v5/market/kline",
                params={"category": "spot", "symbol": "BTCUSDT", "interval": "1", "limit": limit})
            data = r.json()["result"]["list"]
            return [{"o": float(k[1]), "h": float(k[2]), "l": float(k[3]), "c": float(k[4]), "v": float(k[5])} for k in reversed(data)]
        except Exception as e:
            log.warning(f"Klines error: {e}")
    return []

async def find_market():
    async with httpx.AsyncClient(timeout=8) as client:
        try:
            now_sec   = int(time.time())
            window_ts = now_sec - (now_sec % ROUND_SEC)
            slug      = f"btc-updown-5m-{window_ts}"
            r = await client.get(f"{GAMMA_HOST}/events?slug={slug}")
            events = r.json()
            if events and len(events) > 0:
                market = events[0].get("markets", [{}])[0]
                prices = market.get("outcomePrices", "[0.5,0.5]")
                if isinstance(prices, str):
                    prices = json.loads(prices)
                return {
                    "id":       market.get("id") or market.get("conditionId"),
                    "up_price": float(prices[0]) if prices else 0.5,
                    "dn_price": float(prices[1]) if len(prices) > 1 else 0.5,
                    "volume":   float(market.get("volume", 0)),
                    "tokens":   market.get("clobTokenIds", []),
                }
        except Exception as e:
            log.debug(f"Market search error: {e}")
        try:
            r = await client.get(f"{GAMMA_HOST}/markets",
                params={"q": "BTC UP DOWN 5 minutes", "active": "true", "limit": 3})
            resp = r.json()
            markets = resp if isinstance(resp, list) else resp.get("markets", [])
            if markets:
                m = markets[0]
                prices = m.get("outcomePrices", "[0.5,0.5]")
                if isinstance(prices, str):
                    prices = json.loads(prices)
                log.info(f"Market found: {m.get('question','?')}")
                return {
                    "id":       m.get("id") or m.get("conditionId"),
                    "up_price": float(prices[0]) if prices else 0.5,
                    "dn_price": float(prices[1]) if len(prices) > 1 else 0.5,
                    "volume":   float(m.get("volume", 0)),
                    "tokens":   m.get("clobTokenIds", []),
                }
        except Exception as e:
            log.debug(f"Keyword search error: {e}")
    return None

async def place_order(market, direction, amount_usd):
    if not market or not API_KEY:
        log.error("No market or API key")
        return False
    try:
        tokens = market.get("tokens", [])
        is_up  = direction == "UP"
        if len(tokens) >= 2:
            token_id = tokens[0] if is_up else tokens[1]
        elif len(tokens) == 1:
            token_id = tokens[0]
        else:
            log.error("No token_id")
            return False

        price    = market["up_price"] if is_up else market["dn_price"]
        size     = round(amount_usd / max(price, 0.01), 2)
        body_obj = {"orderType": "FOK", "tokenID": str(token_id), "side": "BUY",
                    "price": str(round(price, 4)), "size": str(size)}
        body_str = json.dumps(body_obj)
        headers  = make_auth_headers("POST", "/order", body_str)

        log.info(f"Order: {direction} token={str(token_id)[:8]}... price={price:.3f} size={size} ~${amount_usd:.2f}")

        async with httpx.AsyncClient(timeout=10) as client:
            r    = await client.post(f"{CLOB_HOST}/order", headers=headers, content=body_str)
            data = r.json()
            log.info(f"Response: {data}")
            if r.status_code in (200, 201) and (data.get("success") or data.get("orderID") or data.get("id")):
                log.info(f"Order accepted! ID: {data.get('orderID') or data.get('id','OK')}")
                return True
            else:
                log.error(f"Order rejected: {data}")
                return False
    except Exception as e:
        log.error(f"Order error: {e}")
        return False

def ai_signal(klines, pm_up=0.5, pm_down=0.5):
    if len(klines) < 3:
        return {"direction": "UP", "confidence": 52, "score": 0, "skip": True, "reason": "No data"}

    n = len(klines)
    c = [k["c"] for k in klines]; o = [k["o"] for k in klines]
    h = [k["h"] for k in klines]; l = [k["l"] for k in klines]
    v = [k["v"] for k in klines]
    score = 0.0; sigs = []

    rA = (c[-1]+c[-2]+c[-3])/3; eA = (c[0]+c[1]+c[2])/3
    if   rA > eA*1.001: score += 2.5; sigs.append("TrendUP")
    elif rA < eA*0.999: score -= 2.5; sigs.append("TrendDN")

    lb = c[-1]-o[-1]; lr = h[-1]-l[-1]; br = abs(lb)/lr if lr > 0 else 0
    if   lb > 0 and br > 0.6: score += 2.0; sigs.append("BullCandle")
    elif lb < 0 and br > 0.6: score -= 2.0; sigs.append("BearCandle")

    bR  = sum(1 for i in range(n-3,n) if c[i]>o[i])
    beR = sum(1 for i in range(n-3,n) if c[i]<o[i])
    if bR  == 3: score += 2.5; sigs.append("3xBull")
    if beR == 3: score -= 2.5; sigs.append("3xBear")

    avg_v = sum(v[:-1])/(n-1) if n>1 else 1
    if v[-1] > avg_v*1.5:
        score += 1.5 if lb>0 else -1.5

    ranges = [h[i]-l[i] for i in range(n)]; avg_r = sum(ranges)/n
    if lr > avg_r*2.5: score *= 0.3; sigs.append("Reversal")

    gains  = [c[i]-c[i-1] for i in range(1,n) if c[i]>c[i-1]]
    losses = [abs(c[i]-c[i-1]) for i in range(1,n) if c[i]<c[i-1]]
    aG = sum(gains)/len(gains) if gains else 0
    aL = sum(losses)/len(losses) if losses else 0.001
    rsi = 100 - 100/(1+aG/aL)
    if   rsi > 72: score -= 2.0; sigs.append(f"RSI{rsi:.0f}OB")
    elif rsi < 28: score += 2.0; sigs.append(f"RSI{rsi:.0f}OS")

    if pm_up != 0.5:
        if   pm_up >= 0.65: score += 4.0; sigs.append(f"PM_UP{pm_up*100:.0f}")
        elif pm_up <= 0.35: score -= 4.0; sigs.append(f"PM_DN{pm_up*100:.0f}")
        elif pm_up >= 0.57: score += 2.0
        elif pm_up <= 0.43: score -= 2.0

    hour_utc = datetime.utcnow().hour
    if not (7 <= hour_utc <= 21):
        score *= 0.4; sigs.append("Night")

    abs_score  = abs(score)
    skip       = abs_score < MIN_SCORE
    direction  = "UP" if score >= 0 else "DOWN"
    confidence = min(87, max(52, 52 + abs_score*4))
    reason     = f"Score:{score:.1f} RSI:{rsi:.0f} PM:{pm_up:.2f} {' '.join(sigs)} -> {'UP' if direction=='UP' else 'DOWN'} {confidence:.0f}%{' SKIP' if skip else ''}"

    return {"direction": direction, "confidence": confidence, "score": score, "skip": skip, "reason": reason}

async def bot_loop():
    log.info("BTC 5MIN Polymarket Bot started!")
    log.info(f"Bet: ${BET_USD} | Daily limit: ${MAX_DAILY_LOSS_USD} | Min score: {MIN_SCORE}")

    load_state()

    market         = None
    last_market_ts = 0
    placed_round   = None
    resolved_round = None

    while True:
        try:
            reset_daily()
            rid    = round_id()
            remain = round_remain()

            if state["balance"] < MIN_BALANCE_USD:
                log.warning(f"STOP: Balance ${state['balance']:.2f} < ${MIN_BALANCE_USD}")
                break

            if state["daily_loss"] >= MAX_DAILY_LOSS_USD:
                log.warning(f"STOP: Daily limit ${MAX_DAILY_LOSS_USD} reached. Pause 5min.")
                await asyncio.sleep(300)
                continue

            if time.time() - last_market_ts > 240:
                market         = await find_market()
                last_market_ts = time.time()
                if market:
                    log.info(f"Market: UP={market['up_price']:.2f} DOWN={market['dn_price']:.2f} Vol={market['volume']:.0f}")
                else:
                    log.warning("No active BTC 5MIN market found")

            if remain > ROUND_SEC - 20 and placed_round != rid:
                klines = await get_klines()
                price  = await get_price()
                pm_up  = market["up_price"] if market else 0.5
                pm_dn  = market["dn_price"] if market else 0.5
                sig    = ai_signal(klines, pm_up, pm_dn)

                log.info(f"=== Round {fmt_window(rid)} ===")
                log.info(f"BTC: ${price['price']:,.0f} ({price['change']:+.2f}%) [{price['source']}]")
                log.info(f"AI: {sig['reason']}")

                placed_round = rid

                if sig["skip"] or not market:
                    log.info("SKIP - weak signal or no market")
                else:
                    log.info(f"BET: {sig['direction']} ${BET_USD:.2f} conf={sig['confidence']:.0f}%")
                    ok = await place_order(market, sig["direction"], BET_USD)
                    if ok:
                        state["balance"]    -= BET_USD
                        state["daily_loss"] += BET_USD
                        state["pending"] = {
                            "rid":         rid,
                            "direction":   sig["direction"],
                            "bet":         BET_USD,
                            "start_price": price["price"],
                        }
                        save_state()
                        log.info(f"Bet placed. Balance: ${state['balance']:.2f}")

            elif remain < 8 and state["pending"] and resolved_round != rid:
                bet   = state["pending"]
                price = await get_price()
                up    = price["price"] >= bet["start_price"]
                won   = (bet["direction"]=="UP" and up) or (bet["direction"]=="DOWN" and not up)

                if won:
                    profit = round(bet["bet"] * WIN_MULT, 2)
                    state["balance"]    += bet["bet"] + profit
                    state["daily_loss"] -= bet["bet"]
                    state["pnl"]        += profit
                    state["wins"]       += 1
                    log.info(f"WIN! +${profit:.2f} Balance: ${state['balance']:.2f}")
                else:
                    state["pnl"]    -= bet["bet"]
                    state["losses"] += 1
                    log.info(f"LOSS -${bet['bet']:.2f} Balance: ${state['balance']:.2f}")

                log.info(f"Stats: W={state['wins']} L={state['losses']} PnL=${state['pnl']:.2f}")
                state["pending"] = None
                resolved_round   = rid
                save_state()

            await asyncio.sleep(TICK_SEC)

        except KeyboardInterrupt:
            log.info("Bot stopped")
            save_state()
            break
        except Exception as e:
            log.error(f"Loop error: {e}")
            await asyncio.sleep(10)

if __name__ == "__main__":
    asyncio.run(bot_loop())
