import { useEffect, useState } from "react";
import { NavLink, Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { apiFetch, setCsrfToken } from "./api/client.js";

const emptyContest = () => ({
  标题: "",
  截止日期: "",
  来源链接: "",
  日期原句: "",
  报名条件: "",
});

function normalizeDeadline(raw) {
  const text = raw.trim();
  if (!text || text === "未找到") {
    return "未找到";
  }
  if (!/^\d{4}-\d{2}-\d{2}$/.test(text)) {
    return "";
  }
  const [year, month, day] = text.split("-").map(Number);
  const parsed = new Date(Date.UTC(year, month - 1, day));
  if (
    parsed.getUTCFullYear() !== year ||
    parsed.getUTCMonth() !== month - 1 ||
    parsed.getUTCDate() !== day
  ) {
    return "";
  }
  return text;
}

async function readJson(response) {
  const resolved = typeof response?.json === "function" ? response : await response;
  const body = await resolved.json().catch(() => ({}));
  response = resolved;
  if (!response.ok) {
    const detail = body.detail;
    const error = new Error(typeof detail === "string" ? detail : `接口返回 ${response.status}`);
    error.code = body.code || "";
    error.status = response.status;
    throw error;
  }
  return body;
}

const UPLOAD_ERRORS = {
  draft_busy: "上一张还在识别，等它出来再传下一张。",
  upload_rate_limited: "今天传得有点多，歇一会儿再试。",
  ocr_queue_full: "前面还有人在识别，约 30 秒后再试。",
};

function uploadErrorMessage(err) {
  return UPLOAD_ERRORS[err?.code] || err?.message || "没有收下这张图";
}

function Card({ item, showMark }) {
  return (
    <li>
      <h3>{item.标题}</h3>
      <p className="owner">{item.归属}</p>
      <p>
        截止日期：{item.截止日期}
        {showMark ? `（${item.标记}）` : ""}
      </p>
      {item.日期原句 ? <p className="quote">{item.日期原句}</p> : null}
      {item.来源链接 && item.来源链接 !== "未找到" ? (
        <a href={item.来源链接} target="_blank" rel="noreferrer" title={item.来源链接}>
          来源
        </a>
      ) : null}
    </li>
  );
}

export default function App() {
  const navigate = useNavigate();
  const location = useLocation();
  const [me, setMe] = useState(undefined);
  const [data, setData] = useState(null);
  const [reminders, setReminders] = useState(null);
  const [mine, setMine] = useState([]);
  const [error, setError] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [todayInput, setTodayInput] = useState("");
  const [serverToday, setServerToday] = useState("");
  const [contest, setContest] = useState(emptyContest());

  async function loadViews(today) {
    const query = today ? `?today=${encodeURIComponent(today)}` : "";
    const [timeline, reminderBody, recordBody] = await Promise.all([
      readJson(await apiFetch(`/api/timeline${query}`, {})),
      readJson(await apiFetch(`/api/reminders${query}`, {})),
      readJson(await apiFetch("/api/records", {})),
    ]);
    setData(timeline);
    setReminders(reminderBody);
    setMine((recordBody.records || []).filter((row) => row.归属 === "我的"));
    if (!today) {
      setServerToday(reminderBody.today);
    }
  }

  useEffect(() => {
    let cancelled = false;
    apiFetch("/api/me", {})
      .then(async (response) => {
        if (response.status === 401) {
          return null;
        }
        return readJson(response);
      })
      .then(async (user) => {
        if (cancelled) {
          return;
        }
        setMe(user);
        if (user) {
          setCsrfToken(user.csrf_token);
          await loadViews("");
        } else {
          setCsrfToken("");
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setMe(null);
          setError(err.message || "读不到账号");
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function submit(path) {
    setError("");
    try {
      const user = await readJson(
        await apiFetch(path, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ email, password }),
        }),
      );
      setCsrfToken(user.csrf_token);
      setMe(user);
      setPassword("");
      navigate("/query");
      await loadViews("");
    } catch (err) {
      setError(err.message || "没有完成");
    }
  }

  async function addContest(event) {
    event.preventDefault();
    setError("");
    const deadline = normalizeDeadline(contest.截止日期);
    if (!deadline) {
      setError("相对日期请写成「未找到」，不要换算成某一天。截止日期只能是 YYYY-MM-DD 或未找到。");
      return;
    }
    const quote = contest.日期原句.trim();
    if (!quote) {
      setError("日期原句要照抄官网原文。没有公历日期时，也把那句相对说法留下。");
      return;
    }
    const link = contest.来源链接.trim() || "未找到";
    if (link !== "未找到" && !/^https?:\/\//.test(link)) {
      setError("来源链接要是官网地址，或写成「未找到」。");
      return;
    }
    try {
      await readJson(
        await apiFetch("/api/records", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            标题: contest.标题.trim(),
            类型: "竞赛",
            截止日期: deadline,
            来源链接: link,
            更新时间: serverToday,
            适合专业: "未找到",
            状态: "未处理",
            核对状态: "已核对",
            日期原句: quote,
            是否参与提醒: deadline === "未找到" ? "否" : "是",
            报名条件: contest.报名条件.trim() || "未找到",
          }),
        }),
      );
      setContest(emptyContest());
      await loadViews(todayInput.trim());
    } catch (err) {
      setError(err.message || "没有录入");
    }
  }

  async function signOut() {
    try {
      await apiFetch("/api/logout", { method: "POST" });
    } finally {
      setCsrfToken("");
      setMe(null);
      setData(null);
      setReminders(null);
      setMine([]);
      navigate("/");
    }
  }

  if (location.pathname === "/reset") {
    return <ResetPage />;
  }

  if (me === undefined) {
    return <p className="loading">正在读取…</p>;
  }

  if (!me) {
    return (
      <main className="login-screen">
        <p className="eyebrow">学习助手</p>
        <h1>竞赛截止</h1>
        <form
          onSubmit={(event) => {
            event.preventDefault();
          }}
        >
          <label>
            邮箱
            <input
              type="email"
              autoComplete="username"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              required
            />
          </label>
          <label>
            密码
            <input
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              minLength={8}
              required
            />
          </label>
          <p className="note">注册密码至少 10 位；老账号仍可用原密码登录。</p>
          <div className="actions">
            <button type="button" onClick={() => submit("/api/login")}>
              登录
            </button>
            <button type="button" onClick={() => submit("/api/register")}>
              注册
            </button>
          </div>
        </form>
        {error ? <p className="error">{error}</p> : null}
      </main>
    );
  }

  return (
    <div className="shell">
      <aside className="sidebar">
        <p className="eyebrow">学习助手</p>
        <NavLink to="/query">查询</NavLink>
        <NavLink to="/add">添加</NavLink>
        <NavLink to="/recognize">识图</NavLink>
        <NavLink to="/mine">我的记录</NavLink>
        <NavLink to="/review">本周复盘</NavLink>
        <NavLink to="/courses">课程</NavLink>
        <NavLink to="/materials">资料夹</NavLink>
        <NavLink to="/settings/reminders">提醒设置</NavLink>
        <NavLink to="/settings/account">账号与安全</NavLink>
        <button type="button" onClick={signOut}>
          退出
        </button>
        <p className="note">{me.email}</p>
      </aside>
      <div className="content">
        {error ? <p className="error">{error}</p> : null}
        <Routes>
          <Route
            path="/query"
            element={
              <QueryPage
                data={data}
                reminders={reminders}
                todayInput={todayInput}
                setTodayInput={setTodayInput}
                onView={() => loadViews(todayInput.trim()).catch((err) => setError(err.message || "读不到提醒"))}
              />
            }
          />
          <Route
            path="/add"
            element={
              <AddPage
                contest={contest}
                setContest={setContest}
                mine={mine}
                onSubmit={addContest}
              />
            }
          />
          <Route path="/recognize" element={<RecognizePage />} />
          <Route path="/mine" element={<MinePage />} />
          <Route path="/courses" element={<CoursesPage />} />
          <Route path="/materials" element={<MaterialsPage />} />
          <Route path="/review" element={<ReviewPage />} />
          <Route
            path="/settings/reminders"
            element={<SettingsPage onSaved={() => loadViews(todayInput.trim())} />}
          />
          <Route path="/settings/account" element={<AccountPage />} />
          <Route path="*" element={<Navigate to="/query" replace />} />
        </Routes>
      </div>
    </div>
  );
}

