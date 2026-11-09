# 批次 3：识图异步化（详细计划）

日期：2026-11-09  
范围：`_analysis/learning-assistant`  
状态：待确认。本文只是计划，没有改代码、没有跑迁移、没有动任何数据。  
前置：批次 0/1/2 已完成，数据库在 Alembic `0003`，tag `batch-2`。  
预计：4.5-5.2 人日（一个人），含可选的粘贴截图。

## 0. 先看这三句

1. 这一批只解决一件事：**上传一张图不再卡住整个服务**。不碰日期规则，不碰 `data/records.csv`，不碰时间线和提醒的判断，不碰复盘正文。
2. 做完之后：上传 300 毫秒内返回 202；识别在后台两个线程里跑；前端每 800 毫秒问一次进度；同一张图不重复识别；一个人最多 2 个在途、全局最多 20 个排队，超了当场拒绝；服务重启不静默丢任务。
3. 最大的坑不是线程池，是**状态语义**。`status` 和 `ocr_state` 必须分开；而且 `queued` 状态下绝不能让人点「确认写入」——现有 `confirm_draft()` 会把没识别完的草稿**误判成「人工补录」并真的写进我的记录**。这是本批必须堵住的洞，不是可选项。

为什么现在做：批次 2 解决的是「账号能不能给别人用」，是防未来的坑；批次 3 治的是现在的病。`src/api.py:622` 那个 `async def` 里直接调同步的 `create_draft()`，FastAPI 不会把它丢线程池，所以识别期间整个事件循环停摆，`/api/timeline`、`/api/health` 全在门口排队。这是当前唯一的 P0。

---

## 1. 六个概念，用大白话讲

### 1.1 事件循环：只有一个服务员的饭馆

FastAPI 跑在一个叫「事件循环」的东西上。可以把它想成一个服务员：他负责接单、传菜、结账，动作很快，但他**一次只能做一件事**。

正常情况下一单只花几毫秒，所以他看起来像能同时招呼十桌。

问题在于：如果某一桌点了道要炖一小时的菜，而这个服务员**亲自进后厨炖**，那这一小时里全店没人接单。这就是现在上传图片发生的事。

`async def` 的意思是「这个函数归服务员管」。归他管，就意味着他调用任何**同步**函数时，都必须站在那儿等——他不会自己去找别人帮忙。

`create_draft()` 是同步函数。所以第 630 行那句 `return create_draft(...)` 相当于让服务员亲自进后厨。

### 1.2 为什么会卡 60 秒：两次 PowerShell

`create_draft()` 里藏着两次「起一个 PowerShell 进程」：

| 位置 | 调用 | 超时 |
| --- | --- | --- |
| `src/image_drafts.py:27` | `engine_status()` → `windows_status()` → `_run(mode="status")` | 30 秒 |
| `src/image_drafts.py:29` | `transcribe()` → `_run(mode="recognize")` | 30 秒 |

Windows 的 OCR 引擎（`Windows.Media.Ocr`）只能从 .NET 里调，Python 只能通过 PowerShell 转一手。起进程本身就慢——几百毫秒到几秒；识别一张海报再花几秒。**最坏情况 30 + 30 = 60 秒。**

更浪费的是：`engine_status()` **没有缓存**。打开识图页要起一次，每次上传要起一次，刷新状态又要起一次。它只是问「系统装没装中文识别包」，答案一分钟内不会变。

### 1.3 202 和轮询：取了号看屏幕

银行办业务：以前是把材料递进窗口，然后站在窗口前等柜员办完，后面的人全堵着。现在改成取号——柜员收下材料，给你一张号单，你退到一边看屏幕。

- **202** 就是「收下了，正在办」。它不是成功，是「已受理」。
- **轮询**就是看屏幕。前端每 800 毫秒问一次 `GET /api/image-drafts/{id}`，直到 `ocr_state` 变成 `done` 或 `failed`。

为什么不用 WebSocket 或 SSE 推送：这个项目只有一个前端、一个后端、一次最多几个人用，轮询够用，而且不需要新依赖、不需要处理断线重连。将来真需要推送再换，接口形状不用变。

### 1.4 两个状态：草稿的生死 vs 任务的进度

这是本批最容易做错的地方。

| 字段 | 回答什么问题 | 取值 |
| --- | --- | --- |
| `status` | 这份草稿还是活的吗 | `pending` / `confirmed` / `cancelled` |
| `ocr_state` | 识别跑到哪一步了 | `queued` / `running` / `done` / `failed` |

为什么必须分开：**「识别失败了」和「草稿废了」是两件事。** 识别失败之后，草稿仍然活着，用户还能人工补录、还能确认写入。如果把两者合成一个字段，就会出现「`failed` 到底是草稿作废还是识别作废」的歧义，而且以后想加「重试」就得再拆一次。

组合起来有意义的只有这几种：

| `status` | `ocr_state` | 含义 |
| --- | --- | --- |
| `pending` | `queued` | 刚上传，排队中 |
| `pending` | `running` | 正在识别 |
| `pending` | `done` | 识别完，等用户确认或改 |
| `pending` | `failed` | 识别失败，可以人工补录或重试 |
| `confirmed` | `done` | 已写入记录 |
| `cancelled` | 任意 | 用户放弃，识别结果到了也丢弃 |

### 1.5 有界队列：最多排 20 个任务，满了就劝退

先纠正一个容易被读错的措辞：**20 是任务槽位，不是在线人数**。真实容量是「2 个正在识别 + 20 个等待 = 22 个在途任务」。打开页面挂着不动的人**一个位都不占**——轮询只是一次几毫秒的 `GET`，60 秒后前端自己停。

队列用 `queue.Queue(maxsize=20)`，工人用 `ThreadPoolExecutor(max_workers=2)`。

- **为什么是 2 个工人**：每个工人会起一个 PowerShell 进程。两个并行已经能吃掉这台机器不少内存和句柄，再多会互相拖慢。而且 SQLite 只有一个写者，工人越多撞锁越频繁。
- **为什么队列要上限**：不设上限，一百个人同时传图就是一百个任务堆在内存里，服务会被拖死。设 20，满了直接返回 503 并告诉用户「稍后再试」，这是**可控的拒绝**，比**不可控的崩溃**好。
- **队列里只放 `draft_id`，不放图片字节**。一张图最大 8MB，20 张就是 160MB 全压在内存里。工人拿到 id 再去私有目录读原图，内存只占正在识别的那一两张。

**入队必须原子。** 19 个在排队、两个人同时点上传时，「判断满没满」和「放进去」必须是一步：

```python
try:
    queue.put_nowait(draft_id)          # 满了抛 queue.Full
except queue.Full:
    raise QueueFull()
```

不能写成 `if queue.qsize() < 20: queue.put(...)`——那是 check-then-act，两个请求都会读到 19，最后挤进 21 个。`Queue` 内部有锁，`put_nowait` 判满和入队是一步完成的，不需要自己再加锁。

**三道闸，顺序不能反。** 队列上限是最后一道，不是第一道——一个人狂点就能吃掉全部 20 个位，别人全被 503：

| 闸 | 限制 | 拒绝 | 作用 |
| --- | --- | --- | --- |
| 1 | 单用户在途草稿 ≤ 2（`queued` + `running`） | 429 `draft_busy` | 防一个人占满全局队列 |
| 2 | 单用户每小时上传 ≤ 20 次（复用 `src/ratelimit.py`） | 429 `upload_rate_limited` | 防脚本刷 |
| 3 | 全局等待队列 20 | 503 `ocr_queue_full` + `Retry-After` | 防整机被拖死 |

第 2 道闸用的就是现成的 `rate_limits` 表（重启还在、多进程共享），不用新造轮子。

**任务卡住不会无限期占位**：PowerShell 内部 `$task.Wait(25000)`，`subprocess.run(timeout=30)`，两次调用最坏 60 秒，超时直接 kill 进程。另外配一个 reaper 线程兜底（见 6.1）。

### 1.6 去重和重启恢复

**去重**：同一张图（`user_id` + `sha256` 相同）命中时，分两种：

- 已有 `done` 的草稿 → 直接返回那条结果，不再识别一次。你反复粘贴同一张海报时，第二次是瞬间出结果。
- 已有 `queued` / `running` 的草稿 → 返回**那条在途的**（`reused: true`），不新入队。这样连点两下、或者两个人同时传同一张，只会识别一次。

