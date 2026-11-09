# 识图异步化：状态、队列与运维

批次 3 把「上传图片」从「当场识别完再回话」改成「先收下、后台识别、前端来问」。
这份文档说明为什么这么做、状态怎么流转、出问题看哪里。计划过程见
[批次 3 计划](plan-batch3-ocr-async.md)。

## 1. 为什么改

改动前 `POST /api/image-drafts` 是 `async def`，里面直接调同步的 `create_draft()`。
FastAPI 不会把 `async def` 里的同步调用丢进线程池，所以识别期间**整个事件循环停摆**。
而识别要起两次 PowerShell（查引擎状态、真识别），每次超时 30 秒，最坏 60 秒。

2026-11-09 实测（真实 lifespan、真实本机 OCR、合成海报）：

| 指标 | 结果 |
| --- | --- |
| 上传响应 | 202，124ms |
| 识别完成 | done，902ms，1 次尝试 |
| 上传期间 `/api/timeline` | 54 次请求，中位 15ms，最大 55ms，0 错误 |
| 同一张图再传 | `reused: true`，不再起识别进程 |

改动前的同一场景里，那 54 次时间线请求会排在识别后面，最坏等几十秒。

## 2. 两个状态，不要混

| 字段 | 回答什么 | 取值 |
| --- | --- | --- |
| `status` | 这份草稿还是活的吗 | `pending` / `confirmed` / `cancelled` |
| `ocr_state` | 识别跑到哪一步 | `queued` / `running` / `done` / `failed` |

必须分开：「识别失败了」和「草稿废了」是两件事。失败之后草稿仍可人工补录、仍可确认。
合成一个字段，就会出现「`failed` 到底是草稿作废还是识别作废」的歧义。

有意义的组合：

| `status` | `ocr_state` | 含义 | 能不能确认 |
| --- | --- | --- | --- |
| `pending` | `queued` | 排队中 | 不能，409 `ocr_not_ready` |
| `pending` | `running` | 识别中 | 不能，409 `ocr_not_ready` |
| `pending` | `done` | 识别完成，等确认 | 能 |
| `pending` | `failed` | 识别失败，可人工补录或重试 | 能（走人工补录） |
| `confirmed` | `done` | 已写入记录 | 不用再确认 |
| `cancelled` | 任意 | 用户放弃，结果丢弃 | 不能 |

`confirm_draft()` 里那行守卫是**修 bug**，不是防御性编程。守卫加之前，
`queued` 状态下 `ocr_text` 和 `ocr_error` 都是空，`manual` 判断会为真，
一份还没识别完的草稿会被当成「人工补录」，真的写进 `user_records`、
`materials` 和 `review_activities`。前端禁用按钮挡不住直接调接口的人。

## 3. 队列与三道闸

`src/ocr_queue.py`，只做排队和派活，不认识数据库也不认识识别。

- `queue.Queue(maxsize=20)` + 2 个 daemon 工人线程。
- **`submit()` 非阻塞**：`put_nowait()`，满了返回 `False`，接口回 503 并带 `Retry-After: 30`。这里如果阻塞，事件循环又被卡住，等于白改。
- **队列里只放 `draft_id`**：一张图最大 8MB，20 张就是 160MB。工人拿 id 再去私有目录读原图。
- **daemon 线程，不用 `ThreadPoolExecutor`**：后者在解释器退出时会 join，一个卡在 30 秒识别里的任务会把重启拖住。
- **job 抛异常不让线程死**：死一个工人，队列就永远不动，而且没人知道。

**20 是任务槽位，不是在线人数。** 真实容量是「2 个正在识别 + 20 个等待 = 22 个在途任务」。
打开页面挂着不动的人一个位都不占——轮询只是一次几毫秒的 `GET`，60 秒后前端自己停。

**三道闸，顺序不能反**（在 `enqueue_draft` 里）：

| 闸 | 限制 | 拒绝 | 作用 |
| --- | --- | --- | --- |
| 1 | 单用户在途草稿 ≤ 2（`queued` + `running`） | 429 `draft_busy` | 防一个人占满全局队列 |
| 2 | 单用户每小时上传 ≤ 20 次（`rate_limits` 表） | 429 `upload_rate_limited` | 防脚本刷 |
| 3 | 全局等待队列 20 | 503 `ocr_queue_full` + `Retry-After: 30` | 防整机被拖死 |

