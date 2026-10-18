# TheFundingHouse

Escáner de arbitraje de funding delta-neutral entre **Hyperliquid** y **Backpack**.
Para cada token que cotiza en ambos: long en el exchange con APR más bajo, short en el
más alto, y el spread anualizado es lo que cobras por mantener la posición.

## Arrancar

```bash
docker compose up -d --build
# o sin Docker:
pip install -r requirements.txt && python -m tfh
```

Abre http://localhost:8080. Los datos se guardan en un SQLite (`/data` en Docker, `./data` en local).

## Qué calcula

| Vista | Fuente | Significado |
|---|---|---|
| **Live** | Tasa de funding *prevista* ahora mismo (cada 60 s) | Lo que pagaría el próximo periodo |
| **1h … 31d** | Funding *liquidado* real descargado de cada exchange | Lo que habrías cobrado manteniendo la posición toda la ventana |

Por par: `spread_apr_percent`, `fee_round_trip_percent` (abrir + cerrar las dos patas),
`breakeven_hours` (horas de funding para cubrir comisiones), `net_apr_percent` (a un
horizonte configurable), `price_spread_percent`, OI en USD y cobertura real de datos.

## Configuración

Variables de entorno (ver `docker-compose.yml`): comisiones por exchange
(`FEE_<EXCHANGE>_TAKER/MAKER`), `NET_APR_HORIZON_HOURS`, cadencias de refresco y
`HISTORY_BACKFILL_DAYS`.

## Estructura

```
tfh/
  app.py         API FastAPI + frontend estático
  scheduler.py   tareas de fondo: live, snapshots, histórico
  analytics.py   pares, comisiones, breakeven, ventanas
  store.py       SQLite (funding_history, market_snapshots)
  adapters/      Hyperliquid y Backpack (API pública, REST)
frontend/        HTML + JS estático
```

## Hechos de las APIs que el código asume

- Funding horario en ambos exchanges.
- `openInterest` en unidades del activo base en ambos (se convierte a USD con el mark).
- Hyperliquid marca mercados retirados con `isDelisted`; Backpack con `orderBookState != "Open"`.
