from __future__ import annotations

import html
import ipaddress
import os
import re
import socket
import urllib.error
import urllib.parse
from typing import Any

from service_club.capabilities.safe_http import PinnedHttpClient, SafeHttpError
from service_club.core.runtime.network_resilience import (
    CircuitOpenError,
    NetworkCallCancelled,
    NetworkResilience,
)


# 作用：表示网页研究请求违反安全限制或网络访问失败。
# 参数：无。
class ResearchError(ValueError):
    pass


# 作用：提供带 SSRF 防护、熔断重试和响应大小限制的网页研究。
# 参数：无。
class SafeWebResearch:
    MAX_BYTES = 2 * 1024 * 1024
    FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")
    LOCAL_HOST_SUFFIXES = (".internal", ".localhost", ".local", ".lan", ".home", ".test")

    # 作用：初始化弹性网络调用器、安全 HTTP 客户端和 Fake-IP 探测缓存。
    # 参数 network：提供重试、熔断和取消能力的网络调用器。
    def __init__(self, network: NetworkResilience | None = None) -> None:
        self.network = network or NetworkResilience()
        self.http = PinnedHttpClient()
        self._fake_ip_dns: bool | None = None

    # 作用：根据配置或 DNS 探针判断宿主机是否使用 Fake-IP 代理。
    # 参数：无。
    def _fake_ip_dns_active(self) -> bool:
        configured = os.getenv("YUKINO_ALLOW_FAKE_IP_DNS", "auto").strip().lower()
        if configured in {"1", "true", "yes", "on"}:
            return True
        if configured in {"0", "false", "no", "off"}:
            return False
        if self._fake_ip_dns is not None:
            return self._fake_ip_dns
        try:
            probe = {
                ipaddress.ip_address(item[4][0])
                for item in socket.getaddrinfo("example.com", 443)
            }
        except OSError:
            probe = set()
        self._fake_ip_dns = bool(probe) and all(
            address in self.FAKE_IP_NETWORK for address in probe
        )
        return self._fake_ip_dns

    # 作用：公开返回当前 Fake-IP DNS 检测结果。
    # 参数：无。
    def fake_ip_dns_active(self) -> bool:
        """Report whether the host intentionally uses a Fake-IP DNS range."""
        return self._fake_ip_dns_active()

    # 作用：校验网页 URL、端口、域名和全部解析地址是否允许访问。
    # 参数 url：待校验、下载或请求的完整 URL。
    def _validate_url(self, url: str) -> str:
        try:
            parsed = self.http.validate_url(url, allowed_ports={80, 443})
        except SafeHttpError as exc:
            raise ResearchError(str(exc)) from exc
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        expected_port = 443 if parsed.scheme == "https" else 80
        if port != expected_port:
            raise ResearchError("网页研究只允许 HTTP 80 或 HTTPS 443 端口。")
        hostname = (parsed.hostname or "").rstrip(".").lower()
        try:
            literal_ip = ipaddress.ip_address(hostname)
        except ValueError:
            literal_ip = None
        if literal_ip is not None and not literal_ip.is_global:
            raise ResearchError("为防止 SSRF，不允许访问内网或保留地址。")
        if "." not in hostname or hostname.endswith(self.LOCAL_HOST_SUFFIXES):
            raise ResearchError("为防止 SSRF，不允许访问本机或内部域名。")
        try:
            addresses = {
                item[4][0]
                for item in socket.getaddrinfo(
                    hostname,
                    port,
                    type=socket.SOCK_STREAM,
                )
            }
        except OSError as exc:
            raise ResearchError("域名解析失败。") from exc
        for address in addresses:
            ip = ipaddress.ip_address(address.split("%", 1)[0])
            if not self._public_address_allowed(hostname, ip, literal_ip is not None):
                raise ResearchError("为防止 SSRF，不允许访问内网或保留地址。")
        return url

    # 作用：仅放行公网地址，或代理环境中受控的 Fake-IP 地址。
    # 参数 hostname：规范化、解析或执行地址策略的目标主机名。
    # 参数 address：待审核或连接的目标 IP 地址。
    # 参数 is_literal：目标主机是否由调用方直接写成 IP 地址。
    def _public_address_allowed(
        self,
        hostname: str,
        address: ipaddress.IPv4Address | ipaddress.IPv6Address,
        is_literal: bool,
    ) -> bool:
        del hostname, is_literal
        return address.is_global or (
            address in self.FAKE_IP_NETWORK and self._fake_ip_dns_active()
        )

    # 作用：在禁止重定向的前提下下载网页，并应用重试、熔断和大小限制。
    # 参数 url：待校验、下载或请求的完整 URL。
    # 参数 timeout_seconds：网络、代码或任务执行允许持续的最长秒数。
    def _download(self, url: str, timeout_seconds: float) -> tuple[str, bytes, str, str]:
        target = self._validate_url(url)

        # 作用：重新校验目标后完成一次不自动重定向的安全 HTTP 下载。
        # 参数：无。
        def download_once() -> tuple[str, bytes, str, str]:
            request_target = self._validate_url(target)
            response = self.http.request(
                "GET",
                request_target,
                headers={
                    "User-Agent": "AGI-Yukino/0.1 (+local companion research tool)"
                },
                timeout_seconds=max(1.0, min(timeout_seconds, 20.0)),
                max_bytes=self.MAX_BYTES,
                address_policy=self._public_address_allowed,
                allowed_ports={80, 443},
            )
            if 300 <= response.status < 400:
                raise urllib.error.HTTPError(
                    request_target,
                    response.status,
                    response.reason,
                    response.headers,
                    None,
                )
            if response.status >= 400:
                raise urllib.error.HTTPError(
                    request_target,
                    response.status,
                    response.reason,
                    response.headers,
                    None,
                )
            content_type = response.headers.get_content_type()
            charset = response.headers.get_content_charset() or "utf-8"
            return request_target, response.body, content_type, charset

        try:
            hostname = urllib.parse.urlparse(target).hostname or "unknown"
            final_url, raw, content_type, charset = self.network.call(
                f"web:{hostname}",
                download_once,
                retry_if=self._retryable_error,
                max_attempts=3,
            )
        except urllib.error.HTTPError as exc:
            if 300 <= exc.code < 400:
                raise ResearchError("为防止重定向绕过 SSRF，网页工具不跟随 HTTP 重定向。") from exc
            raise ResearchError(f"网页返回 HTTP {exc.code}。") from exc
        except CircuitOpenError as exc:
            raise ResearchError(f"网页服务暂时熔断：{exc}") from exc
        except NetworkCallCancelled as exc:
            raise ResearchError(str(exc)) from exc
        except (OSError, ValueError) as exc:
            raise ResearchError(f"网页访问失败：{exc}") from exc
        if len(raw) > self.MAX_BYTES:
            raise ResearchError("网页超过 2 MiB 限制。")
        return final_url, raw, content_type, charset

    # 作用：判断网络异常或 HTTP 状态是否适合自动重试。
    # 参数 exc：待判断是否可重试的异常对象。
    @staticmethod
    # 作用：执行“retryable_error”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 exc：调用方传入的exc，用于本次处理。
    def _retryable_error(exc: Exception) -> bool:
        if isinstance(exc, urllib.error.HTTPError):
            return exc.code in {408, 425, 429, 500, 502, 503, 504}
        return isinstance(exc, (TimeoutError, urllib.error.URLError, OSError))

    # 作用：抓取网页正文，清理 HTML 后返回标题和受限长度文本。
    # 参数 url：待校验、下载或请求的完整 URL。
    # 参数 timeout_seconds：网络、代码或任务执行允许持续的最长秒数。
    def fetch(self, url: str, *, timeout_seconds: float = 10.0) -> dict[str, Any]:
        final_url, raw, content_type, charset = self._download(url, timeout_seconds)
        text = raw.decode(charset, errors="replace")
        title = ""
        if "html" in content_type:
            match = re.search(r"<title[^>]*>(.*?)</title>", text, flags=re.I | re.S)
            title = html.unescape(re.sub(r"\s+", " ", match.group(1)).strip()) if match else ""
            text = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", text, flags=re.I | re.S)
            text = re.sub(r"<[^>]+>", " ", text)
            text = html.unescape(re.sub(r"\s+", " ", text)).strip()
        return {"url": final_url, "title": title, "content_type": content_type, "content": text[:120_000]}

    # 作用：查询 DuckDuckGo Lite，并提取前若干个有效外部链接。
    # 参数 query：能力、文件、网页或图谱检索使用的查询文本。
    def search(self, query: str) -> dict[str, Any]:
        if not query.strip():
            raise ResearchError("搜索词不能为空。")
        url = "https://lite.duckduckgo.com/lite/?" + urllib.parse.urlencode({"q": query})
        final_url, raw, _, charset = self._download(url, 10.0)
        source = raw.decode(charset, errors="replace")
        links = []
        for href, label in re.findall(
            r"<a\b(?=[^>]*class=['\"][^'\"]*\bresult-link\b[^'\"]*['\"])[^>]*"
            r"href=['\"]([^'\"]+)['\"][^>]*>(.*?)</a>",
            source,
            flags=re.I | re.S,
        )[:10]:
            target = html.unescape(href)
            if target.startswith("//"):
                target = f"https:{target}"
            parsed = urllib.parse.urlparse(target)
            if parsed.hostname in {"duckduckgo.com", "www.duckduckgo.com"}:
                redirect_target = urllib.parse.parse_qs(parsed.query).get("uddg", [])
                if redirect_target:
                    target = redirect_target[0]
                    parsed = urllib.parse.urlparse(target)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                continue
            links.append(
                {
                    "title": html.unescape(re.sub(r"<[^>]+>", "", label)).strip(),
                    "url": target,
                }
            )
        return {"query": query, "results": links, "source": final_url}
