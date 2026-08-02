# 商汤自动注册

全自动商汤科技（SenseNova）账号注册 + API Key 获取工具。

基于 [Wenaixi/shangtang-register](https://github.com/Wenaixi/shangtang-register) 改造，主要变更：

- 接码平台使用 [疾驰短信 (jichisms.com)](https://www.jichisms.com/)
- tkinter GUI 替换为 FastAPI + 单页 Web 面板
- Docker Compose 一键部署
- SSE 实时日志推送
- Web 面板在线配置疾驰短信 fcToken / 项目 SID

## 功能

- 自动完成 OAuth2 PKCE 全流程：获取 challenge → 取号 → 短信验证 → 注册 → Token → API Key
- 失败自动释放手机号并重试（最多 2 次）
- 支持指定卡号类型（1=移动, 2=联通）和号段筛选
- **Web 面板**：浏览器操作，实时 SSE 日志，账号管理（查看 / 复制 / 删除 / 导出）
- **CLI**：`--search` `--list` `--export` `--count` 子命令
- 每个账号独立存储：`data/shangtang-{username}.json`

## 快速开始

### Docker Compose 部署（推荐）

```bash
git clone https://github.com/Tumb1er1376/shangtang-register.git
cd shangtang-register

cp .env.example .env
# 编辑 .env 填入疾驰短信 fcToken / 项目 SID

docker compose up -d --build
```

默认监听 8000 端口，浏览器访问 `http://localhost:8000` 即可使用 Web 面板。

如需通过反向代理部署，将容器接入已有的 Docker 网络并在 Caddy / Nginx 中反代即可。

### 本地运行

#### 环境要求

- Python 3.12+
- `pip install fastapi uvicorn[standard] requests`

#### 配置

```bash
cp .env.example .env
# 编辑 .env 填入疾驰短信 fcToken / 项目 SID
```

疾驰短信账号注册：[https://www.jichisms.com/](https://www.jichisms.com/)

#### 启动 Web 面板

```bash
python web_app.py
# 或
uvicorn web_app:app --host 0.0.0.0 --port 8000
```

#### CLI 用法

```bash
python cli.py                    # 单次注册
python cli.py --count 5          # 批量注册 5 个
python cli.py --search 商汤      # 搜索疾驰短信项目
python cli.py --list             # 列出已有账号
python cli.py --export           # 导出所有 API Key（每行一个）
```

## 项目结构

```
shangtang-register/
  web_app.py                # FastAPI Web 后端（REST API + SSE）
  cli.py                    # CLI 入口
  templates/
    index.html              # 单页 Web 面板
  sensenova/                # 核心包
    config.py               # 配置管理（.env 读写）
    core/
      sms_client.py         # 疾驰短信 API 客户端：取号 / 验证码 / 释放
      sensenova_client.py   # 商汤 OAuth2 PKCE 客户端
      orchestrator.py       # 注册编排器：8 步流程 / 重试 / 持久化
    utils/
      crypto.py             # PKCE / 密码生成 / JWT 解码
      log.py                # 日志 + 事件回调
  Dockerfile
  docker-compose.yaml
  .env.example              # 配置模板
  api_documentation.md      # 疾驰短信 API 文档
```

## 注册流程

| 步骤 | 操作 | API |
|------|------|-----|
| 1 | PKCE + login_challenge | OAuth2 Auth Page |
| 2 | 疾驰短信取号 | POST /api/user/getPhone |
| 3 | 发送短信验证码 | IAM sendSmsCode |
| 4 | 轮询验证码（5s×20） | POST /api/user/getVerifyCode |
| 5 | 短信校验 | IAM smsLogin |
| 6 | 注册账号 | IAM register |
| 7 | OAuth2 授权码 → Token | oauth2/token |
| 8 | 获取 API Key | /metered/api-keys |
| + | 释放手机号 | POST /api/user/releasePhone |

## 配置项

| 变量 | 说明 | 示例 |
|------|------|------|
| JC_TOKEN | 疾驰短信 fcToken | your_fctoken |
| JC_SID | 疾驰短信项目 ID | 12345 |
| HTTP_PROXY | HTTP 代理 | http://127.0.0.1:10801 |
| HTTPS_PROXY | HTTPS 代理 | http://127.0.0.1:10801 |
| SMS_ASCRIPTION | 卡号类型 | 1=移动, 2=联通 |
| SMS_PARAGRAPH | 号段筛选（可选） | 138 |
| REGISTER_COUNT | 注册数量 | 1 |

## 接码平台

本项目使用 [疾驰短信 (jichisms.com)](https://www.jichisms.com/) 接码平台。

注册疾驰短信账号后，在「我的Token」页面获取 fcToken，在项目列表中找到商汤科技对应项目的 ID，填入 `.env` 文件即可。

## 致谢

- 原项目：[Wenaixi/shangtang-register](https://github.com/Wenaixi/shangtang-register) — HAR 逆向分析和 OAuth2 PKCE 注册流程
- 接码平台：[疾驰短信](https://www.jichisms.com/)

## License

MIT
