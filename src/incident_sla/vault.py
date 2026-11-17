"""Offline rotatable-key multi-tenant incident evidence vault (schema v5).

This application encrypts per-tenant v1 incident payloads into a single
SQLite vault, retrieves exactly one tenant's documents, and supports atomic
key rotation without any external key service.  It knows nothing about
responder planning, credit settlement or telemetry alignment; the only
business payload it stores is the v1 ``targets/incidents/pauses`` document,
validated by reusing :func:`incident_sla.model.analyze` itself so the
accept/reject contract cannot drift.

Data representation (why decryptability and index reachability survive
rotation):

* Each keyring key is a 256-bit key-encryption key (KEK), identified by an
  ``ident`` key id.  The keyring is re-read from its file on every command;
  key bytes are never written to the database, the reports or the logs.
* Every document gets a fresh random 256-bit document-encryption key (DEK).
  The record ``{"payload","payload_sha256","tenant"}`` is serialized as
  canonical JSON and encrypted with AES-256-GCM under the DEK; the DEK is
  wrapped (AES-256-GCM) under the active KEK.  Both encryptions bind the
  random vault identity and the document id as associated data, so
  ciphertexts cannot be swapped between documents or ported between vaults.
  Random 96-bit nonces follow the AES-GCM uniqueness requirement (NIST SP
  800-38D random-nonce construction; document counts here are bounded far
  below 2**32 invocations per key).
* The tenant equality index is ``HMAC-SHA256`` over the tenant name keyed by
  an index key derived from the KEK with HKDF-SHA256 (domain-separated info
  string).  It is irreversible and keyed, so neither a plain SHA nor
  deterministic plaintext encryption is involved; the token is the only
  tenant locator in the database.
* One document therefore has one logical version: ``(key_id, wrap_nonce,
  wrapped_dek)`` plus its index row ``(doc_id, token)``.  Rotation re-wraps
  the DEK under the target KEK and rebuilds the token under the target
  index key in the same SQLite transaction, so the key material and the
  index of a document always move together and a query can never locate a
  document through a stale index and then fail on a new ciphertext.
* A query computes the tenant token once per key version still referenced
  by the vault (at most the old and the new key during a rotation) and
  selects candidates through the SQLite index on those tokens; only
  candidate rows are decrypted, and each decrypted record's tenant is
  verified against the query before publication.
* ``key_checks`` stores, per key version ever active in this vault, an
  irreversible key-check value: ``HMAC-SHA256(kek, domain || vault_id ||
  key_id)``.  Every command verifies the check of each key it relies on,
  so a wrong key value is rejected explicitly instead of silently
  producing an empty or garbled result.  Checks of retired keys are
  deleted when a rotation completes.

Rotation state lives in ``meta`` (``active_key_id`` for new imports and
``rekey_target`` for the single in-progress target).  Every rekey call
commits its progress in one transaction; a crash before commit rolls back
cleanly and a crash after commit is simply continued by the next
same-target call, because candidates are always re-selected as
``key_id != target`` — no in-process cursor and no document is ever
re-created.  ``completed`` is reported only when no document, index row or
wrapped key depends on any non-target key any more.

Retirement boundary: after a completed rotation the old key alone cannot
recover any tenant or payload from the current logically valid records and
their reachable wrappings.  The guarantee does not cover old database
snapshots, SQLite free pages or stale journal/WAL remnants of superseded
ciphertexts, and no physical erasure is claimed by logical deletion.  The
vault assumes a single writer and does not defend against whole-database
rollback, row deletion, or leakage of document ids, counts and access
frequency.

``INCIDENT_SLA_VAULT_TEST_FAULT`` is a test-only hook: when the environment
variable equals one of the named fault steps, that step raises before the
corresponding commit (or right after it), which lets the test-suite inject
crashes inside real CLI subprocesses.  It performs no I/O otherwise.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .common import check_paths, ident, read, require, rows, save
from .model import analyze

SCHEMA_VERSION = 5
MAX_KEYS = 1000
MAX_IMPORT_DOCUMENTS = 100
MAX_REKEY_LIMIT = 100
VAULT_ID_BYTES = 16
DEK_BYTES = 32
NONCE_BYTES = 12  # 96-bit random nonces for AES-GCM
KEY_HEX = re.compile(r'^[0-9a-f]{64}$')

SCHEMA = """
CREATE TABLE meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE documents (
  id TEXT PRIMARY KEY,
  key_id TEXT NOT NULL,
  wrap_nonce BLOB NOT NULL,
  wrapped_dek BLOB NOT NULL,
  rec_nonce BLOB NOT NULL,
  record BLOB NOT NULL
);
CREATE TABLE tenant_index (
  doc_id TEXT PRIMARY KEY,
  token BLOB NOT NULL
);
CREATE INDEX tenant_index_by_token ON tenant_index(token);
CREATE TABLE key_checks (
  key_id TEXT PRIMARY KEY,
  kcv BLOB NOT NULL
);
"""

_INDEX_INFO = b'incident-sla/vault5/tenant-index'
_RECORD_AAD_PREFIX = b'incident-sla/vault5/record\x00'
_WRAP_AAD_PREFIX = b'incident-sla/vault5/wrap\x00'
_KEY_CHECK_MSG = b'incident-sla/vault5/key-check\x00'

FAULT_ENV = 'INCIDENT_SLA_VAULT_TEST_FAULT'


def _fault(step):
    """Test-only crash injection point (see module docstring)."""
    if os.environ.get(FAULT_ENV) == step:
        raise ValueError('injected test fault at step: ' + step)


# ---------------------------------------------------------------------------
# canonical JSON and keyring


def canonical_bytes(payload):
    """The exact canonical JSON bytes whose SHA-256 verifies a restoration."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def payload_digest(payload):
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def load_keyring(keyring_path):
    """Read and strictly validate ``{keys:[{id,key_hex}]}``; id -> 32 bytes."""
    data, _ = read(keyring_path)
    require(data, 'keys')
    keys = {}
    for entry in rows(data['keys'], MAX_KEYS):
        require(entry, 'id key_hex')
        key_id = ident(entry['id'])
        if key_id in keys:
            raise ValueError('duplicate key id in keyring: ' + key_id)
        key_hex = entry['key_hex']
        if not isinstance(key_hex, str) or not KEY_HEX.match(key_hex):
            raise ValueError(
                'key_hex must be exactly 64 lowercase hex characters for key: '
                + key_id)
        keys[key_id] = bytes.fromhex(key_hex)
    if not keys:
        raise ValueError('keyring must contain at least one key')
    return keys


