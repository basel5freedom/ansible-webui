from pathlib import Path
from typing import Optional

from .config import get_settings


def scripts_dir() -> Path:
    # daily_check_*.ymlがRedmine連携の最後に一括更新スクリプトを保存する先。
    return get_settings().ansible_dir / "daily_check"


def list_scripts() -> list[dict]:
    d = scripts_dir()
    if not d.is_dir():
        return []
    items = []
    for path in d.glob("daily_check_update_*.sh"):
        stat = path.stat()
        items.append({"name": path.name, "mtime": stat.st_mtime, "size": stat.st_size})
    return sorted(items, key=lambda i: i["mtime"], reverse=True)


def resolve_script(name: str) -> Optional[Path]:
    # フォームの値をそのまま実行に使わないため、命名規則への一致とディレクトリ内への
    # 実在を両方確認してから初めてPathを返す（パストラバーサル・任意ファイル実行を防ぐ）。
    if "/" in name or "\\" in name or ".." in name:
        return None
    if not name.startswith("daily_check_update_") or not name.endswith(".sh"):
        return None
    path = scripts_dir() / name
    if path.is_file():
        return path
    return None
