# 批次 2：账号安全（详细计划）

日期：2026-11-09  
范围：`_analysis/learning-assistant`  
状态：已确认并执行（2026-11-09）。三项拍板：A 加改密码、B 砍 `src/mail/`、C 账号页本批做。  
前置：批次 0/1 已完成，数据库在 Alembic `0002`。  
预计：2.5-3.5 人日（一个人）。

## 0. 先看这三句

1. 这一批不碰日期规则，不碰 `data/records.csv`，不碰时间线和提醒的判断。只动「账号」这一层：谁能进、进来后能做什么、忘了密码怎么办。
2. 做完之后：写请求要带本站口令；会话会过期；失败次数重启不丢；忘记密码有一条 30 分钟的一次性链接；邮箱验证只做记录，不挡人。
3. 最大的坑是 CSRF 会让旧前端的所有写请求变成 403。所以前端必须同批改，并留一个版本的关闭开关。

为什么不是先做批次 3：批次 3 解决「上传一张图卡住整个服务」，体感更痛；但批次 2 是「账号能不能给别人用」的门槛。两批不冲突，先做哪批都行；按 0→1→2→3 的顺序做，批次 3 的 `ocr_state` 列可以复用这一批的迁移和测试套路。

---

## 1. 六个概念，用大白话讲

### 1.1 CSRF：别人借你的门禁卡

你登录之后，浏览器像给你发了一张门禁卡（Cookie）。以后你每次请求，浏览器自动把卡附上，服务器看到卡就放行。

坏处是：你访问别的网站时，那个网站可以偷偷让你的浏览器向我们的站点发一个写请求，浏览器照样把门禁卡附上。服务器分不清这是你点的，还是别人借你的手点的。

`SameSite=Lax` 相当于规定：只有从我们自家大门进来的请求才认这张卡。跨站 POST 不带卡，挡住了大部分情况。但它有两个漏洞：

- `localhost` 上`所有端口算同一栋楼（same-site）。你在本机开的另一个网页，仍然算「自`家」，Lax 挡不住。
- 老浏览器、内嵌网页、部分客户端不一定执行这条规定。

CSRF token 是另一把钥匙：登录时服务器随机生成一串字符，只存在这次会话里，页面通过 `/api/me` 拿到。以后每个写请求都要在 `X-CSRF-Token` 头里带上它。坏网站读不到它（浏览器不让跨站读我们的响应），也猜不到（32 字节随机）。没有它，写请求一律 403。

Origin 校验是第二道锁：浏览器发跨站 POST 时会带一个 `Origin` 头，写着请求从哪个网站来。不是我们的地址就拒绝。它几乎不要成本，但挡不住所有情况（有些请求没有 Origin），所以是补充，不是主力。

为什么不用「双提交 Cookie」：那种做法要把 token 放在一个 JavaScript 能读的 Cookie 里。既然 JS 能读，XSS 也能读，防线就退化了。我们已经有 `sessions` 表，直接把 token 存在会话行里更直接。

### 1.2 会话：手环 + 登记簿

Cookie 是手环，`sessions` 表是前台登记簿。手环上只有一个编号；服务器每次都要翻登记簿，看这个编号还在不在、有没有过期。手环本身不是身份，登记簿才是。

两种过期，谁先到算谁：

- 绝对过期：从登录那一刻起 14 天，不管你来不来。它限制「偷到手环的人最多用多久」。
- 空闲过期：连续 7 天没来，就作废。它限制「网吧电脑上忘了退出」。

`last_seen_at` 不是每次请求都写。登记簿只有一支笔（SQLite 同一时间只允许一个写），每进一次门都写一笔会排队。所以最多 60 秒记一次「今天来过」。7 天的判断不需要秒级精度。

会话固定攻击：坏人先塞给你一个他知道编号的手环，你拿着它登录；如果登录后编号不变，他也有一份，就能跟你一起进。所以登录成功时，先删掉浏览器带来的旧会话，再发一个新编号。

改密码或重置密码后，删掉这个用户的其他会话。原因是：如果密码已经泄露、坏人已经登录，只改密码不踢人，他还能继续用旧会话。删掉才算真的收回。

### 1.3 限流：门卫的小本子 vs 前台的总账

现在 `src/ratelimit.py` 是进程内计数：重启清零，多进程各记各的。放到 `rate_limits` 表里，相当于前台一本总账，谁都能翻，重启也还在。

固定窗口：一行记录 = 一个键（比如「这个 IP + 这个邮箱」）+ 窗口开始时间 + 次数。每失败一次，次数加一；窗口过期就归零。简单，一次写入。

缺点是窗口交界处最多能放过两倍的请求（59 秒打 10 次，下一秒再打 10 次）。登录限流够用，它不是计费系统。

清理：每 60 秒最多清一次 24 小时前的行。不是每次写都清，否则一次登录变两次写。

数据库出错时放行并记一条日志。理由很实际：这是本地应用，限流表被锁住时，不能把主人也锁在门外。这个选择有代价，写在风险里。

### 1.4 重置令牌：一封盖了章的一次性介绍信

重置令牌像一封盖了章的一次性介绍信。信上写三件事：给哪个用户、什么时候过期、密码版本号。章是服务器密钥算出来的 HMAC。服务器不用存信的副本，收到信验章就行。

一次性：信上的密码版本号就是 `password_changed_at`。你用这封信改完密码，版本号变了，同一封信再用就对不上。不需要 `password_resets` 表，也不需要定时清理过期令牌。

30 分钟：够你把链接复制到浏览器，不够它长期躺在聊天记录里。

链接写成 `/reset#token=...`，不是 `?token=...`。浏览器不会把 `#` 后面的内容发给服务器，所以访问日志里没有令牌。页面打开后立刻用 `history.replaceState` 把地址栏里的令牌抹掉。

代价：拿到链接的人在 30 分钟内可以改密码。把它当临时密码，别贴群里。

密钥丢了：没用的链接全部失效，数据不丢；重新生成一个就行。所以密钥不跟数据库一起备份。

