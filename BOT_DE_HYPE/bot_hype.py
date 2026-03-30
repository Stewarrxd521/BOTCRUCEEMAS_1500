"""
BOT_DE_HYPE
Estrategia exclusiva solicitada:
- Descarga últimas 1000 velas 1m de HYPEUSDT (Binance Futures)
- Señal: volumen de vela > 70,000 y precio actual < EMA8
- Entrada LONG con TP por ROI agregado de +1%
- Si ROI agregado <= -1.6% y aparece una nueva señal, añade entradas DCA:
  1) 10 USDT, 2) 10 USDT, 3) 20 USDT (máximo 3 entradas, total 40 USDT)
- Monitoreo en tiempo real por WebSocket
"""

import asyncio
import hashlib
import hmac
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlencode

import aiohttp

BINANCE_REST = "https://fapi.binance.com"
BINANCE_WS = "wss://fstream.binance.com/ws"
SYMBOL = os.getenv("SYMBOL", "HYPEUSDT").upper()
INTERVAL = "1m"
KLINE_LIMIT = 1000
VOLUME_THRESHOLD = float(os.getenv("VOLUME_THRESHOLD", "70000"))
EMA_PERIOD = int(os.getenv("EMA_PERIOD", "8"))
ROI_TP = float(os.getenv("ROI_TP", "1.0"))
ROI_REENTRY = float(os.getenv("ROI_REENTRY", "-1.6"))
ENTRY_SIZES = [10.0, 10.0, 20.0]
POLL_SECONDS = 1.0

BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
LEVERAGE = int(os.getenv("LEVERAGE", "1"))


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("BOT_DE_HYPE")


@dataclass
class Layer:
    usdt_size: float
    entry_price: float
    quantity: float
    opened_at: float = field(default_factory=time.time)


@dataclass
class PositionState:
    layers: list[Layer] = field(default_factory=list)

    @property
    def is_open(self) -> bool:
        return len(self.layers) > 0

    @property
    def total_usdt(self) -> float:
        return sum(x.usdt_size for x in self.layers)

    @property
    def total_qty(self) -> float:
        return sum(x.quantity for x in self.layers)

    @property
    def avg_entry(self) -> float:
        if not self.layers:
            return 0.0
        return self.total_usdt / self.total_qty

    @property
    def next_layer_index(self) -> int:
        return len(self.layers)

    @property
    def can_add_layer(self) -> bool:
        return self.next_layer_index < len(ENTRY_SIZES)

    def roi_pct(self, mark_price: float) -> float:
        if not self.is_open:
            return 0.0
        pnl = (mark_price - self.avg_entry) * self.total_qty
        return (pnl / self.total_usdt) * 100.0


