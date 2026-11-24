import os


def load_env(path=".env"):
    """Минимальный разбор .env (KEY=VALUE), не перетирает уже заданные переменные окружения."""
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def id_list(name):
    return [int(x) for x in os.environ.get(name, "").replace(" ", "").split(",") if x]


load_env()
TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT_IDS = id_list("TG_CHAT_IDS")          # куда слать и кому отвечать
TG_ALLOWED_USERS = id_list("TG_ALLOWED_USERS")  # доп. user_id, которым можно управлять ботом
DETAIL_WORKERS = int(os.environ.get("DETAIL_WORKERS", "8"))
FEED_IDLE_SEC = float(os.environ.get("FEED_IDLE_SEC", "5"))
