"""GuDong adapter for upstream 2657d38 / 31db652 / 3b2de87 category cards."""
from copy import deepcopy
from utils.platform_category_fields import category_result, first_text
from utils.platform_category_selection import build_category_selection, CategorySelectionError


def selected(value):
    return value is True or str(value).lower() in ('1', 'true')


def normalize_category(value):
    value = category_result(value) or {}
    t = value.get('transportData') if isinstance(value.get('transportData'), dict) else {}
    return {
        'cat_id': first_text(value.get('cat_id'), value.get('catId'), value.get('categoryId'), t.get('catId'), t.get('categoryId')),
        'cat_name': first_text(value.get('cat_name'), value.get('catName'), value.get('text'), t.get('text'), t.get('valueName'), t.get('channelCateName')),
        'channel_cat_id': first_text(value.get('channel_cat_id'), value.get('channelCatId'), value.get('channelCategoryId'), t.get('channelCateId'), t.get('channelCategoryId')),
        'channel_cat_name': first_text(value.get('channel_cat_name'), value.get('channelCatName'), t.get('channelCateName')),
        'tb_cat_id': first_text(value.get('tb_cat_id'), value.get('tbCatId'), value.get('taobaoCategoryId'), t.get('tbCatId'), t.get('taobaoCategoryId')),
    }


def normalize_cards(response):
    data = response.get('data') or {}
    raw = data.get('cardList') or []
    if not isinstance(raw, list):
        raise ValueError('平台类目卡格式异常')
    cards = []
    for wrapper in raw:
        if not isinstance(wrapper, dict): continue
        card = deepcopy(wrapper.get('cardData') or wrapper)
        if not isinstance(card, dict):
            raise ValueError('平台属性卡格式异常')
        if not card.get('propertyId'): continue
        if not isinstance(card.get('valuesList') or [], list):
            raise ValueError('平台类目选项格式异常')
        if any(not isinstance(value, dict) for value in card.get('valuesList') or []):
            raise ValueError('平台类目选项格式异常')
        if str(card['propertyId']) == '-10000':
            for value in card.get('valuesList') or []:
                if not isinstance(value, dict): continue
                c = normalize_category(value)
                value.update(catId=c['cat_id'], catName=c['cat_name'], channelCatId=c['channel_cat_id'], tbCatId=c['tb_cat_id'])
        cards.append(card)
    return cards


def choose_cards(cards, choice):
    try:
        return build_category_selection(cards, normalize_category(choice))
    except CategorySelectionError as exc:
        raise ValueError(str(exc)) from exc


def describe_categories(response, choice=None):
    cards = normalize_cards(response)
    predicted = normalize_category((response.get('data') or {}).get('categoryPredictResult'))
    category_cards = [c for c in cards if str(c['propertyId']) == '-10000']
    if choice:
        selection = choose_cards(cards, choice)
    elif category_cards:
        # Use prediction if supplied, otherwise the platform's explicit selected option.
        source = predicted if any(predicted.values()) else next(
            (v for c in category_cards for v in c.get('valuesList') or [] if selected(v.get('isClicked'))), {})
        selection = choose_cards(cards, source)
    elif any(predicted.get(k) for k in ('cat_id', 'channel_cat_id', 'tb_cat_id')):
        selection = {'current_card_list': cards, 'selected_list': [], **predicted}
    else:
        raise ValueError('平台未返回有效类目，请调整标题或类目提示')
    cards = selection['current_card_list']
    category = normalize_category(selection)
    if selection['selected_list']:
        category['tb_cat_id'] = str(selection['selected_list'][0].get('tbCatId') or '')
    else:
        category.update(predicted)
    # The selected channel card may omit its leaf ID; use prediction only when
    # comparable IDs agree, never borrow an ID from another same-name category.
    comparable = [key for key in ('cat_id', 'channel_cat_id', 'tb_cat_id') if category.get(key) and predicted.get(key)]
    if comparable and all(category[key] == predicted[key] for key in comparable):
        for key, value in predicted.items():
            if not category.get(key):
                category[key] = value
    attributes = (choice or {}).get('attributes') or []
    if not isinstance(attributes, list): raise ValueError('平台属性必须为列表')
    by_id = {str(card['propertyId']): card for card in cards}
    for attr in attributes:
        if not isinstance(attr, dict): raise ValueError('平台属性格式异常')
        pid = str(attr.get('property_id') or '')
        card = by_id.get(pid)
        if not card or pid == '-10000': raise ValueError('所选属性已失效，请重新获取类目')
        choices = attr.get('values') if 'values' in attr else [attr]
        if not isinstance(choices, list) or not choices or any(not isinstance(c, dict) for c in choices):
            raise ValueError('平台属性选项格式异常')
        if len(choices) > 1 and not selected(card.get('isMultiple')):
            raise ValueError('当前平台属性仅支持单选')
        matches = [0] * len(choices)
        for value in card.get('valuesList') or []:
            transport = value.get('transportData') or {}
            vid = first_text(value.get('valueId'), transport.get('valueId'))
            name = first_text(value.get('text'), transport.get('valueName'), transport.get('text'))
            hits = []
            for index, option in enumerate(choices):
                hit = (str(option.get('value_id')) == vid) if option.get('value_id') not in (None, '') else (str(option.get('value_name') or '') == name)
                if hit:
                    matches[index] += 1
                    hits.append(index)
            value['isClicked'] = '1' if hits else '0'
        if any(count != 1 for count in matches):
            raise ValueError('所选属性值已失效或不唯一，请重新选择')
    candidates = []
    properties = []
    for card in cards:
        if str(card['propertyId']) == '-10000':
            for value in card.get('valuesList') or []:
                c = normalize_category(value)
                c['is_selected'] = selected(value.get('isClicked'))
                if c['is_selected']:
                    for key in category: c[key] = c.get(key) or category[key]
                candidates.append(c)
        else:
            options = []
            for v in card.get('valuesList') or []:
                t = v.get('transportData') or {}
                options.append({'value_id': first_text(v.get('valueId'), t.get('valueId')),
                                'value_name': first_text(v.get('text'), v.get('valueName'), t.get('valueName'), t.get('text')),
                                'is_selected': selected(v.get('isClicked'))})
            properties.append({'property_id': str(card['propertyId']), 'property_name': card.get('propertyName') or str(card['propertyId']), 'is_multiple': selected(card.get('isMultiple')), 'options': options})
    if not candidates: candidates = [{**category, 'is_selected': True}]
    return {'category': category, 'candidates': candidates, 'properties': properties, 'cards': cards}


def build_labels(cards):
    labels = []
    for wrapper in cards:
        card = wrapper.get('cardData') or wrapper
        for v in card.get('valuesList') or []:
            if not selected(v.get('isClicked')): continue
            t = deepcopy(v.get('transportData') or {})
            t.update(propertyId=str(card['propertyId']), propertyName=card.get('propertyName'), isUserClick='1', labelFrom='newPublish', **{'from': 'newPublishChoice'})
            t.setdefault('valueId', v.get('valueId'))
            t.setdefault('valueName', v.get('text') or v.get('valueName'))
            t.setdefault('text', v.get('text') or v.get('catName') or t.get('valueName'))
            if not t.get('properties') and v.get('properties'): t['properties'] = v['properties']
            labels.append(t)
    return labels
