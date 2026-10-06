import unittest
from fastapi.testclient import TestClient
import reply_server

class LocalEntryTests(unittest.TestCase):
    def test_old_accounts_bookmark_returns_login_entry(self):
        with TestClient(reply_server.app) as client:
            result = client.get('/accounts', follow_redirects=False)
        self.assertEqual(result.status_code, 307)
        self.assertEqual(result.headers['location'], '/')

if __name__ == '__main__': unittest.main()
