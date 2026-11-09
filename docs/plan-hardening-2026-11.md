# 加固与识图粘贴计划（v2）

日期：2026-11-09
范围：`_analysis/learning-assistant`
状态：批次 0/1/2/3 已执行；批次 4-6 待确认。
修订：v1 17:45；v2 18:05，按「D1 假分歧、D2 拆需求、D3 部署决定、D4 删需求、批 4 改规约」重写。

## 执行记录（2026-11-09 18:20）

- 备份：`_analysis/learning-assistant.backup-2026-11-09-1756/` 和同名 `.tar.gz`；`diff -r` 无差异，`PRAGMA integrity_check=ok`，用户 1 行、识图草稿 1 行。
- 批次 0 完成：git 基线 `5d42351`、CI、`pyproject.toml`、依赖拆分、`scripts/backup_db.py`、request_id JSON 日志；后端 60 个测试 + 前端 6 个测试通过。
- 批次 1 完成：`src/db/` 包、11 张表 SQLAlchemy 模型、Alembic `0001`/`0002`、DDL 移出连接、老库自动备份并接管；真实库已升到 `0002`，用户/草稿行数不变。
- WAL 实验：4 写 4 读 × 3 轮，WAL 每轮都出现 `attempt to write a readonly database`（最多只写入 400/800 行）；DELETE 0 错误、800 行全写入。默认保持 DELETE。
- 批次 2 完成（19:45）：迁移 `0003`、`users` 两列、`sessions` 六列、`rate_limits` 表；CSRF + Origin；14 天绝对过期 / 7 天空闲过期 / 登录换号；数据库固定窗口限流；CLI `reset-password`/`mark-verified`/`sessions`；改密码接口；前端最小 `api/client.js`、`/reset`、`/settings/account`。真实库已升到 `0003`，用户/会话/草稿行数不变，`foreign_key_check` 为空。全量测试通过（含前端构建）。
- 顺带修批次 1 的洞：`session()` 的 SQLAlchemy 连接现在也跑 `busy_timeout` / `foreign_keys` / `synchronous`。
- 批次 3 完成（21:20）：迁移 `0004`（`image_drafts` 六列）、`src/ocr_queue.py`（有界队列 + 2 个 daemon 工人 + 看门狗）、`create_draft` 拆成 `enqueue_draft` + `run_ocr`、上传改 202 且路由改成同步 `def`、`/retry`、`confirm` 守卫、`sha256` 去重（含在途复用）、三道准入闸、`queue_position`、重启恢复、引擎状态缓存 300 秒、前端轮询。实测上传 83ms 返回 202，识别 721ms 完成，上传期间时间线 62 次请求中位 11ms / 最大 32ms / 0 错误。详细说明见 `docs/ocr.md`，计划与执行记录见 `docs/plan-batch3-ocr-async.md`。
- 未做：71 条 SQL 重写、批次 4-6。

## 0. 结论

先把两件事解决：**上传识图会卡住整个服务**，以及**每次建立数据库连接都跑一遍建表和迁移**。这两件是当前卡顿和锁库问题的放大器。

真正的成本不在 D1-D4。四个选项加起来 1-2 人日，占原估算的 5%。成本在批 1 的 71 条 SQL 和批 4 的 1282 行 JSX，原计划都做成了 big-bang。v2 把这两处也改成渐进：

- 批 1 只建模型和 Alembic 迁移，**不重写 71 条 SQL**；谁被改到谁换，停在任何一个模块都是自洽状态。
- 批 4 只建 `api/client.js`、`useAsync`、`AuthContext` 骨架，**不一次性拆 1282 行**；新页面按新结构写，旧页面留在原地直到被改到。
- Ctrl+V 就是第一个按新结构写的页面，不再等整批重构完成。

顺序：安全网 → 数据层 → 账号安全 → 识图异步 → 前端骨架 → 契约与可观测 → Ctrl+V。

---

## 1. 现状核对

### 1.1 上传识图不是「占一个 worker」，是卡住整个事件循环，最长约 60 秒

`src/api.py:362-372` 的上传接口是 `async def`，第 370 行直接调用同步的 `create_draft()`。FastAPI 不会把 `async def` 里的同步调用丢进线程池，所以识别期间整个事件循环停住，`/api/health`、`/api/timeline` 和别人的请求一起排队。

`src/image_drafts.py:27-29` 先调 `engine_status()`；`src/ocr.py:33-46` 转到 `src/ocr_windows.py:141-180`，起一次 PowerShell（`_run`，`timeout=30`）。随后 `transcribe()` 再起一次 PowerShell（`src/ocr_windows.py:255-263`，同样 `timeout=30`）。最坏是 30 + 30 = 60 秒，不是 30 秒。

`engine_status()` 没有缓存，所以每次上传、每次打开识图页、每次刷新状态都会再起一个 PowerShell。

### 1.2 其余问题与证据

| 你提的问题 | 证据 | 实际影响 |
| --- | --- | --- |
| 前端单文件、无状态管理 | `frontend/src/App.jsx` 1282 行；8 个页面组件（302、396、461、627、693、822、922、1069 行起）加 `Card`、`readJson`、`normalizeDeadline` 全在一个文件；每个页面各自 `fetch`、各自 `error`、各自 loading；`loadViews` 每次改数据都重拉 3 个接口 | 加第 9 个页面要在一屏放不下的文件里找位置；同一份请求逻辑重复 8 次；没有缓存和失效，改一条记录要重拉整页 |
| 没有 ORM 和迁移框架 | `src/db.py:11-120` 内联 11 张表的 DDL；`src/migrate.py:132-136` 的 `_ensure_column` 就是手写 `ALTER TABLE`；没有版本表 | 加字段靠人记得；表多了会漏；迁移是否跑过没有记录；`connect()` 每次执行 `executescript` + `migrate()`（`src/db.py:141-156`），27 个调用点都在重复做 DDL |
| SQLite 单写者 | `src/db.py:130-139` 只设了 `busy_timeout=5000`，没有 WAL；`docs/stack-and-scope.md` 自己写明多进程会锁库 | 多开一个 API 进程就可能 `database is locked`；当前「每连接建表」还会放大写冲突 |
| OCR 阻塞请求 | `src/api.py:362-372`；`src/ocr_windows.py:255-263` | 见 1.1；并发两张图就会把服务拖死 |
| 缺工程化配套 | 没有 `.github`、没有 `pyproject.toml`、没有 `logging`、没有 `@app.exception_handler`；`src/store.py:218-238` 的查询没有 `LIMIT`；`src/api.py:234-239` 全量返回 | 出错只有一句 `detail`；慢请求无法定位；记录多了页面会越来越慢；目录当前还不是 git 仓库，重构没有自动回归 |
| 账号安全只做了一半 | `src/accounts.py` 没有邮箱验证、没有找回密码；`sessions` 表没有 CSRF、没有过期时间、没有设备信息；`src/ratelimit.py:1-5` 自己写明计数只在进程内 | 邮箱可以乱填；忘记密码只能放弃账号；跨站写请求只靠 SameSite；多进程时限流失效 |
| 无 CI | 目录不是 git 仓库 | 62 个测试只能手动跑；任何一批改动都没有自动门禁 |

