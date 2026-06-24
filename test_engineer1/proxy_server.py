"""Serve a static frontend and proxy declared backend API paths on one origin."""

from __future__ import annotations

import argparse
import functools
import http.server
import urllib.error
import urllib.request
from pathlib import Path


_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


def should_proxy_path(request_path: str, api_paths: set[str]) -> bool:
    return request_path.split("?", 1)[0] in api_paths


class ContractProxyHandler(http.server.SimpleHTTPRequestHandler):
    backend_url = ""
    api_paths: set[str] = set()

    def _is_api_path(self) -> bool:
        return should_proxy_path(self.path, self.api_paths)

    def _proxy(self) -> None:
        body_length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(body_length) if body_length else None
        target = self.backend_url.rstrip("/") + self.path
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in _HOP_BY_HOP_HEADERS | {"host", "content-length"}
        }
        request = urllib.request.Request(
            target,
            data=body,
            headers=headers,
            method=self.command,
        )
        try:
            response = urllib.request.urlopen(request, timeout=15)
        except urllib.error.HTTPError as exc:
            response = exc
        except urllib.error.URLError as exc:
            self.send_error(502, f"backend unavailable: {exc.reason}")
            return

        with response:
            payload = response.read()
            self.send_response(response.status)
            for key, value in response.headers.items():
                if key.lower() not in _HOP_BY_HOP_HEADERS | {"content-length"}:
                    self.send_header(key, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        if self._is_api_path():
            self._proxy()
            return
        super().do_GET()

    def do_HEAD(self) -> None:  # noqa: N802
        if self._is_api_path():
            self._proxy()
            return
        super().do_HEAD()

    def do_POST(self) -> None:  # noqa: N802
        self._proxy_or_method_not_allowed()

    def do_PUT(self) -> None:  # noqa: N802
        self._proxy_or_method_not_allowed()

    def do_PATCH(self) -> None:  # noqa: N802
        self._proxy_or_method_not_allowed()

    def do_DELETE(self) -> None:  # noqa: N802
        self._proxy_or_method_not_allowed()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._proxy_or_method_not_allowed()

    def _proxy_or_method_not_allowed(self) -> None:
        if self._is_api_path():
            self._proxy()
            return
        self.send_error(405, "method not allowed for static files")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--backend-url", required=True)
    parser.add_argument("--api-path", action="append", default=[])
    args = parser.parse_args()

    handler_class = type(
        "ConfiguredContractProxyHandler",
        (ContractProxyHandler,),
        {
            "backend_url": args.backend_url,
            "api_paths": set(args.api_path),
        },
    )
    handler = functools.partial(
        handler_class,
        directory=str(Path(args.directory).resolve()),
    )
    with http.server.ThreadingHTTPServer(("127.0.0.1", args.port), handler) as server:
        server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
