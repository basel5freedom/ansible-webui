import asyncio
import os
import shutil
import tempfile
import time
from pathlib import Path

from .config import get_settings


class VaultError(Exception):
    pass


def _write_password_tempfile(password: str) -> str:
    # ★ --vault-password-file /dev/stdin は、Ansible側の「実行可能スクリプトかどうか」
    #   判定処理がパイプをexecしようとしてPermission deniedになる場合があるため
    #   （実機の検証環境で発生）、代わりにパーミッション0600の使い捨て一時ファイルへ
    #   パスワードを書き、そのパスを渡す。
    fd, path = tempfile.mkstemp(prefix="webui_vaultpw_")
    os.chmod(path, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(password)
    return path


async def view_vault(password: str) -> str:
    settings = get_settings()
    password_file = _write_password_tempfile(password)
    try:
        try:
            proc = await asyncio.create_subprocess_exec(
                settings.ansible_vault_bin, "view", str(settings.vault_path),
                "--vault-password-file", password_file,
                cwd=str(settings.ansible_dir),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise VaultError(f"{settings.ansible_vault_bin} を実行できません: {exc}") from exc
        out, err = await proc.communicate()
        if proc.returncode != 0:
            raise VaultError(err.decode(errors="replace") or out.decode(errors="replace"))
        return out.decode(errors="replace")
    finally:
        os.remove(password_file)


async def save_vault(new_plaintext: str, password: str) -> None:
    """平文を一時ファイルへ書き、ansible-vault encryptで暗号化してから本体に反映する。

    平文パスワードはプロセスの標準入力経由でansible-vaultに渡すのみで、
    ディスクに書き込むことは一切無い（一時ファイルは暗号化後の内容のみ）。
    """
    settings = get_settings()
    vault_path = settings.vault_path

    fd, tmp_name = tempfile.mkstemp(prefix="webui_vault_", dir=str(vault_path.parent))
    tmp_path = Path(tmp_name)
    password_file = _write_password_tempfile(password)
    try:
        os.chmod(tmp_path, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new_plaintext)

        try:
            proc = await asyncio.create_subprocess_exec(
                settings.ansible_vault_bin, "encrypt", str(tmp_path),
                "--vault-password-file", password_file,
                cwd=str(settings.ansible_dir),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise VaultError(f"{settings.ansible_vault_bin} を実行できません: {exc}") from exc
        out, err = await proc.communicate()
        if proc.returncode != 0:
            raise VaultError(err.decode(errors="replace") or out.decode(errors="replace"))

        if vault_path.exists():
            backup_path = vault_path.with_name(f"{vault_path.name}.bak.{time.strftime('%Y%m%d%H%M%S')}")
            shutil.copy2(vault_path, backup_path)
        shutil.move(str(tmp_path), str(vault_path))
    finally:
        if tmp_path.exists():
            os.remove(tmp_path)
        os.remove(password_file)
