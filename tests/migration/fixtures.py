"""Handcrafted redacted rows matching audited GuDong DDL; no live DB imports."""
import sqlite3

KEY = b'synthetic-integrity-key-32-bytes!!'


def make_source(path):
    with sqlite3.connect(path) as db:
        db.executescript('''
        CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT, email TEXT, password_hash TEXT, is_active INTEGER, is_admin INTEGER);
        CREATE TABLE cookies(id TEXT PRIMARY KEY, user_id INTEGER, value TEXT, pause_duration INTEGER, username TEXT, password TEXT, remark TEXT, proxy_type TEXT, proxy_host TEXT, proxy_port INTEGER, proxy_user TEXT, proxy_pass TEXT, auto_confirm INTEGER);
        CREATE TABLE cookie_status(cookie_id TEXT PRIMARY KEY, enabled INTEGER);
        CREATE TABLE ai_reply_settings(cookie_id TEXT PRIMARY KEY, ai_enabled INTEGER, api_type TEXT, api_key TEXT, model_name TEXT, base_url TEXT, max_bargain_rounds INTEGER);
        CREATE TABLE keywords(cookie_id TEXT, keyword TEXT, reply TEXT, item_id TEXT, type TEXT, image_url TEXT);
        CREATE TABLE default_replies(cookie_id TEXT PRIMARY KEY, enabled INTEGER, reply_content TEXT, reply_once INTEGER);
        CREATE TABLE default_reply_records(id INTEGER PRIMARY KEY, cookie_id TEXT, chat_id TEXT, replied_at TIMESTAMP);
        CREATE TABLE item_replay(id INTEGER PRIMARY KEY, cookie_id TEXT, item_id TEXT, reply_content TEXT);
        CREATE TABLE ai_conversations(id INTEGER PRIMARY KEY, cookie_id TEXT, chat_id TEXT, user_id TEXT, item_id TEXT, role TEXT, content TEXT, intent TEXT, bargain_count INTEGER, created_at TIMESTAMP);
        CREATE TABLE chat_messages(id INTEGER PRIMARY KEY, cookie_id TEXT, chat_id TEXT, sender_id TEXT, content TEXT, direction INTEGER, reply_source TEXT, created_at TIMESTAMP);
        CREATE TABLE notification_channels(id INTEGER PRIMARY KEY, user_id INTEGER, name TEXT, type TEXT, config TEXT, enabled INTEGER);
        CREATE TABLE message_notifications(id INTEGER PRIMARY KEY, cookie_id TEXT, channel_id INTEGER, enabled INTEGER);
        CREATE TABLE user_settings(id INTEGER PRIMARY KEY, user_id INTEGER, key TEXT, value TEXT);
        CREATE TABLE system_settings(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE item_info(id INTEGER PRIMARY KEY, cookie_id TEXT, item_id TEXT, item_title TEXT, item_price TEXT, item_detail TEXT, is_multi_spec INTEGER, multi_quantity_delivery INTEGER, created_at TIMESTAMP);
        CREATE TABLE cards(id INTEGER PRIMARY KEY, user_id INTEGER, name TEXT, type TEXT, data_content TEXT, enabled INTEGER, is_multi_spec INTEGER, spec_name TEXT, spec_value TEXT);
        CREATE TABLE orders(order_id TEXT PRIMARY KEY, cookie_id TEXT, item_id TEXT, buyer_id TEXT, sid TEXT, order_status TEXT, quantity TEXT, amount TEXT, is_rated INTEGER, is_red_flower INTEGER);
        CREATE TABLE data_card_reservations(id INTEGER PRIMARY KEY, card_id INTEGER, cookie_id TEXT, order_id TEXT, unit_index INTEGER, reserved_content TEXT, status TEXT);
        CREATE TABLE delivery_finalization_states(id INTEGER PRIMARY KEY, order_id TEXT, cookie_id TEXT, unit_index INTEGER, status TEXT, delivery_meta TEXT);
        ''')
        db.executemany('INSERT INTO users VALUES(?,?,?,?,?,?)', [(1,'seller-a','a@example.invalid','a'*64,1,0), (2,'seller-b','b@example.invalid','b'*64,1,0)])
        db.executemany('INSERT INTO cookies VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)', [
            ('account-a',1,'FAKE_COOKIE_A',0,'saved-user','FAKE_LOGIN_PASSWORD','keep remark','http','proxy.invalid',0,'u','FAKE_PROXY_PASS',0),
            ('account-b',2,'FAKE_COOKIE_B',10,'','','B','none','',0,'','',0)])
        db.execute("INSERT INTO cookie_status VALUES('account-a',0)")
        db.execute("INSERT INTO ai_reply_settings VALUES('account-a',1,'openai','FAKE_API_KEY','fixture-model','https://ai.invalid/v1',0)")
        db.execute("INSERT INTO keywords VALUES('account-a','hello','hi',NULL,'text',NULL)")
        db.execute("INSERT INTO default_replies VALUES('account-a',1,'',1)")
        db.execute("INSERT INTO default_reply_records VALUES(1,'account-a','chat-shared','2026-09-01 10:00:00')")
        db.execute("INSERT INTO item_replay VALUES(1,'account-a','item-a','exclusive')")
        db.executemany('INSERT INTO ai_conversations VALUES(?,?,?,?,?,?,?,?,?,?)', [
            (1,'account-a','chat-shared','buyer-a','item-a','user','hello','default',0,'2026-09-01 10:00:00'),
            (2,'account-b','chat-shared','buyer-b','item-b','user','B only','default',0,'2026-09-01 10:00:01')])
        db.execute("INSERT INTO chat_messages VALUES(1,'account-a','chat-shared','seller-a','manual promise',1,'manual','2026-09-01 10:01:00')")
        db.execute('INSERT INTO notification_channels VALUES(1,1,?,?,?,0)', ('fixture-channel','ding_talk','{"secret":"FAKE_NOTIFY_SECRET"}'))
        db.execute("INSERT INTO message_notifications VALUES(1,'account-a',1,0)")
        db.execute("INSERT INTO user_settings VALUES(1,1,'fixture_unknown_zero','0')")
        db.execute("INSERT INTO system_settings VALUES('fixture_unknown_secret','FAKE_SETTING_SECRET')")
        db.execute("INSERT INTO item_info VALUES(1,'account-a','item-a','fixture item','0','{}',0,0,'2026-09-01 10:00:00')")
        db.execute("INSERT INTO cards VALUES(1,1,'fixture cards','data','FAKE_FREE_CARD',1,0,NULL,NULL)")
        db.execute("INSERT INTO orders VALUES('order-a','account-a','item-a','buyer-a','chat-shared','pending','2','0',0,0)")
        db.execute("INSERT INTO data_card_reservations VALUES(1,1,'account-a','order-a',1,'FAKE_RESERVED_CARD','reserved')")
        db.execute("INSERT INTO delivery_finalization_states VALUES(1,'order-a','account-a',1,'sent','{}')")
    return path


