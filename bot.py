"""
EMA Strategy Bot — Binance USDT → Telegram
──────────────────────────────────────────
Estrategia:
  • EMAs: 35, 300, 1500 (2 días de velas de 1 minuto)
  • LONG : EMA35 y EMA300 < EMA1500  +  spread EMA1500-EMA300 >= X%
           Y EMA35 cruza HACIA ARRIBA la EMA300
  • SHORT: EMA35 y EMA300 > EMA1500  +  spread EMA1500-EMA300 >= X%
           Y EMA35 cruza HACIA ABAJO  la EMA300
Sin dependencias compiladas — Python puro + aiohttp
"""
import asyncio
import aiohttp
from aiohttp import web
import logging
from datetime import datetime, timezone
import os
import time
 
# ══════════════════════════════════════════════════════════
#  CONFIGURACIÓN  (variables de entorno en Render)
# ══════════════════════════════════════════════════════════
TELEGRAM_BOT_TOKEN = "8700613197:AAFu7KAP3_9joN8Jq76r3ZcKIZiGcUWzSc4"
TELEGRAM_CHAT_ID   =  "1474510598"

 
# EMAs
EMA_FAST   = int(os.environ.get("EMA_FAST",   "35"))    # EMA rápida
EMA_MID    = int(os.environ.get("EMA_MID",    "300"))   # EMA media
EMA_SLOW   = int(os.environ.get("EMA_SLOW",   "1500"))  # EMA lenta (filtro de tendencia)
 
# Spread mínimo entre EMA1500 y EMA300 (en %) para considerar la señal válida
SPREAD_PCT = float(os.environ.get("SPREAD_PCT", "1.0"))
 
# Cuántos días de velas descargar (2 días = 2880 velas de 1m)
DAYS_BACK  = int(os.environ.get("DAYS_BACK", "2"))
 
# Binance
QUOTE_ASSET   = "USDT"
BINANCE_BASE  = "https://api.binance.com"
INTERVAL      = "1m"
LIMIT_PER_REQ = 1000   # máximo de Binance por petición
 
# Bot
PORT           = int(os.environ.get("PORT", "10000"))
SCAN_INTERVAL  = 60    # segundos entre escaneos completos
MAX_CONCURRENT = 8     # peticiones simultáneas (más cauteloso, 3 req por símbolo)
 
# ══════════════════════════════════════════════════════════
#  ESTADO GLOBAL
# ══════════════════════════════════════════════════════════
bot_status = {
    "last_scan"         : "Iniciando...",
    "total_alerts"      : 0,
    "symbols_monitored" : 0,
    "last_alerts"       : [],   # últimas 10 alertas para mostrar en el dashboard
}
 
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("EMA-Bot")
 
 
# ══════════════════════════════════════════════════════════
#  EMA — cálculo puro en Python (sin pandas/numpy)
# ══════════════════════════════════════════════════════════
def calc_ema(closes: list, period: int) -> list:
    """Calcula EMA completa. Retorna lista del mismo largo que closes (NaN al inicio)."""
    if len(closes) < period:
        return []
    k   = 2.0 / (period + 1)
    ema = [None] * (period - 1)
    ema.append(sum(closes[:period]) / period)   # semilla = SMA
    for price in closes[period:]:
        ema.append(price * k + ema[-1] * (1 - k))
    return ema
 
 