### 1.3 已经做对的，不要改坏

- 密码用 scrypt + 每用户随机盐，比较用 `hmac.compare_digest`（`src/accounts.py:24-46`）。
- Cookie 是 HttpOnly + SameSite=Lax，HTTPS 下可加 Secure（`src/api.py:152-176`）。
- 上传只接受真实可解码的位图，限制 8MB / 8MP / 6000px，拒绝 SVG/PDF/ZIP（`src/bitmap.py:38-61`）。
- 私有文件按 `user_id` 分目录，`private_file` 防路径穿越（`src/bitmap.py:76-98`）。
- 确认草稿是单事务，重复确认幂等（`src/image_drafts.py:130-188`）。
- 日期规则集中在 Python（`src/timeline.py`、`src/reminder.py`、`src/validate_records.py`），网页不重算。
- 现有 62 个测试全过，包含一次前端生产构建。

这些是这次加固的边界：可以换实现，不能换行为。

### 1.4 一条已知教训：WAL 不是万能开关

2026-11-09 已经实测过一次 WAL：在「每个连接都 `executescript` 建表 + `BEGIN IMMEDIATE`」的情况下，Windows 上稳定复现 `attempt to write a readonly database`，当时已回退。

这次的区别是先把 DDL 从连接路径里拿掉，只在启动时跑一次，再单独验证 WAL。**如果最小实验仍复现，就不开 WAL**，只保留「DDL 移出请求路径 + 单写进程 + 写重试」，多进程需求交给 Postgres。不要为了「更正式」硬开。

另外要说清楚：WAL 不会让 SQLite 支持多个写者。它改善的是「读写并发」和「写不挡读」。真正的多写进程只有 Postgres 一条路。

---

## 2. 目标与非目标

### 2.1 做完后的样子（可验收）

1. 上传一张 2MB 截图，`POST /api/image-drafts` 在 300ms 内返回 202；识别进行中，`/api/timeline` 的 p95 仍低于 200ms。
2. 新加一张表或一个字段，只写一个 Alembic revision；空库和老库都能升到同一结构，CI 自动验证。
3. 同时开 2 个 API 进程读、1 个进程写，不出现 `database is locked`；需要 2 个写进程时，改 `DATABASE_URL` 就能切 Postgres。
4. 忘记密码可以用运维命令生成一次性链接；重置后旧会话全部失效；没有 CSRF token 的写请求返回 403。
5. 每个请求一行 JSON 日志，带 `request_id`；`/api/ready` 能分别报出 DB、迁移版本、OCR、磁盘；`/metrics` 有请求和 OCR 指标。
6. 列表默认 50 条，可搜索、可加载更多，不再一次返回全部。
7. 识图页支持 Ctrl+V / Cmd+V 粘贴截图和拖拽，粘贴后本地预览立即出现，识别在后台跑。
8. 新页面不再往 `App.jsx` 里加代码；旧页面被改到一次，就搬出去一次。

### 2.2 明确不做

- 不重写日期规则；`timeline` / `reminder` / `validate_records` 的行为不变。
- 不把共享目录 `data/records.csv` 搬进数据库。
- 不引入 Redis、Celery、Kubernetes；OCR 队列先用进程内有界线程池。
- 不做 OAuth、短信登录、2FA、SSO。
- 不做 SSR，不换 Next.js。
- 不在这一轮把生产数据切到 Postgres；只做到「一行配置可切 + CI 迁移验证」，真正切换单独决策。
- 不做邮箱验证的 token 表、验证接口和重发限流；不做 `/forgot-password` 自助接口。
- 不做一次性拆 1282 行；不做全量 ORM 查询重写。

---

## 3. 四个选择的复核结论

### 3.1 D1 前端状态：假分歧，代价被高估

真正跨页面共享的数据只有三个：`me`、`courses`、`prefs`。其余五个页面各自拉取自己的数据，tab 切换频率低，缓存的收益接近零。

「手写 useApi 要实现缓存、失效、重试、取消」把四件事打包了，但只有第一件贵。而且：

- 失效粒度问题是 `loadViews()` 重拉三个接口造成的，不是「没缓存」造成的。让每个 mutation 只重拉它改的那一个 query，40 行的 `useAsync` 也做得到。
- `client.js`（错误映射、CSRF、AbortSignal）两个方案都要写。
- 取消就是 `AbortController` 加 `useEffect` 清理，8 行。

所以手写方案的真实成本是半天，不是三个人日：`client.js` + `useAsync(data/loading/error/abort)` + 一个只装那三个值的 `AuthContext`。没有缓存，因为没东西需要缓存。

结论：先写这三样（半天），把 hook 边界定下来。之后要不要 TanStack Query 是一个两小时的替换，不是架构分叉。**仍然推荐它**，理由是批 3 的轮询、批 5 的 cursor 分页和精确失效都落在它擅长的范围内，而不是因为手写太贵；但这个决定现在不需要一次做对。

Zustand 划掉：它管客户端全局状态，而这个应用只有服务端状态和表单局部状态。它不是「代价高」，是不在同一个问题上。

### 3.2 D2 ORM：真分歧，但需求拆错了

「有 ORM」的真实含义通常是三件事：

- (a) 结构有单一真相；
- (b) 变更可版本化、可复现；
- (c) 少手拼 SQL。

(b) 只有 Alembic 能给，(a) 模型能给，(c) 是可选的。

拆开之后：**模型 + Alembic 是必须的，ORM 查询风格是可选的。**

11 张表的模型写出来（约 300 行），autogenerate 就有了真相源。71 条 SQL 不用一次换完：`src/db/repositories/*` 签名不变，谁被改到谁换。停在任何一个 repository 上都是自洽状态。

这样「有 ORM」和「不承担 71 条重写风险」同时成立，批 1 砍掉一半工作量。

SQLModel 被排除的理由是版本耦合，并非「生态少」：它同时绑 SQLAlchemy 和 Pydantic 两条主线，任一大版本升级它都可能滞后，而单人项目没有能力也不该去 patch 它。对要活几个月的项目，这是修不动的依赖。

补一个事实：SQLAlchemy 2.0 的 `session.execute(select(M)).scalars()` 本身就是 Core 风格，跟 `session.query(M)` 在代码层面几乎没有可感知差异。所以「用不用 ORM」真正的差别只在关系加载和 autogenerate，不在写法。

### 3.3 D3 数据库：部署决定，不是代码决定

真正的决定不是「哪个库」，是「允不允许第二个写进程」——这是部署决定，不是代码决定。双份代价来自一个假设：数据库必须随时可换。换成「换的时候只有一天工作量」，两边都便宜了。

