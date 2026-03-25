"""
EMA Strategy Bot — Binance USDT → Telegram + Paper Trading Simulator
══════════════════════════════════════════════════════════════════════
Estrategia:
  • EMAs: 35, 300, 1500 (2 días de velas de 1 minuto)
  • LONG : EMA35 y EMA300 < EMA1500  +  spread EMA1500-EMA300 >= X%
           Y EMA35 cruza HACIA ARRIBA la EMA300
  • SHORT: EMA35 y EMA300 > EMA1500  +  spread EMA1500-EMA300 >= X%
           Y EMA35 cruza HACIA ABAJO  la EMA300

PAPER TRADING (simulación):
  • Balance virtual configurable (default 1000 USDT)
  • Máximo 10 LONG + 10 SHORT simultáneos
  • Tamaño mínimo por operación: 5 USDT (estándar Binance Spot)
  • TP: +1% ROI | SL: -5% ROI por operación
  • Precio en tiempo real vía WebSocket de Binance (miniTicker)
  • Reconexión automática al cambiar el conjunto de símbolos activos
  • Dashboard HTML con posiciones abiertas/cerradas y estadísticas
"""
import asyncio
import aiohttp
from aiohttp import web
import logging
from datetime import datetime, timezone
import os
import time
import json
from dataclasses import dataclass
from typing import Optional

# ══════════════════════════════════════════════════════════
#  CONFIGURACIÓN  (variables de entorno en Render/Railway)
# ══════════════════════════════════════════════════════════
TELEGRAM_BOT_TOKEN = "8700613197:AAFu7KAP3_9joN8Jq76r3ZcKIZiGcUWzSc4"
TELEGRAM_CHAT_ID   = "1474510598"

# ── EMAs ───────────────────────────────────────────────────
EMA_FAST   = int(os.environ.get("EMA_FAST",   "35"))
EMA_MID    = int(os.environ.get("EMA_MID",    "300"))
EMA_SLOW   = int(os.environ.get("EMA_SLOW",   "1500"))

# Spread mínimo entre EMA1500 y EMA300 (en %) para señal válida
SPREAD_PCT = float(os.environ.get("SPREAD_PCT", "1.0"))

# Cuántos días de velas descargar (2 días = 2880 velas de 1m)
DAYS_BACK  = int(os.environ.get("DAYS_BACK", "2"))

# ── Paper Trading ──────────────────────────────────────────
INITIAL_BALANCE = float(os.environ.get("INITIAL_BALANCE", "1000.0"))
# Tamaño por operación — Binance Spot exige mínimo 5 USDT (usamos 5.1 por margen)
USDT_PER_TRADE  = max(float(os.environ.get("USDT_PER_TRADE", "50.0")), 5.1)
MAX_LONGS       = int(os.environ.get("MAX_LONGS",  "10"))   # máx posiciones LONG
MAX_SHORTS      = int(os.environ.get("MAX_SHORTS", "10"))   # máx posiciones SHORT
TP_PCT          = float(os.environ.get("TP_PCT", "1.0"))    # Take Profit: +1% ROI
SL_PCT          = float(os.environ.get("SL_PCT", "5.0"))    # Stop Loss:   -5% ROI

# ── Binance ────────────────────────────────────────────────
QUOTE_ASSET   = "USDT"
BINANCE_REST  = "https://api.binance.com"
BINANCE_WS    = "wss://stream.binance.com:9443"
INTERVAL      = "1m"
LIMIT_PER_REQ = 1000   # máximo de Binance por petición de klines

# ── Bot ────────────────────────────────────────────────────
PORT           = int(os.environ.get("PORT", "10000"))
SCAN_INTERVAL  = 60    # segundos entre escaneos EMA completos
MAX_CONCURRENT = 8     # peticiones HTTP simultáneas a Binance

