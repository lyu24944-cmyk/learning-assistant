# 截止提醒

本地就能看的竞赛截止时间线。不需要飞书账号，不登录来源网站，也不抓网页。已核对的记录在 `data/records.csv`，在仓库根目录执行：

```bash
python3 -m src.timeline
python3 -m src.timeline --today 2026-11-02
python3 -m src.reminder --today 2026-11-02
```

需要 Python 3.11 及以上。命令行提醒只用标准库。网页另需 `requirements.txt` 里的 FastAPI。默认基准日时区是 Asia/Shanghai。Windows 如果没有系统时区数据，安装依赖里的 `tzdata`；仍然找不到时区时，才回退到本机日期。这一版先不上线。

下面这些旧包都不要再覆盖到已有数据库的目录：`learning-assistant-update-2026-11-07.zip`、`learning-assistant-update-migration-fix-2026-11-07.zip`、`learning-assistant-update-windows-fix-2026-11-07.zip`、`learning-assistant-update-windows-encoding-fix-2026-11-07.zip`。请只用带 `utf8-protocol-fix` 的包。它保留旧库升级。测试启动的 Python 子进程用 UTF-8 写标准输出，npm/node 的管道也按 UTF-8 读取，所以 Windows 默认 GBK 时 CLI 中文仍是原文。这不改用户的区域设置。真正非法的字节在当前线程报错，不用转义把中文改掉。不要删除或覆盖 `data/app.sqlite`。

`timeline` 只列出当前信息类型、核对状态为「已核对」、截止日期是公历 `YYYY-MM-DD` 的行，按日期再按标题排序。还没到的写成「剩余N天」（含「剩余0天」），已经过去的写成「已过期」。相对日期在表里是「未找到」，不会出现。

`reminder` 仍只看今天起 7 天（`reminder_within_days`，没有改）。2026-11-02 这四条都在窗口外，所以输出「今日不发送」。11 月 13 日那条从 2026-11-06 起进入窗口，可以这样预览：

```bash
python3 -m src.reminder --today 2026-11-08
```

两条命令都只在终端打印，不发邮件、不发微信。网页登录后的「今日提醒」用同一套规则：基准日省略时是 Asia/Shanghai 的今天，只含已核对且落在 7 天内的公历日期。2026-11-02 页面上应写「今日不发送」。把基准日改成 `2026-11-08` 后，只会出现 11 月 13 日那一条。邮件提醒以后再做，这一截不接外部邮件商。

识图在 http://127.0.0.1:5173/recognize 。图片只在这台电脑上解码，不上传到第三方，也不需要识别账户或密钥。Windows 优先使用系统里已经装好的简体中文 `Windows.Media.Ocr`（本机核对到的语言是 `zh-Hans-CN`），不必安装 Tesseract。英语识别语言没有安装，页面会写明不保证中英混排和网址；这不是识别质量结论，要等 Windows 上对合成图实际跑过。Linux 仍用本机 Tesseract（`chi_sim+eng`）。可以用 `LEARNING_ASSISTANT_OCR_PROVIDER=windows` 或 `tesseract` 指定引擎；指定了却不可用就失败，不偷偷换成另一个，也不伪造识别文字。官方 Windows 安装包 `tesseract-ocr-w64-setup-5.5.3.20260724.exe` 的摘要能对上发布页，但 Authenticode 证书时间校验失败、时间戳链不受信任，不能执行。5.5.4 修复了 17 项安全公告，却没有 Windows 安装包。引擎上限按整数比较：1700 不大于 10000。读不出上限时，不会把图当成超限。云端 Linux 跑不到 `Windows.Media.Ocr`，失败诊断写在 UTF-8 的 JSON 里，不靠控制台打印中文。Windows 简体引擎可能在汉字之间插入空格，并把「11月」读成「且月」、把网址标点丢掉。识别原文按引擎输出原样保存。匹配只删除汉字和中文标点之间的空格，不把错字改对，也不补网址标点。报名或作品提交这几个字还在、但日期不是公历时，候选保留，截止日期写「未找到」，不提醒。要改日期必须你在确认时自己写成公历并标成已核对。找不到可用引擎时，页面写明识别不可用，可以到「添加」页人工补录，那不是识别结果。合成海报测试会找真实中文字体；字体集合选择简体面，单个 TTF/OTF 用第 0 面。也可以设置 `LEARNING_ASSISTANT_CJK_FONT`。找不到字体时测试失败，不跳过。