# ---------------------------------------------------------------------------
# cryptographic data representation


def _index_key(kek):
    """Derive the per-KEK tenant-index key (domain separated from wrapping)."""
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                info=_INDEX_INFO).derive(kek)


def _tenant_token(kek, tenant):
    return hmac.new(_index_key(kek), tenant.encode('utf-8'),
                    hashlib.sha256).digest()


def _record_aad(vault_id, doc_id):
    return (_RECORD_AAD_PREFIX + vault_id.encode('ascii') + b'\x00'
            + doc_id.encode('utf-8'))


def _wrap_aad(vault_id, doc_id, key_id):
    return (_WRAP_AAD_PREFIX + vault_id.encode('ascii') + b'\x00'
            + doc_id.encode('utf-8') + b'\x00' + key_id.encode('utf-8'))


def _key_check(kek, vault_id, key_id):
    """Irreversible key-check value binding key, vault and key version."""
    return hmac.new(kek, _KEY_CHECK_MSG + vault_id.encode('ascii') + b'\x00'
                    + key_id.encode('utf-8'), hashlib.sha256).digest()


def _verify_key_checks(conn, keys, vault_id, key_ids):
    """Reject a missing or wrong key value before it is relied on."""
    for key_id in sorted(key_ids):
        row = conn.execute('SELECT kcv FROM key_checks WHERE key_id=?',
                           (key_id,)).fetchone()
        if row is None:
            raise ValueError(
                'vault has no key check for key version: ' + key_id)
        kek = keys.get(key_id)
        if kek is None:
            raise ValueError(
                'keyring is missing still-referenced key: ' + key_id)
        if not hmac.compare_digest(row[0], _key_check(kek, vault_id, key_id)):
            raise ValueError(
                'key value does not match vault records for key: ' + key_id)


