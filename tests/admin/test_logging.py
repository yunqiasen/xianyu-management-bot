from support import *
from common.utils import logging_utils
class LoggingTests(unittest.TestCase):
    def test_redaction_contract(self):
        self.assertTrue(callable(getattr(logging_utils,'redact_secrets',None)), '普通日志需要共享脱敏入口')
        value=logging_utils.redact_secrets({'cookie':'SAMPLE_A','nested':{'password':'SAMPLE_B'},'message':'token=SAMPLE_C; password=SAMPLE_D','order_id':'123'})
        self.assertNotIn('SAMPLE_',str(value)); self.assertEqual(value['order_id'],'123')
    def test_rotation_archives_instead_of_deleting_old_logs(self):
        import tempfile,os,time
        self.assertTrue(callable(getattr(logging_utils,'archive_expired_logs',None)))
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'runtime.old.log';path.write_text('unknown operation')
            os.utime(path,(time.time()-40*86400,)*2)
            logging_utils.archive_expired_logs([str(path)],30)
            self.assertFalse(path.exists());self.assertEqual((Path(root)/'archive'/path.name).read_text(),'unknown operation')
    def test_loguru_sink_hides_exception_and_header_secrets(self):
        from loguru import logger
        captured=[]
        sink=logger.add(lambda m:captured.append(str(m)),format='{message}')
        try:
            with logging_utils.log_context('fixture-trace'):
                logger.patch(logging_utils._redact_record).info('Cookie: session=SAMPLE_ONE; other=SAMPLE_TWO')
            self.assertNotIn('SAMPLE_', ''.join(captured))
        finally:logger.remove(sink)
