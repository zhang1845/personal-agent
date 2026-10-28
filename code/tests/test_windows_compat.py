"""Portable regressions plus the Windows blocking-pipe branch on every host."""
import http.client
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from personal_agent.api_model import APISettings
from personal_agent.private_config import PrivateConfig
from personal_agent.errors import AgentError
from personal_agent.ics import write_snapshot
from personal_agent.webapp import make_server

CODE = Path(__file__).resolve().parents[1]
ROOT = CODE.parent
spec = importlib.util.spec_from_file_location('service_manager_under_test', CODE / 'manage_service.py')
manager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manager)


class ThreadedBridgeTests(__import__('test_bridge').BridgeTests):
    def make(self, *args, **kwargs):
        bridge = super().make(*args, **kwargs)
        bridge._threaded_pipes = True
        return bridge

    def test_timeout_reconnect_discards_old_queues(self):
        bridge = self.make('timeout', timeout_seconds=.15)
        self.assert_error('backend_unavailable', bridge.list_calendars)
        self.assertIsNone(bridge._pipes)
        bridge.config['args'][1] = 'normal'
        self.assertEqual('work', bridge.list_calendars()[0]['id'])

    def test_workers_exit_after_close(self):
        bridge = self.make()
        bridge.list_calendars()
        pipes = bridge._pipes
        bridge.close()
        self.assertTrue(all(not worker.is_alive() for worker in pipes.workers))

    def test_child_not_reading_times_out_without_retry_or_hang(self):
        script = """
import json,sys,time
request=json.loads(sys.stdin.readline())
print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':{'protocolVersion':'2025-06-18','capabilities':{'tools':{}}}}),flush=True)
time.sleep(5)
"""
        bridge = self.make(writes_enabled=True, timeout_seconds=.15)
        bridge.config['args'] = ['-u', '-c', script]
        started = time.monotonic()
        self.assert_error('unknown_outcome', lambda: bridge.create_event({'description':'中'*130000}, 'once'))
        self.assertLess(time.monotonic() - started, 2)
        self.assertIsNone(bridge._pipes)


class PortableStorageTests(unittest.TestCase):
    def test_private_chinese_settings_without_unix_fchmod(self):
        with tempfile.TemporaryDirectory(prefix='个人 工作台 ') as directory:
            with patch('personal_agent.platform_utils.IS_WINDOWS', True), patch('os.fchmod', create=True, side_effect=AssertionError('Unix only')):
                settings = APISettings(Path(directory) / 'API 配置.json')
                settings.save({'base_url':'https://example.com/v1', 'api_key':'unit-test', 'model':'模型名称', 'enabled':False, 'expected_version':1})
                self.assertEqual('模型名称', settings.get()['model'])
                private = PrivateConfig(Path(directory) / '日历.json', {'username':'', 'password':''}, {'password'})
                saved = private.save({'username':'测试用户', 'password':'中文密码', 'expected_version':1}, lambda *args: None)
                self.assertEqual('测试用户', saved['username'])
                self.assertTrue(saved['has_password'])
                self.assertNotIn('password', saved)
                self.assertEqual('中文密码', private.read()['password'])
                self.assertIn('测试用户', private.path.read_text(encoding='utf-8'))

    def test_windows_export_uses_exclusive_rename(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / '日程.ics'
            # On POSIX rename overwrites; emulate Windows' documented no-overwrite.
            with patch('personal_agent.ics.IS_WINDOWS', True), patch('personal_agent.ics.os.rename', side_effect=FileExistsError()), patch('personal_agent.ics.os.link') as link:
                with self.assertRaises(AgentError) as caught:
                    write_snapshot(target, [])
                self.assertEqual('output_exists', caught.exception.code)
                link.assert_not_called()
            self.assertEqual([], list(Path(directory).iterdir()))

    def test_cli_utf8_even_with_legacy_console_encoding(self):
        with tempfile.TemporaryDirectory(prefix='中文 路径 ') as directory:
            result = subprocess.run([sys.executable, '-X', 'utf8=0', '-B', str(CODE/'run.py'),
                '--db', str(Path(directory)/'日程.sqlite3'), 'tool', 'calendar_status'],
                env=dict(os.environ, PYTHONUTF8='0', PYTHONIOENCODING='cp1252'),
                text=True, encoding='utf-8', capture_output=True, timeout=10)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn('backend', json.loads(result.stdout))

    def test_source_archive_has_launchers_but_no_private_data_or_logs(self):
        spec = importlib.util.spec_from_file_location('packager_under_test', CODE/'package_release.py')
        packager = importlib.util.module_from_spec(spec);spec.loader.exec_module(packager)
        import zipfile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)/'项目';root.mkdir()
            def put(name, value):
                target = root/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_text(value,encoding='utf-8')
            put('code/personal_agent/__init__.py','__version__ = "0.6.0"')
            put('启动个人Agent.cmd','@echo off')
            for name in ('output/runtime/model-api.json','logs/session.log','.venv/pyvenv.cfg','assets/private/token.json','.env','old.sqlite3-wal'):
                put(name,'private-unit-test-token')
            archive = packager.build_release(Path(directory)/'release.zip',root=root)
            with zipfile.ZipFile(archive) as release:
                names = release.namelist()
                self.assertEqual(2,len(names))
                self.assertTrue(any(name.endswith('/启动个人Agent.cmd') for name in names))
                self.assertTrue(all('\\' not in name for name in names))
                self.assertTrue(all(b'private-unit-test-token' not in release.read(name) for name in names))

    def test_windows_detachment_does_not_use_unix_session(self):
        with patch.object(subprocess, 'DETACHED_PROCESS', 8, create=True), patch.object(subprocess, 'CREATE_NEW_PROCESS_GROUP', 512, create=True):
            self.assertEqual({'creationflags':520}, manager.detached_options(windows=True))
        self.assertEqual({'start_new_session':True}, manager.detached_options(windows=False))


