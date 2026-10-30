"""Optional CloudBase persistence for bounded, single-owner Daynest installations.

SQLite still executes each original transaction in memory. Its complete committed
image is synchronously compare-and-swapped into a private PostgreSQL record. This
is transaction persistence, not an asynchronous filesystem backup. A global
request lease, obtained by the gateway, also protects operations spanning stores.
No environment variables, credentials or native data files are read implicitly.
"""
import base64
import hashlib
import json
from pathlib import Path
import re
import secrets
import sqlite3
import threading
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import zlib

DB_NAMES = frozenset({
    'calendar.sqlite3', 'assistant.sqlite3', 'knowledge.sqlite3',
    'automations.sqlite3', 'caldav.sqlite3', 'agent-runs.sqlite3',
    'chat-integrations.sqlite3', 'quality.sqlite3', 'cloud-auth.sqlite3',
})
DOCUMENT_NAMES = frozenset({'model-api.json', 'notifications.json', 'caldav.json'})
NAMES = DB_NAMES | DOCUMENT_NAMES
MAX_RAW = 32 * 1024 * 1024
MAX_COMPRESSED = 3 * 1024 * 1024
MAX_PAYLOAD = 4 * 1024 * 1024
MAX_DOCUMENT = 65536
_UNAVAILABLE = '云端数据暂不可用，请稍后重试；不要重复提交已发送的操作。'
_registry = {}
_registry_lock = threading.RLock()


class SnapshotConflict(sqlite3.OperationalError):
    def __init__(self):
        super().__init__('云端数据已变化，请刷新后核查结果。')


class SnapshotWriteUnknown(sqlite3.OperationalError):
    def __init__(self):
        super().__init__('云端保存结果待核查；请刷新核对，不要重复提交。')


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _name(value):
    if value not in NAMES:
        raise sqlite3.OperationalError('不支持的云端数据名称')
    return value


def _owner(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', value):
        raise sqlite3.OperationalError('云端请求尚未取得数据锁')
    return value


def _encode(raw):
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_RAW:
        raise sqlite3.OperationalError('云端数据超过大小限制或格式无效')
    packed = zlib.compress(raw, level=6)
    if len(packed) > MAX_COMPRESSED:
        raise sqlite3.OperationalError('云端数据超过大小限制')
    return base64.b64encode(packed).decode('ascii'), hashlib.sha256(raw).hexdigest()


def _decode(payload, digest):
    try:
        if (not isinstance(payload, str) or not 0 < len(payload) <= MAX_PAYLOAD
                or not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)):
            raise ValueError()
        packed = base64.b64decode(payload, validate=True)
        if len(packed) > MAX_COMPRESSED:
            raise ValueError()
        decoder = zlib.decompressobj()
        raw = decoder.decompress(packed, MAX_RAW + 1)
        if len(raw) > MAX_RAW or decoder.unconsumed_tail or not decoder.eof or decoder.unused_data:
            raise ValueError()
        if not raw or not secrets.compare_digest(hashlib.sha256(raw).hexdigest(), digest):
            raise ValueError()
        return raw
    except (ValueError, TypeError, zlib.error, UnicodeError):
        raise sqlite3.OperationalError('云端数据校验失败，未载入空白数据') from None