class BinanceFuturesClient:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    async def get_klines(self, symbol: str, interval: str, limit: int) -> list:
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        async with self.session.get(f"{BINANCE_REST}/fapi/v1/klines", params=params, timeout=20) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def get_price(self, symbol: str) -> float:
        params = {"symbol": symbol}
        async with self.session.get(f"{BINANCE_REST}/fapi/v1/ticker/price", params=params, timeout=20) as resp:
            resp.raise_for_status()
            data = await resp.json()
            return float(data["price"])

    async def ensure_leverage(self, symbol: str, leverage: int) -> None:
        if DRY_RUN:
            return
        await self._signed_post("/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage})

    async def market_buy(self, symbol: str, quantity: float) -> dict:
        if DRY_RUN:
            return {"status": "DRY_RUN", "symbol": symbol, "side": "BUY", "executedQty": f"{quantity:.8f}"}
        return await self._signed_post(
            "/fapi/v1/order",
            {"symbol": symbol, "side": "BUY", "type": "MARKET", "quantity": self._fmt_qty(quantity)},
        )

    async def market_sell(self, symbol: str, quantity: float) -> dict:
        if DRY_RUN:
            return {"status": "DRY_RUN", "symbol": symbol, "side": "SELL", "executedQty": f"{quantity:.8f}"}
        return await self._signed_post(
            "/fapi/v1/order",
            {"symbol": symbol, "side": "SELL", "type": "MARKET", "quantity": self._fmt_qty(quantity)},
        )

    async def _signed_post(self, path: str, params: dict) -> dict:
        if not BINANCE_API_KEY or not BINANCE_API_SECRET:
            raise RuntimeError("Faltan BINANCE_API_KEY / BINANCE_API_SECRET para operar en real")

        enriched = {**params, "timestamp": int(time.time() * 1000), "recvWindow": 10000}
        query = urlencode(enriched)
        signature = hmac.new(BINANCE_API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
        headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
        async with self.session.post(
            f"{BINANCE_REST}{path}", params={**enriched, "signature": signature}, headers=headers, timeout=20
        ) as resp:
            body = await resp.text()
            if resp.status >= 400:
                raise RuntimeError(f"Binance error {resp.status}: {body}")
            return await resp.json()

    @staticmethod
    def _fmt_qty(quantity: float) -> str:
        return f"{quantity:.3f}".rstrip("0").rstrip(".")


def ema(values: list[float], period: int) -> float:
    if len(values) < period:
        raise ValueError(f"Se requieren al menos {period} datos para calcular EMA")

    multiplier = 2 / (period + 1)
    current = sum(values[:period]) / period
    for val in values[period:]:
        current = (val - current) * multiplier + current
    return current


async def has_entry_signal(client: BinanceFuturesClient, symbol: str) -> tuple[bool, float, float, float]:
    klines = await client.get_klines(symbol=symbol, interval=INTERVAL, limit=KLINE_LIMIT)
    closes = [float(k[4]) for k in klines]

    latest_closed_volume = float(klines[-2][5])
    ema8 = ema(closes, EMA_PERIOD)
    current_price = await client.get_price(symbol)

    ok_volume = latest_closed_volume > VOLUME_THRESHOLD
    ok_price = current_price < ema8
    signal = ok_volume and ok_price

    return signal, latest_closed_volume, ema8, current_price


async def mark_price_listener(symbol: str, queue: asyncio.Queue):
    stream = f"{symbol.lower()}@markPrice@1s"
    url = f"{BINANCE_WS}/{stream}"

    while True:
        try:
            async with aiohttp.ClientSession() as ws_session:
                async with ws_session.ws_connect(url, heartbeat=20) as ws:
                    log.info("WS conectado: %s", stream)
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = msg.json()
                            if "p" in data:
                                await queue.put(float(data["p"]))
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
        except Exception as exc:
            log.warning("WS error: %s. Reconectando...", exc)
            await asyncio.sleep(2)


async def strategy_loop():
    position = PositionState()
    mark_queue: asyncio.Queue = asyncio.Queue(maxsize=10)

    listener_task = asyncio.create_task(mark_price_listener(SYMBOL, mark_queue))

    async with aiohttp.ClientSession() as session:
        client = BinanceFuturesClient(session)
        await client.ensure_leverage(SYMBOL, LEVERAGE)

        last_mark: Optional[float] = None

        while True:
            try:
                while not mark_queue.empty():
                    last_mark = await mark_queue.get()

                signal, vol, ema8, current = await has_entry_signal(client, SYMBOL)
                if last_mark is None:
                    last_mark = current

                if position.is_open:
                    roi = position.roi_pct(last_mark)
                    tp_price = position.avg_entry * (1 + ROI_TP / 100.0)
                    log.info(
                        "POSICION ABIERTA | capas=%d total=%.2fUSDT avg=%.6f mark=%.6f roi=%.3f%% tp=%.6f",
                        len(position.layers),
                        position.total_usdt,
                        position.avg_entry,
                        last_mark,
                        roi,
                        tp_price,
                    )

                    if roi >= ROI_TP:
                        await client.market_sell(SYMBOL, position.total_qty)
                        log.info("TP alcanzado (+%.2f%%). Cerrando TODO en %s", ROI_TP, SYMBOL)
                        position = PositionState()

                    elif roi <= ROI_REENTRY and signal and position.can_add_layer:
                        next_size = ENTRY_SIZES[position.next_layer_index]
                        qty = next_size / current
                        await client.market_buy(SYMBOL, qty)
                        position.layers.append(Layer(usdt_size=next_size, entry_price=current, quantity=qty))
                        log.info(
                            "REENTRADA #%d activada por ROI %.3f%% y nueva señal. +%.2f USDT @ %.6f",
                            len(position.layers),
                            roi,
                            next_size,
                            current,
                        )

                else:
                    if signal and position.can_add_layer:
                        size = ENTRY_SIZES[position.next_layer_index]
                        qty = size / current
                        await client.market_buy(SYMBOL, qty)
                        position.layers.append(Layer(usdt_size=size, entry_price=current, quantity=qty))
                        log.info(
                            "ENTRADA INICIAL LONG | vol=%.2f ema8=%.6f price=%.6f size=%.2fUSDT",
                            vol,
                            ema8,
                            current,
                            size,
                        )
                    else:
                        log.info(
                            "Sin entrada | signal=%s vol=%.2f(>%s) price=%.6f ema8=%.6f",
                            signal,
                            vol,
                            VOLUME_THRESHOLD,
                            current,
                            ema8,
                        )

                await asyncio.sleep(POLL_SECONDS)

            except Exception as exc:
                log.exception("Error en strategy_loop: %s", exc)
                await asyncio.sleep(2)

    listener_task.cancel()


if __name__ == "__main__":
    log.info(
        "Iniciando BOT_DE_HYPE | symbol=%s dry_run=%s roi_tp=%.2f%% roi_reentry=%.2f%%",
        SYMBOL,
        DRY_RUN,
        ROI_TP,
        ROI_REENTRY,
    )
    asyncio.run(strategy_loop())
