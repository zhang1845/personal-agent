"""Authenticated WSGI gateway to one private loopback workbench.

The existing HTTP API keeps its Host, Origin, CSRF and write-confirmation rules.
Only this gateway is exposed, behind HTTPS; it relays authenticated requests to
an ephemeral loopback port after validating the configured public origin.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from http import HTTPStatus
from http.client import HTTPConnection
from http.cookies import SimpleCookie, CookieError
import hashlib
import html
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from urllib.parse import parse_qs, quote, urlsplit

from .webapp import make_server, PROJECT_ROOT
from .cloudbase_store import connect as open_database

COOKIE = '__Host-daynest'
SESSION_SECONDS = 8 * 60 * 60
ITERATIONS = 600_000
STATIC = {'/login.css': ('login.css', 'text/css; charset=utf-8'),
          '/brand.css': ('brand.css', 'text/css; charset=utf-8'),
          '/vendor/Geist-variable.woff2': ('vendor/Geist-variable.woff2', 'font/woff2')}
CSP = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"


def password_hash(password, salt=None):
    if not isinstance(password, str) or not 12 <= len(password) <= 256:
        raise ValueError('登录密码须为12至256个字符')
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), bytes.fromhex(salt), ITERATIONS).hex()
    return f'pbkdf2_sha256${ITERATIONS}${salt}${digest}'


def validate_hash(value):
    if not isinstance(value, str) or not re.fullmatch(r'pbkdf2_sha256\$600000\$[0-9a-f]{32}\$[0-9a-f]{64}', value):
        raise ValueError('请先使用 code/cloud_admin.py 生成登录配置')
    return value


@dataclass(frozen=True)
class CloudConfig:
    origin: str
    data_dir: Path
    password: str

    @classmethod
    def from_env(cls, values=None):
        values = os.environ if values is None else values
        origin = values.get('DAYNEST_PUBLIC_URL', '').rstrip('/')
        parts = urlsplit(origin)
        try:
            if (parts.scheme != 'https' or not parts.hostname or parts.username or parts.password
                    or parts.path or parts.query or parts.fragment or '\\' in origin
                    or any(ord(c) <= 32 or ord(c) >= 127 for c in origin)
                    or parts.port is not None and not 1 <= parts.port <= 65535):
                raise ValueError()
        except ValueError:
            raise ValueError('DAYNEST_PUBLIC_URL 必须是完整的 HTTPS 站点地址，不含路径或账号') from None
        data = Path(values.get('DAYNEST_DATA_DIR', ''))
        if not values.get('DAYNEST_DATA_DIR') or not data.is_absolute():
            raise ValueError('DAYNEST_DATA_DIR 必须指向持久化存储的绝对路径')
        hostname = parts.hostname.lower()
        try:
            address = ipaddress.ip_address(hostname)
            authority = f'[{hostname}]' if address.version == 6 else hostname
        except ValueError:
            label = r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?'
            if len(hostname) > 253 or not re.fullmatch(label + r'(?:\.' + label + r')*', hostname):
                raise ValueError('HTTPS 站点域名格式无效') from None
            authority = hostname
        if parts.port is not None and parts.port != 443:
            authority += f':{parts.port}'
        return cls('https://' + authority, data, validate_hash(values.get('DAYNEST_PASSWORD_HASH')))


class AuthStore:
    """Persist only hashed session tokens; rotating the password revokes sessions."""
    def __init__(self, folder, credential):
        self.path = Path(folder) / 'cloud-auth.sqlite3'
        self.credential = validate_hash(credential)
        self.revision = hashlib.sha256(credential.encode()).hexdigest()
        with self.connection() as c:
            c.execute('CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY,expires REAL,revision TEXT)')
            c.execute('CREATE TABLE IF NOT EXISTS failures(at REAL NOT NULL)')

    @contextmanager
    def connection(self):
        c = open_database(self.path, timeout=10)
        try:
            with c:
                yield c
        finally:
            c.close()

    def login(self, password):
        now = time.time()
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('DELETE FROM failures WHERE at<?', (now - 600,))
            c.execute('DELETE FROM sessions WHERE expires<=? OR revision!=?', (now, self.revision))
            if c.execute('SELECT COUNT(*) FROM failures').fetchone()[0] >= 10:
                return 'limited', None
            valid = isinstance(password, str) and 12 <= len(password) <= 256
            if valid:
                valid = secrets.compare_digest(password_hash(password, self.credential.split('$')[2]), self.credential)
            if not valid:
                c.execute('INSERT INTO failures VALUES(?)', (now,))
                return 'invalid', None
            token = secrets.token_urlsafe(32)
            c.execute('INSERT INTO sessions VALUES(?,?,?)', (self.digest(token), now + SESSION_SECONDS, self.revision))
            return 'ok', token

    @staticmethod
    def digest(token):
        return hashlib.sha256(token.encode('utf-8')).hexdigest()

    def authenticated(self, token):
        if not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
            return False
        with self.connection() as c:
            return bool(c.execute('SELECT 1 FROM sessions WHERE token=? AND expires>? AND revision=?',
                                  (self.digest(token), time.time(), self.revision)).fetchone())

    def logout(self, token):
        with self.connection() as c:
            c.execute('DELETE FROM sessions WHERE token=?', (self.digest(token),))


class CloudGateway:
    def __init__(self, config, *, model_client=None, scheduler=True):
        self.config = config
        config.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        # A dedicated held transaction prevents duplicate schedulers on one disk.
        self.lease = sqlite3.connect(config.data_dir / 'cloud-lease.sqlite3', timeout=0, check_same_thread=False)
        try:
            self.lease.execute('CREATE TABLE IF NOT EXISTS lease(id INTEGER)')
            self.lease.execute('BEGIN EXCLUSIVE')
        except sqlite3.Error:
            self.lease.close()
            raise ValueError('此数据目录已有云端进程运行，请保持单实例') from None
        self.backend = None
        self.thread = None
        try:
            self.auth = AuthStore(config.data_dir, config.password)
            self.backend = make_server(config.data_dir, port=0, model_client=model_client)
            self.backend_origin = f'http://127.0.0.1:{self.backend.server_port}'
            self.thread = threading.Thread(target=self.backend.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
            self.thread.start()
            if scheduler:
                self.backend.application.automations.start()
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.backend is not None:
            if self.thread is not None and self.thread.is_alive():
                self.backend.shutdown()
            self.backend.server_close()
            if self.thread is not None and self.thread.ident is not None:
                self.thread.join(timeout=5)
            self.backend.application.close()
            self.backend = None
        self.lease.close()

    @staticmethod
    def respond(start, status, body, kind='application/json; charset=utf-8', extra=()):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode('utf-8')
        headers = [('Content-Type', kind), ('Content-Length', str(len(body))), ('Cache-Control', 'no-store'),
                   # HTML form POST must retain its real Origin for CSRF checks.
                   ('X-Content-Type-Options', 'nosniff'), ('Referrer-Policy', 'same-origin'),
                   ('Content-Security-Policy', CSP), ('Cross-Origin-Resource-Policy', 'same-origin'),
                   ('Strict-Transport-Security', 'max-age=31536000')]
        start(f'{status} {HTTPStatus(status).phrase}', headers + list(extra))
        return [body]

    def login_page(self, start, status=200, message=''):
        body = (PROJECT_ROOT / 'assets/web/login.html').read_text(encoding='utf-8')
        return self.respond(start, status, body.replace('{{message}}', html.escape(message)).encode('utf-8'), 'text/html; charset=utf-8')

    @staticmethod
    def token(environ):
        try:
            cookie = SimpleCookie(environ.get('HTTP_COOKIE', ''))
            return cookie[COOKIE].value if COOKIE in cookie else ''
        except CookieError:
            return ''

    def __call__(self, environ, start):
        try:
            return self.dispatch(environ, start)
        except (OSError, sqlite3.Error, TimeoutError):
            return self.respond(start, 503, {'error': {'code': 'cloud_unavailable', 'message': '服务暂不可用，请稍后重试；不要重复提交已发送的操作。'}})
        except Exception:
            # No exception text, credentials, input bodies or traceback in responses.
            return self.respond(start, 500, {'error': {'code': 'internal_error', 'message': '请求未完成，请稍后重试。'}})

    def authenticated_request(self, path, method):
        """Optional hosting policy; native persistent-disk mode needs no hook."""

    def proxy_failed(self):
        """Optional hosting policy after an uncertain loopback request."""

    def deployment_info(self):
        return {'mode': 'cloud'}

    def dispatch(self, environ, start):
        path = environ.get('PATH_INFO', '/')
        method = environ.get('REQUEST_METHOD', 'GET')
        if method == 'GET' and path == '/healthz':
            ready = self.backend is not None and self.thread is not None and self.thread.is_alive()
            return self.respond(start, 200 if ready else 503, {'ok': ready})
        if environ.get('HTTP_HOST', '').lower() != urlsplit(self.config.origin).netloc.lower():
            return self.respond(start, 400, {'error': {'code': 'forbidden_host', 'message': '站点地址不匹配'}})
        origin = environ.get('HTTP_ORIGIN')
        if origin is not None and origin != self.config.origin or method not in ('GET', 'HEAD') and origin != self.config.origin:
            return self.respond(start, 403, {'error': {'code': 'forbidden_origin', 'message': '请从 Daynest 站点操作'}})
        if path.startswith('/api/service/'):
            return self.respond(start, 404, {'error': {'code': 'not_found', 'message': '没有此操作'}})
        if path in STATIC and method == 'GET':
            name, kind = STATIC[path]
            return self.respond(start, 200, (PROJECT_ROOT / 'assets/web' / name).read_bytes(), kind)
        token = self.token(environ)
        authenticated = self.auth.authenticated(token)
        if path == '/login':
            if method == 'GET':
                return self.respond(start, 303, b'', extra=[('Location', '/')]) if authenticated else self.login_page(start)
            if method != 'POST':
                return self.respond(start, 405, b'')
            try:
                size = int(environ.get('CONTENT_LENGTH') or '0')
                if not 0 < size <= 4096 or environ.get('CONTENT_TYPE', '').split(';')[0] != 'application/x-www-form-urlencoded':
                    raise ValueError()
                values = parse_qs(environ['wsgi.input'].read(size).decode('utf-8'), max_num_fields=2)
                if set(values) != {'password'} or len(values['password']) != 1:
                    raise ValueError()
            except (ValueError, UnicodeError):
                return self.login_page(start, 400, '登录信息格式无效')
            state, token = self.auth.login(values['password'][0])
            if state != 'ok':
                return self.login_page(start, 429 if state == 'limited' else 401,
                                       '尝试次数过多，请10分钟后重试' if state == 'limited' else '密码不正确')
            cookie = f'{COOKIE}={token}; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age={SESSION_SECONDS}'
            return self.respond(start, 303, b'', extra=[('Location', '/'), ('Set-Cookie', cookie)])
        if not authenticated:
            if path.startswith('/api/'):
                return self.respond(start, 401, {'error': {'code': 'authentication_required', 'message': '请先登录 Daynest'}})
            return self.respond(start, 303, b'', extra=[('Location', '/login')])
        self.authenticated_request(path, method)
        if path == '/logout':
            if method != 'POST':
                return self.respond(start, 405, b'')
            self.auth.logout(token)
            return self.respond(start, 303, b'', extra=[('Location', '/login'),
                ('Set-Cookie', f'{COOKIE}=; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=0')])
        if method not in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE'):
            return self.respond(start, 405, b'')
        try:
            size = int(environ.get('CONTENT_LENGTH') or '0')
        except ValueError:
            size = -1
        if size < 0 or size > 12 * 1024 * 1024 or environ.get('HTTP_TRANSFER_ENCODING'):
            return self.respond(start, 413, {'error': {'code': 'body_too_large', 'message': '请求大小无效'}})
        body = environ['wsgi.input'].read(size)
        headers = {'Host': self.backend_origin.removeprefix('http://'), 'Connection': 'close'}
        for source, dest in [('CONTENT_TYPE', 'Content-Type'), ('HTTP_X_AGENT_TOKEN', 'X-Agent-Token')]:
            if source in environ:
                headers[dest] = environ[source]
        if origin is not None:
            headers['Origin'] = self.backend_origin
        # WSGI PATH_INFO uses Latin-1 to carry UTF-8 URL bytes.
        target = quote(path.encode('latin-1'), safe='/')
        query = environ.get('QUERY_STRING', '')
        if query:
            target += '?' + query
        connection = HTTPConnection('127.0.0.1', self.backend.server_port, timeout=240)
        try:
            connection.request(method, target, body=body, headers=headers)
            response = connection.getresponse()
            content = response.read()
            kind = response.getheader('Content-Type', 'application/json; charset=utf-8')
            extra = [('Content-Disposition', response.getheader('Content-Disposition'))] if response.getheader('Content-Disposition') else []
            if path == '/api/bootstrap' and response.status == 200:
                value = json.loads(content)
                value['deployment'] = self.deployment_info()
                content = json.dumps(value, ensure_ascii=False).encode('utf-8')
            return self.respond(start, response.status, content, kind, extra)
        except (OSError, TimeoutError):
            self.proxy_failed()
            raise
        finally:
            connection.close()
