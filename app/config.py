import os
from functools import lru_cache
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).resolve().parent.parent


class PlaybookEntry:
    def __init__(self, file: str, label: str, vault: bool = True):
        self.file = file
        self.label = label
        self.vault = vault


class Category:
    def __init__(self, key: str, label: str, playbooks):
        self.key = key
        self.label = label
        self.playbooks = [PlaybookEntry(**p) for p in playbooks]


class Settings:
    def __init__(self, data: dict):
        self.ansible_dir = Path(data["ansible_dir"])
        self.inventory_path = Path(data["inventory_path"])
        self.vault_path = Path(data["vault_path"])
        self.ansible_playbook_bin = data.get("ansible_playbook_bin", "ansible-playbook")
        self.ansible_vault_bin = data.get("ansible_vault_bin", "ansible-vault")
        self.ansible_inventory_bin = data.get("ansible_inventory_bin", "ansible-inventory")
        self.categories = [Category(**c) for c in data.get("categories", [])]

    def find_playbook(self, category_key: str, filename: str):
        for cat in self.categories:
            if cat.key != category_key:
                continue
            for pb in cat.playbooks:
                if pb.file == filename:
                    return cat, pb
        return None, None


@lru_cache()
def get_settings() -> Settings:
    config_path = Path(os.environ.get("WEBUI_CONFIG", BASE_DIR / "config.yml"))
    with open(config_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return Settings(data)


# ログイン・セッション関連の機密情報は環境変数から取得する（config.ymlには書かない）。
WEBUI_USERNAME = os.environ.get("WEBUI_USERNAME", "admin")
WEBUI_PASSWORD_HASH = os.environ.get("WEBUI_PASSWORD_HASH", "")
WEBUI_SESSION_SECRET = os.environ.get("WEBUI_SESSION_SECRET", "")

LOG_DIR = Path(os.environ.get("WEBUI_LOG_DIR", BASE_DIR / "logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)
