"""
疾驰短信接码平台 API 客户端
https://www.jichisms.com/

鉴权方式: HTTP Header fcToken (在平台"我的Token"页面获取)
API 文档: https://www.jichisms.com/index/index/apiDoc
"""

import time
import logging
from typing import Optional

import requests

logger = logging.getLogger("sensenova")

JICHI_API = "https://www.jichisms.com"


class SMSClient:
    """疾驰短信接码客户端，兼容 orchestrator 调用接口"""

    def __init__(
        self,
        base_url: str = JICHI_API,
        token: str = "",
        sid: str = "",
        ascription: str = "1",
        paragraph: str = "",
        proxies: Optional[dict] = None,
        **_kw,
    ):
        self.base_url = (base_url or JICHI_API).rstrip("/")
        self.token = token
        self.sid = str(sid)
        self.ascription = ascription or "1"
        self.paragraph = paragraph
        self.proxies = proxies
        self.current_phone: Optional[str] = None

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36",
        })
        if self.token:
            self.session.headers.update({"fcToken": self.token})

    def _post(self, path: str, data: dict, retries: int = 3) -> dict:
        """POST 表单请求，返回 JSON"""
        url = self.base_url + path
        last_exc = None
        for i in range(retries):
            try:
                r = self.session.post(
                    url, data=data,
                    proxies=self.proxies, timeout=30,
                )
                return r.json()
            except Exception as e:
                last_exc = e
                logger.warning("[疾驰] 请求失败(第%d次): %s", i + 1, e)
                time.sleep(2 ** i)
        raise RuntimeError(f"疾驰请求失败: {last_exc}")

    def _get(self, path: str, params: dict, retries: int = 3) -> dict:
        """GET 请求，返回 JSON"""
        url = self.base_url + path
        last_exc = None
        for i in range(retries):
            try:
                r = self.session.get(
                    url, params=params,
                    proxies=self.proxies, timeout=30,
                )
                return r.json()
            except Exception as e:
                last_exc = e
                logger.warning("[疾驰] 请求失败(第%d次): %s", i + 1, e)
                time.sleep(2 ** i)
        raise RuntimeError(f"疾驰请求失败: {last_exc}")

    def _ensure_token(self):
        if not self.token:
            raise RuntimeError("疾驰短信 fcToken 未设置，请到平台「我的Token」页面获取")

    def login(self) -> str:
        """兼容接口：疾驰使用 fcToken，无需登录，直接返回 token"""
        self._ensure_token()
        logger.info("[疾驰] Token 已配置")
        return self.token

    def get_phone(self) -> str:
        """获取手机号（官方引擎 /api/user/getPhone）"""
        self._ensure_token()
        data_payload = {"project_id": self.sid}
        if self.paragraph:
            data_payload["paragraph"] = self.paragraph
        if self.ascription:
            data_payload["ascription"] = self.ascription

        data = self._post("/api/user/getPhone", data_payload)
        if str(data.get("code")) != "1":
            raise RuntimeError(f"取号失败: {data.get('msg', data)}")

        # 成功时 phone 可能在 data.data.phone 或 data.phone
        d = data.get("data") or {}
        phone = d.get("phone") or data.get("phone")
        if not phone:
            raise RuntimeError(f"取号返回无 phone 字段: {data}")

        self.current_phone = phone
        logger.info(
            "[取号] %s (%s %s)",
            phone, d.get("sp", ""), d.get("phone_gsd", ""),
        )
        return self.current_phone

    def get_verify_code(
        self, phone: str, max_retries: int = 24, interval: int = 5
    ) -> str:
        """轮询获取验证码（/api/user/getVerifyCode），首次等 2 秒，之后每 5 秒一次"""
        self._ensure_token()
        for i in range(max_retries):
            # 首次等 2 秒让短信到达，之后按 interval 间隔
            if i == 0:
                time.sleep(2)
            elif i > 0:
                time.sleep(interval)
            try:
                data = self._post("/api/user/getVerifyCode", {
                    "project_id": self.sid,
                    "phone": phone,
                }, retries=2)
                if str(data.get("code")) == "1":
                    # 验证码在 msg 字段
                    code = str(data.get("msg", "")).strip()
                    # msg 可能是 "123456" 或包含其他文字，提取数字
                    import re
                    m = re.search(r"\d{4,8}", code)
                    if m:
                        code = m.group()
                    logger.info("[验证码] 第%d次查询 -> %s", i + 1, code)
                    return code
                else:
                    msg = data.get("msg", "")
                    # 请求过于频繁时跳过本轮回合
                    if "频繁" in str(msg) or "5秒" in str(msg):
                        logger.warning("[验证码] 频率限制，等待中 (%d/%d)", i + 1, max_retries)
                        continue
            except Exception as e:
                logger.warning("[验证码] 轮询失败: %s", e)
            logger.info("[验证码] 等待中 (%d/%d)", i + 1, max_retries)
        raise TimeoutError(f"验证码获取超时 ({max_retries * interval}秒)")

    def release_phone(self, phone: Optional[str] = None) -> bool:
        """释放号码（/api/user/releasePhone）"""
        self._ensure_token()
        phone = phone or self.current_phone
        if not phone:
            return False
        try:
            data = self._post("/api/user/releasePhone", {
                "project_id": self.sid,
                "phone": phone,
            })
            ok = str(data.get("code")) == "1"
            logger.info("[释放] %s -> %s", phone, "OK" if ok else data.get("msg", "?"))
            return ok
        except Exception as e:
            logger.error("[释放] %s 失败: %s", phone, e)
            return False

    def blacklist(self, phone: Optional[str] = None) -> bool:
        """拉黑号码 - 疾驰无独立拉黑接口，使用释放代替"""
        self._ensure_token()
        phone = phone or self.current_phone
        if not phone:
            return False
        logger.info("[拉黑] %s -> 使用 releasePhone 代替", phone)
        return self.release_phone(phone)

    def search_projects(self, keyword: str = "", page: int = 1, pagesize: int = 100):
        """搜索项目列表（/api/user/projects），返回原始 JSON"""
        self._ensure_token()
        params = {"page": page, "pagesize": pagesize}
        if keyword:
            params["project_name"] = keyword
        return self._get("/api/user/projects", params)