上传只收能解码的 PNG、JPEG、GIF、WEBP、BMP，单张不超过 8MB。原图按账号分开存在 `data/private/`，不进 git，也不走公共静态目录。识别出的名称、报名截止、作品提交截止、日期原句、报名条件、主办方和链接都只是草稿，字段可以改。没有年份、相对日期或几种日期对不上时写成「未找到」，不会补今年。图里没有官网就是「未找到」，原图本身留作来源证据。只有你把某条改成「已核对」并且截止日期是公历 `YYYY-MM-DD`，它才会进入提醒。确认前不写入我的记录，上传也不会变成已报名或已提交。确认后原图进入资料夹，并和私人记录互相能找到；本周复盘仍保留你手写的完成、卡住和下周计划，只额外记下「识图新增 / 整理了资料」或「人工补录，不是识别结果」。

上传不再等识别做完。`POST /api/image-drafts` 收下原图、写一行 `queued` 就返回 **202**，识别在后台两个工人线程里跑，前端每 800 毫秒问一次 `GET /api/image-drafts/{id}`，间隔由响应里的 `poll_after_ms` 给。所以传图的时候时间线、提醒、其它页面都还能用。入口有三个，走同一条路：文件选择框、截图后直接 Ctrl+V、把图片拖进页面。在输入框里粘文字不会被抢走。同一张图（`user_id` 加 `sha256` 相同）第二次上传直接复用上一次的结果，`reused` 为 `true`，不会再起一次识别进程；还在排队或识别中的同一张图也会复用那条在途草稿，连点两下只识别一次。识别失败可以点「重新识别」，最多 3 次；也可以在失败后人工补录。草稿还在排队或识别中时，确认接口返回 409 `ocr_not_ready`，不会把没识别完的东西写进记录。服务重启时，所有卡在 `running` 的任务标成失败并写明「服务重启，识别中断」，还在排队的重新入队；排不下的也标失败，不静默丢。运行中还有一道看门狗，每 60 秒把卡在识别中超过 180 秒的任务标失败。队列在进程内存里，重启会丢任务——这是当前接受的缺口，多进程部署前必须换掉，见 [docs/ocr.md](docs/ocr.md)。

网页骨架读同一份 CSV。一个终端启动 API，另一个终端启动页面：

```bash
pip install -r requirements.txt
python3 -m uvicorn src.api:app --host 127.0.0.1 --port 8010
```

```bash
cd frontend
npm install
npm run dev
```

如果后端换过端口（Windows 上 8010 可能落在系统保留端口段，用 `netsh interface ipv4 show excludedportrange protocol=tcp` 查），让前端代理跟着走：`LEARNING_ASSISTANT_API=http://127.0.0.1:8090 npm run dev`，或把这行写进 `frontend/.env.local`。PowerShell 里用 `$env:LEARNING_ASSISTANT_API="http://127.0.0.1:8090"; npm run dev`。

浏览器打开 http://127.0.0.1:5173/query 。先注册或登录，登录后默认就是查询页：左边是导航，右边两列分别是「今日提醒」和「全部已核对」。录入打开 http://127.0.0.1:5173/add 。提醒设置打开 http://127.0.0.1:5173/settings/reminders ：基准日留空表示今天，提前几天决定查询页的提醒窗口；邮件开关只保存偏好，邮件触达未接通，不会发信。查询页另填的基准日只临时覆盖这一次。我的记录打开 http://127.0.0.1:5173/mine ，只看自己录入的行，可把状态改成未处理、已报名、已交或放弃。本周复盘打开 http://127.0.0.1:5173/review ，填写本周完成、卡住和下周目标，服务端只按原文拼总结和计划，不编造日期。课程索引打开 http://127.0.0.1:5173/courses ，资料夹打开 http://127.0.0.1:5173/materials 。资料可以挂到自己的一门课。这一轮只做索引，不代写作业，也不编造课程内容。未登录访问 `/api/timeline` 或 `/api/reminders` 会得到 401。账号存在 `data/app.sqlite`，密码只存哈希，不接 Clerk 或 Auth0。登录后看到的 4 条竞赛是共享只读目录，日期仍来自 `data/records.csv`，网页不能改这张表。在官网核对后，用「添加竞赛（我的）」只抄原文：有公历日期就写成 `YYYY-MM-DD`，相对日期写成「未找到」，不要自己换算。私人写入带 `user_id`，别人看不到。框架取舍和模块排期见 [docs/stack-and-scope.md](docs/stack-and-scope.md)。