### 1.5 邮箱验证：一笔记录，不是一把锁

`email_verified_at` 只回答一个问题：我们确认过这个邮箱吗。

它不是门锁，不挡登录、不挡写入、不挡提醒。现在它只是账号页的一行状态。将来真的发邮件提醒了，它才用来决定「只发给已验证地址」。

为什么不做验证链接：没有发信服务时，要么把链接打印在控制台（假流程），要么把注册卡在 SMTP 上（真麻烦）。两个都比「运维手动标一下」差。所以不建 `email_verifications` 表，不做 `/verify-email`，不做重发限流。

### 1.6 密码：已经做对的，不改

密码存的是 scrypt 的哈希，不是密码本身。可以把它想成榨汁：知道渣子推不回原来的水果。每个用户加不同的盐，同样的密码榨出来也不一样。比较用 `compare_digest`，不会因为比较时间长短泄露信息。这部分批次 0 已经做对，批次 2 不动它。

批次 2 只加三条规矩：

- 新密码至少 10 位。
- 不能是常见弱密码（内置一份表，不联网）。
- 不能和邮箱 `@` 前面那段一样。

老用户的 8 位密码仍能登录，因为登录只验哈希，不重新量长度。

---

## 2. 做完后是什么样（可验收的场景）

1. 新用户注册：拿到会话 Cookie 和 `csrf_token`；`/api/me` 返回 `email_verified: false`。
2. 写请求：不带 `X-CSRF-Token` → 403 `csrf_failed`；带上 → 正常。GET 不受影响。
3. 换设备登录：旧设备的会话出现在 `/api/sessions` 里；可以在账号页踢掉；改密码后其他设备全部 401，当前设备还在。
4. 忘记密码：运维跑一条命令，打印 30 分钟链接；用户改完密码，所有旧会话失效；同一链接第二次用 → 400 `token_invalid`。
5. 重启服务：连续失败 10 次的计数还在，第 11 次仍 429。
6. 过期：`expires_at` 已过的会话 → 401；`last_seen_at` 超过 7 天的会话 → 401，行被删掉。
7. 邮箱验证：`mark-verified` 后 `/api/me` 的 `email_verified` 变 true；没有任何功能被解锁，因为本来就没锁。
8. 迁移：真实库从 `0002` 升到 `0003`，用户、记录、草稿行数不变，`PRAGMA foreign_key_check` 为空。

---

## 3. 本批做什么 / 不做什么

### 3.1 做

| 项         | 具体改动                                           | 为什么在这一批                     |
| --------- | ---------------------------------------------- | --------------------------- |
| 迁移 `0003` | `users` 两列、`sessions` 六列、`rate_limits` 新表、两个索引 | 后面四件事都要落库                   |
| CSRF      | 会话里存 token；写请求校验；Origin 校验；CORS 加头             | 账号能给别人用之前，写请求不能只靠 SameSite  |
| 会话        | 14 天绝对过期、7 天空闲过期、登录换号、列表/踢人/全退                 | 现在只有 14 天 Cookie，没有服务端过期    |
| 数据库限流     | `rate_limits` 表 + 原子自增 + 清理                    | 进程内计数重启就忘，多进程失效             |
| CLI 重置密码  | 签名令牌 + `reset-password` + `mark-verified`      | 不接 SMTP 也能让人找回密码            |
| 密码策略      | 10 位、弱密码表、邮箱本地部分                               | 注册口子不能太松                    |
| 改密码接口     | `POST /api/auth/change-password`               | 没有它，登录用户只能走运维重置；这是本批建议新增的一项 |
| 前端最小改动    | 最小 `api/client.js`、重置页、账号与安全页                  | CSRF 不同批改前端会全线 403          |
| 文档与测试     | README、`docs/auth.md`、验收清单                     | 半年后要知道为什么这么选                |

### 3.2 不做

| 不做                                        | 原因                                     |
| ----------------------------------------- | -------------------------------------- |
| SMTP、`/forgot-password`、验证邮件              | 现在没有发信服务；自助找回的门槛见 3.3                  |
| `email_verifications`、`password_resets` 表 | 签名令牌不需要落库；多两张表就多两套清理和限流                |
| OAuth、短信登录、2FA、SSO                        | 这一批解决「这次请求是不是你点的」，不解决「你是谁」             |
| 会话 token 哈希存储                             | 见 12 的已知缺口；这一批不改 `sessions` 主键，避免回滚风险  |
| Redis、Celery                              | 限流用数据库够了；识图队列是批次 3 的事                  |
| 完整错误信封、metrics、`/api/ready`               | 批次 5                                   |
| `AuthContext`、`routes.jsx`、页面大拆分          | 批次 4A/4B；批次 2 只提前一个最小的 `api/client.js` |
| 会话列表分页                                    | 一个用户的设备数是个位数                           |

### 3.3 一条门槛，写进文档

出现第一个「非运维用户」（不在本机、需要自助找回密码）时：

1. `SmtpMailer` 从「以后」变成「现在」。
2. `/forgot-password` 才允许加。
3. 那时再评估要不要邮箱验证的 token 表。

这条写进 `docs/stack-and-scope.md`，避免以后忘记为什么现在是 CLI。

---

## 4. 数据模型（迁移 0003）

迁移就是给数据库结构记一笔账：第 `0003` 号改动加了哪几列、哪张表。空库和老库都按同一本账升级，不靠人记得「这台机器上有没有加过那一列」。记这笔账的工具是 Alembic；它只改结构，不碰记录内容。

### 4.1 `users`

| 列                     | 类型   | 可空 | 默认   | 用途                      |
| --------------------- | ---- | -- | ---- | ----------------------- |
| `email_verified_at`   | TEXT | 是  | NULL | NULL = 没确认过；有值 = 运维确认过  |
| `password_changed_at` | TEXT | 否  | `''` | 注册、改密、重置时写；重置令牌的「密码版本号」 |

老行回填：`password_changed_at = created_at`。`email_verified_at` 保持 NULL，由运维决定要不要标。

