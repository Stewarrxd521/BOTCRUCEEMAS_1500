"""
EMA Crossover Bot — Binance USDT → Telegram
Sin dependencias compiladas (sin pandas, sin numpy)
Compatible con cualquier versión de Python 3.8+
"""
import asyncio
import aiohttp
from aiohttp import web
import logging
from datetime import datetime
import os

# ══════════════════════════════════════════════
#  CONFIGURACIÓN
# ══════════════════════════════════════════════
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "TU_TOKEN_AQUI")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID",   "TU_CHAT_ID_AQUI")

EMA_FAST  = int(os.environ.get("EMA_FAST",  "8"))
EMA_MID   = int(os.environ.get("EMA_MID",  "21"))
EMA_SLOW  = int(os.environ.get("EMA_SLOW", "55"))

PORT           = int(os.environ.get("PORT", "10000"))
INTERVAL       = "1m"
CANDLES_NEEDED = max(EMA_SLOW * 3, 150)
QUOTE_ASSET    = "USDT"
BINANCE_BASE   = "https://api.binance.com"
SCAN_INTERVAL  = 60
MAX_CONCURRENT = 15

# ══════════════════════════════════════════════
#  ESTADO GLOBAL
# ══════════════════════════════════════════════
bot_status = {
    "last_scan": "Iniciando...",
    "total_alerts": 0,
    "symbols_monitored": 0,
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("EMA-Bot")


# ══════════════════════════════════════════════
#  EMA — cálculo puro en Python (sin pandas/numpy)
# ══════════════════════════════════════════════
def calc_ema(closes: list, period: int) -> list:
    """Calcula EMA de una lista de precios."""
    if len(closes) < period:
        return []
    k = 2.0 / (period + 1)
    ema = [sum(closes[:period]) / period]   # primera EMA = SMA
    for price in closes[period:]:
        ema.append(price * k + ema[-1] * (1 - k))
    return ema


def detect_crossover(closes: list) -> str | None:
    """
    BULLISH → fast > mid > slow en vela actual, NO era así en vela anterior
    BEARISH → fast < mid < slow en vela actual, NO era así en vela anterior
    """
    if len(closes) < EMA_SLOW + 10:
        return None

    fast = calc_ema(closes, EMA_FAST)
    mid  = calc_ema(closes, EMA_MID)
    slow = calc_ema(closes, EMA_SLOW)

    if len(fast) < 2 or len(mid) < 2 or len(slow) < 2:
        return None

    # Alinear listas al mismo tamaño por la cola
    n = min(len(fast), len(mid), len(slow))
    fast = fast[-n:]
    mid  = mid[-n:]
    slow = slow[-n:]

    cf, cm, cs = fast[-1], mid[-1], slow[-1]
    pf, pm, ps = fast[-2], mid[-2], slow[-2]

    bullish_now  = cf > cm > cs
    bearish_now  = cf < cm < cs
    bullish_prev = pf > pm > ps
    bearish_prev = pf < pm < ps

    if bullish_now and not bullish_prev:
        return "BULLISH"
    if bearish_now and not bearish_prev:
        return "BEARISH"
    return None


# ══════════════════════════════════════════════
#  SERVIDOR HTTP (para que Render no duerma el bot)
# ══════════════════════════════════════════════
async def health_handler(request):
    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>EMA Bot</title>
  <style>
    body{{font-family:monospace;background:#0d1117;color:#58a6ff;padding:2rem}}
    h1{{color:#3fb950}} .ok{{color:#3fb950}} .tag{{color:#d29922}} span{{color:#f0f6fc}}
  </style>
</head>
<body>
  <h1>🤖 EMA Crossover Bot — ACTIVO</h1>
  <p>Estado: <span class="ok">✅ Corriendo</span></p>
  <p>EMAs: <span>EMA{EMA_FAST} / EMA{EMA_MID} / EMA{EMA_SLOW}</span></p>
  <p>Símbolos monitoreados: <span>{bot_status['symbols_monitored']}</span></p>
  <p>Último escaneo: <span>{bot_status['last_scan']}</span></p>
  <p>Total alertas enviadas: <span class="tag">{bot_status['total_alerts']}</span></p>
  <hr><p style="color:#8b949e">Timeframe: 1m | Mercado: Binance USDT | Python puro ✅</p>
</body>
</html>"""
    return web.Response(text=html, content_type="text/html")


async def start_http_server():
    app = web.Application()
    app.router.add_get("/", health_handler)
    app.router.add_get("/health", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info(f"Servidor HTTP activo en puerto {PORT}")


# ══════════════════════════════════════════════
#  BINANCE API
# ══════════════════════════════════════════════
async def get_usdt_symbols(session: aiohttp.ClientSession) -> list:
    url = f"{BINANCE_BASE}/api/v3/exchangeInfo"
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
        data = await resp.json()
    symbols = [
        s["symbol"] for s in data["symbols"]
        if s["quoteAsset"] == QUOTE_ASSET
        and s["status"] == "TRADING"
        and s["isSpotTradingAllowed"]
    ]
    log.info(f"Símbolos USDT activos: {len(symbols)}")
    return symbols


async def get_klines(session: aiohttp.ClientSession, symbol: str) -> list:
    url = f"{BINANCE_BASE}/api/v3/klines"
    params = {"symbol": symbol, "interval": INTERVAL, "limit": CANDLES_NEEDED}
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return []
            return await resp.json()
    except Exception:
        return []


# ══════════════════════════════════════════════
#  TELEGRAM
# ══════════════════════════════════════════════
async def send_telegram(session: aiohttp.ClientSession, message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                log.error(f"Telegram error {resp.status}: {await resp.text()}")
    except Exception as e:
        log.error(f"Error Telegram: {e}")


def build_message(symbol, signal, price, change, fv, mv, sv) -> str:
    emoji = "🟢" if signal == "BULLISH" else "🔴"
    word  = "ALCISTA ▲" if signal == "BULLISH" else "BAJISTA ▼"
    return (
        f"{emoji} <b>CRUCE 3 EMAs — {word}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>Par:</b>        <code>{symbol}</code>\n"
        f"💰 <b>Precio:</b>    <code>${price:,.6f}</code>\n"
        f"📉 <b>Cambio 1m:</b> <code>{change:+.2f}%</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📈 EMA {EMA_FAST}:   <code>{fv:.6f}</code>\n"
        f"📈 EMA {EMA_MID}:  <code>{mv:.6f}</code>\n"
        f"📈 EMA {EMA_SLOW}: <code>{sv:.6f}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏱ Timeframe: <b>1 Minuto</b>\n"
        f"🕐 {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
    )


# ══════════════════════════════════════════════
#  PROCESAMIENTO POR SÍMBOLO
# ══════════════════════════════════════════════
async def process_symbol(session, symbol, semaphore):
    async with semaphore:
        klines = await get_klines(session, symbol)
        if len(klines) < EMA_SLOW + 10:
            return None

        closes = [float(k[4]) for k in klines]
        signal = detect_crossover(closes)

        if signal:
            fast_list = calc_ema(closes, EMA_FAST)
            mid_list  = calc_ema(closes, EMA_MID)
            slow_list = calc_ema(closes, EMA_SLOW)
            price  = closes[-1]
            change = ((closes[-1] - closes[-2]) / closes[-2]) * 100
            return symbol, signal, price, change, fast_list[-1], mid_list[-1], slow_list[-1]

        return None


# ══════════════════════════════════════════════
#  ESCANEO COMPLETO
# ══════════════════════════════════════════════
async def run_scan(session, symbols):
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    log.info(f"Escaneando {len(symbols)} símbolos...")
    tasks   = [process_symbol(session, sym, semaphore) for sym in symbols]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    alerts  = [r for r in results if r and not isinstance(r, Exception)]
    log.info(f"Cruces detectados: {len(alerts)}")

    for item in alerts:
        symbol, signal, price, change, fv, mv, sv = item
        msg = build_message(symbol, signal, price, change, fv, mv, sv)
        await send_telegram(session, msg)
        bot_status["total_alerts"] += 1
        log.info(f"  → Alerta: {symbol} {signal}")
        await asyncio.sleep(0.3)


# ══════════════════════════════════════════════
#  LOOP PRINCIPAL
# ══════════════════════════════════════════════
async def bot_loop():
    async with aiohttp.ClientSession() as session:
        await send_telegram(session,
            "🤖 <b>Bot EMA Crossover iniciado</b>\n"
            f"Monitoreando todos los pares USDT de Binance\n"
            f"EMAs: <b>{EMA_FAST} / {EMA_MID} / {EMA_SLOW}</b> — Timeframe: <b>1m</b>"
        )
        symbols = await get_usdt_symbols(session)
        bot_status["symbols_monitored"] = len(symbols)

        while True:
            start = asyncio.get_event_loop().time()
            try:
                await run_scan(session, symbols)
                bot_status["last_scan"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
            except Exception as e:
                log.error(f"Error en escaneo: {e}")

            elapsed = asyncio.get_event_loop().time() - start
            wait    = max(0, SCAN_INTERVAL - elapsed)
            log.info(f"Completado en {elapsed:.1f}s. Próximo en {wait:.1f}s\n")
            await asyncio.sleep(wait)


# ══════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════
async def main():
    log.info("╔══════════════════════════════════════╗")
    log.info("║  EMA Bot — Sin dependencias pesadas  ║")
    log.info(f"║  EMAs: {EMA_FAST}/{EMA_MID}/{EMA_SLOW} | Timeframe: 1m         ║")
    log.info("╚══════════════════════════════════════╝")
    await asyncio.gather(
        start_http_server(),
        bot_loop(),
    )


if __name__ == "__main__":
    asyncio.run(main())
