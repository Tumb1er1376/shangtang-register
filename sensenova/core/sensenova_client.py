"""
商汤 SenseNova 平台注册客户端
封装完整 OAuth2 PKCE 注册流程, 统一 _get/_post 含 raise_for_status
"""

import re
import time
from typing import Optional
from urllib.parse import parse_qs, urlparse

import requests

from sensenova.utils.crypto import generate_pkce, random_str, decode_jwt_payload
from sensenova.utils.log import proxy as log


class SensenovaClient:
    """商汤 SenseNova API 客户端: OAuth2 PKCE -> SMS -> Register -> Token -> API Key"""

    AUTH_URL = "https://platform.sensenova.cn/oauth2/auth"
    TOKEN_URL = "https://platform.sensenova.cn/oauth2/token"
    API_BASE = "https://platform.sensenova.cn/lite/console/v1"
    IAM_BASE = "https://iam.sensecoreapi.cn/iam/authn/v1"
    IAM_IDP = "https://iam.sensecoreapi.cn/iam/idp/v1"

    def __init__(self, proxies: Optional[dict] = None):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
            "Accept-Language": "zh-CN",
            "Origin": "https://platform.sensenova.cn",
        })
        self.proxies = proxies
        self.code_verifier: Optional[str] = None
        self.code_challenge: Optional[str] = None
        self.state: Optional[str] = None
        self.login_challenge: Optional[str] = None
        self.token_code: Optional[str] = None
        self.access_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.user_id: Optional[str] = None
        self.tenant_id: Optional[str] = None

    def _get(self, url, timeout=15, **kw):
        r = self.session.get(url, proxies=self.proxies, timeout=timeout, **kw)
        r.raise_for_status()
        return r.json() if r.headers.get("content-type", "").startswith("application/json") else r

    def _get_raw(self, url, timeout=15, **kw):
        """GET 返回原始 Response (用于跟随重定向等场景)"""
        r = self.session.get(url, proxies=self.proxies, timeout=timeout, **kw)
        r.raise_for_status()
        return r

    def _post(self, url, timeout=15, **kw):
        r = self.session.post(url, proxies=self.proxies, timeout=timeout, **kw)
        r.raise_for_status()
        return r.json() if r.headers.get("content-type", "").startswith("application/json") else r

    # -------- OAuth2 / PKCE --------

    def init_oauth(self):
        self.code_verifier, self.code_challenge = generate_pkce()
        self.state = random_str(20)

    def fetch_login_challenge(self) -> str:
        self.init_oauth()
        params = {
            "response_type": "code", "client_id": "nova",
            "code_challenge_method": "S256", "code_challenge": self.code_challenge,
            "redirect_uri": "https://platform.sensenova.cn",
            "scope": "openid offline offline_access", "state": self.state, "lang": "zh-CN",
        }
        for attempt in range(3):
            r = self._get_raw(self.AUTH_URL, params=params, allow_redirects=True)
            qs = parse_qs(urlparse(r.url).query)
            challenge = qs.get("login_challenge", [None])[0]
            if challenge:
                self.login_challenge = challenge
                log.info(f"[Challenge] {challenge}")
                return challenge
            m = re.search(r'login_challenge["\']?\s*[:=]\s*["\']?([a-f0-9]+)', r.text)
            if m:
                self.login_challenge = m.group(1)
                log.info(f"[Challenge] from body: {self.login_challenge}")
                return self.login_challenge
            log.warning(f"未找到 challenge (attempt {attempt+1}/3)")
            time.sleep(1)
        raise RuntimeError(f"无法获取 login_challenge, URL: {r.url}")

    def check_challenge(self) -> bool:
        data = self._get(f"{self.IAM_BASE}/auth/checkChallenge",
                         params={"challenge": self.login_challenge}, timeout=10)
        valid = data.get("is_valid", False)
        log.info(f"[CheckChallenge] valid={valid}")
        return valid

    # -------- 短信验证 --------

    def _get_captcha(self) -> dict:
        """获取滑块验证码"""
        data = self._get(f"{self.IAM_BASE}/auth/getCaptcha", timeout=15)
        return data

    def _solve_captcha(self, bg_b64: str, block_b64: str) -> int:
        """识别滑块缺口 x 坐标"""
        import cv2
        import numpy as np
        import base64

        bg_bytes = base64.b64decode(bg_b64)
        block_bytes = base64.b64decode(block_b64)
        bg = cv2.imdecode(np.frombuffer(bg_bytes, np.uint8), cv2.IMREAD_COLOR)
        block = cv2.imdecode(np.frombuffer(block_bytes, np.uint8), cv2.IMREAD_UNCHANGED)

        bg_gray = cv2.cvtColor(bg, cv2.COLOR_BGR2GRAY)
        block_gray = cv2.cvtColor(block[:, :, :3], cv2.COLOR_BGR2GRAY)

        bg_edge = cv2.Canny(bg_gray, 100, 200)
        block_edge = cv2.Canny(block_gray, 100, 200)

        result = cv2.matchTemplate(bg_edge, block_edge, cv2.TM_CCOEFF_NORMED)
        _, _, _, max_loc = cv2.minMaxLoc(result)
        return max_loc[0]

    def _check_captcha(self, code_key: str, x: int) -> bool:
        """校验滑块验证码"""
        try:
            data = self._get(f"{self.IAM_BASE}/auth/checkCaptcha",
                             params={"code_key": code_key, "code_value": str(x)}, timeout=10)
            return bool(data.get("result", False))
        except Exception:
            return False

    def _solve_and_get_code_key(self, max_attempts: int = 5) -> str:
        """获取滑块验证码 code_key（自动识别+校验，失败重试）"""
        for i in range(max_attempts):
            captcha = self._get_captcha()
            code_key = captcha.get("code_key", "")
            if not code_key:
                time.sleep(0.5)
                continue
            x = self._solve_captcha(captcha["image"], captcha["block"])
            log.info(f"[Captcha] 尝试 {i+1}/{max_attempts} x={x}")
            if self._check_captcha(code_key, x):
                log.info("[Captcha] 验证通过")
                return code_key
            log.warning(f"[Captcha] 验证失败 (x={x})")
            time.sleep(0.15)
        raise RuntimeError("滑块验证码识别失败，请重试")

    def send_sms(self, phone: str, region_code: str = "86") -> str:
        # 第一次尝试不带验证码
        data = self._post(f"{self.IAM_BASE}/auth/nova/sendSmsCode",
                          json={"phone": phone, "region_code": region_code})
        self.token_code = data.get("token_code", "")
        if self.token_code:
            log.info(f"[SMS] 已发送 -> {phone}")
            return self.token_code

        # token_code 为空 → 需要滑块验证码
        log.info("[SMS] 需要滑块验证码，开始自动识别...")
        code_key = self._solve_and_get_code_key()

        # 带 code_key 重新发送
        data = self._post(f"{self.IAM_BASE}/auth/nova/sendSmsCode",
                          json={"phone": phone, "region_code": region_code, "code_key": code_key})
        self.token_code = data.get("token_code", "")
        if not self.token_code:
            raise RuntimeError(f"发送验证码失败(即使通过验证码): {data}")
        log.info(f"[SMS] 已发送(验证码) -> {phone}")
        return self.token_code

    def verify_sms(self, code: str) -> dict:
        return self._post(f"{self.IAM_BASE}/auth/nova/smsLogin", json={
            "token_code": self.token_code, "verify_code": code,
            "challenge": self.login_challenge,
        })

    # -------- 注册 --------

    def register(self, username: str, password: str) -> str:
        data = self._post(f"{self.IAM_BASE}/auth/nova/register", json={
            "token_code": self.token_code, "user_name": username,
            "password": password, "challenge": self.login_challenge,
        })
        redirect = data.get("redirect") or data.get("redirect_to") or data.get("redirect_uri") or ""
        if not redirect:
            raise RuntimeError(f"注册失败: {data}")
        log.info(f"[Register] 完成 -> {redirect[:60]}...")
        return redirect

    # -------- Token 交换 --------

    def exchange_code_for_token(self, redirect_url: str) -> dict:
        log.info("[Token] 获取授权码...")
        r = self._get_raw(redirect_url, allow_redirects=True)
        final = r.url
        qs = parse_qs(urlparse(final).query)
        if "code" in qs:
            return self._do_exchange(qs["code"][0])
        # 回到登录页带 login_verifier
        challenge = qs.get("login_challenge", [None])[0]
        if challenge and qs.get("login_verifier", [None]):
            data = self._post(f"{self.IAM_BASE}/auth/nova/smsLogin",
                              json={"token_code": self.token_code, "challenge": challenge})
            redirect2 = data.get("redirect") or data.get("redirect_to") or data.get("redirect_uri") or ""
            if redirect2:
                return self.exchange_code_for_token(redirect2)
        raise RuntimeError(f"无法获取授权码: {final[:200]}")

    def _do_exchange(self, code: str) -> dict:
        data = self._post(self.TOKEN_URL, data={
            "code": code, "redirect_uri": "https://platform.sensenova.cn",
            "code_verifier": self.code_verifier, "state": self.state,
            "client_id": "nova", "grant_type": "authorization_code",
        }, headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.access_token = data.get("access_token", "")
        self.refresh_token = data.get("refresh_token", "")
        if not self.access_token:
            raise RuntimeError(f"Token 交换失败: {data}")
        log.info("[Token] access_token 已获取")
        payload = decode_jwt_payload(self.access_token)
        if payload:
            ext = payload.get("ext", {})
            self.user_id = ext.get("user_id", "")
            self.tenant_id = ext.get("tenant_id", "")
        return data

    # -------- API Key --------

    def get_api_keys(self) -> list:
        keys = self._get(f"{self.API_BASE}/metered/api-keys",
            params={"key_type": "API_KEY_TYPE_TOKEN_PLAN", "page_size": 10},
            headers={"Authorization": f"Bearer {self.access_token}"}, timeout=10,
        ).get("api_keys", [])
        log.info(f"[API Keys] {len(keys)} 个 key")
        return keys

    def create_api_key(self, name: Optional[str] = None) -> dict:
        if not name:
            name = f"apikey-{time.strftime('%Y%m%d%H%M%S')}"
        headers = {"Authorization": f"Bearer {self.access_token}"}
        for method in ("post", "put"):
            try:
                fn = getattr(self.session, method)
                r = fn(f"{self.API_BASE}/metered/api-keys",
                    json={"displayname": name, "key_type": "API_KEY_TYPE_TOKEN_PLAN"},
                    headers=headers, proxies=self.proxies, timeout=15)
                r.raise_for_status()
                data = r.json()
                if data.get("api_key"):
                    return data
            except Exception as e:
                log.warning(f"[API Key] {method.upper()} failed: {e}")
                continue
        raise RuntimeError("无法创建 API Key")

    def get_user_info(self) -> dict:
        if not self.user_id:
            return {}
        try:
            return self._get(f"{self.IAM_IDP}/users/{self.user_id}",
                headers={"Authorization": f"Bearer {self.access_token}"}, timeout=10)
        except Exception:
            return {}

    def refresh_access_token(self) -> bool:
        if not self.refresh_token:
            return False
        try:
            data = self._post(self.TOKEN_URL, data={
                "refresh_token": self.refresh_token, "grant_type": "refresh_token",
                "client_id": "nova", "scope": "openid offline offline_access",
            }, headers={"Content-Type": "application/x-www-form-urlencoded"})
            if data.get("access_token"):
                self.access_token = data["access_token"]
                return True
        except Exception:
            pass
        return False