### 4.2 `sessions`

| 列              | 类型   | 可空 | 默认   | 用途                         |
| -------------- | ---- | -- | ---- | -------------------------- |
| `public_id`    | TEXT | 是  | NULL | 对外 ID；**绝不把 `token` 发给前端** |
| `csrf_token`   | TEXT | 否  | `''` | 同步令牌                       |
| `last_seen_at` | TEXT | 否  | `''` | 60 秒节流更新                   |
| `user_agent`   | TEXT | 否  | `''` | 账号页显示；截断 200 字符            |
| `ip_hash`      | TEXT | 否  | `''` | HMAC 后的 IP；不存原始 IP         |
| `expires_at`   | TEXT | 否  | `''` | 登录时间 + 14 天                |

`public_id` 为什么可空：回滚到批次 1 的代码时，旧代码插会话不会写这一列，只能落 NULL。唯一索引允许多个 NULL（SQLite 和 Postgres 都允许），所以旧代码还能继续登录。新代码遇到 NULL 会补一个 ID。如果设成 `NOT NULL DEFAULT ''` 再加唯一索引，回滚后第二个会话就会撞唯一约束——这是一个很容易漏的坑。

### 4.3 `rate_limits`

| 列              | 类型               | 说明                                |
| -------------- | ---------------- | --------------------------------- |
| `bucket_key`   | TEXT PRIMARY KEY | 例如 `login:ab12cd34:a@example.com` |
| `window_start` | INTEGER          | epoch 秒；用整数避免字符串时间比较的坑            |
| `count`        | INTEGER          | 当前窗口内的次数                          |

### 4.4 索引

- `ux_sessions_public_id`：`public_id` 唯一（允许多个 NULL）。
- `ix_sessions_expires_at`：清理过期会话。
- `ix_sessions_user_id` 批次 1 已有，不重复加。

### 4.5 回填规则

迁移里用 Python 循环，不写 `datetime(created_at, '+14 days')` 这种 SQLite 方言 SQL：

1. `users.password_changed_at = created_at`。
2. 每个已有 session：
   - `public_id = secrets.token_urlsafe(9)`（循环内用 set 去重）。
   - `csrf_token = secrets.token_urlsafe(32)`。
   - `last_seen_at = created_at`。
   - `expires_at = created_at + 14 天`（解析失败就取 now + 14 天）。
3. `user_agent`、`ip_hash` 留空，下次登录才填。
4. 回填完再建唯一索引，避免中途撞值。

### 4.6 升降级

- `upgrade`：加列 → 回填 → 建 `rate_limits` → 建索引。
- `downgrade`：先删索引，再删列/表。SQLite 3.35+ 支持直接 `DROP COLUMN`；Python 3.11/3.12/3.13 自带的 SQLite 都够。
- 降级会丢掉这一批新列里的值（验证时间、会话元数据），但不动用户、记录、草稿的原有数据。这句话要写进迁移注释。

### 4.7 迁移测试

- 造一个 `0002` 的库（`command.upgrade(config, "0002")`），插一个用户 + 一个会话。
- 升到 `0003`：断言列存在、`password_changed_at == created_at`、`public_id`/`csrf_token` 非空、`expires_at` 约等于 `created_at + 14 天`。
- `downgrade` 回 `0002`：断言列和表消失，用户/会话行数不变。
- 再 `upgrade`：结构恢复。

---

## 5. 接口契约

### 5.1 总表

| 方法     | 路径                          | 登录 | CSRF | 说明                                            |
| ------ | --------------------------- | -- | ---- | --------------------------------------------- |
| POST   | `/api/register`             | 否  | 否    | 返回 `{email, csrf_token}`；201                  |
| POST   | `/api/login`                | 否  | 否    | 返回 `{email, csrf_token}`；换新会话                 |
| POST   | `/api/logout`               | 是  | 是    | 删当前会话，清 Cookie                                |
| GET    | `/api/me`                   | 是  | 否    | 增加 `email_verified`、`csrf_token`、`session_id` |
| POST   | `/api/auth/reset-password`  | 否  | 否    | `{token, password}`；限流                        |
| POST   | `/api/auth/change-password` | 是  | 是    | `{current_password, new_password}`；删其他会话      |
| GET    | `/api/sessions`             | 是  | 否    | 当前用户的会话列表                                     |
| DELETE | `/api/sessions/{id}`        | 是  | 是    | 踢掉指定会话；`id` 是 `public_id`                     |
| POST   | `/api/auth/logout-all`      | 是  | 是    | 删全部会话，清 Cookie                                |

不新增：`/verify-email`、`/resend-verification`、`/forgot-password`。

### 5.2 请求与响应示例

登录：

```json
POST /api/login
{"email": "a@example.com", "password": "..."}

200
{"email": "a@example.com", "csrf_token": "..."}
```

`/api/me`：

```json
200
{
  "email": "a@example.com",
  "email_verified": false,
  "csrf_token": "...",
  "session_id": "abc123"
}
```

会话列表：

```json
GET /api/sessions
200
{
  "sessions": [
    {
      "id": "abc123",
      "created_at": "2026-11-09T08:00:00+00:00",
      "last_seen_at": "2026-11-09T08:05:00+00:00",
      "user_agent": "Mozilla/5.0 ...",
      "current": true
    }
  ]
}
```

重置密码：

```json
POST /api/auth/reset-password
{"token": "...", "password": "新密码"}

200 {"ok": true}
400 {"detail": "链接已失效，请让管理员重新生成。", "code": "token_invalid"}
400 {"detail": "链接已过期，请让管理员重新生成。", "code": "token_expired"}
400 {"detail": "密码至少 10 位。", "code": "password_too_weak"}
429 {"detail": "尝试次数过多，请稍后再试。", "code": "rate_limited"}
```

### 5.3 错误码

