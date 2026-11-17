import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from incident_sla import vault
from incident_sla.model import analyze

ROOT = Path(__file__).resolve().parents[1]

KEY_A = 'aa' * 32
KEY_B = 'bb' * 32
KEY_C = 'cc' * 32
KEY_WRONG = 'ab' * 32


def cli(*args, fault=None):
    env = None
    if fault is not None:
        env = dict(os.environ)
        env[vault.FAULT_ENV] = fault
    return subprocess.run(
        [sys.executable, '-m', 'incident_sla.cli', *[str(a) for a in args]],
        capture_output=True, text=True, env=env)


def base_payload():
    return {
        'targets': {'response_seconds': 300, 'resolution_seconds': 3600},
        'incidents': [
            {'id': 'I-1', 'opened': 1000, 'first_response': 1200,
             'resolved': 4000},
        ],
        'pauses': [{'incident': 'I-1', 'start': 1500, 'end': 1800}],
    }


def doc(doc_id, tenant, payload=None):
    return {'id': doc_id, 'tenant': tenant,
            'payload': payload if payload is not None else base_payload()}


def renamed_payload(new_incident_id):
    """A valid payload whose single incident (and its pause) is renamed."""
    payload = base_payload()
    payload['incidents'][0]['id'] = new_incident_id
    payload['pauses'][0]['incident'] = new_incident_id
    return payload


def canonical(payload):
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


