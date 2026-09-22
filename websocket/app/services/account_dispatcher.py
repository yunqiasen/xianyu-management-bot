"""仅调用 get_manager().instances 中已有账号执行方；没有登录、连接或抢租约代码。"""
from __future__ import annotations

import asyncio
import base64
import json
import time

from common.services.account_dispatch import (
    COMMANDS, CURRENT_OPERATION, DispatchError, DispatchRequest, ExecutionContext,
    OperationStore, PlatformFailure,
)
from common.services.account_request_budget import BudgetError, account_risk_config, parse_retry_after

CHAT_COMMANDS = frozenset(('get_conversations', 'get_messages', 'send_text_message', 'send_image_message', 'recall_message'))


class AccountDispatcher:
    def __init__(self, manager, sessions, budget, *, response_timeout=15, operation_timeout=60,
                 risk_loader=account_risk_config):
        self.manager, self.store, self.budget = manager, OperationStore(sessions), budget
        self.response_timeout, self.operation_timeout = response_timeout, operation_timeout
        self.risk_loader = risk_loader
        from common.services.listing_monitor_transport import execute_monitor_search_page
        self.handlers = {name: self._chat for name in CHAT_COMMANDS}
        self.handlers['monitor_search_page'] = execute_monitor_search_page
        from common.services.platform_rpc import execute_platform_request
        self.handlers['platform_request'] = execute_platform_request
        from common.services.image_gateway import execute_image_upload
        self.handlers['upload_image'] = execute_image_upload
        from common.services.video_gateway import execute_video_upload
        self.handlers['upload_video'] = execute_video_upload
        self.handlers.update({name: self._business_http for name in
                              ('polish_item', 'rate_buyer', 'request_red_flower')})

    def register(self, command, handler):
        """仅服务启动代码注册固定命令；HTTP 没有注册接口，也不反射执行用户提供的名称。"""
        if command not in COMMANDS or command in self.handlers or not callable(handler):
            raise ValueError('命令未列入白名单、已注册或处理器无效')
        self.handlers[command] = handler

    async def inspect_executor(self, owner_id, account_id):
        from common.services.account_dispatch import ExecutorStatus
        from common.services.account_policy import snapshot
        account = await self.store.account(owner_id, account_id)
        state = snapshot(account)
        versions = {key: state[key] for key in ('generation', 'credential_version', 'config_version')}
        result = ExecutorStatus(account_id=account_id, owner_id=owner_id, ready=False,
                                reason='executor_offline', **versions)
        live = self.manager.instances.get(account_id)
        if live is None:
            return result
        request = DispatchRequest(owner_id=owner_id, account_id=account_id, request_id='status',
                                  command='get_conversations', payload={}, **versions)
        try:
            await ExecutionContext(request, live, self.store, self.budget, self.risk_loader).check()
        except DispatchError as exc:
            return result.model_copy(update={'reason': exc.code})
        socket = getattr(live, 'ws', None)
        if socket is None or getattr(socket, 'closed', False):
            return result.model_copy(update={'reason': 'executor_disconnected'})
        if state['business_state'] != 'ready':
            return result.model_copy(update={'reason': 'account_' + state['business_state']})
        return result.model_copy(update={'ready': True, 'reason': None})

    async def get_operation(self, owner_id, account_id, request_id):
        return await self.store.get(owner_id, account_id, request_id)

    async def execute(self, request: DispatchRequest):
        # model_copy / Python 调用同样重新验证，避免只有 HTTP 入口校验。
        request = DispatchRequest.model_validate(request.model_dump())
        await self.store.account(request.owner_id, request.account_id)
        previous = await self.store.existing(request)
        if previous is not None: return previous
        if request.command not in self.handlers:
            raise DispatchError('command_unavailable', 501)
        live = self.manager.instances.get(request.account_id)
        if live is None: raise DispatchError('executor_offline', 409)
        context = ExecutionContext(request, live, self.store, self.budget, self.risk_loader)
        await context.check()
        operation_timeout = max(self.operation_timeout, 900) if request.command == 'upload_video' else self.operation_timeout
        duplicate = await self.store.claim(request, time.time() + operation_timeout + 5)
        if duplicate is not None: return duplicate
        token = CURRENT_OPERATION.set(context)
        try:
            async with asyncio.timeout(operation_timeout):
                result = await self.handlers[request.command](context, request.payload)
                # 外部已返回、版本却过期：仅记 unknown，不把结果当新账号状态写回。
                await context.check()
                if not context.external_started:
                    raise DispatchError('handler_missing_outbound_guard')
            return await self.store.finish(request, 'confirmed', result)
        except asyncio.CancelledError:
            await asyncio.shield(self.store.finish(request, 'unknown' if context.external_started else 'failed',
                                                  error_code='executor_interrupted'))
            raise
        except PlatformFailure as exc:
            return await self.store.finish(request, 'failed', result=exc.result, error_code=exc.code, retry_after=exc.retry_after)
        except (DispatchError, BudgetError) as exc:
            return await self.store.finish(request, 'unknown' if context.external_started else 'failed',
                                           error_code=exc.code, retry_after=getattr(exc, 'retry_after', None))
        except Exception:
            # 网络超时、提交之后进程故障和不完整回执均不是“未发送”。不存外部异常中的秘密。
            return await self.store.finish(request, 'unknown' if context.external_started else 'failed',
                                           error_code='result_unverified' if context.external_started else 'execution_error')
        finally:
            CURRENT_OPERATION.reset(token)

    async def _chat(self, context, payload):
        live, request = context.live, context.request
        socket = getattr(live, 'ws', None)
        if socket is None: raise DispatchError('executor_disconnected')
        if not isinstance(getattr(live, '_pending_mid_futures', None), dict):
            raise DispatchError('response_dispatch_unavailable')
        from common.utils.xianyu_utils import generate_mid, generate_uuid
        mid = generate_mid()
        packet = {'headers': {'mid': mid}}
        message_uuid = generate_uuid()
        command = request.command
        if command == 'get_conversations':
            packet.update(lwp='/r/Conversation/listNewestPagination',
                          body=[payload['start_timestamp'] if payload['start_timestamp'] is not None else 9007199254740991,
                                payload['limit']])
        elif command == 'get_messages':
            packet.update(lwp='/r/MessageManager/listUserMessages',
                          body=[self._jid(payload['cid']), False,
                                payload['start_timestamp'] if payload['start_timestamp'] is not None else 9007199254740991,
                                payload['limit'], False])
        elif command == 'recall_message':
            packet.update(lwp='/r/MessageManager/recallMessage', body=[payload['message_id']])
        else:
            if command == 'send_text_message':
                content = {'contentType': 1, 'text': {'text': payload['text']}}
            else:
                content = {'contentType': 2, 'image': {'pics': [dict(height=payload['height'], type=0,
                                                   url=payload['image_url'], width=payload['width'])]}}
            if not getattr(live, 'myid', None): raise DispatchError('executor_identity_missing')
            encoded = base64.b64encode(json.dumps(content, ensure_ascii=False).encode()).decode()
            packet.update(lwp='/r/MessageSend/sendByReceiverScope', body=[
                dict(uuid=message_uuid, cid=self._jid(payload['cid']), conversationType=1,
                     content={'contentType': 101, 'custom': {'type': 1, 'data': encoded}}, redPointPolicy=0,
                     extension={'extJson': '{}'}, ctx={'appVersion': '1.0', 'platform': 'web'},
                     mtags={}, msgReadStatusSetting=1),
                {'actualReceivers': [self._jid(payload['to_user_id']), self._jid(str(live.myid))]},
            ])
        evidence = {'mid': mid}
        if command.startswith('send_'):
            evidence.update(uuid=message_uuid, cid=payload['cid'], to_user_id=payload['to_user_id'])
        await self.store.note_submission(request, evidence)
        future = asyncio.get_running_loop().create_future()
        if mid in live._pending_mid_futures:
            raise DispatchError('correlation_id_collision')
        live._pending_mid_futures[mid] = future
        try:
            await context.before_external()
            # 复用已经固定代理且 fenced 的 WS；已有 receive loop 的 _dispatch_mid_response 回填 future。
            await socket.send(json.dumps(packet, ensure_ascii=False))
            response = await asyncio.wait_for(future, self.response_timeout)
            await context.check()
            if not isinstance(response, dict): raise DispatchError('invalid_platform_response')
            body = response.get('body')
            code = response.get('code')
            if code in (429, '429') or (isinstance(body, dict) and body.get('code') in ('400600001', 400600001)):
                retry_after = parse_retry_after(body.get('retryAfter') or body.get('retry_after')) if isinstance(body, dict) else None
                if retry_after is not None: await context.budget.defer(request.account_id, retry_after)
                raise PlatformFailure('platform_rate_limited', retry_after)
            if isinstance(body, dict):
                if body.get('reason'): raise PlatformFailure('platform_rejected')
                if body.get('code') not in (None, 0, '0', 200, '200'):
                    raise PlatformFailure('platform_rejected')
            if code not in (None, 200, '200'):
                raise PlatformFailure('platform_rejected')
            # 收到某个 body 不等于确认；缺少正向协议 code 的响应保持待核实。
            if code not in (200, '200') or not isinstance(body, dict):
                raise DispatchError('missing_positive_ack')
            if command.startswith('get_'): return body
            message_id = body.get('messageId') or body.get('1')
            if isinstance(message_id, dict): message_id = message_id.get('messageId') or message_id.get('1')
            return {'response': response, 'messageId': message_id if isinstance(message_id, str) else '',
                    'uuid': message_uuid, 'mid': mid}
        finally:
            live._pending_mid_futures.pop(mid, None)
            if not future.done(): future.cancel()

    @staticmethod
    def _jid(value):
        if '@' in value and not value.endswith('@goofish'):
            raise DispatchError('invalid_im_identity', 422)
        return value if value.endswith('@goofish') else value + '@goofish'


    async def _business_http(self, context, payload):
        """固定商品运输命令。复用 live.session；上层商品服务保留其业务日志/每日周期/模板。"""
        import aiohttp
        from sqlalchemy import select
        from common.models.xy_catalog_item import XYCatalogItem
        from common.models.xy_order import XYOrder
        from common.services.product_feedback_policy import feedback_eligibility
        from common.utils.xianyu_utils import generate_sign, trans_cookies
        request = context.request
        account = await context.check()
        async with self.store.sessions() as db:
            if request.command == 'polish_item':
                item = (await db.execute(select(XYCatalogItem).where(
                    XYCatalogItem.owner_id == request.owner_id, XYCatalogItem.account_pk == account.id,
                    XYCatalogItem.item_id == payload['item_id']))).scalars().first()
                if item is None: raise DispatchError('item_not_found', 404)
                if item.is_polished: raise DispatchError('item_already_polished')
                api, version, path_version = 'mtop.taobao.idle.item.polish', '2.0', '1.0'
                data = {'itemId': payload['item_id']}
            else:
                order = (await db.execute(select(XYOrder).where(XYOrder.owner_id == request.owner_id,
                    XYOrder.account_id == request.account_id, XYOrder.order_no == payload['order_no']))).scalars().first()
                if order is None: raise DispatchError('order_not_found', 404)
                kind = 'rate' if request.command == 'rate_buyer' else 'red_flower'
                reason = feedback_eligibility(account, order, kind=kind)
                if reason != 'ready': raise DispatchError('order_' + reason)
                if kind == 'rate':
                    api, version, path_version = 'mtop.taobao.idle.rate.create', '4.0', '4.0'
                    data = {'tradeId': payload['order_no'], 'rate': 1, 'feedback': payload['feedback'], 'createOrAppend': 0}
                else:
                    api, version, path_version = 'mtop.taobao.idlemessage.red.flower', '4.0', '1.0'
                    data = {'orderId': payload['order_no'], 'channel': 'list'}
        session = getattr(context.live, 'session', None)
        if session is None: raise DispatchError('executor_http_unavailable')
        token = trans_cookies(account.cookie or '').get('_m_h5_tk', '').split('_')[0]
        if not token: raise DispatchError('account_token_missing')
        timestamp = str(int(time.time() * 1000))
        data_text = json.dumps(data, separators=(',', ':'), ensure_ascii=False)
        params = dict(jsv='2.7.2', appKey='34839810', t=timestamp,
                      sign=generate_sign(timestamp, token, data_text), v=version, type='originaljson',
                      accountSite='xianyu', dataType='json', timeout='20000', api=api, sessionOption='AutoLoginOnly')
        await self.store.note_submission(request, {'api': api, **{k: v for k, v in payload.items()
                                                              if k in {'order_no', 'item_id'}}})
        await context.before_external()
        # URL 完全由白名单构建，RPC 没有 HTTP 地址/函数/脚本参数。
        async with session.post(f'https://h5api.m.goofish.com/h5/{api}/{path_version}/',
                params=params, data={'data': data_text},
                headers={'cookie': account.cookie, 'Referer': 'https://www.goofish.com/'},
                timeout=aiohttp.ClientTimeout(total=20), allow_redirects=False) as response:
            await context.check()
            if response.status == 429:
                hint = parse_retry_after(response.headers.get('Retry-After'))
                if hint is not None: await context.budget.defer(request.account_id, hint)
                raise PlatformFailure('platform_rate_limited', hint)
            if response.status >= 500: raise DispatchError('platform_result_unverified')
            if response.status != 200: raise DispatchError('platform_http_unverified')
            body = await response.json()
            ret = body.get('ret') if isinstance(body, dict) else None
            if not isinstance(ret, list) or not ret or not isinstance(ret[0], str):
                raise DispatchError('missing_positive_ack')
            success = ret[0].startswith('SUCCESS::')
            if request.command == 'polish_item' and ('POLISH_DUPLICATE' in ret[0] or '一天只能擦亮一次' in ret[0]):
                success = True
            if not success: raise PlatformFailure('platform_rejected')
            return {'success': True, 'status': 'confirmed', 'data': body.get('data') or {}}
