from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from email.message import Message


# 作用：表示 URL、地址、请求头或 HTTP 传输不符合安全约束。
# 参数：无。
class SafeHttpError(OSError):
    pass


# 作用：承载安全 HTTP 调用的状态、响应体和实际对端 IP。
# 参数：无。
@dataclass(frozen=True)
# 作用：定义“SafeHttpResponse”相关的数据结构、异常类型或服务组件。
# 字段：url：该对象中的结构化字段。、status：该对象中的结构化字段。、reason：该对象中的结构化字段。、headers：该对象中的结构化字段。、body：该对象中的结构化字段。、peer_ip：该对象中的结构化字段。
class SafeHttpResponse:
    url: str
    status: int
    reason: str
    headers: Message
    body: bytes
    peer_ip: str


AddressPolicy = Callable[[str, ipaddress.IPv4Address | ipaddress.IPv6Address, bool], bool]


# 作用：将 HTTP 连接固定到预先验证的 IP，避免 DNS 重绑定。
# 参数：无。
class _PinnedHTTPConnection(http.client.HTTPConnection):
    # 作用：保存已通过策略校验的目标 IP。
    # 参数 host：监听服务或建立网络连接的主机名。
    # 参数 port：监听或连接使用的网络端口。
    # 参数 pinned_ip：DNS 审核通过后固定连接的目标 IP。
    # 参数 timeout：底层网络连接的超时秒数。
    def __init__(
        self,
        host: str,
        port: int,
        *,
        pinned_ip: str,
        timeout: float,
    ) -> None:
        super().__init__(host, port, timeout=timeout)
        self._pinned_ip = pinned_ip

    # 作用：直接连接固定 IP，同时保留原始主机名用于 HTTP 请求。
    # 参数：无。
    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._pinned_ip, self.port),
            self.timeout,
        )


# 作用：将 HTTPS 连接固定到已验证 IP，并保留主机名证书校验。
# 参数：无。
class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    # 作用：保存固定 IP 和 TLS 上下文。
    # 参数 host：监听服务或建立网络连接的主机名。
    # 参数 port：监听或连接使用的网络端口。
    # 参数 pinned_ip：DNS 审核通过后固定连接的目标 IP。
    # 参数 timeout：底层网络连接的超时秒数。
    # 参数 context：HTTPS 连接使用的 TLS 安全上下文。
    def __init__(
        self,
        host: str,
        port: int,
        *,
        pinned_ip: str,
        timeout: float,
        context: ssl.SSLContext,
    ) -> None:
        super().__init__(host, port, timeout=timeout, context=context)
        self._pinned_ip = pinned_ip

    # 作用：连接固定 IP 后以原主机名完成 TLS 握手。
    # 参数：无。
    def connect(self) -> None:
        raw_socket = socket.create_connection(
            (self._pinned_ip, self.port),
            self.timeout,
        )
        try:
            self.sock = self._context.wrap_socket(
                raw_socket,
                server_hostname=self.host,
            )
        except Exception:
            raw_socket.close()
            raise


