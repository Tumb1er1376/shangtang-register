#!/usr/bin/env python3
"""
商汤自动注册工具 - Web API 后端
基于 FastAPI，提供 REST API + SSE 实时日志
豪猪码接码平台
"""

import json
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from sensenova.core.orchestrator import RegistrationOrchestrator
from sensenova.core.sms_client import SMSClient
from sensenova.utils.log import setup as setup_log, proxy as log
from sensenova.config import config

DATA_DIR = PROJECT_ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="商汤自动注册工具")


# ─── 全局状态 ───

class TaskState:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.task_id: Optional[str] = None
        self.logs: list[dict] = []
        self.result: Optional[dict] = None
        self.error: Optional[str] = None
        self.progress: dict = {}  # {done, total}

    def reset(self, task_id: str, total: int):
        self.running = True
        self.task_id = task_id
        self.logs = []
        self.result = None
        self.error = None
        self.progress = {"done": 0, "total": total}

    def add_log(self, level: str, msg: str):
        self.logs.append({
            "time": time.strftime("%H:%M:%S"),
            "level": level,
            "msg": msg,
        })

    def finish(self, result: dict = None, error: str = None):
        self.running = False
        self.result = result
        self.error = error


state = TaskState()


# ─── 数据模型 ───

class ConfigUpdate(BaseModel):
    HZM_USER: Optional[str] = None
    HZM_PASS: Optional[str] = None
    HZM_SID: Optional[str] = None
    HTTP_PROXY: Optional[str] = None
    HTTPS_PROXY: Optional[str] = None
    SMS_ASCRIPTION: Optional[str] = None
    SMS_PARAGRAPH: Optional[str] = None
    REGISTER_COUNT: Optional[int] = None


class RegisterRequest(BaseModel):
    count: int = 1


# ─── 辅助函数 ───

def _load_accounts() -> list[dict]:
    accounts = []
    for f in sorted(DATA_DIR.glob("shangtang-*.json")):
        try:
            accounts.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            pass
    return accounts


def _on_event(event: str, msg: str):
    """注册事件回调 → 写入 logs"""
    level = "info"
    if "失败" in msg or "错误" in msg or "异常" in msg:
        level = "error"
    elif "等待" in msg or "警告" in msg:
        level = "warning"
    state.add_log(level, msg)


def _do_register(count: int):
    """后台注册线程"""
    try:
        config.reload()
        if not config.HZM_USER or not config.HZM_PASS or not config.HZM_SID:
            state.finish(error="配置不完整，请先设置豪猪码账号/密码/项目SID")
            return

        sms = SMSClient(
            user=config.HZM_USER,
            pwd=config.HZM_PASS,
            sid=config.HZM_SID,
            ascription=config.SMS_ASCRIPTION,
            paragraph=config.SMS_PARAGRAPH,
            proxies=config.proxies or None,
        )

        setup_log(callback=_on_event)
        orch = RegistrationOrchestrator(sms)
        orch.on_event = _on_event

        ok = 0
        for i in range(count):
            state.add_log("info", f"\n{'='*40}\n第 {i+1}/{count} 次注册\n{'='*40}")
            r = orch.run()
            if r:
                ok += 1
                state.progress["done"] = ok
            elif i < count - 1:
                time.sleep(5)

        state.add_log("info", f"全部完成: 成功 {ok}/{count}")
        state.finish(result={"success": ok, "total": count})

    except Exception as e:
        state.add_log("error", str(e))
        state.finish(error=str(e))


# ─── 路由 ───

@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = PROJECT_ROOT / "templates" / "index.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@app.get("/api/accounts")
async def api_accounts():
    return _load_accounts()


@app.get("/api/accounts/export")
async def api_export_keys():
    accounts = _load_accounts()
    keys = [a["api_key"] for a in accounts if a.get("api_key")]
    return {"keys": keys, "count": len(keys)}


@app.delete("/api/accounts/{username}")
async def api_delete_account(username: str):
    p = DATA_DIR / f"shangtang-{username}.json"
    if p.exists():
        p.unlink()
        return {"ok": True}
    return JSONResponse({"error": "未找到"}, status_code=404)


@app.get("/api/config")
async def api_get_config():
    config.reload()
    return {
        "HZM_USER": config.HZM_USER,
        "HZM_PASS": config.HZM_PASS[:3] + "***" if config.HZM_PASS else "",
        "HZM_PASS_FULL": config.HZM_PASS,
        "HZM_SID": config.HZM_SID,
        "HTTP_PROXY": config.HTTP_PROXY,
        "HTTPS_PROXY": config.HTTPS_PROXY,
        "SMS_ASCRIPTION": config.SMS_ASCRIPTION,
        "SMS_PARAGRAPH": config.SMS_PARAGRAPH,
        "REGISTER_COUNT": config.REGISTER_COUNT,
    }


@app.put("/api/config")
async def api_update_config(body: ConfigUpdate):
    if body.HZM_USER is not None:
        config.HZM_USER = body.HZM_USER
    if body.HZM_PASS is not None:
        config.HZM_PASS = body.HZM_PASS
    if body.HZM_SID is not None:
        config.HZM_SID = body.HZM_SID
    if body.HTTP_PROXY is not None:
        config.HTTP_PROXY = body.HTTP_PROXY
    if body.HTTPS_PROXY is not None:
        config.HTTPS_PROXY = body.HTTPS_PROXY
    if body.SMS_ASCRIPTION is not None:
        config.SMS_ASCRIPTION = body.SMS_ASCRIPTION
    if body.SMS_PARAGRAPH is not None:
        config.SMS_PARAGRAPH = body.SMS_PARAGRAPH
    if body.REGISTER_COUNT is not None:
        config.REGISTER_COUNT = body.REGISTER_COUNT
    config.save_to_file()
    return {"ok": True}


@app.post("/api/register")
async def api_register(body: RegisterRequest):
    with state.lock:
        if state.running:
            return JSONResponse({"error": "已有注册任务正在运行"}, status_code=409)
        task_id = str(uuid.uuid4())[:8]
        state.reset(task_id, body.count)

    thread = threading.Thread(target=_do_register, args=(body.count,), daemon=True)
    thread.start()
    return {"task_id": task_id}


@app.get("/api/status")
async def api_status():
    return {
        "running": state.running,
        "task_id": state.task_id,
        "progress": state.progress,
        "result": state.result,
        "error": state.error,
        "log_count": len(state.logs),
    }


@app.get("/api/register/stream")
async def api_register_stream():
    """SSE 实时日志流"""
    def event_stream():
        sent = 0
        while True:
            # 发送新日志
            while sent < len(state.logs):
                entry = state.logs[sent]
                yield f"data: {json.dumps(entry, ensure_ascii=False)}\n\n"
                sent += 1
            # 检查是否结束
            if not state.running:
                yield f"data: {json.dumps({'type': 'done', 'result': state.result, 'error': state.error}, ensure_ascii=False)}\n\n"
                break
            time.sleep(0.3)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
