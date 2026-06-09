# Crypto Arbitrage Scanner — Разработка с Claude Code

**Период:** 2026-05-12 — 2026-05-14
**Стартовый капитал:** $1000
**Статус:** Paper trading, 172 сделки, 100% win rate, +$38.10 PnL за 21.7ч

---

## Идея и постановка задачи

**Цель:** Бот, который отслеживает все монеты на всех биржах и находит лучшую разницу цен (арбитраж).

**Решение:** Спот-арбитраж между несколькими CEX биржами в реальном времени. Сначала сканер, потом авто-исполнение.

---

## Стек

- **Python 3.14**
- **CCXT 4.5** (унифицированный API к 100+ биржам)
- **ccxt.pro** (бесплатно с 2024) — WebSocket для real-time данных
- **asyncio** — параллельные запросы к биржам
- **rich** — терминальный UI с обновлением таблицы
- **python-dotenv** — конфиги вне кода

---

## Эволюция списка бирж

**Изначально планировалось:** Binance, Bybit, OKX + азиатские.

**Финальный список (6 бирж):** MEXC, Bitget, HTX, KuCoin, BitMart, BingX

**Почему убрали:**
- **Binance** — требует KYC для каждого действия (русские аккаунты)
- **Bybit** — банит русские аккаунты с конца 2024
- **OKX** — есть RU-ограничения, не стоит времени на ещё один ключ
- **Gate.io** — у пользователя не грузил сайт

---

## Архитектура (4 слоя)

```
┌─────────────────────────────────────────┐
│  Presentation: rich.Live таблица        │
└─────────────────▲───────────────────────┘
                  │
┌─────────────────┴───────────────────────┐
│  Business Logic                          │
│  - find_opportunities                    │
│  - antifake (contract verification)      │
│  - depth check (VWAP)                    │
│  - executor (paper/live)                 │
└─────────────────▲───────────────────────┘
                  │
┌─────────────────┴───────────────────────┐
│  Data Aggregation: TickerHub            │
│  WebSocket + REST → shared state        │
└─────────────────▲───────────────────────┘
                  │
┌─────────────────┴───────────────────────┐
│  Exchange Adapter: CCXT.pro             │
│  watch_tickers / fetch_order_book / ... │
└─────────────────────────────────────────┘
```

---

## Структура файлов

```
arb_scanner/
├── ws_scanner.py       # Главный — WS-сканер с executor (запуск)
├── scanner.py          # REST-only fallback
├── currencies.py       # Метаданные: контракты, withdraw fees, deposit/withdraw статус
├── depth.py            # VWAP и проверка глубины стакана
├── executor.py         # Paper + Live режим, risk-management
├── test_keys.py        # Проверка валидности API ключей
├── test_perms.py       # Проверка прав ключей (read/trade)
├── test_depth.py       # Тест depth check
├── test_ws_run.py      # Тест WS feeders
├── .env                # API ключи (не в git)
├── .env.example        # Шаблон
├── .gitignore
├── trades.jsonl        # Журнал всех сделок (append-only)
└── executor_state.json # Текущий PnL, серия убытков
```

---

## Ключевые алгоритмы

### 1. Asynchronous fan-out
```python
results = await asyncio.gather(*(fetch_one(ex) for ex in exchanges))
```
Параллельные запросы к 6 биржам — 1 сек вместо 6.

### 2. Поиск возможности
```python
buy = min(rows, key=lambda r: r.ask)   # дешевле всего
sell = max(rows, key=lambda r: r.bid)  # дороже всего
gross = (sell.bid - buy.ask) / buy.ask * 100
```

### 3. Net profit с учётом комиссий
```
net = ((bid×(1-fee_taker) - ask×(1+fee_taker)) / ask) − (fee_withdraw / position) × 100%
```

### 4. Антифейк по контрактам
**Проблема:** На разных биржах под `GT/USDT` могут быть разные токены — фейковый спред 61694%.

**Решение:**
- Спреды > 200% — отбрасываем сразу
- Спреды ≥ 5% — проверяем contract address через `fetch_currencies()`
- Если адреса не совпадают на общей сети → тикер-коллизия → отбрасываем

### 5. Depth check (VWAP)
Top-of-book врёт — в стакане может лежать $5 ликвидности при «спреде 2%».

Решение: для топ-12 кандидатов дёргаем `fetch_order_book`, считаем VWAP на $30 позицию. Если real_net ≤ 0 после слиппажа — отбрасываем.

**Демонстрация эффективности:**
- До depth check: 28 «возможностей»
- После: 1 реальная

### 6. WebSocket для скорости
- **kucoin**: 388 000 апдейтов/сек
- **bitget**: 10 000/сек
- **bitmart**: 500/сек
- **mexc/htx/bingx**: REST поллинг 3 сек (WS не поддерживается на споте)