class ManagedServiceTests(unittest.TestCase):
    def test_real_background_cycle_chinese_path_keeps_existing_data(self):
        with tempfile.TemporaryDirectory(prefix='个人 Agent 测试 ') as directory:
            data = Path(directory).resolve() / '工作台数据'
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            command = [sys.executable, '-X', 'utf8', '-B', str(CODE / 'manage_service.py')]
            tail = ['--port', str(port), '--data-dir', str(data)]
            def service_diagnostics():
                log = data / 'service.log'
                text = log.read_text(encoding='utf-8', errors='replace')[-16384:] if log.exists() else '(service log not created)'
                diagnostics = ROOT / 'output' / 'ci-service-startup.log'
                diagnostics.parent.mkdir(parents=True, exist_ok=True)
                diagnostics.write_text(text, encoding='utf-8')
                return text
            def run(action):
                # The manager's real 30s startup deadline plus process cleanup and
                # cold interpreter imports must fit inside this outer test bound.
                try:
                    result = subprocess.run(command + [action] + tail, cwd=directory, text=True,
                                            encoding='utf-8', capture_output=True, timeout=60 if action == 'start' else 30)
                except subprocess.TimeoutExpired as exc:
                    self.fail('Managed %s exceeded its test deadline.\n%s\nstdout=%r\nstderr=%r' %
                              (action, service_diagnostics(), exc.stdout, exc.stderr))
                if result.returncode and action != 'status':
                    result.stderr += '\nService startup log:\n' + service_diagnostics()
                return result
            def request(path, method='GET', body=None, **headers):
                connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
                try:
                    connection.request(method, path, body=body, headers=headers)
                    response = connection.getresponse()
                    return response.status, json.loads(response.read())
                finally:
                    connection.close()
            try:
                started = run('start')
                self.assertEqual(0, started.returncode, started.stdout + started.stderr + service_diagnostics() if started.returncode else '')
                self.assertEqual(0, run('status').returncode)
                status, runtime = request('/api/runtime')
                self.assertEqual(200, status)
                self.assertEqual(str(data), runtime['data_dir'])
                self.assertTrue(runtime['scheduler_running'])
                self.assertNotIn('stop_token', runtime)
                # Starting twice keeps the original instance/credentials.
                self.assertEqual(0, run('start').returncode)
                self.assertEqual(runtime['service_instance'], request('/api/runtime')[1]['service_instance'])
                status, bootstrap = request('/api/bootstrap')
                origin = 'http://127.0.0.1:' + str(port)
                headers = {'Origin':origin, 'Content-Type':'application/json', 'X-Agent-Token':bootstrap['token']}
                status, _ = request('/api/service/stop', 'POST', b'{}', **headers)
                self.assertEqual(403, status)  # Browser CSRF token alone cannot stop it.
                wrong = dict(headers, **{'X-Agent-Service-Token':'wrong'})
                self.assertEqual(403, request('/api/service/stop', 'POST', b'{}', **wrong)[0])
                status, result = request('/api/tasks', 'POST', json.dumps({'title':'Windows 迁移任务'}, ensure_ascii=False).encode(), **headers)
                self.assertEqual(200, status, result)
                self.assertEqual(0, run('stop').returncode)
                self.assertEqual(1, run('status').returncode)
                self.assertFalse((data / ('service-%s.json' % port)).exists())
                self.assertEqual(0, run('start').returncode)
                status, tasks = request('/api/tasks')
                self.assertEqual('Windows 迁移任务', tasks['tasks'][0]['title'])
                self.assertEqual(0, run('stop').returncode)
            finally:
                run('stop')

    def test_occupied_port_and_stale_pid_file_never_stop_other_service(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        class Other(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                self.send_response(200); self.end_headers(); self.wfile.write(b'{"project_root":"different-project","service_pid":1}')
        with tempfile.TemporaryDirectory() as directory:
            server = ThreadingHTTPServer(('127.0.0.1',0), Other)
            worker = threading.Thread(target=server.serve_forever, daemon=True);worker.start()
            try:
                command = [sys.executable, '-X', 'utf8', '-B', str(CODE/'manage_service.py')]
                tail = ['--port',str(server.server_port),'--data-dir',directory]
                state = Path(directory)/('service-%s.json'%server.server_port)
                state.write_text(json.dumps({'pid':os.getpid()}),encoding='utf-8')
                start = subprocess.run(command+['start']+tail,text=True,encoding='utf-8',capture_output=True,timeout=10)
                self.assertNotEqual(0,start.returncode)
                self.assertIn('端口已被其他服务占用', start.stderr)
                stop = subprocess.run(command+['stop']+tail,text=True,encoding='utf-8',capture_output=True,timeout=10)
                self.assertEqual(0,stop.returncode)
                self.assertTrue(worker.is_alive())
            finally:
                server.shutdown();server.server_close();worker.join()


if __name__ == '__main__':
    unittest.main()
