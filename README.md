# Polymarket BTC 5-min Sniper
# Abg Mindset

Bot de trading simule pour les marches binaires BTC 5 minutes sur Polymarket.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install python-dotenv rich numpy websocket-client requests scipy py-clob-client
cp .env.example .env             # defaults projet
cp .env.example ~/.pulse-env     # tes cles operateur (pas commite)
```

Remplir `~/.pulse-env` avec tes cles (CLOB_API_KEY, TG_TOKEN, etc).

## Usage

```bash
# Simulation (aucun trade reel)
PYTHONPATH=src python -m pulse.main

# Score threshold ajustable
PYTHONPATH=src python -m pulse.main --score 0.45

# Collecteur de donnees Polymarket (pour backtest)
python collect.py --hours 24

# Test de direction (accuracy du scoring)
python direction_test.py --hours 168
```

## Architecture

```
src/pulse/
  config.py      # constantes, Settings, dataclasses
  feed.py        # BTC WebSocket Binance, RSI candles 1s
  strategy.py    # scoring, Kelly, direction vote
  orders.py      # CLOB client, order book, marches
  risk.py        # circuit breaker, crash recovery
  dashboard.py   # TUI Rich
  logger.py      # logging structure, CSV, Telegram
  main.py        # orchestration, scan loop, shutdown
```

## Securite

- **Mode dry par defaut** -- `--live` requis pour les vrais trades
- **PRIVATE_KEY vide = impossible de signer** -- double securite
- Les cles operateur vivent dans `~/.pulse-env`, jamais dans le repo
