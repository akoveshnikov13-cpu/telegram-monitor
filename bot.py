"""Private join notifications for a single administrator. Python 3.10+."""
import getpass
import json
import logging
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("joins")


class APIError(Exception):
    def __init__(self, code=0, retry_after=5):
        self.code = code
        self.retry_after = retry_after
        super().__init__(f"Telegram: код ошибки {code}")


class Telegram:
    def __init__(self, token):
        self.base = f"https://api.telegram.org/bot{token}/"

    def call(self, method, **params):
        request = urllib.request.Request(self.base + method,
                    data=json.dumps(params).encode(), headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                result = json.load(exc)
            except (ValueError, OSError):
                raise APIError(exc.code) from None
        except (OSError, ValueError):
            raise APIError() from None
        if not result.get("ok"):
            raise APIError(result.get("error_code", 0), result.get("parameters", {}).get("retry_after", 5))
        return result["result"]


def member(value):
    return value.get("status") in ("member", "administrator", "creator") or (
        value.get("status") == "restricted" and value.get("is_member", False))


def setup_menu(api, admin_id):
    """Install quick commands only in the administrator's private chat."""
    commands = [
        {"command": "chats", "description": "Мои чаты"},
        {"command": "connect", "description": "Подключить чат / канал"},
        {"command": "check", "description": "Проверить права бота"},
        {"command": "setlink", "description": "Задать ссылку на чат"},
        {"command": "help", "description": "Помощь"},
    ]
    api.call("setMyCommands", commands=commands,
             scope={"type": "chat", "chat_id": admin_id})
    api.call("setChatMenuButton", chat_id=admin_id,
             menu_button={"type": "commands"})


class Monitor:
    def __init__(self, api, admin_id, database):
        self.api = api
        self.admin = admin_id
        self.db = sqlite3.connect(database)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS chats(id INTEGER PRIMARY KEY, info TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1, link TEXT);
            CREATE TABLE IF NOT EXISTS queue(id INTEGER PRIMARY KEY AUTOINCREMENT,
                payload TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0,
                retries INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS seen(id INTEGER PRIMARY KEY, received REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)

    def offset(self):
        row = self.db.execute("SELECT value FROM meta WHERE key='offset'").fetchone()
        # Telegram can choose a new random update ID after a week without events.
        last = self.db.execute("SELECT MAX(received) FROM seen").fetchone()[0]
        return int(row[0]) if row and last and time.time() - last < 6 * 86400 else 0

    def enqueue(self, text, chat=None, markup=None):
        self.db.execute("INSERT INTO queue(payload) VALUES (?)",
                        (json.dumps({"text": text, "chat": chat, "markup": markup}, ensure_ascii=False),))

    def remember(self, chat, active=None):
        self.db.execute("INSERT INTO chats(id, info) VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET info=excluded.info",
                        (chat["id"], json.dumps(chat, ensure_ascii=False)))
        if active is not None:
            self.db.execute("UPDATE chats SET active=? WHERE id=?", (int(active), chat["id"]))

    def handle(self, update):
        with self.db:
            if self.db.execute("SELECT 1 FROM seen WHERE id=?", (update["update_id"],)).fetchone():
                return
            change = update.get("chat_member")
            own = update.get("my_chat_member")
            if change and change["chat"]["type"] in ("group", "supergroup", "channel"):
                chat = change["chat"]
                self.remember(chat)
                old, new = change["old_chat_member"], change["new_chat_member"]
                if not member(old) and member(new):
                    user = new["user"]  # 'from' is the actor, not necessarily the new member.
                    name = " ".join(filter(None, [user.get("first_name"), user.get("last_name")])) or "Без имени"
                    username = "@" + user["username"] if user.get("username") else "не указан"
                    kind = "Канал" if chat["type"] == "channel" else "Чат"
                    date = datetime.fromtimestamp(change["date"], timezone(timedelta(hours=3))).strftime("%d.%m.%Y %H:%M:%S МСК")
                    self.enqueue(f"Новый участник\n\n{kind}: {chat.get('title', 'Без названия')}\n"
                                 f"ID чата: {chat['id']}\nИмя: {name}\nЮзернейм: {username}\n"
                                 f"Telegram ID: {user['id']}\nВступил: {date}", chat)
            elif own and own["chat"]["type"] in ("group", "supergroup", "channel"):
                active = own["new_chat_member"]["status"] == "administrator"
                self.remember(own["chat"], active)
                text = "Отслеживание подключено" if active else "Отслеживание недоступно: бот должен быть администратором"
                self.enqueue(f"{text}\n{own['chat'].get('title', '')}\nID: {own['chat']['id']}")
            elif "message" in update:
                msg = update["message"]
                if msg.get("chat", {}).get("type") == "private" and msg["chat"]["id"] == self.admin and msg.get("from", {}).get("id") == self.admin:
                    origin = msg.get("forward_origin", {})
                    if origin.get("type") == "channel":
                        self.connect(origin["chat"]["id"])
                    else:
                        self.command(msg.get("text", ""))
            self.db.execute("INSERT INTO seen VALUES (?, ?)", (update["update_id"], time.time()))
            self.db.execute("INSERT OR REPLACE INTO meta VALUES ('offset', ?)", (str(update["update_id"] + 1),))
            self.db.execute("DELETE FROM seen WHERE received<?", (time.time() - 8 * 86400,))

    def command(self, text):
        args = text.split()
        command = args[0].split("@")[0] if args else ""
        if command == "/connect" and len(args) == 1:
            command = "/help"  # Show connection buttons when selected in Menu.
        if command in ("/connect", "/check"):
            if len(args) != 2:
                self.enqueue("Пришлите /connect @имя_канала или /check -1001234567890. "
                             "Можно также переслать мне публикацию из канала. "
                             "Сначала назначьте бота администратором канала.")
            else:
                target = args[1]
                if re.fullmatch(r"https://t\.me/[A-Za-z0-9_]+/?", target):
                    target = "@" + target.rstrip("/").rsplit("/", 1)[1]
                if re.fullmatch(r"-\d+", target):
                    target = int(target)
                if isinstance(target, int) or re.fullmatch(r"@[A-Za-z0-9_]+", target):
                    self.connect(target)
                else:
                    self.enqueue("Нужен @username канала или его числовой ID -100… . "
                                 "Закрытую пригласительную ссылку нельзя использовать для подключения. "
                                 "Перешлите публикацию из канала либо добавьте бота через администраторов канала.")
        elif command == "/chats":
            rows = self.db.execute("SELECT * FROM chats ORDER BY id").fetchall()
            self.enqueue(f"Подключённых чатов: {len(rows)}")
            for row in rows:
                chat = json.loads(row["info"])
                self.enqueue(f"{chat.get('title', 'Без названия')}\nID: {row['id']}\n"
                             + ("Отслеживание включено" if row["active"] else "Нет прав администратора"), chat)
        elif command == "/setlink":
            if len(args) != 3 or not re.fullmatch(r"-\d+", args[1]) or not re.fullmatch(r"https://t\.me/[A-Za-z0-9_+/-]+", args[2]):
                self.enqueue("Формат: /setlink -1001234567890 https://t.me/+ВАША_ССЫЛКА")
                return
            cursor = self.db.execute("UPDATE chats SET link=? WHERE id=?", (args[2], int(args[1])))
            self.enqueue("Ссылка сохранена." if cursor.rowcount else "Чат пока не подключён. Добавьте туда бота администратором.")
        else:
            markup = None
            try:
                username = self.api.call("getMe")["username"]
                markup = {"inline_keyboard": [
                    [{"text": "Подключить канал", "url": f"https://t.me/{username}?startchannel&admin=invite_users"}],
                    [{"text": "Подключить группу", "url": f"https://t.me/{username}?startgroup&admin=invite_users"}]]}
            except APIError:
                pass
            self.enqueue("Бот уведомляет о вступлениях только вас.\n"
                         "Быстрый доступ к командам — кнопка «Меню» возле строки ввода.\n\n"
                         "Для канала нажмите «Подключить канал» ниже. В списке будут каналы, "
                         "где вы можете назначать администраторов. Или откройте канал → Управление → "
                         "Администраторы → Добавить администратора и найдите бота по @username.\n\n"
                         "Добавьте его администратором в нужные группы и каналы. "
                         "В закрытых чатах разрешите приглашать пользователей для кнопки «Перейти».\n\n"
                         "/chats — подключённые чаты\n"
                         "/connect @канал — подключить и проверить права\n"
                         "/check ID_ЧАТА — проверить текущие права\n"
                         "Пересланная публикация из канала тоже запускает проверку.\n"
                         "/setlink ID_ЧАТА https://t.me/+ССЫЛКА — задать ссылку вручную", markup=markup)

    def connect(self, target):
        try:
            chat = self.api.call("getChat", chat_id=target)
            if chat.get("type") not in ("channel", "group", "supergroup"):
                self.enqueue("Подключить можно только канал или группу.")
                return
            me = self.api.call("getMe")
            rights = self.api.call("getChatMember", chat_id=chat["id"], user_id=me["id"])
        except APIError as exc:
            self.enqueue(f"Не удалось проверить канал/чат (код {exc.code}). "
                         "Проверьте адрес и добавьте бота в Администраторы канала. "
                         "Для закрытого канала перешлите публикацию или укажите ID -100… . "
                         "Если код 0 или 429, повторите проверку позже.")
            return
        active = rights.get("status") in ("administrator", "creator")
        self.remember(chat, active)
        if not active:
            self.enqueue(f"{chat.get('title', '')}: бот не администратор. "
                         "Назначьте его администратором именно канала, а не только привязанной группы обсуждений.")
            return
        self.enqueue(f"Подключено: {chat.get('title', '')}\nID: {chat['id']}\n"
                     "Права администратора подтверждены Telegram. Отслеживаются новые вступления. "
                     "Проверьте другим аккаунтом: выйдите и вступите снова. "
                     "Ожидающая одобрения заявка ещё не считается вступлением.\n"
                     + ("Право создавать ссылки включено." if rights.get("can_invite_users") else
                        "Для кнопки в закрытом канале разрешите приглашать пользователей или используйте /setlink."), chat)

    def link(self, chat):
        if chat.get("username"):
            return "https://t.me/" + chat["username"]
        row = self.db.execute("SELECT link FROM chats WHERE id=?", (chat["id"],)).fetchone()
        if row and row[0]:
            return row[0]
        try:
            info = self.api.call("getChat", chat_id=chat["id"])
            url = "https://t.me/" + info["username"] if info.get("username") else info.get("invite_link")
            if not url:
                url = self.api.call("createChatInviteLink", chat_id=chat["id"], name="Переход администратора")["invite_link"]
        except APIError:
            return None
        with self.db:
            self.db.execute("UPDATE chats SET link=? WHERE id=?", (url, chat["id"]))
        return url

    def drain(self):
        rows = self.db.execute("SELECT * FROM queue WHERE done=0 AND next_try<=? ORDER BY id LIMIT 20", (time.time(),)).fetchall()
        for row in rows:
            data = json.loads(row["payload"])
            payload = {"chat_id": self.admin, "text": data["text"], "link_preview_options": {"is_disabled": True}}
            if data.get("markup"):
                payload["reply_markup"] = data["markup"]
            if data["chat"]:
                url = self.link(data["chat"])
                if url:
                    payload["reply_markup"] = {"inline_keyboard": [[{"text": "Перейти", "url": url}]]}
                else:
                    payload["text"] += ("\n\nНе удалось получить ссылку. Разрешите боту приглашать участников "
                                        "или задайте ссылку через /setlink.")
            try:
                self.api.call("sendMessage", **payload)
            except APIError as exc:
                delay = max(exc.retry_after, min(300, 5 * 2 ** min(row["retries"], 6)))
                with self.db:
                    self.db.execute("UPDATE queue SET retries=retries+1, next_try=? WHERE id=?", (time.time() + delay, row["id"]))
                LOG.warning("Уведомление сохранено, повтор через %s сек. Код %s", delay, exc.code)
                break
            with self.db:
                self.db.execute("UPDATE queue SET done=1 WHERE id=?", (row["id"],))
            time.sleep(1.1)  # Avoid bursts into the administrator's private chat.


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    lock = None
    monitor = None
    try:
        path = ROOT / "config.json"
        config = json.loads(path.read_text(encoding="utf-8-sig"))
        token = os.environ.get("BOT_TOKEN") or config.get("bot_token", "")
        if not token:
            if not sys.stdin.isatty():
                raise ValueError("Задайте BOT_TOKEN в настройках хостинга или bot_token в config.json.")
            print("Создайте ОТДЕЛЬНОГО бота через @BotFather /newbot.")
            token = getpass.getpass("Вставьте его токен (ввод скрыт): ").strip()
            if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", token):
                raise ValueError("Неверный формат токена.")
            config["bot_token"] = token
            path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", token):
            raise ValueError("Проверьте токен в config.json.")
        admin = int(os.environ.get("ADMIN_ID") or config["admin_id"])
        if admin <= 0:
            raise ValueError("admin_id должен быть положительным Telegram ID.")
        data = ROOT / "data"
        data.mkdir(exist_ok=True)
        lock = (data / "bot.lock").open("a+b")
        lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError("Этот бот уже запущен в другом окне.") from None
        api = Telegram(token)
        me = api.call("getMe")
        api.call("deleteWebhook", drop_pending_updates=False)
        monitor = Monitor(api, admin, data / "monitor.sqlite3")
        print(f"Бот @{me['username']} работает. Получатель: {admin}. Остановка: Ctrl+C.")
        print("Откройте бота со своего аккаунта и нажмите «Запустить».")
        menu_ready = False
        menu_retry_at = 0
        while True:
            if not menu_ready and time.monotonic() >= menu_retry_at:
                try:
                    setup_menu(api, admin)
                    menu_ready = True
                    LOG.info("Меню быстрого доступа включено для администратора.")
                except APIError as exc:
                    # An unopened private chat or API outage must not stop monitoring.
                    menu_retry_at = time.monotonic() + max(60, exc.retry_after)
                    LOG.warning("Не удалось настроить меню (код %s). Повтор позже; откройте личный чат с ботом.", exc.code)
            try:
                monitor.drain()
                for update in api.call("getUpdates", offset=monitor.offset(), timeout=5,
                                       allowed_updates=["chat_member", "my_chat_member", "message"]):
                    monitor.handle(update)
            except APIError as exc:
                LOG.warning("%s. Повтор подключения позже.", exc)
                time.sleep(max(5, exc.retry_after))
    except KeyboardInterrupt:
        print("\nОстановлено. Очередь уведомлений сохранена.")
    except (ValueError, KeyError, OSError, APIError) as exc:
        LOG.error("Ошибка запуска: %s", exc)
        return 1
    finally:
        if monitor:
            monitor.db.close()
        if lock:
            lock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