---

## Executor (исполнитель)

### Режимы

**PAPER** — симулирует исполнение по VWAP, считает виртуальный PnL.
**LIVE** — реальные ордера через `asyncio.gather(buy, sell)`. При провале одного — emergency hedge.

### Risk limits (hard в коде)

```python
position_size_usd = 30
min_real_net_pct = 0.30
require_depth_full = True
max_concurrent = 3
cooldown_per_pair_sec = 60
daily_loss_limit_usd = -30
consecutive_losses_trigger = 2  # → 1ч пауза
order_timeout_sec = 5
```

### Журналирование
- Каждая сделка → одна строка JSON в `trades.jsonl`
- Состояние (дневной PnL, серия) → `executor_state.json`

---

## Результаты paper trading (21.7ч)

```
Сделок:    172
Win rate:  100%
PnL:       +$38.10
Темп:      7.9 сделок/час
24h план:  ~$42
```

### Топ пары

| Пара | Маршрут | Сделок | PnL | Средняя |
|---|---|---|---|---|
| WARD/USDT | bitget↔mexc (двунаправленно) | 23 | $11.75 | $0.51 |
| POLS/USDT | mexc → bingx | 13 | $5.04 | $0.39 |
| CHECK/USDT | bingx → mexc | 44 | $5.00 | $0.11 |
| CHIP/USDT | bingx → mexc | 13 | $2.65 | $0.20 |
| OMI/USDT | mexc → bingx | 15 | $2.17 | $0.14 |

**Ключевые наблюдения:**
- WARD крутится **в обе стороны** — самовыравнивание балансов
- Главный маршрут: **mexc ↔ bingx** (большинство сделок)
- 100% win rate — **только в paper** (реальный live будет 40-60%)

---

## Реалистичный прогноз для LIVE

Paper не учитывает:
1. **Latency** — пока ордер летит 100-300мс, конкуренты съедят спред
2. **Slippage** хуже чем VWAP снапшота
3. **Order rejections** — биржа отклоняет по лот-сайзу/минималам
4. **Партиальное исполнение** — не весь лот пройдёт
5. **Asymmetric fills** — один ордер прошёл, второй нет → emergency hedge → минус

**Ожидание для LIVE с $1000:**
- Win rate ~50% (vs 100% paper)
- Средняя прибыль ~50% от paper (slippage)
- На 2-3 топ-парах: **$5-12/день** реалистично
- Масштабирование лимитировано капиталом

---

## Важный нюанс — pre-funded inventory

Текущий PnL предполагает что **все нужные токены уже куплены на нужных биржах**. Бот не отслеживает балансы.

Для $1000 капитала на 10 пар нужно ~$700-900 связанного капитала (USDT + каждый токен на каждой бирже). Реалистично — **2-3 пары, не 10**.

Перед LIVE добавить:
- Кеш балансов (обновление раз в 30 сек)
- Pre-trade проверка достаточности
- Логирование пропусков по причине нехватки баланса

---

## Что сделано

- [x] WS-сканер на 6 биржах (kucoin/bitget/bitmart на WS, mexc/htx/bingx на REST)
- [x] Антифейк по smart contract address
- [x] Depth check + VWAP
- [x] Withdraw fee + deposit/withdraw status
- [x] Executor с paper-режимом
- [x] Hard risk-limits в коде
- [x] Журнал сделок и persistent state
- [x] API ключи Read+Trade на всех 6 биржах (verified)

## Что дальше

- [ ] **Balance-aware paper** — учитывать ограничения капитала
- [ ] **24-48ч paper trading** — собрать больше статистики
- [ ] **LIVE с микро-лотами** ($10-20) — калибровка vs paper
- [ ] **VPS** — перенос с ноута для 24/7 работы
- [ ] **Self-healing** — auto-reconnect, balance auto-rebalancing
- [ ] **Telegram алерты** — для VPS-режима когда не смотришь в терминал

---

## Команды

```powershell
# Запуск (PAPER по умолчанию)
cd "c:\Users\danil\Downloads\Telegram Desktop\mexc_session_v7\arb_scanner"
python ws_scanner.py

# С другими параметрами
$env:MIN_NET_PCT="0.30" ; $env:POSITION_USD="30" ; python ws_scanner.py

# LIVE (только когда paper показал стабильность)
$env:MODE="live" ; python ws_scanner.py

# Проверка ключей
python test_keys.py

# Анализ журнала
python -c "
import json
recs = [json.loads(l) for l in open('trades.jsonl')]
print(f'{len(recs)} trades, ${sum(r[\"actual_pnl_usd\"] for r in recs):.2f} PnL')
"
```
