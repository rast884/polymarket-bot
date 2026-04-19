"""
╔══════════════════════════════════════════════════════╗
║       BTC 5MIN POLYMARKET BOT  —  Real Trading       ║
║       Сигналы: RSI + Тренд + Momentum + Sentim.      ║
╚══════════════════════════════════════════════════════╝

БЕЗОПАСНОСТЬ:
  • Приватный ключ хранится только в .env — НИКОГДА в коде
  • Дневной лимит потерь: MAX_DAILY_LOSS_USD
  • Минимальный скор для ставки: MIN_SCORE
  • Автостоп при балансе < MIN_BALANCE_USD
"""

import os, time, math, json, logging, asyncio
from datetime import datetime, timezone
from typing import Optional
import httpx
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, Side

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

# ══════════════════════════════════════════
#  КОНФИГ  (всё через .env)
# ══════════════════════════════════════════
PRIVATE_KEY       = os.getenv("POLYMARKET_PRIVATE_KEY")   # 0x...
API_KEY           = os.getenv("POLYMARKET_API_KEY")        # из polymarket.com
API_SECRET        = os.getenv("POLYMARKET_API_SECRET")
API_PASSPHRASE    = os.getenv("POLYMARKET_PASSPHRASE")
CHAIN_ID          = int(os.getenv("CHAIN_ID", "137"))      # 137 = Polygon

BET_USD           = float(os.getenv("BET_USD",   "2.0"))   # ставка за раунд
MAX_DAILY_LOSS_USD= float(os.getenv("MAX_DAILY_LOSS", "10.0"))
MIN_BALANCE_USD   = float(os.getenv("MIN_BALANCE",    "5.0"))
MIN_SCORE         = float(os.getenv("MIN_SCORE",      "3.5"))  # порог сигнала
WIN_MULT          = float(os.getenv("WIN_MULT",       "0.9"))  # ~90% выплата

ROUND_SEC         = 300  # 5 минут
TICK_SEC          = 5    # частота проверки

# ══════════════════════════════════════════
#  СОСТОЯНИЕ БОТА
# ══════════════════════════════════════════
state = {
    "balance":     float(os.getenv("START_BALANCE", "100.0")),
    "pnl":         0.0,
    "wins":        0,
    "losses":      0,
    "daily_loss":  0.0,
    "daily_date":  "",
    "pending":     None,   # текущая активная ставка
    "last_round":  0,
}

# ══════════════════════════════════════════
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ══════════════════════════════════════════

