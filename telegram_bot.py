"""Eye Crypt Telegram bot — control & monitoring for a single subscriber instance.

Runs as an asyncio task inside ws_scanner.main(). Dependency-free: talks to the
Telegram Bot API directly over httpx (long-polling getUpdates + sendMessage), so
it adds nothing to the PyInstaller build.

Features:
  - Subscription: status / renew / cancel (via the Eye Crypt backend API).
  - Notifications: position open & close with PnL (hooked into Executor).
  - Balances: free balances per exchange (from the live RealBalanceCache, or a
    fresh fetch_balance() fallback in paper mode).

Auth: only chat_ids in TELEGRAM_CHAT_ID may use the bot. If that var is empty,
the first user to /start binds themselves (persisted to .telegram_chats.json).

Subscription commands talk to the Eye Crypt backend with the *account* JWT
(from POST /v1/login with email+password), which is separate from the bot's
license key. Real endpoints:
  GET  /v1/me/subscription        status (tier, expires_at, days_left)
  POST /v1/me/cancel-subscription cancel (runs until expiry)
  GET  /v1/payments/wallet        deposit address + per-tier USDT prices
  POST /v1/payments/submit        {tier, tx_hash} → verifies TRC-20 tx, +30 days

Env:
  TELEGRAM_BOT_TOKEN       BotFather token (required to enable the bot)
  TELEGRAM_CHAT_ID         comma-separated authorized chat ids (optional)
  TELEGRAM_NOTIFY_OPENS    "1" (default) to also notify on position open
  EYECRYPT_API             backend base url (default https://api.eyecryptbot.com)
  EYECRYPT_ACCOUNT_TOKEN   optional pre-set account JWT (else use /login)
  TELEGRAM_MINIAPP_URL     public https base of the local dashboard (Cloudflare
                           tunnel) — enables the "Кабинет" Mini App button
"""
import asyncio
import html
import json
import os
import time
from pathlib import Path

import httpx

from logsetup import get_logger

log = get_logger("telegram")

_CHATS_FILE = Path(__file__).parent / ".telegram_chats.json"
_SESSION_FILE = Path(__file__).parent / ".eyecrypt_session.json"

# ── brand (Eye Crypt) ──
EYE = "👁"
DIV = "─────────────"


def _h(title: str) -> str:
    """Branded section header."""
    return f"{EYE} <b>Eye Crypt</b> · {title}\n{DIV}"


def _fmt_usd(v: float) -> str:
    return f"${v:,.2f}"


