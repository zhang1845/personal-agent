"""Optional cloud entry point; local Windows/macOS launchers stay unchanged."""
import argparse
import os
import signal
import sys

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
        if not 1 <= args.port <= 65535:
            raise ValueError('端口须为1至65535')
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    if args.check:
        print('Daynest 云端配置校验通过。需要持久化存储及 HTTPS 反向代理。')
        return 0
    try:
        from waitress import create_server
    except ImportError:
        parser.error('请先安装 requirements-cloud.txt；本机版无需此依赖')
    if os.name != 'nt':
        os.umask(0o077)
    gateway = CloudGateway(config)
    server = None
    previous = None
    try:
        server = create_server(gateway, host=args.host, port=args.port, threads=4,
                               max_request_body_size=12*1024*1024, channel_timeout=300,
                               expose_tracebacks=False, clear_untrusted_proxy_headers=True)
        def shutdown(*_):
            raise KeyboardInterrupt()
        previous = signal.signal(signal.SIGTERM, shutdown)
        print('Daynest 云端入口已启动；请通过配置的 HTTPS 地址登录。', flush=True)
        server.run()
    except KeyboardInterrupt:
        pass
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
        if server is not None:
            server.close()
            server.task_dispatcher.shutdown()
        gateway.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
