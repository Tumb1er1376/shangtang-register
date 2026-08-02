"""
配置管理模块
从 .env 文件加载，支持环境变量覆盖
"""

import os
from pathlib import Path

ENV_PATH = Path(__file__).parent.parent / ".env"


def _load_dotenv(path: Path) -> None:
    """加载 .env 文件"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


class Config:
    """应用配置单例"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._loaded = False
        return cls._instance

    def __init__(self):
        if self._loaded:
            return
        self._loaded = True
        _load_dotenv(ENV_PATH)
        self.reload()

    def reload(self):
        """重新读取配置"""
        self.HZM_USER = os.environ.get("HZM_USER", "")
        self.HZM_PASS = os.environ.get("HZM_PASS", "")
        self.HZM_SID = os.environ.get("HZM_SID", "")
        self.HTTP_PROXY = os.environ.get("HTTP_PROXY", "") or os.environ.get("http_proxy", "")
        self.HTTPS_PROXY = os.environ.get("HTTPS_PROXY", "") or os.environ.get("https_proxy", "")
        self.SMS_ASCRIPTION = os.environ.get("SMS_ASCRIPTION", "1")
        self.SMS_PARAGRAPH = os.environ.get("SMS_PARAGRAPH", "")
        try:
            self.REGISTER_COUNT = int(os.environ.get("REGISTER_COUNT", "1"))
        except (ValueError, TypeError):
            self.REGISTER_COUNT = 1
        self.REGISTER_OUTPUT = os.environ.get("REGISTER_OUTPUT", "data/export.json")

    @property
    def proxies(self) -> dict:
        p = {}
        if self.HTTP_PROXY:
            p["http"] = self.HTTP_PROXY
        if self.HTTPS_PROXY:
            p["https"] = self.HTTPS_PROXY
        return p

    def save_to_file(self) -> None:
        """将当前配置写回 .env 文件"""
        lines = [
            f"HZM_USER={self.HZM_USER}\n",
            f"HZM_PASS={self.HZM_PASS}\n",
            f"HZM_SID={self.HZM_SID}\n",
            f"HTTP_PROXY={self.HTTP_PROXY}\n",
            f"HTTPS_PROXY={self.HTTPS_PROXY}\n",
            f"SMS_ASCRIPTION={self.SMS_ASCRIPTION}\n",
            f"SMS_PARAGRAPH={self.SMS_PARAGRAPH}\n",
            f"REGISTER_COUNT={self.REGISTER_COUNT}\n",
            f"REGISTER_OUTPUT={self.REGISTER_OUTPUT}\n",
        ]
        ENV_PATH.write_text("".join(lines), encoding="utf-8")
        for key in ("HZM_USER", "HZM_PASS", "HZM_SID",
                     "SMS_ASCRIPTION", "SMS_PARAGRAPH",
                     "REGISTER_COUNT", "REGISTER_OUTPUT"):
            val = getattr(self, key, "")
            if val:
                os.environ[key] = str(val)


config = Config()
