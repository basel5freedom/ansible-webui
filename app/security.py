import bcrypt

from .config import WEBUI_PASSWORD_HASH, WEBUI_USERNAME


def verify_login(username: str, password: str) -> bool:
    if not WEBUI_PASSWORD_HASH:
        return False
    if username != WEBUI_USERNAME:
        return False
    try:
        return bcrypt.checkpw(password.encode(), WEBUI_PASSWORD_HASH.encode())
    except ValueError:
        return False
