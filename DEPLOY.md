# Ansible WebUI 配置手順

NetBox連携Ansible Playbookを、Webブラウザから実行・差分チェック・hosts編集・
vault編集・一括更新スクリプト実行できるようにする管理ツール。Playbookが
実際に実行されるAnsibleサーバー上に、常駐サービスとして配置する。

## 1. 配置

このフォルダ（`webui/`）をAnsibleサーバーの任意の場所（例: `/opt/ansible-webui/`）に
配置する。`config.yml`の`ansible_dir`/`inventory_path`/`vault_path`を、
配置先の実際のパスに合わせて書き換える。配置前に対象サーバーで実際のパスを
確認すること（例: `find / -name "register_cisco_to_netbox.yml" 2>/dev/null`）。

`categories:`配下のPlaybook一覧は、実際に存在するplaybookファイル名と
一致させること（存在しないファイルを指定しても実行時にエラーになるだけで
害は無いが、一覧に出さないほうが分かりやすい）。

## 2. Python環境

```bash
cd /opt/ansible-webui
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 3. 認証情報の準備

```bash
source venv/bin/activate
python hash_password.py
# → パスワードを2回入力すると、bcryptハッシュが1行出力される
```

出力されたハッシュと、セッション署名用の鍵（`python -c "import secrets; print(secrets.token_hex(32))"`
で生成）を、`/etc/ansible-webui.env` のような環境変数ファイルに保存する
（`.env.example`を参考に、以下の3つの環境変数を必ず設定する）。

```
WEBUI_USERNAME=admin
WEBUI_PASSWORD_HASH=（hash_password.pyの出力）
WEBUI_SESSION_SECRET=（secrets.token_hexの出力）
```

このファイルは`chmod 600`にしておくこと。

## 4. 動作確認（手動起動）

```bash
cd /opt/ansible-webui
source venv/bin/activate
set -a; source /etc/ansible-webui.env; set +a
uvicorn app.main:app --host 127.0.0.1 --port 8080 --workers 1
```

**★ `--workers 1` は必須。** ジョブの実行状態・WebSocket購読者はプロセス内
メモリで管理しているため、workerを複数にすると「実行したジョブと別のworker
がリクエストを受けてログが見えない」という事態になる。

ブラウザ（同じホスト、またはSSHポートフォワード経由）で
`http://127.0.0.1:8080/` を開き、ログイン→「実行」ページから何か軽い
Playbook（差分チェック系など）を動かして、ログがリアルタイムに流れることを
確認する。

## 5. systemdサービス化

`/etc/systemd/system/ansible-webui.service` を以下の内容で作成する
（パスは実際の配置先に合わせる）。

```ini
[Unit]
Description=Ansible WebUI
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/ansible-webui
EnvironmentFile=/etc/ansible-webui.env
ExecStart=/opt/ansible-webui/venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8080 --workers 1
Restart=on-failure
User=root

[Install]
WantedBy=multi-user.target
```

`User=root`としているのは、ansible-playbook/ansible-vaultの実行や
hosts/vaultファイルの書き換えに必要な権限を素直に持たせるため。
ネットワークから直接たたかれないよう、`--host 127.0.0.1`でlocalhostにのみ
bindし、外部からのアクセスは次項のリバースプロキシ経由に限定すること。

```bash
systemctl daemon-reload
systemctl enable --now ansible-webui
systemctl status ansible-webui
```

## 6. 外部公開（リバースプロキシ・アクセス制限）

このツールはVaultの平文編集・Playbook実行という強力な操作ができるため、
**社内ネットワーク/VPN経由でのみアクセスできるようにする**ことを強く推奨する。
nginx等を立て、ansible-webuiへは`127.0.0.1:8080`へのリバースプロキシとして
接続する。

WebSocket（`/ws/`配下。ライブログ表示に使用）のプロキシ設定では、
`Upgrade`/`Connection`ヘッダーの転送に加え、**タイムアウトを長めに設定する
こと**。Playbookの実行中は出力が長時間止まることがあり、デフォルトの
アイドルタイムアウト（多くのプロキシで60秒程度）だと接続が切れ、ブラウザの
状態表示が更新されなくなる。

nginxの構成例（HTTP）:

```nginx
server {
    listen 80;
    server_name _;

    location /ws/ {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
```

社内ネットワーク外からの到達性が無いことを前提にHTTPで運用する場合でも、
ログインパスワード・Vaultパスワードが平文で流れる点は把握しておくこと。
より安全にしたい場合は、HTTPS（`listen 443 ssl;` + 証明書）を追加する。

さらに、ファイアウォール（`firewalld`/`iptables`/`ufw`等）で、公開した
ポートへの到達を社内ネットワーク/VPNのアドレス範囲のみに制限すること。

## 既知の制約（把握した上で運用すること）

- ジョブ状態・ログはインメモリ＋`webui/logs/`配下のファイルで管理している。
  サービス再起動中に実行していたジョブの状態は失われる（ログファイル自体は
  残るので、再起動後も`/jobs/<job_id>`で過去のログ本文は見える）。
- CSRFトークンは実装していない（セッション認証のみ）。社内/VPN限定公開を
  前提とした設計である。より強固にしたい場合は、手前のリバースプロキシで
  クライアント証明書やBasic認証を併用することを検討する。
- vaultパスワードは画面入力の都度サーバーのメモリ上で使い捨てる設計で、
  ディスクへの平文保存は行わない（保存時も再入力を必須にしている）。
- Playbookの登録用`--vault-password-file`には使い捨て一時ファイル
  （パーミッション0600、実行後すぐ削除）を使う。`/dev/stdin`を渡すと、
  Ansible側の「渡されたパスが実行可能なスクリプトかどうか」判定処理が
  パイプの実行を試みてPermission deniedになる環境があるため。
  一方、「一括更新スクリプト実行」機能で動かすシェルスクリプトは、
  スクリプト自身が内部で`read -s -p`のような対話入力を求める前提のため、
  標準入力へパスワードをパイプで渡す方式のままにしている。
- xterm.js（ライブログ表示のターミナル描画）は`static/xterm/`に同梱済みで、
  外部CDNへの到達性が無い環境でも動作する。