function QueryPage({ data, reminders, todayInput, setTodayInput, onView }) {
  const [copyNote, setCopyNote] = useState("");

  function reminderText() {
    return reminders?.message || "今日不发送";
  }

  async function copyReminders() {
    const text = reminderText();
    try {
      await navigator.clipboard.writeText(text);
      setCopyNote("已复制今日待提醒。没有发送邮件。");
    } catch {
      setCopyNote(text);
    }
  }

  function downloadReminders() {
    const text = reminderText();
    const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "今日待提醒.txt";
    link.click();
    URL.revokeObjectURL(url);
    setCopyNote("已导出纯文本。没有发送邮件。");
  }

  return (
    <>
      <h1>查询</h1>
      <form
        className="day-form"
        onSubmit={(event) => {
          event.preventDefault();
          onView();
        }}
      >
        <label>
          基准日
          <input
            value={todayInput}
            placeholder="留空则用提醒设置，或临时填写 YYYY-MM-DD"
            onChange={(event) => setTodayInput(event.target.value)}
          />
        </label>
        <button type="submit">查看</button>
      </form>
      <div className="query-layout columns">
        <section className="reminders panel">
          <h2>今日提醒</h2>
          <button type="button" onClick={copyReminders}>
            复制今日待提醒
          </button>
          <button type="button" onClick={downloadReminders}>
            导出纯文本
          </button>
          {copyNote ? <p className="note">{copyNote}</p> : null}
          {reminders ? (
            <p className="meta">
              基准日 {reminders.today} · 未来 {reminders.within_days} 天
            </p>
          ) : (
            <p>正在读取…</p>
          )}
          {reminders && reminders.items.length === 0 ? <p className="quiet">今日不发送</p> : null}
          <ul className="card-grid card-row">
            {(reminders?.items || []).map((item) => (
              <Card key={`remind-${item.归属}-${item.标题}-${item.截止日期}`} item={item} showMark />
            ))}
          </ul>
        </section>
        <section className="panel">
          <h2>全部已核对</h2>
          {data ? (
            <p className="meta">
              基准日 {data.today} · {data.info_type}
            </p>
          ) : (
            <p>正在读取时间线…</p>
          )}
          {data && data.items.length === 0 ? <p>没有已核对的公历截止日期。</p> : null}
          <ul className="card-grid card-row">
            {(data?.items || []).map((item) => (
              <Card key={`${item.归属}-${item.标题}-${item.截止日期}`} item={item} showMark />
            ))}
          </ul>
        </section>
      </div>
    </>
  );
}

