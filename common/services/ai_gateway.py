"""客服 AI 的唯一无状态协议入口；一次调用最多一次付费请求。"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
import re
from typing import Any, Mapping
from urllib.parse import parse_qs, quote, urlencode, urlsplit, urlunsplit

PROVIDERS = {'openai_compatible', 'responses', 'azure', 'anthropic', 'gemini', 'dashscope_app'}
SECRET_MASK = '********'
DEFAULT_URLS = {
    'openai_compatible': 'https://dashscope.aliyuncs.com/compatible-mode/v1',
    'responses': 'https://api.openai.com/v1',
    'azure': '',
    'anthropic': 'https://api.anthropic.com',
    'gemini': 'https://generativelanguage.googleapis.com',
    'dashscope_app': 'https://dashscope.aliyuncs.com',
}


def canonical_provider(value: Any) -> str:
    value = str(value or 'openai_compatible').strip().lower().replace('-', '_')
    value = {'openai': 'openai_compatible', 'ollama': 'openai_compatible',
             'openai_responses': 'responses', 'azure_openai': 'azure',
             'claude': 'anthropic', 'google_gemini': 'gemini',
             'dashscope_compatible': 'openai_compatible', 'qwen': 'openai_compatible',
             'dashscope': 'openai_compatible', 'openai兼容': 'openai_compatible',
             'dashscope应用': 'dashscope_app'}.get(value, value)
    if value not in PROVIDERS:
        raise ValueError('未知 AI 协议，请选择支持的服务商')
    return value


def is_secret_placeholder(value: Any) -> bool:
    text = str(value or '').strip()
    return bool(text) and ('***' in text or text in {'[REDACTED_SECRET]', '[REDACTED]', '••••••••'})


@dataclass(frozen=True)
class AIConfig:
    provider_type: str
    url: str
    api_key: str = field(repr=False)
    model_name: str = ''
    azure_auth_mode: str = 'api_key'
    timeout_seconds: float = 60.0
    max_tokens: int = 512
    temperature: float = 0.7
    max_context_chars: int = 24000
    config_version: int = 0

    @classmethod
    def from_settings(cls, settings: Mapping[str, Any]) -> 'AIConfig':
        provider = canonical_provider(settings.get('provider_type'))
        base = str(settings.get('base_url') or DEFAULT_URLS[provider]).strip().rstrip('/')
        key = str(settings.get('api_key') or '').strip()
        model = str(settings.get('model_name') or '').strip()
        if not key or is_secret_placeholder(key) or '\n' in key or '\r' in key:
            raise ValueError('请填写有效 API Key')
        parts = urlsplit(base)
        try:
            port = parts.port
        except ValueError as exc:
            raise ValueError("API 地址端口无效") from exc
        if any(char.isspace() for char in base) or port == 0:
            raise ValueError("API 地址格式无效")
        if parts.scheme not in {'http', 'https'} or not parts.hostname or parts.username or parts.password or parts.fragment:
            raise ValueError('API 地址须为无内嵌凭据的 HTTP(S) 地址')
        query = parse_qs(parts.query)
        if parts.query and (provider != 'azure' or set(query) != {'api-version'}):
            raise ValueError('API 地址仅允许 Azure 的 api-version 查询参数')
        path = parts.path.rstrip('/')
        auth_mode = str(settings.get('azure_auth_mode') or 'api_key')
        if provider not in {'azure', 'dashscope_app'} and not model:
            raise ValueError('请填写模型名称')
        if provider in {'openai_compatible', 'responses'}:
            suffix = '/responses' if provider == 'responses' else '/chat/completions'
            other = '/chat/completions' if provider == 'responses' else '/responses'
            if path.endswith(other):
                raise ValueError('完整接口地址与协议类型不一致')
            if not path.endswith(suffix):
                path = (path or '/v1') + suffix
        elif provider == 'anthropic':
            if not path.endswith('/messages'):
                path += '/messages' if path.endswith('/v1') else '/v1/messages'
        elif provider == 'gemini':
            if not path.endswith(':generateContent'):
                if not path.endswith(('/v1', '/v1beta')):
                    path += '/v1beta'
                path += '/models/' + quote(model.removeprefix('models/'), safe='') + ':generateContent'
            elif path.rsplit('/models/', 1)[-1] != quote(model.removeprefix('models/'), safe='') + ':generateContent':
                raise ValueError('Gemini 完整地址的模型与配置不一致')
        elif provider == 'azure':
            deployment = str(settings.get('azure_deployment') or '').strip()
            version = str(settings.get('azure_api_version') or '').strip()
            if not deployment or not version or auth_mode not in {'api_key', 'bearer'}:
                raise ValueError('Azure 须明确部署名、API 版本及认证方式')
            suffix = '/openai/deployments/' + quote(deployment, safe='') + '/chat/completions'
            if '/deployments/' in path and not path.endswith(suffix):
                raise ValueError('Azure 完整地址的部署名与配置不一致')
            if query and query['api-version'] != [version]:
                raise ValueError('Azure 完整地址的 API 版本与配置不一致')
            if not path.endswith(suffix):
                path = path.removesuffix('/openai') + suffix
            query = {'api-version': [version]}
        elif provider == 'dashscope_app':
            app_id = str(settings.get('app_id') or '').strip()
            match = re.search(r'/apps/([^/]+)/completion$', path)
            if match:
                if '{' in match[1] or (app_id and quote(app_id, safe='') != match[1]):
                    raise ValueError('DashScope 完整地址的应用标识与配置不一致')
            elif app_id:
                path = path.removesuffix('/api/v1') + '/api/v1/apps/' + quote(app_id, safe='') + '/completion'
            else:
                raise ValueError('请填写 DashScope 应用标识')
        timeout = float(settings.get('timeout_seconds', 60))
        temperature = float(settings.get('temperature', .7))
        tokens = int(settings.get('max_tokens', 512))
        context = int(settings.get('max_context_chars', 24000))
        if not math.isfinite(timeout) or not 0 < timeout <= 120 or not math.isfinite(temperature) or not 0 <= temperature <= 2 or not 1 <= tokens <= 32768 or not 256 <= context <= 200000:
            raise ValueError('AI 超时或生成预算超出范围')
        url = urlunsplit((parts.scheme, parts.netloc, path, urlencode(query, doseq=True), ''))
        return cls(provider, url, key, model, auth_mode, timeout, tokens, temperature, context, int(settings.get('config_version') or 0))


class AIGatewayError(RuntimeError):
    """可展示的错误，不携带上游正文、请求地址或认证头。"""
    def __init__(self, stage: str, *, status_code: int | None = None, provider_code: str | None = None, call_id: str = ''):
        messages = {'configuration': 'AI 配置无效', 'authentication': 'AI 认证失败',
                    'http': 'AI 服务返回 HTTP 错误', 'provider': 'AI 服务返回业务错误',
                    'parse': 'AI 响应格式错误', 'empty': 'AI 返回空正文',
                    'timeout': 'AI 请求超时，未自动重试', 'connection': 'AI 连接失败，未自动重试',
                    'budget': '最新消息或系统提示词超出上下文预算'}
        super().__init__(messages.get(stage, 'AI 调用失败'))
        self.stage, self.status_code, self.provider_code, self.call_id = stage, status_code, provider_code, call_id

    def public_data(self) -> dict[str, Any]:
        return {'stage': self.stage, 'status_code': self.status_code, 'provider_code': self.provider_code, 'call_id': self.call_id}


@dataclass(frozen=True)
class AIResult:
    text: str
    call_id: str
    config_version: int
    trimmed_messages: int = 0
    stage: str = 'success'


def _window(messages: list[dict[str, Any]], budget: int) -> tuple[list[dict[str, str]], int]:
    rows = [{'role': str(row['role']), 'content': str(row['content'])} for row in messages]
    if not rows or any(row['role'] not in {'system', 'user', 'assistant'} for row in rows):
        raise AIGatewayError('configuration')
    original = len(rows)
    while sum(len(row['content']) for row in rows) > budget:
        removable = next((i for i, row in enumerate(rows[:-1]) if row['role'] != 'system'), None)
        if removable is None:
            raise AIGatewayError('budget')
        rows.pop(removable)
    return rows, original - len(rows)


def _request(config: AIConfig, messages: list[dict[str, str]]) -> tuple[dict[str, str], dict[str, Any]]:
    headers = {'Content-Type': 'application/json', 'Authorization': f'Bearer {config.api_key}'}
    options = {'max_tokens': config.max_tokens, 'temperature': config.temperature}
    provider = config.provider_type
    if provider == 'responses':
        return headers, {'model': config.model_name, 'input': messages, 'max_output_tokens': config.max_tokens, 'temperature': config.temperature, 'stream': False, 'store': False}
    if provider == 'azure':
        if config.azure_auth_mode == 'api_key':
            headers.pop('Authorization')
            headers['api-key'] = config.api_key
        return headers, {'messages': messages, **options, 'stream': False}
    system = '\n'.join(row['content'] for row in messages if row['role'] == 'system')
    turns = [row for row in messages if row['role'] != 'system']
    if provider == 'anthropic':
        headers.pop('Authorization')
        headers.update({'x-api-key': config.api_key, 'anthropic-version': '2023-06-01'})
        return headers, {'model': config.model_name, 'system': system, 'messages': turns, **options, 'stream': False}
    if provider == 'gemini':
        headers.pop('Authorization')
        headers['x-goog-api-key'] = config.api_key
        return headers, {
            'systemInstruction': {'parts': [{'text': system}]},
            'contents': [{'role': 'model' if row['role'] == 'assistant' else 'user', 'parts': [{'text': row['content']}]} for row in turns],
            'generationConfig': {'maxOutputTokens': config.max_tokens, 'temperature': config.temperature},
        }
    if provider == 'dashscope_app':
        # 不复用服务端 session_id，账号/会话历史完全由调用方提供，防止跨账号串话。
        return headers, {'input': {'messages': messages}, 'parameters': options}
    return headers, {'model': config.model_name, 'messages': messages, **options, 'stream': False}


def _provider_code(data: Any, secret: str) -> str | None:
    if not isinstance(data, dict):
        return None
    error = data.get('error')
    code = error.get('code') or error.get('type') if isinstance(error, dict) else data.get('code')
    if not code:
        return None
    code = str(code)
    return code if secret not in code and re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]{0,63}', code) else 'redacted'


def _extract(provider: str, data: Any) -> str:
    if not isinstance(data, dict):
        raise AIGatewayError('parse')
    if data.get('error') or (provider == 'dashscope_app' and data.get('code') not in (None, '', 'Success', 'success')):
        raise AIGatewayError('provider')
    try:
        if provider in {'openai_compatible', 'azure'}:
            content = data['choices'][0]['message']['content']
            if isinstance(content, list):
                content = ''.join(row['text'] for row in content if row.get('type') == 'text')
        elif provider == 'responses':
            if data.get('status') in {'failed', 'incomplete', 'cancelled'}:
                raise AIGatewayError('provider')
            content = ''.join(part['text'] for row in data['output'] if row.get('type') == 'message' for part in row['content'] if part.get('type') == 'output_text')
        elif provider == 'anthropic':
            content = ''.join(row['text'] for row in data['content'] if row.get('type') == 'text')
        elif provider == 'gemini':
            content = ''.join(row.get('text', '') for row in data['candidates'][0]['content']['parts'] if not row.get('thought'))
        else:
            content = data['output']['text']
        if content is None or content == '':
            raise AIGatewayError('empty')
        if not isinstance(content, str):
            raise AIGatewayError('parse')
        text = content.strip()
        if not text:
            raise AIGatewayError('empty')
        return text
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise AIGatewayError('parse') from exc


async def generate_text(settings: Mapping[str, Any] | AIConfig, messages: list[dict[str, Any]], *, client: Any = None) -> AIResult:
    """测试和线上共用；不重试、不沿用供应商会话、不记录内容或凭据。"""
    import asyncio
    import uuid
    import httpx

    call_id = uuid.uuid4().hex
    try:
        config = settings if isinstance(settings, AIConfig) else AIConfig.from_settings(settings)
    except (ValueError, TypeError) as exc:
        raise AIGatewayError('configuration', call_id=call_id) from exc
    owns_client = client is None
    http_client = client or httpx.AsyncClient(timeout=config.timeout_seconds, follow_redirects=False, trust_env=False)
    try:
        window, trimmed = _window(messages, config.max_context_chars)
        headers, payload = _request(config, window)
        async with asyncio.timeout(config.timeout_seconds):
            response = await http_client.post(config.url, headers=headers, json=payload, timeout=config.timeout_seconds)
        try:
            data = response.json()
        except ValueError:
            data = None
        code = _provider_code(data, config.api_key)
        if not 200 <= response.status_code < 300:
            raise AIGatewayError('authentication' if response.status_code in {401, 403} else 'http', status_code=response.status_code, provider_code=code)
        try:
            text = _extract(config.provider_type, data)
        except AIGatewayError as exc:
            exc.status_code, exc.provider_code = response.status_code, code
            raise
        return AIResult(text, call_id, config.config_version, trimmed)
    except (TimeoutError, httpx.TimeoutException) as exc:
        raise AIGatewayError('timeout', call_id=call_id) from exc
    except httpx.HTTPError as exc:
        raise AIGatewayError('connection', call_id=call_id) from exc
    except AIGatewayError as exc:
        exc.call_id = call_id
        raise
    finally:
        if owns_client:
            await http_client.aclose()