`sha256` 在 `inspect_bitmap()` 里已经算好了，不用额外读文件。第二种命中补上了原计划 12 节承认的缺口（「两个相同图片同时上传，会各自识别一次」）。

**重启恢复**：进程内的队列有个天生的毛病——进程一死，队列就没了。能做的只有「不静默丢」：

- 启动时扫一遍 `ocr_state = 'running'` 且 `ocr_started_at` 超过 5 分钟的行 → 标 `failed`，文案「服务重启，识别中断，请重试」。
- 启动时扫一遍 `ocr_state = 'queued'` 的行 → 重新入队，最多 20 个，超出的标 `failed`。

这做不到「绝不丢」，但能做到「用户看得见」。真要「绝不丢」得上 Redis + RQ/Arq，这一批不引入。

---

## 2. 做完后是什么样（可验收的场景）

1. 上传一张 2MB 海报：**300 毫秒内**返回 202，响应体里 `ocr_state = "queued"`，带 `preview_url`。
2. 上传的同时打开时间线：**正常秒开**，p95 不超过 200 毫秒。这是本批的核心验收点。
3. 前端立刻显示本地预览，下面一行「正在识别…（已等 2 秒）」，确认按钮是灰的。
4. 约 3-8 秒后 `ocr_state` 变 `done`，候选事项出现，确认按钮可点。
5. 再传同一张图：立刻返回 `reused: true`，`ocr_state = "done"`，不再起 PowerShell。
6. 传一张没有文字的纯色图：`ocr_state = "failed"`，`ocr_error` 有文案，`mode = "人工补录"`，可以手动填。
7. 点「重试」：重新入队，`ocr_attempts` 加一，最多 3 次。
8. 在 `queued` 状态调 `POST /confirm`：**409 `ocr_not_ready`**，一条记录都不写。
9. 队列塞满 20 个后再传：**503 `ocr_queue_full`**，带 `Retry-After: 30`。
10. 识别中途杀掉 uvicorn 再起来：那条草稿变 `failed`，文案说明中断，可以重试。
11. 迁移：真实库从 `0003` 升到 `0004`，用户、记录、草稿行数不变，所有老草稿 `ocr_state = "done"`。
12. 同一用户连传 3 张不同图：第 3 张 **429 `draft_busy`**，文案「上一张还在识别」。
13. 两个线程同时传同一张图：只产生一条草稿，另一个 `reused: true`，只识别一次。
14. 排队时页面显示「前面还有 4 个，约 20 秒」，不是干等「排队中…」。

---

## 3. 本批做什么 / 不做什么

### 3.1 做

| 项 | 具体改动 | 为什么在这一批 |
| --- | --- | --- |
| 迁移 `0004` | `image_drafts` 加五列 | 队列状态必须落库，否则重启就是黑盒 |
| 新模块 `src/ocr_queue.py` | 有界队列 + 2 个工人 + 提交/恢复/统计 | 本批的主体 |
| `src/ocr.py` 缓存 | `engine_status()` 结果缓存 300 秒 | 现在每次上传都白起一次 PowerShell |
| `src/image_drafts.py` 拆分 | `create_draft` 拆成「入队」和「识别」两半 | 这是把同步变异步的实际动作 |
| `confirm_draft` 守卫 | `ocr_state != done` 直接拒绝 | 否则会误判成人工补录并真的写记录 |
| `src/api.py` | 上传改 202、新增 `retry`、路由改同步 `def` | 202 是契约变更，前端必须同批改 |
| 前端轮询 | 800ms 起、退到 2s、60s 放弃、卸载清理 | 异步必须有人来问 |
| 去重 | `(user_id, sha256)` 命中 `done` 或 `queued/running` 都复用 | 便宜且体感明显 |
| 准入三道闸 | 单用户在途 ≤2、每小时 ≤20 次、全局队列 20 | 只靠队列上限挡不住一个人狂点 |
| reaper 看门狗 | 每 60 秒回收超 180 秒还在 `running` 的行 | 进程不重启时，卡住的任务会一直占工人 |
| 排队位置 | `GET` 响应带 `queue_position` | 让用户知道在第几位，成本极低 |
| 重启恢复 | 启动时处理 `running` / `queued` | 「不静默丢」 |
| 同步开关 | `LEARNING_ASSISTANT_OCR_SYNC=1` 走老路径 | 测试和回滚都要用 |
| 文档与测试 | `docs/ocr.md`、README、验收清单 | 半年后要知道为什么这么选 |

### 3.2 不做

| 不做 | 原因 |
| --- | --- |
| Redis、Celery、RQ、Arq | 单进程本地应用，标准库够了。多进程部署时再说，见 12 的缺口 |
| WebSocket / SSE 推送 | 轮询够用，少一套连接管理。接口形状不因此变化 |
| 杀掉正在跑的识别任务 | Python 线程杀不掉。做法是标 `cancelled`，工人跑完发现已取消就丢弃结果 |
| 进度百分比 | OCR 是一次性调用，没有中间进度可报。只能报「排队中 / 识别中」 |
| 多图批量上传 | 批次 6 的拖拽可能顺带支持，但本批接口仍是单文件 |
| 识别结果自动写入记录 | 这是产品红线：识别结果必须人工确认。本批只做异步，不改这条 |
| 换 OCR 引擎、接云端 OCR | 与「只在本机识别」的约束冲突 |
| 完整错误信封、metrics、`/api/ready` | 批次 5 |
| `useAsync`、`AuthContext`、`routes.jsx` | 批次 4A。本批只在 `RecognizePage` 内部加轮询 |
| 草稿列表分页 | 一个用户的草稿是个位数 |

### 3.3 一条门槛，写进文档

当出现以下任一情况时，进程内队列必须换掉，不能再打补丁：

1. 部署成多进程（`uvicorn --workers N` 或 gunicorn）。
2. 迁移到 Postgres 并准备横向扩容。
3. 出现「识别任务丢了但用户不知道」的真实事故。

那时直接上 RQ 或 Arq，`ocr_state` 这套字段可以原样保留——它们描述的是业务状态，与用什么队列无关。这条写进 `docs/stack-and-scope.md`。

---

## 4. 数据模型（迁移 0004）

迁移就是给数据库结构记一笔账：第 `0004` 号改动加了哪几列。空库和老库按同一本账升级，不靠人记得「这台机器上有没有加过那一列」。工具是 Alembic，它只改结构，不碰记录内容。

### 4.1 `image_drafts` 新增列

| 列 | 类型 | 可空 | 默认 | 用途 |
| --- | --- | --- | --- | --- |
| `ocr_state` | TEXT | 否 | `'done'` | `queued` / `running` / `done` / `failed` |
| `ocr_queued_at` | TEXT | 否 | `''` | 入队时间；算排队时长 |
| `ocr_started_at` | TEXT | 否 | `''` | 工人开始时间；重启恢复靠它判断超时 |
| `ocr_finished_at` | TEXT | 否 | `''` | 结束时间；算识别耗时 |
| `ocr_attempts` | INTEGER | 否 | `0` | 重试次数；上限 3 |
| `ocr_provider` | TEXT | 否 | `''` | `windows` / `tesseract`；从 `candidate_json` 里提出来 |

### 4.2 为什么默认是 `'done'` 而不是 `'queued'`

这是本批最需要想清楚的一个默认值。

- **老行**：`0003` 之前的草稿都是同步识别完才入库的，语义上就是 `done`。默认 `'done'` 让回填变成「什么都不用做」，比写一段循环 UPDATE 更安全。
- **回滚兼容**：如果代码退回 `batch-2`，旧代码的 `INSERT` 语句里没有 `ocr_state`，落库自动取 `'done'`。旧代码读不到这一列，行为不变。**这是「结构向前、代码可退」的关键。**
- **代价**：新代码必须显式写 `ocr_state = 'queued'`，不能依赖默认值。这一点要在 `models.py` 的注释里写死，否则哪天漏写，任务会以「已完成」的姿态静静躺着，永远没人识别。

`ocr_provider` 提成独立列的理由：现在它被塞在 `candidate_json` 的 `识别提供者` 里（`image_drafts.py:43`），要查「这台机器上识别都用哪个引擎」得解析 JSON。提出来之后，批次 5 的 metrics 能直接 `GROUP BY`。为了兼容，`_public()` 先读列、为空再回退读 `candidate_json`。