function AddPage({ contest, setContest, mine, onSubmit }) {
  return (
    <>
      <h1>添加</h1>
      <form className="add-form" onSubmit={onSubmit}>
        <p className="note span-2">
          先在官网核对。只抄原文里的公历日期；「本周五」这类写成未找到。这里改不了共享目录。
        </p>
        <label>
          标题
          <input
            value={contest.标题}
            onChange={(event) => setContest({ ...contest, 标题: event.target.value })}
            required
          />
        </label>
        <label>
          截止日期
          <input
            value={contest.截止日期}
            placeholder="YYYY-MM-DD 或 未找到"
            onChange={(event) => setContest({ ...contest, 截止日期: event.target.value })}
          />
        </label>
        <label>
          来源链接
          <input
            value={contest.来源链接}
            placeholder="https:// 或留空表示未找到"
            onChange={(event) => setContest({ ...contest, 来源链接: event.target.value })}
          />
        </label>
        <label>
          日期原句
          <input
            value={contest.日期原句}
            onChange={(event) => setContest({ ...contest, 日期原句: event.target.value })}
            required
          />
        </label>
        <label className="span-2">
          报名条件
          <input
            value={contest.报名条件}
            placeholder="留空则记为未找到"
            onChange={(event) => setContest({ ...contest, 报名条件: event.target.value })}
          />
        </label>
        <button type="submit">录入</button>
      </form>
      <section>
        <h2>我的录入</h2>
        {mine.length === 0 ? <p>还没有自己录入的竞赛。</p> : null}
        <ul className="card-grid card-row">
          {mine.map((item) => (
            <Card key={`mine-${item.标题}-${item.日期原句}`} item={item} />
          ))}
        </ul>
      </section>
    </>
  );
}

const MINE_STATUSES = ["未处理", "已报名", "已交", "放弃"];

function MinePage() {
  const [rows, setRows] = useState(null);
  const [localError, setLocalError] = useState("");
  const [editingId, setEditingId] = useState(null);
  const [draft, setDraft] = useState(null);

  async function load() {
    const body = await apiFetch("/api/mine", {}).then(readJson);
    setRows(body.records || []);
  }

  useEffect(() => {
    load().catch((err) => setLocalError(err.message || "读不到我的记录"));
  }, []);

  function startEdit(item) {
    setEditingId(item.id);
    setDraft({
      标题: item.标题,
      截止日期: item.截止日期,
      来源链接: item.来源链接,
      日期原句: item.日期原句,
      报名条件: item.报名条件,
    });
  }

  async function saveEdit(id) {
    setLocalError("");
    const deadline = normalizeDeadline(draft.截止日期);
    if (!deadline) {
      setLocalError("相对日期请写成「未找到」，不要换算成某一天。");
      return;
    }
    try {
      await readJson(
        await apiFetch(`/api/mine/${id}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ...draft, 截止日期: deadline }),
        }),
      );
      setEditingId(null);
      setDraft(null);
      await load();
    } catch (err) {
      setLocalError(err.message || "没有保存");
    }
  }

  async function remove(id) {
    if (!window.confirm("确定删除这条私人记录？只删除你自己的这一行，共享目录不会改。")) {
      return;
    }
    setLocalError("");
    try {
      await readJson(
        await apiFetch(`/api/mine/${id}`, { method: "DELETE" }),
      );
      await load();
    } catch (err) {
      setLocalError(err.message || "没有删除");
    }
  }

  async function changeStatus(id, status) {
    setLocalError("");
    try {
      await readJson(
        await apiFetch(`/api/mine/${id}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ 状态: status }),
        }),
      );
      await load();
    } catch (err) {
      setLocalError(err.message || "没有改成");
    }
  }

  return (
    <>
      <h1>我的记录</h1>
      <p className="note">只显示你自己录入的竞赛，不含共享目录。</p>
      {localError ? <p className="error">{localError}</p> : null}
      {rows && rows.length === 0 ? <p>还没有自己录入的竞赛。到「添加」页抄官网原文。</p> : null}
      <ul className="card-grid card-row">
        {(rows || []).map((item) => (
          <li key={item.id} className={item.展示 === "已过期" ? "expired" : ""}>
            <h3>{item.标题}</h3>
            {item.展示 === "已过期" ? <p className="quiet">已过期，建议下线。截止日期仍是原来的那一天。</p> : null}
            <p>截止日期：{item.截止日期}</p>
            <p>日期原句：{item.日期原句}</p>
            {item.资料id ? <p>资料 #{item.资料id}，从资料夹可以打开原图。</p> : null}
            {item.来源链接 && item.来源链接 !== "未找到" ? (
              <a href={item.来源链接} target="_blank" rel="noreferrer" title={item.来源链接}>
                来源
              </a>
            ) : (
              <p>来源：未找到</p>
            )}
            <label>
              当前状态
              <select value={item.状态} onChange={(event) => changeStatus(item.id, event.target.value)}>
                {MINE_STATUSES.map((status) => (
                  <option key={status} value={status}>
                    {status}
                  </option>
                ))}
              </select>
            </label>
            {editingId === item.id && draft ? (
              <div className="edit-fields">
                <label>
                  标题
                  <input value={draft.标题} onChange={(event) => setDraft({ ...draft, 标题: event.target.value })} />
                </label>
                <label>
                  截止日期
                  <input
                    value={draft.截止日期}
                    onChange={(event) => setDraft({ ...draft, 截止日期: event.target.value })}
                  />
                </label>
                <label>
                  来源链接
                  <input
                    value={draft.来源链接}
                    onChange={(event) => setDraft({ ...draft, 来源链接: event.target.value })}
                  />
                </label>
                <label>
                  日期原句
                  <input
                    value={draft.日期原句}
                    onChange={(event) => setDraft({ ...draft, 日期原句: event.target.value })}
                  />
                </label>
                <label>
                  报名条件
                  <input
                    value={draft.报名条件}
                    onChange={(event) => setDraft({ ...draft, 报名条件: event.target.value })}
                  />
                </label>
                <button type="button" onClick={() => saveEdit(item.id)}>
                  保存修改
                </button>
              </div>
            ) : (
              <button type="button" onClick={() => startEdit(item)}>
                编辑
              </button>
            )}
            <button type="button" className="danger" onClick={() => remove(item.id)}>
              删除
            </button>
          </li>
        ))}
      </ul>
    </>
  );
}

