import asyncio
import json
import os
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .config import LOG_DIR, get_settings


@dataclass
class Job:
    id: str
    category: str
    playbook: str
    limit: str
    started_at: float
    status: str = "running"  # running / success / failed
    returncode: Optional[int] = None
    finished_at: Optional[float] = None
    buffer: list = field(default_factory=list)
    subscribers: list = field(default_factory=list)  # list[asyncio.Queue]

    @property
    def log_path(self) -> Path:
        return LOG_DIR / f"{self.id}.log"

    @property
    def meta_path(self) -> Path:
        return LOG_DIR / f"{self.id}.json"

    def meta_dict(self) -> dict:
        return {
            "id": self.id,
            "category": self.category,
            "playbook": self.playbook,
            "limit": self.limit,
            "started_at": self.started_at,
            "status": self.status,
            "returncode": self.returncode,
            "finished_at": self.finished_at,
        }

    def save_meta(self) -> None:
        self.meta_path.write_text(json.dumps(self.meta_dict(), ensure_ascii=False), encoding="utf-8")


class JobManager:
    """1プロセス・1ワーカー構成前提のインメモリなジョブ管理。

    ジョブの状態・購読者(WebSocket)はプロセス内メモリで保持するため、
    uvicornは必ずworker数1で動かすこと（複数workerだとジョブが
    どのワーカーで動いているか分からなくなる）。再起動後もジョブ一覧・
    ログ本文自体はlogsディレクトリのファイルから参照できる。
    """

    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}

    def list_jobs(self) -> list[dict]:
        items = {j.id: j.meta_dict() for j in self.jobs.values()}
        for meta_file in LOG_DIR.glob("*.json"):
            if meta_file.stem in items:
                continue
            try:
                items[meta_file.stem] = json.loads(meta_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
        return sorted(items.values(), key=lambda m: m["started_at"], reverse=True)

    def get(self, job_id: str) -> Optional[Job]:
        return self.jobs.get(job_id)

    async def start(
        self,
        category: str,
        playbook_file: str,
        limit: str,
        vault_password: Optional[str],
    ) -> Job:
        settings = get_settings()
        job = Job(
            id=uuid.uuid4().hex,
            category=category,
            playbook=playbook_file,
            limit=limit,
            started_at=time.time(),
        )
        self.jobs[job.id] = job
        job.save_meta()

        cmd = [settings.ansible_playbook_bin, "-i", str(settings.inventory_path), playbook_file]
        if limit:
            cmd += ["-l", limit]

        asyncio.create_task(self._run_playbook(job, cmd, settings.ansible_dir, vault_password))
        return job

    async def start_script(
        self,
        category: str,
        script_path: Path,
        vault_password: Optional[str],
    ) -> Job:
        """生成済みのシェルスクリプト（一括更新プログラム等）を実行する。

        ansible-playbookへの直接実行（start）とは異なり、スクリプト自身が
        内部で`read -s -p "Vault password: "`のような対話入力を求める前提のため、
        標準入力へパスワードをパイプで渡す（--vault-password-fileは使わない）。
        """
        settings = get_settings()
        job = Job(
            id=uuid.uuid4().hex,
            category=category,
            playbook=script_path.name,
            limit="",
            started_at=time.time(),
        )
        self.jobs[job.id] = job
        job.save_meta()

        stdin_bytes = (vault_password + "\n").encode() if vault_password else None
        asyncio.create_task(self._execute(job, ["bash", str(script_path)], settings.ansible_dir, stdin_bytes))
        return job

    async def _run_playbook(self, job: Job, cmd: list, cwd: Path, vault_password: Optional[str]) -> None:
        # ★ --vault-password-file /dev/stdin を使うと、Ansible側が渡されたパスを
        #   「実行可能なスクリプトかどうか」判定する処理でパイプを実行しようとして
        #   Permission denied になる場合がある（実機の検証環境で発生）。
        #   シェルの`echo "$VAULT_PASS" | ansible-playbook ...`パイプとは異なる
        #   経路でstdinを渡しているため、このWebUIでは代わりにパーミッション
        #   0600の使い捨て一時ファイルへパスワードを書き、そのパスを渡す方式にした。
        vault_password_file: Optional[str] = None
        full_cmd = list(cmd)
        if vault_password:
            fd, vault_password_file = tempfile.mkstemp(prefix="webui_vaultpw_")
            os.chmod(vault_password_file, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(vault_password)
            full_cmd += ["--vault-password-file", vault_password_file]

        try:
            await self._execute(job, full_cmd, cwd, None)
        finally:
            if vault_password_file:
                try:
                    os.remove(vault_password_file)
                except OSError:
                    pass

    async def _execute(self, job: Job, cmd: list, cwd: Path, stdin_bytes: Optional[bytes]) -> None:
        env = dict(os.environ)
        env["ANSIBLE_FORCE_COLOR"] = "1"
        env["PY_COLORS"] = "1"

        log_file = job.log_path.open("w", encoding="utf-8")
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(cwd),
                stdin=asyncio.subprocess.PIPE if stdin_bytes else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
            if stdin_bytes and proc.stdin is not None:
                proc.stdin.write(stdin_bytes)
                await proc.stdin.drain()
                proc.stdin.close()

            assert proc.stdout is not None
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                self._emit(job, line.decode(errors="replace"), log_file)

            returncode = await proc.wait()
            job.returncode = returncode
            job.status = "success" if returncode == 0 else "failed"
        except FileNotFoundError as exc:
            self._emit(job, f"\n★ 実行に失敗しました: {exc}\n", log_file)
            job.status = "failed"
            job.returncode = -1
        except Exception as exc:  # noqa: BLE001 - 想定外のエラーもジョブ失敗として画面に出す
            self._emit(job, f"\n★ 予期しないエラー: {exc}\n", log_file)
            job.status = "failed"
            job.returncode = -1
        finally:
            job.finished_at = time.time()
            job.save_meta()
            log_file.close()
            self._emit(job, None, None)  # 終了通知（購読者へ）

    def _emit(self, job: Job, text: Optional[str], log_file) -> None:
        if text is not None:
            job.buffer.append(text)
            if log_file is not None:
                log_file.write(text)
                log_file.flush()
        for queue in list(job.subscribers):
            queue.put_nowait(text)

    async def subscribe(self, job_id: str):
        """ジョブ出力を1行(chunk)ずつyieldする。終了時にNoneを1回yieldする。

        プロセス再起動後などメモリにジョブが残っていない場合は、
        保存済みのログファイルの内容を一括でyieldしてから終了する。
        """
        job = self.jobs.get(job_id)
        if job is None:
            log_path = LOG_DIR / f"{job_id}.log"
            if log_path.exists():
                yield log_path.read_text(encoding="utf-8")
            yield None
            return

        queue: asyncio.Queue = asyncio.Queue()
        job.subscribers.append(queue)
        try:
            for text in job.buffer:
                yield text
            if job.status != "running":
                yield None
                return
            while True:
                text = await queue.get()
                yield text
                if text is None:
                    break
        finally:
            if queue in job.subscribers:
                job.subscribers.remove(queue)


job_manager = JobManager()