### 4.3 索引

不新增索引。现有 `ix_image_drafts_user_id_sha256`（`0001` 建的）已经覆盖去重查询 `WHERE user_id = ? AND sha256 = ? AND ocr_state = 'done'`——前两列选择性已经很高，`ocr_state` 只是过滤，用不上索引前缀也没关系。

`ocr_state` 单独建索引**不建议**：取值只有四种，选择性极低，SQLite 会直接全表扫，白占写入成本。重启恢复扫的是全表，但草稿表是个位数到几百行，一次启动扫一遍完全无所谓。

### 4.4 升降级

- `upgrade`：加六列（`ocr_state` 带默认值，其余带默认值）→ 完成。不写回填循环。
- `downgrade`：删六列。SQLite 3.35+ 支持 `DROP COLUMN`；Python 3.11/3.12/3.13 自带的 SQLite 都够（与 `0003` 同样的判断）。
- 降级会丢掉队列状态（`queued`/`failed` 的任务信息），但不动用户、记录、草稿的原有内容，也不删原图文件。这句话要写进迁移注释。

### 4.5 迁移测试

- 造一个 `0003` 的库，插一个用户 + 一条草稿（`ocr_text` 有值）。
- 升到 `0004`：断言六列存在；`ocr_state == 'done'`；`ocr_attempts == 0`；`ocr_provider == ''`。
- `downgrade` 回 `0003`：断言六列消失，草稿行数不变，`ocr_text` 不变。
- 再 `upgrade`：结构恢复。

---

## 5. 接口契约

### 5.1 总表

| 方法 | 路径 | 登录 | CSRF | 变化 |
| --- | --- | --- | --- | --- |
| POST | `/api/image-drafts` | 是 | 是 | **201 → 202**；响应体加 `ocr_state` / `reused` |
| GET | `/api/image-drafts/{id}` | 是 | 否 | 加 `ocr_state` / `ocr_attempts` / `queue_position` / `poll_after_ms` |
| POST | `/api/image-drafts/{id}/retry` | 是 | 是 | **新增**；仅 `failed` 可用 |
| POST | `/api/image-drafts/{id}/confirm` | 是 | 是 | **加守卫**：`ocr_state != done` → 409 |
| POST | `/api/image-drafts/{id}/cancel` | 是 | 是 | 允许取消 `queued` / `running` |
| GET | `/api/image-drafts/{id}/preview` | 是 | 否 | 不变 |
| GET | `/api/image-drafts/{id}/original` | 是 | 否 | 不变 |
| GET | `/api/ocr/status` | 是 | 否 | 加 `queue` 段 |

### 5.2 请求与响应示例

上传：

```json
POST /api/image-drafts   (multipart/form-data, file=...)

202
{
  "id": 42,
  "status": "pending",
  "ocr_state": "queued",
  "mode": "识别草稿",
  "engine": "",
  "provider": "",
  "languages": [],
  "ocr_error": "",
  "ocr_text": "",
  "candidate": {"方式": "识别草稿", "事项": []},
  "width": 1080,
  "height": 1440,
  "preview_url": "/api/image-drafts/42/preview",
  "reused": false,
  "poll_after_ms": 800
}
```

复用命中：

```json
202
{"id": 41, "ocr_state": "done", "reused": true, "poll_after_ms": 0, "...": "..."}
```

轮询：

```json
GET /api/image-drafts/42

200
{
  "id": 42,
  "status": "pending",
  "ocr_state": "running",
  "ocr_attempts": 1,
  "queue_position": 0,
  "ocr_error": "",
  "candidate": {"方式": "识别草稿", "事项": []},
  "poll_after_ms": 1000,
  "...": "..."
}
```

排队中（`queue_position` 是前面还有几个任务）：

```json
200
{"id": 43, "ocr_state": "queued", "queue_position": 4, "poll_after_ms": 800}
```

识别完成：

```json
200
{
  "id": 42,
  "ocr_state": "done",
  "ocr_text": "全国大学生数学建模竞赛\n报名截止：2026年11月25日\n...",
  "candidate": {"方式": "识别草稿", "事项": [{"标题": "全国大学生数学建模竞赛", "截止日期": "2026-11-25", "...": "..."}]},
  "poll_after_ms": 0
}
```

识别失败：

```json
200
{
  "id": 42,
  "ocr_state": "failed",
  "ocr_error": "识别超时，没有得到竞赛信息。",
  "candidate": {"方式": "人工补录", "事项": []},
  "poll_after_ms": 0
}
```

未就绪就确认：

```json
POST /api/image-drafts/42/confirm

409 {"detail": "识别还没结束，请等结果出来再确认。", "code": "ocr_not_ready"}
```

队列满：

```json
POST /api/image-drafts

503 {"detail": "识别任务排队已满，请稍后再试。", "code": "ocr_queue_full"}
Retry-After: 30
```

### 5.3 错误码

| code | 状态 | 什么时候 |
| --- | --- | --- |
| `ocr_not_ready` | 409 | `ocr_state` 是 `queued` 或 `running` 时调 confirm |
| `ocr_queue_full` | 503 | 全局等待队列满 20 个（带 `Retry-After: 30`） |
| `ocr_already_running` | 409 | retry 时任务已经在排队或运行 |
| `ocr_retry_exhausted` | 409 | `ocr_attempts >= 3` |
| `draft_busy` | 429 | 同一用户 `queued` + `running` 的草稿 ≥ 2 个 |
| `upload_rate_limited` | 429 | 同一用户一小时内上传超过 20 次（复用 `ratelimit`） |
| `draft_not_found` | 404 | 不存在或不属于当前用户 |

沿用现有的 `{detail, code}` 形态，批次 5 再长成完整信封。

### 5.4 `poll_after_ms` 为什么由后端给

前端自己定「800ms 还是 2s」的问题是：后端想降频时改不了。让后端在每次响应里带一个建议间隔：

- `queued` → 800ms（排队中，变化快）
- `running` → 1000ms
- `done` / `failed` → 0（停止轮询）
- 队列深度 > 15 → 2000ms（后端忙，别来添乱）

前端只负责遵守，不自己算。这样将来调整节奏不用发前端版本。

### 5.5 `queue_position`：让人知道自己在第几位

口径固定：**前面还有几个任务**。

- `0` —— 不在排队：正在识别，或者已经结束。
- `1..n` —— 前面还有 n 个。界面上显示「前面还有 4 个，约 20 秒」。
- 队列满被拒时响应里不带这个字段，只有 503 + `Retry-After`。

实现是 `SELECT COUNT(*) FROM image_drafts WHERE ocr_state='queued' AND id < ?`，走主键，草稿表只有个位数到几百行，一次几毫秒。不需要在内存里维护位置表——内存里的位置和库里的顺序会不一致，反而难对账。

这个字段的价值全在体感上：「排队中…」和「前面还有 4 个，约 20 秒」是两种完全不同的等待体验。成本一个 `COUNT`。

---

## 6. 后端实现

### 6.1 新模块 `src/ocr_queue.py`

只装队列本身，不装识别逻辑。识别仍然在 `src/image_drafts.py` 里，通过一个回调传进来——这样队列可以独立测试，不需要真的起 PowerShell。

```python
def start(on_job: Callable[[int], None]) -> None: ...
def shutdown(wait: bool = False) -> None: ...
def submit(draft_id: int) -> bool: ...          # 非阻塞；满返回 False
def position(draft_id: int) -> int: ...          # 前面还有几个；0 表示不在排队
def recover() -> dict: ...                       # 启动时跑，返回 {"requeued": n, "failed": m}
def stats() -> dict: ...                         # {"depth": n, "running": n, "max_workers": 2}
def reset() -> None: ...                         # 测试用
```

要点：