function CoursesPage() {
  const [rows, setRows] = useState([]);
  const [form, setForm] = useState({ name: "", term: "", notes: "" });
  const [localError, setLocalError] = useState("");

  async function load() {
    const body = await apiFetch("/api/courses", {}).then(readJson);
    setRows(body.courses || []);
  }

  useEffect(() => {
    load().catch((err) => setLocalError(err.message || "读不到课程"));
  }, []);

  async function onSubmit(event) {
    event.preventDefault();
    setLocalError("");
    try {
      await readJson(
        await apiFetch("/api/courses", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(form),
        }),
      );
      setForm({ name: "", term: "", notes: "" });
      await load();
    } catch (err) {
      setLocalError(err.message || "没有保存");
    }
  }

  return (
    <>
      <h1>课程</h1>
      <p className="note">只登记你自己填写的课名。这里不编造课程内容，也不代写作业。</p>
      <form className="add-form" onSubmit={onSubmit}>
        <label>
          课程名称
          <input value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} required />
        </label>
        <label>
          学期
          <input value={form.term} onChange={(event) => setForm({ ...form, term: event.target.value })} />
        </label>
        <label className="span-2">
          备注
          <input value={form.notes} onChange={(event) => setForm({ ...form, notes: event.target.value })} />
        </label>
        <button type="submit">添加课程</button>
      </form>
      {localError ? <p className="error">{localError}</p> : null}
      <ul className="card-grid card-row">
        {rows.map((item) => (
          <li key={item.id}>
            <h3>{item.name}</h3>
            {item.term ? <p>{item.term}</p> : null}
            {item.notes ? <p className="quote">{item.notes}</p> : null}
          </li>
        ))}
      </ul>
    </>
  );
}