那一天由三件事决定：连接串在一处（6.5 已做）、SQL 无方言、搬迁脚本（一次性）。前两条是规约。

所以现在唯一必须做的动作不是切库，也不是只做 SQLite，而是加一条测试：`PRAGMA`、`INSERT OR REPLACE`、`strftime`、`AUTOINCREMENT`、`sqlite_master` 只允许出现在 `src/db/sqlite_adapter.py`（legacy `src/migrate.py` 冻结后移走或列入白名单）。没有这条，可换性会静默过期——半年后写出二十条方言 SQL，那时「一行配置切换」就是假的。

两个调整：CI 双库不要跑全量 pytest（时间翻倍、flaky 面翻倍），Postgres job 只跑 `alembic upgrade head` + 迁移测试 + repository 测试。WAL 的 go/no-go 保持原样——它不是选立场，是定一个可判定的门槛，形态是对的。

### 3.4 D4 邮件：删需求，不是选通道

它把两件事合并了：「证明邮箱是我的」和「忘了密码能自己回来」。混在一起才产生「要么接 SMTP 要么用假邮件」。

**证明邮箱。** 软门槛已经决定它不封功能，那它就不是安全控制，只是一个提示。那它不该有 token 表、验证接口、重发限流。一个 `email_verified_at` 列 + CLI 手动置位就够了。可删掉 `email_verifications` 表、`/verify-email`、`/resend-verification`、对应限流桶、前端提示，约省一人日。

**忘记密码。** 使用者是「你 + 几个朋友」时，最便宜的通道是运维通道而不是邮件：

```bash
python -m src.accounts reset-password <email>
```

打印一次性链接。零表、零接口、零限流（链接用 HMAC 签名，`password_changed_at` 参与签名，改密即失效）。代价是不能自助、手机点不了，而这个部署形态下这个代价是零。

所以 Mailer 是唯一符合约束的形态，并非退而求其次：单用户本地应用里，邮件本来就是一个本地文件。保留 Mailer 协议（40 行）+ `OutboxMailer`，`SmtpMailer` 留成「有域名再写」。

诚实的缺口：一旦有第二个真实用户且不在你机器上，CLI 重置就不成立。写一条门槛——出现第一个非运维用户时，SMTP 从「以后」变成「现在」。和 D3 同一个手法。

### 3.5 四个选项之外的真正成本

四个选项加起来 1-2 人日，占 19-28 的 5%。真正的成本在批 1（71 条 SQL）和批 4（1282 行 JSX），都是 big-bang。同一手法用在那里收益大得多：

- 批 1 不做全量重写：只建模型 + Alembic，查询按需换。
- 批 4 不做一次性拆 1282 行：先建 `api/client.js` 和 `routes.jsx` 骨架，新页面按新结构写，旧页面留在原地直到被改到。
- Ctrl+V 就是第一个按新结构写的页面——顺序里已经这么排了，只是批 4 不必前置。

把「重构」从一批，改成一条规约。

---

## 4. 批次总览

| 批次 | 解决什么 | 主要改动 | 依赖 | 粗估 |
| --- | --- | --- | --- | --- |
| 0 安全网 | 无 CI、无回归基线 | git、`pyproject.toml`、CI、备份脚本、request_id 日志 | 无 | 1-2 人日 |
| 1 数据层 | 无 ORM/迁移、SQLite 单写 | 11 张表模型、Alembic 基线、`init_db()`、DDL 移出连接、索引、方言测试、WAL 实验 | 0 | 2-3 人日 |
| 2 账号安全 | 验证、找回、CSRF、会话、限流 | `email_verified_at`、CLI 重置、CSRF、会话、DB 限流 | 1 | 1-2 人日 |
| 3 识图异步 | OCR 阻塞 | 有界队列、状态机、202 + 轮询、去重、重启恢复 | 1 | 3-4 人日 |
| 4A 前端骨架 | 单文件继续膨胀 | `client.js`、`useAsync`、`AuthContext`、`routes.jsx` | 3 | 0.5-1 人日 |
| 4B 页面迁移 | 1282 行 | 改到哪个页面搬哪个，不单独成批 | 4A | 随各批 |
| 5 契约与可观测 | 错误、日志、监控、分页 | 错误信封、JSON 日志、metrics、ready、cursor 分页 | 1、4A | 3-4 人日 |
| 6 Ctrl+V | 新功能 | 粘贴/拖拽、本地预览、边界处理、E2E | 4A、3 | 1-2 人日 |

合计约 11.5-18 人日（一个人）。粗估只用于排序，不作为承诺。

```
0 ──> 1 ──> 2
       ├──> 3 ──> 4A ──> 5
       │             └──> 6
       └──────────────> 5（后端部分可并行）
```

每批一个独立提交/PR，不混批。每批结束都跑同一套检查点（见第 12 节）。

---

## 5. 批次 0：安全网

### 5.1 为什么先做

当前目录不是 git 仓库，没有 CI，也没有「改之前长什么样」的快照。后面要动数据库连接和前端骨架，没有这层网，任何一批都可能悄悄改坏日期规则。

### 5.2 任务

1. `git init`，补 `.gitattributes`（`* text=auto eol=lf`，`*.sqlite binary`），提交当前状态作为基线。`.gitignore` 补 `data/outbox/`、`.env*`、`frontend/coverage/`、`logs/`。
2. 新增 `pyproject.toml`：ruff（`E,F,I,B,UP`，`line-length=120`，`target-version=py311`）、pytest、coverage。
3. 拆依赖：`requirements.txt` 只留运行时，新增 `requirements-dev.txt`；全部锁版本。
4. `.github/workflows/ci.yml`：
   - backend：Python 3.11 / 3.12，`python -m ruff check`、`pytest -q --ignore=tests/test_frontend.py`、coverage；
   - frontend：Node 20，`npm ci`、`npm run build`；
   - postgres：只跑 `alembic upgrade head` + 迁移/模型测试，不跑全量 pytest（见 6.5）。
5. `scripts/backup_db.py`：用 `sqlite3.Connection.backup()` 生成带时间戳的副本，跑 `PRAGMA integrity_check`，默认保留 7 份；同时校验 `data/private/` 的文件数。恢复流程写进 README。
6. `src/logging.py` + 中间件：生成 `request_id`，输出一行 JSON（`ts, level, request_id, method, path, status, duration_ms`），响应头回 `X-Request-ID`。完整字段在批次 5 补。
7. `tests/test_logging.py`：断言响应头有 `X-Request-ID`，日志行能被 `json.loads` 解析。
8. `docs/checklist.md` 增加「加固」一节，每批完成打勾并写日期。

### 5.3 验收

- CI 在 GitHub 上绿；本地 `pytest` 62 个测试全过；`npm run build` 通过。
- `scripts/backup_db.py` 生成的副本能打开，`integrity_check` 返回 `ok`，行数与源库一致。
- 任意请求的响应头有 `X-Request-ID`，stdout 有对应的一行 JSON。
- 现有测试不需要改断言就能通过（除日志新增的输出）。