## 现在表里有什么

`templates/sources.csv` 有 3 个公开竞赛网页（不需要登录，录入方式是只手动粘贴）。`data/records.csv` 有 4 条已核对记录：

| 标题 | 截止日期 | 来源 |
| --- | --- | --- |
| 计挑赛人工智能挑战赛-区域赛作品提交截止 | 2026-11-13 | 计挑赛人工智能页 |
| APMCM2026英文赛项-报名截止 | 2026-11-25 | 校级通知页 |
| 计挑赛程序设计挑战赛-区域赛报名截止 | 2026-11-25 | 计挑赛程序设计页 |
| 计挑赛程序设计挑战赛-区域赛考试 | 2026-11-29 | 计挑赛程序设计页 |

`templates/records.example.csv` 仍是脱敏示例，用来核对 10 列和「草稿 / 未找到不提醒」。默认时间线和提醒不读它。

校验：

```bash
python3 -m src.validate_records data/records.csv --min-rows 3
python3 -m src.validate_records
```

第二条检查示例表至少 10 行。真实记录还不到 10 条，不要用 `--min-rows 10` 去卡现在的 `data/records.csv`。

信息类型默认是竞赛，只在 `config/settings.json` 改：

```json
"info_type": "竞赛"
```

可改成 `实习` 或 `考研`，必须是 `allowed_info_types` 里的词。本周仍然只录入这一类。

## 下一条怎么加

按 [docs/sources.md](docs/sources.md) 看来源，按 [docs/first-notice.md](docs/first-notice.md) 把原文手工追加到 `data/records.csv`。不要发明日期。原文只有「本周五」这类说法时，截止日期写「未找到」，是否参与提醒写「否」。

## 可选：飞书

飞书多维表格和每天 08:00 的工作流是旁路，不是本地助手的前提。要导出时：

```bash
python3 -m src.export_feishu --source data/records.csv --output data/feishu-import.csv
```

不带参数时，`export_feishu` 仍导出示例表 `templates/feishu-import.example.csv`。点选步骤见 [docs/feishu-automation.md](docs/feishu-automation.md)。预览示例表的 7 天窗口时要写明文件，否则会读真实记录：

```bash
python3 -m src.reminder --records templates/records.example.csv --today 2026-11-08
```

## 部署前要设的

