import importlib.util
from pathlib import Path
import tempfile
import unittest
SPEC = importlib.util.spec_from_file_location('dev_reload', Path(__file__).parents[1]/'tools/dev_reload.py')
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)
class ReloadTests(unittest.TestCase):
    def test_code_changes_detected_but_runtime_writes_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); (root/'Start.py').write_text('one')
            first=m.snapshot(root)
            (root/'data').mkdir(); (root/'data'/'state.py').write_text('runtime')
            self.assertEqual(first,m.snapshot(root))
            (root/'Start.py').write_text('two')
            self.assertNotEqual(first,m.snapshot(root))
    def test_delete_and_new_code_detected(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); first=m.snapshot(root)
            (root/'new.py').write_text('pass'); second=m.snapshot(root)
            self.assertNotEqual(first,second)
            (root/'new.py').unlink(); self.assertEqual(first,m.snapshot(root))
if __name__=='__main__': unittest.main()
