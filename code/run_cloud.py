"""Optional cloud entry point; local Windows/macOS launchers stay unchanged."""
import argparse
import os
import signal
import sys
import threading

from cloud_admin import read_config
from personal_agent.cloud import CloudConfig, CloudGateway
from personal_agent.platform_utils import configure_stdio


def main():
    configure_stdio()
    parser = argparse.ArgumentParser(description='Daynest 单用户云端入口；须由 HTTPS 反向代理接入')
    parser.add_argument('--env-file', help='私密登录配置路径，默认从部署平台环境变量读取')
    parser.add_argument('--check', action='store_true', help='只校验配置，不启动或读取业务数据')
    parser.add_argument('--host', default='127.0.0.1', help='默认仅本机；容器显式指定 0.0.0.0')
    parser.add_argument('--port', type=int, default=int(os.environ.get('PORT', '8080')))
    args = parser.parse_args()
    try:
        values = read_config(args.env_file) if args.env_file else {}
        values.update(os.environ)
        config = CloudConfig.from_env(values)
        storage = values.get('DAYNEST_STORAGE', 'sqlite')
        if storage not in ('sqlite', 'cloudbase'):
            raise ValueError('DAYNEST_STORAGE 仅支持 sqlite 或 cloudbase')
        remote = None
        if storage == 'cloudbase':
            from personal_agent.cloudbase_store import CloudBaseClient
            env_id = values.get('DAYNEST_CLOUDBASE_ENV', '')
            backend_key = values.get('DAYNEST_CLOUDBASE_KEY', '')
            if backend_key:
                remote = CloudBaseClient(env_id, backend_key)
            else:
                from personal_agent.cloudbase_identity import CloudBaseIdentity
                identity = CloudBaseIdentity(env_id)
                remote = CloudBaseClient(env_id, bearer_provider=identity.bearer)
        if not 1 <= args.port <= 65535:
            raise ValueError('端口须为1至65535')
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    if args.check:
        print('Daynest 云端配置格式校验通过；尚未连接数据库或启动服务。')
        return 0
    try:
        from waitress import create_server
    except ImportError:
        parser.error('请先安装 requirements-cloud.txt；本机版无需此依赖')
    if os.name != 'nt':
        os.umask(0o077)
    if remote is None:
        gateway = CloudGateway(config)
    else:
        from personal_agent.cloudbase_gateway import CloudBaseGateway
        gateway = CloudBaseGateway(config, remote)
    server = None
    previous = None
    stopped = threading.Event()
    try:
        server = create_server(gateway, host=args.host, port=args.port, threads=4,
                               max_request_body_size=12*1024*1024, channel_timeout=300,
                               expose_tracebacks=False, clear_untrusted_proxy_headers=True)
        if remote is not None:
            def abort_unhealthy_worker():
                # A timed-out loopback handler can still be executing. Exiting
                # the whole process prevents it from resuming after takeover.
                # Keep the remote lease; expiry is the conservative handover.
                while not stopped.wait(.5):
                    if gateway.faulted:
                        os._exit(75)
            threading.Thread(target=abort_unhealthy_worker, daemon=True).start()
        def shutdown(*_):
            raise KeyboardInterrupt()
        previous = signal.signal(signal.SIGTERM, shutdown)
        print('Daynest 云端入口已启动；请通过配置的 HTTPS 地址登录。', flush=True)
        server.run()
    except KeyboardInterrupt:
        pass
    finally:
        stopped.set()
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
        if server is not None:
            server.close()
            server.task_dispatcher.shutdown()
        gateway.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
