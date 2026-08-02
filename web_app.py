#!/usr/bin/env python3
"""
商汤自动注册工具 - Web API 后端
基于 FastAPI，提供 REST API + SSE 实时日志
疾驰短信接码平台 (jichisms.com)
"""

import json
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
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
        with self.lock:
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
    JC_TOKEN: Optional[str] = None
    JC_SID: Optional[str] = None
    HTTP_PROXY: Optional[str] = None
    HTTPS_PROXY: Optional[str] = None
    SMS_ASCRIPTION: Optional[str] = None
    SMS_PARAGRAPH: Optional[str] = None
    REGISTER_COUNT: Optional[int] = None


class RegisterRequest(BaseModel):
    count: int = 1
    workers: int = 1


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
    """注册事件回调 → 写入 logs（线程安全）"""
    level = "info"
    if "失败" in msg or "错误" in msg or "异常" in msg:
        level = "error"
    elif "等待" in msg or "警告" in msg:
        level = "warning"
    state.add_log(level, msg)


# 全局速率控制：取号和发送验证码需要串行 + 最小间隔，避免触发平台风控
_phone_lock = threading.Lock()
_sms_lock = threading.Lock()
_rate_state = {"phone_last": 0.0, "sms_last": 0.0}
_PHONE_INTERVAL = 3.0   # 取号最小间隔 3 秒
_SMS_INTERVAL = 5.0     # 发送验证码最小间隔 5 秒


class _RateLimitedOrchestrator(RegistrationOrchestrator):
    """带速率控制的编排器：取号和发验证码串行化+限速，其余步骤并行"""

    def __init__(self, sms, data_dir="data", platform_name="商汤科技"):
        super().__init__(sms, data_dir, platform_name)

    def _execute(self) -> dict:
        """重写 _execute，在取号和发送验证码处加全局锁"""
        from sensenova.core.sensenova_client import SensenovaClient
        from sensenova.utils.crypto import gen_username, gen_password
        import time as _time

        ss = SensenovaClient(proxies=self.sms.proxies)
        self._emit("step", "1/8 获取 login_challenge")

        # 1. Challenge
        ss.fetch_login_challenge()
        if not ss.check_challenge():
            raise RuntimeError("challenge 无效, 请重试")

        # 2. 取号（全局串行 + 限速，避免疾驰风控）
        self._emit("step", "2/8 获取手机号")
        with _phone_lock:
            elapsed = time.time() - _rate_state["phone_last"]
            if elapsed < _PHONE_INTERVAL:
                _time.sleep(_PHONE_INTERVAL - elapsed)
            phone = self.sms.get_phone()
            _rate_state["phone_last"] = time.time()

        try:
            # 3. 发送验证码（全局串行 + 限速，避免商汤风控）
            self._emit("step", "3/8 发送短信验证码")
            with _sms_lock:
                elapsed = time.time() - _rate_state["sms_last"]
                if elapsed < _SMS_INTERVAL:
                    _time.sleep(_SMS_INTERVAL - elapsed)
                ss.send_sms(phone)
                _rate_state["sms_last"] = time.time()

            # 4. 轮询验证码（可并行，纯查询）
            self._emit("step", "4/8 等待验证码")
            code = self.sms.get_verify_code(phone)

            # 5. 校验
            self._emit("step", "5/8 校验验证码")
            verify_resp = ss.verify_sms(code)
            if verify_resp.get("code") != 1 and "access_token" not in str(verify_resp):
                raise RuntimeError(f"验证码校验失败: {verify_resp.get('msg', 'unknown')}")

            # 6. 注册
            self._emit("step", "6/8 注册账号")
            username = gen_username()
            password = gen_password()
            log.info(f"[注册] 用户名={username}")
            redirect = ss.register(username, password)

            # 释放
            self._emit("step", "7/8 释放号码 & 获取 Token")
            self.sms.release_phone()

            # 7. Token
            ss.exchange_code_for_token(redirect)

            # 8. API Key
            self._emit("step", "8/8 获取 API Key")
            keys = ss.get_api_keys()
            if not keys:
                keys = [ss.create_api_key()]

            api_key = keys[0].get("api_key", "")
            user_info = ss.get_user_info()

        except Exception:
            self._cleanup_phone()
            raise

        result = {
            "platform": self.platform,
            "username": username,
            "password": password,
            "tenant_code": user_info.get("tenant_code", username),
            "user_id": ss.user_id or "",
            "phone": phone,
            "access_token": ss.access_token or "",
            "refresh_token": ss.refresh_token or "",
            "api_key": api_key,
            "api_key_name": keys[0].get("displayname", ""),
            "create_time": _time.strftime("%Y-%m-%d %H:%M:%S"),
        }

        self.results.append(result)
        self._persist(result)
        self._emit("done", f"注册成功: {username}")
        return result


