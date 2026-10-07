"""Run against upstream_fixture.py only; platform is stubbed, persistence is real."""
import json
import os
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

base = os.environ['TEST_BASE_URL']
out = Path(os.environ['TEST_OUTPUT']); out.mkdir(parents=True, exist_ok=True)
token = os.environ['TEST_TOKEN']
checks = []
with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=os.environ['CHROMIUM_PATH'], headless=True, args=['--no-proxy-server'])
    context = browser.new_context(viewport={'width':1440,'height':1000})
    context.add_init_script('localStorage.setItem("auth_token", '+json.dumps(token)+');')
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.goto(base+'/admin', wait_until='networkidle')
    page.locator('[onclick="showSection(\'item-publish\')"]').click()
    expect(page.locator('#publishMaterialList .item-publish-side-item')).to_have_count(20)
    page.locator('#publishMaterialPageSize').select_option('10')
    expect(page.locator('#publishMaterialList .item-publish-side-item')).to_have_count(10)
    expect(page.locator('#publishMaterialPageStatus')).to_contain_text('1 / 3 页')
    page.locator('#publishMaterialNext').click()
    expect(page.locator('#publishMaterialPageStatus')).to_contain_text('2 / 3 页')
    page.locator('#publishMaterialNext').click()
    expect(page.locator('#publishMaterialList .item-publish-side-item')).to_have_count(5)
    checks.append('material pagination 20/10 and last page')
    page.locator('#publishMaterialList button').filter(has_text='载入').first.click()
    page.locator('#publishTitle').fill('类目兼容UI测试')
    page.locator('#publishDescription').fill('不发布，只测试素材保存')
    page.locator('#publishCookieId').select_option('fixture-account')
    page.locator('#publishCategoryRecommendBtn').click()
    expect(page.locator('#publishPlatformCategoryStatus')).to_contain_text('已选择')
    expect(page.locator('#publishPlatformCategory option')).to_have_count(3)
    page.locator('#publishPlatformCategory').select_option('0')
    expect(page.locator('#publishCategoryAttr0')).to_be_visible()
    page.locator('#publishCategoryAttr0').select_option('0')
    page.locator('#publishCategoryAttr1').select_option(['0','1'])
    page.locator('#itemPublishSaveMaterialBtn').click()
    expect(page.locator('#itemPublishSaveMaterialBtn')).to_be_enabled()
    mid = page.evaluate('itemPublishLoadedMaterialId')
    material = context.request.get(base+f'/product-materials/{mid}',headers={'Authorization':'Bearer '+token}).json()['material']
    assert material['platform_category']['channel_cat_id']=='10', material
    assert material['platform_category']['attributes'][0]['value_id']=='b1'
    assert [v['value_id'] for v in material['platform_category']['attributes'][1]['values']]==['t1','t2']
    checks.append('category recommendation, same-name category selection, property selection, real API persistence')
    page.reload(wait_until='networkidle')
    page.locator('[onclick="showSection(\'item-publish\')"]').click()
    # Fetch the saved material through the API independently of the current page.
    page.evaluate('async mid => {const data=await requestItemPublishJson(`/product-materials/${mid}`); itemPublishMaterials=[data.material]; renderItemPublishMaterials();}', mid)
    page.locator('#publishMaterialList button').filter(has_text='载入').first.click()
    assert page.evaluate('itemPublishCategoryState.choice.channel_cat_id')=='10'
    expect(page.locator('#publishCategoryAttr1 option:checked')).to_have_count(2)
    page.locator('#publishCategoryAttr1').select_option([])
    page.locator('#itemPublishSaveMaterialBtn').click()
    expect(page.locator('#itemPublishSaveMaterialBtn')).to_be_enabled()
    material = context.request.get(base+f'/product-materials/{mid}',headers={'Authorization':'Bearer '+token}).json()['material']
    assert material['platform_category']['attributes'][1]['values'] == []
    page.reload(wait_until='networkidle')
    page.evaluate("showSection('item-publish')")
    page.evaluate('async mid => {const data=await requestItemPublishJson(`/product-materials/${mid}`); itemPublishMaterials=[data.material]; renderItemPublishMaterials();}', mid)
    page.locator('#publishMaterialList button').filter(has_text='载入').first.click()
    expect(page.locator('#publishCategoryAttr1 option:checked')).to_have_count(0)
    assert page.evaluate('itemPublishCategoryState.choice.attributes[1].values') == []
    page.locator('#itemPublishSaveMaterialBtn').click()
    expect(page.locator('#itemPublishSaveMaterialBtn')).to_be_enabled()
    material = context.request.get(base+f'/product-materials/{mid}',headers={'Authorization':'Bearer '+token}).json()['material']
    assert material['platform_category']['attributes'][1]['values'] == []
    checks.append('explicit multi-select clear persists through save, reload and second save')
    page.locator('#publishTitle').fill('无默认分类')
    page.locator('#publishCookieId').select_option('fixture-account')
    page.locator('#publishCategoryRecommendBtn').click()
    expect(page.locator('#publishPlatformCategory option')).to_have_count(3)
    assert page.evaluate('itemPublishCategoryState.choice') is None
    page.locator('#publishPlatformCategory').select_option('0')
    expect(page.locator('#publishPlatformCategoryStatus')).to_contain_text('已选择')
    assert page.evaluate('itemPublishCategoryState.choice.channel_cat_id') == '10'
    checks.append('candidates without platform default remain visible and manually selectable')
    page.locator('#publishTitle').fill('修改标题后清除分类')
    expect(page.locator('#publishPlatformCategoryStatus')).to_contain_text('未选择')
    assert page.evaluate('itemPublishCategoryState.choice') is None
    checks.append('material reload preserves selection; title edit clears stale selection')
    page.screenshot(path=str(out/'publish-desktop.png'),full_page=True)
    page.set_viewport_size({'width':390,'height':844})
    page.wait_for_function("document.querySelector('#itemPublishForm').getBoundingClientRect().width > 250 && document.querySelector('.main-content').getBoundingClientRect().x < 1")
    page.wait_for_timeout(400)  # Finish the existing sidebar CSS transition before capture.
    page.screenshot(path=str(out/'publish-mobile.png'),full_page=True)
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth+1'), 'mobile horizontal overflow'
    checks.append('390px mobile no horizontal overflow')
    page.set_viewport_size({'width':1440,'height':1000})
    page.evaluate("showSection('notification-channels')")
    page.locator('[onclick="showAddChannelModal(\'webhook\')"]').click()
    page.locator('#channelName').fill('UI测试渠道')
    page.locator('#add_webhook_url').fill('http://127.0.0.1:9/not-sent')
    page.locator('#add_chat_template').fill('测试 {{buyer_nick}}：{{message}}')
    page.locator('[onclick="previewChannelTemplate(\'add_\', \'chat\')"]').click()
    expect(page.locator('#add_chat_template_preview')).to_contain_text('测试')
    page.screenshot(path=str(out/'channel-template.png'),full_page=True)
    page.locator('[onclick="saveNotificationChannel()"]').click()
    expect(page.locator('#addChannelModal')).not_to_be_visible()
    channels=context.request.get(base+'/notification-channels',headers={'Authorization':'Bearer '+token}).json()
    channel=next(c for c in channels if c['name']=='UI测试渠道')
    assert json.loads(channel['config'])['chat_template']=='测试 {{buyer_nick}}：{{message}}'
    page.locator(f'[onclick="editNotificationChannel({channel["id"]})"]').click()
    expect(page.locator('#edit_chat_template')).to_have_value('测试 {{buyer_nick}}：{{message}}')
    page.locator('#edit_chat_template').fill('{{typo}}')
    page.locator('[onclick="previewChannelTemplate(\'edit_\', \'chat\')"]').click()
    expect(page.locator('#edit_chat_template_preview')).to_contain_text('不支持')
    page.locator('#edit_chat_template').fill('')
    page.locator('[onclick="updateNotificationChannel()"]').click()
    expect(page.locator('#editChannelModal')).not_to_be_visible()
    page.reload(wait_until='networkidle')
    channels=context.request.get(base+'/notification-channels',headers={'Authorization':'Bearer '+token}).json()
    channel=next(c for c in channels if c['name']=='UI测试渠道')
    assert json.loads(channel['config'])['chat_template']==''
    checks.append('channel create, preview, save, edit, validation, clear and reload via real API')
    assert not errors, errors
    checks.append('zero browser page errors')
    (out/'browser-results.json').write_text(json.dumps({'checks':checks,'platform':'fixture only','browser_errors':errors},ensure_ascii=False,indent=2))
    print(json.dumps(checks,ensure_ascii=False))
    browser.close()