| code                       | 状态  | 什么时候                       |
| -------------------------- | --- | -------------------------- |
| `csrf_failed`              | 403 | 写请求没有带或带错 `X-CSRF-Token`   |
| `origin_rejected`          | 403 | 写请求的 `Origin` 不在允许列表，也不是同源 |
| `rate_limited`             | 429 | 任一限流桶超限                    |
| `token_invalid`            | 400 | 重置令牌签名不对、用户不存在、密码版本对不上     |
| `token_expired`            | 400 | 重置令牌过期                     |
| `password_too_weak`        | 400 | 长度、弱密码表、邮箱本地部分             |
| `current_password_invalid` | 400 | 改密码时当前密码不对                 |
| `session_not_found`        | 404 | 踢一个不属于自己或已不存在的会话           |

这是错误码的最小形态：响应体是 `{detail, code}`。批次 5 再长成 `{detail, error: {code, message, request_id, fields}}`，`code` 保留一个版本做兼容。

### 5.4 CSRF 的豁免与覆盖

- 豁免：`/api/login`、`/api/register`、`/api/auth/reset-password`。它们没有会话，靠 Origin 和令牌本身保护。
- 覆盖：其余所有 `POST/PUT/PATCH/DELETE`。实现放在 `current_user` 里：认证通过后，如果是写方法，就比对 token。这样不会漏掉某个新接口。
- `OPTIONS` 不检查，CORS 预检要能过。

---

## 6. 后端实现

### 6.1 新模块 `src/security.py`

一个文件装三件事：密钥、签名令牌、脱敏。不要让 `accounts.py` 同时管数据库和密码学。

```python
def secret() -> bytes: ...
def hash_ip(ip: str) -> str: ...
def mask_email(email: str) -> str: ...
def sign_reset_token(user_id: int, password_changed_at: str, ttl: int = 1800) -> str: ...
def verify_reset_token(token: str) -> dict: ...   # 失败抛 TokenError(code)
```

令牌格式：

```
base64url({"uid":1,"exp":1762...,"pwd":"2026-11-09T..."}) + "." + base64url(HMAC-SHA256(secret, "reset:" + payload_b64))
```

要点：

- 签名消息加 `reset:` 前缀，和 `ip:` 前缀分开，避免同一把密钥在不同用途之间串用。
- 验证分两层：`security.verify_reset_token` 做「格式 → 签名（`compare_digest`）→ 过期」；`accounts.reset_password` 再做「用户存在 → `pwd` 等于当前 `password_changed_at`」。密码学不碰数据库，数据库不碰密钥。  
  |  |  |  
  | --- | --- |
- 密钥来源：`LEARNING_ASSISTANT_SECRET` 优先；否则 `database_path().parent / "secret.key"`，首次用 `O_CREAT|O_EXCL` 生成 32 字节，`0o600`。Windows 上 `0o600` 基本不生效，靠用户目录 ACL，文档里要说清楚。
- 密钥按文件路径缓存；测试换库时自然换密钥，不需要清全局状态。
- `.gitignore` 加 `data/secret.key`。
- 登录成功/失败、重置成功/失败各记一条 `event` 日志，邮箱用 `mask_email` 脱敏；`src/logging.py` 的 `_EXTRA_FIELDS` 加 `email_masked`。不记密码、token、Cookie。


### 6.2 `src/accounts.py`

这一批会大改这个文件。按批次 1 的规约「谁被改到谁换」，顺手把它从 `connect()` + 原生 SQL 换成 `session()` + `select()`。**单独一个提交**，先保证行为不变，再加新功能；这样出问题可以只回滚转换。

函数签名（计划）：

```python
def register(email, password, *, user_agent="", ip_hash="", previous_token=None) -> tuple[dict, str, str]
def login(email, password, *, user_agent="", ip_hash="", previous_token=None) -> tuple[dict, str, str]
def logout(token: str | None) -> None
def user_for_token(token: str | None) -> dict | None
def list_sessions(user_id: int, current_token: str | None) -> list[dict]
def revoke_session(user_id: int, public_id: str) -> bool
def delete_all_sessions(user_id: int) -> None
def change_password(user_id: int, current_password: str, new_password: str, keep_public_id: str | None = None) -> None
def reset_password_token(email: str) -> tuple[dict, str, str]
def reset_password(token: str, new_password: str) -> None
def mark_verified(email: str) -> str | None
```

行为细节：

- `register`：校验邮箱和密码 → 查重 → 插用户（`password_changed_at = now`）→ 删旧会话（如果有）→ 插新会话 → 返回 `(user, token, csrf_token)`。并发注册同一邮箱时唯一约束会抛 `IntegrityError`，捕获后返回 409，不要让它变成 500。
- `login`：查用户 → 验密码 → 删浏览器带来的旧会话（防固定）→ 插新会话 → 返回。用户不存在时也跑一次 scrypt 对比假哈希，避免用响应时间判断邮箱是否存在。
- `user_for_token`：查会话 + 用户 → 补 `public_id`/`csrf_token`/`expires_at`（仅当为空）→ 判绝对过期 → 判空闲过期 → 60 秒节流更新 `last_seen_at` → 返回 `{id, email, email_verified, csrf_token, session_id}`。
- `change_password`：验当前密码 → 校验新密码 → 更新哈希和 `password_changed_at` → 删其他会话（保留 `keep_public_id` 对应的当前会话）。
- `reset_password`：验令牌 → 校验新密码 → 更新哈希和 `password_changed_at` → 删全部会话。
- `reset_password_token`：查用户 → 用当前 `password_changed_at` 签令牌 → 返回 `(user, token, expires_at)`；用户不存在抛 `LookupError`。
- `mark_verified`：写 `email_verified_at = now`；返回时间戳或 None。

密码策略单独放 `src/passwords.py`：`validate_password(password, email)` 检查长度（10-128）、弱密码表、邮箱本地部分；弱密码表放 `src/data/weak_passwords.txt`，一行一个，小写，启动时读进内存。不联网，不引 `zxcvbn`。`accounts.py` 在注册、改密、重置三处调用它。

### 6.3 `src/ratelimit.py`

对外函数名尽量不变，`api.py` 少改：