def _fmt_age(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    return f"{seconds / 3600:.1f}h"


class TelegramBot:
    def __init__(self, executor=None, hub=None, ex_by_id=None):
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.api = f"https://api.telegram.org/bot{self.token}"
        self.executor = executor
        self.hub = hub
        self.ex_by_id = ex_by_id or {}
        self.notify_opens = os.getenv("TELEGRAM_NOTIFY_OPENS", "1").strip() not in ("0", "", "false")
        self.api_base = os.getenv("EYECRYPT_API", "https://api.eyecryptbot.com").rstrip("/")
        self.miniapp_url = os.getenv("TELEGRAM_MINIAPP_URL", "").strip().rstrip("/") or None
        self._miniapp_pinned = self.miniapp_url is not None  # explicit env wins; don't override
        self.account_token = os.getenv("EYECRYPT_ACCOUNT_TOKEN", "").strip() or None
        if self.account_token is None and _SESSION_FILE.exists():
            try:
                self.account_token = json.loads(_SESSION_FILE.read_text()).get("access_token")
            except Exception:
                pass
        self._offset = 0
        self._client: httpx.AsyncClient | None = None
        self.authorized: set[int] = set()
        self._load_chats()

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    # ---------- chat authorization ----------
    def _load_chats(self):
        env_ids = os.getenv("TELEGRAM_CHAT_ID", "")
        for part in env_ids.replace(";", ",").split(","):
            part = part.strip()
            if part:
                try:
                    self.authorized.add(int(part))
                except ValueError:
                    log.warning(f"bad TELEGRAM_CHAT_ID entry: {part!r}")
        self._env_authorized = set(self.authorized)
        if not self.authorized and _CHATS_FILE.exists():
            try:
                for cid in json.loads(_CHATS_FILE.read_text()):
                    self.authorized.add(int(cid))
            except Exception as e:
                log.warning(f"could not read {_CHATS_FILE.name}: {e}")

    def _persist_chats(self):
        # Only persist self-bound chats (env-configured ones live in .env).
        learned = sorted(self.authorized - self._env_authorized)
        try:
            _CHATS_FILE.write_text(json.dumps(learned))
        except Exception as e:
            log.warning(f"could not persist chats: {e}")

    def _is_authorized(self, chat_id: int) -> bool:
        return chat_id in self.authorized

    # ---------- Telegram transport ----------
    async def _api_call(self, method: str, **params):
        assert self._client is not None
        try:
            r = await self._client.post(f"{self.api}/{method}", json=params)
            data = r.json()
            if not data.get("ok"):
                log.warning(f"telegram {method} not ok: {data.get('description')}")
            return data
        except Exception as e:
            log.warning(f"telegram {method} failed: {e}")
            return {"ok": False}

    async def send(self, chat_id: int, text: str, keyboard: list | None = None,
                   inline: list | None = None):
        params = dict(chat_id=chat_id, text=text, parse_mode="HTML",
                      disable_web_page_preview=True)
        if inline is not None:
            params["reply_markup"] = {"inline_keyboard": inline}
        elif keyboard is not None:
            params["reply_markup"] = {"keyboard": keyboard, "resize_keyboard": True}
        await self._api_call("sendMessage", **params)

    def _cabinet_button(self):
        """Inline web_app button that opens the Mini App, if a URL is configured."""
        if not self.miniapp_url:
            return None
        return [[{"text": f"{EYE} Открыть кабинет", "web_app": {"url": f"{self.miniapp_url}/miniapp"}}]]

    async def broadcast(self, text: str):
        for cid in list(self.authorized):
            await self.send(cid, text)

    # ---------- notification hooks (called by Executor) ----------
    async def notify_open(self, opp: dict, position_usd: float):
        if not self.notify_opens or not self.authorized:
            return
        sym = html.escape(str(opp.get("symbol", "?")))
        buy = html.escape(str(opp.get("buy_ex", "?")))
        sell = html.escape(str(opp.get("sell_ex", "?")))
        spread = opp.get("real_net_pct", 0) or 0
        text = (f"🟢 <b>Открыта</b> {sym}\n"
                f"{buy} → {sell}\n"
                f"объём {_fmt_usd(position_usd)} · спред {spread:.3f}%")
        await self.broadcast(text)

    async def notify_close(self, rec):
        if not self.authorized:
            return
        sym = html.escape(str(getattr(rec, "symbol", "?")))
        buy = html.escape(str(getattr(rec, "buy_ex", "?")))
        sell = html.escape(str(getattr(rec, "sell_ex", "?")))
        status = getattr(rec, "status", "?")
        pnl = getattr(rec, "actual_pnl_usd", None) or 0.0
        net = getattr(rec, "actual_net_pct", None) or 0.0
        if status == "ok":
            icon = "✅" if pnl >= 0 else "🔻"
            sign = "+" if pnl >= 0 else ""
            text = (f"{icon} <b>Закрыта</b> {sym}\n"
                    f"{buy} → {sell}\n"
                    f"PnL {sign}{_fmt_usd(pnl)} ({net:+.3f}%)")
        else:
            err = html.escape(str(getattr(rec, "error", ""))[:120])
            text = (f"⚠️ <b>{html.escape(status)}</b> {sym}\n"
                    f"{buy} → {sell}\n{err}")
        # Append running daily total when available
        if self.executor is not None:
            day = getattr(self.executor, "daily_pnl_usd", None)
            if day is not None:
                text += f"\nза день: {_fmt_usd(day)}"
        await self.broadcast(text)

    # ---------- command handlers ----------
    def _main_keyboard(self):
        return [["/balances", "/status"], ["/pnl", "/sub"]]

    async def _cmd_start(self, chat_id: int, just_bound: bool):
        hello = "чат привязан ✅" if just_bound else "бот на связи"
        text = f"{_h(hello)}\n{self._help_text()}"
        cabinet = self._cabinet_button()
        if cabinet is not None:
            await self.send(chat_id, text, inline=cabinet)
            await self.send(chat_id, "Меню ниже 👇", self._main_keyboard())
        else:
            await self.send(chat_id, text, self._main_keyboard())

    def _help_text(self) -> str:
        return ("<b>Команды</b>\n"
                "/balances — балансы по всем биржам\n"
                "/balance &lt;биржа&gt; — балансы одной биржи\n"
                "/status — статус бота (режим, сделки, активные)\n"
                "/pnl — дневной PnL и винрейт\n"
                "/login &lt;email&gt; &lt;пароль&gt; [totp] — вход в аккаунт\n"
                "/sub — статус подписки\n"
                "/renew — реквизиты для продления\n"
                "/pay &lt;тариф&gt; &lt;tx_hash&gt; — отправить оплату\n"
                "/cancel — отменить подписку\n"
                "/logout — выйти из аккаунта")

    async def _cmd_balances(self, chat_id: int, ex_filter: str | None = None):
        bc = getattr(self.executor, "balance_cache", None)
        vp = getattr(self.executor, "virtual_portfolio", None)
        lines: list[str] = []

        if bc is not None and getattr(bc, "balances", None):
            total = 0.0
            for ex_id in sorted(bc.balances):
                if ex_filter and ex_id != ex_filter:
                    continue
                bals = {k: v for k, v in bc.balances[ex_id].items() if v and v > 0}
                age = time.time() - bc.last_update.get(ex_id, 0)
                err = bc.errors.get(ex_id)
                head = f"<b>{html.escape(ex_id)}</b> <i>({_fmt_age(age)} назад)</i>"
                if err:
                    lines.append(f"{head}\n  ⚠️ {html.escape(err)}")
                    continue
                if not bals:
                    lines.append(f"{head}\n  — пусто")
                    continue
                usdt = bals.get("USDT", 0)
                total += usdt
                body = "\n".join(
                    f"  {html.escape(a)}: {q:,.6g}".rstrip("0").rstrip(".")
                    for a, q in sorted(bals.items(), key=lambda x: -x[1])[:15]
                )
                lines.append(f"{head}\n{body}")
            if not ex_filter:
                lines.append(f"\n<b>Σ USDT (free): {_fmt_usd(total)}</b>")
        elif vp is not None:
            for ex_id in sorted(vp.balances):
                if ex_filter and ex_id != ex_filter:
                    continue
                bals = {k: v for k, v in vp.balances[ex_id].items() if v}
                body = "\n".join(f"  {html.escape(a)}: {q:,.6g}" for a, q in
                                 sorted(bals.items(), key=lambda x: -abs(x[1]))[:15])
                lines.append(f"<b>{html.escape(ex_id)}</b> (paper)\n{body or '  — пусто'}")
            if vp is not None and self.hub is not None:
                lines.append(f"\n<b>MTM: {_fmt_usd(vp.total_value_usd(self.hub))}</b>")

        if not lines:
            await self.send(chat_id, f"{_h('балансы')}\nНедоступны (кэш ещё не заполнен или биржи не подключены).")
            return
        await self.send(chat_id, f"{_h('балансы')}\n" + "\n".join(lines), inline=self._cabinet_button())

    async def _cmd_status(self, chat_id: int):
        ex = self.executor
        if ex is None:
            await self.send(chat_id, "Исполнитель не подключён.")
            return
        mode = getattr(getattr(ex, "cfg", None), "mode", None)
        mode = getattr(mode, "value", str(mode))
        active = ", ".join(sorted(getattr(ex, "active", set()))) or "—"
        paused = getattr(ex, "paused_until", 0) - time.time()
        text = (f"{_h('статус')}\n"
                f"режим: {html.escape(str(mode))}\n"
                f"сделок: {getattr(ex, 'total_trades', 0)} · побед: {getattr(ex, 'total_wins', 0)}\n"
                f"рассмотрено: {getattr(ex, 'considered', 0)}\n"
                f"активных: {len(getattr(ex, 'active', set()))} ({html.escape(active)})\n"
                f"серия убытков: {getattr(ex, 'consecutive_losses', 0)}")
        if paused > 0:
            text += f"\n⏸ на паузе ещё {_fmt_age(paused)}"
        await self.send(chat_id, text)

    async def _cmd_pnl(self, chat_id: int):
        ex = self.executor
        if ex is None:
            await self.send(chat_id, "Исполнитель не подключён.")
            return
        trades = getattr(ex, "total_trades", 0)
        wins = getattr(ex, "total_wins", 0)
        wr = (wins / trades * 100) if trades else 0
        day = getattr(ex, "daily_pnl_usd", 0.0)
        text = (f"{_h('PnL')}\n"
                f"за день: {_fmt_usd(day)}\n"
                f"сделок: {trades} · винрейт: {wr:.1f}%")
        await self.send(chat_id, text, inline=self._cabinet_button())

    # ---------- subscription (backend API, account JWT) ----------
    def _account_headers(self):
        if not self.account_token:
            return None
        return {"Authorization": f"Bearer {self.account_token}"}

    def _persist_session(self):
        try:
            _SESSION_FILE.write_text(json.dumps({"access_token": self.account_token}))
        except Exception as e:
            log.warning(f"could not persist session: {e}")

    async def _cmd_login(self, chat_id: int, args: list[str]):
        if len(args) < 2:
            await self.send(chat_id, "Использование: /login <email> <пароль> [totp]")
            return
        email, password = args[0], args[1]
        totp = args[2] if len(args) > 2 else None
        body = {"email": email, "password": password}
        if totp:
            body["totp_code"] = totp
        try:
            async with httpx.AsyncClient(timeout=15) as cli:
                r = await cli.post(f"{self.api_base}/v1/login", json=body)
            data = r.json() if r.content else {}
            if r.status_code == 200 and data.get("access_token"):
                self.account_token = data["access_token"]
                self._persist_session()
                await self.send(chat_id, "Вход выполнен ✅", self._main_keyboard())
            elif data.get("detail") == "totp_required":
                await self.send(chat_id, "Нужен код 2FA: /login <email> <пароль> <totp>")
            else:
                await self.send(chat_id, f"Вход не удался: {html.escape(str(data.get('detail', r.status_code)))}")
        except Exception as e:
            await self.send(chat_id, f"Вход недоступен: {html.escape(str(e)[:120])}")

    async def _cmd_logout(self, chat_id: int):
        self.account_token = None
        try:
            _SESSION_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        await self.send(chat_id, "Выход выполнен.")

    async def _need_login(self, chat_id: int) -> bool:
        if self._account_headers() is None:
            await self.send(chat_id, "Сначала войдите в аккаунт: /login <email> <пароль> [totp]")
            return True
        return False

    async def _cmd_sub(self, chat_id: int):
        if await self._need_login(chat_id):
            return
        try:
            async with httpx.AsyncClient(timeout=10) as cli:
                r = await cli.get(f"{self.api_base}/v1/me/subscription", headers=self._account_headers())
            if r.status_code == 401:
                await self.send(chat_id, "Сессия истекла. Войдите снова: /login")
                return
            if r.status_code != 200:
                await self.send(chat_id, f"Подписка: ошибка ({r.status_code}).")
                return
            d = r.json()
            if not d:
                await self.send(chat_id, "Активной подписки нет. /renew — оформить.",
                                [["/renew"], ["/balances", "/status"]])
                return
            active = "активна ✅" if d.get("is_active") else "истекла ❌"
            text = (f"{_h('подписка')}\n"
                    f"статус: {active}\n"
                    f"тариф: {d.get('tier', '?')}\n"
                    f"осталось дней: {d.get('days_left', '?')}\n"
                    f"действует до: {html.escape(str(d.get('expires_at', '?')))}")
            await self.send(chat_id, text, [["/renew", "/cancel"], ["/balances", "/status"]])
        except Exception as e:
            await self.send(chat_id, f"Подписка недоступна: {html.escape(str(e)[:120])}")

    async def _cmd_renew(self, chat_id: int):
        if await self._need_login(chat_id):
            return
        try:
            async with httpx.AsyncClient(timeout=10) as cli:
                r = await cli.get(f"{self.api_base}/v1/payments/wallet", headers=self._account_headers())
            d = r.json() if r.status_code == 200 else {}
        except Exception as e:
            await self.send(chat_id, f"Реквизиты недоступны: {html.escape(str(e)[:120])}")
            return
        addr = d.get("address") or "—"
        prices = d.get("prices") or {}
        price_lines = "\n".join(f"  тариф {t}: {p} USDT" for t, p in sorted(prices.items()))
        text = (f"<b>Продление (+30 дней)</b>\n"
                f"Сеть: {html.escape(str(d.get('network', 'TRC-20')))} · {html.escape(str(d.get('asset', 'USDT')))}\n"
                f"Кошелёк: <code>{html.escape(str(addr))}</code>\n"
                f"{price_lines}\n\n"
                f"После перевода отправьте:\n<code>/pay &lt;тариф&gt; &lt;tx_hash&gt;</code>")
        await self.send(chat_id, text)

    async def _cmd_pay(self, chat_id: int, args: list[str]):
        if await self._need_login(chat_id):
            return
        if len(args) < 2 or not args[0].isdigit():
            await self.send(chat_id, "Использование: /pay <тариф 1-3> <tx_hash>")
            return
        body = {"tier": int(args[0]), "tx_hash": args[1]}
        try:
            async with httpx.AsyncClient(timeout=30) as cli:
                r = await cli.post(f"{self.api_base}/v1/payments/submit",
                                   headers=self._account_headers(), json=body)
            data = r.json() if r.content else {}
            if r.status_code == 200:
                await self.send(chat_id, f"Платёж подтверждён ✅ статус: {html.escape(str(data.get('status', 'confirmed')))}.\n"
                                         f"Подписка продлена. /sub — проверить.")
            elif r.status_code == 401:
                await self.send(chat_id, "Сессия истекла. Войдите снова: /login")
            else:
                await self.send(chat_id, f"Платёж не принят: {html.escape(str(data.get('detail', r.status_code)))}")
        except Exception as e:
            await self.send(chat_id, f"Оплата недоступна: {html.escape(str(e)[:120])}")

    async def _cmd_cancel(self, chat_id: int):
        if await self._need_login(chat_id):
            return
        try:
            async with httpx.AsyncClient(timeout=15) as cli:
                r = await cli.post(f"{self.api_base}/v1/me/cancel-subscription",
                                   headers=self._account_headers(), json={})
            data = r.json() if r.content else {}
            if r.status_code == 200:
                until = html.escape(str(data.get("active_until", "")))
                await self.send(chat_id, f"Подписка отменена. Работает до: {until}")
            elif r.status_code == 404:
                await self.send(chat_id, "Активной подписки нет.")
            elif r.status_code == 401:
                await self.send(chat_id, "Сессия истекла. Войдите снова: /login")
            else:
                await self.send(chat_id, f"Отмена не удалась: {html.escape(str(data.get('detail', r.status_code)))}")
        except Exception as e:
            await self.send(chat_id, f"Отмена недоступна: {html.escape(str(e)[:120])}")

    # ---------- dispatch ----------
    async def _handle(self, msg: dict):
        chat = msg.get("chat", {})
        chat_id = chat.get("id")
        text = (msg.get("text") or "").strip()
        if chat_id is None or not text:
            return

        just_bound = False
        if not self._is_authorized(chat_id):
            # Self-bind only when nobody is configured yet.
            if not self.authorized and text.split()[0].split("@")[0] == "/start":
                self.authorized.add(chat_id)
                self._persist_chats()
                just_bound = True
                log.info(f"bound first chat_id={chat_id}")
            else:
                await self.send(chat_id, "⛔ Доступ запрещён.")
                log.warning(f"unauthorized chat_id={chat_id}")
                return

        parts = text.split()
        cmd = parts[0].split("@")[0].lower()
        arg = parts[1] if len(parts) > 1 else None
        rest = parts[1:]

        if cmd in ("/start",):
            await self._cmd_start(chat_id, just_bound)
        elif cmd in ("/help",):
            await self.send(chat_id, self._help_text(), self._main_keyboard())
        elif cmd == "/balances":
            await self._cmd_balances(chat_id)
        elif cmd == "/balance":
            await self._cmd_balances(chat_id, ex_filter=(arg.lower() if arg else None))
        elif cmd == "/status":
            await self._cmd_status(chat_id)
        elif cmd == "/pnl":
            await self._cmd_pnl(chat_id)
        elif cmd == "/login":
            await self._cmd_login(chat_id, rest)
        elif cmd == "/logout":
            await self._cmd_logout(chat_id)
        elif cmd == "/sub":
            await self._cmd_sub(chat_id)
        elif cmd == "/renew":
            await self._cmd_renew(chat_id)
        elif cmd == "/pay":
            await self._cmd_pay(chat_id, rest)
        elif cmd == "/cancel":
            await self._cmd_cancel(chat_id)
        else:
            await self.send(chat_id, "Неизвестная команда. /help")

    # ---------- mini app tunnel resolution ----------
    def _resolve_miniapp_url(self) -> str | None:
        """If no explicit URL was pinned via env, discover it from the Cloudflare
        tunnel log and persist to .env. Returns the resolved url or None."""
        if self._miniapp_pinned:
            return self.miniapp_url
        try:
            import tunnel
            url = tunnel.sync_miniapp_url()
        except Exception as e:
            log.debug(f"tunnel resolve failed: {e}")
            return self.miniapp_url
        if url:
            self.miniapp_url = url.rstrip("/")
        return self.miniapp_url

    async def _tunnel_watch(self):
        """Re-discover the tunnel URL periodically; refresh the menu button on change."""
        if self._miniapp_pinned:
            return
        while True:
            await asyncio.sleep(60)
            try:
                prev = self.miniapp_url
                self._resolve_miniapp_url()
                if self.miniapp_url and self.miniapp_url != prev:
                    log.info(f"miniapp url changed → {self.miniapp_url}; refreshing menu button")
                    await self._api_call("setChatMenuButton", menu_button={
                        "type": "web_app", "text": f"{EYE} Кабинет",
                        "web_app": {"url": f"{self.miniapp_url}/miniapp"},
                    })
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.debug(f"tunnel watch error: {e}")

    # ---------- profile / branding ----------
    async def _setup_profile(self):
        """Brand the bot: command menu, description, and the chat menu button.
        Idempotent — Telegram just overwrites on each start."""
        commands = [
            {"command": "balances", "description": "👁 Балансы по биржам"},
            {"command": "status", "description": "Статус бота"},
            {"command": "pnl", "description": "PnL и винрейт"},
            {"command": "sub", "description": "Статус подписки"},
            {"command": "renew", "description": "Продлить подписку"},
            {"command": "cancel", "description": "Отменить подписку"},
            {"command": "login", "description": "Вход в аккаунт"},
            {"command": "help", "description": "Список команд"},
        ]
        await self._api_call("setMyCommands", commands=commands)
        await self._api_call("setMyShortDescription",
                             short_description="Eye Crypt — кросс-биржевой бот: балансы, PnL, подписка.")
        await self._api_call("setMyDescription",
                             description="👁 Eye Crypt\nБалансы по всем биржам, уведомления об "
                                         "открытии/закрытии позиций и PnL, управление подпиской. "
                                         "Нажмите «Запустить», затем /help.")
        if self.miniapp_url:
            await self._api_call("setChatMenuButton", menu_button={
                "type": "web_app", "text": f"{EYE} Кабинет",
                "web_app": {"url": f"{self.miniapp_url}/miniapp"},
            })
        else:
            await self._api_call("setChatMenuButton", menu_button={"type": "commands"})

    # ---------- main loop ----------
    async def run(self):
        if not self.enabled:
            log.info("telegram disabled (no TELEGRAM_BOT_TOKEN)")
            return
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(40.0, connect=10.0))
        me = await self._api_call("getMe")
        uname = (me.get("result") or {}).get("username", "?")
        self._resolve_miniapp_url()
        try:
            await self._setup_profile()
        except Exception as e:
            log.warning(f"profile setup failed: {e}")
        asyncio.create_task(self._tunnel_watch())
        log.info(f"telegram bot @{uname} started; authorized={sorted(self.authorized) or 'none (await /start)'}; "
                 f"miniapp={'on' if self.miniapp_url else 'off'}")
        # Drop any backlog so we don't replay old commands on restart.
        first = await self._api_call("getUpdates", offset=-1, timeout=0)
        for u in first.get("result", []):
            self._offset = max(self._offset, u["update_id"] + 1)
        if self.authorized:
            await self.broadcast(f"{EYE} <b>Eye Crypt</b> запущен. /help")
        while True:
            try:
                resp = await self._api_call("getUpdates", offset=self._offset, timeout=30)
                for u in resp.get("result", []):
                    self._offset = max(self._offset, u["update_id"] + 1)
                    m = u.get("message") or u.get("edited_message")
                    if m:
                        try:
                            await self._handle(m)
                        except Exception as e:
                            log.exception(f"handle failed: {e}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning(f"getUpdates loop error: {e}")
                await asyncio.sleep(3)
        try:
            await self._client.aclose()
        except Exception:
            pass
