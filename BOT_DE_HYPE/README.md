# BOT_DE_HYPE

Repositorio separado y enfocado **exclusivamente** en la estrategia solicitada para `HYPEUSDT` en Binance Futures.

## Estrategia implementada

1. Descarga las últimas **1000 velas de 1m** de `HYPEUSDT`.
2. Calcula **EMA 8**.
3. Evalúa señal de entrada:
   - Volumen de la última vela cerrada (`1m`) **> 70,000**.
   - Precio actual **< EMA 8**.
4. Si hay señal, abre **LONG inicial de 10 USDT**.
5. Monitorea la operación por **WebSocket (`markPrice@1s`)**.
6. Cierre por objetivo:
   - Cuando el **ROI agregado de la posición abierta >= +1%**, cierra todo.
7. Reentrada escalonada (máximo 3 capas):
   - Si ROI agregado **<= -1.6%** **y** aparece otra señal igual, añade:
     - 2ª entrada: **10 USDT** (total 20)
     - 3ª entrada: **20 USDT** (total 40)
8. Tras cada reentrada, el bot recalcula promedio de entrada y usa el TP de **+1% sobre ROI agregado**.

---

## Estructura

- `bot_hype.py`: bot principal de estrategia.

## Variables de entorno

- `DRY_RUN` (default `true`):
  - `true`: simula órdenes.
  - `false`: envía órdenes reales a Binance Futures.
- `BINANCE_API_KEY` y `BINANCE_API_SECRET`: requeridas si `DRY_RUN=false`.
- `SYMBOL` (default `HYPEUSDT`)
- `VOLUME_THRESHOLD` (default `70000`)
- `EMA_PERIOD` (default `8`)
- `ROI_TP` (default `1.0`)
- `ROI_REENTRY` (default `-1.6`)
- `LEVERAGE` (default `1`)

## Uso

```bash
pip install -r requirements.txt
python bot_hype.py
```

> Recomendado: arrancar primero en `DRY_RUN=true` para validar comportamiento.