```python
def login_blocked(key) -> bool
def note_login_failure(key) -> None
def clear_login_failures(key) -> None
def register_blocked(client) -> bool
def note_registration(client) -> None
def reset_blocked(client) -> bool
def note_reset(client) -> None
def change_password_blocked(user_id) -> bool
def note_change_password_failure(user_id) -> None
def cleanup(now: float | None = None) -> int
def reset() -> None   # 测试用：清空表
```

键的构造放在 `api.py`，因为那里才知道 IP 和邮箱：

- `login:{ip_hash}:{email_lower}`，15 分钟，10 次。
- `login-ip:{ip_hash}`，15 分钟，60 次（可选第二层，先写进计划，实施时如果觉得多余可以砍）。
- `register:{ip_hash}`，60 分钟，30 次。
- `reset:{ip_hash}`，60 分钟，10 次。
- `change:{user_id}`，15 分钟，5 次。

自增 SQL（SQLite 和 Postgres 都认）：

```sql
INSERT INTO rate_limits (bucket_key, window_start, count) VALUES (:key, :start, 1)
ON CONFLICT(bucket_key) DO UPDATE SET
  count = CASE WHEN rate_limits.window_start = excluded.window_start
               THEN rate_limits.count + 1 ELSE 1 END,
  window_start = excluded.window_start
```

- 检查是「读一行 + 比时间」；记录是「一次 upsert」。检查和记录分开，登录成功时才能先判断、失败时才计数。
- 清理用模块级时间戳节流：距上次清理不到 60 秒就跳过。
- 数据库异常：`login_blocked` 等返回 `False`（放行），`note_*` 吞掉异常，记一条 `rate_limit_degraded` 日志。
- 429 响应带 `Retry-After`，沿用现有登录/注册的做法；算不出精确剩余时间就写窗口长度。
- `LEARNING_ASSISTANT_RATE_LIMIT=0` 仍然全局关闭。

### 6.4 `src/api.py`

1. `current_user` 变成唯一入口：

```python
def current_user(request: Request) -> dict:
    session = user_for_token(request.cookies.get(SESSION_COOKIE))
    if session is None:
        raise HTTPException(401, "请先登录")
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and csrf_enabled():
        token = request.headers.get("X-CSRF-Token", "")
        if not hmac.compare_digest(token, session["csrf_token"]):
            raise ApiError(403, "请求校验失败，请刷新页面重试。", "csrf_failed")
    return session
```

2. `origin_guard` 中间件：写方法且带 `Origin` 时，允许「同源」或「在 `LEARNING_ASSISTANT_ALLOWED_ORIGINS` 里」；否则 403 `origin_rejected`。`OPTIONS` 跳过。反向代理要开 `--proxy-headers`，否则同源判断会看错 scheme/host；看错时用 allowlist 兜底。
3. CORS 的 `allow_headers` 加 `X-CSRF-Token`。
4. 登录/注册：把 `previous_token`、`user_agent`、`ip_hash` 传进 `accounts`；响应加 `csrf_token`。
5. `/api/me`：加 `email_verified`、`csrf_token`、`session_id`。
6. 新接口按第 5 节实现；`logout` 改成依赖 `current_user`，这样也受 CSRF 保护。
7. 顺手加四个响应头（4 行）：`X-Frame-Options: DENY`、`Content-Security-Policy: frame-ancestors 'none'`、`X-Content-Type-Options: nosniff`、`Referrer-Policy: same-origin`。前两个挡点击劫持，后两个是便宜的默认值。
8. `ApiError` + 异常处理器：返回 `{detail, code}`；普通 `HTTPException` 不变。

### 6.5 `src/db/engine.py`：补批次 1 的一个洞

`session()` 用的 SQLAlchemy 连接没有跑 `PRAGMA busy_timeout / foreign_keys / synchronous`。批次 1 只给 `sqlite_adapter.connect()` 配了，ORM 连接漏了。这一批的新代码要用 `session()`，必须补上，否则外键不生效、锁等待也没有。

做法：

- `sqlite_adapter.py` 新增 `configure_dbapi(dbapi_connection)`，里面还是那三条 `PRAGMA`（方言留在 adapter）。
- `engine.py` 对 SQLite engine 注册 `connect` 事件，调用这个函数。
- 加一个测试：`session()` 里 `PRAGMA foreign_keys` 返回 1，`busy_timeout` 返回 5000。

这是批次 1 的补丁，单独一个提交。

### 6.6 `src/db/models.py`

- `User` 加 `email_verified_at`（可空）、`password_changed_at`。
- `UserSession` 加六个字段和两个索引；`public_id` 可空。
- 新增 `RateLimit` 模型。
- 结构测试会自动比对，漏一列就红。

### 6.7 CLI

```bash
python -m src.accounts reset-password <email>
python -m src.accounts mark-verified <email>
python -m src.accounts sessions <email>
```

- `reset-password` 打印：

```text
重置链接（30 分钟内有效，只能用一次）：
http://127.0.0.1:5173/reset#token=...
如果这是服务器，先设置 LEARNING_ASSISTANT_WEB_BASE 再生成链接。
```

- `mark-verified` 打印写入的时间；已经是已验证就打印「已经是已验证状态」，退出码 0。
- `sessions` 列出 `public_id`、创建时间、最近使用、UA 前 60 字，方便运维判断哪台设备要踢。
- 退出码：0 成功，2 用户不存在，1 其他错误。
- 入口先调 `bootstrap()`，保证迁移已跑。

### 6.8 文档

- `docs/auth.md`：三个凭证（密码哈希、会话 Cookie、CSRF token）、会话生命周期、重置流程、限流表、邮箱验证「是什么/不是什么」、威胁模型与已知缺口、环境变量和运维命令。
- `README.md`：新增环境变量（`LEARNING_ASSISTANT_SECRET`、`LEARNING_ASSISTANT_WEB_BASE`、`LEARNING_ASSISTANT_CSRF`、`LEARNING_ASSISTANT_ALLOWED_ORIGINS`）、CLI 命令、重置流程、「老 8 位密码仍可登录」。
- `docs/stack-and-scope.md`：写入 3.3 的门槛。
- `docs/checklist.md`：全部验收通过后再打勾，写日期。

