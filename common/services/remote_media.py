"""Public media fetches share account execution/budget, but never its credentials.

Resolve once, validate every result, connect to the pinned public IP through the
account's fixed connector and preserve the original Host/TLS name. Each redirect
is resolved and checked afresh; no implicit environment proxy or cookie jar.
"""
import asyncio
import ipaddress
import socket
from urllib.parse import urljoin
import aiohttp
from yarl import URL
from common.services.account_proxy import account_connector


def validate_media_url(value):
    try:
        url = URL(value)
        if (not isinstance(value, str) or not 1 <= len(value) <= 4096 or url.scheme not in {'http', 'https'}
                or not url.raw_host or url.user is not None or url.password is not None
                or url.fragment or url.port not in (80, 443)):
            raise ValueError()
        return url
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError('invalid_remote_media_url') from exc


def _public(address):
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not (ip.is_multicast or ip.is_reserved)


async def _pin(url):
    from common.services.account_dispatch import PlatformFailure
    host = url.raw_host
    try:
        try:
            ipaddress.ip_address(host)
            addresses = [host]
        except ValueError:
            records = await asyncio.to_thread(socket.getaddrinfo, host, url.port, 0, socket.SOCK_STREAM)
            addresses = list(dict.fromkeys(record[4][0] for record in records))
        if not addresses or not all(_public(value) for value in addresses):
            raise PlatformFailure('remote_media_address_blocked')
        return url.with_host(addresses[0])
    except (OSError, ValueError) as exc:
        raise PlatformFailure('remote_media_dns_failed') from exc


async def read_remote_media(context, value, *, max_bytes):
    from common.services.account_dispatch import PlatformFailure
    from common.services.account_request_budget import parse_retry_after
    url = validate_media_url(value)
    for redirects in range(4):
        pinned = await _pin(url)
        await context.before_external()
        account = await context.check()
        proxy = {key:getattr(account, key) for key in ('proxy_type','proxy_host','proxy_port','proxy_user','proxy_pass')}
        try:
            connector = account_connector(proxy)
            async with aiohttp.ClientSession(connector=connector, trust_env=False,
                    cookie_jar=aiohttp.DummyCookieJar(), timeout=aiohttp.ClientTimeout(total=60)) as http:
                async with http.get(pinned, headers={'Host':url.raw_authority, 'Accept':'*/*',
                        'Referer':'https://www.goofish.com/', 'User-Agent':'Mozilla/5.0'},
                        server_hostname=url.raw_host if url.scheme == 'https' else None,
                        allow_redirects=False) as response:
                    if response.status == 429 or response.status == 420:
                        delay = parse_retry_after(response.headers.get('Retry-After')) or 60
                        await context.budget.defer(context.request.account_id, delay)
                        raise PlatformFailure('platform_rate_limited', retry_after=delay)
                    if response.status in {301,302,303,307,308}:
                        if redirects == 3 or not response.headers.get('Location'):
                            raise PlatformFailure('remote_media_redirect_failed')
                        url = validate_media_url(urljoin(str(url), response.headers['Location']))
                        continue
                    if response.status != 200:
                        raise PlatformFailure('remote_media_http_' + str(response.status))
                    if response.content_length is not None and response.content_length > max_bytes:
                        raise PlatformFailure('remote_media_too_large')
                    chunks, size = [], 0
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        size += len(chunk)
                        if size > max_bytes:
                            raise PlatformFailure('remote_media_too_large')
                        chunks.append(chunk)
                    if not size:
                        raise PlatformFailure('remote_media_empty')
                    content_type = response.headers.get('Content-Type', '').split(';', 1)[0].strip()
            await context.check()
            return b''.join(chunks), content_type
        except PlatformFailure:
            raise
        except Exception as exc:
            # Fetching is read-only. A failed fetch is a known non-upload, not a
            # reason to retransmit a previously submitted platform upload.
            raise PlatformFailure('remote_media_fetch_failed') from exc
    raise PlatformFailure('remote_media_redirect_failed')