队列上限放最后，否则一个人狂点就能吃掉全部 20 个位，别人全被 503。
闸和查重都在写文件之前——被拒的请求不该在磁盘上留下任何字节。

**入队必须原子。** 用 `put_nowait()` 而不是 `if qsize() < 20: put()`：后者是 check-then-act，
两个请求都读到 19，最后挤进 21 个。

**看门狗（reaper）**：`ocr_queue.start()` 时起一个 daemon 线程，每 60 秒调一次
`image_drafts.reap_stale()`，把 `ocr_state='running'` 且 `ocr_started_at` 早于 180 秒前的行标成
`failed`（文案「识别超时，请重试。」）。正常情况永远不该有活干——`subprocess.run(timeout=30)`
已经给了单个任务上界。它防的是异常：PowerShell 卡在 kill 不掉的状态，或者工人线程被某个
没预料到的阻塞调用占住。没有它，一条 `running` 行会一直占着两个工人之一，
而且**只在进程重启时才被收拾**。

在途上限是 `max_workers + max_queue` = 22 个任务。`/api/ocr/status` 的 `queue` 段把两者分开报。

## 4. 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/image-drafts` | **202**，返回 `ocr_state`、`reused`、`queue_position`、`poll_after_ms` |
| GET | `/api/image-drafts/{id}` | 轮询用，带 `ocr_state`、`ocr_attempts`、`queue_position`、`poll_after_ms` |
| POST | `/api/image-drafts/{id}/retry` | 仅 `failed` 可用，`ocr_attempts` 上限 3 |
| POST | `/api/image-drafts/{id}/confirm` | `queued`/`running` 时 409 `ocr_not_ready` |
| POST | `/api/image-drafts/{id}/cancel` | 允许取消排队中或识别中的草稿 |
| GET | `/api/ocr/status` | 引擎状态（缓存 300 秒）+ 队列深度 |

`queue_position` 的口径是**前面还有几个任务**：`0` 表示不在排队（正在识别或已结束），
`1..n` 表示前面还有 n 个。实现是一条走主键的 `COUNT`，不维护内存位置表——
内存里的顺序和库里的顺序会不一致，反而难对账。这个字段属于队列不属于草稿，
所以在 `api.py` 的路由层拼，不塞进 `_public()`。

`poll_after_ms` 由后端给，前端只遵守：

- `queued` → 800ms；队列深度 ≥ 15 → 2000ms
- `running` → 1000ms
- `done` / `failed` / 已取消 / 已确认 → 0（停止轮询）

错误码：

| code | 状态 | 什么时候 |
| --- | --- | --- |
| `ocr_not_ready` | 409 | 排队中或识别中调 confirm |
| `ocr_queue_full` | 503 | 全局等待队列满 20（带 `Retry-After: 30`） |
| `ocr_already_running` | 409 | retry 时任务已在排队或运行 |
| `ocr_retry_exhausted` | 409 | 已经试过 3 次 |
| `draft_busy` | 429 | 同一用户已有 2 个在排队或识别中的草稿 |
| `upload_rate_limited` | 429 | 同一用户一小时内上传超过 20 次 |
| `draft_cancelled` | 409 | retry 一份已取消的草稿 |
| `draft_confirmed` | 409 | retry 一份已写入记录的草稿 |

## 5. 去重与恢复

**去重**：`(user_id, sha256)` 命中时看那条的状态：

- `done` → 直接返回现成结果，不再识别一次。
- `queued` / `running` → 返回**那条在途的**（`reused: true`），不新入队。

第二种命中让「连点两下」和「两个标签页同时传同一张」只识别一次。
两种都不写文件、不插行、不入队。`sha256` 在 `inspect_bitmap()` 里已经算好。

去重按字节精确匹配：同一张图被微信/QQ 压过、尺寸变了，指纹就不同，会重新识别一次。
从剪贴板粘贴的 PNG 是浏览器重新编码的，和「另存为」的文件字节不同，也认不出是同一张。

**恢复**：`recover_interrupted()` 在 lifespan 里 `bootstrap()` 之后跑。

- `running` 且 `ocr_started_at` 早于 5 分钟前（或为空）→ 标 `failed`，文案「服务重启，识别中断，请重试」。
- `queued` → 重新入队；塞不下的标 `failed`，不静默丢。

启动时最多重排 20 个（队列上限），其余标失败。

## 6. 环境变量