- **`submit` 必须非阻塞**。用 `put_nowait()`，`queue.Full` 返回 `False`。如果这里阻塞，事件循环又会被卡住，等于白改。
- **`on_job` 由调用方注入**。`api.py` 的 lifespan 里传 `image_drafts.run_ocr`，避免 `ocr_queue` 反向依赖 `image_drafts` 造成循环导入。
- **工人循环必须捕获 `BaseException` 之外的异常并落 `failed`**。任何一个 job 抛异常，线程不能死；死了队列就永远不动，而且没有任何提示。用 `try/except Exception` 包住整个 job，写 `ocr_state='failed'` + `ocr_error`，然后 `continue`。
- **`shutdown(wait=False)`**：lifespan 退出时不等正在跑的任务。等待会让重启卡住几十秒；那些任务下次启动会被 `recover()` 标 `failed`，这是可接受的。
- **全局单例 + `threading.Lock`**：`start()` 和 `shutdown()` 都可能被重复调用（测试里尤其），必须幂等。
- **工人线程设 `daemon=True`**：主进程退出时不阻塞。
- **reaper 看门狗**：`start()` 时起一个 daemon 线程，每 60 秒扫一次 `ocr_state='running'` 且 `ocr_started_at` 早于 180 秒前的行 → 标 `failed`（文案「识别超时，请重试」），打一条 `event=ocr_reaped`。

reaper 正常情况下永远不该有活干——`subprocess.run(timeout=30)` 已经保证单个任务有上界。它防的是异常情况：PowerShell 卡在 kill 不掉的状态，或者工人线程被某个没预料到的阻塞调用占住。没有它，一条 `running` 行会一直占着两个工人之一，而且**只在进程重启时才被 `recover()` 收拾**。十几行代码换「不会静默降容」，值。

一个容易漏的细节：`ThreadPoolExecutor` 的 `max_workers=2` 只限制**同时执行**的 job 数，`queue.Queue(maxsize=20)` 限制**等待**的 job 数。所以实际上限是 22 个在途任务。`stats()` 要把两者分开报，否则 `/api/ocr/status` 的数字会让人困惑。

### 6.2 `src/ocr.py` 加缓存

现在 `engine_status()` 每次调用都起一次 PowerShell（`ocr_windows.py:141` → `_run(mode="status")`）。加一层 300 秒 TTL 缓存：

```python
def engine_status_cached() -> dict: ...
def clear_status_cache() -> None: ...   # 测试用
```

要点：

- **失败结果也要缓存**。否则系统没装中文包时，每次上传都要白等一次 PowerShell 超时——那才是最坏情况。
- **缓存键要包含 `LEARNING_ASSISTANT_OCR_PROVIDER`**。测试会在同一个进程里切换引擎，不隔离就会读到上一个测试的结果。
- **`transcribe()` 内部仍然调原始的 `engine_status()`**（当 `status=None` 时），避免缓存把测试的假状态串味。或者更干净：`transcribe(image, status)` 已经支持传 status，缓存只用在 `create_draft` 那一层。
- 用 `threading.Lock` 保护，因为两个工人线程会同时读。
- 加 `LEARNING_ASSISTANT_OCR_STATUS_TTL` 环境变量，默认 300，设 0 关闭缓存（测试和调试用）。

### 6.3 `src/image_drafts.py` 拆分

现有 `create_draft()` 是「校验 → 识别 → 写文件 → 插库」一条龙。拆成两个函数：

```python
def enqueue_draft(user_id: int, data: bytes) -> tuple[dict, bool]:
    """同步做：校验、写文件、插 queued 行、提交队列。返回 (草稿, 是否复用)。"""

def run_ocr(draft_id: int) -> None:
    """工人调用：读原图、识别、解析、更新行。不抛异常。"""
```

`enqueue_draft` 的顺序必须是：

1. `inspect_bitmap(data)` — 解码校验。**这一步不能异步**，因为它负责拒绝坏文件；放进队列意味着先接受一个垃圾文件再失败，用户白等。
2. 闸 1：`SELECT COUNT(*) FROM image_drafts WHERE user_id=? AND status='pending' AND ocr_state IN ('queued','running')`，≥ 2 抛 `DraftBusy`。
3. 闸 2：`ratelimit.check(("upload", user_id), 20, 3600)`，超了抛 `RateLimited`。放在写文件之前，省一次落盘。
4. 查重：先看有没有可复用的行。

   ```sql
   SELECT id, ocr_state FROM image_drafts
   WHERE user_id = ? AND sha256 = ? AND status = 'pending'
     AND ocr_state IN ('done', 'queued', 'running')
   ORDER BY id DESC LIMIT 1
   ```

   命中 `done` → 直接返回结果；命中 `queued` / `running` → 返回那条在途草稿（`reused: true`）。两种都**不写文件、不插行、不入队**。
5. `write_private(...)` — 落盘原图和预览。
6. `INSERT ... ocr_state='queued', candidate_json='{"方式":"识别草稿","事项":[]}'`。
7. `submit(draft_id)`。返回 `False` 说明队列满——这时要**回滚**：删掉刚插的行和刚写的文件，抛 `QueueFull`。不能留一条永远 `queued` 的孤儿行。
8. 返回 `(草稿, False)`。

顺序上的两个要点：**闸和查重都放在写文件之前**，被拒的请求不该留下任何字节；**第 7 步的回滚是必测分支**，它只在并发打满时触发，平时跑不到，最容易被漏。

`run_ocr` 的顺序：

1. 读行；如果 `status = 'cancelled'`，直接返回（用户已经放弃）。
2. `UPDATE ocr_state='running', ocr_started_at=now, ocr_attempts=ocr_attempts+1`。
3. 读原图（`private_file(row["original_rel"])`），`Image.open`。
4. `status = engine_status_cached()`；`transcribe(image, status)`；`parse_poster(text)`。
5. 再读一次行，如果 `status = 'cancelled'`，丢弃结果不写。
6. `UPDATE ocr_text, candidate_json, ocr_provider, ocr_state='done', ocr_finished_at`。
7. 任何异常 → `UPDATE ocr_state='failed', ocr_error=..., ocr_finished_at`，`candidate_json` 改成 `{"方式":"人工补录","事项":[]}`（与现有失败语义一致）。

两个必须注意的点：

- **`candidate_json` 不能是空字符串。** `_public()` 第 311 行无条件 `json.loads(row["candidate_json"])`，空串会抛 `JSONDecodeError`，前端一读就 500。入队时就要写一个合法的空 candidate。
- **第 5、6 步之间不要持有长事务。** 识别可能跑 30 秒，如果这期间开着 `BEGIN IMMEDIATE`，整个库都写不进去。做法是「读一次、关连接、识别、再开连接写一次」。

`confirm_draft()` 加守卫，插在状态检查之后：

```python
if row["ocr_state"] in {"queued", "running"}:
    raise DraftError("识别还没结束，请等结果出来再确认。", "ocr_not_ready", 409)
```

只拦这两个状态。`failed` 必须放行——那正是人工补录的入口。写成 `!= "done"` 会把
「识别失败后手动填」这条路堵死，和第 1.4 节的表格、第 7.2 节的界面设计自相矛盾。

**这一行不是防御性编程，是修 bug。** 现有第 149 行：

```python
manual = bool(row["ocr_error"]) or not str(row["ocr_text"]).strip()
```

`queued` 状态下 `ocr_error` 是空、`ocr_text` 也是空，这个表达式为 `True`，草稿会被当成「人工补录」，`_insert_material()` 会写一条 `kind="人工补录原图"` 的资料，`_activity_text()` 会写一条「识别失败后人工补录」的复盘——**全部基于一份还没识别完的草稿**。前端禁用按钮挡不住直接调接口的人。

`cancel_draft()` 的改动：允许取消 `queued` 和 `running`。取消 `running` 时只改 `status='cancelled'`，不试图杀线程（Python 杀不掉）；工人跑完在第 5 步发现已取消，丢弃结果。原图文件保留——用户可能想恢复，清理留给以后。

### 6.4 `src/api.py`

1. **上传路由从 `async def` 改成 `def`。**

```python
@app.post("/api/image-drafts", status_code=202)
def upload_image_draft(
    file: UploadFile = File(...),
    user: dict = Depends(current_user),
) -> dict:
    payload = file.file.read(MAX_BYTES + 1)
    ...
```

改成同步 `def` 之后，FastAPI 会自动把整个函数丢进线程池，连文件读取都不占事件循环。这是本批最便宜、收益最大的一行改动。

注意两个连带变化：不能 `await`，所以 `await file.read(...)` 要改成 `file.file.read(...)`；`await file.close()` 改成 `file.file.close()`（或者干脆不显式关，`UploadFile` 的生命周期由框架管）。