def round_id(ts: float = None) -> int:
    """Возвращает начало текущего 5-минутного окна (Unix ms)."""
    t = ts or time.time()
    return int(t // ROUND_SEC) * ROUND_SEC

def round_remain() -> float:
    """Секунд до конца текущего раунда."""
    rid = round_id()
    return (rid + ROUND_SEC) - time.time()

def fmt_window(rid: int) -> str:
    s = datetime.fromtimestamp(rid, tz=timezone.utc)
    e = datetime.fromtimestamp(rid + ROUND_SEC, tz=timezone.utc)
    return f"{s:%H:%M}–{e:%H:%M} UTC"

def reset_daily_if_needed():
    today = datetime.utcnow().strftime("%Y-%m-%d")
    if state["daily_date"] != today:
        state["daily_date"]  = today
        state["daily_loss"]  = 0.0
        log.info("📅 Новый день — дневной лимит потерь сброшен")

def save_state():
    try:
        with open("state.json", "w") as f:
            json.dump(state, f, ensure_ascii=False, indent=2, default=str)
    except Exception as e:
        log.warning(f"Не удалось сохранить state.json: {e}")

def load_state():
    try:
        with open("state.json") as f:
            saved = json.load(f)
            state.update(saved)
            log.info(f"✅ Состояние загружено: баланс=${state['balance']:.2f}, PnL=${state['pnl']:.2f}")
    except FileNotFoundError:
        log.info("Файл state.json не найден — начинаем с нуля")

# ══════════════════════════════════════════
#  ЦЕНОВЫЕ ДАННЫЕ
# ══════════════════════════════════════════

async def get_price() -> dict:
    """Получаем BTC цену: Bybit → CoinGecko → fallback."""
    async with httpx.AsyncClient(timeout=6) as client:
        # 1. Bybit
        try:
            r = await client.get("https://api.bybit.com/v5/market/tickers?category=spot&symbol=BTCUSDT")
            d = r.json()["result"]["list"][0]
            return {"price": float(d["lastPrice"]), "change": float(d["price24hPcnt"])*100, "source": "Bybit"}
        except Exception:
            pass
        # 2. CoinGecko
        try:
            r = await client.get("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd&include_24hr_change=true")
            d = r.json()["bitcoin"]
            return {"price": float(d["usd"]), "change": float(d.get("usd_24h_change", 0)), "source": "CoinGecko"}
        except Exception:
            pass
    return {"price": 0, "change": 0, "source": "NONE"}

async def get_klines(limit=10) -> list:
    """Получаем свечи BTCUSDT 1-минутные с Bybit."""
    async with httpx.AsyncClient(timeout=6) as client:
        try:
            r = await client.get(
                "https://api.bybit.com/v5/market/kline",
                params={"category": "spot", "symbol": "BTCUSDT", "interval": "1", "limit": limit}
            )
            data = r.json()["result"]["list"]
            candles = [{"o": float(k[1]), "h": float(k[2]), "l": float(k[3]), "c": float(k[4]), "v": float(k[5])} for k in reversed(data)]
            return candles
        except Exception as e:
            log.warning(f"Klines Bybit ошибка: {e}")
    return []

async def get_polymarket_sentiment(market_id: str) -> dict:
    """Получаем вероятности UP/DOWN с Polymarket для конкретного рынка."""
    if not market_id:
        return {"up": 0.5, "down": 0.5, "volume": 0}
    async with httpx.AsyncClient(timeout=6) as client:
        try:
            r = await client.get(f"https://clob.polymarket.com/markets/{market_id}")
            d = r.json()
            tokens = d.get("tokens", [])
            up_price   = next((float(t["price"]) for t in tokens if t.get("outcome","").upper() in ("UP","YES","HIGHER")), 0.5)
            down_price = next((float(t["price"]) for t in tokens if t.get("outcome","").upper() in ("DOWN","NO","LOWER")), 0.5)
            volume = float(d.get("volume", 0))
            return {"up": up_price, "down": down_price, "volume": volume}
        except Exception as e:
            log.debug(f"Polymarket sentiment ошибка: {e}")
    return {"up": 0.5, "down": 0.5, "volume": 0}

# ══════════════════════════════════════════
#  AI СИГНАЛ (точная копия из HTML бота)
# ══════════════════════════════════════════

def ai_signal(klines: list, pm_up: float = 0.5, pm_down: float = 0.5) -> dict:
    """
    8-факторный AI сигнал — идентичен логике из btc-5m-bot.html
    Возвращает: direction, confidence, score, skip, signals, reason
    """
    if len(klines) < 3:
        return {"direction": "UP", "confidence": 52, "score": 0, "skip": True,
                "signals": [{"l": "NO DATA", "t": "neu"}], "reason": "Нет данных свечей"}

    n = len(klines)
    c = [k["c"] for k in klines]
    o = [k["o"] for k in klines]
    h = [k["h"] for k in klines]
    l = [k["l"] for k in klines]
    v = [k["v"] for k in klines]

    score = 0.0
    sigs  = []

    # 1. Тренд (последние 3 vs первые 3)
    rA = (c[-1] + c[-2] + c[-3]) / 3
    eA = (c[0]  + c[1]  + c[2])  / 3
    if   rA > eA * 1.001: score += 2.5; sigs.append({"l": "Тренд▲", "t": "bull"})
    elif rA < eA * 0.999: score -= 2.5; sigs.append({"l": "Тренд▼", "t": "bear"})
    else:                                sigs.append({"l": "Флет",    "t": "neu"})

    # 2. Тело последней свечи
    lb = c[-1] - o[-1]
    lr = h[-1] - l[-1]
    br = abs(lb) / lr if lr > 0 else 0
    if   lb > 0 and br > 0.6: score += 2.0; sigs.append({"l": "Bull свеча", "t": "bull"})
    elif lb < 0 and br > 0.6: score -= 2.0; sigs.append({"l": "Bear свеча", "t": "bear"})

    # 3. Моментум (3 свечи подряд)
    bR  = sum(1 for i in range(n-3, n) if c[i] > o[i])
    beR = sum(1 for i in range(n-3, n) if c[i] < o[i])
    if bR  == 3: score += 2.5; sigs.append({"l": "3× Bull", "t": "bull"})
    if beR == 3: score -= 2.5; sigs.append({"l": "3× Bear", "t": "bear"})

    # 4. Объём
    avg_v = sum(v[:-1]) / (n-1) if n > 1 else 1
    lV    = v[-1]
    if lV > avg_v * 1.5:
        s = 1.5 if lb > 0 else -1.5
        score += s
        sigs.append({"l": f"Vol×{lV/avg_v:.1f}", "t": "bull" if lb > 0 else "bear"})

    # 5. Разворот (аномальная свеча)
    ranges = [h[i] - l[i] for i in range(n)]
    avg_r  = sum(ranges) / n
    if lr > avg_r * 2.5:
        score *= 0.3
        sigs.append({"l": "Reversal", "t": "neu"})

    # 6. RSI
    gains  = [c[i]-c[i-1] for i in range(1,n) if c[i] > c[i-1]]
    losses = [abs(c[i]-c[i-1]) for i in range(1,n) if c[i] < c[i-1]]
    aG = sum(gains)  / len(gains)  if gains  else 0
    aL = sum(losses) / len(losses) if losses else 0.001
    rsi = 100 - 100 / (1 + aG / aL)
    if   rsi > 72: score -= 2.0; sigs.append({"l": f"RSI{rsi:.0f}OB", "t": "bear"})
    elif rsi < 28: score += 2.0; sigs.append({"l": f"RSI{rsi:.0f}OS", "t": "bull"})
    elif rsi > 60: score -= 0.5; sigs.append({"l": f"RSI{rsi:.0f}",   "t": "neu"})
    elif rsi < 40: score += 0.5; sigs.append({"l": f"RSI{rsi:.0f}",   "t": "neu"})
    else:                         sigs.append({"l": f"RSI{rsi:.0f}",   "t": "neu"})

    # 7. Polymarket Sentiment
    if pm_up != 0.5:
        if   pm_up >= 0.65: score += 4.0; sigs.append({"l": f"PM▲{pm_up*100:.0f}%", "t": "bull"})
        elif pm_up <= 0.35: score -= 4.0; sigs.append({"l": f"PM▼{pm_up*100:.0f}%", "t": "bear"})
        elif pm_up >= 0.57: score += 2.0; sigs.append({"l": f"PM▲{pm_up*100:.0f}%", "t": "bull"})
        elif pm_up <= 0.43: score -= 2.0; sigs.append({"l": f"PM▼{pm_up*100:.0f}%", "t": "bear"})
        else:                              sigs.append({"l": "PM~50/50",               "t": "neu"})

    # 8. Консенсус сигналов
    bull_count = sum(1 for s in sigs if s["t"] == "bull")
    bear_count = sum(1 for s in sigs if s["t"] == "bear")
    if bull_count >= 4 and bear_count == 0: score += 2.0; sigs.append({"l": "Консенсус▲", "t": "bull"})
    if bear_count >= 4 and bull_count == 0: score -= 2.0; sigs.append({"l": "Консенсус▼", "t": "bear"})

    # 9. Время суток (UTC 07–21 = активная сессия)
    hour_utc = datetime.utcnow().hour
    if 7 <= hour_utc <= 21:
        sigs.append({"l": "Активная сессия", "t": "bull"})
    else:
        score *= 0.4
        sigs.append({"l": "Ночь⚠", "t": "neu"})

    abs_score  = abs(score)
    skip       = abs_score < MIN_SCORE
    direction  = "UP" if score >= 0 else "DOWN"
    confidence = min(87, max(52, 52 + abs_score * 4))

    pm_str   = f" | PM:▲{pm_up*100:.0f}%▼{pm_down*100:.0f}%" if pm_up != 0.5 else ""
    skip_str = " | ⚠ПРОПУСК" if skip else ""
    reason   = (f"Скор:{score:.1f} RSI:{rsi:.0f}{pm_str} | "
                f"{bull_count}🟢{bear_count}🔴 | "
                f"{'▲ВВЕРХ' if direction=='UP' else '▼ВНИЗ'} {confidence:.0f}%{skip_str}")

    return {
        "direction":  direction,
        "confidence": confidence,
        "score":      score,
        "skip":       skip,
        "signals":    sigs,
        "reason":     reason,
        "rsi":        rsi,
    }

# ══════════════════════════════════════════
#  POLYMARKET ТОРГОВЛЯ
# ══════════════════════════════════════════

def get_clob_client() -> Optional[ClobClient]:
    """Создаём CLOB клиент для реальных ордеров."""
    if not PRIVATE_KEY:
        log.error("❌ POLYMARKET_PRIVATE_KEY не задан в .env!")
        return None
    try:
        client = ClobClient(
            host       = "https://clob.polymarket.com",
            key        = PRIVATE_KEY,
            chain_id   = CHAIN_ID,
            api_creds  = {
                "apiKey":     API_KEY,
                "secret":     API_SECRET,
                "passphrase": API_PASSPHRASE,
            } if API_KEY else None,
        )
        return client
    except Exception as e:
        log.error(f"Ошибка создания CLOB клиента: {e}")
        return None

async def get_balance_usdc(client: ClobClient) -> float:
    """Получаем реальный баланс USDC из Polymarket."""
    try:
        bal = client.get_balance()
        return float(bal)
    except Exception as e:
        log.warning(f"Не удалось получить баланс: {e}")
        return state["balance"]

async def find_btc5m_market() -> Optional[str]:
    """
    Ищем текущий активный BTC 5MIN рынок на Polymarket.
    Возвращает token_id для UP исхода.
    """
    async with httpx.AsyncClient(timeout=8) as client:
        try:
            now_sec    = int(time.time())
            window_ts  = now_sec - (now_sec % ROUND_SEC)
            slug       = f"btc-updown-5m-{window_ts}"

            # Пробуем через gamma-api
            r = await client.get(f"https://gamma-api.polymarket.com/events?slug={slug}")
            events = r.json()
            if events and len(events) > 0:
                market = events[0].get("markets", [{}])[0]
                market_id = market.get("id") or market.get("conditionId")
                log.info(f"✅ Рынок найден: {slug} | ID: {market_id}")
                return market_id

            # Альтернатива: поиск по ключевым словам
            r2 = await client.get(
                "https://gamma-api.polymarket.com/markets",
                params={"q": "BTC UP DOWN 5 minutes", "active": True, "limit": 5}
            )
            markets = r2.json() if isinstance(r2.json(), list) else r2.json().get("markets", [])
            if markets:
                m = markets[0]
                log.info(f"✅ Рынок найден по поиску: {m.get('question','?')}")
                return m.get("id") or m.get("conditionId")

        except Exception as e:
            log.warning(f"Ошибка поиска рынка: {e}")
    return None

async def place_real_bet(client: ClobClient, market_id: str, direction: str, amount_usd: float) -> bool:
    """
    Размещаем реальный ордер на Polymarket.
    direction: 'UP' или 'DOWN'
    amount_usd: сумма ставки в USDC
    """
    if not client or not market_id:
        log.error("❌ Нет клиента или market_id — ставка невозможна")
        return False
    try:
        # Получаем текущие цены для рынка
        async with httpx.AsyncClient(timeout=6) as http:
            r = await http.get(f"https://clob.polymarket.com/markets/{market_id}")
            mdata  = r.json()
            tokens = mdata.get("tokens", [])

        # Находим нужный токен (UP или DOWN)
        target_outcome = "UP" if direction == "UP" else "DOWN"
        token = next(
            (t for t in tokens if target_outcome in t.get("outcome", "").upper()),
            tokens[0] if tokens else None
        )
        if not token:
            log.error(f"❌ Не найден токен для {direction}")
            return False

        token_id = token["token_id"]
        price    = float(token["price"])  # вероятность 0-1

        # Считаем количество контрактов = $amount / price
        size = round(amount_usd / price, 2)

        log.info(f"📤 Ордер: {direction} | token={token_id[:8]}... | price={price:.3f} | size={size} | ~${amount_usd:.2f}")

        order_args = OrderArgs(
            token_id = token_id,
            price    = price,
            size     = size,
            side     = Side.BUY,
        )

        resp = client.create_and_post_order(order_args)
        order_id = resp.get("orderID") or resp.get("id", "?")
        log.info(f"✅ Ордер размещён! ID: {order_id}")
        return True

    except Exception as e:
        log.error(f"❌ Ошибка размещения ордера: {e}")
        return False

# ══════════════════════════════════════════
#  ОСНОВНОЙ ЦИКЛ БОТА
# ══════════════════════════════════════════

async def bot_loop():
    log.info("🤖 BTC 5MIN Polymarket Bot запущен!")
    log.info(f"   Ставка: ${BET_USD} | Дневной лимит: ${MAX_DAILY_LOSS_USD} | Мин. скор: {MIN_SCORE}")

    load_state()

    # Создаём CLOB клиент
    clob = get_clob_client()
    if not clob:
        log.critical("Не удалось создать клиент. Проверьте .env файл!")
        return

    # Обновляем реальный баланс
    real_balance = await get_balance_usdc(clob)
    if real_balance > 0:
        state["balance"] = real_balance
        log.info(f"💰 Реальный баланс USDC: ${real_balance:.2f}")

    last_market_id   = None
    last_market_ts   = 0
    pm_sentiment     = {"up": 0.5, "down": 0.5, "volume": 0}

    placed_this_round  = None   # rid для которого уже ставили
    resolved_this_round = None  # rid который уже разрезолвили

    while True:
        try:
            reset_daily_if_needed()
            rid    = round_id()
            remain = round_remain()

            # ── ПРОВЕРКА ЛИМИТОВ ──────────────────────────
            if state["balance"] < MIN_BALANCE_USD:
                log.warning(f"⛔ Баланс ${state['balance']:.2f} < минимума ${MIN_BALANCE_USD}. Стоп.")
                break

            if state["daily_loss"] >= MAX_DAILY_LOSS_USD:
                log.warning(f"⛔ Дневной лимит потерь ${MAX_DAILY_LOSS_USD} исчерпан. Стоп до завтра.")
                await asyncio.sleep(300)
                continue

            # ── ПОИСК РЫНКА (обновляем каждые 4 минуты) ──
            if time.time() - last_market_ts > 240:
                last_market_id  = await find_btc5m_market()
                last_market_ts  = time.time()
                if last_market_id:
                    pm_sentiment = await get_polymarket_sentiment(last_market_id)
                    log.info(f"📊 Sentiment: UP={pm_sentiment['up']:.2f} DOWN={pm_sentiment['down']:.2f} Vol={pm_sentiment['volume']:.0f}")

            # ── РАЗМЕЩЕНИЕ СТАВКИ (первые 20 сек раунда) ──
            if remain > ROUND_SEC - 20 and placed_this_round != rid:
                klines = await get_klines()
                price  = await get_price()
                sig    = ai_signal(klines, pm_sentiment["up"], pm_sentiment["down"])

                log.info(f"\n{'='*55}")
                log.info(f"⏰ Раунд: {fmt_window(rid)}")
                log.info(f"₿  Цена:  ${price['price']:,.0f}  ({price['change']:+.2f}%)")
                log.info(f"🤖 Сигнал: {sig['reason']}")

                if sig["skip"]:
                    log.info("⏭️  Пропуск (слабый сигнал)")
                    placed_this_round = rid
                else:
                    log.info(f"🎯 Ставим {sig['direction']} ${BET_USD:.2f} | уверенность {sig['confidence']:.0f}%")
                    success = await place_real_bet(clob, last_market_id, sig["direction"], BET_USD)

                    if success:
                        state["balance"]    -= BET_USD
                        state["daily_loss"] += BET_USD  # учитываем как потенциальная потеря (скорректируем при выигрыше)
                        state["pending"] = {
                            "rid":        rid,
                            "direction":  sig["direction"],
                            "confidence": sig["confidence"],
                            "bet":        BET_USD,
                            "start_price":price["price"],
                            "market_id":  last_market_id,
                        }
                        placed_this_round = rid
                        save_state()
                        log.info(f"✅ Ставка принята. Баланс: ${state['balance']:.2f}")
                    else:
                        log.error("❌ Ставка не прошла")
                        placed_this_round = rid  # не повторяем в этом раунде

            # ── РАЗРЕШЕНИЕ СТАВКИ (последние 8 сек раунда) ──
            elif remain < 8 and state["pending"] and resolved_this_round != rid:
                bet   = state["pending"]
                price = await get_price()
                end_p = price["price"]
                up    = end_p >= bet["start_price"]
                won   = (bet["direction"] == "UP" and up) or (bet["direction"] == "DOWN" and not up)

                if won:
                    profit = round(bet["bet"] * WIN_MULT, 2)
                    state["balance"]    += bet["bet"] + profit
                    state["daily_loss"] -= bet["bet"]  # возвращаем — мы выиграли
                    state["pnl"]        += profit
                    state["wins"]       += 1
                    log.info(f"🏆 ВЫИГРЫШ! +${profit:.2f} | Баланс: ${state['balance']:.2f}")
                else:
                    state["pnl"]     -= bet["bet"]
                    state["losses"]  += 1
                    log.info(f"💸 Проигрыш -${bet['bet']:.2f} | Баланс: ${state['balance']:.2f}")

                log.info(f"📊 Статистика: побед={state['wins']} | поражений={state['losses']} | PnL=${state['pnl']:.2f}")
                state["pending"]       = None
                resolved_this_round    = rid
                save_state()

            await asyncio.sleep(TICK_SEC)

        except KeyboardInterrupt:
            log.info("🛑 Бот остановлен вручную")
            save_state()
            break
        except Exception as e:
            log.error(f"Ошибка в основном цикле: {e}")
            await asyncio.sleep(10)

if __name__ == "__main__":
    asyncio.run(bot_loop())
