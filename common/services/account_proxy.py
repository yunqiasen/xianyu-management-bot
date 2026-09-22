"""Transport construction for the one explicitly selected account proxy."""
import ssl
from urllib.parse import unquote, urlsplit
import aiohttp
from common.services.account_policy import proxy_url


def account_connector(config, **options):
    url = proxy_url(config)
    if url is None:
        return aiohttp.TCPConnector(**options)
    from aiohttp_socks import ProxyConnector
    if urlsplit(url).scheme != 'https':
        return ProxyConnector.from_url(url, rdns=True, **options)
    from python_socks import ProxyType
    parsed = urlsplit(url)
    # HTTPS means TLS to the proxy, not an HTTP CONNECT proxy silently renamed.
    return ProxyConnector(host=parsed.hostname, port=parsed.port,
        proxy_type=ProxyType.HTTP, username=unquote(parsed.username) if parsed.username else None,
        password=unquote(parsed.password) if parsed.password else None,
        proxy_ssl=ssl.create_default_context(), rdns=True, **options)