2. 三个准入异常各映射一个状态码：`QueueFull` → 503 `ocr_queue_full` + `Retry-After: 30`；`DraftBusy` → 429 `draft_busy`；`RateLimited` → 429 `upload_rate_limited`。都用同一套 `{detail, code}` 形态。
3. 新增 `POST /api/image-drafts/{id}/retry`。
4. `confirm` 的 `ValueError` 里，`ocr_not_ready` 要映射成 409 而不是 400。现有实现把所有 `ValueError` 都映射成 400（`api.py:669`），需要区分：给 `confirm_draft` 抛一个带 code 的 `DraftStateError`，或者在 `api.py` 里比对文案。**推荐前者**，比对文案太脆。
5. `/api/ocr/status` 加 `queue` 段：`{"depth": 0, "running": 0, "max_workers": 2}`。
6. lifespan 里 `ocr_queue.start(image_drafts.run_ocr)` + `ocr_queue.recover()`，`yield` 之后 `ocr_queue.shutdown()`。
7. `GET /api/image-drafts/{id}` 的响应里补 `queue_position`（调 `ocr_queue.position(id)`）；`running` / `done` / `failed` 一律给 0。这个字段放在 `_public()` 里不合适——它属于队列，不属于草稿，放在路由层拼。

**`recover()` 必须在 `bootstrap()` 之后**——它要读表，表得先建好。

### 6.5 `src/db/models.py`

`ImageDraft` 加六个字段，`ocr_state` 用 `server_default=text("'done'")`。结构测试会自动比对，漏一列就红。

### 6.6 `src/logging.py`

`_EXTRA_FIELDS` 加五个：`draft_id`、`ocr_state`、`queue_depth`、`queue_wait_ms`、`ocr_ms`。

打点位置：

- 入队成功：`event=ocr_enqueued`，`queue_depth`、`reused`
- 工人开始：`event=ocr_started`，`queue_wait_ms`（= `ocr_started_at` − `ocr_queued_at`）
- 工人结束：`event=ocr_done` / `ocr_failed`，`ocr_ms`、`error_code`
- 队列满：`event=ocr_queue_full`
- 其他准入拒绝：`event=ocr_rejected`，`reason=draft_busy` / `upload_rate_limited`
- reaper 回收：`event=ocr_reaped`，`draft_id`、`age_ms`
- 恢复：`event=ocr_recovered`，`requeued`、`failed`

这五条日志是批次 5 metrics 的原料，现在埋好，那时不用返工。

### 6.7 同步开关

`LEARNING_ASSISTANT_OCR_SYNC=1` 时：

- `enqueue_draft` 插完行**不**提交队列，而是直接在当前线程调 `run_ocr(draft_id)`。
- 响应仍然是 202，但 `ocr_state` 已经是 `done` 或 `failed`。

用途有三个：

1. **测试**。`tests/test_images.py` 现有约 20 处「上传后立刻断言 candidate」，有开关就不用全改成轮询等待。
2. **回滚**。线上出问题，设一个环境变量就回到同步行为，不用回退代码。
3. **调试**。想看清识别失败的真实报错时，同步路径的堆栈最直接。

代价要写清楚：**同步模式下测不到队列**。队列本身由 `tests/test_ocr_queue.py` 单独覆盖，两边不重叠。这是个诚实的取舍，不是遗漏。

---

## 7. 前端改动

### 7.1 上传改成「立刻显示 + 轮询」

现在 `onFile` 是 `await apiFetch(...)` 然后 `setDraft(body)`，用户对着转圈等 30 秒。改成：

1. `URL.createObjectURL(file)` 生成 `localPreview`，立刻 `setDraft({preview_url: localPreview, ocr_state: "queued"})`，`setItems([])`。
2. `await apiFetch` 拿到 202，`setDraft(body)`。
3. 如果 `body.reused`，直接用返回的 `candidate` 填 `items`，不轮询。
4. 否则启动轮询。
5. 整个过程 `setUploading(true)`，`<input>` 和按钮都禁用，`onFile` 开头再兜一句 `if (uploading) return;`。这是防双击最便宜的一道——比后端任何限流都早，也最不容易让人困惑。

被准入拒绝时文案要分开，不要一个「失败了」打天下：429 `draft_busy` →「上一张还在识别，等它出来再传下一张」；429 `upload_rate_limited` →「今天传得有点多，歇一会儿再试」；503 `ocr_queue_full` →「前面还有人在识别，约 30 秒后再试」。

轮询用 `setTimeout` 递归，**不要用 `setInterval`**：

```js
useEffect(() => {
  if (!draft || !["queued", "running"].includes(draft.ocr_state)) return;
  let cancelled = false;
  let timer = null;
  const startedAt = Date.now();

  async function tick() {
    if (cancelled) return;
    if (Date.now() - startedAt > 60000) {
      setLocalError("识别超过 60 秒还没回来，可以重试。");
      return;
    }
    try {
      const body = await readJson(await apiFetch(`/api/image-drafts/${draft.id}`, {}));
      if (cancelled) return;
      setDraft(body);
      if (body.ocr_state === "done") setItems(draftItems(body));
      if (body.ocr_state === "failed") setItems([]);
      if (body.poll_after_ms > 0) timer = setTimeout(tick, body.poll_after_ms);
    } catch (err) {
      if (!cancelled) setLocalError(err.message || "读不到识别进度");
    }
  }

  timer = setTimeout(tick, draft.poll_after_ms || 800);
  return () => {
    cancelled = true;
    if (timer) clearTimeout(timer);
  };
}, [draft?.id, draft?.ocr_state]);
```

`setInterval` 的问题是：请求比间隔慢时，会堆积一串未完成的请求，服务越慢堆积越多，形成雪崩。`setTimeout` 递归保证「上一次回来才发下一次」。

### 7.2 界面状态

| `ocr_state` | 显示 | 确认按钮 |
| --- | --- | --- |
| `queued` | 「已收到，前面还有 4 个，约 20 秒」+ 预览 | 禁用 |
| `running` | 「正在识别…（已等 3 秒）」+ 预览 | 禁用 |
| `done` | 候选事项表格 | 可用 |
| `failed` | `ocr_error` 文案 + 「重试」+「人工补录」 | 可用（走人工补录） |

`queue_position` 为 0 时就不提排队，直接显示「正在识别…」；大于 0 时按「约 3 秒一个」粗估一个秒数，估不准也没关系，比干等强。

秒数用 `useEffect` + `setInterval(1000)` 单独驱动，不影响轮询。

### 7.3 重试和取消

- 「重试」按钮：`POST /api/image-drafts/{id}/retry`，成功后 `setDraft(body)`，轮询会自动重启（因为 `ocr_state` 变回 `queued`）。
- 「取消」：现有逻辑不变，但现在也允许在 `running` 时点。取消后清掉预览和定时器。
- 换图时先 `URL.revokeObjectURL(localPreview)`，否则 blob 泄漏。`useEffect` 的 cleanup 里也要 revoke。

### 7.4 本地预览和 `preview_url` 的关系

上传成功后服务端已经有预览（`write_private` 写了 `preview.jpg`），`body.preview_url` 是 `/api/image-drafts/42/preview`。但**不要**立刻切到服务端预览——它要一次额外请求，而本地 blob 已经在屏幕上。等用户点「确认」或页面刷新时才用服务端的。

这样做的副作用：刷新页面后 `draft` 从服务端读，`preview_url` 是相对路径，需要能正常显示。现有代码已经这么用了，不用改。

### 7.5 粘贴截图：不用先去文件夹找图（建议，待拍板）

截图之后按 Ctrl+V 直接贴进识图页，比「保存到桌面 → 打开文件选择框 → 找到那张图」少三步。技术上没有障碍：

- 浏览器的 `paste` 事件里，`event.clipboardData.items` 就带着 `image/png` 的 `Blob`。Win+Shift+S 截图工具、QQ/微信截图都往剪贴板放位图，Chrome/Edge 都取得到。
- **后端完全不用改**：`inspect_bitmap()` 靠解码后的真实格式判断类型，源码注释写着「文件名和路径都不可信」。所以 `form.append("file", blob, "clipboard.png")` 里那个名字只是占位，写什么都行。
- 8MB / 6000px 的限制照旧生效；截图一般几百 KB，远够。

实现要点：

