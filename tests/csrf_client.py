"""测试用的客户端：写请求自动带 CSRF token。

每次写请求前读一次 /api/me，不缓存。测试量小，换来的是登录/退出后不用手动刷新。
专门的 CSRF 测试用裸 TestClient，才能控制「不带」和「带错」。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

_UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


class CsrfClient:
    def __init__(self, client: TestClient) -> None:
        self._client = client

    def request(self, method: str, url: str, **kwargs):
        if method.upper() in _UNSAFE:
            me = self._client.get("/api/me")
            if me.status_code == 200:
                headers = dict(kwargs.get("headers") or {})
                headers["X-CSRF-Token"] = me.json()["csrf_token"]
                kwargs["headers"] = headers
        return self._client.request(method, url, **kwargs)

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def put(self, url, **kwargs):
        return self.request("PUT", url, **kwargs)

    def patch(self, url, **kwargs):
        return self.request("PATCH", url, **kwargs)

    def delete(self, url, **kwargs):
        return self.request("DELETE", url, **kwargs)

    @property
    def cookies(self):
        return self._client.cookies

    def close(self):
        return self._client.close()