def _seal_document(vault_id, active_key_id, kek, doc_id, tenant, payload):
    """Build the full logical version of one document under the active key."""
    digest = payload_digest(payload)
    record_plain = canonical_bytes(
        {'payload': payload, 'payload_sha256': digest, 'tenant': tenant})
    dek = secrets.token_bytes(DEK_BYTES)
    rec_nonce = secrets.token_bytes(NONCE_BYTES)
    record = AESGCM(dek).encrypt(rec_nonce, record_plain,
                                 _record_aad(vault_id, doc_id))
    wrap_nonce = secrets.token_bytes(NONCE_BYTES)
    wrapped_dek = AESGCM(kek).encrypt(wrap_nonce, dek,
                                      _wrap_aad(vault_id, doc_id,
                                                active_key_id))
    token = _tenant_token(kek, tenant)
    return {
        'id': doc_id,
        'key_id': active_key_id,
        'wrap_nonce': wrap_nonce,
        'wrapped_dek': wrapped_dek,
        'rec_nonce': rec_nonce,
        'record': record,
        'token': token,
    }


def _unwrap_dek(keys, vault_id, row):
    """Unwrap a document row's DEK; any mismatch is an authentication error."""
    key_id = row['key_id']
    kek = keys.get(key_id)
    if kek is None:
        raise ValueError(
            'keyring is missing still-referenced key: ' + key_id)
    try:
        return AESGCM(kek).decrypt(row['wrap_nonce'], row['wrapped_dek'],
                                   _wrap_aad(vault_id, row['id'], key_id))
    except InvalidTag:
        raise ValueError(
            'authentication failed unwrapping document: ' + row['id'])