1. 监听挂在 `document` 上，只在这个页面挂（`useEffect` 里加，卸载时 `removeEventListener`）。
2. **先判断焦点**：`if (event.target.closest("input, textarea")) return;`——否则会抢掉用户在输入框里粘文字的常规操作。
3. 从 `clipboardData.items` 里找 `type.startsWith("image/")` 的项，`getAsFile()` 拿到 Blob，交给同一个上传函数。
4. 顺手把 `dragover` / `drop` 也接上（同一个处理函数，`dataTransfer.files[0]`），从资源管理器拖进来也能用，只多十行。

一个要提前知道的副作用：从剪贴板拿到的 PNG 是浏览器重新编码的，字节和你「另存为」的那个文件不一样，`sha256` 对不上，所以「先粘贴一次、再从文件夹传同一张」会各识别一次。不影响正确性，只是去重在这条路径上失效。

不做也不影响功能，只是每次截图多三次点击。估 0.2 人日，含手动验收。

### 7.6 不改的部分

`frontend/src/api/client.js` 不动。`apiFetch` 已经处理 credentials 和 CSRF，`retry` 是 POST，天然带头。`useAsync`、`AuthContext` 留给批次 4A——本批只在 `RecognizePage` 内部加两个 `useEffect`，不引入新抽象。

---

## 8. 实施顺序

每一步一个提交，最后合成一个 PR、打 `batch-3` tag。每步结束都跑对应检查点，不攒到最后。

| 步 | 做什么 | 检查点 |
| --- | --- | --- |
| 0 | 建分支 `batch-3-ocr-async`；`python scripts/backup_db.py`；记录用户/记录/草稿行数；跑一遍现有测试 | 备份 `integrity_check=ok`；现有测试全绿 |
| 1 | 迁移 `0004` + 模型 + 迁移/结构测试 | 真库副本升/降级；行数不变；老草稿 `ocr_state='done'` |
| 2 | `src/ocr.py` 缓存 + 测试 | 连续调两次只起一次 PowerShell；失败结果也缓存 |
| 3 | `src/ocr_queue.py` + 单元测试（不起 HTTP、不真识别），含 `position()` 和 reaper | 提交/满/异常/关闭/统计/回收六类用例过 |
| 4 | `image_drafts.py` 拆成 `enqueue_draft` + `run_ocr`；加三道闸、在途复用、`confirm` 守卫 | 同步开关下现有测试全过；`queued` 调 confirm 被拒；同用户连传 3 张第 3 张 429 |
| 5 | `api.py`：202、retry、路由改 `def`、`queue_position`、lifespan 接线 | curl 上传 300ms 内回 202；同时请求时间线不卡；并发同图只入队一次 |
| 6 | 前端轮询 + 状态显示 + 重试 + 上传中禁用 + 排队位置 | `npm run build`；手动走上传→等→确认 |
| 6b | （可选）粘贴截图 + 拖拽，见 7.5 | Win+Shift+S 后 Ctrl+V 出预览；输入框粘文字不受影响 |
| 7 | 重启恢复 `recover()` | 杀掉进程再起，`running` 变 `failed`，`queued` 重排 |
| 8 | 去重 | 同图第二次 `reused: true`，不起 PowerShell |
| 9 | 日志字段、文档、全量测试、备份、tag | 第 10 节验收全过 |

第 4 步单独提交很重要：它是「只换结构、行为不变」（同步开关下）。如果第 5-8 步出问题，可以只回滚后面的提交，不用把 `image_drafts` 的拆分一起退掉。

第 7、8 步可以砍（见 13 的可裁剪），砍掉之后第 9 步照做。

---

## 9. 测试计划

### 9.1 新增

| 文件 | 覆盖 |
| --- | --- |
| `tests/test_ocr_queue.py` | 提交执行、队列满返回 False、job 抛异常后线程不死、`shutdown` 幂等、`stats` 数字、`recover` 重排 |
| `tests/test_ocr_cache.py` | 缓存命中不重复起进程、失败结果也缓存、TTL 过期、provider 切换不串味、`TTL=0` 关闭 |

### 9.2 改写

| 文件 | 改动 |
| --- | --- |
| `tests/test_images.py` | 大部分用例设 `LEARNING_ASSISTANT_OCR_SYNC=1`，只把 `201` 改成 `202`；新增异步路径用例（见 9.3） |
| `tests/test_api.py` | 上传相关断言从 201 改 202；新增 409 `ocr_not_ready`、503 `ocr_queue_full` |
| `tests/test_migrations.py` | `HEAD` 改 `0004`；加 0004 升降级用例 |
| `tests/test_schema_models.py` | `HEAD` 改 `0004`；六个新字段自动纳入比对 |
| `tests/test_frontend.py` | 断言 `RecognizePage` 含 `ocr_state`、`poll_after_ms`、`setTimeout`、`revokeObjectURL` |

### 9.3 关键用例（按行为写，不按函数写）

1. 上传 → 202，`ocr_state='queued'`，`candidate` 是合法空对象（`json.loads` 不抛）。
2. 上传的**同时**发一个 `GET /api/timeline`：后者在 200ms 内返回，不被上传阻塞。这是本批的核心用例，用两个线程并发跑。
3. 轮询到 `done`：`ocr_text` 有内容，`candidate.items` 非空，`poll_after_ms=0`。
4. 纯色图：`ocr_state='failed'`，`mode='人工补录'`，`candidate.items` 为空。
5. `queued` 状态调 `confirm` → 409 `ocr_not_ready`；断言 `user_records` 行数**没变**、`materials` 行数没变、`review_activities` 没新增。这条比状态码更重要。
6. 队列塞满：把 `maxsize` 打成 1、**同时把单用户上限调高**（否则第 2 张先被闸 1 拦成 429，测不到队列），连传 3 张 → 第 3 张 503；断言库里**没有**多出孤儿草稿行，私有目录也没有多余文件。
7. 同图第二次上传 → `reused=true`，`ocr_attempts` 不变，`ocr_finished_at` 不变（证明没重跑）。
8. `recover()`：造一条 `running` + `ocr_started_at` 是 10 分钟前的行 → 变 `failed`；造一条 `queued` 的行 → 被重排。
9. `cancel` 一个 `running` 的草稿：工人跑完后 `ocr_state` 停在 `done` 或 `failed`，但 `status` 仍是 `cancelled`，`candidate` 不被写。断言没有记录被创建。
10. `retry` 一个 `failed`：`ocr_attempts` 从 1 变 2；连点 4 次后第 4 次 409 `ocr_retry_exhausted`。
11. `LEARNING_ASSISTANT_OCR_SYNC=1`：上传后同一个响应里 `ocr_state` 已经是 `done`。
12. 迁移：0003 库带一条草稿，升到 0004 后 `ocr_state='done'`、`ocr_attempts=0`。
13. **并发同图**：两个线程同时 POST 同一份 bytes → 只有一条草稿行，另一个拿到 `reused=true`，`ocr_attempts` 只加一次。
14. **单用户在途上限**：同一用户连传 3 张不同图 → 第 3 张 429 `draft_busy`；库里只有 2 条在途草稿，私有目录只有 2 组文件。
15. **队列满回滚**：`maxsize=1` + 放宽单用户上限，连传 3 张不同图 → 后两张 503；断言 `image_drafts` 行数只加了 1，私有目录没有残留文件。这条专门覆盖 `submit` 返回 `False` 的分支。
16. **reaper**：造一条 `running` + `ocr_started_at` 是 20 分钟前的行，跑一次 reaper → 变 `failed`，`stats()["running"]` 归零。
17. **`queue_position`**：塞 3 个排队，第 3 个读到 2；开始识别后读到 0。

### 9.4 不测什么

- 不测真实的 Windows OCR 识别准确率——那是 `test_images.py` 现有用例的事，本批不碰。
- 不测线程调度的精确时序——测试里用 `Event` 和轮询等待，不 `sleep` 固定秒数。
- 不测 60 秒前端超时——那需要 fake timer，成本高于收益；靠手动验收。

---

## 10. 验收清单

### 10.1 自动

```bash
python -m pytest -q --ignore=tests/test_frontend.py
cd frontend && npm run build
python -m src.validate_records data/records.csv --min-rows 3
python scripts/backup_db.py
```

四条都要过。测试数会从现在的 90 多个涨到 120 个左右。

### 10.2 手动（用临时库，不碰真实数据）

