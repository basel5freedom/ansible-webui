"""パスワードのbcryptハッシュを生成する（WEBUI_PASSWORD_HASH環境変数に設定する値）。

使い方: python hash_password.py
（パスワードをコマンドライン引数に直接書くと ps 等で他ユーザーから見えてしまうため、
 実行後にその場で入力させる方式にしている）
"""
import getpass

import bcrypt

if __name__ == "__main__":
    pw = getpass.getpass("パスワード: ")
    pw2 = getpass.getpass("確認: ")
    if pw != pw2:
        raise SystemExit("パスワードが一致しません")
    print(bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode())