# ══════════════════════════════════════════════════════════
#  ESTADO GLOBAL DEL BOT
# ══════════════════════════════════════════════════════════
bot_status = {
    "last_scan"         : "Iniciando...",
    "total_alerts"      : 0,
    "symbols_monitored" : 0,
    "last_alerts"       : [],   # últimas 10 señales EMA detectadas
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("EMA-Bot")


# ══════════════════════════════════════════════════════════
#  PAPER TRADING — Modelo de datos
# ══════════════════════════════════════════════════════════
@dataclass
class Trade:
    """Representa una operación simulada (paper trade)."""
    id            : int
    symbol        : str
    direction     : str         # "LONG" | "SHORT"
    entry_price   : float
    quantity      : float       # unidades del activo base compradas/vendidas
    usdt_size     : float       # USDT comprometidos en la operación
    open_time     : str
    tp_price      : float       # precio de Take Profit
    sl_price      : float       # precio de Stop Loss
    current_price : float = 0.0
    status        : str   = "OPEN"   # "OPEN" | "TP" | "SL"
    close_price   : float = 0.0
    close_time    : str   = ""
    pnl_usdt      : float = 0.0
    roi_pct       : float = 0.0


class TradeManager:
    """
    Gestiona el portafolio simulado.
    • Abre y cierra trades con asyncio.Lock para evitar condiciones de carrera.
    • update_price y trades_to_close son síncronos (sin I/O) — seguros en asyncio.
    """

    def __init__(self):
        self.balance  = INITIAL_BALANCE
        self.trades   : list[Trade] = []
        self._counter = 0
        self._lock    = asyncio.Lock()

    # ── Propiedades de consulta ──────────────────────────
    @property
    def open_trades(self) -> list[Trade]:
        return [t for t in self.trades if t.status == "OPEN"]

    @property
    def closed_trades(self) -> list[Trade]:
        return [t for t in self.trades if t.status != "OPEN"]

    @property
    def open_longs(self) -> list[Trade]:
        return [t for t in self.open_trades if t.direction == "LONG"]

    @property
    def open_shorts(self) -> list[Trade]:
        return [t for t in self.open_trades if t.direction == "SHORT"]

    @property
    def active_symbols(self) -> set:
        return {t.symbol for t in self.open_trades}

    @property
    def total_realized_pnl(self) -> float:
        return sum(t.pnl_usdt for t in self.closed_trades)

    @property
    def unrealized_pnl(self) -> float:
        return sum(t.pnl_usdt for t in self.open_trades)

    @property
    def equity(self) -> float:
        """Balance disponible + PnL no realizado de posiciones abiertas."""
        return self.balance + sum(t.usdt_size + t.pnl_usdt for t in self.open_trades)

    # ── Abrir operación ──────────────────────────────────
    async def open_trade(self, symbol: str, direction: str, price: float) -> Optional[Trade]:
        """
        Intenta abrir un trade. Retorna el Trade o None si no se cumplen
        las condiciones (límites, balance insuficiente, símbolo ya activo).
        """
        async with self._lock:
            # Validaciones
            if symbol in self.active_symbols:
                return None                          # ya hay posición en este par
            if direction == "LONG" and len(self.open_longs) >= MAX_LONGS:
                return None                          # máx LONGs alcanzado
            if direction == "SHORT" and len(self.open_shorts) >= MAX_SHORTS:
                return None                          # máx SHORTs alcanzado
            if self.balance < USDT_PER_TRADE:
                return None                          # balance insuficiente

            # Calcular precios TP/SL
            if direction == "LONG":
                tp_price = price * (1 + TP_PCT / 100)
                sl_price = price * (1 - SL_PCT / 100)
            else:
                tp_price = price * (1 - TP_PCT / 100)
                sl_price = price * (1 + SL_PCT / 100)

            self._counter += 1
            trade = Trade(
                id            = self._counter,
                symbol        = symbol,
                direction     = direction,
                entry_price   = price,
                quantity      = USDT_PER_TRADE / price,  # cantidad en activo base
                usdt_size     = USDT_PER_TRADE,
                open_time     = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                tp_price      = tp_price,
                sl_price      = sl_price,
                current_price = price,
            )

            self.balance -= USDT_PER_TRADE
            self.trades.append(trade)
            log.info(
                f"[TRADE #{trade.id}] ABIERTO {direction} {symbol} "
                f"@ ${price:.8f} | TP: ${tp_price:.8f} | SL: ${sl_price:.8f} "
                f"| {USDT_PER_TRADE:.2f} USDT → {trade.quantity:.6f} {symbol.replace('USDT', '')}"
            )
            return trade

    # ── Cerrar operación ─────────────────────────────────
    async def close_trade(self, trade: Trade, close_price: float, reason: str) -> bool:
        """
        Cierra un trade. reason = "TP" | "SL".
        Retorna True si se cerró efectivamente, False si ya estaba cerrado.
        """
        async with self._lock:
            if trade.status != "OPEN":
                return False   # ya cerrado (posible doble disparo del WS)

            trade.status      = reason
            trade.close_price = close_price
            trade.close_time  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

            # PnL según dirección
            if trade.direction == "LONG":
                trade.pnl_usdt = (close_price - trade.entry_price) * trade.quantity
            else:
                trade.pnl_usdt = (trade.entry_price - close_price) * trade.quantity

            trade.roi_pct = (trade.pnl_usdt / trade.usdt_size) * 100
            self.balance  += trade.usdt_size + trade.pnl_usdt   # devolver capital + PnL

            log.info(
                f"[TRADE #{trade.id}] CERRADO {reason} {trade.symbol} "
                f"@ ${close_price:.8f} | PnL: {trade.pnl_usdt:+.4f} USDT ({trade.roi_pct:+.2f}%)"
            )
            return True

    # ── Actualizar precio en tiempo real (síncrono) ──────
    def update_price(self, symbol: str, price: float):
        """Actualiza el precio actual y PnL no realizado para todas las posiciones del símbolo."""
        for t in self.open_trades:
            if t.symbol == symbol:
                t.current_price = price
                if t.direction == "LONG":
                    t.pnl_usdt = (price - t.entry_price) * t.quantity
                else:
                    t.pnl_usdt = (t.entry_price - price) * t.quantity
                t.roi_pct = (t.pnl_usdt / t.usdt_size) * 100

    # ── Verificar si algún trade alcanzó TP o SL ────────
    def trades_to_close(self, symbol: str, price: float) -> list[tuple]:
        """Retorna lista de (Trade, reason) que deben cerrarse al precio dado."""
        result = []
        for t in self.open_trades:
            if t.symbol != symbol:
                continue
            if t.direction == "LONG":
                if price >= t.tp_price:
                    result.append((t, "TP"))
                elif price <= t.sl_price:
                    result.append((t, "SL"))
            else:  # SHORT
                if price <= t.tp_price:
                    result.append((t, "TP"))
                elif price >= t.sl_price:
                    result.append((t, "SL"))
        return result


# Instancia global del gestor de trades
trade_manager = TradeManager()


# ══════════════════════════════════════════════════════════
#  EMA — cálculo puro en Python (sin pandas/numpy)
# ══════════════════════════════════════════════════════════
def calc_ema(closes: list, period: int) -> list:
    """Calcula EMA completa. Retorna lista del mismo largo (None al inicio)."""
    if len(closes) < period:
        return []
    k   = 2.0 / (period + 1)
    ema = [None] * (period - 1)
    ema.append(sum(closes[:period]) / period)   # semilla = SMA del primer periodo
    for price in closes[period:]:
        ema.append(price * k + ema[-1] * (1 - k))
    return ema


def detect_signal(closes: list) -> dict | None:
    """
    Detecta señal LONG o SHORT según la estrategia EMA triple.

    LONG  → EMA35[-1] y EMA300[-1] < EMA1500[-1]
             spread(EMA1500, EMA300) >= SPREAD_PCT
             EMA35 cruzó ARRIBA EMA300 (prev < , actual > )

    SHORT → EMA35[-1] y EMA300[-1] > EMA1500[-1]
             spread(EMA1500, EMA300) >= SPREAD_PCT
             EMA35 cruzó ABAJO  EMA300 (prev > , actual < )
    """
    if len(closes) < EMA_SLOW + 5:
        return None

    fast_list = calc_ema(closes, EMA_FAST)
    mid_list  = calc_ema(closes, EMA_MID)
    slow_list = calc_ema(closes, EMA_SLOW)

    if (not fast_list or not mid_list or not slow_list
            or fast_list[-1] is None or mid_list[-1] is None or slow_list[-1] is None
            or fast_list[-2] is None or mid_list[-2] is None):
        return None

    cf, pf = fast_list[-1], fast_list[-2]   # EMA35  actual / anterior
    cm, pm = mid_list[-1],  mid_list[-2]    # EMA300 actual / anterior
    cs     = slow_list[-1]                  # EMA1500 actual

    spread = abs(cs - cm) / cs * 100
    if spread < SPREAD_PCT:
        return None

    # LONG: ambas EMAs rápidas bajo EMA lenta + cruce alcista de EMA35 sobre EMA300
    if cf < cs and cm < cs and pf < pm and cf > cm:
        return {"signal": "LONG",  "cf": cf, "cm": cm, "cs": cs, "spread": spread}

    # SHORT: ambas EMAs rápidas sobre EMA lenta + cruce bajista de EMA35 bajo EMA300
    if cf > cs and cm > cs and pf > pm and cf < cm:
        return {"signal": "SHORT", "cf": cf, "cm": cm, "cs": cs, "spread": spread}

    return None


# ══════════════════════════════════════════════════════════
#  TELEGRAM — mensajes
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


def build_open_message(trade: Trade, ema_result: dict, change_1m: float) -> str:
    """Mensaje Telegram al abrir una posición."""
    emoji = "🟢" if trade.direction == "LONG" else "🔴"
    word  = "LONG  ▲" if trade.direction == "LONG" else "SHORT ▼"
    cond  = ("EMA35 y EMA300 BAJO EMA1500\nEMA35 cruzó ↑ EMA300"
             if trade.direction == "LONG" else
             "EMA35 y EMA300 SOBRE EMA1500\nEMA35 cruzó ↓ EMA300")
    base  = trade.symbol.replace("USDT", "")
    return (
        f"{emoji} <b>📂 POSICIÓN ABIERTA — {word}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>Par:</b>         <code>{trade.symbol}</code>\n"
        f"💰 <b>Entrada:</b>    <code>${trade.entry_price:,.8f}</code>\n"
        f"📉 <b>Cambio 1m:</b>  <code>{change_1m:+.2f}%</code>\n"
        f"📦 <b>Tamaño:</b>     <code>{USDT_PER_TRADE:.2f} USDT "
        f"({trade.quantity:.6f} {base})</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 <b>Take Profit:</b> <code>${trade.tp_price:,.8f}</code>  "
        f"<i>(+{TP_PCT}%)</i>\n"
        f"🛑 <b>Stop Loss:</b>   <code>${trade.sl_price:,.8f}</code>  "
        f"<i>(-{SL_PCT}%)</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📈 EMA{EMA_FAST}:    <code>{ema_result['cf']:.8f}</code>\n"
        f"📈 EMA{EMA_MID}:   <code>{ema_result['cm']:.8f}</code>\n"
        f"📈 EMA{EMA_SLOW}: <code>{ema_result['cs']:.8f}</code>\n"
        f"📐 <b>Spread EMA1500-EMA300:</b> <code>{ema_result['spread']:.2f}%</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📋 <i>{cond}</i>\n"
        f"💼 <b>Balance libre:</b> <code>{trade_manager.balance:.2f} USDT</code>\n"
        f"📊 <b>Posiciones:</b> "
        f"<code>{len(trade_manager.open_longs)}L / {len(trade_manager.open_shorts)}S</code>\n"
        f"🆔 Trade <b>#{trade.id}</b>  |  ⏱ {trade.open_time}"
    )


def build_signal_no_trade_message(symbol: str, result: dict, price: float) -> str:
    """Mensaje cuando hay señal EMA pero no se puede abrir trade (límite alcanzado)."""
    emoji = "🟢" if result["signal"] == "LONG" else "🔴"
    limit = MAX_LONGS if result["signal"] == "LONG" else MAX_SHORTS
    return (
        f"{emoji} <b>SEÑAL {result['signal']} — Sin trade</b>\n"
        f"📊 <code>{symbol}</code>  @ <code>${price:,.8f}</code>\n"
        f"📐 Spread: <code>{result['spread']:.2f}%</code>\n"
        f"⚠️ <i>Máx. posiciones {result['signal']} alcanzado ({limit})"
        f" o balance insuficiente</i>"
    )


def build_close_message(trade: Trade) -> str:
    """Mensaje Telegram al cerrar una posición (TP o SL)."""
    if trade.status == "TP":
        emoji, reason = "✅", "TAKE PROFIT 🎯"
    else:
        emoji, reason = "❌", "STOP LOSS 🛑"

    dir_str = "🟢 LONG" if trade.direction == "LONG" else "🔴 SHORT"
    pnl_emoji = "💚" if trade.pnl_usdt >= 0 else "❗"
    wins   = sum(1 for t in trade_manager.closed_trades if t.status == "TP")
    total  = len(trade_manager.closed_trades)
    wr_str = f"{wins}/{total} ({wins/total*100:.1f}%)" if total else "N/A"

    return (
        f"{emoji} <b>POSICIÓN CERRADA — {reason}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>Par:</b>      <code>{trade.symbol}</code>  {dir_str}\n"
        f"💵 <b>Entrada:</b> <code>${trade.entry_price:,.8f}</code>\n"
        f"💵 <b>Salida:</b>  <code>${trade.close_price:,.8f}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{pnl_emoji} <b>PnL:</b>    <code>{trade.pnl_usdt:+.4f} USDT</code>\n"
        f"📊 <b>ROI:</b>    <code>{trade.roi_pct:+.2f}%</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏱ Abierto:  {trade.open_time}\n"
        f"⏱ Cerrado:  {trade.close_time}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💼 <b>Balance:</b>  <code>{trade_manager.balance:.2f} USDT</code>\n"
        f"💼 <b>Equity:</b>   <code>{trade_manager.equity:.2f} USDT</code>\n"
        f"📈 <b>Win Rate:</b> <code>{wr_str}</code>\n"
        f"🆔 Trade <b>#{trade.id}</b>"
    )


# ══════════════════════════════════════════════════════════
#  WEBSOCKET — Precios en tiempo real (miniTicker de Binance)
# ══════════════════════════════════════════════════════════
async def ws_price_loop(session: aiohttp.ClientSession):
    """
    Mantiene una conexión WebSocket con Binance para recibir precios en
    tiempo real de todos los símbolos activos simultáneamente.

    • Usa el combined stream de Binance: /stream?streams=sym1@miniTicker/sym2@...
    • Reconecta automáticamente cada vez que el conjunto de símbolos cambia
      (apertura o cierre de posiciones) o si la conexión se cae.
    • Al recibir un precio, verifica TP/SL y cierra posiciones si corresponde.
    """
    log.info("WebSocket Price Manager — iniciado")
    last_symbols: frozenset = frozenset()
    reconnect_delay = 3

    while True:
        symbols = frozenset(trade_manager.active_symbols)

        # Sin posiciones activas: esperar
        if not symbols:
            if last_symbols:
                log.info("WS: Sin posiciones activas, cerrando conexión")
            last_symbols = symbols
            await asyncio.sleep(2)
            reconnect_delay = 3
            continue

        # Nueva conexión si los símbolos cambiaron
        if symbols != last_symbols:
            log.info(
                f"WS: Conectando con {len(symbols)} símbolo(s): "
                f"{', '.join(sorted(symbols))}"
            )

        streams = "/".join(f"{s.lower()}@miniTicker" for s in sorted(symbols))
        url     = f"{BINANCE_WS}/stream?streams={streams}"

        try:
            async with session.ws_connect(
                url,
                heartbeat=20,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as ws:
                last_symbols = symbols
                reconnect_delay = 3
                log.info("WS: Conectado correctamente ✅")

                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        try:
                            data   = json.loads(msg.data)
                            ticker = data.get("data", {})
                            sym    = ticker.get("s")           # símbolo, ej. "BTCUSDT"
                            price  = float(ticker.get("c") or 0)  # precio de cierre (actual)

                            if sym and price > 0:
                                # 1. Actualizar precio y PnL no realizado
                                trade_manager.update_price(sym, price)

                                # 2. Verificar si algún trade alcanzó TP o SL
                                for trade, reason in trade_manager.trades_to_close(sym, price):
                                    closed = await trade_manager.close_trade(trade, price, reason)
                                    if closed:
                                        await send_telegram(session, build_close_message(trade))

                        except (ValueError, KeyError, TypeError) as ex:
                            log.debug(f"WS parse skip: {ex}")

                    elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                        log.warning("WS: Conexión cerrada por Binance, reconectando...")
                        break

                    # Reconectar si los símbolos activos cambiaron
                    new_sym = frozenset(trade_manager.active_symbols)
                    if new_sym != last_symbols:
                        log.info("WS: Símbolos activos cambiaron, reconectando...")
                        break

        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error(f"WS error: {e}")
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 30)


# ══════════════════════════════════════════════════════════
#  BINANCE REST — Símbolos y klines
# ══════════════════════════════════════════════════════════
async def get_usdt_symbols(session: aiohttp.ClientSession) -> list:
    """
    Obtiene todos los pares USDT activos de Binance Spot.
    Reintenta hasta 5 veces con espera exponencial ante errores de red,
    rate limit (429/418) o respuestas inesperadas.
    """
    url     = f"{BINANCE_REST}/api/v3/exchangeInfo"
    # Solo pedimos permisos de permisos de SPOT para reducir payload
    params  = {"permissions": "SPOT"}
    max_tries = 5

    for attempt in range(1, max_tries + 1):
        try:
            async with session.get(
                url, params=params, timeout=aiohttp.ClientTimeout(total=45)
            ) as resp:
                # Manejar rate-limit de Binance
                if resp.status in (429, 418):
                    retry_after = int(resp.headers.get("Retry-After", "10"))
                    log.warning(
                        f"get_usdt_symbols: rate limit HTTP {resp.status} "
                        f"— esperando {retry_after}s (intento {attempt}/{max_tries})"
                    )
                    await asyncio.sleep(retry_after)
                    continue

                if resp.status != 200:
                    body = await resp.text()
                    log.error(
                        f"get_usdt_symbols: HTTP {resp.status} "
                        f"(intento {attempt}/{max_tries}) — {body[:200]}"
                    )
                    await asyncio.sleep(5 * attempt)
                    continue

                data = await resp.json(content_type=None)   # acepta text/plain también

                # Validar que la respuesta tenga la estructura esperada
                if not isinstance(data, dict) or "symbols" not in data:
                    log.error(
                        f"get_usdt_symbols: respuesta inesperada de Binance "
                        f"(intento {attempt}/{max_tries}): {str(data)[:300]}"
                    )
                    await asyncio.sleep(5 * attempt)
                    continue

                symbols = [
                    s["symbol"]
                    for s in data["symbols"]
                    if s.get("quoteAsset") == QUOTE_ASSET
                    and s.get("status") == "TRADING"
                    and s.get("isSpotTradingAllowed", False)
                ]
                log.info(f"Símbolos USDT activos en Binance: {len(symbols)}")
                return symbols

        except asyncio.TimeoutError:
            log.warning(
                f"get_usdt_symbols: timeout (intento {attempt}/{max_tries})"
            )
        except Exception as e:
            log.error(
                f"get_usdt_symbols: error inesperado (intento {attempt}/{max_tries}): {e}"
            )

        await asyncio.sleep(5 * attempt)   # espera exponencial entre intentos

    # Si agotamos todos los intentos, abortamos con mensaje claro
    raise RuntimeError(
        "get_usdt_symbols: No se pudo obtener la lista de símbolos de Binance "
        f"tras {max_tries} intentos. Verifica la conectividad de red en Render."
    )


async def get_klines_multi(session: aiohttp.ClientSession, symbol: str) -> list:
    """
    Descarga DAYS_BACK días de velas de 1 minuto paginando de a 1000.
    Total velas: DAYS_BACK × 1440 (ej. 2 días = 2880 velas)
    """
    total_needed  = DAYS_BACK * 1440
    now_ms        = int(time.time() * 1000)
    start_ms      = now_ms - (DAYS_BACK * 24 * 60 * 60 * 1000)
    all_klines    = []
    current_start = start_ms
    url           = f"{BINANCE_REST}/api/v3/klines"

    while current_start < now_ms and len(all_klines) < total_needed:
        params = {
            "symbol"    : symbol,
            "interval"  : INTERVAL,
            "startTime" : current_start,
            "limit"     : LIMIT_PER_REQ,
        }
        try:
            async with session.get(
                url, params=params, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status == 429:
                    log.warning(f"Rate limit Binance ({symbol}), esperando 5s...")
                    await asyncio.sleep(5)
                    continue
                if resp.status != 200:
                    break
                batch = await resp.json()
                if not batch:
                    break
                all_klines.extend(batch)
                current_start = int(batch[-1][0]) + 60_000   # siguiente minuto en ms
                if len(batch) < LIMIT_PER_REQ:
                    break
        except Exception:
            break

    return all_klines


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
            price     = closes[-1]
            change_1m = ((closes[-1] - closes[-2]) / closes[-2]) * 100
            return symbol, result, price, change_1m

        return None


# ══════════════════════════════════════════════════════════
#  ESCANEO COMPLETO DE TODOS LOS SÍMBOLOS
# ══════════════════════════════════════════════════════════
async def run_scan(session, symbols):
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    log.info(
        f"Iniciando escaneo: {len(symbols)} símbolos | "
        f"EMAs {EMA_FAST}/{EMA_MID}/{EMA_SLOW} | spread ≥ {SPREAD_PCT}%"
    )

    tasks   = [process_symbol(session, sym, semaphore) for sym in symbols]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    signals = [r for r in results if r and not isinstance(r, Exception)]

    log.info(f"Señales detectadas esta ronda: {len(signals)}")

    for symbol, result, price, change_1m in signals:
        # Registrar señal en el dashboard (independientemente del trade)
        bot_status["total_alerts"] += 1
        bot_status["last_alerts"].append({
            "time"   : datetime.now(timezone.utc).strftime("%H:%M:%S"),
            "symbol" : symbol,
            "signal" : result["signal"],
            "price"  : f"{price:,.8f}",
            "spread" : result["spread"],
        })
        bot_status["last_alerts"] = bot_status["last_alerts"][-10:]

        # Intentar abrir paper trade
        trade = await trade_manager.open_trade(symbol, result["signal"], price)

        if trade:
            msg = build_open_message(trade, result, change_1m)
            log.info(
                f"  → {result['signal']} {symbol} | spread {result['spread']:.2f}% "
                f"| Trade #{trade.id}"
            )
        else:
            msg = build_signal_no_trade_message(symbol, result, price)
            log.info(
                f"  → {result['signal']} {symbol} | spread {result['spread']:.2f}% "
                f"| Sin trade (límite o balance)"
            )

        await send_telegram(session, msg)
        await asyncio.sleep(0.3)


# ══════════════════════════════════════════════════════════
#  DASHBOARD HTML
# ══════════════════════════════════════════════════════════


DASHBOARD_JS = r"""
<script>
  const fmtPrice = (v) => Number(v || 0).toLocaleString('en-US', { minimumFractionDigits: 8, maximumFractionDigits: 8 });
  const fmt4 = (v) => Number(v || 0).toLocaleString('en-US', { minimumFractionDigits: 4, maximumFractionDigits: 4 });
  const fmt2 = (v) => Number(v || 0).toFixed(2);

  function openRowsHTML(trades) {
    if (!trades || !trades.length) {
      return '<tr><td colspan="11" style="color:#8b949e;text-align:center;padding:.8rem">Sin posiciones abiertas</td></tr>';
    }
    return trades.map(t => {
      const dirColor = t.direction === 'LONG' ? '#3fb950' : '#f85149';
      const pnlColor = Number(t.pnl_usdt) >= 0 ? '#3fb950' : '#f85149';
      const current = Number(t.current_price || 0);
      const tp = Number(t.tp_price || 0);
      const sl = Number(t.sl_price || 0);
      const distTp = current ? Math.abs(tp - current) / current * 100 : 0;
      const distSl = current ? Math.abs(sl - current) / current * 100 : 0;
      return `
        <tr>
          <td>#${t.id}</td>
          <td><b>${t.symbol}</b></td>
          <td style="color:${dirColor}">${t.direction === 'LONG' ? '🟢 LONG' : '🔴 SHORT'}</td>
          <td>$${fmtPrice(t.entry_price)}</td>
          <td><b>$${fmtPrice(current)}</b></td>
          <td style="color:#3fb950">$${fmtPrice(tp)} <small>(${distTp.toFixed(2)}%)</small></td>
          <td style="color:#f85149">$${fmtPrice(sl)} <small>(${distSl.toFixed(2)}%)</small></td>
          <td style="color:${pnlColor};font-weight:bold">${Number(t.pnl_usdt || 0) >= 0 ? '+' : ''}${fmt4(t.pnl_usdt)}</td>
          <td style="color:${pnlColor};font-weight:bold">${Number(t.roi_pct || 0) >= 0 ? '+' : ''}${fmt2(t.roi_pct)}%</td>
          <td>${fmt2(t.usdt_size)}</td>
          <td style="font-size:.72rem">${t.open_time || ''}</td>
        </tr>`;
    }).join('');
  }

  function alertRowsHTML(alerts) {
    if (!alerts || !alerts.length) {
      return '<tr><td colspan="5" style="color:#8b949e;text-align:center;padding:.8rem">Sin señales aún...</td></tr>';
    }
    return alerts.map(a => {
      const color = a.signal === 'LONG' ? '#3fb950' : '#f85149';
      return `
        <tr style="color:${color}">
          <td>${a.time || ''}</td>
          <td><b>${a.symbol || ''}</b></td>
          <td>${a.signal === 'LONG' ? '🟢 LONG' : '🔴 SHORT'}</td>
          <td>$${a.price || ''}</td>
          <td>${Number(a.spread || 0).toFixed(2)}%</td>
        </tr>`;
    }).join('');
  }

  function closedRowsHTML(trades) {
    if (!trades || !trades.length) {
      return '<tr><td colspan="10" style="color:#8b949e;text-align:center;padding:.8rem">Sin operaciones cerradas aún</td></tr>';
    }
    return trades.map(t => {
      const pnlColor = Number(t.pnl_usdt) >= 0 ? '#3fb950' : '#f85149';
      const reason = t.status === 'TP' ? '✅ TP' : '❌ SL';
      return `
        <tr style="color:${pnlColor}">
          <td>#${t.id}</td>
          <td><b>${t.symbol}</b></td>
          <td>${t.direction === 'LONG' ? '🟢' : '🔴'} ${t.direction}</td>
          <td>$${fmtPrice(t.entry_price)}</td>
          <td>$${fmtPrice(t.close_price)}</td>
          <td><b>${Number(t.pnl_usdt || 0) >= 0 ? '+' : ''}${fmt4(t.pnl_usdt)}</b></td>
          <td><b>${Number(t.roi_pct || 0) >= 0 ? '+' : ''}${fmt2(t.roi_pct)}%</b></td>
          <td>${fmt2(t.usdt_size)}</td>
          <td>${reason}</td>
          <td style="font-size:.72rem">${t.close_time || ''}</td>
        </tr>`;
    }).join('');
  }

  async function refreshDashboard() {
    try {
      const res = await fetch('/api/state', { cache: 'no-store' });
      if (!res.ok) return;
      const d = await res.json();

      document.getElementById('balance_value').textContent = fmt2(d.balance) + ' USDT';
      document.getElementById('equity_value').textContent = fmt2(d.equity) + ' USDT';
      document.getElementById('rpnl_value').textContent = (Number(d.realized_pnl) >= 0 ? '+' : '') + fmt4(d.realized_pnl) + ' USDT';
      document.getElementById('upnl_value').textContent = (Number(d.unrealized_pnl) >= 0 ? '+' : '') + fmt4(d.unrealized_pnl) + ' USDT';
      document.getElementById('wr_value').textContent = d.win_rate === null ? 'N/A' : `${d.win_rate.toFixed(1)}% (${d.wins}✅ / ${d.losses}❌)`;
      document.getElementById('open_count_value').textContent = `${d.open_trades.length} / ${d.settings.max_longs + d.settings.max_shorts}`;
      document.getElementById('long_count_value').textContent = `${d.open_longs}L`;
      document.getElementById('short_count_value').textContent = `${d.open_shorts}S`;
      document.getElementById('alerts_value').textContent = d.total_alerts;
      document.getElementById('last_scan_value').textContent = d.last_scan || '';
      document.getElementById('ws_symbols_value').textContent = 'WS activo en: ' + (d.active_symbols.length ? d.active_symbols.join(', ') : 'Ninguno');

      document.getElementById('open_trades_body').innerHTML = openRowsHTML(d.open_trades);
      document.getElementById('alerts_body').innerHTML = alertRowsHTML(d.alerts);
      document.getElementById('closed_trades_body').innerHTML = closedRowsHTML(d.closed_trades);
    } catch (e) {
      console.error('Dashboard refresh failed', e);
    }
  }

  refreshDashboard();
  setInterval(refreshDashboard, 1000);
</script>
"""


def get_dashboard_state() -> dict:
    tm = trade_manager

    def serialize_trade(t: Trade) -> dict:
        return {
            "id": t.id,
            "symbol": t.symbol,
            "direction": t.direction,
            "entry_price": t.entry_price,
            "quantity": t.quantity,
            "usdt_size": t.usdt_size,
            "open_time": t.open_time,
            "tp_price": t.tp_price,
            "sl_price": t.sl_price,
            "current_price": t.current_price,
            "status": t.status,
            "close_price": t.close_price,
            "close_time": t.close_time,
            "pnl_usdt": t.pnl_usdt,
            "roi_pct": t.roi_pct,
        }

    closed_all = tm.closed_trades
    wins = sum(1 for t in closed_all if t.status == "TP")
    losses = sum(1 for t in closed_all if t.status == "SL")
    total_cl = len(closed_all)

    return {
        "balance": tm.balance,
        "equity": tm.equity,
        "realized_pnl": tm.total_realized_pnl,
        "unrealized_pnl": tm.unrealized_pnl,
        "wins": wins,
        "losses": losses,
        "win_rate": (wins / total_cl * 100.0) if total_cl else None,
        "total_alerts": bot_status["total_alerts"],
        "last_scan": bot_status["last_scan"],
        "symbols_monitored": bot_status["symbols_monitored"],
        "active_symbols": sorted(tm.active_symbols),
        "open_longs": len(tm.open_longs),
        "open_shorts": len(tm.open_shorts),
        "open_trades": [serialize_trade(t) for t in sorted(tm.open_trades, key=lambda x: x.id)],
        "closed_trades": [serialize_trade(t) for t in list(reversed(closed_all))[:20]],
        "alerts": list(reversed(bot_status["last_alerts"])),
        "settings": {
            "ema_fast": EMA_FAST,
            "ema_mid": EMA_MID,
            "ema_slow": EMA_SLOW,
            "spread_pct": SPREAD_PCT,
            "days_back": DAYS_BACK,
            "tp_pct": TP_PCT,
            "sl_pct": SL_PCT,
            "max_longs": MAX_LONGS,
            "max_shorts": MAX_SHORTS,
        },
    }


async def api_state_handler(request):
    return web.json_response(get_dashboard_state())

def build_dashboard() -> str:
    tm = trade_manager

    # ── Filas de posiciones abiertas ─────────────────────
    open_rows = ""
    for t in sorted(tm.open_trades, key=lambda x: x.id):
        dir_color = "#3fb950" if t.direction == "LONG" else "#f85149"
        pnl_color = "#3fb950" if t.pnl_usdt >= 0 else "#f85149"
        dist_tp   = abs(t.tp_price - t.current_price) / t.current_price * 100
        dist_sl   = abs(t.sl_price - t.current_price) / t.current_price * 100
        open_rows += (
            f"<tr>"
            f"<td>#{t.id}</td>"
            f"<td><b>{t.symbol}</b></td>"
            f'<td style="color:{dir_color}">{"🟢 LONG" if t.direction=="LONG" else "🔴 SHORT"}</td>'
            f"<td>${t.entry_price:,.8f}</td>"
            f"<td><b>${t.current_price:,.8f}</b></td>"
            f'<td style="color:#3fb950">${t.tp_price:,.8f} <small>({dist_tp:.2f}%)</small></td>'
            f'<td style="color:#f85149">${t.sl_price:,.8f} <small>({dist_sl:.2f}%)</small></td>'
            f'<td style="color:{pnl_color};font-weight:bold">{t.pnl_usdt:+.4f}</td>'
            f'<td style="color:{pnl_color};font-weight:bold">{t.roi_pct:+.2f}%</td>'
            f"<td>{t.usdt_size:.2f}</td>"
            f'<td style="font-size:.72rem">{t.open_time}</td>'
            f"</tr>"
        )

    # ── Filas de operaciones cerradas ─────────────────────
    closed_rows = ""
    for t in list(reversed(tm.closed_trades))[:20]:
        pnl_color = "#3fb950" if t.pnl_usdt >= 0 else "#f85149"
        reason    = "✅ TP" if t.status == "TP" else "❌ SL"
        closed_rows += (
            f'<tr style="color:{pnl_color}">'
            f"<td>#{t.id}</td>"
            f"<td><b>{t.symbol}</b></td>"
            f'<td>{"🟢" if t.direction=="LONG" else "🔴"} {t.direction}</td>'
            f"<td>${t.entry_price:,.8f}</td>"
            f"<td>${t.close_price:,.8f}</td>"
            f"<td><b>{t.pnl_usdt:+.4f}</b></td>"
            f"<td><b>{t.roi_pct:+.2f}%</b></td>"
            f"<td>{t.usdt_size:.2f}</td>"
            f"<td>{reason}</td>"
            f'<td style="font-size:.72rem">{t.close_time}</td>'
            f"</tr>"
        )

    # ── Señales EMA detectadas ────────────────────────────
    alerts_html = ""
    for a in reversed(bot_status["last_alerts"]):
        color = "#3fb950" if a["signal"] == "LONG" else "#f85149"
        alerts_html += (
            f'<tr style="color:{color}">'
            f"<td>{a['time']}</td><td><b>{a['symbol']}</b></td>"
            f'<td>{"🟢 LONG" if a["signal"]=="LONG" else "🔴 SHORT"}</td>'
            f"<td>${a['price']}</td><td>{a['spread']:.2f}%</td>"
            f"</tr>"
        )

    # ── Estadísticas ──────────────────────────────────────
    closed_all = tm.closed_trades
    wins       = sum(1 for t in closed_all if t.status == "TP")
    losses     = sum(1 for t in closed_all if t.status == "SL")
    total_cl   = len(closed_all)
    wr_str     = f"{wins/total_cl*100:.1f}%" if total_cl else "N/A"
    equity     = tm.equity
    rpnl       = tm.total_realized_pnl
    upnl       = tm.unrealized_pnl
    eq_color   = "#3fb950" if equity >= INITIAL_BALANCE else "#f85149"
    rpnl_color = "#3fb950" if rpnl >= 0 else "#f85149"
    upnl_color = "#3fb950" if upnl >= 0 else "#f85149"
    ws_syms    = ", ".join(sorted(tm.active_symbols)) if tm.active_symbols else "Ninguno"

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>EMA Strategy Bot — Paper Trading</title>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:'Courier New',monospace;background:#0d1117;color:#c9d1d9;padding:1.2rem}}
    h1{{color:#3fb950;margin-bottom:.8rem;font-size:1.3rem}}
    h2{{color:#58a6ff;margin:.9rem 0 .5rem;font-size:.95rem}}
    .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(155px,1fr));gap:.6rem;margin-bottom:1.2rem}}
    .card{{background:#161b22;border:1px solid #30363d;border-radius:6px;padding:.75rem}}
    .card .label{{color:#8b949e;font-size:.7rem;margin-bottom:.25rem;text-transform:uppercase;letter-spacing:.04em}}
    .card .value{{color:#f0f6fc;font-size:1rem;font-weight:bold}}
    .ok{{color:#3fb950}} .warn{{color:#d29922}} .err{{color:#f85149}}
    .wrap{{overflow-x:auto;margin-bottom:1.2rem}}
    table{{width:100%;border-collapse:collapse;font-size:.78rem;min-width:700px}}
    th{{color:#8b949e;text-align:left;padding:.35rem .45rem;border-bottom:1px solid #30363d;white-space:nowrap;font-size:.72rem}}
    td{{padding:.3rem .45rem;border-bottom:1px solid #1c2128;white-space:nowrap}}
    tr:hover td{{background:#161b22}}
    .tag{{display:inline-block;padding:.1rem .35rem;border-radius:3px;font-size:.7rem;font-weight:bold}}
    .ws-indicator{{display:inline-block;width:8px;height:8px;background:#3fb950;border-radius:50%;margin-right:5px;animation:blink 1.5s infinite}}
    @keyframes blink{{0%,100%{{opacity:1}}50%{{opacity:.3}}}}
  </style>
</head>
<body>
  <h1>🤖 EMA Strategy Bot — Paper Trading Activo</h1>

  <!-- ── BALANCE Y EQUITY ── -->
  <div class="grid">
    <div class="card">
      <div class="label">Balance libre</div>
      <div class="value ok" id="balance_value">{tm.balance:.2f} USDT</div>
    </div>
    <div class="card">
      <div class="label">Equity total</div>
      <div class="value" style="color:{eq_color}" id="equity_value">{equity:.2f} USDT</div>
    </div>
    <div class="card">
      <div class="label">PnL realizado</div>
      <div class="value" style="color:{rpnl_color}" id="rpnl_value">{rpnl:+.4f} USDT</div>
    </div>
    <div class="card">
      <div class="label">PnL no realizado</div>
      <div class="value" style="color:{upnl_color}" id="upnl_value">{upnl:+.4f} USDT</div>
    </div>
    <div class="card">
      <div class="label">Win Rate</div>
      <div class="value warn" id="wr_value">{wr_str} ({wins}✅ / {losses}❌)</div>
    </div>
    <div class="card">
      <div class="label">Posiciones abiertas</div>
      <div class="value" id="open_count_value">{len(tm.open_trades)} / {MAX_LONGS + MAX_SHORTS}</div>
    </div>
    <div class="card">
      <div class="label">LONG / SHORT activos</div>
      <div class="value">
        <span class="ok" id="long_count_value">{len(tm.open_longs)}L</span> /
        <span class="err" id="short_count_value">{len(tm.open_shorts)}S</span>
      </div>
    </div>
    <div class="card">
      <div class="label">Balance inicial</div>
      <div class="value" id="initial_balance_value">{INITIAL_BALANCE:.2f} USDT</div>
    </div>
    <div class="card">
      <div class="label">Por operación</div>
      <div class="value warn" id="trade_size_value">{USDT_PER_TRADE:.2f} USDT</div>
    </div>
    <div class="card">
      <div class="label">TP / SL</div>
      <div class="value">
        <span class="ok">+{TP_PCT}%</span> /
        <span class="err">-{SL_PCT}%</span>
      </div>
    </div>
    <div class="card">
      <div class="label">Señales EMA</div>
      <div class="value warn" id="alerts_value">{bot_status['total_alerts']}</div>
    </div>
    <div class="card">
      <div class="label">Último escaneo</div>
      <div class="value" style="font-size:.75rem" id="last_scan_value">{bot_status['last_scan']}</div>
    </div>
  </div>

  <!-- ── POSICIONES ABIERTAS ── -->
  <h2>
    <span class="ws-indicator"></span>
    📊 Posiciones Abiertas — precios en tiempo real (WebSocket Binance)
  </h2>
  <p id="ws_symbols_value" style="color:#484f58;font-size:.72rem;margin-bottom:.4rem">
    WS activo en: {ws_syms}
  </p>
  <div class="wrap">
    <table>
      <thead>
        <tr>
          <th>#</th><th>Par</th><th>Dir</th>
          <th>Entrada</th><th>Precio actual</th>
          <th>Take Profit</th><th>Stop Loss</th>
          <th>PnL (USDT)</th><th>ROI%</th>
          <th>Tamaño</th><th>Abierto</th>
        </tr>
      </thead>
      <tbody id="open_trades_body">
        {open_rows if open_rows else
          '<tr><td colspan="11" style="color:#8b949e;text-align:center;padding:.8rem">Sin posiciones abiertas</td></tr>'}
      </tbody>
    </table>
  </div>

  <!-- ── SEÑALES EMA ── -->
  <h2>📡 Señales EMA detectadas (últimas 10)</h2>
  <div class="wrap">
    <table>
      <thead>
        <tr><th>Hora UTC</th><th>Par</th><th>Señal</th><th>Precio</th><th>Spread</th></tr>
      </thead>
      <tbody id="alerts_body">
        {alerts_html if alerts_html else
          '<tr><td colspan="5" style="color:#8b949e;text-align:center;padding:.8rem">Sin señales aún...</td></tr>'}
      </tbody>
    </table>
  </div>

  <!-- ── OPERACIONES CERRADAS ── -->
  <h2>📋 Operaciones Cerradas (últimas 20)</h2>
  <div class="wrap">
    <table>
      <thead>
        <tr>
          <th>#</th><th>Par</th><th>Dir</th>
          <th>Entrada</th><th>Salida</th>
          <th>PnL (USDT)</th><th>ROI%</th>
          <th>Tamaño</th><th>Resultado</th><th>Cerrado</th>
        </tr>
      </thead>
      <tbody id="closed_trades_body">
        {closed_rows if closed_rows else
          '<tr><td colspan="10" style="color:#8b949e;text-align:center;padding:.8rem">Sin operaciones cerradas aún</td></tr>'}
      </tbody>
    </table>
  </div>

  <p style="color:#484f58;margin-top:.6rem;font-size:.7rem">
    Estrategia: EMA{EMA_FAST}/{EMA_MID}/{EMA_SLOW} | Spread ≥{SPREAD_PCT}% |
    Timeframe: 1m | Datos: {DAYS_BACK}d | Binance USDT Spot |
    Última actualización local: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}
  </p>

  {DASHBOARD_JS}
</body>
</html>"""


async def health_handler(request):
    return web.Response(text=build_dashboard(), content_type="text/html")


async def start_http_server():
    app = web.Application()
    app.router.add_get("/", health_handler)
    app.router.add_get("/health", health_handler)
    app.router.add_get("/api/state", api_state_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info(f"Dashboard activo en http://0.0.0.0:{PORT}")


# ══════════════════════════════════════════════════════════
#  BOT LOOP PRINCIPAL
# ══════════════════════════════════════════════════════════
async def bot_loop():
    async with aiohttp.ClientSession() as session:

        # Mensaje de inicio
        await send_telegram(session,
            f"🤖 <b>EMA Strategy Bot + Paper Trading — Iniciado</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📈 EMAs: <b>{EMA_FAST} / {EMA_MID} / {EMA_SLOW}</b>\n"
            f"📐 Spread mínimo: <b>{SPREAD_PCT}%</b>\n"
            f"📅 Datos: <b>{DAYS_BACK} días</b> de velas de 1m\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 Balance inicial: <b>{INITIAL_BALANCE:.2f} USDT</b>\n"
            f"📦 Por operación: <b>{USDT_PER_TRADE:.2f} USDT</b>  "
            f"<i>(≥5 USDT mínimo Binance)</i>\n"
            f"🎯 Take Profit: <b>+{TP_PCT}%</b> | 🛑 Stop Loss: <b>-{SL_PCT}%</b>\n"
            f"📊 Máx posiciones: <b>{MAX_LONGS} LONG + {MAX_SHORTS} SHORT</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🟢 LONG:  EMA{EMA_FAST} y EMA{EMA_MID} bajo EMA{EMA_SLOW} "
            f"+ EMA{EMA_FAST} cruza ↑ EMA{EMA_MID}\n"
            f"🔴 SHORT: EMA{EMA_FAST} y EMA{EMA_MID} sobre EMA{EMA_SLOW} "
            f"+ EMA{EMA_FAST} cruza ↓ EMA{EMA_MID}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔍 Monitoreando todos los pares USDT de Binance Spot"
        )

        # Iniciar WebSocket de precios como tarea paralela
        asyncio.create_task(ws_price_loop(session))

        # ── Obtener lista de símbolos con reintentos ──────────────
        symbols            = []
        SYMBOLS_REFRESH_S  = 6 * 3600   # refrescar lista cada 6 horas
        last_symbols_fetch = 0.0

        while not symbols:
            try:
                symbols = await get_usdt_symbols(session)
                bot_status["symbols_monitored"] = len(symbols)
                last_symbols_fetch = time.time()
            except RuntimeError as e:
                log.critical(str(e))
                log.info("Reintentando obtener símbolos en 30s...")
                await asyncio.sleep(30)

        # ── Bucle principal de escaneo EMA ────────────────────────
        while True:
            # Refrescar lista de símbolos cada 6 horas
            if time.time() - last_symbols_fetch > SYMBOLS_REFRESH_S:
                try:
                    symbols = await get_usdt_symbols(session)
                    bot_status["symbols_monitored"] = len(symbols)
                    last_symbols_fetch = time.time()
                    log.info("Lista de símbolos actualizada.")
                except RuntimeError as e:
                    log.error(f"No se pudo refrescar símbolos: {e} — usando lista anterior.")

            t0 = asyncio.get_event_loop().time()
            try:
                await run_scan(session, symbols)
                bot_status["last_scan"] = datetime.now(timezone.utc).strftime(
                    "%Y-%m-%d %H:%M:%S UTC"
                )
            except Exception as e:
                log.error(f"Error en escaneo: {e}")

            elapsed = asyncio.get_event_loop().time() - t0
            wait    = max(0.0, SCAN_INTERVAL - elapsed)
            log.info(
                f"Escaneo completado en {elapsed:.1f}s | "
                f"Posiciones: {len(trade_manager.open_longs)}L/{len(trade_manager.open_shorts)}S | "
                f"Balance: {trade_manager.balance:.2f} USDT | "
                f"Equity: {trade_manager.equity:.2f} USDT | "
                f"Próximo en {wait:.1f}s\n"
            )
            await asyncio.sleep(wait)


# ══════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════
async def main():
    log.info("╔══════════════════════════════════════════════════════╗")
    log.info("║   EMA Strategy Bot + Paper Trading — Binance USDT    ║")
    log.info(f"║   EMAs: {EMA_FAST}/{EMA_MID}/{EMA_SLOW} | TP:{TP_PCT}% SL:{SL_PCT}% | Balance: {INITIAL_BALANCE:.0f} USDT ║")
    log.info(f"║   Máx: {MAX_LONGS}L + {MAX_SHORTS}S | Tamaño/trade: {USDT_PER_TRADE:.1f} USDT         ║")
    log.info("╚══════════════════════════════════════════════════════╝")

    await asyncio.gather(
        start_http_server(),
        bot_loop(),
    )


if __name__ == "__main__":
    asyncio.run(main())
