# 账号与安全

这一页写清楚批次 2 之后「账号」这一层怎么工作，以及哪些事是故意不做的。日期：2026-11-09。

## 能做什么

- 邮箱 + 密码注册、登录、退出。
- 会话 14 天绝对过期，连续 7 天不用也会失效。
- 账号页能看到登录的设备，踢掉某一台，或退出所有设备。
- 登录用户可以改密码；忘了密码可以找运维生成一条 30 分钟的一次性链接。
- 邮箱验证只记一笔「确认过」，不挡任何功能。

不做什么：自助找回密码、验证邮件、OAuth、短信、2FA、会话 token 哈希存储。原因见最后一节。

## 三个凭证

| 凭证 | 存在哪 | 作用 | 泄露的后果 |
| --- | --- | --- | --- |
| 密码哈希 | `users.password_hash` | 验证你是你 | 不能还原密码；但弱密码可能被猜 |
| 会话 Cookie | 浏览器 `session`，HttpOnly | 证明这次请求已登录 | 等于登录态被拿走，直到会话过期 |
| CSRF token | `sessions.csrf_token` | 证明写请求来自本站页面 | 配合 XSS 才能用；单独泄露危害小 |

密码用 scrypt（n=2^14, r=8, p=1），每个用户独立盐；比较用 `hmac.compare_digest`。登录不检查密码长度，所以批次 2 之前注册的 8 位密码仍能登录。

## 登录流程

```text
POST /api/login
  → 限流检查（IP+邮箱、IP 两个桶）
  → 查用户；用户不存在也跑一次假哈希，避免用响应时间判断邮箱是否存在
  → 验密码（scrypt）
  → 删掉浏览器带来的旧会话（防会话固定）
  → 新建会话：token / csrf_token / public_id / expires_at / last_seen_at
  → Set-Cookie + 返回 csrf_token
GET /api/me
  → 查会话：绝对过期？空闲过期？→ 401 并删行
  → 60 秒节流更新 last_seen_at
  → 返回 email / email_verified / csrf_token / session_id
POST /api/mine/1
  → 查会话（同上）
  → 比对 X-CSRF-Token
  → 业务
```

## 会话

- `expires_at` = 登录时间 + 14 天。到点就失效，不管用不用。
- `last_seen_at` 超过 7 天 → 失效。最多 60 秒写一次，避免每个请求都写库。
- 登录成功换新 token，旧的删掉。这是防会话固定：坏人先塞给你一个他知道编号的会话，你登录后如果编号不变，他也能用。
- 改密码保留当前会话，删其他会话；重置密码删全部会话。
- `public_id` 是对外 ID，账号页踢人用它。会话 `token` 本身永远不发给前端。
- 回滚兼容：`public_id` 可空，唯一索引允许多个 NULL。旧代码插会话不写这一列也能成功。

## CSRF

写请求（POST/PUT/PATCH/DELETE）必须带 `X-CSRF-Token`，值和 `sessions.csrf_token` 一致。GET 不需要。

豁免：`/api/login`、`/api/register`、`/api/auth/reset-password`。它们没有会话，靠 Origin 和令牌本身保护。

Origin 校验是第二道锁：写请求带 `Origin` 时，必须是同源，或在 `LEARNING_ASSISTANT_ALLOWED_ORIGINS` 里；`Origin: null` 直接拒绝。反向代理要开 `--proxy-headers`，否则同源判断会看错 scheme/host。

为什么 SameSite=Lax 不够：`localhost` 上所有端口算同一栋楼（same-site），本机另一个网页仍然算「自家」；老浏览器和内嵌网页也不一定执行。CSRF token 是主锁，Origin 是补充。

关掉开关：`LEARNING_ASSISTANT_CSRF=0`。只关 token 校验，Origin 仍然开。保留一个版本，确认前端都更新后删掉。

## 重置密码

运维生成：

```bash
python -m src.accounts reset-password <email>
```

输出一条 `http://127.0.0.1:5173/reset#token=...`。`#` 后面的内容不会发给服务器，访问日志里没有令牌；页面打开后立刻用 `history.replaceState` 把地址栏里的令牌抹掉。

令牌格式：

```text
base64url({"uid":1,"exp":1762...,"pwd":"2026-11-09T..."}) + "." + base64url(HMAC-SHA256(secret, "reset:" + payload))
```

