from support import *
import tempfile
class CollectionTests(unittest.TestCase):
    def test_multiservice_filter_missing_and_redacted(self):
        try: from app.services.admin_log_service import collect_logs
        except ImportError: self.fail('缺跨服务日志聚合')
        with tempfile.TemporaryDirectory() as root:
            base=Path(root); (base/'backend-web/logs').mkdir(parents=True);(base/'websocket/logs').mkdir(parents=True)
            (base/'backend-web/logs/app.log').write_text('TRACE-1 | INFO | submitted cookie=SAMPLE_SECRET\nTRACE-2 | INFO | skipped\n')
            (base/'websocket/logs/app.log').write_text('TRACE-1 | WARNING | unknown token=SAMPLE_SECRET\n')
            result=collect_logs(base,correlation='TRACE-1',limit=1)
            self.assertEqual(result['total'],2);self.assertEqual(len(result['logs']),1)
            self.assertEqual(result['services']['scheduler'],'missing')
            self.assertNotIn('SAMPLE_SECRET',str(result))