### 5.4 风险

- 日志中间件如果读 `request.client` 为 `None`，会抛错；用 `getattr` 兜底。
- 现有测试大量使用 `TestClient`，新增响应头不影响断言；但如果有测试断言完整 headers，需要更新。

---

## 6. 批次 1：数据层（模型 + Alembic，不重写 SQL）

### 6.1 目标结构

```
src/db/__init__.py        connect()、session()、init_db()、database_path()
src/db/paths.py           ROOT、database_path()、database_url()
src/db/engine.py          SQLAlchemy engine、SessionLocal、init_db()
src/db/models.py          11 张表的 ORM 模型（英文列名）
src/db/sqlite_adapter.py  所有 SQLite 方言：connect、PRAGMA、write_retry、table_names
src/db/migrations.py      Alembic 的 upgrade/stamp/current 包装
migrations/               Alembic 脚本
alembic.ini
scripts/bench_sqlite.py   WAL go/no-go
scripts/backup_db.py      批次 0 已建
```

`src/db.py` 改成包后，现有 `from src.db import connect` / `database_path` 不变。旧模块继续用 `sqlite3.Connection`，新模块用 `session()`。

### 6.2 迁移策略（老库怎么办）

1. `0001_baseline`：按当前真实结构建 11 张表，包含 `migrate.py` 后来补的 `materials.image_rel`、`materials.draft_id`、`reminder_prefs.updated_at`。
2. `0002_indexes`：加第 6.4 节的索引。
3. 启动时判断：
   - 空库：`alembic upgrade head`；
   - 有表但没有 `alembic_version`：先备份 → 跑一次 `src.migrate.migrate()` 把老结构补齐 → `alembic stamp 0001` → `upgrade head`；
   - 已有 `alembic_version`：`upgrade head`。
4. 老库接管只允许执行一次，写进日志和 `docs/checklist.md`；`src/migrate.py` 标记为 legacy，冻结，不再加新逻辑。
5. 迁移测试用 `tests/fixtures/schema-only-20261107.json` 和一份带数据的临时库，验证：行数不变、`PRAGMA foreign_key_check` 为空、结构 diff 为空。

### 6.3 连接生命周期与写冲突

1. `connect()` 不再建表、不再迁移。DDL 只在 `init_db()` 里跑一次：FastAPI `lifespan` 启动时、CLI 入口启动时，或每个进程第一次 `connect()` 时（按数据库路径去重）。
2. 每个请求一个 `sqlite3.Connection`（旧代码）或一个 `Session`（新代码），用完关闭；OCR 工作线程各自开自己的连接，不跨线程共享。
3. 写操作保留 `BEGIN IMMEDIATE`；新增 `write_retry`：捕获 `OperationalError: database is locked`，最多重试 3 次，退避 50/150/400ms。重试次数进指标（批次 5）。
4. `PRAGMA busy_timeout=5000`、`foreign_keys=ON`、`synchronous=NORMAL` 保留。
5. **WAL go/no-go 实验**：`scripts/bench_sqlite.py`，4 个写线程 × 200 次 + 4 个读线程 × 200 次，在 Windows 上跑 3 轮。
   - 0 次 `readonly database` / 未恢复的 `database is locked`：开 `journal_mode=WAL`；
   - 任意一次复现：保持 `DELETE`，记录结论，不再尝试。
   - **2026-11-09 结果**：WAL 3 轮全部复现 `attempt to write a readonly database`，第 1/2 轮只写入 200/800 行，第 3 轮 400/800 行；DELETE 3 轮 0 错误、800/800 行，约 5.6-6.0 秒。默认保持 DELETE，`LEARNING_ASSISTANT_SQLITE_JOURNAL` 只用于复现实验。
6. 单写进程写进 README 和 `docs/stack-and-scope.md`：SQLite 阶段只允许 1 个 API 写进程；读进程可以多个。要 2 个写进程，走 6.5。

### 6.4 索引

| 表 | 索引 | 用途 |
| --- | --- | --- |
| `user_records` | `(user_id, id)`、`(user_id, status)`、`(user_id, deadline)`、`(user_id, review_state)` | 我的记录、筛选、时间线、提醒 |
| `materials` | `(user_id, course_id)`、`(user_id, id)` | 资料夹、按课程筛选 |
| `image_drafts` | `(user_id, id)`、`(user_id, sha256)`、`(ocr_state)` | 草稿查询、去重、恢复 |
| `sessions` | `(user_id)`、`(expires_at)` | 会话列表、过期清理 |
| `review_activities` | `(user_id, week_key)` | 本周依据 |
| `image_links` | `(user_id, draft_id)`、`(user_id, record_id)` | 关联查询 |

### 6.5 可换性规约与 Postgres

1. `DATABASE_URL` 优先，其次 `LEARNING_ASSISTANT_DB`，默认 `sqlite:///data/app.sqlite`。
2. **方言泄漏测试** `tests/test_dialect.py`：扫描 `src/**/*.py`，`PRAGMA`、`INSERT OR REPLACE`、`strftime`、`AUTOINCREMENT`、`sqlite_master` 只允许出现在 `src/db/sqlite_adapter.py`；`src/migrate.py` 作为冻结的 legacy 列入白名单，并写明「只减不增」。
3. CI 的 Postgres job 只做：`alembic upgrade head` + `tests/test_schema_models.py` + `tests/test_migrations.py`。不跑全量 pytest。
4. `docker-compose.yml` 提供 `postgres:16`，本地可起。
5. `scripts/copy_to_postgres.py` 放到决定切换时再写；现在只保证连接串在一处、SQL 无方言。
6. 诚实说明：旧模块仍用 `sqlite3.Connection`，所以 Postgres **运行时**要等这些模块被改到、换成 `session()` 之后才完整。现在能保证的是 schema 和迁移可换，不会静默长出方言 SQL。

### 6.6 71 条 SQL 的渐进规则

- 新代码一律用 `session()` + `select()`，不再新增 `sqlite3` 调用。
- 旧模块保持 `connect()` + 原生 SQL，直到它因为真实需求被修改。
- 修改某个模块时，在同一提交里把它的查询换成 `select()`，并补该模块的测试；不做「专门重写」分支。
- 每次转换后跑全量测试；停在任何一个模块，代码都自洽。
- `src/db/repositories/` 等第一个模块真正转换时再建，不提前铺空目录。

### 6.7 验收

- 空库 `alembic upgrade head` 后的结构，与老库接管后的结构完全一致（归一化后 diff 为空）。
- 老库 fixture 接管后行数不变，`foreign_key_check` 为空。
- 4 写 4 读压测 3 轮无未恢复的锁错误；如果开了 WAL，记录实验数据。
- 62 个测试全过；`tests/test_dialect.py`、`tests/test_schema_models.py` 全过。
- 单次请求不再执行任何 DDL（用 SQLAlchemy 事件计数或日志断言）。
- CI 的 SQLite 和 Postgres 两个 job 都绿。

