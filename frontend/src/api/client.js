// 最小 API 客户端：带上 Cookie，写请求带 CSRF token。
// 错误映射、AbortSignal、JSON 自动序列化留给批次 4A。

let csrfToken = "";

export function setCsrfToken(token) {
  csrfToken = token || "";
}

export function getCsrfToken() {
  return csrfToken;
}

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
