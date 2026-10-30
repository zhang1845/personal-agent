import base64
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
import zlib

from personal_agent.cloudbase_store import (
    CloudBaseClient, MAX_RAW, SnapshotConflict, SnapshotWriteUnknown,
    clear_remote, configure_remote, connect, remote_document, _decode, _encode,
    _NoRedirect,
)


class MemoryClient:
    def __init__(self):
        self.rows = {}
        self.lock = threading.Lock()
        self.writes = 0
        self.unknown = False

    def read(self, name):
        with self.lock:
            return self.rows.get(name, (0, None))

    def compare_swap(self, name, expected, raw):
        with self.lock:
            version, _ = self.rows.get(name, (0, None))
            if expected != version:
                raise SnapshotConflict()
            self.rows[name] = (version + 1, raw)
            self.writes += 1
            if self.unknown:
                raise SnapshotWriteUnknown()
            return version + 1


class FakeResponse:
    status = 200
    def __init__(self, value):
        self.raw = json.dumps(value).encode()
    def read(self, limit):
        return self.raw[:limit]
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass


class FakeOpener:
    def __init__(self, value=None, error=None):
        self.value, self.error = value, error
        self.requests = []
    def open(self, request, timeout):
        self.requests.append(request)
        if self.error:
            raise self.error
        return FakeResponse(self.value)


class SnapshotStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.client = MemoryClient()
        configure_remote(self.root, self.client)
        self.path = self.root / 'assistant.sqlite3'

    def tearDown(self):
        clear_remote(self.root)
        self.directory.cleanup()

    def seed(self):
        with connect(self.path) as c:
            c.executescript('CREATE TABLE entries(id INTEGER PRIMARY KEY AUTOINCREMENT,value TEXT);')
            c.execute('BEGIN IMMEDIATE')
            c.execute('INSERT INTO entries(value) VALUES(?)', ('你好，Daynest',))
        c.close()

    def test_ddl_rows_lastrowid_and_reopen_do_not_use_local_file(self):
        self.seed()
        with connect(self.path) as c:
            c.row_factory = sqlite3.Row
            row = c.execute('SELECT * FROM entries').fetchone()
            self.assertEqual(row['value'], '你好，Daynest')
            self.assertEqual(row[0], 1)
            self.assertEqual(c.execute('INSERT INTO entries(value) VALUES(:value)', {'value':'第二条'}).lastrowid, 2)
        c.close()
        clear_remote(self.root)
        configure_remote(self.root, self.client)
        with connect(self.path) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM entries').fetchone()[0], 2)
        c.close()
        self.assertFalse(self.path.exists())

    def test_read_only_transactions_never_write(self):
        self.seed()
        before = self.client.writes
        with connect(self.path, isolation_level=None) as c:
            c.execute('BEGIN')
            self.assertEqual(c.execute('SELECT value FROM entries').fetchone()[0], '你好，Daynest')
        c.close()
        self.assertEqual(self.client.writes, before)

    def test_rollback_discards_uncommitted_changes(self):
        self.seed()
        before = self.client.writes
        with self.assertRaisesRegex(ValueError, 'cancel'):
            with connect(self.path) as c:
                c.execute('INSERT INTO entries(value) VALUES(?)', ('discard',))
                raise ValueError('cancel')
        c.close()
        with connect(self.path) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM entries').fetchone()[0], 1)
        c.close()
        self.assertEqual(self.client.writes, before)

    def test_two_connections_with_same_revision_only_one_commits(self):
        self.seed()
        first, second = connect(self.path), connect(self.path)
        try:
            first.execute('INSERT INTO entries(value) VALUES(?)', ('winner',))
            second.execute('INSERT INTO entries(value) VALUES(?)', ('loser',))
            first.commit()
            with self.assertRaises(SnapshotConflict):
                second.commit()
            with self.assertRaises(SnapshotWriteUnknown):
                second.commit()
        finally:
            first.close(); second.close()
        with connect(self.path) as c:
            values = [r[0] for r in c.execute('SELECT value FROM entries ORDER BY id')]
        c.close()
        self.assertEqual(values, ['你好，Daynest', 'winner'])

    def test_lost_ack_is_unknown_and_never_replayed(self):
        self.seed()
        self.client.unknown = True
        c = connect(self.path)
        c.execute('INSERT INTO entries(value) VALUES(?)', ('may already exist',))
        before = self.client.writes
        with self.assertRaises(SnapshotWriteUnknown):
            c.commit()
        with self.assertRaises(SnapshotWriteUnknown):
            c.commit()
        self.assertEqual(self.client.writes, before + 1)
        c.close()
        self.client.unknown = False
        with connect(self.path) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM entries').fetchone()[0], 2)
        c.close()

    def test_close_does_not_implicitly_acknowledge_write(self):
        self.seed()
        c = connect(self.path)
        c.execute('INSERT INTO entries(value) VALUES(?)', ('never committed',))
        c.close()
        with connect(self.path) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM entries').fetchone()[0], 1)
        c.close()

    def test_corrupt_snapshot_fails_closed(self):
        self.client.rows['assistant.sqlite3'] = (1, b'corrupt sqlite image')
        with self.assertRaisesRegex(sqlite3.OperationalError, '未载入空白数据'):
            connect(self.path)
        self.assertFalse(self.path.exists())

    def test_cloud_path_allowlist_and_native_fallback(self):
        for name in ('unknown.sqlite3', 'cloud-lease.sqlite3', 'sub/assistant.sqlite3'):
            with self.assertRaises(sqlite3.OperationalError):
                connect(self.root / name)
        with tempfile.TemporaryDirectory() as native:
            p = Path(native) / 'native.sqlite3'
            with connect(p) as c:
                c.execute('CREATE TABLE native(value)')
            c.close()
            self.assertTrue(p.exists())

    def test_symlink_cannot_escape_to_native_storage(self):
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / 'native.sqlite3'
            try:
                self.path.symlink_to(target)
            except (OSError, NotImplementedError):
                self.skipTest('symlinks unavailable on this host')
            with self.assertRaises(sqlite3.OperationalError):
                connect(self.path)
            self.assertFalse(target.exists())

    def test_private_document_cas_unicode_and_size(self):
        doc = remote_document(self.root / 'model-api.json')
        self.assertEqual(doc.read(), (0,None))
        revision = doc.compare_swap(0, {'api_key':'fake-test-key', 'model':'测试', 'version':2})
        self.assertEqual(revision, 1)
        self.assertEqual(doc.read()[1]['model'], '测试')
        with self.assertRaises(SnapshotConflict):
            doc.compare_swap(0, {'version':3})
        with self.assertRaises(sqlite3.OperationalError):
            doc.compare_swap(1, {'too_large':'x' * 65536})
        self.assertFalse((self.root / 'model-api.json').exists())