class VaultCase(unittest.TestCase):
    """Shared fixture: a temp dir with a two-key keyring on disk."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.dir = Path(self.td.name)
        self.db = self.dir / 'vault.db'
        self.keyring = self.dir / 'keyring.json'
        self.write_keyring([('key-a', KEY_A), ('key-b', KEY_B)])

    def write_keyring(self, keys, name='keyring.json', raw=None):
        path = self.dir / name
        if raw is not None:
            path.write_text(raw)
        else:
            path.write_text(json.dumps(
                {'keys': [{'id': k, 'key_hex': h} for k, h in keys]}))
        return path

    def write_import(self, docs, name='import.json'):
        path = self.dir / name
        path.write_text(json.dumps({'documents': docs}))
        return path

    def init_vault(self, key_id='key-a', db=None, keyring=None):
        result = cli('--vault-init', '--db', db or self.db, '--keyring',
                     keyring or self.keyring, '--key-id', key_id)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def import_docs(self, docs, name='import.json', expect=0, **kw):
        path = self.write_import(docs, name)
        result = cli('--vault-import', path, '--db', kw.get('db', self.db),
                     '--keyring', kw.get('keyring', self.keyring),
                     fault=kw.get('fault'))
        self.assertEqual(result.returncode, expect, result.stderr)
        return result

    def query(self, tenant, output='out.json', expect=0, **kw):
        out = self.dir / output
        result = cli('--vault-query', tenant, '--db', kw.get('db', self.db),
                     '--keyring', kw.get('keyring', self.keyring),
                     '--output', out, fault=kw.get('fault'))
        self.assertEqual(result.returncode, expect, result.stderr)
        if expect == 0:
            return json.loads(out.read_bytes())
        return result

    def rekey(self, target, limit, expect=0, **kw):
        result = cli('--vault-rekey', target, '--db', kw.get('db', self.db),
                     '--keyring', kw.get('keyring', self.keyring),
                     '--limit', limit, fault=kw.get('fault'))
        self.assertEqual(result.returncode, expect, result.stderr)
        if expect == 0:
            report = json.loads(result.stdout)
            self.assertEqual(set(report), {'schema_version', 'vault_id',
                                           'active_key_id', 'migrated',
                                           'remaining', 'completed'})
            return report
        return result

    def sql(self, statement, args=(), db=None):
        conn = sqlite3.connect(str(db or self.db), isolation_level=None)
        try:
            return conn.execute(statement, args).fetchall()
        finally:
            conn.close()

    def doc_count(self):
        return self.sql('SELECT COUNT(*) FROM documents')[0][0]

    def doc_key_ids(self):
        return dict(self.sql('SELECT id, key_id FROM documents'))


class KeyringValidation(VaultCase):
    def bad_keyring(self, raw):
        path = self.write_keyring([], name='bad.json', raw=raw)
        result = cli('--vault-init', '--db', self.dir / 'v.db',
                     '--keyring', path, '--key-id', 'key-a')
        self.assertEqual(result.returncode, 2)
        return result

    def test_strict_shape(self):
        self.bad_keyring('{}')
        self.bad_keyring('{"keys":[],"extra":1}')
        self.bad_keyring('{"keys":[]}')
        self.bad_keyring('{"keys":{}}')
        self.bad_keyring('{"keys":[{"id":"key-a"}]}')
        self.bad_keyring('{"keys":[{"id":"key-a","key_hex":"%s","x":1}]}'
                         % KEY_A)
        self.bad_keyring('{"keys":[{"id":" ","key_hex":"%s"}]}' % KEY_A)
        self.bad_keyring('{"keys":[{"id":"key-a","key_hex":"%s"},'
                         '{"id":"key-a","key_hex":"%s"}]}' % (KEY_A, KEY_B))

    def test_key_hex_format(self):
        for bad in ('aa' * 31 + 'a',            # 63 chars
                    'aa' * 32 + 'aa',           # 66 chars
                    'AA' * 32,                  # uppercase
                    'zz' * 32,                  # not hex
                    12345, True, None):
            raw = json.dumps({'keys': [{'id': 'key-a', 'key_hex': bad}]})
            self.bad_keyring(raw)

    def test_errors_never_leak_key_hex(self):
        secret = 'cf' * 32
        raw = json.dumps({'keys': [{'id': 'key-a', 'key_hex': secret},
                                   {'id': 'key-b', 'key_hex': 'zz' * 32}]})
        result = self.bad_keyring(raw)
        self.assertNotIn(secret, result.stderr)
        self.assertNotIn(secret, result.stdout)
        self.assertNotIn('zzzz', result.stderr)

    def test_keyring_reread_each_command(self):
        # The keyring is re-read per command: a key added later is usable.
        self.write_keyring([('key-a', KEY_A)])
        self.init_vault('key-a')
        self.import_docs([doc('d1', 'acme')])
        self.write_keyring([('key-a', KEY_A), ('key-b', KEY_B)])
        report = self.rekey('key-b', 10)
        self.assertTrue(report['completed'])


class VaultInit(VaultCase):
    def test_init_report_and_persistence(self):
        report = self.init_vault('key-a')
        self.assertEqual(set(report),
                         {'schema_version', 'vault_id', 'active_key_id'})
        self.assertEqual(report['schema_version'], 5)
        self.assertEqual(report['active_key_id'], 'key-a')
        vault_id = report['vault_id']
        self.assertRegex(vault_id, r'^[0-9a-f]{32}$')
        # The identity persists and is reported by later commands.
        self.import_docs([doc('d1', 'acme')])
        self.assertEqual(self.query('acme')['vault_id'], vault_id)
        # A second vault gets a fresh random identity.
        other = self.init_vault('key-a', db=self.dir / 'other.db')
        self.assertNotEqual(other['vault_id'], vault_id)

    def test_init_only_creates_missing_db(self):
        self.init_vault()
        again = cli('--vault-init', '--db', self.db, '--keyring',
                    self.keyring, '--key-id', 'key-a')
        self.assertEqual(again.returncode, 2)

    def test_init_requires_known_key_id(self):
        result = cli('--vault-init', '--db', self.db, '--keyring',
                     self.keyring, '--key-id', 'ghost')
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.db.exists())

    def test_init_rejects_bad_key_id_and_paths(self):
        self.assertEqual(cli('--vault-init', '--db', self.db, '--keyring',
                             self.keyring, '--key-id', ' spaced ').returncode, 2)
        # keyring == db rejected; dangling db symlink rejected.
        self.assertEqual(cli('--vault-init', '--db', self.keyring, '--keyring',
                             self.keyring, '--key-id', 'key-a').returncode, 2)
        link = self.dir / 'link.db'
        link.symlink_to(self.dir / 'nowhere')
        self.assertEqual(cli('--vault-init', '--db', link, '--keyring',
                             self.keyring, '--key-id', 'key-a').returncode, 2)
        keyring_link = self.dir / 'keyring-link.json'
        keyring_link.symlink_to(self.keyring)
        self.assertEqual(cli('--vault-init', '--db', self.db, '--keyring',
                             keyring_link, '--key-id', 'key-a').returncode, 2)


class ImportContract(VaultCase):
    def setUp(self):
        super().setUp()
        self.init_vault('key-a')

    def import_expecting(self, docs, expect):
        result = self.import_docs(docs, expect=expect)
        return result

    def test_v1_lifecycle_contract_reused(self):
        # resolved without a response: rejected by v1, rejected here.
        bad = base_payload()
        bad['incidents'] = [{'id': 'I-1', 'opened': 10, 'first_response': None,
                             'resolved': 20}]
        self.import_expecting([doc('d1', 'acme', bad)], 2)
        # response preceding opened: rejected.
        bad = base_payload()
        bad['incidents'] = [{'id': 'I-1', 'opened': 10, 'first_response': 5,
                             'resolved': None}]
        self.import_expecting([doc('d1', 'acme', bad)], 2)
        # resolved before response: rejected.
        bad = base_payload()
        bad['incidents'] = [{'id': 'I-1', 'opened': 10, 'first_response': 15,
                             'resolved': 12}]
        self.import_expecting([doc('d1', 'acme', bad)], 2)
        # open and responded-unresolved incidents: accepted by v1.
        ok = base_payload()
        ok['incidents'] = [
            {'id': 'I-1', 'opened': 10, 'first_response': None,
             'resolved': None},
            {'id': 'I-2', 'opened': 10, 'first_response': 12,
             'resolved': None},
        ]
        self.import_expecting([doc('d1', 'acme', ok)], 0)
        self.assertEqual(self.doc_count(), 1)

    def test_v1_payload_field_and_value_rules(self):
        cases = []
        payload = base_payload()
        payload['unknown'] = 1
        cases.append(payload)                      # unknown top-level field
        payload = base_payload()
        del payload['pauses']
        cases.append(payload)                      # missing field
        payload = base_payload()
        payload['incidents'] = [
            {'id': 'I-1', 'opened': 1, 'first_response': 2, 'resolved': 3},
            {'id': 'I-1', 'opened': 1, 'first_response': 2, 'resolved': 3}]
        cases.append(payload)                      # duplicate incident id
        payload = base_payload()
        payload['pauses'] = [{'incident': 'ghost', 'start': 1, 'end': 2}]
        cases.append(payload)                      # unknown pause reference
        payload = base_payload()
        payload['pauses'] = [{'incident': 'I-1', 'start': 5, 'end': 5}]
        cases.append(payload)                      # empty pause
        payload = base_payload()
        payload['incidents'][0]['opened'] = True
        cases.append(payload)                      # bool as timestamp
        payload = base_payload()
        payload['incidents'][0]['extra'] = 1
        cases.append(payload)                      # unknown incident field
        for i, payload in enumerate(cases):
            self.import_expecting([doc('bad%d' % i, 'acme', payload)], 2)
        self.assertEqual(self.doc_count(), 0)

    def test_overlapping_pauses_accepted_and_measured_like_v1(self):
        payload = base_payload()
        payload['pauses'] = [
            {'incident': 'I-1', 'start': 1100, 'end': 1300},
            {'incident': 'I-1', 'start': 1200, 'end': 1600},
        ]
        self.import_expecting([doc('d1', 'acme', payload)], 0)
        report = self.query('acme')
        self.assertEqual(report['documents'][0]['sla'], analyze(payload))

    def test_batch_size_bounds(self):
        self.import_expecting([], 2)
        self.import_expecting([doc('d%d' % i, 'acme') for i in range(101)], 2)
        self.assertEqual(self.doc_count(), 0)
        self.import_expecting([doc('d%d' % i, 'acme') for i in range(100)], 0)
        self.assertEqual(self.doc_count(), 100)

    def test_document_shape_and_ident_rules(self):
        entry = doc('d1', 'acme')
        entry['extra'] = 1
        self.import_expecting([entry], 2)
        entry = doc('d1', 'acme')
        del entry['tenant']
        self.import_expecting([entry], 2)
        self.import_expecting([doc('d1', '  ')], 2)
        self.import_expecting([doc('d1', ' padded ')], 2)
        self.import_expecting([doc('', 'acme')], 2)
        self.assertEqual(self.doc_count(), 0)

    def test_duplicate_ids_roll_back_the_whole_batch(self):
        # Duplicate inside the batch: nothing is written.
        self.import_expecting([doc('d1', 'acme'), doc('d2', 'globex'),
                               doc('d1', 'acme')], 2)
        self.assertEqual(self.doc_count(), 0)
        self.import_expecting([doc('d1', 'acme')], 0)
        # Duplicate against the vault: the valid sibling is not written.
        self.import_expecting([doc('d2', 'acme'), doc('d1', 'acme')], 2)
        self.assertEqual(self.doc_count(), 1)
        # A bad payload anywhere rolls back the whole batch as well.
        bad = base_payload()
        bad['incidents'][0]['resolved'] = 1  # before response
        self.import_expecting([doc('d3', 'acme'), doc('d4', 'acme', bad)], 2)
        self.assertEqual(self.doc_count(), 1)

    def test_import_report_shape(self):
        result = self.import_docs([doc('d1', 'acme'), doc('d2', 'globex')])
        report = json.loads(result.stdout)
        self.assertEqual(set(report), {'schema_version', 'vault_id',
                                       'active_key_id', 'imported',
                                       'documents'})
        self.assertEqual((report['imported'], report['documents']), (2, 2))
        self.import_docs([doc('d3', 'acme')], name='import2.json')
        report = json.loads(self.import_docs(
            [doc('d4', 'acme')], name='import3.json').stdout)
        self.assertEqual((report['imported'], report['documents']), (1, 4))


class QueryBehaviour(VaultCase):
    def setUp(self):
        super().setUp()
        self.init_vault('key-a')
        self.payloads = {}
        docs = []
        for doc_id, tenant in [('d1', 'acme'), ('d2', 'acme'), ('d3', 'acme'),
                               ('d4', 'globex'), ('d5', 'Acme'),
                               ('d6', 'acme-eu')]:
            payload = renamed_payload('I-' + doc_id)
            self.payloads[doc_id] = payload
            docs.append(doc(doc_id, tenant, payload))
        self.import_docs(docs)

    def test_exact_complete_ordered_set(self):
        report = self.query('acme')
        self.assertEqual(set(report), {'schema_version', 'vault_id', 'tenant',
                                       'documents'})
        self.assertEqual(report['tenant'], 'acme')
        docs = report['documents']
        self.assertEqual([d['id'] for d in docs], ['d1', 'd2', 'd3'])
        for entry in docs:
            self.assertEqual(set(entry), {'id', 'tenant', 'payload',
                                          'payload_sha256', 'sla'})
            self.assertEqual(entry['tenant'], 'acme')
            payload = self.payloads[entry['id']]
            self.assertEqual(entry['payload'], payload)
            self.assertEqual(entry['payload_sha256'],
                             hashlib.sha256(canonical(payload)).hexdigest())
            self.assertEqual(entry['sla'], analyze(payload))

    def test_case_prefix_and_unicode_sensitivity(self):
        self.assertEqual([d['id'] for d in self.query('Acme')['documents']],
                         ['d5'])
        self.assertEqual([d['id'] for d in self.query('acme-eu')['documents']],
                         ['d6'])
        self.assertEqual(self.query('ACME')['documents'], [])
        self.assertEqual(self.query('acm')['documents'], [])
        self.assertEqual(self.query('acme-')['documents'], [])
        # Unicode lookalikes are distinct identifiers, never normalized.
        composed = doc('u1', 'café')
        decomposed = doc('u2', 'café')
        self.assertNotEqual(composed['tenant'], decomposed['tenant'])
        self.import_docs([composed, decomposed, doc('u3', 'cafe')],
                         name='uni.json')
        self.assertEqual([d['id'] for d in self.query('café')['documents']],
                         ['u1'])
        self.assertEqual([d['id'] for d in self.query('café')['documents']],
                         ['u2'])
        self.assertEqual([d['id'] for d in self.query('cafe')['documents']],
                         ['u3'])

    def test_unicode_payload_round_trip_without_normalization(self):
        payload = renamed_payload('事故-é')      # composed
        self.import_docs([doc('n1', 'acme', payload)], name='n1.json')
        payload2 = renamed_payload('事故-é')    # decomposed
        self.import_docs([doc('n2', 'acme', payload2)], name='n2.json')
        report = self.query('acme')
        by_id = {d['id']: d for d in report['documents']}
        self.assertEqual(by_id['n1']['payload']['incidents'][0]['id'], '事故-é')
        self.assertEqual(by_id['n2']['payload']['incidents'][0]['id'], '事故-é')
        self.assertNotEqual(by_id['n1']['payload_sha256'],
                            by_id['n2']['payload_sha256'])
        for doc_id, payload in (('n1', payload), ('n2', payload2)):
            self.assertEqual(by_id[doc_id]['payload_sha256'],
                             hashlib.sha256(canonical(payload)).hexdigest())

    def test_zero_hit_query_writes_empty_report(self):
        report = self.query('nobody')
        self.assertEqual(report['documents'], [])
        self.assertEqual(report['tenant'], 'nobody')

    def test_query_is_byte_stable(self):
        first = self.query('acme', 'q1.json')
        second = self.query('acme', 'q2.json')
        self.assertEqual((self.dir / 'q1.json').read_bytes(),
                         (self.dir / 'q2.json').read_bytes())
        self.assertEqual(first, second)

    def test_query_does_not_write_db(self):
        before = self.db.read_bytes()
        self.query('acme')
        self.assertEqual(self.db.read_bytes(), before)
        # A read-only database file still serves queries.
        os.chmod(self.db, 0o444)
        try:
            self.query('acme', 'ro.json')
        finally:
            os.chmod(self.db, 0o644)

    def test_failed_query_preserves_old_output(self):
        self.query('acme', 'keep.json')
        before = (self.dir / 'keep.json').read_bytes()
        self.sql('UPDATE documents SET record=? WHERE id=?',
                 (b'garbage-ciphertext', 'd1'))
        self.query('acme', 'keep.json', expect=2)
        self.assertEqual((self.dir / 'keep.json').read_bytes(), before)

    def test_query_requires_valid_tenant_ident(self):
        for bad in ('', '  ', ' padded ', 'a\x01b'):
            result = cli('--vault-query', bad, '--db', self.db, '--keyring',
                         self.keyring, '--output', self.dir / 'x.json')
            self.assertEqual(result.returncode, 2, repr(bad))


class IndexAndPlan(VaultCase):
    def setUp(self):
        super().setUp()
        self.init_vault('key-a')
        self.import_docs([doc('d1', 'acme'), doc('d2', 'globex'),
                          doc('d3', 'acme')])

    def test_query_plan_uses_the_keyed_index(self):
        conn = sqlite3.connect('file:%s?mode=ro' % self.db, uri=True)
        try:
            plan = conn.execute('EXPLAIN QUERY PLAN '
                                + vault.candidate_sql(2),
                                (b'x', b'y')).fetchall()
        finally:
            conn.close()
        details = ' | '.join(row[3] for row in plan)
        self.assertIn('tenant_index_by_token', details)
        self.assertIn('SEARCH', details)
        self.assertNotIn('SCAN ti', details)

    def test_index_token_is_not_a_plain_sha(self):
        token = self.sql('SELECT token FROM tenant_index WHERE doc_id=?',
                         ('d1',))[0][0]
        self.assertNotEqual(token, hashlib.sha256(b'acme').digest())
        self.assertEqual(len(token), 32)

    def test_non_hit_ciphertext_corruption_does_not_affect_query(self):
        # Corrupting a document of another tenant must not change the result.
        expected = self.query('acme')
        row = self.sql('SELECT record FROM documents WHERE id=?', ('d2',))[0][0]
        flipped = bytearray(row)
        flipped[5] ^= 1
        self.sql('UPDATE documents SET record=? WHERE id=?',
                 (bytes(flipped), 'd2'))
        report = self.query('acme', 'after.json')
        self.assertEqual(report, expected)
        # The corrupted row itself still fails authentication when queried.
        self.query('globex', 'g.json', expect=2)


class TamperDetection(VaultCase):
    def setUp(self):
        super().setUp()
        self.init_vault('key-a')
        self.import_docs([doc('d1', 'acme'), doc('d2', 'acme'),
                          doc('d3', 'globex')])

    def flip(self, column, doc_id, db=None):
        row = self.sql('SELECT %s FROM documents WHERE id=?' % column,
                       (doc_id,), db=db)[0][0]
        flipped = bytearray(row)
        flipped[len(flipped) // 2] ^= 1
        self.sql('UPDATE documents SET %s=? WHERE id=?' % column,
                 (bytes(flipped), doc_id), db=db)

    def test_bit_flips_fail_authentication_without_partial_report(self):
        for column in ('record', 'rec_nonce', 'wrapped_dek', 'wrap_nonce'):
            db = self.dir / ('v2-%s.db' % column)
            self.init_vault('key-a', db=db)
            path = self.write_import([doc('d1', 'acme')], 'single.json')
            self.assertEqual(cli('--vault-import', path, '--db', db,
                                 '--keyring', self.keyring).returncode, 0)
            self.flip(column, 'd1', db=db)
            out = self.dir / 'tamper.json'
            out.write_text('prior')
            result = cli('--vault-query', 'acme', '--db', db,
                         '--keyring', self.keyring, '--output', out)
            self.assertEqual(result.returncode, 2, column)
            self.assertEqual(out.read_text(), 'prior', column)

    def test_cross_document_ciphertext_swap_fails(self):
        rows = self.sql('SELECT id, record FROM documents ORDER BY id')
        self.sql('UPDATE documents SET record=? WHERE id=?',
                 (rows[2][1], rows[0][0]))
        self.sql('UPDATE documents SET record=? WHERE id=?',
                 (rows[0][1], rows[2][0]))
        self.query('acme', 'x.json', expect=2)
        self.query('globex', 'y.json', expect=2)

    def test_same_tenant_ciphertext_swap_fails(self):
        rows = self.sql(
            "SELECT id, record FROM documents WHERE id IN ('d1','d2') "
            'ORDER BY id')
        self.sql('UPDATE documents SET record=? WHERE id=?',
                 (rows[1][1], rows[0][0]))
        self.sql('UPDATE documents SET record=? WHERE id=?',
                 (rows[0][1], rows[1][0]))
        self.query('acme', 'x.json', expect=2)

    def test_cross_vault_transplant_fails(self):
        other = self.dir / 'other.db'
        self.init_vault('key-a', db=other)
        rows = self.sql('SELECT * FROM documents WHERE id=?', ('d3',))
        token = self.sql('SELECT token FROM tenant_index WHERE doc_id=?',
                         ('d3',))[0][0]
        conn = sqlite3.connect(str(other), isolation_level=None)
        try:
            conn.execute('INSERT INTO documents VALUES (?,?,?,?,?,?)', rows[0])
            conn.execute('INSERT INTO tenant_index VALUES (?,?)',
                         ('d3', token))
        finally:
            conn.close()
        result = cli('--vault-query', 'globex', '--db', other, '--keyring',
                     self.keyring, '--output', self.dir / 't.json')
        self.assertEqual(result.returncode, 2)
        # The source vault is unaffected.
        self.query('globex')

    def test_wrong_key_value_never_produces_garbage(self):
        self.write_keyring([('key-a', KEY_WRONG), ('key-b', KEY_B)],
                           name='wrong.json')
        self.query('acme', 'w.json', expect=2,
                   keyring=self.dir / 'wrong.json')


class RekeyRotation(VaultCase):
    def setUp(self):
        super().setUp()
        self.init_vault('key-a')

    def test_empty_vault_rotation_completes_immediately(self):
        report = self.rekey('key-b', 1)
        self.assertEqual(report, {'schema_version': 5,
                                  'vault_id': report['vault_id'],
                                  'active_key_id': 'key-b', 'migrated': 0,
                                  'remaining': 0, 'completed': True})
        self.import_docs([doc('d1', 'acme')])
        self.assertEqual(self.doc_key_ids(), {'d1': 'key-b'})

    def test_limit_bounds(self):
        for bad in (0, 101, -3):
            result = cli('--vault-rekey', 'key-b', '--db', self.db,
                         '--keyring', self.keyring, '--limit', bad)
            self.assertEqual(result.returncode, 2, bad)
        for bad in ('x', '1.5'):
            result = cli('--vault-rekey', 'key-b', '--db', self.db,
                         '--keyring', self.keyring, '--limit', bad)
            self.assertEqual(result.returncode, 2, bad)

    def test_target_must_be_in_keyring_and_ident(self):
        self.rekey('ghost', 1, expect=2)
        self.rekey(' spaced ', 1, expect=2)

    def test_rotation_walk_and_idempotent_completion(self):
        self.import_docs([doc('d1', 'acme'), doc('d2', 'acme'),
                          doc('d3', 'globex')])
        walk = [(1, 2, False), (1, 1, False), (1, 0, True), (0, 0, True)]
        for migrated, remaining, completed in walk:
            report = self.rekey('key-b', 1)
            self.assertEqual((report['migrated'], report['remaining'],
                              report['completed']),
                             (migrated, remaining, completed))
            self.assertEqual(report['active_key_id'], 'key-b')
        self.assertEqual(set(self.doc_key_ids().values()), {'key-b'})

    def test_other_target_rejected_while_rotation_incomplete(self):
        self.import_docs([doc('d1', 'acme'), doc('d2', 'acme')])
        self.write_keyring([('key-a', KEY_A), ('key-b', KEY_B),
                            ('key-c', KEY_C)])
        self.rekey('key-b', 1)
        self.rekey('key-c', 1, expect=2)
        self.rekey('key-a', 1, expect=2)  # even rotating back is rejected
        # The unfinished target still continues and completes.
        report = self.rekey('key-b', 1)
        self.assertTrue(report['completed'])
        # After completion a new rotation may start.
        report = self.rekey('key-c', 10)
        self.assertTrue(report['completed'])
        self.assertEqual(set(self.doc_key_ids().values()), {'key-c'})

    def test_mixed_version_query_and_import(self):
        payloads = {}
        docs = []
        for doc_id in ('d1', 'd2', 'd3'):
            payload = renamed_payload('I-' + doc_id)
            payloads[doc_id] = payload
            docs.append(doc(doc_id, 'acme', payload))
        self.import_docs(docs)
        self.rekey('key-b', 1)  # real mixed-version state
        self.assertEqual(set(self.doc_key_ids().values()), {'key-a', 'key-b'})
        # A new import lands on the new active key.
        payload4 = base_payload()
        payloads['d4'] = payload4
        self.import_docs([doc('d4', 'acme', payload4)], name='i2.json')
        self.assertEqual(self.doc_key_ids()['d4'], 'key-b')
        # Every document is returned exactly once, SLA intact.
        report = self.query('acme')
        self.assertEqual([d['id'] for d in report['documents']],
                         ['d1', 'd2', 'd3', 'd4'])
        for entry in report['documents']:
            self.assertEqual(entry['sla'], analyze(payloads[entry['id']]))
        # Byte-stable across the mixed state.
        again = self.query('acme', 'again.json')
        self.assertEqual((self.dir / 'out.json').read_bytes(),
                         (self.dir / 'again.json').read_bytes())
        self.assertEqual(report, again)

    def test_retirement_of_old_key(self):
        self.import_docs([doc('d1', 'acme'), doc('d2', 'globex')])
        self.rekey('key-b', 10)
        # Only the new key: everything still works.
        self.write_keyring([('key-b', KEY_B)], name='newonly.json')
        new_only = self.dir / 'newonly.json'
        self.query('acme', keyring=new_only)
        self.query('globex', 'g.json', keyring=new_only)
        self.import_docs([doc('d3', 'acme')], name='i2.json', keyring=new_only)
        # Only the old key: nothing works.
        self.write_keyring([('key-a', KEY_A)], name='oldonly.json')
        old_only = self.dir / 'oldonly.json'
        self.query('acme', 'o.json', expect=2, keyring=old_only)
        self.import_docs([doc('d4', 'acme')], name='i3.json', expect=2,
                         keyring=old_only)
        self.rekey('key-a', 1, expect=2, keyring=old_only)
        # The old key cannot unwrap any current wrapping, verified directly.
        vault_id = self.sql("SELECT value FROM meta WHERE key='vault_id'")[0][0]
        for doc_id, key_id, nonce, wrapped in self.sql(
                'SELECT id, key_id, wrap_nonce, wrapped_dek FROM documents'):
            self.assertEqual(key_id, 'key-b')
            with self.assertRaises(InvalidTag):
                AESGCM(bytes.fromhex(KEY_A)).decrypt(
                    nonce, wrapped, vault._wrap_aad(vault_id, doc_id, key_id))

    def test_missing_or_wrong_still_referenced_key_rejected_before_write(self):
        self.import_docs([doc('d1', 'acme'), doc('d2', 'acme')])
        self.rekey('key-b', 1)  # one document still on key-a
        # Old key removed mid-rotation: rekey and query both refuse.
        self.write_keyring([('key-b', KEY_B)], name='noold.json')
        self.rekey('key-b', 1, expect=2, keyring=self.dir / 'noold.json')
        self.query('acme', 'q.json', expect=2, keyring=self.dir / 'noold.json')
        # Wrong value under the old id: rejected before any write.
        self.write_keyring([('key-a', KEY_WRONG), ('key-b', KEY_B)],
                           name='wrong.json')
        self.rekey('key-b', 1, expect=2, keyring=self.dir / 'wrong.json')
        key_ids = self.doc_key_ids()
        self.assertEqual(sorted(key_ids.values()).count('key-a'), 1)
        self.assertEqual(sorted(key_ids.values()).count('key-b'), 1)
        # The intact keyring finishes the rotation cleanly.
        report = self.rekey('key-b', 1)
        self.assertTrue(report['completed'])


class CrashRecovery(VaultCase):
    def setUp(self):
        super().setUp()
        self.init_vault('key-a')
        self.import_docs([doc('d1', 'acme'), doc('d2', 'acme'),
                          doc('d3', 'globex')])

    def test_fault_between_ciphertext_and_index_rolls_back(self):
        before = self.query('acme')
        self.rekey('key-b', 2, expect=2, fault='rekey-mid-document')
        # The pre-commit set is still served; nothing was migrated.
        self.assertEqual(self.query('acme', 'after.json'), before)
        self.assertEqual(set(self.doc_key_ids().values()), {'key-a'})
        meta = dict(self.sql('SELECT key, value FROM meta'))
        self.assertEqual(meta['active_key_id'], 'key-a')
        # Restarting the same target continues to completion.
        self.rekey('key-b', 2)
        report = self.rekey('key-b', 2)
        self.assertTrue(report['completed'])
        self.assertEqual(self.query('acme', 'final.json'), before)
        # The full retirement chain: drop the old key, everything still
        # works; an old-key-only keyring can read nothing.
        self.assertEqual(set(dict(self.sql('SELECT * FROM key_checks'))),
                         {'key-b'})
        self.write_keyring([('key-b', KEY_B)], name='newonly.json')
        self.query('acme', 'n1.json', keyring=self.dir / 'newonly.json')
        self.query('globex', 'n2.json', keyring=self.dir / 'newonly.json')
        self.import_docs([doc('d4', 'acme')], name='n3.json',
                         keyring=self.dir / 'newonly.json')
        self.write_keyring([('key-a', KEY_A)], name='oldonly.json')
        self.query('acme', 'n4.json', expect=2,
                   keyring=self.dir / 'oldonly.json')

    def test_fault_before_rekey_commit_rolls_back(self):
        self.rekey('key-b', 2, expect=2, fault='rekey-before-commit')
        self.assertEqual(set(self.doc_key_ids().values()), {'key-a'})
        self.rekey('key-b', 2)
        self.assertTrue(self.rekey('key-b', 2)['completed'])

    def test_fault_after_rekey_commit_continues_without_duplicates(self):
        self.rekey('key-b', 1, expect=2, fault='rekey-after-commit')
        # The progress committed despite the failed exit.
        self.assertEqual(set(self.doc_key_ids().values()), {'key-a', 'key-b'})
        self.assertEqual(self.doc_count(), 3)
        report = self.rekey('key-b', 1)
        self.assertEqual((report['migrated'], report['remaining']), (1, 1))
        report = self.rekey('key-b', 1)
        self.assertTrue(report['completed'])
        self.assertEqual(self.doc_count(), 3)
        self.assertEqual([d['id'] for d in self.query('acme')['documents']],
                         ['d1', 'd2'])

    def test_import_faults_are_atomic(self):
        docs = [doc('n1', 'acme'), doc('n2', 'acme')]
        self.import_docs(docs, name='n.json', expect=2,
                         fault='import-before-commit')
        self.assertEqual(self.doc_count(), 3)
        self.import_docs(docs, name='n.json')
        self.assertEqual(self.doc_count(), 5)
        # A crash after the commit keeps the batch; a retry sees the
        # committed ids and rejects the duplicate instead of doubling.
        docs2 = [doc('m1', 'acme')]
        self.import_docs(docs2, name='m.json', expect=2,
                         fault='import-after-commit')
        self.assertEqual(self.doc_count(), 6)
        self.import_docs(docs2, name='m.json', expect=2)
        self.assertEqual(self.doc_count(), 6)
        self.assertIn('m1', [d['id'] for d in self.query('acme')['documents']])

    def test_init_fault_leaves_no_partial_db(self):
        db = self.dir / 'fresh.db'
        result = cli('--vault-init', '--db', db, '--keyring', self.keyring,
                     '--key-id', 'key-a', fault='init-before-commit')
        self.assertEqual(result.returncode, 2)
        self.assertFalse(db.exists())
        self.assertFalse(Path(str(db) + '-journal').exists())
        result = cli('--vault-init', '--db', db, '--keyring', self.keyring,
                     '--key-id', 'key-a')
        self.assertEqual(result.returncode, 0, result.stderr)


class SentinelScan(VaultCase):
    SENTINEL_TENANT = 'Tenant哨兵-Zq9'
    SENTINEL_INCIDENT = 'INC-哨兵-Xw7'

    def test_no_business_or_key_plaintext_in_db_files(self):
        self.init_vault('key-a')
        payload = renamed_payload(self.SENTINEL_INCIDENT)
        self.import_docs([doc('d1', self.SENTINEL_TENANT, payload),
                          doc('d2', self.SENTINEL_TENANT)])
        self.rekey('key-b', 1)
        self.import_docs([doc('d3', self.SENTINEL_TENANT)], name='i2.json')
        self.rekey('key-b', 10)
        needles = [self.SENTINEL_TENANT.encode('utf-8'),
                   self.SENTINEL_INCIDENT.encode('utf-8'),
                   KEY_A.encode('ascii'), KEY_B.encode('ascii'),
                   bytes.fromhex(KEY_A), bytes.fromhex(KEY_B)]
        scanned = 0
        for path in self.dir.iterdir():
            # Intentional JSON inputs/outputs may hold plaintext; the
            # database, its journals and any stray file may not.
            if path.suffix == '.json':
                continue
            data = path.read_bytes()
            scanned += 1
            for needle in needles:
                self.assertNotIn(needle, data, (path.name, needle[:12]))
        self.assertGreaterEqual(scanned, 1)  # the database itself

    def test_stdout_stderr_never_contain_key_hex(self):
        outputs = []
        result = cli('--vault-init', '--db', self.db, '--keyring',
                     self.keyring, '--key-id', 'key-a')
        outputs.append(result)
        outputs.append(self.import_docs([doc('d1', 'acme')]))
        outputs.append(cli('--vault-query', 'acme', '--db', self.db,
                           '--keyring', self.keyring, '--output',
                           self.dir / 'o.json'))
        outputs.append(cli('--vault-rekey', 'key-b', '--db', self.db,
                           '--keyring', self.keyring, '--limit', '1'))
        bad = self.write_keyring([], name='bad.json', raw='{"keys":[{"id":'
                                 '"key-a","key_hex":"zzzz"}]}')
        outputs.append(cli('--vault-query', 'acme', '--db', self.db,
                           '--keyring', bad, '--output',
                           self.dir / 'o2.json'))
        for result in outputs:
            for key_hex in (KEY_A, KEY_B):
                self.assertNotIn(key_hex, result.stdout)
                self.assertNotIn(key_hex, result.stderr)


class CliFlags(VaultCase):
    def test_flag_combination_matrix(self):
        self.init_vault('key-a')
        imp = self.write_import([doc('d1', 'acme')])
        out = self.dir / 'o.json'
        kr = self.keyring
        db = self.db
        combos = [
            ['--vault-init', '--db', db, '--keyring', kr],               # no key-id
            ['--vault-init', '--db', db, '--keyring', kr, '--key-id', 'key-a',
             '--output', out],
            ['--vault-init', '--db', db, '--keyring', kr, '--key-id', 'key-a',
             '--limit', '1'],
            ['--vault-init', '--keyring', kr, '--key-id', 'key-a'],      # no db
            ['--vault-init', '--db', db, '--key-id', 'key-a'],           # no keyring
            ['--vault-import', imp, '--keyring', kr],                    # no db
            ['--vault-import', imp, '--db', db],                         # no keyring
            ['--vault-import', imp, '--db', db, '--keyring', kr,
             '--output', out],
            ['--vault-import', imp, '--db', db, '--keyring', kr,
             '--key-id', 'key-a'],
            ['--vault-import', imp, '--db', db, '--keyring', kr,
             '--limit', '1'],
            ['--vault-query', 'acme', '--db', db, '--keyring', kr],      # no output
            ['--vault-query', 'acme', '--db', db, '--keyring', kr,
             '--output', out, '--limit', '1'],
            ['--vault-query', 'acme', '--db', db, '--keyring', kr,
             '--output', out, '--key-id', 'key-a'],
            ['--vault-rekey', 'key-b', '--db', db, '--keyring', kr],     # no limit
            ['--vault-rekey', 'key-b', '--db', db, '--keyring', kr,
             '--limit', '1', '--output', out],
            ['--vault-rekey', 'key-b', '--db', db, '--keyring', kr,
             '--limit', '1', '--key-id', 'key-a'],
            # vault options without a vault mode
            ['--source', ROOT / 'fixtures/input.json', '--output', out,
             '--keyring', kr],
            ['--source', ROOT / 'fixtures/input.json', '--output', out,
             '--limit', '1'],
            ['--credit-view', '1', '--db', db, '--output', out,
             '--key-id', 'key-a'],
            # vault modes are mutually exclusive with every other entry
            ['--vault-init', '--source', ROOT / 'fixtures/input.json',
             '--db', db, '--keyring', kr, '--key-id', 'key-a',
             '--output', out],
            ['--vault-import', imp, '--vault-query', 'acme', '--db', db,
             '--keyring', kr, '--output', out],
            ['--vault-rekey', 'key-b', '--credit-view', '1', '--db', db,
             '--keyring', kr, '--limit', '1', '--output', out],
        ]
        for args in combos:
            result = cli(*args)
            self.assertEqual(result.returncode, 2, args)

    def test_v1_to_v4_entries_still_work(self):
        out = self.dir / 'v1.json'
        result = cli('--source', ROOT / 'fixtures/input.json', '--output', out)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(out.read_bytes())['schema_version'], 1)
        out = self.dir / 'v2.json'
        result = cli('--plan-source', ROOT / 'fixtures/plan_input.json',
                     '--output', out)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = self.dir / 'v4.json'
        result = cli('--reconcile-source', ROOT / 'fixtures/reconcile_input.json',
                     '--output', out)
        self.assertEqual(result.returncode, 0, result.stderr)
        credit_db = self.dir / 'ledger.db'
        result = cli('--credit-init', ROOT / 'fixtures/credit_config.json',
                     '--db', credit_db)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_path_safety(self):
        self.init_vault('key-a')
        imp = self.write_import([doc('d1', 'acme')])
        out = self.dir / 'o.json'
        kr, db = self.keyring, self.db
        same_path = [
            ['--vault-import', imp, '--db', imp, '--keyring', kr],
            ['--vault-import', imp, '--db', db, '--keyring', imp],
            ['--vault-import', db, '--db', db, '--keyring', kr],
            ['--vault-query', 'acme', '--db', db, '--keyring', db,
             '--output', out],
            ['--vault-query', 'acme', '--db', db, '--keyring', kr,
             '--output', db],
            ['--vault-query', 'acme', '--db', db, '--keyring', out,
             '--output', out],
            ['--vault-rekey', 'key-b', '--db', kr, '--keyring', kr,
             '--limit', '1'],
        ]
        for args in same_path:
            self.assertEqual(cli(*args).returncode, 2, args)
        # symlinks are rejected for input, keyring, db and output
        link = self.dir / 'link'
        for target, args in [
            (imp, ['--vault-import', link, '--db', db, '--keyring', kr]),
            (kr, ['--vault-import', imp, '--db', db, '--keyring', link]),
            (db, ['--vault-import', imp, '--db', link, '--keyring', kr]),
            (out, ['--vault-query', 'acme', '--db', db, '--keyring', kr,
                   '--output', link]),
        ]:
            if link.exists() or link.is_symlink():
                link.unlink()
            if not target.exists() and target == out:
                target.write_text('x')
            link.symlink_to(target)
            self.assertEqual(cli(*args).returncode, 2, args)
        link.unlink()


class CliWalkthrough(VaultCase):
    """The fixture-based CLI scenario documented in the README."""

    def test_fixture_walkthrough_with_rotation(self):
        db = self.dir / 'vault.db'
        keyring = ROOT / 'fixtures/vault_keyring.json'
        result = cli('--vault-init', '--db', db, '--keyring', keyring,
                     '--key-id', 'key-2026-a')
        self.assertEqual(result.returncode, 0, result.stderr)
        vault_id = json.loads(result.stdout)['vault_id']
        result = cli('--vault-import', ROOT / 'fixtures/vault_import1.json',
                     '--db', db, '--keyring', keyring)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['imported'], 3)
        # Rotate with limit=1 into a real mixed-version state, then import.
        report = json.loads(cli('--vault-rekey', 'key-2026-b', '--db', db,
                                '--keyring', keyring, '--limit', '1').stdout)
        self.assertEqual((report['migrated'], report['remaining'],
                          report['completed']), (1, 2, False))
        result = cli('--vault-import', ROOT / 'fixtures/vault_import2.json',
                     '--db', db, '--keyring', keyring)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = self.dir / 'acme.json'
        result = cli('--vault-query', 'acme', '--db', db, '--keyring',
                     keyring, '--output', out)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(out.read_bytes())
        self.assertEqual(report['vault_id'], vault_id)
        self.assertEqual([d['id'] for d in report['documents']],
                         ['acme-2026-0001', 'acme-2026-0002', 'acme-2026-0003'])
        # Finish the rotation; the query result is unchanged.
        for _ in range(2):
            last = json.loads(cli('--vault-rekey', 'key-2026-b', '--db', db,
                                  '--keyring', keyring, '--limit', '2').stdout)
        self.assertTrue(last['completed'])
        out2 = self.dir / 'acme2.json'
        self.assertEqual(cli('--vault-query', 'acme', '--db', db, '--keyring',
                             keyring, '--output', out2).returncode, 0)
        self.assertEqual(out.read_bytes(), out2.read_bytes())


if __name__ == '__main__':
    unittest.main()
