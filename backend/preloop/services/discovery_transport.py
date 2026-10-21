"""Pin custom-provider discovery connections to policy-checked IP addresses."""

import httpx


class DiscoveryTransport(httpx.AsyncBaseTransport):
    """Preserve Host/TLS identity while preventing a second DNS resolution."""

    def __init__(self, *, verify: bool | str = True) -> None:
        self.transport = httpx.AsyncHTTPTransport(verify=verify, trust_env=False)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        from preloop.services.ai_model_provider import (
            ProviderValidationError,
            _allowed_endpoint_networks,
            _endpoint_address_error,
            _parse_ip_literal,
            _resolve_endpoint_host_uncached,
        )

        if request.url.scheme not in {"http", "https"}:
            raise ProviderValidationError("api_endpoint must be an http(s) URL")
        host = request.url.host
        literal = _parse_ip_literal(host)
        addresses = (
            [literal] if literal is not None else _resolve_endpoint_host_uncached(host)
        )
        if not addresses:
            raise ProviderValidationError("api_endpoint host did not resolve")
        for address in addresses:
            error = _endpoint_address_error(address, _allowed_endpoint_networks())
            if error:
                raise ProviderValidationError(error)
        headers = request.headers.copy()
        headers["Host"] = request.url.netloc.decode("ascii")
        pinned = httpx.Request(
            request.method,
            request.url.copy_with(host=str(addresses[0])),
            headers=headers,
            stream=request.stream,
            extensions={**request.extensions, "sni_hostname": host},
        )
        return await self.transport.handle_async_request(pinned)

    async def aclose(self) -> None:
        await self.transport.aclose()