class EncodingTests(unittest.TestCase):
    def test_roundtrip_and_checksum_validation(self):
        raw = '中文资料'.encode() * 100
        payload, digest = _encode(raw)
        self.assertEqual(_decode(payload, digest), raw)
        with self.assertRaises(sqlite3.OperationalError):
            _decode(payload, '0' * 64)

    def test_bomb_malformed_trailing_and_maximum_bytes(self):
        bomb = base64.b64encode(zlib.compress(b'x' * (MAX_RAW + 1))).decode()
        with self.assertRaises(sqlite3.OperationalError):
            _decode(bomb, hashlib.sha256(b'x').hexdigest())
        for payload in ('!!!!', base64.b64encode(zlib.compress(b'data') + b'extra').decode()):
            with self.assertRaises(sqlite3.OperationalError):
                _decode(payload, hashlib.sha256(b'data').hexdigest())
        with patch('personal_agent.cloudbase_store.MAX_RAW', 8):
            with self.assertRaises(sqlite3.OperationalError):
                _encode(b'123456789')


class ClientTests(unittest.TestCase):
    OWNER = 'A' * 43

    def client(self, opener):
        return CloudBaseClient('example-env', 'test-secret-never-log-123456789', opener=opener)

    def test_backend_auth_fixed_origin_and_owner(self):
        opener = FakeOpener({'version':0,'payload':None,'digest':None})
        client = self.client(opener)
        with self.assertRaises(sqlite3.OperationalError):
            client.read('assistant.sqlite3')
        self.assertEqual(len(opener.requests), 0)
        client.owner = self.OWNER
        self.assertEqual(client.read('assistant.sqlite3'), (0,None))
        request = opener.requests[0]
        self.assertEqual(request.full_url, 'https://example-env.api.tcloudbasegateway.com/v1/rdb/rest/rpc/daynest_state_read')
        self.assertEqual(request.get_header('Authorization'), 'Bearer test-secret-never-log-123456789')
        self.assertIsNone(request.get_header('Apikey'))
        self.assertEqual(json.loads(request.data)['p_owner'], self.OWNER)

    def test_bearer_provider_is_used_for_each_real_rpc(self):
        opener = FakeOpener(True)
        calls = []
        def bearer():
            calls.append(len(calls) + 1)
            return 'temporary-test-token-' + str(calls[-1])
        client = CloudBaseClient('example-env', bearer_provider=bearer, opener=opener)
        self.assertTrue(client.check(self.OWNER))
        self.assertTrue(client.check(self.OWNER))
        self.assertEqual(calls, [1,2])
        self.assertEqual(opener.requests[0].get_header('Authorization'), 'Bearer temporary-test-token-1')
        self.assertEqual(opener.requests[1].get_header('Authorization'), 'Bearer temporary-test-token-2')
        self.assertIsNone(client._key)

    def test_identity_requires_exactly_one_valid_source(self):
        for kwargs in ({}, {'api_key':'x'*32, 'bearer_provider':lambda:'y'*32},
                       {'bearer_provider':'not-callable'}, {'api_key':''}):
            with self.assertRaises(ValueError):
                CloudBaseClient('example-env', **kwargs)

    def test_identity_failure_or_invalid_token_sends_no_rpc(self):
        def failed():
            raise RuntimeError('provider credential must never be disclosed')
        for provider in (failed, lambda:None, lambda:'bad\nheader', lambda:'short'):
            opener = FakeOpener(True)
            client = CloudBaseClient('example-env', bearer_provider=provider, opener=opener)
            with self.assertRaises(sqlite3.OperationalError) as caught:
                client.claim(self.OWNER)
            self.assertEqual(opener.requests, [])
            self.assertNotIn('credential', str(caught.exception))
            self.assertNotIn('header', str(caught.exception))
            self.assertNotIn('provider', str(caught.exception))

    def test_redirect_never_forwards_bearer(self):
        opener = FakeOpener(error=HTTPError('https://example-env.api.tcloudbasegateway.com',302,
                                          'redirect',{'Location':'https://elsewhere.invalid'},io.BytesIO(b'')))
        client = self.client(opener)
        with self.assertRaises(sqlite3.OperationalError):
            client.check(self.OWNER)
        self.assertEqual(len(opener.requests), 1)
        self.assertIsNone(_NoRedirect().redirect_request(opener.requests[0], None, 302, 'redirect',
                                                        {}, 'https://elsewhere.invalid'))

    def test_claim_check_release_and_validation(self):
        opener = FakeOpener(True)
        client = self.client(opener)
        self.assertTrue(client.claim(self.OWNER))
        self.assertEqual(client.owner, self.OWNER)
        self.assertTrue(client.check())
        client.complete_init()
        self.assertTrue(opener.requests[-1].full_url.endswith('/daynest_lease_complete_init'))
        self.assertEqual(json.loads(opener.requests[-1].data), {'p_owner':self.OWNER})
        self.assertTrue(client.release(self.OWNER))
        self.assertIsNone(client.owner)
        for value in ('https://evil.test', 'evil/../host', 'bad host'):
            with self.assertRaises(ValueError):
                CloudBaseClient(value, 'x' * 32)

    def test_request_cache_updated_on_cas_and_cleared_between_claims(self):
        opener = FakeOpener(True)
        client = self.client(opener)
        self.assertTrue(client.claim(self.OWNER))
        opener.value = {'version':0,'payload':None,'digest':None}
        self.assertEqual(client.read('assistant.sqlite3'), (0,None))
        count = len(opener.requests)
        self.assertEqual(client.read('assistant.sqlite3'), (0,None))
        self.assertEqual(len(opener.requests), count)
        opener.value = {'ok':True,'version':1}
        client.compare_swap('assistant.sqlite3', 0, b'committed')
        count = len(opener.requests)
        self.assertEqual(client.read('assistant.sqlite3'), (1,b'committed'))
        self.assertEqual(len(opener.requests), count)
        opener.value = True
        self.assertTrue(client.release(self.OWNER))
        self.assertTrue(client.claim('B' * 43))
        payload, digest = _encode(b'restored')
        opener.value = {'version':2,'payload':payload,'digest':digest}
        self.assertEqual(client.read('assistant.sqlite3'), (2,b'restored'))
        self.assertEqual(json.loads(opener.requests[-1].data)['p_owner'], 'B' * 43)

    def test_unknown_write_invalidates_request_cache(self):
        opener = FakeOpener({'version':0,'payload':None,'digest':None})
        client = self.client(opener)
        client.owner = self.OWNER
        client.read('assistant.sqlite3')
        opener.error = URLError('lost acknowledgement')
        with self.assertRaises(SnapshotWriteUnknown):
            client.compare_swap('assistant.sqlite3', 0, b'might have committed')
        opener.error = None
        payload, digest = _encode(b'might have committed')
        opener.value = {'version':1,'payload':payload,'digest':digest}
        count = len(opener.requests)
        self.assertEqual(client.read('assistant.sqlite3'), (1,b'might have committed'))
        self.assertEqual(len(opener.requests), count + 1)

    def test_write_timeout_does_not_retry_or_expose_key(self):
        opener = FakeOpener(error=URLError('backend secret that must not escape'))
        client = self.client(opener)
        client.owner = self.OWNER
        with self.assertRaises(SnapshotWriteUnknown) as caught:
            client.compare_swap('assistant.sqlite3', 0, b'some data')
        self.assertEqual(len(opener.requests), 1)
        self.assertNotIn('secret', str(caught.exception))

    def test_read_network_failure_and_bad_cas_response(self):
        client = self.client(FakeOpener(error=URLError('secret')))
        client.owner = self.OWNER
        with self.assertRaises(sqlite3.OperationalError) as caught:
            client.read('assistant.sqlite3')
        self.assertNotIn('secret', str(caught.exception))
        opener = FakeOpener({'ok':False})
        client = self.client(opener)
        client.owner = self.OWNER
        with self.assertRaises(SnapshotConflict):
            client.compare_swap('assistant.sqlite3', 0, b'data')
        opener.value = {'ok':True,'version':99}
        with self.assertRaises(SnapshotWriteUnknown):
            client.compare_swap('assistant.sqlite3', 0, b'data')
        opener.value = {'ok':True,'version':True}
        with self.assertRaises(SnapshotWriteUnknown):
            client.compare_swap('assistant.sqlite3', 0, b'data')


if __name__ == '__main__':
    unittest.main()
