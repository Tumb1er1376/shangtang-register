"""
豪猪码接码平台 API 客户端
https://api.haozhuma.com/sms/
"""

import time
import logging
from typing import Optional

import requests

logger = logging.getLogger("sensenova")

HAOZHUMA_API = "https://api.haozhuma.com/sms/"


class SMSClient:
    """豪猪码接码客户端，兼容 orchestrator 调用接口"""

    def __init__(
        self,
        base_url: str = HAOZHUMA_API,
        user: str = "",
        pwd: str = "",
        sid: str = "",
        ascription: str = "1",
        paragraph: str = "",
        proxies: Optional[dict] = None,
        **_kw,
    ):
        self.base_url = base_url or HAOZHUMA_API
        self.user = user
        self.pwd = pwd
        self.sid = str(sid)
        self.ascription = ascription or "1"
        self.paragraph = paragraph
        self.proxies = proxies
        self.current_phone: Optional[str] = None
        self.token: Optional[str] = None

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        })

    def _request(self, params: dict, retries: int = 3) -> dict:
        last_exc = None
        for i in range(retries):
            try:
                r = self.session.get(self.base_url, params=params, proxies=self.proxies, timeout=30)
                return r.json()
            except Exception as e:
                last_exc = e
                logger.warning("[豪猪码] 请求失败(第%d次): %s", i + 1, e)
                time.sleep(2 ** i)
        raise RuntimeError(f"豪猪码请求失败: {last_exc}")

    def login(self) -> str:
        data = self._request({
            "api": "login",
            "user": self.user,
            "pass": self.pwd,
        })
        if str(data.get("code")) not in ("0", "200"):
            raise RuntimeError(f"豪猪码登录失败: {data}")
        self.token = data["token"]
        logger.info("[豪猪码] 登录成功")
        return self.token

    def _ensure_token(self):
        if not self.token:
            self.login()

    def get_phone(self) -> str:
        """获取手机号"""
        self._ensure_token()
        params = {
            "api": "getPhone",
            "token": self.token,
            "sid": self.sid,
            "ascription": self.ascription,
        }
        if self.paragraph:
            params["paragraph"] = self.paragraph

        data = self._request(params)
        if str(data.get("code")) != "0":
            raise RuntimeError(f"取号失败: {data.get('msg', data)}")
        self.current_phone = data["phone"]
        logger.info("[取号] %s (%s %s)", self.current_phone, data.get("sp", ""), data.get("phone_gsd", ""))
        return self.current_phone

    def get_verify_code(self, phone: str, max_retries: int = 20, interval: int = 5) -> str:
        """轮询获取验证码"""
        self._ensure_token()
        for i in range(max_retries):
            if i > 0:
                time.sleep(interval)
            try:
                data = self._request({
                    "api": "getMessage",
                    "token": self.token,
                    "sid": self.sid,
                    "phone": phone,
                }, retries=2)
                if str(data.get("code")) == "0":
                    code = data.get("yzm", "")
                    logger.info("[验证码] 第%d次查询 -> %s", i + 1, code)
                    return code
            except Exception as e:
                logger.warning("[验证码] 轮询失败: %s", e)
            logger.info("[验证码] 等待中 (%d/%d)", i + 1, max_retries)
        raise TimeoutError(f"验证码获取超时 ({max_retries * interval}秒)")

    def release_phone(self, phone: Optional[str] = None) -> bool:
        """释放号码"""
        self._ensure_token()
        phone = phone or self.current_phone
        if not phone:
            return False
        try:
            data = self._request({
                "api": "cancelRecv",
                "token": self.token,
                "sid": self.sid,
                "phone": phone,
            })
            ok = str(data.get("code")) == "0"
            logger.info("[释放] %s -> %s", phone, "OK" if ok else data.get("msg", "?"))
            return ok
        except Exception as e:
            logger.error("[释放] %s 失败: %s", phone, e)
            return False

    def blacklist(self, phone: Optional[str] = None) -> bool:
        """拉黑号码"""
        self._ensure_token()
        phone = phone or self.current_phone
        if not phone:
            return False
        try:
            data = self._request({
                "api": "addBlacklist",
                "token": self.token,
                "sid": self.sid,
                "phone": phone,
            })
            logger.info("[拉黑] %s -> %s", phone, data.get("msg", "?"))
            return True
        except Exception as e:
            logger.error("[拉黑] %s 失败: %s", phone, e)
            return False