function MaterialsPage() {
  const [courses, setCourses] = useState([]);
  const [rows, setRows] = useState([]);
  const [filter, setFilter] = useState("");
  const [form, setForm] = useState({ title: "", course_id: "", kind: "", source_url: "", page_hint: "" });
  const [localError, setLocalError] = useState("");

  async function load(courseId) {
    const query = courseId ? `?course_id=${encodeURIComponent(courseId)}` : "";
    const [courseBody, materialBody] = await Promise.all([
      apiFetch("/api/courses", {}).then(readJson),
      apiFetch(`/api/materials${query}`, {}).then(readJson),
    ]);
    setCourses(courseBody.courses || []);
    setRows(materialBody.materials || []);
  }

  useEffect(() => {
    load("").catch((err) => setLocalError(err.message || "读不到资料"));
  }, []);

  async function onSubmit(event) {
    event.preventDefault();
    setLocalError("");
    try {
      await readJson(
        await apiFetch("/api/materials", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            title: form.title,
            course_id: form.course_id ? Number(form.course_id) : null,
            kind: form.kind,
            source_url: form.source_url,
            page_hint: form.page_hint,
          }),
        }),
      );
      setForm({ title: "", course_id: form.course_id, kind: "", source_url: "", page_hint: "" });
      await load(filter);
    } catch (err) {
      setLocalError(err.message || "没有保存");
    }
  }

  return (
    <>
      <h1>资料夹</h1>
      <p className="note">只做索引。把你已有的讲义或链接挂到某门课，不代写作业。</p>
      <form className="add-form" onSubmit={onSubmit}>
        <label>
          标题
          <input value={form.title} onChange={(event) => setForm({ ...form, title: event.target.value })} required />
        </label>
        <label>
          挂到课程
          <select value={form.course_id} onChange={(event) => setForm({ ...form, course_id: event.target.value })}>
            <option value="">不挂课程</option>
            {courses.map((course) => (
              <option key={course.id} value={course.id}>
                {course.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          类型
          <input
            value={form.kind}
            placeholder="例如讲义、链接，可留空"
            onChange={(event) => setForm({ ...form, kind: event.target.value })}
          />
        </label>
        <label>
          链接
          <input value={form.source_url} onChange={(event) => setForm({ ...form, source_url: event.target.value })} />
        </label>
        <label className="span-2">
          页码或位置
          <input
            value={form.page_hint}
            placeholder="你自己看到的位置，可留空"
            onChange={(event) => setForm({ ...form, page_hint: event.target.value })}
          />
        </label>
        <button type="submit">添加资料</button>
      </form>
      <form
        className="day-form"
        onSubmit={(event) => {
          event.preventDefault();
          load(filter).catch((err) => setLocalError(err.message || "读不到资料"));
        }}
      >
        <label>
          按课程筛选
          <select value={filter} onChange={(event) => setFilter(event.target.value)}>
            <option value="">全部</option>
            {courses.map((course) => (
              <option key={course.id} value={course.id}>
                {course.name}
              </option>
            ))}
          </select>
        </label>
        <button type="submit">筛选</button>
      </form>
      {localError ? <p className="error">{localError}</p> : null}
      <ul className="card-grid card-row">
        {rows.map((item) => (
          <li key={item.id}>
            <h3>{item.title}</h3>
            {item.kind ? <p>{item.kind}</p> : null}
            {item.page_hint ? <p className="quote">{item.page_hint}</p> : null}
            {item.has_image ? <img src={`/api/materials/${item.id}/image`} alt="资料原图预览" /> : null}
            {item.record_ids && item.record_ids.length ? <p>关联记录 #{item.record_ids.join("、")}</p> : null}
            {item.source_url ? (
              <a href={item.source_url} target="_blank" rel="noreferrer" title={item.source_url}>
                来源
              </a>
            ) : null}
          </li>
        ))}
      </ul>
    </>
  );
}

function ReviewPage() {
  const [form, setForm] = useState({ done: "", stuck: "", next: "" });
  const [saved, setSaved] = useState(null);
  const [localError, setLocalError] = useState("");

  useEffect(() => {
    let cancelled = false;
    apiFetch("/api/weekly-review", {})
      .then(readJson)
      .then((body) => {
        if (cancelled) {
          return;
        }
        setSaved(body);
        setForm({
          done: body.done || "",
          stuck: body.stuck || "",
          next: body.next || "",
        });
      })
      .catch((err) => {
        if (!cancelled) {
          setLocalError(err.message || "读不到本周复盘");
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function onSubmit(event) {
    event.preventDefault();
    setLocalError("");
    try {
      const body = await readJson(
        await apiFetch("/api/weekly-review", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(form),
        }),
      );
      setSaved(body);
    } catch (err) {
      setLocalError(err.message || "没有保存");
    }
  }

  return (
    <>
      <h1>本周复盘</h1>
      <p className="note">只整理你写下的话，不编造竞赛日期。{saved ? `本周 ${saved.week}` : ""}</p>
      <form className="add-form" onSubmit={onSubmit}>
        <label>
          本周完成
          <textarea value={form.done} onChange={(event) => setForm({ ...form, done: event.target.value })} />
        </label>
        <label>
          卡住
          <textarea value={form.stuck} onChange={(event) => setForm({ ...form, stuck: event.target.value })} />
        </label>
        <label className="span-2">
          下周目标
          <textarea value={form.next} onChange={(event) => setForm({ ...form, next: event.target.value })} />
        </label>
        <button type="submit">生成总结</button>
      </form>
      {localError ? <p className="error">{localError}</p> : null}
      {saved && saved.summary ? (
        <div className="query-layout columns">
          <section className="panel">
            <h2>本周总结</h2>
            <p className="quote">{saved.summary}</p>
          </section>
          <section className="panel">
            <h2>下周计划</h2>
            <p className="quote">{saved.plan}</p>
          </section>
        </div>
      ) : null}
      {saved && saved.activities && saved.activities.length ? (
        <section className="panel">
          <h2>本周依据</h2>
          <p className="note">这是实际发生的识图或补录，不会改写上面的完成、卡住和下周计划。</p>
          <ul>
            {saved.activities.map((item) => (
              <li key={item.id}>
                {item.text}
                {item.record_id ? ` 记录 #${item.record_id}` : ""}
                {item.material_id ? ` 资料 #${item.material_id}` : ""}
              </li>
            ))}
          </ul>
        </section>
      ) : null}
      {saved && saved.behind_hint ? <p className="note">{saved.behind_hint}</p> : null}
    </>
  );
}

function SettingsPage({ onSaved }) {
  const [form, setForm] = useState(null);
  const [message, setMessage] = useState("");
  const [localError, setLocalError] = useState("");

  useEffect(() => {
    let cancelled = false;
    apiFetch("/api/reminder-settings", {})
      .then(readJson)
      .then((body) => {
        if (!cancelled) {
          setForm({
            基准日: body.基准日 || "",
            提前几天: String(body.提前几天),
            是否邮件: Boolean(body.是否邮件),
          });
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setLocalError(err.message || "读不到提醒设置");
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function onSubmit(event) {
    event.preventDefault();
    setLocalError("");
    setMessage("");
    const baseline = form.基准日.trim();
    if (baseline && !/^\d{4}-\d{2}-\d{2}$/.test(baseline)) {
      setLocalError("基准日只能是 YYYY-MM-DD，或留空表示今天。不要把相对日期换算成某一天。");
      return;
    }
    const days = Number(form.提前几天);
    if (!Number.isInteger(days) || days < 1) {
      setLocalError("提前几天必须是正整数。");
      return;
    }
    try {
      const saved = await readJson(
        await apiFetch("/api/reminder-settings", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            基准日: baseline,
            提前几天: days,
            是否邮件: form.是否邮件,
          }),
        }),
      );
      setForm({
        基准日: saved.基准日,
        提前几天: String(saved.提前几天),
        是否邮件: Boolean(saved.是否邮件),
      });
      setMessage("已保存。邮件触达未接通，不会发信。");
      await onSaved();
    } catch (err) {
      setLocalError(err.message || "没有保存");
    }
  }

  return (
    <>
      <h1>提醒设置</h1>
      <p className="note">只影响当前账号的提醒窗口。邮件开关先记住选择，邮件触达未接通。</p>
      {!form ? <p>正在读取…</p> : null}
      {form ? (
        <form className="add-form" onSubmit={onSubmit}>
          <label>
            基准日
            <input
              value={form.基准日}
              placeholder="留空表示今天"
              onChange={(event) => setForm({ ...form, 基准日: event.target.value })}
            />
          </label>
          <label>
            提前几天
            <input
              type="number"
              value={form.提前几天}
              onChange={(event) => setForm({ ...form, 提前几天: event.target.value })}
              required
            />
          </label>
          <label>
            <input
              type="checkbox"
              checked={form.是否邮件}
              onChange={(event) => setForm({ ...form, 是否邮件: event.target.checked })}
            />
            是否邮件
          </label>
          <button type="submit">保存</button>
          {localError ? <p className="error">{localError}</p> : null}
          {message ? <p className="success">{message}</p> : null}
        </form>
      ) : null}
    </>
  );
}

function draftItems(body) {
  const candidate = body.candidate || {};
  const matters = candidate.事项 || [];
  if (!matters.length) {
    return [
      {
        事项: "",
        标题: "",
        类型: "竞赛",
        截止日期: "未找到",
        日期原句: "",
        来源链接: "未找到",
        报名条件: "未找到",
        主办方: "未找到",
        适合专业: "未找到",
        核对状态: "草稿",
        是否参与提醒: "否",
        标注: "未找到",
        说明: "没有识别结果。若填写，属于人工补录。",
      },
    ];
  }
  return matters.map((matter) => ({
    事项: matter.事项,
    标题: candidate.标题 === "未找到" ? "" : candidate.标题,
    类型: candidate.类型 === "未找到" ? "竞赛" : candidate.类型,
    截止日期: matter.截止日期,
    日期原句: matter.日期原句,
    来源链接: candidate.来源链接,
    报名条件: candidate.报名条件,
    主办方: candidate.主办方,
    适合专业: candidate.适合专业,
    核对状态: "草稿",
    是否参与提醒: "否",
    标注: matter.标注,
    说明: matter.说明,
  }));
}

function RecognizePage() {
  const [status, setStatus] = useState(null);
  const [draft, setDraft] = useState(null);
  const [items, setItems] = useState([]);
  const [localError, setLocalError] = useState("");
  const [saved, setSaved] = useState(null);
  const [localPreview, setLocalPreview] = useState("");
  const [polling, setPolling] = useState(false);
  const [draftId, setDraftId] = useState(null);
  const [elapsed, setElapsed] = useState(0);
  const [uploading, setUploading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    apiFetch("/api/ocr/status", {})
      .then(readJson)
      .then((body) => {
        if (!cancelled) {
          setStatus(body);
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setLocalError(err.message || "读不到识别状态");
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(
    () => () => {
      if (localPreview) {
        URL.revokeObjectURL(localPreview);
      }
    },
    [localPreview],
  );

  const busy = Boolean(draft && ["queued", "running"].includes(draft.ocr_state));

  useEffect(() => {
    if (!busy) {
      setElapsed(0);
      return undefined;
    }
    const timer = setInterval(() => setElapsed((value) => value + 1), 1000);
    return () => clearInterval(timer);
  }, [busy]);

  useEffect(() => {
    if (!polling || !draftId) {
      return undefined;
    }
    let cancelled = false;
    let timer = null;
    const startedAt = Date.now();

    async function tick() {
      if (cancelled) {
        return;
      }
      if (Date.now() - startedAt > 60000) {
        setLocalError("识别超过 60 秒还没回来。可以点「重新识别」，或取消后重传。");
        setPolling(false);
        return;
      }
      try {
        const body = await readJson(await apiFetch(`/api/image-drafts/${draftId}`, {}));
        if (cancelled) {
          return;
        }
        setDraft(body);
        if (body.ocr_state === "done") {
          setItems(draftItems(body));
        }
        if (body.ocr_state === "failed") {
          setItems([]);
        }
        if (body.poll_after_ms > 0) {
          timer = setTimeout(tick, body.poll_after_ms);
        } else {
          setPolling(false);
        }
      } catch (err) {
        if (!cancelled) {
          setLocalError(err.message || "读不到识别进度");
          setPolling(false);
        }
      }
    }

    timer = setTimeout(tick, 800);
    return () => {
      cancelled = true;
      if (timer) {
        clearTimeout(timer);
      }
    };
  }, [polling, draftId]);

  function updateItem(index, field, value) {
    setItems((current) => current.map((item, itemIndex) => (itemIndex === index ? { ...item, [field]: value } : item)));
  }

  async function onFile(event) {
    const file = event.target.files && event.target.files[0];
    event.target.value = "";
    if (!file || uploading) {
      return;
    }
    setUploading(true);
    setLocalError("");
    setSaved(null);
    setItems([]);
    setPolling(false);
    const preview = URL.createObjectURL(file);
    setLocalPreview(preview);
    setDraft({
      id: null,
      status: "pending",
      ocr_state: "queued",
      mode: "识别草稿",
      ocr_text: "",
      ocr_error: "",
      candidate: {},
      preview_url: preview,
    });
    const form = new FormData();
    form.append("file", file);
    try {
      const body = await readJson(
        await apiFetch("/api/image-drafts", { method: "POST", body: form }),
      );
      setDraft(body);
      setDraftId(body.id);
      if (body.reused || body.ocr_state === "done") {
        setItems(draftItems(body));
        setPolling(false);
      } else if (body.ocr_state === "failed") {
        setItems([]);
        setPolling(false);
      } else {
        setPolling(true);
      }
    } catch (err) {
      setDraft(null);
      setItems([]);
      setDraftId(null);
      setPolling(false);
      setLocalError(uploadErrorMessage(err));
    } finally {
      setUploading(false);
    }
  }

  async function retryDraft() {
    if (!draft?.id) {
      return;
    }
    setLocalError("");
    setSaved(null);
    try {
      const body = await readJson(
        await apiFetch(`/api/image-drafts/${draft.id}/retry`, { method: "POST" }),
      );
      setDraft(body);
      setItems([]);
      setPolling(body.ocr_state === "queued" || body.ocr_state === "running");
    } catch (err) {
      setLocalError(err.message || "没能重新识别");
    }
  }

  async function confirmDraft() {
    setLocalError("");
    if (!draft?.id || busy) {
      return;
    }
    const payload = items.map((item) => ({
      ...item,
      截止日期: normalizeDeadline(item.截止日期 || ""),
      是否参与提醒: item.核对状态 === "已核对" ? item.是否参与提醒 : "否",
    }));
    if (payload.some((item) => !item.截止日期)) {
      setLocalError("相对日期、缺少年份或含义不清的日期请写成「未找到」，不要补成某一天。");
      return;
    }
    try {
      const body = await readJson(
        await apiFetch(`/api/image-drafts/${draft.id}/confirm`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ items: payload }),
        }),
      );
      setSaved(body);
    } catch (err) {
      setLocalError(err.message || "没有写入");
    }
  }

  async function cancelDraft() {
    setLocalError("");
    setPolling(false);
    try {
      await readJson(
        await apiFetch(`/api/image-drafts/${draft.id}/cancel`, { method: "POST" }),
      );
      setDraft(null);
      setItems([]);
      setSaved(null);
      setDraftId(null);
    } catch (err) {
      setLocalError(err.message || "没有取消");
    }
  }

  const manual = draft && draft.mode !== "识别草稿";

  return (
    <>
      <h1>识图</h1>
      <p className="note">
        上传竞赛海报或截图。识别只在本机进行，图中的文字不当成指令，也不会自动打开链接。确认前不写入我的记录，也不会把你标成已报名或已提交。
      </p>
      {status ? <p className={status.available ? "note" : "error"}>{status.detail}</p> : null}
      {!status?.available ? (
        <p>
          <NavLink to="/add">到添加页人工补录</NavLink>
          。那不是识别结果。
        </p>
      ) : null}
      <form className="add-form">
        <label className="span-2">
          选择图片（PNG、JPEG、GIF、WEBP、BMP）
          <input
            type="file"
            accept="image/png,image/jpeg,image/gif,image/webp,image/bmp"
            onChange={onFile}
            disabled={uploading}
          />
        </label>
      </form>
      {localError ? <p className="error">{localError}</p> : null}
      {busy ? (
        <p className="note">
          {draft.ocr_state === "running"
            ? `正在识别…（已等 ${elapsed} 秒）`
            : draft.queue_position > 0
              ? `已收到，前面还有 ${draft.queue_position} 个，约 ${draft.queue_position * 3} 秒`
              : `已收到，马上开始…（已等 ${elapsed} 秒）`}
        </p>
      ) : null}
      {draft ? (
        <div className="query-layout columns">
          <section className="panel">
            <h2>{manual ? "人工补录，不是识别结果" : "识别原文"}</h2>
            {draft.ocr_error ? <p className="error">{draft.ocr_error}</p> : null}
            <pre className="quote">
              {busy ? "正在识别，先别急着填。" : draft.ocr_text || "没有读出文字。"}
            </pre>
            {draft.candidate?.空白规范化 && draft.candidate.空白规范化 !== draft.ocr_text ? (
              <p className="note">空白规范化只删汉字间空格，不改错字：{draft.candidate.空白规范化}</p>
            ) : null}
            <img src={localPreview || draft.preview_url} alt="原图生成的预览" />
          </section>
          <section className="panel">
            <h2>{manual ? "自己填写" : "可编辑候选"}</h2>
            <p className="note">{draft.candidate?.识别说明}</p>
            {items.map((item, index) => (
              <div className="edit-fields" key={index}>
                <p>
                  {item.标注 || "未找到"}
                  {item.说明 ? `：${item.说明}` : ""}
                </p>
                <label>
                  事项
                  <input value={item.事项} onChange={(event) => updateItem(index, "事项", event.target.value)} />
                </label>
                <label>
                  标题
                  <input value={item.标题} onChange={(event) => updateItem(index, "标题", event.target.value)} />
                </label>
                <label>
                  类型
                  <select value={item.类型} onChange={(event) => updateItem(index, "类型", event.target.value)}>
                    <option value="竞赛">竞赛</option>
                    <option value="实习">实习</option>
                    <option value="考研">考研</option>
                  </select>
                </label>
                <label>
                  日期原句（图中文字）
                  <input value={item.日期原句} onChange={(event) => updateItem(index, "日期原句", event.target.value)} />
                </label>
                <label>
                  确认后的截止日期
                  <input
                    value={item.截止日期}
                    onChange={(event) => updateItem(index, "截止日期", event.target.value)}
                  />
                </label>
                <label>
                  来源链接
                  <input value={item.来源链接} onChange={(event) => updateItem(index, "来源链接", event.target.value)} />
                </label>
                <label>
                  报名条件
                  <input value={item.报名条件} onChange={(event) => updateItem(index, "报名条件", event.target.value)} />
                </label>
                <label>
                  主办方
                  <input value={item.主办方} onChange={(event) => updateItem(index, "主办方", event.target.value)} />
                </label>
                <label>
                  核对状态
                  <select value={item.核对状态} onChange={(event) => updateItem(index, "核对状态", event.target.value)}>
                    <option value="草稿">草稿</option>
                    <option value="已核对">已核对</option>
                  </select>
                </label>
                <label>
                  是否参与提醒
                  <select
                    value={item.是否参与提醒}
                    onChange={(event) => updateItem(index, "是否参与提醒", event.target.value)}
                  >
                    <option value="否">否</option>
                    <option value="是">是</option>
                  </select>
                </label>
              </div>
            ))}
            {saved ? (
              <p className="success">
                已写入记录 {saved.record_ids.join("、")}，资料 #{saved.material_id}。上传本身不是已报名或已提交。
              </p>
            ) : (
              <div className="actions">
                <button type="button" onClick={confirmDraft} disabled={busy}>
                  {busy ? "识别中，稍等" : manual ? "按人工补录保存" : "确认写入我的记录"}
                </button>
                {draft.ocr_state === "failed" && draft.id && (draft.ocr_attempts || 0) < 3 ? (
                  <button type="button" onClick={retryDraft}>
                    重新识别
                  </button>
                ) : null}
                <button type="button" className="danger" onClick={cancelDraft}>
                  取消
                </button>
              </div>
            )}
          </section>
        </div>
      ) : null}
    </>
  );
}


function ResetPage() {
  const [token] = useState(() => {
    const params = new URLSearchParams(window.location.hash.replace(/^#/, ""));
    return params.get("token") || "";
  });
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [done, setDone] = useState(false);

  useEffect(() => {
    window.history.replaceState(null, "", "/reset");
  }, []);

  async function onSubmit(event) {
    event.preventDefault();
    setError("");
    setMessage("");
    if (password.length < 10) {
      setError("密码至少 10 位。");
      return;
    }
    if (password !== confirm) {
      setError("两次输入的密码不一致。");
      return;
    }
    try {
      const body = await readJson(
        await apiFetch("/api/auth/reset-password", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ token, password }),
        }),
      );
      if (body.ok) {
        setDone(true);
        setMessage("密码已重置，请用新密码登录。");
        setPassword("");
        setConfirm("");
      }
    } catch (err) {
      setError(err.message || "没有完成重置");
    }
  }

  if (!token) {
    return (
      <main className="login-screen">
        <p className="eyebrow">学习助手</p>
        <h1>重置密码</h1>
        <p className="error">链接不完整。请让管理员重新生成。</p>
        <p className="note"><a href="/">回登录页</a></p>
      </main>
    );
  }

  return (
    <main className="login-screen">
      <p className="eyebrow">学习助手</p>
      <h1>重置密码</h1>
      <p className="note">链接 30 分钟内有效，只能用一次。</p>
      {done ? (
        <>
          <p className="success">{message}</p>
          <p className="note"><a href="/">去登录</a></p>
        </>
      ) : (
        <form onSubmit={onSubmit}>
          <label>
            新密码
            <input
              type="password"
              autoComplete="new-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              minLength={10}
              required
            />
          </label>
          <label>
            再输一次
            <input
              type="password"
              autoComplete="new-password"
              value={confirm}
              onChange={(event) => setConfirm(event.target.value)}
              minLength={10}
              required
            />
          </label>
          <div className="actions">
            <button type="submit">重置密码</button>
          </div>
          {error ? <p className="error">{error}</p> : null}
        </form>
      )}
    </main>
  );
}

function AccountPage() {
  const [me, setMe] = useState(null);
  const [sessions, setSessions] = useState([]);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [form, setForm] = useState({ current_password: "", new_password: "", confirm: "" });

  function shortTime(value) {
    if (!value) {
      return "未知";
    }
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
  }

  async function load() {
    const [meBody, sessionBody] = await Promise.all([
      readJson(await apiFetch("/api/me")),
      readJson(await apiFetch("/api/sessions")),
    ]);
    setMe(meBody);
    setSessions(sessionBody.sessions || []);
  }

  useEffect(() => {
    let cancelled = false;
    load().catch((err) => {
      if (!cancelled) {
        setError(err.message || "读不到账号");
      }
    });
    return () => {
      cancelled = true;
    };
  }, []);

  async function revoke(id) {
    setError("");
    setMessage("");
    try {
      const body = await readJson(await apiFetch(`/api/sessions/${id}`, { method: "DELETE" }));
      if (body.current) {
        setCsrfToken("");
        window.location.href = "/";
        return;
      }
      await load();
      setMessage("已退出那台设备。");
    } catch (err) {
      setError(err.message || "没有退出");
    }
  }

  async function logoutAll() {
    if (!window.confirm("确定退出所有设备？当前页面也会退出。")) {
      return;
    }
    setError("");
    try {
      await readJson(await apiFetch("/api/auth/logout-all", { method: "POST" }));
      setCsrfToken("");
      window.location.href = "/";
    } catch (err) {
      setError(err.message || "没有退出");
    }
  }

  async function changePassword(event) {
    event.preventDefault();
    setError("");
    setMessage("");
    if (form.new_password.length < 10) {
      setError("新密码至少 10 位。");
      return;
    }
    if (form.new_password !== form.confirm) {
      setError("两次输入的新密码不一致。");
      return;
    }
    try {
      await readJson(
        await apiFetch("/api/auth/change-password", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            current_password: form.current_password,
            new_password: form.new_password,
          }),
        }),
      );
      setForm({ current_password: "", new_password: "", confirm: "" });
      setMessage("密码已改，其他设备已退出。");
    } catch (err) {
      setError(err.message || "没有改成");
    }
  }

  if (!me) {
    return (
      <>
        <h1>账号与安全</h1>
        {error ? <p className="error">{error}</p> : <p>正在读取…</p>}
      </>
    );
  }

  return (
    <>
      <h1>账号与安全</h1>
      <p className="note">
        {me.email} · {me.email_verified ? "邮箱已验证" : "邮箱未验证（不影响使用）"}
      </p>
      {error ? <p className="error">{error}</p> : null}
      {message ? <p className="success">{message}</p> : null}

      <section className="panel">
        <h2>登录的设备</h2>
        <ul className="plain-list">
          {sessions.map((item) => (
            <li key={item.id}>
              <span>{item.current ? "当前浏览器" : "其他设备"}</span>
              <span className="note" title={item.user_agent}>
                最近使用：{shortTime(item.last_seen_at)}
              </span>
              {item.current ? null : (
                <button type="button" onClick={() => revoke(item.id)}>
                  退出
                </button>
              )}
            </li>
          ))}
        </ul>
        <button type="button" className="danger" onClick={logoutAll}>
          退出所有设备
        </button>
      </section>

      <section className="panel">
        <h2>改密码</h2>
        <form className="add-form" onSubmit={changePassword}>
          <label>
            当前密码
            <input
              type="password"
              autoComplete="current-password"
              value={form.current_password}
              onChange={(event) => setForm({ ...form, current_password: event.target.value })}
              required
            />
          </label>
          <label>
            新密码
            <input
              type="password"
              autoComplete="new-password"
              value={form.new_password}
              onChange={(event) => setForm({ ...form, new_password: event.target.value })}
              minLength={10}
              required
            />
          </label>
          <label>
            再输一次
            <input
              type="password"
              autoComplete="new-password"
              value={form.confirm}
              onChange={(event) => setForm({ ...form, confirm: event.target.value })}
              minLength={10}
              required
            />
          </label>
          <button type="submit">改密码</button>
        </form>
        <p className="note">改完密码，其他设备上的登录会失效；当前浏览器保持登录。</p>
      </section>
    </>
  );
}
