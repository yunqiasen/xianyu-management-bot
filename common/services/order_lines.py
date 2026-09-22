"""Canonical fulfillment lines. Missing/ambiguous identities never collapse to one SKU."""
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json


@dataclass(frozen=True)
class OrderLine:
    line_id: str
    item_id: str | None
    spec_name: str | None
    spec_value: str | None
    quantity: int
    amount: str | None = None
    title: str = ''
    sku_id: str | None = None

    @property
    def payment_key(self):
        return 'payment' if self.line_id == 'main' else 'payment:' + hashlib.sha256(self.line_id.encode()).hexdigest()[:40]

    @property
    def signature(self):
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def order_lines(order):
    if (order.metadata_json or {}).get('order_lines_error'):
        raise ValueError('订单明细待核实')
    raw = (order.metadata_json or {}).get('order_lines')
    if raw is None:
        raw = [{'line_id':'main', 'item_id':order.item_id, 'spec_name':order.spec_name,
                'spec_value':order.spec_value, 'quantity':order.quantity,
                'amount':str(order.amount) if order.amount is not None else None,
                'title':(order.item_snapshot or {}).get('title') or ''}]
    if not isinstance(raw, list) or not 1 <= len(raw) <= 100: raise ValueError('订单明细格式无效')
    output, seen = [], set()
    for value in raw:
        if not isinstance(value, dict): raise ValueError('订单明细格式无效')
        identity = value.get('line_id')
        if not isinstance(identity, str) or not 1 <= len(identity) <= 80 or identity in seen:
            raise ValueError('订单明细身份缺失或重复')
        seen.add(identity)
        quantity = value.get('quantity')
        if type(quantity) is not int or not 1 <= quantity <= 1000: raise ValueError('订单数量无效')
        data = {key:value.get(key) for key in ('item_id','spec_name','spec_value','sku_id')}
        for key, content in data.items():
            if content is not None and (not isinstance(content,str) or len(content) > (255 if key.startswith('spec_') else 64)):
                raise ValueError('订单明细字段无效')
        amount = value.get('amount')
        if amount is not None:
            try:
                parsed = Decimal(str(amount))
                if not parsed.is_finite() or parsed < 0: raise ValueError()
            except (ValueError, InvalidOperation): raise ValueError('订单明细金额无效') from None
            amount = str(parsed.quantize(Decimal('.01')))
        title = value.get('title') or ''
        if not isinstance(title,str) or len(title)>255: raise ValueError('订单明细标题无效')
        output.append(OrderLine(identity, quantity=quantity, amount=amount, title=title, **data))
    if sum(line.quantity for line in output) > 1000: raise ValueError('订单总数量超过上限')
    return sorted(output, key=lambda line:line.line_id)


def canonical_lines(values):
    from types import SimpleNamespace
    return [asdict(line) for line in order_lines(SimpleNamespace(metadata_json={'order_lines':values}))]


def lines_from_platform(values):
    """Supported extension: explicit subOrderId + item/SKU fields. No guessed line IDs."""
    if values is None: return None
    if not isinstance(values,list): raise ValueError('订单明细格式无效')
    output=[]
    for value in values:
        if not isinstance(value,dict): raise ValueError('订单明细格式无效')
        try:
            raw_quantity=value['buyNum']
            if isinstance(raw_quantity,bool) or not str(raw_quantity).isdigit(): raise ValueError()
            quantity=int(raw_quantity)
        except (ValueError,TypeError,KeyError): raise ValueError('订单数量无效') from None
        output.append({'line_id':str(value.get('subOrderId') or ''), 'item_id':str(value.get('itemId') or ''),
            'sku_id':str(value['skuId']) if value.get('skuId') is not None else None,
            'spec_name':value.get('skuName'), 'spec_value':value.get('skuValue'),
            'quantity':quantity, 'amount':value.get('totalPrice'), 'title':value.get('title') or ''})
    return canonical_lines(output)


def lines_from_detail(data):
    """Decode only explicit line identities; ambiguous multi-item responses stop fulfillment."""
    if 'orderLines' in data:
        return lines_from_platform(data['orderLines'])
    entries=[component.get('data',{}) for component in data.get('components',[]) if component.get('render')=='orderInfoVO']
    if not entries: return None
    if len(entries)==1 and not (entries[0].get('subOrderId') or entries[0].get('itemInfo',{}).get('subOrderId')):
        return None  # Original single-item contract; never synthesize multiple identities.
    output=[]
    for entry in entries:
        item=entry.get('itemInfo',{})
        sku=item.get('skuInfo','')
        name,value=(sku.split(':',1) if isinstance(sku,str) and ':' in sku else ('',''))
        if isinstance(sku,list):
            if not sku or not all(isinstance(v,dict) and v.get('name') and v.get('value') for v in sku):
                raise ValueError('订单明细SKU格式无效')
            name=json.dumps([v['name'] for v in sku],ensure_ascii=False)
            value=json.dumps([v['value'] for v in sku],ensure_ascii=False)
        output.append({'subOrderId':entry.get('subOrderId') or item.get('subOrderId'),
            'itemId':item.get('itemId') or entry.get('itemId'), 'buyNum':item.get('buyAmount'),
            'skuId':item.get('skuId'), 'skuName':name.strip(), 'skuValue':value.strip(),
            'title':item.get('title') or '', 'totalPrice':item.get('totalPrice')})
    return lines_from_platform(output)
