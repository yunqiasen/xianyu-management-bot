"""The published specification is usable without deleted workstation scratch files."""
import hashlib
from pathlib import Path
import re
import unittest

from tools.release.checker import INPUT_HASHES, load_catalog

ROOT = Path(__file__).resolve().parents[2]


class SpecificationReferenceTests(unittest.TestCase):
    def test_specification_references_survive_scratch_cleanup(self):
        spec = ROOT / 'docs/specs/XYMB-SPEC-001.md'
        text = spec.read_text()
        self.assertNotIn('/.scratch/', text)
        for target in re.findall(r'\]\(([^)]+)\)', text):
            if target.startswith(('http:', 'https:', '/', '#')):
                continue
            self.assertTrue((spec.parent / target.split('#')[0]).is_file(), target)
        self.assertEqual(hashlib.sha256(spec.read_bytes()).hexdigest(), INPUT_HASHES['spec'])
        catalog = load_catalog(spec, ROOT / 'docs/provenance/feature-coverage-matrix.json',
                               ROOT / 'docs/provenance/local-patch-dispositions.json')
        self.assertIsNotNone(catalog)