def _open_record(vault_id, row, dek):
    """Decrypt and verify one record; returns (tenant, payload, digest)."""
    try:
        plain = AESGCM(dek).decrypt(row['rec_nonce'], row['record'],
                                    _record_aad(vault_id, row['id']))
    except InvalidTag:
        raise ValueError(
            'authentication failed decrypting document: ' + row['id'])
    try:
        record = json.loads(plain.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        raise ValueError('corrupt record encoding in document: ' + row['id'])
    require(record, 'payload payload_sha256 tenant')
    tenant = record['tenant']
    if not isinstance(tenant, str):
        raise ValueError('corrupt record tenant in document: ' + row['id'])
    digest = record['payload_sha256']
    if not isinstance(digest, str) or digest != payload_digest(record['payload']):
        raise ValueError('payload digest mismatch in document: ' + row['id'])
    return tenant, record['payload'], digest


# ---------------------------------------------------------------------------
# database helpers


def _connect(db_path, readonly=False):
    try:
        if readonly:
            uri = Path(db_path).absolute().as_uri() + '?mode=ro'
            return sqlite3.connect(uri, uri=True)
        return sqlite3.connect(str(db_path), isolation_level=None)
    except sqlite3.Error as error:
        raise ValueError('cannot open database: %s' % error)


def _rollback_quietly(conn):
    try:
        conn.execute('ROLLBACK')
    except sqlite3.Error:
        pass


def _remove_partial_db(db_path):
    for suffix in ('', '-journal', '-wal'):
        try:
            Path(str(db_path) + suffix).unlink()
        except OSError:
            pass


def _load_meta(conn):
    try:
        meta = dict(conn.execute('SELECT key, value FROM meta'))
    except sqlite3.Error:
        raise ValueError('not a schema v5 vault database')
    if meta.get('schema_version') != str(SCHEMA_VERSION):
        raise ValueError('not a schema v5 vault database')
    vault_id = meta.get('vault_id', '')
    if not re.match(r'^[0-9a-f]{32}$', vault_id):
        raise ValueError('vault database has an invalid vault identity')
    active = meta.get('active_key_id', '')
    pending = meta.get('rekey_target', '')
    try:
        ident(active)
        if pending:
            ident(pending)
    except ValueError:
        raise ValueError('vault database has invalid key version metadata')
    return {'vault_id': vault_id, 'active_key_id': active,
            'rekey_target': pending}


def _referenced_key_ids(conn):
    return {row[0] for row in conn.execute(
        'SELECT DISTINCT key_id FROM documents')}


def _require_referenced_keys(keys, referenced):
    for key_id in sorted(referenced):
        if key_id not in keys:
            raise ValueError(
                'keyring is missing still-referenced key: ' + key_id)


def candidate_sql(token_count):
    """Index-driven candidate selection over the keyed equality tokens."""
    marks = ','.join('?' * token_count)
    return ('SELECT d.id, d.key_id, d.wrap_nonce, d.wrapped_dek, '
            'd.rec_nonce, d.record FROM tenant_index ti '
            'JOIN documents d ON d.id = ti.doc_id '
            'WHERE ti.token IN (%s) ORDER BY d.id' % marks)


# ---------------------------------------------------------------------------
# vault commands


def init(db_path, keyring_path, key_id):
    """Create a new vault database with ``key_id`` as the active key."""
    key_id = ident(key_id)
    check_paths([keyring_path], db_path, None)
    db_path = Path(db_path)
    if db_path.exists() or db_path.is_symlink():
        raise ValueError('db already exists: init only creates a new database')
    keys = load_keyring(keyring_path)
    if key_id not in keys:
        raise ValueError('key id not present in keyring: ' + key_id)
    vault_id = secrets.token_hex(VAULT_ID_BYTES)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect(db_path)
    try:
        try:
            conn.executescript(SCHEMA)
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('INSERT INTO meta VALUES (?,?)',
                         ('schema_version', str(SCHEMA_VERSION)))
            conn.execute('INSERT INTO meta VALUES (?,?)', ('vault_id', vault_id))
            conn.execute('INSERT INTO meta VALUES (?,?)',
                         ('active_key_id', key_id))
            conn.execute('INSERT INTO meta VALUES (?,?)', ('rekey_target', ''))
            conn.execute('INSERT INTO key_checks VALUES (?,?)',
                         (key_id, _key_check(keys[key_id], vault_id, key_id)))
            _fault('init-before-commit')
            conn.execute('COMMIT')
        except BaseException:
            _rollback_quietly(conn)
            raise
    except sqlite3.Error as error:
        _remove_partial_db(db_path)
        raise ValueError('database error during init: %s' % error)
    except BaseException:
        _remove_partial_db(db_path)
        raise
    finally:
        conn.close()
    return {'schema_version': SCHEMA_VERSION, 'vault_id': vault_id,
            'active_key_id': key_id}


def validate_import(data):
    """Validate the strict import shape; returns normalized document rows."""
    require(data, 'documents')
    documents = rows(data['documents'], MAX_IMPORT_DOCUMENTS)
    if not documents:
        raise ValueError('documents must contain 1..%d entries'
                         % MAX_IMPORT_DOCUMENTS)
    parsed = []
    seen = set()
    for entry in documents:
        require(entry, 'id tenant payload')
        doc_id = ident(entry['id'])
        tenant = ident(entry['tenant'])
        if doc_id in seen:
            raise ValueError('duplicate document id in batch: ' + doc_id)
        seen.add(doc_id)
        payload = entry['payload']
        analyze(payload)  # the v1 accept/reject contract, reused verbatim
        parsed.append({'id': doc_id, 'tenant': tenant, 'payload': payload})
    return parsed


def import_documents(import_path, db_path, keyring_path):
    """Validate one batch, then atomically encrypt and store all of it."""
    check_paths([import_path, keyring_path], db_path, None)
    db_path = Path(db_path)
    if not db_path.exists():
        raise ValueError('db does not exist: run --vault-init first')
    data, _ = read(import_path)
    parsed = validate_import(data)
    keys = load_keyring(keyring_path)
    conn = _connect(db_path)
    try:
        try:
            conn.execute('BEGIN IMMEDIATE')
            meta = _load_meta(conn)
            active = meta['active_key_id']
            if active not in keys:
                raise ValueError(
                    'active key id not present in keyring: ' + active)
            vault_id = meta['vault_id']
            _verify_key_checks(conn, keys, vault_id, {active})
            existing = {row[0] for row in conn.execute(
                'SELECT id FROM documents WHERE id IN (%s)'
                % ','.join('?' * len(parsed)),
                [doc['id'] for doc in parsed])}
            if existing:
                raise ValueError(
                    'document id already in vault: ' + sorted(existing)[0])
            kek = keys[active]
            for doc in parsed:
                sealed = _seal_document(vault_id, active, kek, doc['id'],
                                        doc['tenant'], doc['payload'])
                conn.execute(
                    'INSERT INTO documents VALUES (?,?,?,?,?,?)',
                    (sealed['id'], sealed['key_id'], sealed['wrap_nonce'],
                     sealed['wrapped_dek'], sealed['rec_nonce'],
                     sealed['record']))
                conn.execute('INSERT INTO tenant_index VALUES (?,?)',
                             (sealed['id'], sealed['token']))
            _fault('import-before-commit')
            conn.execute('COMMIT')
            _fault('import-after-commit')
            total = conn.execute('SELECT COUNT(*) FROM documents').fetchone()[0]
        except BaseException:
            _rollback_quietly(conn)
            raise
    except sqlite3.Error as error:
        raise ValueError('database error during import: %s' % error)
    finally:
        conn.close()
    return {'schema_version': SCHEMA_VERSION, 'vault_id': meta['vault_id'],
            'active_key_id': meta['active_key_id'],
            'imported': len(parsed), 'documents': total}


def query(tenant, db_path, keyring_path, output_path):
    """Export every document of exactly one tenant, ordered by id."""
    tenant = ident(tenant)
    check_paths([keyring_path], db_path, output_path)
    db_path = Path(db_path)
    if not db_path.exists():
        raise ValueError('db does not exist: run --vault-init first')
    keys = load_keyring(keyring_path)
    conn = _connect(db_path, readonly=True)
    try:
        try:
            meta = _load_meta(conn)
            vault_id = meta['vault_id']
            referenced = _referenced_key_ids(conn)
            _require_referenced_keys(keys, referenced)
            _verify_key_checks(conn, keys, vault_id, referenced)
            tokens = sorted({_tenant_token(keys[key_id], tenant)
                             for key_id in referenced})
            documents = []
            if tokens:
                rows_found = conn.execute(
                    candidate_sql(len(tokens)), tokens).fetchall()
                for row in rows_found:
                    record_row = {'id': row[0], 'key_id': row[1],
                                  'wrap_nonce': row[2], 'wrapped_dek': row[3],
                                  'rec_nonce': row[4], 'record': row[5]}
                    dek = _unwrap_dek(keys, vault_id, record_row)
                    record_tenant, payload, digest = _open_record(
                        vault_id, record_row, dek)
                    if record_tenant != tenant:
                        raise ValueError(
                            'tenant mismatch after decryption in document: '
                            + row[0])
                    documents.append({
                        'id': row[0], 'tenant': record_tenant,
                        'payload': payload, 'payload_sha256': digest,
                        'sla': analyze(payload)})
        except sqlite3.Error as error:
            raise ValueError('database error during query: %s' % error)
    finally:
        conn.close()
    report = {'schema_version': SCHEMA_VERSION, 'vault_id': vault_id,
              'tenant': tenant, 'documents': documents}
    save(output_path, report)
    return report


def rekey(target, db_path, keyring_path, limit):
    """Rotate the active key to ``target``, migrating at most ``limit`` docs."""
    target = ident(target)
    if type(limit) is not int or not 1 <= limit <= MAX_REKEY_LIMIT:
        raise ValueError('limit must be an integer in 1..%d' % MAX_REKEY_LIMIT)
    check_paths([keyring_path], db_path, None)
    db_path = Path(db_path)
    if not db_path.exists():
        raise ValueError('db does not exist: run --vault-init first')
    keys = load_keyring(keyring_path)
    if target not in keys:
        raise ValueError('rekey target not present in keyring: ' + target)
    conn = _connect(db_path)
    try:
        try:
            conn.execute('BEGIN IMMEDIATE')
            meta = _load_meta(conn)
            vault_id = meta['vault_id']
            active = meta['active_key_id']
            pending = meta['rekey_target']
            if pending and pending != target:
                raise ValueError(
                    'rotation to %s is incomplete; finish it before targeting '
                    '%s' % (pending, target))
            referenced = _referenced_key_ids(conn)
            _require_referenced_keys(keys, referenced)
            _verify_key_checks(conn, keys, vault_id, referenced)
            if target not in referenced:
                # First contact with the target in this vault: register its
                # check; a re-registered key id is verified against the
                # stored check so a changed value is rejected before writes.
                row = conn.execute('SELECT kcv FROM key_checks WHERE key_id=?',
                                   (target,)).fetchone()
                if row is None:
                    conn.execute('INSERT INTO key_checks VALUES (?,?)',
                                 (target, _key_check(keys[target], vault_id,
                                                     target)))
                elif not hmac.compare_digest(
                        row[0], _key_check(keys[target], vault_id, target)):
                    raise ValueError(
                        'key value does not match vault records for key: '
                        + target)
            candidates = conn.execute(
                'SELECT id, key_id, wrap_nonce, wrapped_dek, rec_nonce, record '
                'FROM documents WHERE key_id != ? ORDER BY id LIMIT ?',
                (target, limit)).fetchall()
            # All cryptography for this batch runs before any write, so a
            # missing or wrong key value rejects the call before the
            # database is touched.
            planned = []
            for row in candidates:
                record_row = {'id': row[0], 'key_id': row[1],
                              'wrap_nonce': row[2], 'wrapped_dek': row[3],
                              'rec_nonce': row[4], 'record': row[5]}
                dek = _unwrap_dek(keys, vault_id, record_row)
                record_tenant, _, _ = _open_record(vault_id, record_row, dek)
                wrap_nonce = secrets.token_bytes(NONCE_BYTES)
                wrapped_dek = AESGCM(keys[target]).encrypt(
                    wrap_nonce, dek, _wrap_aad(vault_id, row[0], target))
                token = _tenant_token(keys[target], record_tenant)
                planned.append((row[0], wrap_nonce, wrapped_dek, token))
            migrated = len(planned)
            if not pending and target != active:
                conn.execute(
                    "UPDATE meta SET value=? WHERE key='active_key_id'",
                    (target,))
                conn.execute(
                    "UPDATE meta SET value=? WHERE key='rekey_target'",
                    (target,))
            for doc_id, wrap_nonce, wrapped_dek, token in planned:
                conn.execute(
                    'UPDATE documents SET key_id=?, wrap_nonce=?, '
                    'wrapped_dek=? WHERE id=?',
                    (target, wrap_nonce, wrapped_dek, doc_id))
                _fault('rekey-mid-document')
                conn.execute(
                    'UPDATE tenant_index SET token=? WHERE doc_id=?',
                    (token, doc_id))
            remaining = conn.execute(
                'SELECT COUNT(*) FROM documents WHERE key_id != ?',
                (target,)).fetchone()[0]
            if remaining == 0:
                # Nothing depends on any non-target key any more; the meta
                # value here is either '' or this call's target.  Retired
                # key checks are dropped so no old-key material stays
                # logically reachable; the active key always keeps its row.
                conn.execute(
                    "UPDATE meta SET value='' WHERE key='rekey_target'")
                conn.execute('DELETE FROM key_checks WHERE key_id != ?',
                             (target,))
            _fault('rekey-before-commit')
            conn.execute('COMMIT')
            _fault('rekey-after-commit')
        except BaseException:
            _rollback_quietly(conn)
            raise
    except sqlite3.Error as error:
        raise ValueError('database error during rekey: %s' % error)
    finally:
        conn.close()
    completed = remaining == 0
    return {'schema_version': SCHEMA_VERSION, 'vault_id': vault_id,
            'active_key_id': target, 'migrated': migrated,
            'remaining': remaining, 'completed': completed}
