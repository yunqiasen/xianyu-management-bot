from common.utils.logging_utils import log_context

class CorrelationMiddleware:
    """由主代理在三服务入口注册。跨HTTP调用沿用X-Correlation-ID。"""
    def __init__(self,app):self.app=app
    async def __call__(self,scope,receive,send):
        if scope['type']!='http':return await self.app(scope,receive,send)
        headers=dict(scope.get('headers',[]))
        with log_context(headers.get(b'x-correlation-id',b'').decode('ascii',errors='ignore')) as identifier:
            scope.setdefault('state', {})['correlation_id'] = identifier
            async def traced(message):
                if message['type']=='http.response.start':
                    message=dict(message);message['headers']=[*message.get('headers',[]),(b'x-correlation-id',identifier.encode())]
                await send(message)
            await self.app(scope,receive,traced)