- `LEARNING_ASSISTANT_SECURE_COOKIES=1`：站点在 HTTPS 反向代理后面、代理又没传 `X-Forwarded-Proto` 时，给会话 Cookie 加 Secure。本地 `http://127.0.0.1` 不要设，否则浏览器不回传登录态。设成 `0` 可以强制关闭。
- `LEARNING_ASSISTANT_SECRET`：重置令牌的签名密钥，至少 32 个字符。不设就自动生成 `data/secret.key`。这个文件不进 Git；丢了只会让未用的重置链接失效，数据不丢。
- `LEARNING_ASSISTANT_WEB_BASE`：重置链接的前缀，默认 `http://127.0.0.1:5173`。部署到服务器时改成公网地址，否则打印出来的链接指向本机。
- `LEARNING_ASSISTANT_CSRF=0`：只关闭 CSRF token 校验，Origin 校验仍开。给旧前端留一个版本的退路，确认前端都更新后删掉。
- `LEARNING_ASSISTANT_ALLOWED_ORIGINS`：逗号分隔的额外 Origin，例如 `https://app.example.com`。同源请求不用写；反向代理没开 `--proxy-headers` 时用它兜底。
- `LEARNING_ASSISTANT_RATE_LIMIT=0`：关掉全部频率限制，方便压测或演示。默认是每个 IP 每小时 30 次注册、同一 IP 加同一邮箱 15 分钟内 10 次登录失败、单 IP 15 分钟 60 次登录失败、单 IP 每小时 10 次重置、单用户 15 分钟 5 次改密码失败。计数在 `rate_limits` 表里，重启还在，多进程共享；阈值用对应的 `LEARNING_ASSISTANT_*_LIMIT` 改。
- `LEARNING_ASSISTANT_OCR_QUEUE_SIZE`：识图等待队列长度，默认 20。满了上传直接返回 503，让用户稍后再试，而不是把内存堆满。
- `LEARNING_ASSISTANT_OCR_WORKERS`：识别工人线程数，默认 2。每个工人会起一个 PowerShell（Windows）或 tesseract 进程；调大之前先看内存和 SQLite 写锁。
- `LEARNING_ASSISTANT_OCR_UNFINISHED_LIMIT`：单用户同时排队或识别中的草稿数，默认 2。超了返回 429 `draft_busy`。这道闸比队列上限更重要——只靠队列，一个人狂点就能吃掉全部 20 个位。
- `LEARNING_ASSISTANT_UPLOAD_LIMIT`：单用户每小时上传次数，默认 20，计数在 `rate_limits` 表里。防脚本刷。
- `LEARNING_ASSISTANT_OCR_REAP_INTERVAL`：看门狗扫描间隔，默认 60 秒。它把卡在「识别中」超过 180 秒的任务标成失败，避免一个卡住的任务永久占着工人。
- `LEARNING_ASSISTANT_OCR_STATUS_TTL`：引擎状态缓存秒数，默认 300。设 `0` 关掉缓存，调试「装了语言包但页面还说不可用」时用。
- `LEARNING_ASSISTANT_OCR_SYNC=1`：退回「请求里当场识别完再回话」的老行为。给测试和回滚用；开着的时候队列不参与，行为与批次 2 一致。
- 账号相关的运维命令：`python -m src.accounts reset-password <email>` 生成 30 分钟一次性链接；`mark-verified <email>` 标记邮箱已验证；`sessions <email>` 列出会话。细节和威胁模型见 [docs/auth.md](docs/auth.md)。
- 数据库连接设了 5 秒 busy_timeout；建表和迁移只在每个进程启动时跑一次（`src.db.init_db`），连接不再执行 DDL。SQLite 仍是单文件单写者，只允许 1 个写进程；要 2 个写进程时，改 `DATABASE_URL` 换 Postgres。
- 迁移和备份：`python -m alembic upgrade head` 升级；老库第一次启动会自动备份到 `data/backups/pre-migration-*.sqlite` 再接管。日常备份用 `python scripts/backup_db.py`，默认保留 7 份并跑 `integrity_check`。
- 不要开 WAL。2026-11-09 实测 4 写 4 读 × 3 轮，WAL 每轮都出现 `attempt to write a readonly database`；DELETE 模式 0 错误、800 行全写入。`LEARNING_ASSISTANT_SQLITE_JOURNAL` 只留给复现实验，不要在生产设成 `WAL`。
- 联调脚本读 `LEARNING_ASSISTANT_BASE`，默认 `http://127.0.0.1:8010`。

## 其他页面

| 文件 | 内容 |
| --- | --- |
| [docs/sources.md](docs/sources.md) | 已填的 3 个竞赛来源，以及以后怎么加 |
| [docs/first-notice.md](docs/first-notice.md) | 已录入的 4 条，以及下一条怎么贴 |
| [docs/roadmap-mid-nov.md](docs/roadmap-mid-nov.md) | 11 月中旬给别人用的计划 |
| [docs/stack-and-scope.md](docs/stack-and-scope.md) | 为什么用这套框架，以及各模块何时做 |
| [docs/auth.md](docs/auth.md) | 账号、会话、CSRF、重置密码和限流 |
| [docs/one-pager.md](docs/one-pager.md) | 一页纸方案 |
| [docs/feishu-fields.md](docs/feishu-fields.md) | 多维表格字段、示例表 |
| [docs/feishu-automation.md](docs/feishu-automation.md) | 可选的飞书工作流 |
| [extract/PROMPT.md](extract/PROMPT.md) | 粘贴到 Coze / Dify 的提示词 |
| [docs/exam.md](docs/exam.md) | 8 段抽取考试怎么评分 |
| [docs/checklist.md](docs/checklist.md) | 验收进度 |

抽取考试仍是本地纯函数，不读网络：

```bash
python3 -m src.score_exam
python3 -m src.score_exam --predictions data/exam/coze-output.json
```

## 日期规则

相对日期写成「未找到」，日期原句留着。抽取结果固定是草稿，且不参与提醒。只有「已核对」并且「是否参与提醒」为「是」的公历日期，才会进入今天起 7 天的提醒窗口。时间线同样只要已核对的公历日期，并标出「已过期」或「剩余N天」。抽取代码不发消息、不改表、不读网络。

本月不做个人微信机器人，也不把定时抓取当成默认录入。每周复盘放在上线后一周，课程问答放在 12 月上旬，实习和考研等竞赛稳定后再做。顺序见 [docs/roadmap-mid-nov.md](docs/roadmap-mid-nov.md)。