# ══════════════════════════════════════════════════════════
#  DETECCIÓN DE SEÑAL
# ══════════════════════════════════════════════════════════
def detect_signal(closes: list) -> dict | None:
    """
    Retorna dict con señal o None.
    Condiciones:
      LONG  → EMA35[-1] y EMA300[-1] < EMA1500[-1]
               spread(EMA1500, EMA300) >= SPREAD_PCT
               EMA35 cruzó ARRIBA EMA300 (prev < , actual >)
      SHORT → EMA35[-1] y EMA300[-1] > EMA1500[-1]
               spread(EMA1500, EMA300) >= SPREAD_PCT
               EMA35 cruzó ABAJO  EMA300 (prev > , actual <)
    """
    needed = EMA_SLOW + 5
    if len(closes) < needed:
        return None
 
    fast_list = calc_ema(closes, EMA_FAST)
    mid_list  = calc_ema(closes, EMA_MID)
    slow_list = calc_ema(closes, EMA_SLOW)
 
    # Verificar que tenemos suficientes valores calculados
    if (not fast_list or not mid_list or not slow_list
            or fast_list[-1] is None or mid_list[-1] is None or slow_list[-1] is None
            or fast_list[-2] is None or mid_list[-2] is None):
        return None
 
    cf  = fast_list[-1]    # EMA35 actual
    pf  = fast_list[-2]    # EMA35 anterior
    cm  = mid_list[-1]     # EMA300 actual
    pm  = mid_list[-2]     # EMA300 anterior
    cs  = slow_list[-1]    # EMA1500 actual
 
    # ── Spread entre EMA1500 y EMA300 ──────────────────────
    spread = abs(cs - cm) / cs * 100
    if spread < SPREAD_PCT:
        return None   # no hay suficiente alejamiento, ignorar
 
    # ── LONG ───────────────────────────────────────────────
    # EMA35 y EMA300 ambas por debajo de EMA1500
    # EMA35 cruza HACIA ARRIBA la EMA300 en esta vela
    if cf < cs and cm < cs:
        if pf < pm and cf > cm:   # cruce alcista de EMA35 sobre EMA300
            return {
                "signal" : "LONG",
                "cf": cf, "cm": cm, "cs": cs,
                "spread": spread,
            }
 
    # ── SHORT ──────────────────────────────────────────────
    # EMA35 y EMA300 ambas por encima de EMA1500
    # EMA35 cruza HACIA ABAJO la EMA300 en esta vela
    if cf > cs and cm > cs:
        if pf > pm and cf < cm:   # cruce bajista de EMA35 bajo EMA300
            return {
                "signal" : "SHORT",
                "cf": cf, "cm": cm, "cs": cs,
                "spread": spread,
            }
 
    return None
 
 
