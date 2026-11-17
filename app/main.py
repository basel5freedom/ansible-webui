import datetime as _dt
import json
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import inventory as inv
from . import scripts as scr
from . import vault as vlt
from .config import LOG_DIR, WEBUI_SESSION_SECRET, get_settings
from .jobs import job_manager
from .security import verify_login

BASE_DIR = Path(__file__).resolve().parent.parent

if not WEBUI_SESSION_SECRET:
    raise RuntimeError("環境変数 WEBUI_SESSION_SECRET を設定してください（セッション署名用の鍵）")

app = FastAPI(title="Ansible WebUI")
app.add_middleware(SessionMiddleware, secret_key=WEBUI_SESSION_SECRET)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _format_ts(value: Optional[float]) -> str:
    if value is None:
        return "-"
    return _dt.datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")


templates.env.filters["dt"] = _format_ts


def get_current_user(request: Request) -> Optional[str]:
    return request.session.get("user")


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    if get_current_user(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    if verify_login(username, password):
        request.session["user"] = username
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        "login.html", {"request": request, "error": "ユーザー名またはパスワードが違います"}
    )


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    settings = get_settings()
    jobs = job_manager.list_jobs()[:20]
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "user": user, "categories": settings.categories, "jobs": jobs},
    )


@app.get("/run", response_class=HTMLResponse)
async def run_form(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    settings = get_settings()
    hostnames = inv.parse_hostnames()
    return templates.TemplateResponse(
        "run.html",
        {
            "request": request,
            "user": user,
            "categories": settings.categories,
            "hostnames": hostnames,
            "error": None,
        },
    )


@app.post("/run")
async def run_submit(
    request: Request,
    category: str = Form(...),
    playbook: str = Form(...),
    limit: str = Form(""),
    vault_password: str = Form(""),
):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    settings = get_settings()
    _, pb = settings.find_playbook(category, playbook)
    if pb is None:
        # ★ 設定(config.yml)のカタログに無いPlaybookは実行を拒否する
        #   （フォームの値を直接そのままansible-playbookへ渡さないための必須チェック）。
        hostnames = inv.parse_hostnames()
        return templates.TemplateResponse(
            "run.html",
            {
                "request": request,
                "user": user,
                "categories": settings.categories,
                "hostnames": hostnames,
                "error": "選択されたPlaybookが設定カタログに見つかりません",
            },
            status_code=400,
        )

    job = await job_manager.start(category, pb.file, limit.strip(), vault_password or None)
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


@app.get("/jobs", response_class=HTMLResponse)
async def jobs_list(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(
        "jobs_list.html", {"request": request, "user": user, "jobs": job_manager.list_jobs()}
    )


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_page(request: Request, job_id: str):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    job = job_manager.get(job_id)
    if job is not None:
        meta = job.meta_dict()
    else:
        meta_path = LOG_DIR / f"{job_id}.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else None
    return templates.TemplateResponse(
        "job.html", {"request": request, "user": user, "job_id": job_id, "meta": meta}
    )


@app.get("/api/jobs/{job_id}")
async def job_status_api(request: Request, job_id: str):
    # ★ WebSocketがプロキシのタイムアウト等で切断された場合のフォールバック用。
    #   job.htmlがWebSocket切断時にこれをポーリングして状態表示を更新する。
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    job = job_manager.get(job_id)
    if job is not None:
        meta = job.meta_dict()
    else:
        meta_path = LOG_DIR / f"{job_id}.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else None
    if meta is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(meta)


@app.websocket("/ws/jobs/{job_id}")
async def job_ws(websocket: WebSocket, job_id: str):
    user = websocket.session.get("user")
    if not user:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    try:
        async for chunk in job_manager.subscribe(job_id):
            if chunk is None:
                await websocket.send_json({"type": "done"})
                break
            await websocket.send_json({"type": "line", "data": chunk})
    except WebSocketDisconnect:
        pass


@app.get("/scripts", response_class=HTMLResponse)
async def scripts_form(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(
        "scripts.html", {"request": request, "user": user, "scripts": scr.list_scripts(), "error": None}
    )


@app.post("/scripts/run")
async def scripts_run(request: Request, script: str = Form(...), vault_password: str = Form("")):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    script_path = scr.resolve_script(script)
    if script_path is None:
        # ★ ディレクトリ内に実在し、命名規則に一致するファイル以外は実行を拒否する
        #   （フォームの値を直接そのまま実行しないための必須チェック）。
        return templates.TemplateResponse(
            "scripts.html",
            {"request": request, "user": user, "scripts": scr.list_scripts(), "error": "選択されたスクリプトが見つかりません"},
            status_code=400,
        )
    job = await job_manager.start_script("daily_check_update", script_path, vault_password or None)
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


@app.get("/hosts", response_class=HTMLResponse)
async def hosts_form(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    content = inv.read_inventory()
    return templates.TemplateResponse(
        "hosts.html", {"request": request, "user": user, "content": content, "error": None, "saved": False}
    )


@app.post("/hosts", response_class=HTMLResponse)
async def hosts_submit(request: Request, content: str = Form(...)):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    try:
        await inv.write_inventory(content)
    except inv.InventoryError as exc:
        return templates.TemplateResponse(
            "hosts.html",
            {"request": request, "user": user, "content": content, "error": str(exc), "saved": False},
            status_code=400,
        )
    return templates.TemplateResponse(
        "hosts.html", {"request": request, "user": user, "content": content, "error": None, "saved": True}
    )


@app.get("/vault", response_class=HTMLResponse)
async def vault_form(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse("vault_view.html", {"request": request, "user": user, "error": None})


@app.post("/vault/view", response_class=HTMLResponse)
async def vault_view_submit(request: Request, password: str = Form(...)):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    try:
        plaintext = await vlt.view_vault(password)
    except vlt.VaultError as exc:
        return templates.TemplateResponse(
            "vault_view.html", {"request": request, "user": user, "error": str(exc)}, status_code=400
        )
    return templates.TemplateResponse(
        "vault_edit.html",
        {"request": request, "user": user, "content": plaintext, "error": None, "saved": False},
    )


@app.post("/vault/save", response_class=HTMLResponse)
async def vault_save_submit(request: Request, content: str = Form(...), password: str = Form(...)):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    try:
        await vlt.save_vault(content, password)
    except vlt.VaultError as exc:
        return templates.TemplateResponse(
            "vault_edit.html",
            {"request": request, "user": user, "content": content, "error": str(exc), "saved": False},
            status_code=400,
        )
    return templates.TemplateResponse(
        "vault_edit.html",
        {"request": request, "user": user, "content": content, "error": None, "saved": True},
    )