- 30 分钟过期。
- 一次性：`pwd` 是签发时的 `password_changed_at`。改完密码这一列变了，同一封信再用就对不上。
- 不需要 `password_resets` 表，也没有定时清理。
- 密钥：`LEARNING_ASSISTANT_SECRET` 优先（至少 32 个字符）；否则 `data/secret.key`，首次自动生成 32 字节。Windows 上 `0o600` 基本不生效，靠用户目录 ACL。
- 密钥丢了：没用的链接全部失效，数据不丢。密钥不跟数据库一起备份。

接口：`POST /api/auth/reset-password`，body `{token, password}`。成功后删该用户全部会话，返回 `{ok: true}`，不自动登录。

## 改密码

`POST /api/auth/change-password`，body `{current_password, new_password}`。需要登录 + CSRF。成功后保留当前会话，删其他会话。当前密码错 5 次 / 15 分钟会被限流。

## 限流

计数在 `rate_limits` 表里：重启还在，多进程共享。固定窗口，允许边界突刺（窗口交界最多放过两倍）。数据库出错时放行并记 `rate_limit_degraded`，不能因为限流表把主人锁在门外。

| 桶 | 键 | 窗口 | 次数 |
| --- | --- | --- | --- |
| 登录失败 | IP + 邮箱 | 15 分钟 | 10 |
| 登录 IP | IP | 15 分钟 | 60 |
| 注册 | IP | 60 分钟 | 30 |
| 重置 | IP | 60 分钟 | 10 |
| 改密码 | 用户 | 15 分钟 | 5 |

清理：每 60 秒最多清一次 24 小时前的行。`LEARNING_ASSISTANT_RATE_LIMIT=0` 全局关闭。

## 邮箱验证

`users.email_verified_at` 只回答「我们确认过这个邮箱吗」。它不是门锁：不挡登录、不挡写入、不挡提醒。现在只是账号页的一行状态。

运维手动标：

```bash
python -m src.accounts mark-verified <email>
```

将来真的发邮件提醒了，它才用来决定「只发给已验证地址」。现在不做 `/verify-email`、不发验证邮件、不建 `email_verifications` 表。

## 运维命令

```bash
python -m src.accounts reset-password <email>   # 30 分钟一次性链接
python -m src.accounts mark-verified <email>    # 标记邮箱已验证
python -m src.accounts sessions <email>         # 列出会话，判断哪台设备要踢
```

退出码：0 成功，2 用户不存在，1 其他错误。

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `LEARNING_ASSISTANT_SECRET` | 空 | 至少 32 字符；不设就用 `data/secret.key` |
| `LEARNING_ASSISTANT_WEB_BASE` | `http://127.0.0.1:5173` | 重置链接的前缀；部署时改成公网地址 |
| `LEARNING_ASSISTANT_CSRF` | 开 | `0` 关闭 token 校验（Origin 仍开） |
| `LEARNING_ASSISTANT_ALLOWED_ORIGINS` | 空 | 逗号分隔的额外 Origin，例如 `https://app.example.com` |
| `LEARNING_ASSISTANT_RATE_LIMIT` | 开 | `0` 关闭全部限流 |
| `LEARNING_ASSISTANT_LOGIN_FAILURE_LIMIT` | 10 | 登录失败次数 |
| `LEARNING_ASSISTANT_LOGIN_IP_LIMIT` | 60 | 单 IP 登录失败次数 |
| `LEARNING_ASSISTANT_REGISTER_LIMIT` | 30 | 单 IP 每小时注册次数 |
| `LEARNING_ASSISTANT_RESET_LIMIT` | 10 | 单 IP 每小时重置尝试 |
| `LEARNING_ASSISTANT_CHANGE_PASSWORD_LIMIT` | 5 | 单用户 15 分钟改密码失败次数 |
| `LEARNING_ASSISTANT_SECURE_COOKIES` | 自动 | HTTPS 下强制给 Cookie 加 Secure |

## 已知缺口

- **没有自助找回密码。** 需要能登录服务器的人跑命令。出现第一个非运维用户时，SMTP 从「以后」变成「现在」。
- **`email_verified_at` 现在不产生安全收益**，只是记录。
- **CSRF 不防 XSS。** 页面里能跑 JS 的人可以读 token。React 默认转义，本批不引入 `dangerouslySetInnerHTML`。
- **会话 token 在库里是明文。** 数据库文件泄露等于会话可被劫持；但那时用户数据也已经泄露。等真正多用户公网部署再评估哈希存储。
- **固定窗口是近似限流**，检查与自增之间有竞态，最多多放过几次。
- **`last_seen_at` 有 60 秒粒度**，空闲过期最多晚 60 秒生效。
- **CLI 依赖服务器权限。** 这是有意选的通道，不是遗漏。
