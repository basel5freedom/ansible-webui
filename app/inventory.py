import asyncio
import shutil
import time

from .config import get_settings


class InventoryError(Exception):
    pass


def read_inventory() -> str:
    settings = get_settings()
    return settings.inventory_path.read_text(encoding="utf-8")


async def write_inventory(new_content: str) -> None:
    """ansible-inventoryで検証してから、バックアップを取って本体を書き換える。

    検証に失敗した場合は本体には一切触れない（元のファイルはそのまま残る）。
    """
    settings = get_settings()
    path = settings.inventory_path
    tmp_path = path.with_name(path.name + ".webui_tmp")
    tmp_path.write_text(new_content, encoding="utf-8")

    try:
        proc = await asyncio.create_subprocess_exec(
            settings.ansible_inventory_bin, "-i", str(tmp_path), "--list",
            cwd=str(settings.ansible_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError as exc:
        tmp_path.unlink(missing_ok=True)
        raise InventoryError(f"{settings.ansible_inventory_bin} を実行できません: {exc}") from exc
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        tmp_path.unlink(missing_ok=True)
        raise InventoryError(out.decode(errors="replace"))

    backup_path = path.with_name(f"{path.name}.bak.{time.strftime('%Y%m%d%H%M%S')}")
    shutil.copy2(path, backup_path)
    shutil.move(str(tmp_path), str(path))


def parse_hostnames() -> list[str]:
    """実行フォームの補完用に、インベントリからホスト名らしき行を簡易抽出する。"""
    hosts = []
    for line in read_inventory().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("["):
            continue
        token = line.split()[0]
        if "=" in token:
            continue
        hosts.append(token)
    return hosts