# 作用：先解析并审核地址，再固定目标 IP 完成受限 HTTP 请求。
# 参数：无。
class PinnedHttpClient:
    """Small HTTP transport that never resolves a validated host twice."""

    FORBIDDEN_REQUEST_HEADERS = {
        "connection",
        "content-length",
        "host",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }

    # 作用：校验 URL 和解析地址，逐个连接获准 IP 并限制响应大小。
    # 参数 method：MCP 或 HTTP 请求使用的方法名称。
    # 参数 url：待校验、下载或请求的完整 URL。
    # 参数 headers：调用方提供或即将发送的 HTTP 请求头。
    # 参数 body：HTTP 请求体的原始字节；为空表示无请求体。
    # 参数 timeout_seconds：网络、代码或任务执行允许持续的最长秒数。
    # 参数 max_bytes：响应、日志或输出允许占用的最大字节数。
    # 参数 address_policy：判断解析地址是否允许访问的安全回调。
    # 参数 allowed_ports：允许目标 URL 使用的端口集合；为空时不额外限制。
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        timeout_seconds: float = 10.0,
        max_bytes: int = 2 * 1024 * 1024,
        address_policy: AddressPolicy,
        allowed_ports: set[int] | None = None,
    ) -> SafeHttpResponse:
        parsed = self._parse_url(url, allowed_ports=allowed_ports)
        hostname = self._ascii_hostname(parsed.hostname or "")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        literal = self._literal_ip(hostname)
        addresses = self._resolve(hostname, port)
        approved = [
            address
            for address in addresses
            if address_policy(hostname, ipaddress.ip_address(address), literal is not None)
        ]
        if not approved:
            raise SafeHttpError("目标地址不符合网络访问策略。")

        request_headers = self._request_headers(
            hostname,
            port,
            parsed.scheme,
            headers or {},
        )
        path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        last_error: Exception | None = None
        for address in approved:
            connection = self._connection(
                parsed.scheme,
                hostname,
                port,
                address,
                timeout_seconds,
            )
            try:
                connection.request(
                    method.upper(),
                    path,
                    body=body,
                    headers=request_headers,
                )
                response = connection.getresponse()
                content_length = response.headers.get("Content-Length", "").strip()
                if content_length:
                    try:
                        declared_size = int(content_length)
                    except ValueError as exc:
                        raise SafeHttpError("响应 Content-Length 无效。") from exc
                    if declared_size < 0 or declared_size > max_bytes:
                        raise SafeHttpError("响应超过允许的大小限制。")
                payload = response.read(max_bytes + 1)
                if len(payload) > max_bytes:
                    raise SafeHttpError("响应超过允许的大小限制。")
                return SafeHttpResponse(
                    url=url,
                    status=int(response.status),
                    reason=str(response.reason or ""),
                    headers=response.headers,
                    body=payload,
                    peer_ip=address,
                )
            except SafeHttpError:
                raise
            except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
                last_error = exc
            finally:
                connection.close()
        raise SafeHttpError(f"无法连接已验证的目标地址：{last_error or 'unknown error'}")

    # 作用：对外提供无网络副作用的 URL 格式与端口校验。
    # 参数 url：待校验、下载或请求的完整 URL。
    # 参数 allowed_ports：允许目标 URL 使用的端口集合；为空时不额外限制。
    @classmethod
    # 作用：执行“validate_url”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 url：调用方传入的url，用于本次处理。
    # 参数 allowed_ports：调用方传入的allowed_ports，用于本次处理。
    def validate_url(
        cls,
        url: str,
        *,
        allowed_ports: set[int] | None = None,
    ) -> urllib.parse.SplitResult:
        return cls._parse_url(url, allowed_ports=allowed_ports)

    # 作用：只接受无凭据、无片段且端口获准的完整 HTTP/HTTPS URL。
    # 参数 url：待校验、下载或请求的完整 URL。
    # 参数 allowed_ports：允许目标 URL 使用的端口集合；为空时不额外限制。
    @staticmethod
    # 作用：执行“parse_url”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 url：调用方传入的url，用于本次处理。
    # 参数 allowed_ports：调用方传入的allowed_ports，用于本次处理。
    def _parse_url(
        url: str,
        *,
        allowed_ports: set[int] | None,
    ) -> urllib.parse.SplitResult:
        value = str(url).strip()
        if any(character in value for character in "\r\n\0"):
            raise SafeHttpError("URL 包含不允许的控制字符。")
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise SafeHttpError("只允许完整的 HTTP/HTTPS 地址。")
        if parsed.username or parsed.password:
            raise SafeHttpError("地址中不能包含用户名或密码。")
        if parsed.fragment:
            raise SafeHttpError("服务端请求地址不能包含片段标识。")
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError as exc:
            raise SafeHttpError("URL 端口无效。") from exc
        if allowed_ports is not None and port not in allowed_ports:
            raise SafeHttpError("目标端口不在允许列表中。")
        return parsed

    # 作用：使用 IDNA 规范化主机名，并限制合法长度。
    # 参数 hostname：规范化、解析或执行地址策略的目标主机名。
    @staticmethod
    # 作用：执行“ascii_hostname”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 hostname：调用方传入的hostname，用于本次处理。
    def _ascii_hostname(hostname: str) -> str:
        try:
            value = hostname.rstrip(".").encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise SafeHttpError("目标主机名无效。") from exc
        if not value or len(value) > 253:
            raise SafeHttpError("目标主机名无效。")
        return value

    # 作用：识别主机名是否直接写成 IPv4 或 IPv6 地址。
    # 参数 hostname：规范化、解析或执行地址策略的目标主机名。
    @staticmethod
    # 作用：执行“literal_ip”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 hostname：调用方传入的hostname，用于本次处理。
    def _literal_ip(hostname: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
        try:
            return ipaddress.ip_address(hostname)
        except ValueError:
            return None

    # 作用：解析并去重主机的可连接 IPv4/IPv6 地址。
    # 参数 hostname：规范化、解析或执行地址策略的目标主机名。
    # 参数 port：监听或连接使用的网络端口。
    @staticmethod
    # 作用：执行“resolve”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 hostname：调用方传入的hostname，用于本次处理。
    # 参数 port：调用方传入的port，用于本次处理。
    def _resolve(hostname: str, port: int) -> list[str]:
        try:
            records = socket.getaddrinfo(
                hostname,
                port,
                type=socket.SOCK_STREAM,
            )
        except OSError as exc:
            raise SafeHttpError("域名解析失败。") from exc
        addresses: list[str] = []
        for family, _type, _protocol, _canonical, socket_address in records:
            if family not in {socket.AF_INET, socket.AF_INET6}:
                continue
            address = str(socket_address[0]).split("%", 1)[0]
            try:
                ipaddress.ip_address(address)
            except ValueError:
                continue
            if address not in addresses:
                addresses.append(address)
        if not addresses:
            raise SafeHttpError("域名没有可用的 IPv4/IPv6 地址。")
        return addresses

    # 作用：构造安全 Host 头并拒绝跳级头、控制字符和超长值。
    # 参数 hostname：规范化、解析或执行地址策略的目标主机名。
    # 参数 port：监听或连接使用的网络端口。
    # 参数 scheme：目标连接使用的 HTTP 或 HTTPS 协议。
    # 参数 headers：调用方提供或即将发送的 HTTP 请求头。
    @classmethod
    # 作用：执行“request_headers”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 hostname：调用方传入的hostname，用于本次处理。
    # 参数 port：调用方传入的port，用于本次处理。
    # 参数 scheme：调用方传入的scheme，用于本次处理。
    # 参数 headers：调用方传入的headers，用于本次处理。
    def _request_headers(
        cls,
        hostname: str,
        port: int,
        scheme: str,
        headers: Mapping[str, str],
    ) -> dict[str, str]:
        default_port = 443 if scheme == "https" else 80
        host_value = f"[{hostname}]" if ":" in hostname else hostname
        if port != default_port:
            host_value += f":{port}"
        result = {"Host": host_value, "Accept-Encoding": "identity"}
        if len(headers) > 40:
            raise SafeHttpError("请求头数量超过限制。")
        for raw_name, raw_value in headers.items():
            name = str(raw_name).strip()
            value = str(raw_value).strip()
            if (
                not name
                or name.lower() in cls.FORBIDDEN_REQUEST_HEADERS
                or any(character in name + value for character in "\r\n\0")
                or len(name) > 100
                or len(value) > 4096
            ):
                raise SafeHttpError("请求头格式或名称不安全。")
            result[name] = value
        return result

    # 作用：按协议创建固定 IP 的 HTTP 或 HTTPS 连接。
    # 参数 scheme：目标连接使用的 HTTP 或 HTTPS 协议。
    # 参数 hostname：规范化、解析或执行地址策略的目标主机名。
    # 参数 port：监听或连接使用的网络端口。
    # 参数 address：待审核或连接的目标 IP 地址。
    # 参数 timeout_seconds：网络、代码或任务执行允许持续的最长秒数。
    @staticmethod
    # 作用：执行“connection”对应的内部处理步骤，完成输入转换、状态处理并返回约定结果。
    # 参数 scheme：调用方传入的scheme，用于本次处理。
    # 参数 hostname：调用方传入的hostname，用于本次处理。
    # 参数 port：调用方传入的port，用于本次处理。
    # 参数 address：调用方传入的address，用于本次处理。
    # 参数 timeout_seconds：本次外部调用或执行允许使用的最长秒数。
    def _connection(
        scheme: str,
        hostname: str,
        port: int,
        address: str,
        timeout_seconds: float,
    ) -> http.client.HTTPConnection:
        timeout = max(0.5, min(float(timeout_seconds), 30.0))
        if scheme == "https":
            return _PinnedHTTPSConnection(
                hostname,
                port,
                pinned_ip=address,
                timeout=timeout,
                context=ssl.create_default_context(),
            )
        return _PinnedHTTPConnection(
            hostname,
            port,
            pinned_ip=address,
            timeout=timeout,
        )