def _register_one(idx: int, total: int) -> Optional[dict]:
    """单个账号注册（在工作线程中执行）"""
    state.add_log("info", f"[线程-{idx}] 开始注册 {idx}/{total}")
    try:
        # 每个线程独立的 SMSClient 实例（requests.Session 非线程安全）
        sms = SMSClient(
            token=config.JC_TOKEN,
            sid=config.JC_SID,
            ascription=config.SMS_ASCRIPTION,
            paragraph=config.SMS_PARAGRAPH,
            proxies=config.proxies or None,
        )
        orch = _RateLimitedOrchestrator(sms)
        orch.on_event = lambda ev, msg: _on_event(ev, f"[{idx}] {msg}")
        r = orch.run()
        if r:
            with state.lock:
                state.progress["done"] += 1
            return r
        return None
    except Exception as e:
        state.add_log("error", f"[线程-{idx}] 异常: {e}")
        return None


def _do_register(count: int, workers: int):
    """后台注册线程（线程池并发）"""
    try:
        config.reload()
        if not config.JC_TOKEN or not config.JC_SID:
            state.finish(error="配置不完整，请先设置疾驰短信 fcToken/项目SID")
            return

        setup_log(callback=_on_event)
        state.add_log("info", f"开始注册: {count} 个账号, {workers} 线程并发")

        ok = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_register_one, i + 1, count): i + 1
                for i in range(count)
            }
            for future in as_completed(futures):
                idx = futures[future]
                try:
                    r = future.result()
                    if r:
                        ok += 1
                        state.add_log("done", f"[线程-{idx}] 注册成功: {r.get('username','')}")
                    else:
                        state.add_log("error", f"[线程-{idx}] 注册失败")
                except Exception as e:
                    state.add_log("error", f"[线程-{idx}] 异常: {e}")

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
        "JC_TOKEN": config.JC_TOKEN[:6] + "***" if config.JC_TOKEN else "",
        "JC_TOKEN_FULL": config.JC_TOKEN,
        "JC_SID": config.JC_SID,
        "HTTP_PROXY": config.HTTP_PROXY,
        "HTTPS_PROXY": config.HTTPS_PROXY,
        "SMS_ASCRIPTION": config.SMS_ASCRIPTION,
        "SMS_PARAGRAPH": config.SMS_PARAGRAPH,
        "REGISTER_COUNT": config.REGISTER_COUNT,
    }


@app.put("/api/config")
async def api_update_config(body: ConfigUpdate):
    if body.JC_TOKEN is not None:
        config.JC_TOKEN = body.JC_TOKEN
    if body.JC_SID is not None:
        config.JC_SID = body.JC_SID
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

    workers = max(1, min(body.workers, 10))
    thread = threading.Thread(target=_do_register, args=(body.count, workers), daemon=True)
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
            with state.lock:
                current_logs = list(state.logs)
            while sent < len(current_logs):
                entry = current_logs[sent]
                yield f"data: {json.dumps(entry, ensure_ascii=False)}\n\n"
                sent += 1
            # 检查是否结束
            if not state.running:
                yield f"data: {json.dumps({'type': 'done', 'result': state.result, 'error': state.error}, ensure_ascii=False)}\n\n"
                break
            time.sleep(0.1)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
