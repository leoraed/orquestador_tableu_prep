import logging
import httpx
from src.config import load_settings

logger = logging.getLogger(__name__)


def enviar_telegram(mensaje: str) -> bool:
    s = load_settings()
    token = s.get("telegram_bot_token", "").strip()
    chat_id = s.get("telegram_chat_id", "").strip()
    if not token or not chat_id:
        logger.debug("Telegram no configurado, se omite notificacion.")
        return False
    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": mensaje},
            timeout=10,
        )
        r.raise_for_status()
        logger.info("Notificacion Telegram enviada.")
        return True
    except Exception as exc:
        logger.error(f"Error enviando Telegram: {exc}")
        return False