### 6.8 回滚

- 每个 revision 都写 `downgrade`；只降结构，不删用户数据（新表为空才 drop）。
- 代码回滚到批次 0 的 tag，数据库保留 `alembic_version` 不影响旧代码（旧代码只读它认识的表）。
- 老库接管前必须已有备份；备份失败就中止启动。

---

## 7. 批次 2：账号安全（删掉邮件需求）

详细计划、概念解释和需要拍板的三处，见 [批次 2 计划](plan-batch2-accounts.md)。

### 7.1 数据模型

| 表 | 变更 |
| --- | --- |
| `users` | 加 `email_verified_at TEXT`、`password_changed_at TEXT` |
| `sessions` | 加 `csrf_token TEXT`、`last_seen_at TEXT`、`user_agent TEXT`、`ip_hash TEXT`、`expires_at TEXT` |
| `rate_limits` | 新：`bucket_key PRIMARY KEY, window_start, count` |

不建 `email_verifications`，不建 `password_resets`。重置链接用 HMAC 签名，不落库。

### 7.2 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/auth/reset-password` | `{token, password}`，校验签名和过期时间；成功后更新 `password_changed_at`、删除该用户全部会话 |
| GET | `/api/sessions` | 当前用户的会话列表（设备、最近使用、是否当前） |
| DELETE | `/api/sessions/{id}` | 踢掉指定会话 |
| POST | `/api/auth/logout-all` | 删除全部会话 |
| GET | `/api/me` | 增加 `email_verified`、`csrf_token` |

不新增 `/verify-email`、`/resend-verification`、`/forgot-password`。

### 7.3 CSRF

- 登录/注册时生成 `csrf_token`，存进 `sessions` 行；登录/注册响应和 `/api/me` 都返回它。
- 所有 `POST/PUT/PATCH/DELETE`（除登录、注册、重置密码）要求 `X-CSRF-Token`，与数据库里的值常数时间比较，不匹配返回 403 `csrf_failed`。
- 额外做 `Origin` 校验：带 `Origin` 且不在允许列表的写请求直接 403。
- CORS 的 `allow_headers` 增加 `X-CSRF-Token`（`src/api.py:43-49` 目前只有 `Content-Type`）。
- 灰度开关 `LEARNING_ASSISTANT_CSRF=0` 保留一个版本，默认开启；同批更新前端。

### 7.4 会话

- 登录时删除浏览器带来的旧会话，创建新 token（防会话固定）。
- `expires_at = 登录时间 + 14 天`；`last_seen_at` 超过 7 天未更新则失效。
- `last_seen_at` 最多每 60 秒写一次，避免每个已登录请求都写库（SQLite 单写者下这点很重要）。
- 改密码、重置密码后，删除该用户除当前会话外的全部会话。

### 7.5 限流

- 从进程内 `SlidingWindow` 换成 `rate_limits` 表（固定窗口，`ON CONFLICT DO UPDATE count = count + 1`）。
- 键：`login:{ip}:{email}`、`register:{ip}`、`reset:{ip}`。
- 阈值沿用环境变量：注册 30/小时/IP，登录失败 10 次/15 分钟/IP+邮箱。
- 每次写入顺带清理 24 小时前的行，避免表膨胀。
- 保留 `LEARNING_ASSISTANT_RATE_LIMIT=0` 关闭开关，测试和演示继续可用。

### 7.6 密码

- 新注册最低 10 位（老用户登录不受影响，改密码时按新规则）。
- 内置一份 500 条常见弱密码表，命中直接拒绝；不引入重型 `zxcvbn`。
- 记录 `password_changed_at`，用于「改密后旧会话失效」和重置链接的一次性。

### 7.7 运维通道

```bash
python -m src.accounts reset-password <email>
python -m src.accounts mark-verified <email>
```

- `reset-password` 生成签名 token，打印 `http://127.0.0.1:5173/reset?token=...`；token 30 分钟有效，`password_changed_at` 参与签名，改密即失效。
- 签名密钥优先 `LEARNING_ASSISTANT_SECRET`，否则首次生成 `data/secret.key`（0600），不进 git。
- `mark-verified` 只写 `email_verified_at`，不做邮件、不发信。
- 这两个命令是运维动作，不需要登录、不需要限流；服务器上能执行命令的人本来就能改库。

### 7.8 邮件

- `src/mail/`：`Mailer` 协议 + `OutboxMailer`（写 `.eml` 到 `data/outbox/`）。
- `SmtpMailer` 先不写，留一个 `# 有域名再实现` 的说明。
- 当前没有生产调用方；保留它是为了以后邮件提醒，不是为了让验证流程「看起来完整」。

### 7.9 门槛

出现第一个非运维用户（不在本机、需要自助找回）时：

1. `SmtpMailer` 从「以后」变成「现在」；
2. `/forgot-password` 才允许加；
3. 那时再评估是否需要邮箱验证的 token 表。

把这条写进 `docs/stack-and-scope.md`，避免以后忘记为什么现在是 CLI。

### 7.10 验收

- `python -m src.accounts reset-password a@example.com` 打印链接；打开链接重置成功；旧会话全部 401；同一链接第二次使用失败。
- 不带 `X-CSRF-Token` 的 `POST /api/mine/{id}` 返回 403；带上成功。
- 同一 IP 连续失败登录超过阈值返回 429，重启进程后计数仍在。
- 老用户（8 位密码）仍能登录。
- `mark-verified` 后 `/api/me` 的 `email_verified` 为 true；前端不出现验证提示。

### 7.11 风险

- CSRF 上线会打断旧前端：同批更新 `api/client.js`，并保留一个版本的关闭开关。
- 会话表写入变多：`last_seen_at` 做 60 秒节流，避免把单写者压垮。
- CLI 重置在「非运维用户」出现后不成立：7.9 的门槛就是为此写的。

---

## 8. 批次 3：识图异步化

### 8.1 现在的调用链

```
POST /api/image-drafts (async def)
  → await file.read()                 读 8MB
  → create_draft()                    同步，跑在事件循环里
      → engine_status()               PowerShell #1，最长 30s
      → transcribe()                  PowerShell #2，最长 30s
      → 写私有文件 + 插 image_drafts
  → 201 + 完整 candidate
```

### 8.2 目标状态机

```
上传接收：queued ──> running ──> done
                        └─────> failed（可 retry）
草稿生命周期：pending ──> confirmed
                     └──> cancelled
```

`status` 继续表示草稿生命周期（`pending/confirmed/cancelled`），新增 `ocr_state` 表示识别进度（`queued/running/done/failed`），两者不混。

### 8.3 接口契约