---

## 7. 前端改动

### 7.1 提前一个最小的 `frontend/src/api/client.js`

原计划把这个文件放在批次 4A。这一批要改 13 个写请求，如果先在各处手写 `X-CSRF-Token`，4A 还要再改一遍。所以提前一个**最小版**：只做 credentials、CSRF 头、原样透传 body。错误映射、AbortSignal、JSON 自动序列化留给 4A。

```js
let csrfToken = "";

export function setCsrfToken(token) { csrfToken = token || ""; }
export function getCsrfToken() { return csrfToken; }

export function apiFetch(path, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  const unsafe = !["GET", "HEAD", "OPTIONS"].includes(method);
  return fetch(path, {
    ...options,
    credentials: "include",
    headers: {
      ...(options.headers || {}),
      ...(unsafe && csrfToken ? { "X-CSRF-Token": csrfToken } : {}),
    },
  });
}
```

不自动 `JSON.stringify`，因为现有调用点已经自己序列化了；自动加会双重编码。

### 7.2 接线

- `submit("/api/login")` / `submit("/api/register")`：成功后 `setCsrfToken(user.csrf_token)`。
- 首次 `/api/me`：成功后 `setCsrfToken(body.csrf_token)`。
- `signOut`：用 `apiFetch`；无论返回 200 还是 401（会话已经过期），都清本地状态和 `setCsrfToken("")`。
- 13 个写调用点从 `fetch(` 换成 `apiFetch(`，去掉 `credentials: "include"`（client 已加）。
- 403 `csrf_failed`：提示「页面已过期，请刷新后重试」，并重新拉一次 `/api/me` 更新 token；不自动重放请求，避免 FormData 重放的坑。

### 7.3 重置页 `/reset`

- 位置：`App` 里所有 hook 之后、`if (!me)` 之前判断 `location.pathname === "/reset"`，直接渲染 `ResetPage`。否则未登录时永远进不到这个页面。
- 读 `location.hash` 里的 `token=`；没有就显示「链接不完整」。
- 挂载后 `history.replaceState(null, "", "/reset")`，把地址栏里的令牌抹掉。
- 表单：新密码、确认新密码；前端只校验「两次一致、至少 10 位」，弱密码表交给后端。
- 成功：显示「密码已重置，请用新密码登录」+ 回登录页的链接。
- 失败：按 `code` 显示 `token_invalid` / `token_expired` / `password_too_weak` 的文案。
- 部署时静态站点要支持 SPA 回退（`/reset` 也返回 `index.html`），否则直接打开链接会 404。Vite 开发服务器默认就会回退。

### 7.4 账号与安全页 `/settings/account`

一个紧凑页面，放在现有 `App.jsx` 里，批次 4A 再搬：

- 邮箱、验证状态（未验证只显示一行「不影响使用」，不放按钮）。
- 当前会话 + 其他会话列表；其他会话有「退出」按钮；当前会话显示「当前浏览器」。
- 「退出所有设备」按钮，二次确认。
- 改密码表单：当前密码、新密码、确认；成功后提示「其他设备已退出」。
- 侧栏加 `NavLink to="/settings/account"`。

### 7.5 登录表单的 8/10 位问题

登录和注册共用一个输入框，现在 `minLength={8}`。不能改成 10，否则老用户连登录都点不出去。保持 8，加一行提示：

> 注册密码至少 10 位；老账号仍可用原密码登录。

注册的 10 位由后端强制，错误信息照常显示。

### 7.6 429 和 403 的文案

- 429：显示后端 `detail`，并提示「过几分钟再试」。
- 403 `origin_rejected`：显示「请求来源不对，请从本站页面操作」。
- 403 `csrf_failed`：见 7.2。

---

## 8. 实施顺序

每一步一个提交，最后合成一个 PR、打 `batch-2` tag。每步结束都跑对应检查点，不攒到最后。

| 步 | 做什么 | 检查点 |
| --- | --- | --- |
| 0 | 建分支 `batch-2-accounts`；`python scripts/backup_db.py`；记录用户/会话/草稿行数；跑一遍现有测试 | 备份 `integrity_check=ok`；现有测试全绿 |
| 1 | `sqlite_adapter.configure_dbapi` + `engine.py` 事件 + 测试 | `session()` 的 `foreign_keys=1`、`busy_timeout=5000` |
| 2 | 迁移 `0003` + 模型 + 迁移/结构测试 | 真库副本升/降级；行数不变；`foreign_key_check` 为空 |
| 3 | `src/security.py` + 令牌单元测试 | 签名、篡改、过期、版本不匹配四类用例过 |
| 4 | `accounts.py` 换 `session()`（行为不变） | 现有认证测试全过；不新增功能 |
| 5 | 密码策略 + CLI + 改密码 | CLI 在临时库跑通；老 8 位用户仍能登录 |
| 6 | 会话过期、空闲、换号、列表/踢人/全退 | 过期/空闲/固定/节流四类测试过 |
| 7 | CSRF + Origin + `/api/me` 字段 | curl 不带头 403、带头 200 |
| 8 | 数据库限流 | 重启后计数还在；窗口滚动；清理只跑一次 |
| 9 | 前端 client、重置页、账号页 | `npm run build`；手动走注册→写→改密→重置 |
| 10 | 文档、全量测试、备份、tag | 第 10 节验收全过 |

第 4 步单独提交很重要：它是「只换写法、不改行为」。如果第 5-8 步出问题，可以只回滚后面的提交，不用把 ORM 转换一起退掉。

---

## 9. 测试计划

### 9.1 新增