# ══════════════════════════════════════════════════════════
#  SERVIDOR HTTP — dashboard + health check
# ══════════════════════════════════════════════════════════
async def health_handler(request):
    alerts_html = ""
    for a in reversed(bot_status["last_alerts"]):
        color = "#3fb950" if a["signal"] == "LONG" else "#f85149"
        alerts_html += (
            f'<tr style="color:{color}">'
            f'<td>{a["time"]}</td><td><b>{a["symbol"]}</b></td>'
            f'<td>{"🟢 LONG" if a["signal"]=="LONG" else "🔴 SHORT"}</td>'
            f'<td>${a["price"]}</td><td>{a["spread"]:.2f}%</td>'
            f'</tr>'
        )
 
    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="30">
  <title>EMA Strategy Bot</title>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:monospace;background:#0d1117;color:#c9d1d9;padding:1.5rem}}
    h1{{color:#3fb950;margin-bottom:1rem;font-size:1.4rem}}
    .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:.8rem;margin-bottom:1.5rem}}
    .card{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:1rem}}
    .card .label{{color:#8b949e;font-size:.75rem;margin-bottom:.3rem}}
    .card .value{{color:#f0f6fc;font-size:1.1rem;font-weight:bold}}
    .ok{{color:#3fb950}} .warn{{color:#d29922}}
    table{{width:100%;border-collapse:collapse;font-size:.85rem}}
    th{{color:#8b949e;text-align:left;padding:.4rem .6rem;border-bottom:1px solid #30363d}}
    td{{padding:.4rem .6rem;border-bottom:1px solid #21262d}}
    h2{{color:#58a6ff;margin-bottom:.6rem;font-size:1rem}}
  </style>
</head>
<body>
  <h1>🤖 EMA Strategy Bot — ACTIVO</h1>
  <div class="grid">
    <div class="card">
      <div class="label">Estado</div>
      <div class="value ok">✅ Corriendo</div>
    </div>
    <div class="card">
      <div class="label">Estrategia EMAs</div>
      <div class="value">EMA{EMA_FAST} / EMA{EMA_MID} / EMA{EMA_SLOW}</div>
    </div>
    <div class="card">
      <div class="label">Spread mínimo</div>
      <div class="value warn">{SPREAD_PCT}%</div>
    </div>
    <div class="card">
      <div class="label">Símbolos monitoreados</div>
      <div class="value">{bot_status['symbols_monitored']}</div>
    </div>
    <div class="card">
      <div class="label">Último escaneo</div>
      <div class="value" style="font-size:.85rem">{bot_status['last_scan']}</div>
    </div>
    <div class="card">
      <div class="label">Total alertas enviadas</div>
      <div class="value warn">{bot_status['total_alerts']}</div>
    </div>
  </div>
 
  <h2>📋 Últimas señales</h2>
  <table>
    <thead>
      <tr>
        <th>Hora UTC</th><th>Par</th><th>Señal</th><th>Precio</th><th>Spread</th>
      </tr>
    </thead>
    <tbody>
      {alerts_html if alerts_html else '<tr><td colspan="5" style="color:#8b949e;text-align:center">Sin señales aún...</td></tr>'}
    </tbody>
  </table>
  <p style="color:#484f58;margin-top:1rem;font-size:.75rem">
    Timeframe: 1m | Velas: {DAYS_BACK} días | Binance USDT | Refresca cada 30s
  </p>
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
    log.info(f"Dashboard activo en puerto {PORT}")
 
 
# ══════════════════════════════════════════════════════════
#  BINANCE — obtener símbolos activos
# ══════════════════════════════════════════════════════════
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
 
 
# ══════════════════════════════════════════════════════════
#  BINANCE — descargar 2+ días de velas (múltiples peticiones)
# ══════════════════════════════════════════════════════════
async def get_klines_multi(session: aiohttp.ClientSession, symbol: str) -> list:
    """
    Descarga DAYS_BACK días de velas de 1 minuto.
    Binance permite max 1000 velas por request → paginamos hacia atrás.
    Total velas = DAYS_BACK * 1440 (ej: 2 días = 2880 velas)
    """
    total_needed  = DAYS_BACK * 1440
    now_ms        = int(time.time() * 1000)
    start_ms      = now_ms - (DAYS_BACK * 24 * 60 * 60 * 1000)
 
    all_klines = []
    current_start = start_ms
    url = f"{BINANCE_BASE}/api/v3/klines"
 
    while current_start < now_ms and len(all_klines) < total_needed:
        params = {
            "symbol"    : symbol,
            "interval"  : INTERVAL,
            "startTime" : current_start,
            "limit"     : LIMIT_PER_REQ,
        }
        try:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 429:
                    await asyncio.sleep(5)
                    continue
                if resp.status != 200:
                    break
                batch = await resp.json()
                if not batch:
                    break
                all_klines.extend(batch)
                # La siguiente petición empieza justo después de la última vela recibida
                current_start = int(batch[-1][0]) + 60_000   # +1 minuto en ms
                if len(batch) < LIMIT_PER_REQ:
                    break   # ya no hay más velas
        except Exception:
            break
 
    return all_klines
 
 
# ══════════════════════════════════════════════════════════
#  TELEGRAM
# ══════════════════════════════════════════════════════════
async def send_telegram(session: aiohttp.ClientSession, message: str):
    url     = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                log.error(f"Telegram error {resp.status}: {await resp.text()}")
    except Exception as e:
        log.error(f"Error Telegram: {e}")
 
 
def build_message(symbol: str, result: dict, price: float, change: float) -> str:
    signal  = result["signal"]
    emoji   = "🟢" if signal == "LONG" else "🔴"
    word    = "LONG  ▲" if signal == "LONG" else "SHORT ▼"
    cond    = ("EMA35 y EMA300 BAJO EMA1500\nEMA35 cruzó ↑ EMA300"
               if signal == "LONG" else
               "EMA35 y EMA300 SOBRE EMA1500\nEMA35 cruzó ↓ EMA300")
    return (
        f"{emoji} <b>SEÑAL {word}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>Par:</b>         <code>{symbol}</code>\n"
        f"💰 <b>Precio:</b>     <code>${price:,.6f}</code>\n"
        f"📉 <b>Cambio 1m:</b>  <code>{change:+.2f}%</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📈 EMA {EMA_FAST}:    <code>{result['cf']:.6f}</code>\n"
        f"📈 EMA {EMA_MID}:   <code>{result['cm']:.6f}</code>\n"
        f"📈 EMA {EMA_SLOW}: <code>{result['cs']:.6f}</code>\n"
        f"📐 <b>Spread EMA1500-EMA300:</b> <code>{result['spread']:.2f}%</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📋 <i>{cond}</i>\n"
        f"⏱ Timeframe: <b>1 Minuto</b>\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )
 
 
# ══════════════════════════════════════════════════════════
#  PROCESAMIENTO POR SÍMBOLO
# ══════════════════════════════════════════════════════════
async def process_symbol(session, symbol, semaphore):
    async with semaphore:
        klines = await get_klines_multi(session, symbol)
 
        if len(klines) < EMA_SLOW + 5:
            return None
 
        closes = [float(k[4]) for k in klines]
        result = detect_signal(closes)
 
        if result:
            price  = closes[-1]
            change = ((closes[-1] - closes[-2]) / closes[-2]) * 100
            return symbol, result, price, change
 
        return None
 
 
# ══════════════════════════════════════════════════════════
#  ESCANEO COMPLETO
# ══════════════════════════════════════════════════════════
async def run_scan(session, symbols):
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    log.info(f"Escaneando {len(symbols)} símbolos — EMAs {EMA_FAST}/{EMA_MID}/{EMA_SLOW} | spread ≥ {SPREAD_PCT}%")
 
    tasks   = [process_symbol(session, sym, semaphore) for sym in symbols]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    signals = [r for r in results if r and not isinstance(r, Exception)]
 
    log.info(f"Señales detectadas: {len(signals)}")
 
    for symbol, result, price, change in signals:
        msg = build_message(symbol, result, price, change)
        await send_telegram(session, msg)
        bot_status["total_alerts"] += 1
 
        # Guardar en historial del dashboard (máx 10)
        bot_status["last_alerts"].append({
            "time"   : datetime.now(timezone.utc).strftime("%H:%M:%S"),
            "symbol" : symbol,
            "signal" : result["signal"],
            "price"  : f"{price:,.6f}",
            "spread" : result["spread"],
        })
        bot_status["last_alerts"] = bot_status["last_alerts"][-10:]
 
        log.info(f"  → {result['signal']} {symbol} | spread {result['spread']:.2f}%")
        await asyncio.sleep(0.3)
 
 
# ══════════════════════════════════════════════════════════
#  BOT LOOP
# ══════════════════════════════════════════════════════════
async def bot_loop():
    async with aiohttp.ClientSession() as session:
        await send_telegram(session,
            "🤖 <b>Bot EMA Strategy iniciado</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📈 EMAs: <b>{EMA_FAST} / {EMA_MID} / {EMA_SLOW}</b>\n"
            f"📐 Spread mínimo: <b>{SPREAD_PCT}%</b>\n"
            f"📅 Datos: <b>{DAYS_BACK} días</b> de velas de 1m\n"
            f"🔍 Monitoreando todos los pares USDT de Binance\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🟢 LONG:  EMA{EMA_FAST} y EMA{EMA_MID} bajo EMA{EMA_SLOW} + EMA{EMA_FAST} cruza ↑ EMA{EMA_MID}\n"
            f"🔴 SHORT: EMA{EMA_FAST} y EMA{EMA_MID} sobre EMA{EMA_SLOW} + EMA{EMA_FAST} cruza ↓ EMA{EMA_MID}"
        )
 
        symbols = await get_usdt_symbols(session)
        bot_status["symbols_monitored"] = len(symbols)
 
        while True:
            start = asyncio.get_event_loop().time()
            try:
                await run_scan(session, symbols)
                bot_status["last_scan"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            except Exception as e:
                log.error(f"Error en escaneo: {e}")
 
            elapsed = asyncio.get_event_loop().time() - start
            wait    = max(0, SCAN_INTERVAL - elapsed)
            log.info(f"Escaneo completado en {elapsed:.1f}s | Próximo en {wait:.1f}s\n")
            await asyncio.sleep(wait)
 
 
# ══════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════
async def main():
    log.info("╔════════════════════════════════════════════╗")
    log.info("║   EMA Strategy Bot — Binance USDT          ║")
    log.info(f"║   EMAs: {EMA_FAST}/{EMA_MID}/{EMA_SLOW} | Spread: {SPREAD_PCT}% | {DAYS_BACK}d      ║")
    log.info("╚════════════════════════════════════════════╝")
    await asyncio.gather(
        start_http_server(),
        bot_loop(),
    )
 
 
if __name__ == "__main__":
    asyncio.run(main())