| 方法 | 路径 | 变更 |
| --- | --- | --- |
| POST | `/api/image-drafts` | 改为 202，返回 `{id, status, ocr_state, preview_url, poll_after_ms}`；不再等识别 |
| GET | `/api/image-drafts/{id}` | 增加 `ocr_state`、`ocr_started_at`、`ocr_finished_at`、`ocr_error`、`queue_position` |
| POST | `/api/image-drafts/{id}/retry` | 新增，失败后重新入队 |
| POST | `/api/image-drafts/{id}/cancel` | 已存在，增加「运行中取消」：置 `cancel_requested`，worker 丢弃结果 |

队列满（默认 20）返回 503 `ocr_queue_full`，带 `Retry-After: 5`；单用户同时只允许 2 个未完成草稿，超过返回 429。

### 8.4 队列实现

- `src/ocr_queue.py`：`ThreadPoolExecutor(max_workers=2)` + `queue.Queue(maxsize=20)`；提交时非阻塞，满则拒绝。
- 队列里只放 `draft_id` 和 `user_id`，不放图片字节；worker 从私有目录重新读原图，避免 8MB × 20 占内存。
- 每个 job 自己开连接；只在开始时写一次 `running`，结束时写一次 `done/failed`，识别期间不持有长事务。
- 超时：子进程仍是 30 秒；job 外层 45 秒兜底；超时写 `failed`，文案「识别超时，可以人工补录」。
- 去重：上传后先按 `(user_id, sha256)` 找最近的 `ocr_state='done'` 且未取消的草稿，命中直接返回 `reused: true`，不再跑识别。重复粘贴同一张图会立即出结果。
- 启动恢复：`running` 且超过 5 分钟的草稿标记 `failed`（「服务重启，识别中断，请重试」）；`queued` 重新入队，最多 20 个。
- 关闭：`lifespan` 退出时 `shutdown(wait=False, cancel_futures=True)`。
- 引擎状态缓存 300 秒，`/api/ready` 和上传都读缓存，不再每次起 PowerShell。

### 8.5 前端最小适配（本批只做必需）

- 上传后立即显示本地预览（`URL.createObjectURL`），再显示「已接收，识别中」。
- 每 800ms 轮询一次，退避到 2s，60 秒后停止并提示重试；组件卸载时清定时器。
- `ocr_state != done` 时禁用「确认写入」，失败时切到人工补录模式。
- 本批不拆页面，改动控制在 `RecognizePage` 内部，批次 4A/6 再搬。

### 8.6 验收

- 5 个并发的 2MB 上传全部在 300ms 内返回 202；期间 `/api/timeline` p95 < 200ms。
- 测试图（合成海报）识别在 5 秒内完成；`ocr_state` 依次经过 `queued → running → done`。
- 队列满返回 503；单用户第 3 个未完成草稿返回 429。
- 杀掉进程再启动，`running` 变 `failed` 且有重试按钮；`queued` 能继续跑。
- 同一张图第二次上传命中 `reused`，不产生新文件。
- 识别失败仍能人工补录，行为与现在一致。

### 8.7 风险

- 线程里写 SQLite 可能撞锁：每个 job 短事务 + `write_retry`；必要时把 `max_workers` 降到 1。
- 进程内队列在重启时丢任务：已有恢复逻辑；不追求「绝不丢」，追求「不静默丢」。
- 多进程部署时每个进程各自一个队列，没有全局并发上限：写进 README；以后需要时换 RQ/Arq，接口不变。

---

## 9. 批次 4：从「一批重构」改成「一条规约」

### 9.1 4A 骨架（半天到一天）

只建四样，不动旧页面：

```
frontend/src/api/client.js        fetch 封装、错误映射、CSRF、AbortSignal、request_id
frontend/src/hooks/useAsync.js    data/loading/error/abort，约 40 行
frontend/src/context/AuthContext.jsx   me、courses、prefs、csrf、login/logout
frontend/src/routes.jsx           新页面的路由注册表，初始为空
```

`App.jsx` 只增加 Providers 和「新路由表」的挂载点，不搬旧页面。旧页面继续留在 `App.jsx`。

### 9.2 4B 规约（随页面推进）

1. 新页面一律放 `pages/`，用 `api/` 和 `useAsync`，不直接写 `fetch`。
2. 旧页面被真实需求改到时，才在同一个提交里搬出去；不单独开「拆分页面」分支。
3. 每搬一个页面，`App.jsx` 少一段；不设「App.jsx 必须 ≤120 行」的截止日期。
4. 每搬一个页面，补一个空态/错误态测试；E2E 黄金路径保持通过。
5. `grep -r "fetch(" frontend/src` 只允许命中 `api/`；新代码不允许新增例外。
6. 什么时候换 TanStack Query：批 3 的轮询和批 5 的 cursor 分页真正落地时，如果 `useAsync` 开始重复造缓存/失效，再换；替换范围只在 `hooks/` 和调用点，约两小时。

### 9.3 目标目录（逐步长出来，不一次建空壳）

```
frontend/src/
  api/client.js
  hooks/useAsync.js
  context/AuthContext.jsx
  routes.jsx
  pages/           新页面逐个进来
  components/      被两个以上页面复用时才抽
  lib/dates.js     normalizeDeadline 第一次被复用时抽
  styles/          现有 styles.css 先不动
```

### 9.4 验收

- `api/client.js`、`useAsync`、`AuthContext` 有单元测试；`npm run build` 通过。
- 新页面不往 `App.jsx` 加代码；`grep` 规则通过。
- 旧页面行为不变；现有前端源码断言同步更新，不删检查项。
- Ctrl+V 页面（批次 6）是第一个完整走新结构的页面。

### 9.5 风险

- 两套结构并存会让人不知道该用哪套：写进 README 和 `routes.jsx` 顶部注释——新代码用新结构，旧代码改到时再搬。
- `useAsync` 被过度使用：只用于「一个请求 + 一个结果」，轮询和分页在批 3/5 再评估 TanStack Query。

---

## 10. 批次 5：API 契约与可观测性

### 10.1 错误信封

保留 `detail` 兼容旧前端，同时增加结构化字段：

```json
{
  "detail": "找不到这条记录。",
  "error": {
    "code": "record_not_found",
    "message": "找不到这条记录。",
    "request_id": "01J...",
    "fields": { "截止日期": "必须是 YYYY-MM-DD 或未找到" }
  }
}
```

错误码先定这些：`unauthorized`、`csrf_failed`、`rate_limited`、`validation_failed`、`record_not_found`、`draft_not_found`、`draft_conflict`、`ocr_unavailable`、`ocr_queue_full`、`image_too_large`、`image_unsupported`、`email_taken`、`token_invalid`、`token_expired`、`internal_error`。

- `RequestValidationError` → 422，`fields` 按字段名归并；
- `HTTPException` → 对应状态码 + 错误码；
- 未捕获异常 → 500 `internal_error`，日志记堆栈，响应只给 `request_id`，不泄漏内部信息。

### 10.2 结构化日志