```bash
export LEARNING_ASSISTANT_DB=/tmp/batch3-check.sqlite   # Windows 用 set 或 $env:
export LEARNING_ASSISTANT_PRIVATE=/tmp/batch3-private
python -m uvicorn src.api:app --host 127.0.0.1 --port 8011
```

1. 上传一张真实海报：`time curl` 显示 300ms 内返回 202。
2. **上传的同时**另开一个终端刷 `/api/timeline`：不卡。
3. 浏览器里看：预览立刻出现，「正在识别…」；几秒后候选出现，确认按钮亮。
4. 确认写入：记录出现在「我的记录」，复盘有一条活动。
5. 再传同一张：立刻 `reused: true`。
6. 传一张纯色图：失败文案 + 重试按钮 + 人工补录可填。
7. 点重试：重新排队，`ocr_attempts` 加一。
8. 识别过程中关掉 uvicorn：重启后那条草稿是 `failed`，文案说明中断。
9. 同一张图连点两次上传（或两个标签页同时传）：只产生一条草稿，第二次 `reused: true`。
10. 塞满队列后传图：看到「前面还有人在识别，约 30 秒后再试」，不是红字报错。
11. 若做了 7.5：Win+Shift+S 截图后直接 Ctrl+V，预览立刻出现；在输入框里粘文字不受影响。

### 10.3 迁移（真实库副本）

```bash
python scripts/backup_db.py          # 生成 data/backups/app-YYYYMMDD-HHMMSS.sqlite
LEARNING_ASSISTANT_DB=data/backups/app-YYYYMMDD-HHMMSS.sqlite \
  python -c "from src.db import bootstrap; bootstrap()"
```

对比升级前后的 `users`、`user_records`、`image_drafts` 行数；`PRAGMA foreign_key_check` 为空；老草稿的 `ocr_state` 全是 `done`。不要直接 `cp` 正在写入的库。

---

## 11. 回滚与开关

| 场景 | 动作 |
| --- | --- |
| 队列行为异常 | `LEARNING_ASSISTANT_OCR_SYNC=1` 回到同步识别，不用回退代码 |
| 缓存读到脏状态 | `LEARNING_ASSISTANT_OCR_STATUS_TTL=0` 关闭缓存 |
| 队列满得太频繁 | 调 `LEARNING_ASSISTANT_OCR_QUEUE_SIZE`（默认 20）和 `MAX_WORKERS`（默认 2） |
| 代码整体回滚 | 回到 `batch-2` tag。`ocr_state` 有默认值 `'done'`，旧代码的 `INSERT` 不写它也能成功；旧代码读不到这一列，行为不变 |
| 迁移失败 | 中止启动，用 `data/backups/pre-migration-*.sqlite` 恢复；不手工改表 |
| 队列里未完成的任务 | 回滚时丢失。它们只是草稿，没有写入任何记录，重传即可 |

回滚后新列留着不用。这是「结构向前、代码可退」的设计，与批次 2 一致。

---

## 12. 风险与已知缺口

| 风险 | 触发 | 缓解 |
| --- | --- | --- |
| 线程写 SQLite 撞锁 | 2 个工人 + API 请求同时写 | 每个 job 只在开始和结束各写一次；短事务；走 `write_retry`；最坏把 `max_workers` 降到 1 |
| 进程内队列重启丢任务 | 部署、崩溃 | `recover()` 标 `failed` + 可重试；用户看得见，不静默 |
| 多进程时无全局上限 | `--workers N` | 3.3 的门槛；单进程部署时不成立 |
| 轮询打爆读接口 | 多人同时上传 | `poll_after_ms` 由后端控制；`GET` 是轻查询，走索引 |
| 一个人狂点占满队列 | 单人反复上传 | 闸 1 在途 ≤2、闸 2 每小时 ≤20；队列上限只是最后一道 |
| 两个请求同时抢最后一个位 | 并发上传 | `put_nowait` 原子入队，不加 `qsize` 预检；回滚分支必测 |
| 卡住的 `running` 一直占工人 | PowerShell 卡死、进程不重启 | reaper 每 60 秒回收超 180 秒的行 |
| PowerShell 并发占内存 | 2 个工人同时识别大图 | `prepare_ocr_image` 已限制到 10000px；`max_workers=2` 是硬上限 |
| `queued` 状态误写记录 | 直接调 confirm 接口 | 后端守卫 + 409；测试断言三张表行数不变 |
| `candidate_json` 空串导致 500 | 入队时忘写 | 入队即写合法空对象；`_public()` 加 `try/except` 兜底 |
| 前端 60 秒超时后任务还在跑 | 慢机器 | 超时只是停止轮询，不取消任务；用户点重试会命中 `ocr_already_running`，提示「还在跑」 |
| 缓存把测试状态串味 | 同进程切换 provider | 缓存键含 provider；测试用 `clear_status_cache()` |

已知缺口，写清楚不装：

- **队列在内存里。** 重启丢任务，只能做到「不静默丢」，做不到「绝不丢」。
- **没有进度百分比。** OCR 是一次性调用，只能报「排队 / 识别中」。
- **取消是「软取消」。** 正在跑的 PowerShell 不会被打断，只是结果被丢弃。这期间仍然占一个工人。
- **reaper 只是标 `failed`，不真的停掉那个工人线程。** 卡住的线程要等 `subprocess` 自己超时返回。它恢复的是「用户看得见 + 队列继续收」，不是「立刻释放资源」。
- **去重按 `sha256` 精确匹配。** 同一张图被微信/QQ 压过、尺寸变了，指纹就不同，会重新识别一次。
- **`ocr_attempts` 上限 3 是硬编码。** 想改要动代码，没做环境变量。
- **单进程假设。** 见 3.3。

---

## 13. 工作量与可裁剪

| 块 | 估时 |
| --- | --- |
| 迁移 0004 + 模型 + 测试 | 0.3 人日 |
| `ocr.py` 缓存 + 测试 | 0.3 人日 |
| `ocr_queue.py` + 单元测试 | 0.8 人日 |
| 准入三道闸 + reaper | 0.3 人日 |
| `queue_position` | 0.1 人日 |
| `image_drafts.py` 拆分 + 守卫 | 0.7 人日 |
| `api.py` 改造（202 / retry / 同步路由 / lifespan） | 0.4 人日 |
| 前端轮询 + 状态 + 重试 | 0.5 人日 |
| 重启恢复 + 去重（含在途复用） | 0.5 人日 |
| 测试改写（`test_images.py` 约 20 处） | 0.6 人日 |
| 日志、文档、收尾 | 0.4 人日 |
| **合计** | **约 4.9 人日**（4.5-5.2） |
| 可选：粘贴截图（7.5） | +0.2 人日 |

比批次 2 的 3.0 人多，主要多在 `test_images.py` 的改写、队列本身的测试，以及并发/准入那几条需要真起两个线程的用例。

可裁剪，按优先级从高到低：

1. **去重砍掉**（省 0.2 人日）。收益是体感，不是正确性。
2. **重启恢复砍掉**（省 0.2 人日）。但要在启动日志里明确打一条「有 N 条卡住的任务未处理」，否则就是静默丢失。
3. **`retry` 接口砍掉**（省 0.2 人日）。失败后用户只能重新上传，体验差但能用。

不建议裁：迁移、队列、`confirm` 守卫、202 + 轮询、同步开关、**闸 1（单人在途上限）**。前五件是这一批的骨架——尤其 `confirm` 守卫，砍了就是留一个会写脏数据的洞；闸 1 砍了，一个人狂点就能把队列和两个工人全吃掉，等于没有上限。

reaper 和 `queue_position` 可以砍，但要知道代价：前者砍了就是「卡住的任务只在重启时才被收拾」，后者砍了就是用户干等「排队中…」。

### 一条更省的做法

如果只想用 1 人日看到效果，做「最小闭环」：

> 迁移 0004（只加 `ocr_state`）+ 同步路由改 `def` + 线程池（不设上限）+ 闸 1（单人在途 ≤2）+ 202 + 前端轮询。

这样核心痛点（卡住事件循环）当天就解决，`ocr_state` 的语义也立住了。去重、重试、重启恢复、缓存都是后面的增强，加起来再补 2-3 人日。缺点是中间状态下队列无上限，人多时会被拖慢——但单机自用不会遇到。