class CloudBaseClient:
    """Fixed authenticated HTTPS RPCs. Never retries writes or follows redirects."""
    def __init__(self, env_id, api_key=None, *, bearer_provider=None, opener=None, timeout=30):
        if (not isinstance(env_id, str)
                or not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', env_id)):
            raise ValueError('CloudBase 环境名称无效')
        if (api_key is None) == (bearer_provider is None):
            raise ValueError('CloudBase 后端身份必须且只能配置一种')
        if bearer_provider is not None and not callable(bearer_provider):
            raise ValueError('CloudBase 服务身份提供器无效')
        if api_key is not None and not self._valid_bearer(api_key):
            raise ValueError('CloudBase 后端访问凭据无效')
        self.endpoint = f'https://{env_id}.api.tcloudbasegateway.com/v1/rdb/rest/rpc/'
        self._key = api_key
        self._bearer_provider = bearer_provider
        self.opener = opener or build_opener(ProxyHandler({}), _NoRedirect())
        self.timeout = timeout
        self.owner = None
        self._cache_owner = None
        self._cache = {}
        self._cache_lock = threading.RLock()

    @staticmethod
    def _valid_bearer(value):
        return (isinstance(value, str) and 16 <= len(value) <= 8192
                and all(32 < ord(ch) < 127 for ch in value))

    def _bearer(self):
        try:
            token = self._bearer_provider() if self._bearer_provider is not None else self._key
            if not self._valid_bearer(token):
                raise ValueError()
            return token
        except Exception:
            # Identity providers may hold temporary platform credentials. Never
            # expose their errors, tokens or request details through SQLite/API.
            # This runs before open(), so no data RPC was sent on failure.
            raise sqlite3.OperationalError('CloudBase 服务身份暂不可用') from None

    def _rpc(self, operation, values, *, writing=False):
        if operation not in {'daynest_state_read', 'daynest_state_cas',
                              'daynest_lease_claim', 'daynest_lease_release', 'daynest_lease_check',
                              'daynest_lease_complete_init'}:
            raise sqlite3.OperationalError('不支持的云端操作')
        try:
            body = json.dumps(values, ensure_ascii=True, allow_nan=False).encode('utf-8')
            if len(body) > MAX_PAYLOAD + 4096:
                raise ValueError()
            request = Request(self.endpoint + operation, data=body, method='POST',
                              headers={'Content-Type': 'application/json', 'Accept': 'application/json',
                                       'Authorization': 'Bearer ' + self._bearer()})
            with self.opener.open(request, timeout=self.timeout) as response:
                if getattr(response, 'status', 200) != 200:
                    raise ValueError()
                raw = response.read(MAX_PAYLOAD + 4097)
            if len(raw) > MAX_PAYLOAD + 4096:
                raise ValueError()
            return json.loads(raw.decode('utf-8'))
        except HTTPError as error:
            error.close()
            # A rejected HTTP request is still not replayed. The response body
            # may contain provider internals and is deliberately not inspected.
            if writing:
                raise SnapshotWriteUnknown() from None
            raise sqlite3.OperationalError(_UNAVAILABLE) from None
        except (URLError, OSError, TimeoutError, ValueError, TypeError, UnicodeError, RecursionError):
            if writing:
                raise SnapshotWriteUnknown() from None
            raise sqlite3.OperationalError(_UNAVAILABLE) from None

    def claim(self, owner):
        owner = _owner(owner)
        with self._cache_lock:
            self._cache.clear()
            self._cache_owner = None
            result = self._rpc('daynest_lease_claim', {'p_owner': owner}, writing=True)
            if type(result) is not bool:
                raise SnapshotWriteUnknown()
            if result:
                self.owner = owner
                self._cache_owner = owner
            return result

    def release(self, owner):
        owner = _owner(owner)
        with self._cache_lock:
            # Even a failed/unknown release must not leave reusable snapshots.
            self._cache.clear()
            self._cache_owner = None
            result = self._rpc('daynest_lease_release', {'p_owner': owner}, writing=True)
            if type(result) is not bool:
                raise SnapshotWriteUnknown()
            if result and self.owner == owner:
                self.owner = None
            return result

    def check(self, owner=None):
        result = self._rpc('daynest_lease_check', {'p_owner': _owner(self.owner if owner is None else owner)})
        if type(result) is not bool:
            raise sqlite3.OperationalError(_UNAVAILABLE)
        return result

    def complete_init(self):
        result = self._rpc('daynest_lease_complete_init', {'p_owner': _owner(self.owner)}, writing=True)
        if result is not True:
            raise SnapshotWriteUnknown()

    def read(self, name):
        name, owner = _name(name), _owner(self.owner)
        with self._cache_lock:
            if self._cache_owner != owner:
                self._cache.clear()
                self._cache_owner = owner
            if name in self._cache:
                return self._cache[name]
            result = self._rpc('daynest_state_read', {'p_name': name, 'p_owner': owner})
            if not isinstance(result, dict) or type(result.get('version')) is not int or result['version'] < 0:
                raise sqlite3.OperationalError(_UNAVAILABLE)
            if result['version'] == 0:
                if result.get('payload') is not None or result.get('digest') is not None:
                    raise sqlite3.OperationalError(_UNAVAILABLE)
                value = (0, None)
            else:
                value = (result['version'], _decode(result.get('payload'), result.get('digest')))
            self._cache[name] = value  # A tuple of int/immutable bytes only.
            return value

    def compare_swap(self, name, expected, raw):
        if type(expected) is not int or not 0 <= expected < 2 ** 53:
            raise sqlite3.OperationalError('云端版本无效')
        payload, digest = _encode(raw)
        name, owner = _name(name), _owner(self.owner)
        with self._cache_lock:
            try:
                result = self._rpc('daynest_state_cas', {'p_name': name, 'p_expected': expected,
                                   'p_payload': payload, 'p_digest': digest, 'p_owner': owner}, writing=True)
                if not isinstance(result, dict) or type(result.get('ok')) is not bool:
                    raise SnapshotWriteUnknown()
                if not result['ok']:
                    raise SnapshotConflict()
                if type(result.get('version')) is not int or result['version'] != expected + 1:
                    raise SnapshotWriteUnknown()
                if self._cache_owner != owner:
                    self._cache.clear()
                    self._cache_owner = owner
                self._cache[name] = (expected + 1, raw)
                return expected + 1
            except BaseException:
                self._cache.clear()
                self._cache_owner = None
                raise


class SnapshotConnection(sqlite3.Connection):
    """A disposable SQLite transaction image; close never silently uploads it."""
    def __init__(self, client, name, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._client = client
        self._name = _name(name)
        self._failed = False
        try:
            self._version, raw = client.read(name)
            if raw is not None:
                if not isinstance(raw, bytes) or len(raw) > MAX_RAW or not raw.startswith(b'SQLite format 3\x00'):
                    raise sqlite3.DatabaseError()
                self.deserialize(raw)
                if self.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise sqlite3.DatabaseError()
            else:
                # An initialized empty database can be serialized, unlike a
                # zero-page new :memory: database. It is not uploaded on reads.
                self.execute('PRAGMA user_version=0')
            self._baseline = self.serialize()
        except sqlite3.Error:
            super().close()
            raise sqlite3.OperationalError('云端数据库校验失败，未载入空白数据') from None

    def commit(self):
        if self._failed:
            raise SnapshotWriteUnknown()
        super().commit()
        raw = self.serialize()
        if raw == self._baseline:
            return
        try:
            self._version = self._client.compare_swap(self._name, self._version, raw)
            self._baseline = raw
        except BaseException:
            # This connection cannot be reused to replay a failed/unknown CAS.
            self._failed = True
            raise

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        if kind is not None:
            self.rollback()
            return False
        try:
            self.commit()
        except BaseException:
            self.rollback()
            raise
        return False


def configure_remote(root, client):
    root = Path(root).expanduser().resolve()
    if not hasattr(client, 'read') or not hasattr(client, 'compare_swap'):
        raise ValueError('云端持久化客户端无效')
    with _registry_lock:
        existing = _registry.get(root)
        if existing is not None and existing is not client:
            raise ValueError('同一数据目录已配置云端存储')
        _registry[root] = client
    return root


def clear_remote(root):
    with _registry_lock:
        _registry.pop(Path(root).expanduser().resolve(), None)


def _target(path):
    supplied = Path(path).expanduser().absolute()
    path = supplied.resolve()
    with _registry_lock:
        for root, client in _registry.items():
            # On macOS /var is an alias of /private/var. Resolve ancestors too,
            # so a leaf symlink escaping a configured root cannot accidentally
            # miss this routing check and open a native file instead.
            supplied_inside = any(parent.resolve() == root for parent in supplied.parents)
            if path == root or root in path.parents or supplied == root or supplied_inside:
                if path.parent != root or path.name not in NAMES or supplied.is_symlink():
                    raise sqlite3.OperationalError('云端数据路径无效，未回退本地存储')
                return client, path.name
    return None


def connect(path, **kwargs):
    """Explicit replacement for the three business/auth connect entry points."""
    target = _target(path)
    if target is None:
        return sqlite3.connect(path, **kwargs)
    client, name = target
    if name not in DB_NAMES or 'factory' in kwargs or kwargs.get('uri'):
        raise sqlite3.OperationalError('云端数据库路径或选项无效')
    return SnapshotConnection(client, name, ':memory:', **kwargs)


class RemoteDocument:
    def __init__(self, client, name):
        self.client, self.name = client, name

    def read(self):
        revision, raw = self.client.read(self.name)
        if raw is None:
            return revision, None
        try:
            if len(raw) > MAX_DOCUMENT:
                raise ValueError()
            value = json.loads(raw.decode('utf-8'))
            if not isinstance(value, dict):
                raise ValueError()
            return revision, value
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise sqlite3.OperationalError('云端私密配置校验失败') from None

    def compare_swap(self, revision, value):
        try:
            if not isinstance(value, dict):
                raise ValueError()
            raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
            if len(raw) > MAX_DOCUMENT:
                raise ValueError()
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise sqlite3.OperationalError('云端私密配置无效或过大') from None
        return self.client.compare_swap(self.name, revision, raw)


def remote_document(path):
    target = _target(path)
    if target is None:
        return None
    client, name = target
    if name not in DOCUMENT_NAMES:
        raise sqlite3.OperationalError('云端私密配置路径无效')
    return RemoteDocument(client, name)
