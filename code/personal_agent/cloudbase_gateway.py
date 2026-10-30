"""On-demand CloudBase gateway; all durable state uses fenced HTTPS RPCs.

A request owns one database lease across the existing multi-store workflow.
The owner is deliberately process-wide: the private loopback HTTP server and
nanobot worker threads must use the same fence. The local gate permits only one
such workflow at a time. No scheduler runs outside that gate.
"""
import hashlib
import secrets
import sqlite3
import threading
import time
from urllib.parse import urlsplit

from .cloud import CloudGateway, STATIC
from .cloudbase_store import configure_remote


class CloudBaseGateway(CloudGateway):
    def __init__(self, config, client, *, model_client=None):
        self.remote = client
        self.request_gate = threading.RLock()
        self.faulted = False
        self.last_tick = 0.0
        self._tick_allowed = False
        configure_remote(config.data_dir, client)
        # This fixed read-only RPC enforces the same service_role gate as all
        # writes. A fresh random owner cannot legitimately hold the lease.
        if client.check(secrets.token_urlsafe(32)) is not False:
            raise ValueError('云端服务身份验证未通过，未初始化业务数据')
        owner = secrets.token_urlsafe(32)
        if not client.claim(owner):
            raise ValueError('另一个请求正在使用工作台，请稍后重试部署')
        try:
            super().__init__(config, model_client=model_client, scheduler=False)
            # Consistent across rolling instances, without exposing any secret.
            self.backend.application.token = hashlib.sha256(
                ('daynest-cloudbase-csrf-v1\0' + config.origin + '\0' + config.password).encode('utf-8')
            ).hexdigest()
            client.complete_init()
            if not client.check(owner):
                raise sqlite3.OperationalError('云端初始化未完成')
            if not client.release(owner):
                raise sqlite3.OperationalError('云端初始化结果待核查')
        except BaseException:
            # A failed construction must never fall back to temporary SQLite.
            self.faulted = True
            if hasattr(self, 'lease'):
                self.close()
            raise

    def deployment_info(self):
        return {'mode': 'cloud', 'storage': 'cloudbase', 'reminders': 'on_demand'}

    def authenticated_request(self, path, method):
        # Only authenticated business requests advance the existing scheduler.
        # Existing missed-time / no-automatic-resend semantics remain intact.
        if self._tick_allowed and path.startswith('/api/') and time.monotonic() - self.last_tick >= 30:
            self.last_tick = time.monotonic()
            self.backend.application.automations.tick()

    def proxy_failed(self):
        # A timed-out private request may still be running. Keep its lease until
        # expiry, reject further requests, and advertise unhealthy to the host.
        # Expired sends remain unknown and must not be automatically retried.
        # The cloud runner terminates a faulted process, including loopback
        # workers; an HTTP timeout alone cannot stop those workers safely.
        self.faulted = True

    def unavailable(self, start):
        return self.respond(start, 503, {'error': {'code': 'cloud_unavailable',
            'message': '工作台暂时忙碌或正在恢复，请稍后刷新；不要重复提交已发送的操作。'}},
            extra=[('Retry-After', '5')])

    def __call__(self, environ, start):
        path = environ.get('PATH_INFO', '/')
        method = environ.get('REQUEST_METHOD', 'GET')
        if path == '/healthz' and method == 'GET':
            if self.faulted:
                return self.respond(start, 503, {'ok': False})
            return super().__call__(environ, start)
        # Reject invalid origins before consuming any database allowance.
        if environ.get('HTTP_HOST', '').lower() != urlsplit(self.config.origin).netloc.lower():
            return self.respond(start, 400, {'error': {'code': 'forbidden_host', 'message': '站点地址不匹配'}})
        origin = environ.get('HTTP_ORIGIN')
        if (origin is not None and origin != self.config.origin
                or method not in ('GET', 'HEAD') and origin != self.config.origin):
            return self.respond(start, 403, {'error': {'code': 'forbidden_origin', 'message': '请从 Daynest 站点操作'}})
        if path in STATIC and method == 'GET':
            return super().__call__(environ, start)
        if self.faulted or not self.request_gate.acquire(timeout=5):
            return self.unavailable(start)
        owner = secrets.token_urlsafe(32)
        claimed = False
        captured = []
        try:
            if self.faulted or not self.remote.claim(owner):
                return self.unavailable(start)
            claimed = True
            self._tick_allowed = (method == 'GET' or method in ('POST', 'PUT', 'PATCH', 'DELETE')
                and secrets.compare_digest(environ.get('HTTP_X_AGENT_TOKEN', ''), self.backend.application.token))
            def capture(status, headers, exc_info=None):
                captured.append((status, headers))
            body = super().__call__(environ, capture)
            # Do not acknowledge success after expiry or an uncertain proxy.
            if self.faulted or not self.remote.check(owner):
                self.faulted = True
                return self.unavailable(start)
            if not self.remote.release(owner):
                self.faulted = True
                return self.unavailable(start)
            claimed = False
            if not captured:
                raise sqlite3.OperationalError('云端响应未完成')
            start(*captured[-1])
            return body
        except (OSError, sqlite3.Error, TimeoutError, ValueError):
            self.faulted = True
            return self.unavailable(start)
        finally:
            # On any unknown outcome, retain the lease. Do not let a second
            # process recover while this loopback request may still be alive.
            if claimed:
                self.faulted = True
            self._tick_allowed = False
            self.request_gate.release()