def add_extended_source(path):
    import json
    sku = {'enabled': True, 'properties': [{'name': '颜色', 'values': [{'value': '红'}, {'value': '蓝'}], 'support_image': False}],
           'items': [{'values': ['红'], 'price': 2, 'quantity': 0}, {'values': ['蓝'], 'price': 3, 'quantity': 4}]}
    with sqlite3.connect(path) as db:
        db.executescript('''
        CREATE TABLE ai_config_presets(id INTEGER PRIMARY KEY,user_id INTEGER,preset_name TEXT,model_name TEXT,api_key TEXT,base_url TEXT,api_type TEXT);
        INSERT INTO ai_config_presets VALUES(1,1,'fixture preset','model','FAKE_KEY','https://ai.invalid/v1','openai');
        CREATE TABLE xy_personal_blacklist(id INTEGER PRIMARY KEY,user_id INTEGER,cookie_id TEXT,buyer_id TEXT,buyer_nick TEXT,item_id TEXT,reason TEXT,is_enabled INTEGER);
        INSERT INTO xy_personal_blacklist VALUES(1,1,'account-a','blocked-buyer','fixture',NULL,'fixture',1);
        CREATE TABLE xy_platform_blacklist(id INTEGER PRIMARY KEY,user_id INTEGER,buyer_id TEXT,buyer_nick TEXT);
        CREATE TABLE xy_message_filter_rules(id INTEGER PRIMARY KEY,user_id INTEGER,cookie_id TEXT,item_id TEXT,name TEXT,match_type TEXT,patterns TEXT,message_source TEXT,is_enabled INTEGER,action_skip_auto_reply INTEGER,action_skip_ai_reply INTEGER,action_pause_minutes INTEGER,action_notify INTEGER);
        INSERT INTO xy_message_filter_rules VALUES(1,1,'account-a',NULL,'fixture','exact','["ignore"]','user',1,1,0,0,0);
        CREATE TABLE notification_templates(id INTEGER PRIMARY KEY,type TEXT,template TEXT);
        INSERT INTO notification_templates VALUES(1,'message','{account_id}: {summary}');
        CREATE TABLE product_materials(id INTEGER PRIMARY KEY,user_id INTEGER,title TEXT,description TEXT,price REAL,images TEXT,delivery_method TEXT,postage REAL,can_self_pickup INTEGER,sku_config TEXT);
        CREATE TABLE publish_logs(id INTEGER PRIMARY KEY,user_id INTEGER,account_id TEXT,title TEXT,description TEXT,price TEXT,material_id INTEGER,batch_id TEXT,status TEXT,item_url TEXT,item_id TEXT,error_message TEXT);
        INSERT INTO publish_logs VALUES(1,1,'account-a','title','desc','2',1,'batch','publishing',NULL,NULL,NULL);
        ''')
        db.execute('INSERT INTO product_materials VALUES(1,1,?,?,?,?,?,?,?,?)', ('material','desc',2,'[]','包邮',0,0,json.dumps(sku)))
    return path