| 文件 | 覆盖 |
| --- | --- |
| `tests/test_security.py` | 令牌签名/篡改/过期/版本不匹配；`hash_ip` 稳定；`mask_email` |
| `tests/test_csrf.py` | 无头 403、带头 200、GET 不受影响、豁免接口、Origin 拒绝、开关关闭 |
| `tests/test_sessions.py` | 绝对过期、空闲过期、登录换号、`last_seen_at` 60 秒节流、列表、踢人、全退 |
| `tests/test_password_policy.py` | 9 位拒绝、10 位通过、弱密码表、邮箱本地部分、老 8 位用户可登录 |
| `tests/test_accounts_cli.py` | 重置链接、重置成功、旧会话 401、同一链接第二次失败、过期失败、`mark-verified` |
| `tests/test_ratelimit_db.py` | 跨客户端持久、窗口滚动、清理、关闭开关、数据库异常放行 |

### 9.2 改写

| 文件 | 改动 |
| --- | --- |
| `tests/test_ratelimit.py` | 旧的 `SlidingWindow` 测试删掉；改成临时库上的固定窗口测试 |
| `tests/test_api.py` | 约 20 处写请求加 `headers=_csrf(client)`；新增 `/api/me` 字段、429、403 用例 |
| `tests/test_migrations.py` | `HEAD` 改 `0003`；加 0003 回填/降级用例 |
| `tests/test_schema_models.py` | `HEAD` 改 `0003`；新表自动纳入比对 |
| `tests/test_frontend.py` | 断言 `client.js` 含 `X-CSRF-Token`、`App.jsx` 用 `apiFetch`、重置页和账号页文案存在 |

### 9.3 关键用例（按行为写，不按函数写）

1. 注册后 `GET /api/me` 有 `csrf_token`；不带头的 `POST /api/records` 是 403；带头是 201。
2. 登录两次（同一浏览器）：Cookie 变了；旧 token 在库里查不到。
3. 把 `expires_at` 改成昨天：下一次请求 401，会话行被删。
4. 把 `last_seen_at` 改成 8 天前：下一次请求 401。
5. 连续两次请求：`last_seen_at` 只写一次；把它改成 2 分钟前，再请求才更新。
6. 两个客户端登录同一账号：A 踢掉 B；B 401；A 还能用。
7. A 改密码：A 还在；B 401；新密码能登录；旧密码不能。
8. 重置令牌用一次成功；第二次 400；过期令牌 400；改完密码旧会话全 401。
9. 失败登录 10 次后，换一个 `TestClient`（模拟重启）仍然 429；`rate_limits` 里 count = 10。
10. 插入一条 25 小时前的 `rate_limits` 行，调用 `cleanup()` 后被删。
11. `LEARNING_ASSISTANT_CSRF=0` 时无头写请求通过；`LEARNING_ASSISTANT_RATE_LIMIT=0` 时 11 次失败不 429。
12. 迁移：0002 库带一个用户和一个会话，升到 0003 后 `public_id`/`csrf_token` 非空，`expires_at` 约等于 `created_at + 14 天`。



---

## 10. 验收清单

### 10.1 自动

```bash
python -m pytest -q --ignore=tests/test_frontend.py
cd frontend && npm run build
python -m src.validate_records data/records.csv --min-rows 3
python scripts/backup_db.py
```

四条都要过。测试数会从现在的 60 多个涨到 90 多个。

### 10.2 手动（用临时库，不碰真实数据）

```bash
export LEARNING_ASSISTANT_DB=/tmp/batch2-check.sqlite   # Windows 用 set 或 $env:
python -m uvicorn src.api:app --host 127.0.0.1 --port 8010
```

1. 注册 → `/api/me` 拿到 `csrf_token`。
2. 先 `POST /api/records` 建一条记录，拿到 id；不带 `X-CSRF-Token` 的 `PUT /api/mine/{id}` → 403 `csrf_failed`；带上 → 200。
3. `python -m src.accounts reset-password <email>` → 打开链接 → 改密码 → 旧 Cookie 401 → 同一链接第二次 400。
4. 连续失败登录 10 次 → 第 11 次 429 → 重启 uvicorn → 仍然 429。
5. `python -m src.accounts mark-verified <email>` → `/api/me` 的 `email_verified` 为 true。
6. 账号页：能看到两个会话，踢掉一个，另一个还在；「退出所有设备」后全部 401。

### 10.3 迁移（真实库副本）

```bash
python scripts/backup_db.py          # 生成 data/backups/app-YYYYMMDD-HHMMSS.sqlite，跑 integrity_check
LEARNING_ASSISTANT_DB=data/backups/app-YYYYMMDD-HHMMSS.sqlite \
  python -c "from src.db import bootstrap; bootstrap()"
```

对比升级前后的 `users`、`user_records`、`image_drafts` 行数；`PRAGMA foreign_key_check` 为空。不要直接 `cp` 正在写入的库；`backup_db.py` 用的是 SQLite 的在线备份接口。

---

## 11. 回滚与开关

| 场景 | 动作 |
| --- | --- |
| CSRF 打断旧前端 | `LEARNING_ASSISTANT_CSRF=0`（只关 token 校验，Origin 仍开），保留一个版本 |
| 限流误伤 | `LEARNING_ASSISTANT_RATE_LIMIT=0` |
| 代码整体回滚 | 回到 `batch-1` tag。`0003` 的列都有默认值或可空，旧代码的 `INSERT` 不会失败；`public_id` 可空保证旧代码能连开多个会话 |
| 迁移失败 | 中止启动，用 `data/backups/pre-migration-*.sqlite` 恢复；不手工改表 |
| 重置链接泄露 | 等 30 分钟过期，或改一次密码让版本号失效；不需要删表 |
| 密钥文件丢失 | 未使用的重置链接失效；重新生成即可，数据不丢 |

回滚后旧代码不读新列，`rate_limits` 表留着不用。这是「结构向前、代码可退」的设计。

---

## 12. 风险与已知缺口