- 一行一个 JSON：`ts, level, logger, msg, request_id, method, path, route, status, duration_ms, user_id, client_ip_hash, error_code`。
- `request_id` 用 `contextvars` 贯穿；只接受合法 UUID 的入站 `X-Request-ID`，否则新生成（防日志注入）。
- 邮箱脱敏为 `a***@example.com`；密码、token、Cookie、OCR 原文一律不记。
- 本地可选 `RotatingFileHandler` 写 `logs/app.jsonl`（10MB × 5）；部署时走 stdout，由平台收集。
- uvicorn 的 access log 关掉或改成同一格式，避免一行请求两条日志。

### 10.3 指标与健康检查

- `prometheus-fastapi-instrumentator` + `prometheus-client`：
  - `http_requests_total{method, route, status}`（用路由模板，不用原始路径，防基数爆炸）；
  - `http_request_duration_seconds{method, route}`；
  - `ocr_jobs_total{state}`、`ocr_job_duration_seconds`、`ocr_queue_depth`；
  - `db_write_retries_total`、`db_locked_total`；
  - `auth_login_failures_total`、`auth_rate_limited_total`。
- `/metrics` 默认只绑 localhost；生产用 `METRICS_TOKEN` 保护。
- `/api/health`：只返回 `{ok:true}`，不查库、不起子进程，保持兼容。
- `/api/ready`：DB `SELECT 1`、Alembic 版本等于 head、磁盘剩余 > 100MB、OCR 缓存状态；任一失败返回 503 并列出原因。
- Sentry 可选：`SENTRY_DSN` 存在才初始化，默认关闭。

### 10.4 分页与搜索

- 统一参数：`limit`（1-200，默认 50）、`cursor`（上一页最后一条 id 的 base64）、`q`（≤100 字）、`status`、`course_id`。
- 响应：`{items, next_cursor, has_more}`；`ORDER BY id`，用 `id > cursor` 翻页，避免 offset 越翻越慢。
- 接入：`/api/mine`、`/api/records`（共享目录仍全量返回，行数有上限）、`/api/materials`、新增 `/api/image-drafts`。
- 搜索：`title LIKE ?`（`%q%`），同时匹配 `condition`、`source_url`；SQLite 对中文按字节匹配，够用，不引入 FTS5。
- 共享 CSV 增加 mtime 缓存，避免每个请求重读文件。
- 前端：搜索框 300ms 防抖 + 「加载更多」，不一次性渲染全部。

### 10.5 验收

- 每个请求恰好一行 JSON 日志；500 响应带 `request_id`，日志能用它找到堆栈。
- `/metrics` 能查到请求计数和 OCR 时长；`/api/ready` 在关掉 DB 后返回 503 并写明 `db: failed`。
- `GET /api/mine?limit=2` 返回 2 条 + `next_cursor`；带 `cursor` 能取下一页；`q=计挑赛` 只返回匹配项。
- 422 响应有 `error.code=validation_failed` 和 `fields`。
- 现有页面全部改用分页接口后，空列表、只有一页、翻到最后一页都正常。

---

## 11. 批次 6：Ctrl+V 粘贴识图

### 11.1 交互

1. 识图页常驻一个虚线区域，写明「拖入图片，或按 Ctrl+V 粘贴截图」。
2. 页面挂载时监听 `window` 的 `paste`；卸载时移除。
3. 剪贴板里有图片 → `preventDefault` → 立即生成本地预览 → 走与文件选择相同的上传函数。
4. 上传中显示「正在上传」；服务端接收后显示「已接收，识别中」，轮询进度。
5. 识别完成 → 可编辑候选；失败 → 人工补录，文案与现在一致。
6. 支持拖拽：`dragover` 阻止默认并高亮，`drop` 取第一个文件。

### 11.2 取图规则

```
items = clipboardData.items
找 kind === "file" 且 type 以 "image/" 开头
  → item.getAsFile()
否则看 clipboardData.files
  → 取第一个 image/*
否则忽略（不阻止默认行为）
```

- 文件名：`粘贴-YYYYMMDD-HHmmss.<ext>`（截图工具通常没有文件名）。
- 多张图：只用第一张，提示「一次识别一张，已使用第一张」。
- 超过 8MB：前端直接拒绝，不发请求；提示与后端文案一致。
- 类型不在 PNG/JPEG/GIF/WEBP/BMP：前端拒绝，列出支持的类型。
- 当前有未确认草稿：先确认「粘贴会替换当前草稿，继续？」；取消则不上传。
- 正在识别上一张：忽略新的粘贴，提示「等当前这张识别完」。

### 11.3 不抢输入框

- 事件目标是 `input`、`textarea`、`[contenteditable]`，且剪贴板里有文本 → 不处理，让用户正常粘贴文字。
- 事件目标是上述元素，但剪贴板里只有图片 → 仍然处理（浏览器本来也不会把图粘进文本框）。
- 文件选择框保留，作为移动端和不支持 `paste` 事件的浏览器兜底。

### 11.4 验收用例

| 场景 | 期望 |
| --- | --- |
| Windows 截图工具截屏后 Ctrl+V | 立即出现预览，随后进入识别 |
| 浏览器里右键「复制图片」后 Ctrl+V | 同上 |
| 从 Word 复制带文字和图片 | 焦点在输入框时粘文字；焦点在页面时识别图片 |
| 一次复制两张图 | 只识别第一张，并提示 |
| 粘贴 10MB 图 | 前端拒绝，不发请求 |
| 粘贴 PDF/文本 | 忽略，不报错 |
| 识别中再粘贴 | 提示等待，不排队 |
| 有未确认草稿时粘贴 | 二次确认后替换 |
| 拖拽图片到页面 | 与粘贴一致 |
| macOS Cmd+V | 与 Ctrl+V 一致 |
| 手机长按粘贴 | 不保证；文件选择可用 |

### 11.5 测试

- Vitest：`imageFromClipboard` 用假 `DataTransfer` 覆盖「有图」「只有文本」「多图」「空文件」。
- Playwright：授予 `clipboard-read` / `clipboard-write`，用 `ClipboardItem` 写入一张 PNG，按 `Control+V`，断言预览和 `queued` 状态。
- 后端不新增接口；`source` 表单字段可选（`paste` / `upload` / `drop`），只用于日志统计，不参与业务。

### 11.6 风险

- Firefox/Safari 对 `clipboardData.items` 的支持差异：保留 `files` 兜底，E2E 至少覆盖 Chromium，手动过一遍 Firefox。
- 截图工具可能给 `image/png` 但文件名为空：统一重命名，后端不看文件名。
- 页面级监听可能抢走其他粘贴：用 11.3 的输入框判断，并只处理图片。

---

## 12. 每批结束的固定检查点

