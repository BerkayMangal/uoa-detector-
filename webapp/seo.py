"""Static public introduction and indexing rules for the private trading desk."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

ORIGIN = "https://uoa-detector-production.up.railway.app"
PUBLIC_GET_PATHS = frozenset({
    "/health", "/hakkinda", "/robots.txt", "/sitemap.xml", "/assets/uoa.css", "/favicon.svg",
})
TITLES = {
    "/": "Bugün", "/alfa": "Alfa Board", "/gamma": "Gamma ve volatilite",
    "/journal": "İşlem günlüğü", "/sonuclar": "Sonuçlar", "/defter": "Karar defteri",
    "/opsiyon": "Opsiyon", "/hakkinda": "Opsiyon akışı ve kişisel piyasa çalışma masası",
}
DESCRIPTION = "UOA, ABD hisseleri ve opsiyon akışını fiyat, haber ve risk bağlamında bir araya getiren kişisel piyasa çalışma masasıdır. İşlem kararları kullanıcıya aittir."


def page_title(path: str) -> str:
    return TITLES.get(path, "Kayıt ayrıntısı") + " · UOA"


class SearchHeaders:
    """Apply indexing/cache rules to successes and auth errors alike."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                path = scope["path"]
                public = scope["method"] == "GET" and path in PUBLIC_GET_PATHS and message["status"] == 200
                headers = [(k, v) for k, v in message.get("headers", [])
                           if k.lower() not in (b"x-robots-tag", b"cache-control")]
                index = public and path == "/hakkinda"
                headers.append((b"x-robots-tag", b"index, follow" if index else b"noindex, nofollow, nosnippet"))
                cache = b"public, max-age=300" if public else b"private, no-store"
                if path == "/health":
                    cache = b"no-store"
                if public and path in ("/assets/uoa.css", "/favicon.svg"):
                    cache = b"public, max-age=86400"
                headers.extend([(b"cache-control", cache), (b"x-content-type-options", b"nosniff")])
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, with_headers)