注意：即使走最小闭环，**闸 1 也不能省**。线程池不设上限时，唯一的刹车就是「每个人最多 2 个在途」；这条一去掉，一个人连点十下就是十个 PowerShell 进程。

---

## 14. 与原 v2 计划的差异（需要拍板）

| # | 原计划 | 这份计划 | 理由 |
| --- | --- | --- | --- |
| 1 | `status` 里加 `queued/running/done/failed` | 单独加 `ocr_state` 列 | 「识别失败」和「草稿作废」是两件事，合在一起以后还得拆 |
| 2 | 未提 | 上传路由从 `async def` 改 `def` | 不改的话，文件读取本身仍占事件循环，收益打折 |
| 3 | 未提 | 加 `LEARNING_ASSISTANT_OCR_SYNC` 开关 | 20 处测试用例和回滚都要用；否则测试改写量翻倍 |
| 4 | 未提 | `confirm_draft` 加 `ocr_not_ready` 守卫 | 现有代码会把 `queued` 误判成人工补录并**真的写记录**，这是 bug 不是优化 |
| 5 | 未提 | `ocr_provider` 提成独立列 | 现在塞在 `candidate_json` 里，批次 5 的 metrics 没法 `GROUP BY` |
| 6 | `ocr_state` 默认值未定 | 默认 `'done'` | 老行语义正确 + 回滚兼容，两个目标一次满足 |
| 7 | 估 3-4 人日 | 估 4.9 人日 | `test_images.py` 的改写原估算没算；另加准入、reaper、并发用例 |
| 8 | 未提 | `poll_after_ms` 由后端下发 | 前端定死间隔，后端想降频就改不了 |
| 9 | 队列上限写成「最多 20 个人」 | 改成「20 个任务槽位」，另加准入三道闸 | 20 是任务数不是在线人数；只靠队列挡不住一个人狂点 |
| 10 | 未提 | reaper 每 60 秒回收卡住的 `running` | 进程不重启时，卡住的任务会一直占着工人 |
| 11 | 去重只在 `done` 上生效 | 加在途复用（`queued` / `running`） | 连点和同图并发只识别一次，顺带补上 12 节原缺口 |
| 12 | 未提 | `queue_position` 下发 | 排队体感差别很大，成本一个 `COUNT` |
| 13 | 未提 | 粘贴截图（7.5，可选） | 截图后少三次点击 |

需要你拍板的四件：

- **A. 队列满时**：503 劝退（推荐，可控的拒绝优于不可控的崩溃），还是无界排队（简单，但人多会被拖死）？
- **B. 同步开关**：保留（推荐，测试和回滚都靠它），还是不留（少一个分支，但 `test_images.py` 要全改成轮询等待）？
- **C. 去重 + 重启恢复**：本批一起做（推荐，共 0.5 人日，含在途复用），还是拆成 3B 留到后面？
- **D. 粘贴截图**：做（推荐，+0.2 人日，截图后按 Ctrl+V 直接贴），还是不做（保持只走文件选择框）？

其余按本文执行。你确认后，我按第 8 节的顺序做，每完成一步给一条「改了什么、怎么验」的短报告，最后合成一个 PR 再打 `batch-3` tag。

---

## 15. 名词对照

| 词 | 一句话 |
| --- | --- |
| 事件循环 | 一个服务员，动作快但一次只能做一件事 |
| 阻塞 | 让这个服务员站着等，别人全排后面 |
| `async def` | 「这个函数归服务员管」，里面的同步调用会让他停下 |
| 202 Accepted | 「收下了，正在办」，不是成功 |
| 轮询 | 每隔一会儿问一次「好了吗」 |
| 有界队列 | 最多排 20 个任务，满了就劝退（是任务，不是人） |
| 准入闸 | 门口三道检查：单人在途、单人频率、全局队列；队列只是最后一道 |
| 原子入队 | 判满和放进去是一步完成，不会两个人同时挤进最后一个位 |
| 看门狗 | 定时巡一遍，把卡住太久、还挂着「识别中」的任务标成失败 |
| 排队位置 | 前面还有几个任务，0 表示轮到了 |
| 工人线程 | 后厨真正干活的人，2 个 |
| `ocr_state` | 识别跑到哪一步：排队 / 识别中 / 完成 / 失败 |
| `status` | 草稿还活着吗：待确认 / 已确认 / 已取消 |
| 去重 | 同一张图只识别一次，认的是 `sha256` 指纹 |
| 重启恢复 | 进程死了以后，把卡住的任务标成失败，让用户看得见 |
| 同步开关 | 一个环境变量，让系统退回「当场识别完再回话」的老行为 |
| 软取消 | 不真的打断任务，只是它的结果不要了 |
| 迁移 | 给数据库结构记一笔账；空库和老库按同一本账升级 |
| Alembic | 记这笔账、并按账升级的工具 |

---

## 16. 执行记录（2026-11-09）

按第 8 节九步做完，tag `batch-3`。全量 147 项测试通过（基线 116，新增 31），前端构建通过。

### 16.1 落地清单

| 项 | 状态 | 位置 |
| --- | --- | --- |
| 迁移 `0004` 六列 | 完成 | `migrations/versions/0004_ocr_queue.py` |
| 有界队列 + 2 工人 + daemon 线程 | 完成 | `src/ocr_queue.py` |
| 看门狗 reaper（60 秒 / 180 秒） | 完成 | `ocr_queue.start(on_job, on_reap)` + `image_drafts.reap_stale()` |
| 三道闸 | 完成 | `image_drafts.enqueue_draft()` |
| 在途复用（`queued`/`running`） | 完成 | `image_drafts._find_reusable()` |
| `queue_position` | 完成 | `image_drafts.queue_position()`，路由层拼 |
| `enqueue_draft` + `run_ocr` 拆分 | 完成 | `src/image_drafts.py` |
| `confirm` 守卫 | 完成 | `confirm_draft()`，只拦 `queued`/`running` |
| 上传 202 + 同步 `def` | 完成 | `src/api.py` |
| 前端轮询 + 状态 + 重试 + 上传中禁用 | 完成 | `frontend/src/App.jsx` |
| 引擎状态缓存 300 秒 | 完成 | `src/ocr.py` |
| 同步开关 | 完成 | `LEARNING_ASSISTANT_OCR_SYNC` |
| 日志五个字段 + `ocr_reaped` | 完成 | `src/logging.py` |
| 粘贴截图（7.5） | **未做** | 第 14 节标的是「可选，待拍板 D」，没有拍板就不做 |

### 16.2 与计划不一致的三处

1. **`ocr_queue.position()` 放到了 `image_drafts.queue_position()`。** `ocr_queue` 这一层的约定是
   「不碰数据库」，位置要查库，放进去就破了这个约定。接口形状和语义没变。
2. **`recover_interrupted()` 也在 `image_drafts`，不在 `ocr_queue.recover()`。** 同上：它要读表。
   `ocr_queue` 只提供 `requeue(ids)`。
3. **新增 `LEARNING_ASSISTANT_OCR_UNFINISHED_LIMIT` 环境变量。** 第 9.3 节第 15 条要求
   「`maxsize=1` + 放宽单用户上限」，上限写死就没法测那条分支。默认仍是 2。

### 16.3 实施中补的两个洞

- **取消后的状态收尾。** `_finish_done` / `_finish_failed` 都带 `WHERE status = 'pending'`，
  用户在识别途中取消后，那行会永远停在 `running`，一直占着「在途」名额，还会被下一次
  启动的 `recover_interrupted()` 误标失败。补了 `_finish_cancelled()`：不写识别结果，
  只把 `ocr_state` 收到 `done`。
- **失败候选的 `方式`。** `empty_candidate()` 原来固定写「识别草稿」，导致失败草稿在前端
  显示成一份正常结果。改成带参数，失败时写「人工补录」。

### 16.4 实测（真实 lifespan、真实本机 Windows OCR、合成海报）

| 指标 | 结果 |
| --- | --- |
| 上传响应 | 202，83ms |
| 识别完成 | done，721ms，2 条候选事项 |
| 上传期间 `/api/timeline` | 62 次请求，中位 11ms，最大 32ms，0 错误 |
| 同一张图再传 | `reused: true`，`poll_after_ms: 0` |
| 写入记录 | 0 条（确认前不写） |

改动前同一场景里，那 62 次时间线请求会排在识别后面。
