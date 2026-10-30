"""Real private-loopback requests with an isolated, fenced in-memory RPC server.

These tests do not contact CloudBase or claim PostgreSQL integration coverage.
"""
from io import BytesIO
import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from personal_agent.cloud import CloudConfig, CloudGateway, password_hash
from personal_agent.cloudbase_gateway import CloudBaseGateway
from personal_agent.cloudbase_store import (
    CloudBaseClient, DB_NAMES, SnapshotWriteUnknown, clear_remote, connect,
)
from personal_agent.errors import AgentError

PASSWORD = 'daynest-cloudbase-isolated-test-password'
PASSWORD_HASH = password_hash(PASSWORD)


class OfflineModel:
    def list_models(self):
        return {'available': False, 'models': []}
    def chat(self, *args, **kwargs):
        raise AgentError('model_unavailable', '测试不访问模型')


class MemoryLeaseServer:
    def __init__(self):
        self.lock = threading.RLock()
        self.rows = {}
        self.owner = None
        self.clock = 0
        self.expires = 0
        self.initialized = False
        self.calls = []


class FakeMemoryLeaseClient(CloudBaseClient):
    """Keep the actual client codec, cache and owner propagation under test."""
    def __init__(self, server=None):
        super().__init__('daynest-test-only', 'fake-service-key-for-isolated-tests')
        self.server = server or MemoryLeaseServer()
        self.fail_next = None
        self.lose_cas_ack = None

    def _rpc(self, operation, values, *, writing=False):
        server = self.server
        with server.lock:
            server.calls.append((operation, values.get('p_name'), values.get('p_owner'), threading.get_ident()))
            if self.fail_next == operation:
                self.fail_next = None
                raise sqlite3.OperationalError('测试 RPC 不可用')
            owner = values.get('p_owner')
            valid = server.owner == owner and server.clock < server.expires
            if operation == 'daynest_lease_claim':
                if server.owner is not None and server.clock < server.expires:
                    return False
                server.owner, server.expires = owner, server.clock + 600
                return True
            if operation == 'daynest_lease_check':
                return valid
            if operation == 'daynest_lease_release':
                if not valid:
                    return False
                server.owner = None
                return True
            if not valid:
                raise sqlite3.OperationalError('测试租约已失效')
            if operation == 'daynest_lease_complete_init':
                if not DB_NAMES <= server.rows.keys():
                    raise sqlite3.OperationalError('数据库初始化不完整')
                server.initialized = True
                return True
            name = values['p_name']
            current = server.rows.get(name)
            if operation == 'daynest_state_read':
                if current is None:
                    if server.initialized and name in DB_NAMES:
                        raise sqlite3.OperationalError('已初始化数据库缺失')
                    return {'version': 0, 'payload': None, 'digest': None}
                return dict(current)
            if operation == 'daynest_state_cas':
                version = 0 if current is None else current['version']
                if values['p_expected'] != version:
                    return {'ok': False}
                server.rows[name] = {'version': version + 1,
                    'payload': values['p_payload'], 'digest': values['p_digest']}
                if self.lose_cas_ack == name:
                    self.lose_cas_ack = None
                    raise SnapshotWriteUnknown()
                return {'ok': True, 'version': version + 1}
            raise AssertionError(operation)


class CloudBaseGatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='daynest-cloudbase-')
        self.root = Path(self.temp.name)
        self.server = MemoryLeaseServer()
        self.gateways = []
        self.paths = []
        self.cookie = ''
        self.csrf = ''
        self.gateway = self.make_gateway('first')

    def tearDown(self):
        for gateway in reversed(self.gateways):
            gateway.close()
        for path in self.paths:
            clear_remote(path)
        self.temp.cleanup()

    def config(self, name):
        path = self.root / name
        self.paths.append(path)
        return CloudConfig.from_env({'DAYNEST_PUBLIC_URL': 'https://daynest.example',
            'DAYNEST_DATA_DIR': str(path), 'DAYNEST_PASSWORD_HASH': PASSWORD_HASH})

    def make_gateway(self, name):
        gateway = CloudBaseGateway(self.config(name), FakeMemoryLeaseClient(self.server), model_client=OfflineModel())
        self.gateways.append(gateway)
        return gateway

    def request(self, method, path, body=None, *, gateway=None, headers=None, raw=None):
        gateway = gateway or self.gateway
        raw = raw if raw is not None else json.dumps(body, ensure_ascii=False).encode() if body is not None else b''
        environ = {'REQUEST_METHOD': method, 'PATH_INFO': path, 'QUERY_STRING': '',
            'HTTP_HOST': 'daynest.example', 'CONTENT_LENGTH': str(len(raw)),
            'CONTENT_TYPE': 'application/json', 'wsgi.input': BytesIO(raw)}
        if self.cookie:
            environ['HTTP_COOKIE'] = self.cookie
        if method not in ('GET', 'HEAD'):
            environ.update(HTTP_ORIGIN='https://daynest.example', HTTP_X_AGENT_TOKEN=self.csrf)
        environ.update(headers or {})
        captured = []
        def start(status, response_headers):
            captured.append((int(status.split()[0]), dict(response_headers)))
        value = b''.join(gateway(environ, start))
        self.assertEqual(1, len(captured), '真实响应只能确认一次')
        status, response_headers = captured[0]
        if value and 'application/json' in response_headers.get('Content-Type', ''):
            value = json.loads(value)
        return status, response_headers, value

    def login(self):
        code, headers, _ = self.request('POST', '/login', raw=urlencode({'password': PASSWORD}).encode(),
            headers={'CONTENT_TYPE': 'application/x-www-form-urlencoded'})
        self.assertEqual(303, code)
        self.cookie = headers['Set-Cookie'].split(';')[0]
        code, _, value = self.request('GET', '/api/bootstrap')
        self.assertEqual(200, code)
        self.csrf = value['token']
        self.assertEqual('on_demand', value['deployment']['reminders'])

    def owned(self, gateway, operation):
        with gateway.request_gate:
            owner = 'test-owned-operation-' + 'x' * 32
            self.assertTrue(gateway.remote.claim(owner))
            try:
                return operation()
            finally:
                self.assertTrue(gateway.remote.release(owner))

    def test_restart_restores_all_nine_databases_three_settings_and_session(self):
        self.login()
        self.assertTrue(self.server.initialized)
        self.assertEqual(200, self.request('POST', '/api/tasks', {'title': '远程保留待办'})[0])
        settings = [('/api/model-api', {'base_url':'https://model.invalid/v1', 'model':'fake',
            'api_key':'fake-model-key-only', 'enabled':False, 'expected_version':1}),
            ('/api/notifications/settings', {'urls':'json://notify.invalid', 'enabled':False, 'expected_version':1}),
            ('/api/caldav', {'url':'https://calendar.invalid/', 'username':'fake-user',
                'password':'fake-calendar-password-only', 'enabled':False, 'writes_enabled':False, 'expected_version':1})]
        for path, value in settings:
            self.assertEqual(200, self.request('PUT', path, value)[0], path)
        def seed():
            for name in DB_NAMES:
                with connect(self.gateway.config.data_dir / name) as connection:
                    connection.execute('CREATE TABLE restore_probe(value TEXT)')
                    connection.execute('INSERT INTO restore_probe VALUES(?)', (name,))
                connection.close()
        self.owned(self.gateway, seed)
        self.assertIsNone(self.gateway.backend.application.automations.scheduler)
        self.assertEqual({'cloud-lease.sqlite3'}, {path.name for path in self.gateway.config.data_dir.iterdir()})
        old_token = self.csrf
        self.gateway.close()
        self.gateways.remove(self.gateway)
        self.gateway = self.make_gateway('fresh-empty-container')
        code, _, bootstrap = self.request('GET', '/api/bootstrap')
        self.assertEqual(200, code, '登录会话应从远程恢复')
        self.assertEqual(old_token, bootstrap['token'])
        self.assertEqual('远程保留待办', self.request('GET', '/api/tasks')[2]['tasks'][0]['title'])
        self.assertEqual(200, self.request('POST', '/api/tasks', {'title':'旧页面token继续有效'})[0])
        model = self.request('GET', '/api/model-api')[2]['settings']
        self.assertTrue(model['has_key'])
        self.assertNotIn('fake-model-key-only', json.dumps(model))
        self.assertTrue(self.request('GET', '/api/notifications/settings')[2]['settings']['has_urls'])
        self.assertTrue(self.request('GET', '/api/caldav')[2]['settings']['has_password'])
        def restored():
            for name in DB_NAMES:
                with connect(self.gateway.config.data_dir / name) as connection:
                    self.assertEqual(name, connection.execute('SELECT value FROM restore_probe').fetchone()[0])
                connection.close()
        self.owned(self.gateway, restored)
        self.assertEqual({'cloud-lease.sqlite3'}, {path.name for path in self.gateway.config.data_dir.iterdir()})

    def test_owner_reaches_actual_loopback_thread_and_invalid_csrf_cannot_tick(self):
        self.login()
        self.server.calls.clear()
        self.assertEqual(200, self.request('POST', '/api/tasks', {'title':'线程租约'})[0])
        writes = [call for call in self.server.calls if call[:2] == ('daynest_state_cas', 'assistant.sqlite3')]
        self.assertEqual(1, len(writes))
        self.assertNotEqual(threading.get_ident(), writes[0][3])
        claimed = next(call[2] for call in self.server.calls if call[0] == 'daynest_lease_claim')
        self.assertEqual(claimed, writes[0][2])
        self.gateway.last_tick = 0
        with patch.object(self.gateway.backend.application.automations, 'tick') as tick:
            self.assertEqual(403, self.request('POST', '/api/tasks', {'title':'不得保存'},
                headers={'HTTP_X_AGENT_TOKEN':'wrong'})[0])
            tick.assert_not_called()

    def test_busy_other_instance_does_not_fault_or_recover_active_runs(self):
        self.login()
        def running():
            return self.gateway.backend.application.execution.start('test-session', 'test-engine', 'test-model')
        run_id = self.owned(self.gateway, running)
        other = FakeMemoryLeaseClient(self.server)
        self.assertTrue(other.claim('other-request-' + 'x' * 32))
        try:
            self.assertEqual(503, self.request('GET', '/api/tasks')[0])
            self.assertFalse(self.gateway.faulted)
            with self.assertRaises(ValueError):
                self.make_gateway('busy-startup')
            raw = other.read('agent-runs.sqlite3')[1]
            with sqlite3.connect(':memory:') as connection:
                connection.deserialize(raw)
                self.assertEqual('running', connection.execute('SELECT state FROM runs WHERE id=?', (run_id,)).fetchone()[0])
        finally:
            other.release(other.owner)
        second = self.make_gateway('rolling-second')
        self.assertEqual(200, self.request('GET', '/api/tasks', gateway=second)[0])
        def recovered():
            with second.backend.application.execution.connection() as connection:
                self.assertEqual('interrupted', connection.execute('SELECT state FROM runs WHERE id=?', (run_id,)).fetchone()[0])
        self.owned(second, recovered)

    def test_expiry_after_write_never_acknowledges_success_or_releases(self):
        self.login()
        original = self.gateway.backend.application.route
        def expire(method, path, query, body):
            result = original(method, path, query, body)
            self.server.clock += 601
            return result
        self.server.calls.clear()
        with patch.object(self.gateway.backend.application, 'route', side_effect=expire):
            self.assertEqual(503, self.request('POST', '/api/tasks', {'title':'已写入但请求过期'})[0])
        self.assertTrue(self.gateway.faulted)
        self.assertFalse(any(call[0] == 'daynest_lease_release' for call in self.server.calls))
        self.assertEqual(503, self.request('GET', '/healthz')[0])
        self.assertEqual(503, self.request('GET', '/api/tasks')[0])
        second = self.make_gateway('after-expired-owner')
        self.assertEqual(1, len(self.request('GET', '/api/tasks', gateway=second)[2]['tasks']))

    def test_rpc_failure_is_closed_and_does_not_create_local_fallback(self):
        self.login()
        self.gateway.remote.fail_next = 'daynest_lease_claim'
        self.assertEqual(503, self.request('POST', '/api/tasks', {'title':'不得落本地'})[0])
        self.assertTrue(self.gateway.faulted)
        self.assertEqual(503, self.request('GET', '/healthz')[0])
        self.assertEqual({'cloud-lease.sqlite3'}, {path.name for path in self.gateway.config.data_dir.iterdir()})

    def test_lost_write_ack_is_unknown_and_is_not_replayed(self):
        self.login()
        self.gateway.remote.lose_cas_ack = 'assistant.sqlite3'
        self.assertEqual(503, self.request('POST', '/api/tasks', {'title':'结果未知不自动重试'})[0])
        tasks = self.request('GET', '/api/tasks')[2]['tasks']
        self.assertEqual(['结果未知不自动重试'], [task['title'] for task in tasks])

    def test_external_send_then_storage_failure_recovers_unknown_without_resending(self):
        self.login()
        self.assertEqual(200, self.request('PUT', '/api/notifications/settings', {
            'urls':'json://notify.invalid', 'enabled':True, 'expected_version':1})[0])
        sends = []
        def sent(title, body, urls):
            sends.append((title, body))
            # The durable 'sending' claim already succeeded. Lose the next
            # result write after this external side effect has happened.
            self.gateway.remote.fail_next = 'daynest_state_cas'
            return True
        self.gateway.backend.application.automations.sender = sent
        self.assertEqual(503, self.request('POST', '/api/notifications/test', {})[0])
        self.assertEqual(1, len(sends))
        self.gateway.close()
        self.gateways.remove(self.gateway)
        self.gateway = self.make_gateway('recover-unknown-send')
        self.gateway.backend.application.automations.sender = sent
        code, _, value = self.request('GET', '/api/automations')
        self.assertEqual(200, code)
        self.assertEqual(['unknown'], [item['state'] for item in value['inbox']])
        self.gateway.last_tick = 0
        self.assertEqual(200, self.request('GET', '/api/automations')[0])
        self.assertEqual(1, len(sends), '未知结果不允许自动重发')

    def test_proxy_timeout_retains_owner_while_loopback_worker_is_still_busy(self):
        self.login()
        entered, resume, finished = threading.Event(), threading.Event(), threading.Event()
        original_route = self.gateway.backend.application.route
        original_connection = http.client.HTTPConnection
        def blocked(method, path, query, body):
            entered.set()
            resume.wait(5)
            try:
                return original_route(method, path, query, body)
            finally:
                finished.set()
        class TimeoutConnection(original_connection):
            def getresponse(inner):
                if not entered.wait(3):
                    raise AssertionError('真实 loopback worker 未开始')
                raise TimeoutError('测试代理超时')
        self.server.calls.clear()
        try:
            with patch.object(self.gateway.backend.application, 'route', side_effect=blocked), \
                    patch('personal_agent.cloud.HTTPConnection', TimeoutConnection):
                self.assertEqual(503, self.request('GET', '/api/tasks')[0])
                self.assertTrue(self.gateway.faulted)
                self.assertIsNotNone(self.server.owner)
                self.assertFalse(any(call[0] == 'daynest_lease_release' for call in self.server.calls))
                self.assertFalse(FakeMemoryLeaseClient(self.server).claim('new-request-' + 'x' * 32))
                self.assertEqual(503, self.request('GET', '/healthz')[0])
        finally:
            resume.set()
            self.assertTrue(finished.wait(3))

    def test_native_gateway_still_uses_local_sqlite_without_remote_configuration(self):
        native = CloudGateway(self.config('native'), model_client=OfflineModel(), scheduler=False)
        self.gateways.append(native)
        self.assertTrue((native.config.data_dir / 'assistant.sqlite3').exists())
        self.assertTrue((native.config.data_dir / 'cloud-auth.sqlite3').exists())
        self.assertIsNone(native.backend.application.api_settings._remote)


if __name__ == '__main__':
    unittest.main()