| 变量 | 默认 | 用途 |
| --- | --- | --- |
| `LEARNING_ASSISTANT_OCR_QUEUE_SIZE` | 20 | 等待队列长度 |
| `LEARNING_ASSISTANT_OCR_WORKERS` | 2 | 工人线程数 |
| `LEARNING_ASSISTANT_OCR_UNFINISHED_LIMIT` | 2 | 单用户同时在途的草稿数 |
| `LEARNING_ASSISTANT_OCR_REAP_INTERVAL` | 60 | 看门狗扫描间隔（秒） |
| `LEARNING_ASSISTANT_UPLOAD_LIMIT` | 20 | 单用户每小时上传次数 |
| `LEARNING_ASSISTANT_OCR_STATUS_TTL` | 300 | 引擎状态缓存秒数，0 关闭 |
| `LEARNING_ASSISTANT_OCR_SYNC` | 关 | `1` 退回同步识别（测试、回滚） |

同步模式下测不到队列，队列本身由 `tests/test_ocr_queue.py` 覆盖。这是有意的取舍。

`OCR_UNFINISHED_LIMIT` 做成可调，主要是为了测试「队列满回滚」那条分支：
不放开它，第 3 张会先被闸 1 拦成 429，根本走不到 `submit()` 返回 `False` 的地方。

## 7. 日志

`app.ocr` 这几个事件，字段都在 `src/logging.py` 的 `_EXTRA_FIELDS` 里：

| event | 关键字段 |
| --- | --- |
| `ocr_enqueued` | `draft_id`、`queue_depth` |
| `ocr_started` | `draft_id`、`queue_wait_ms` |
| `ocr_done` | `draft_id`、`ocr_ms` |
| `ocr_reused` | `draft_id` |
| `ocr_reaped` | `draft_id`、`ocr_ms`（看门狗回收了卡住的 running） |
| `ocr_recovered` | `queue_depth`、`error_code`（含 failed/dropped 计数） |
| `ocr_queue_not_started` | 队列没启动，退回同步执行 |
| `ocr_job_crashed` | job 抛了未捕获异常，带堆栈 |

排查顺序：先看 `ocr_enqueued` 有没有、`queue_depth` 涨没涨，再看 `ocr_started` 的
`queue_wait_ms`（排队久说明工人不够或卡住），最后看 `ocr_done` / `ocr_failed`。

## 8. 已知缺口

- **队列在进程内存里。** 重启丢任务，只能做到「不静默丢」，做不到「绝不丢」。
- **没有进度百分比。** OCR 是一次性调用，只能报「排队中 / 识别中」。
- **取消是软取消。** 正在跑的 PowerShell 不会被打断，只是结果被丢弃；这期间仍占一个工人。
- **看门狗只标 `failed`，不真的停掉那个工人线程。** 卡住的线程要等 `subprocess` 自己超时返回。它恢复的是「用户看得见 + 队列继续收」，不是「立刻释放资源」。
- **去重按字节精确匹配。** 见第 5 节。
- **重试上限 3 是硬编码。** 想改要动 `src/image_drafts.py` 的 `MAX_ATTEMPTS`。
- **单进程假设。** 多进程部署时每个进程一个队列，没有全局并发上限。

出现以下任一情况，必须换成 RQ/Arq 之类的真队列，不要再打补丁：

1. 部署成多进程（`uvicorn --workers N` 或 gunicorn）。
2. 迁到 Postgres 并准备横向扩容。
3. 出现「识别任务丢了但用户不知道」的真实事故。

`ocr_state` 这套字段描述的是业务状态，与用什么队列无关，换的时候可以原样保留。

## 9. 怎么自己验一遍

```bash
export LEARNING_ASSISTANT_DB=/tmp/ocr-check.sqlite
export LEARNING_ASSISTANT_PRIVATE=/tmp/ocr-private
python -m uvicorn src.api:app --host 127.0.0.1 --port 8791
```

1. 打开 `/recognize`，传一张真实海报。预览立刻出现，下面一行「正在识别…（已等 N 秒）」。
2. 同时刷新时间线：不卡。
3. 几秒后候选事项出现，确认按钮从灰变亮。
4. 再传同一张：瞬间出结果。
5. 识别过程中关掉 uvicorn 再起来：那条草稿是 `failed`，文案说明中断，可以点「重新识别」。

自动检查：

```bash
python -m pytest tests/test_ocr_queue.py tests/test_ocr_async.py tests/test_ocr_cache.py -q
```