| 风险 | 触发 | 缓解 |
| --- | --- | --- |
| CSRF 上线打断旧前端 | 前端没同批更新 | 同批改 13 个写调用点；开关；手动走一遍 |
| `last_seen_at` 写太多 | 每个请求都写 | 60 秒节流；只对已登录请求写 |
| 限流表被锁 | 并发写 | `busy_timeout=5000`；捕获 `OperationalError` 重试一次；仍失败就放行并记日志 |
| 固定窗口边界突刺 | 窗口交界 | 接受；登录限流不是计费 |
| 重置令牌被转发 | 链接贴群里 | 30 分钟；一次性；`#` 不进日志；文档提醒 |
| 迁移回填慢 | 会话行很多 | 现在个位数；循环即可，不预优化 |
| CLI 在非运维用户出现后不成立 | 第二个真实用户 | 3.3 的门槛：SMTP 从「以后」变「现在」 |
| 老 8 位密码用户被前端拦住 | 输入框改成 10 位 | 登录框保持 8；只在后端管注册/重置 |

已知缺口，写清楚不装：

- **没有自助找回密码。** 需要能登录服务器的人跑命令。
- **`email_verified_at` 现在不产生安全收益**，只是记录；将来邮件提醒才用得上。
- **CSRF 不防 XSS。** 页面里能跑 JS 的人可以读 token。React 默认转义，本批不引入 `dangerouslySetInnerHTML`。
- **会话 token 在库里是明文。** 数据库文件泄露等于会话可被劫持；但那时用户数据也已经泄露。等真正多用户公网部署再评估哈希存储。
- **固定窗口是近似限流**，检查与自增之间有竞态，最多多放过几次。
- **`last_seen_at` 有 60 秒粒度**，空闲过期最多晚 60 秒生效。
- **CLI 依赖服务器权限。** 这是有意选的通道，不是遗漏。

---

## 13. 工作量与可裁剪

| 块 | 估时 |
| --- | --- |
| 迁移 0003 + 模型 + 测试 | 0.4 人日 |
| `security.py` + 令牌测试 | 0.3 人日 |
| `accounts.py` 换 `session()` | 0.3 人日 |
| 密码策略 + CLI | 0.4 人日 |
| 会话 + CSRF 后端 | 0.5 人日 |
| 数据库限流 | 0.3 人日 |
| 前端（client、重置页、账号页、13 个调用点） | 0.7 人日 |
| 测试改写 + 文档 + 收尾 | 0.7 人日 |
| **合计** | **约 3.0 人日**（2.5-3.5） |

比原计划的 1-2 人日多，主要多在两处原计划没算的：`tests/test_api.py` 约 20 处写请求要加 CSRF 头；前端要新增重置页和账号页。这两块不是可选的，除非砍功能。

可裁剪，按优先级从高到低：

1. 账号页的「会话列表 + 踢人」只保留「退出所有设备」按钮（省 0.2 人日）。
2. 改密码接口砍掉，只留 CLI 重置（省 0.2 人日）。
3. `login-ip` 第二层限流砍掉（省 0.1 人日）。

不建议裁：CSRF、会话过期、数据库限流、CLI 重置、迁移。这五件是这一批的骨架。

---

## 14. 与原 v2 计划的差异（需要你拍板）

| # | 原计划 | 这份计划 | 理由 |
| --- | --- | --- | --- |
| 1 | `sessions` 加 `csrf_token` 等五列 | 多加一个可空的 `public_id` | 踢会话需要对外 ID；绝不能把 `token` 发给前端。可空是为了回滚兼容 |
| 2 | 没有改密码接口 | 加 `POST /api/auth/change-password` | 否则登录用户改密码只能走运维；也让 `password_changed_at` 的语义完整 |
| 3 | `src/mail/` + `OutboxMailer` | 不做 | 现在没有调用方。等真发邮件提醒时，按真实需求设计接口，不提前铺空目录 |
| 4 | 重置链接 `?token=` | `#token=` | `#` 后面的内容不会发给服务器，访问日志里没有令牌 |
| 5 | `api/client.js` 在批次 4A | 批次 2 提前一个最小版 | 避免 13 个写调用点改两遍；4A 只扩展，不推翻 |
| 6 | 估 1-2 人日 | 估 2.5-3.5 人日 | 测试改写和前端页面在原估算里没算 |
| 7 | 未提 | 补 `session()` 连接的 PRAGMA | 批次 1 的洞；不补的话 ORM 连接外键不生效、锁等待也没有 |

需要你拍板的三件：

- **A. 改密码接口**：加（推荐）还是只留 CLI 重置？
- **B. `src/mail/`**：砍（推荐）还是保留 40 行协议？
- **C. 账号页**：这一批做（推荐，约 0.2 人日）还是等批次 4A 的骨架？

已拍板（2026-11-09）：A 加、B 砍、C 本批做。按本文执行。

其余按本文执行。你确认后，我按第 8 节的顺序做，每完成一步给一条「改了什么、怎么验」的短报告，最后合成一个 PR 再打 tag。

---

## 15. 名词对照

| 词 | 一句话 |
| --- | --- |
| CSRF | 别人借你的登录状态，让你的浏览器替他发写请求 |
| CSRF token | 只有本站页面读得到的随机口令，写请求必须带 |
| SameSite=Lax | 浏览器规定「跨站 POST 不带 Cookie」，但不覆盖同站和旧浏览器 |
| Origin | 浏览器告诉服务器「这个请求从哪个网站来」 |
| 会话固定 | 坏人先给你一个他知道的会话编号，你登录后他也能用 |
| 绝对过期 | 从登录起算，到点就失效，不管用不用 |
| 空闲过期 | 连续一段时间没请求就失效 |
| 固定窗口限流 | 一个时间格子里计数，格子到期归零 |
| HMAC | 用密钥给一段数据盖章；没有密钥改不了章 |
| scrypt | 故意很慢的密码哈希，偷到库也不好暴力猜 |
| HttpOnly | JavaScript 读不到这个 Cookie，降低 XSS 偷会话的风险 |
| 软验证 | 只记录「确认过邮箱」，不拿它挡任何功能 |
| CORS | 浏览器规定「一个网站不能随便读另一个网站的响应」，要服务器明确允许 |
| 迁移 | 给数据库结构记一笔账；空库和老库按同一本账升级 |
| Alembic | 记这笔账、并按账升级的工具 |
