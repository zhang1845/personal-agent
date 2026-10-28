"""Manage this project's loopback service on macOS, Linux and Windows."""
import argparse
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import build_opener, ProxyHandler, Request
import webbrowser

from personal_agent.platform_utils import IS_WINDOWS, configure_stdio, private_file_permissions

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = ROOT / 'output' / 'runtime'
START_TIMEOUT = 30.0
STOP_TIMEOUT = 15.0
PROBE_TIMEOUT = .5


def probe(port, data_dir=DEFAULT_DATA, timeout=PROBE_TIMEOUT):
    try:
        with build_opener(ProxyHandler({})).open('http://127.0.0.1:%s/api/runtime' % port, timeout=timeout) as response:
            data = json.loads(response.read(16384))
        if Path(data.get('project_root', '')).resolve() != ROOT:
            return None
        # Compatibility with the older macOS launcher, only for its default data.
        actual = data.get('data_dir')
        if actual is None:
            return data if data_dir == DEFAULT_DATA else None
        return data if Path(actual).resolve() == data_dir else None
    except (OSError, ValueError, TypeError):
        return None


def port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        connection.settimeout(.3)
        return connection.connect_ex(('127.0.0.1', port)) == 0


def detached_options(windows=IS_WINDOWS):
    if windows:
        return {'creationflags': subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
    return {'start_new_session': True}


def service_environment(token):
    env = dict(os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8', PERSONAL_AGENT_STOP_TOKEN=token)
    if ('PERSONAL_AGENT_STARTUP_DIAGNOSTICS' not in env
            and any(env.get(flag, '').lower() == 'true' for flag in ('CI', 'GITHUB_ACTIONS'))):
        env['PERSONAL_AGENT_STARTUP_DIAGNOSTICS'] = '1'
    return env


def write_state(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.service-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            private_file_permissions(stream.fileno())
            json.dump(data, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def stop_service(port, data_dir, state_path, info):
    try:
        state = json.loads(state_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        state = {}
    token = state.get('stop_token')
    if (isinstance(token, str) and token and state.get('instance') == info.get('service_instance')
            and state.get('pid') == info.get('service_pid') and state.get('port') == port
            and state.get('root') == str(ROOT) and state.get('data_dir') == str(data_dir)):
        url = 'http://127.0.0.1:%s' % port
        request = Request(url + '/api/service/stop', data=b'{}', method='POST', headers={
            'Origin': url, 'Content-Type': 'application/json', 'X-Agent-Service-Token': token,
        })
        try:
            with build_opener(ProxyHandler({})).open(request, timeout=3) as response:
                if json.loads(response.read(4096)).get('stopping') is not True:
                    raise ValueError()
        except (OSError, ValueError):
            raise SystemExit('未能安全停止服务，请查看运行记录；没有强制结束进程。') from None
    elif not IS_WINDOWS and not info.get('service_instance'):
        # Upgrade path for an already-running older macOS service. Its project
        # identity was just verified through loopback; never trust a stale PID file.
        pid = info.get('service_pid')
        if type(pid) is not int or pid <= 1:
            raise SystemExit('无法核验服务进程，未停止其他进程。')
        os.kill(pid, signal.SIGTERM)
    else:
        raise SystemExit('缺少此服务的停止凭据。前台启动的服务请在原窗口按 Ctrl+C 停止。')
    deadline = time.monotonic() + STOP_TIMEOUT
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        current = probe(port, data_dir, timeout=min(PROBE_TIMEOUT, remaining))
        if not current or current.get('service_instance') != info.get('service_instance'):
            state_path.unlink(missing_ok=True)
            print('个人 Agent 后台服务已停止。')
            return
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(.1, remaining))
    raise SystemExit('服务尚未退出，请稍后检查状态；没有强制结束进程。')


def wait_started(process, port, data_dir, log, timeout=START_TIMEOUT):
    """A wall-clock deadline includes every request, rather than just sleeps."""
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SystemExit('启动未就绪（等待 %g 秒），请查看 %s。' % (timeout, log))
        if process.poll() is not None:
            raise SystemExit('服务未能启动（退出码 %s），请查看 %s。' % (process.returncode, log))
        info = probe(port, data_dir, timeout=min(PROBE_TIMEOUT, remaining))
        if info:
            if info.get('service_pid') != process.pid:
                raise SystemExit('另一个服务已先启动，请重新检查状态。')
            return info
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(.1, remaining))


def reap_failed_start(process):
    """Reap only our newly spawned child when it never reached readiness."""
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def main(argv=None):
    configure_stdio()
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['start', 'stop', 'status'])
    parser.add_argument('--port', type=int, default=8787)
    parser.add_argument('--open', action='store_true')
    parser.add_argument('--data-dir', help='可选的隔离数据目录')
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error('端口无效')
    data_dir = Path(args.data_dir).expanduser().resolve() if args.data_dir else DEFAULT_DATA
    state_path = data_dir / ('service.json' if args.port == 8787 else 'service-%s.json' % args.port)
    log = ROOT / 'output' / 'service.log' if data_dir == DEFAULT_DATA else data_dir / 'service.log'
    info = probe(args.port, data_dir)
    if args.action == 'status':
        print('正在运行' if info else '未运行')
        return 0 if info else 1
    if args.action == 'stop':
        if not info:
            print('该项目服务未运行。')
            return 0
        stop_service(args.port, data_dir, state_path, info)
        return 0
    if not info:
        if port_in_use(args.port):
            raise SystemExit('端口已被其他服务占用，请换一个 --port；没有停止其他程序。')
        token = secrets.token_urlsafe(32)
        env = service_environment(token)
        log.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, 'a', encoding='utf-8') as output:
            private_file_permissions(output.fileno())
            process = subprocess.Popen([
                sys.executable, '-X', 'utf8', '-B', str(ROOT / 'code' / 'run.py'),
                'web', '--port', str(args.port), '--data-dir', str(data_dir),
            ], cwd=str(ROOT), env=env, stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                **detached_options())
        ready = False
        try:
            info = wait_started(process, args.port, data_dir, log)
            write_state(state_path, {'pid': process.pid, 'port': args.port, 'root': str(ROOT),
                                    'data_dir': str(data_dir), 'instance': info['service_instance'], 'stop_token': token})
            ready = True
        finally:
            if not ready:
                reap_failed_start(process)
    url = 'http://127.0.0.1:%s' % args.port
    print('后台服务：' + url + '；关闭终端后仍运行。')
    if args.open:
        webbrowser.open(url)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
