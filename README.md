# 学管助手 · 第一期

本机运行的可交互邮件采集助手。Python 3.11+，建议 Windows 11 / macOS / 有桌面的 Linux。首版为单操作员、单进程部署，SQLite 存储；不需要 Docker 或 Redis。

## 已实现

- 学生邮箱配置、密码加密保存、学生浏览器上下文隔离。
- 定时和手动采集，单邮箱禁止重复任务，可取消，服务重启标记中断。
- 自动填账号密码；遇双因素验证或未知登录页面，工作台提醒并显示截图。
- 在原浏览器窗口人工接管，手机批准后自动检测邮箱界面继续；验证超时报告失败。
- 认证状态加密保存与复用；支持学校 SSO 受信任域名配置。
- 邮件内容版本去重、模型结构化抽取、原文证据验证、北京时间转换。
- 原文查看、提醒编辑、审核、复制微信提醒、手动标记已发送和审计记录。

## Windows 启动

在解压后的项目目录执行 PowerShell：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -m playwright install chromium
# 可选：配置模型，否则邮件进入人工审核
$env:LLM_API_KEY="你的模型API密钥"
$env:LLM_BASE_URL="https://api.openai.com/v1"
$env:LLM_MODEL="你的可用模型名称"
.\.venv\Scripts\python.exe run.py
```

打开 http://127.0.0.1:8000 。首次启动自动生成 `data/admin.token`，读取其内容并填入工作台顶部的管理员令牌框。令牌只保留在页面内存，不写入浏览器 localStorage。

```powershell
Get-Content data/admin.token
```

添加学生，先“立即采集”。添加后定时检查会在 15 秒内开始。遇到验证，在工作台点“查看验证页面”，在手机批准登录；验证码、账号选择、学校 SSO 额外步骤等在已弹出的本机浏览器窗口操作。系统会继续观察同一窗口，直到邮箱认证标记出现，不需要点击“已验证”。

`HEADLESS` 默认为 false：必须在有桌面的机器运行，不能用无桌面的服务器直接完成交互接管。服务器远程浏览器接管不在此版本中。

## Linux / macOS

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/playwright install chromium
# Linux 缺少系统依赖时按 Playwright 提示安装依赖
.venv/bin/python run.py
```

## 邮箱适配：首次真实账号必须联调

Outlook 配置提供起始选择器，不保证所有学校、语言、租户或新版界面相同。本项目未使用真实学生账号验证 Outlook；测试使用本地模拟邮箱。其他邮箱选择 generic 并配置 CSS 选择器。当前采集收件箱已渲染的前 N 行，不翻页、不滚动加载全历史，不采集附件；打开邮件可能将其标记为已读；Outlook 会话视图可能呈现整个会话，应在真实联调时关闭会话视图或修改适配器以定位单封消息。

`authenticated_selector` 必须唯一标识登录成功的邮箱内容，不能选择登录页也存在的元素。`body_selector` 必须只定位对应邮件正文，`subject_selector` 必须只定位该邮件主题。建议首次仅采集 3 封，并逐条核对原文，确认正确后提高数量。

| 配置 | 含义 |
| --- | --- |
| login_url / inbox_url | HTTPS 登录入口、收件箱入口 |
| allowed_hosts | 顶层及 iframe 导航允许的精确域名，包括学校 SSO；不支持通配符 |
| username_selector / password_selector | 可见账号、密码输入框 |
| submit_selector | 提交按钮；无按钮时发送 Enter |
| authenticated_selector | 当前邮箱已认证的 DOM 标记 |
| message_selector | 收件箱单封邮件行 |
| body_selector / subject_selector / sender_selector | 正文、主题、发件人；发件人可不填 |
| mfa_selector / error_selector | 验证界面、登录错误标记 |
| empty_selector | 确认邮箱为空的标记；没有此标记时无邮件行会报适配失败 |
| timezone | 学校 IANA 时区，例如 Europe/London、Australia/Sydney |
| max_messages | 本次读取的已渲染邮件行数上限，默认 30 |
| interval_minutes | 自动检查间隔，默认 60 |
| verification_timeout | 登录与人工验证总等待时间，默认 300 秒 |

如果域名跳转被拒绝，先确认该域名确实是学校 SSO，再加入 allowed_hosts。账号和密码各自动提交一次；后续挑战人工在原窗口处理。程序不绕过验证码或双因素认证。

## 模型与审核

支持兼容 `/chat/completions` 的模型接口，要求 JSON object 输出。输入包含邮件主题、正文和学校时区；不包含邮箱密码或 Cookie。调用失败时原文已保存，任务报告失败；重新采集同一邮件会重试提取。没有密钥时生成“需人工处理”事项。模型提取的证据必须逐字存在于原文；日期和事实仍需人工核对。首版未实现跨来源语义合并或自动变更冲突判定。

工作台审核通过后可复制微信提醒，手动发送，再标记“已发送”。编辑草稿会重新进入待审核；已发送记录不能修改。不自动登录微信或自动发送消息。

## 安全与运行边界

- 仅监听 127.0.0.1；不要直接对公网开放。API 使用管理员令牌、跨站写请求校验及禁止缓存。
- 密码、邮件正文和浏览器 storage_state 使用 Fernet 加密。`data/secret.key` 是解密密钥，丢失无法恢复；备份时保护整个 data 目录。密钥与密文同机存放只保护脱离密钥的文件泄露，不防御电脑被完整控制。
- 登录截图只在内存保存，任务结束即删除。请勿共享截图或令牌。Windows 文件权限由所在目录 ACL 决定，应仅允许当前用户访问 data。
- 并行浏览器默认 3；等待验证占用其中一个位置，其他邮箱可使用剩余位置；满额任务排队。
- 只运行一个进程，不使用 uvicorn --reload / 多 workers。重启中断的任务需重新采集。
- 首版无教务系统采集、课件下载、辅导预约和学习计划，这些为后续阶段。

## 可复现依赖

`requirements-lock.txt` 记录测试时安装的依赖版本，可先安装该文件再使用 `pip install -e . --no-deps`。`TEST_REPORT.md` 记录已测试和未测试范围。

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

测试包括 API 认证、跨站写保护、密码加密、状态流转、修改后重新审核、重复采集、提取失败后重试、重启恢复，以及真实 Chromium 上的模拟邮箱 MFA 等待、批准后恢复、认证状态复用、学生隔离、验证超时。模拟批准按钮只存在于 tests 的测试页面，不用于真实账号。
