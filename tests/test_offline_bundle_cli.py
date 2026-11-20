import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from neural_mesh.ledger import ProvenanceLedger

ROOT = Path(__file__).resolve().parents[1]


class OfflineBundleCLI(unittest.TestCase):
    def setUp(self):
        con = sqlite3.connect(':memory:')
        self.addCleanup(con.close)
        con.row_factory = sqlite3.Row
        ledger = ProvenanceLedger(lambda: con, clock=lambda: 1000.0)
        for op in ['add', 'update']:
            ledger.append(op, node_id='demo')
        self.bundle = ledger.export_bundle()

    def check(self, bundle, *args):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, 'record.json'); p.write_text(json.dumps(bundle))
            return subprocess.run([sys.executable, '-IS', str(ROOT/'scripts/verify_bundle.py'), str(p), *args], cwd=d, capture_output=True, text=True)

    def test_clean_export(self):
        result = self.check(self.bundle)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['checked'], 2)

    def test_changed_event(self):
        self.bundle['entries'][1]['op'] = 'forget'
        result = self.check(self.bundle)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)['broken_at'], 2)

    def test_metadata_and_incomplete_exports(self):
        for key, value in [('merkle_root', 'bad'), ('head', {}), ('chain_id', ''), ('entries', []), ('entries', self.bundle['entries'][:1]), ('scheme','unknown')]:
            with self.subTest(key=key, value=value):
                self.assertEqual(self.check({**self.bundle, key:value}).returncode, 1)

    def test_pin(self):
        self.assertEqual(self.check(self.bundle, '--expect-head', self.bundle['head']['entry_hash']).returncode, 0)
        self.assertEqual(self.check(self.bundle, '--expect-head', '0'*64).returncode, 1)