1. `python -m pytest -q --ignore=tests/test_frontend.py` 全过。
2. `cd frontend && npm run build` 全过（有前端改动的批次再跑）。
3. `python -m src.validate_records data/records.csv --min-rows 3` 通过。
4. `python scripts/backup_db.py` 成功，`integrity_check` 为 `ok`。
5. 更新 `docs/checklist.md`，写完成日期。
6. 打 tag：`batch-0` … `batch-6`。
7. 记录一条「本批改变了什么行为 / 没改变什么」，写进提交说明。

---

## 13. 数据模型变更汇总

| 表 | 变更 | 批次 |
| --- | --- | --- |
| `alembic_version` | 新增（工具表） | 1 |
| `users` | `email_verified_at`、`password_changed_at` | 2 |
| `sessions` | `csrf_token`、`last_seen_at`、`user_agent`、`ip_hash`、`expires_at` | 2 |
| `rate_limits` | 新表 | 2 |
| `image_drafts` | `ocr_state`、`ocr_started_at`、`ocr_finished_at`、`ocr_attempts`、`cancel_requested` | 3 |
| 索引 | 见 6.4 | 1 |

不新增：`email_verifications`、`password_resets`。

---

## 14. API 变更汇总

| 批次 | 变更 | 兼容性 |
| --- | --- | --- |
| 0 | 响应头 `X-Request-ID` | 兼容 |
| 1 | 无接口变化 | 兼容 |
| 2 | 新增 `/api/auth/reset-password`、`/api/sessions`、`/api/sessions/{id}`、`/api/auth/logout-all`；`/api/me` 增加 2 个字段；写接口要求 CSRF | 前端同批更新；CSRF 有灰度开关 |
| 3 | `POST /api/image-drafts` 201 → 202；新增 retry；GET 增加 `ocr_state` | 前端同批更新；无外部客户端 |
| 4 | 无接口变化 | 兼容 |
| 5 | 列表增加 `limit/cursor/q`；错误响应增加 `error`；新增 `/api/ready`、`/metrics` | `detail` 保留；分页默认 50，前端同批更新 |
| 6 | 无接口变化（可选 `source` 字段） | 兼容 |

---

## 15. 测试矩阵

| 层 | 工具 | 覆盖 |
| --- | --- | --- |
| 单元 | pytest | 日期规则、校验、token 签名、CSRF、密码策略 |
| 仓储 | pytest + 临时 SQLite | 每个 repository 的增删改查、用户隔离 |
| API | TestClient | 401/403/404/409/422/429、分页、错误码 |
| 迁移 | pytest | 空库升级、老库接管、downgrade、结构 diff |
| 方言 | pytest | 方言 SQL 只出现在 adapter |
| OCR | pytest + 假引擎 | 队列、超时、重试、重启恢复、去重 |
| 前端单元 | Vitest | 剪贴板解析、日期规范化、api client 错误映射 |
| 前端组件 | Testing Library | 识图状态机、分页、空态 |
| E2E | Playwright | 注册→登录→粘贴→确认→我的记录 |
| 构建 | CI | lint、test、build、首屏体积 |

覆盖率门槛：后端行覆盖 ≥ 75%；`timeline.py`、`reminder.py`、`validate_records.py` ≥ 90%。

---

## 16. 迁移、备份、回滚

1. 批次 1 开始前：`python scripts/backup_db.py`，确认 `integrity_check=ok`，复制 `data/private/` 的文件清单。**本次已手动备份到 `_analysis/learning-assistant.backup-2026-11-09-1756/` 和同名 `.tar.gz`，DB 校验 ok。**
2. 每个 Alembic revision 都写 `downgrade`，并在测试里跑一次 `upgrade → downgrade → upgrade`。
3. 老库接管失败：中止启动，保留备份，不改任何表。
4. 代码回滚：回到上一个 tag；数据库保留 `alembic_version`，旧代码不读它，不影响运行。
5. 功能开关：`LEARNING_ASSISTANT_CSRF`、`LEARNING_ASSISTANT_OCR_ASYNC` 各保留一个版本，默认开启。
6. 永不执行：`rm data/app.sqlite`、`DROP TABLE users`、无备份的 `alembic downgrade base`。

---

## 17. 风险登记

| 风险 | 触发条件 | 缓解 |
| --- | --- | --- |
| ORM 模型与现有结构不一致 | 手写 baseline 漏列 | 结构 diff 测试 + 老库 fixture + 行数校验 |
| 老库 stamp 错位 | 已有数据但结构判断错 | 先备份；结构 diff；失败中止 |
| WAL 再次触发 readonly | Windows 并发 DDL | DDL 只在启动；4 写 4 读实验；复现就保持 DELETE |
| 可换性静默过期 | 半年后写出方言 SQL | `tests/test_dialect.py` 白名单，只减不增 |
| CSRF 打断前端 | 旧客户端没带 token | 同批更新前端；灰度开关；E2E 覆盖 |
| OCR 队列丢任务 | 进程重启 | 启动恢复；失败可见；手动重试 |
| 分页改变返回结构 | 前端仍按全量读 | 同批更新；默认 50；空态/末页测试 |
| CLI 重置在非运维用户出现后失效 | 第二个真实用户 | 7.9 的门槛：SMTP 从「以后」变成「现在」 |
| 剪贴板兼容性 | Firefox/Safari | `files` 兜底；文件选择保留；手动过浏览器 |

---

## 18. 执行顺序与确认方式

按批次推进，每批完成后给一份「改了什么、怎么验、还剩什么」的短报告，确认后再进下一批。

本次已执行：备份 → 批次 0 → 批次 1。批次 1 停在「模型 + Alembic + DDL 移出连接 + 方言测试 + WAL 实验」，不动 71 条 SQL。

如果你只想先解决最痛的两点，可以只批准 **批次 0 + 1 + 3**：先把「上传卡死服务」和「每次连接跑 DDL」解决，账号安全和前端骨架往后放。Ctrl+V 仍然放在最后，避免在旧结构上写两遍。

---

## 19. 本次复核的净效果

| 项 | v1 | v2 |
| --- | --- | --- |
| D1 前端状态 | 3 人日，TanStack Query 前置 | 0.5 人日，先手写 `client.js` + `useAsync` + Context；Query 是两小时替换 |
| D2 ORM | 4-6 人日，71 条 SQL 全换 | 2-3 人日，模型 + Alembic 必须，查询按需换 |
| D3 数据库 | 双库全量 CI + 立即铺 PG | 方言测试 + PG 只跑迁移/模型测试；切库是部署决定 |
| D4 邮件 | 5 张新表、7 个接口 | 2 列 + CLI 重置；删掉验证 token 表和 3 个接口 |
| 批 4 | 一次性拆 1282 行 | 半天骨架 + 一条规约；Ctrl+V 是第一个新页面 |
| 总估算 | 19-28 人日 | 11.5-18 人日 |

四个选项加起来 1-2 人日，占 5%。真正省下来的是批 1 和批 4 的 big-bang。剩下的风险集中在批 3（线程 + SQLite 写）和批 5（分页要碰所有列表页），不在 D1-D4。
