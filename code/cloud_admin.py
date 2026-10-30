"""Create a private cloud login configuration; never reads model API settings."""
import argparse
import getpass
import json
import os
from pathlib import Path
import sys

from personal_agent.cloud import CloudConfig, password_hash
from personal_agent.platform_utils import configure_stdio, private_file_permissions

KEYS = {'DAYNEST_PUBLIC_URL', 'DAYNEST_DATA_DIR', 'DAYNEST_PASSWORD_HASH',
        'DAYNEST_STORAGE', 'DAYNEST_CLOUDBASE_ENV', 'DAYNEST_CLOUDBASE_KEY'}


def read_config(path):
    path = Path(path)
    if path.is_symlink() or path.stat().st_size > 8192:
        raise ValueError('云端配置文件无效')
    values = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        key, sep, value = line.partition('=')
        if not sep or key not in KEYS or key in values:
            raise ValueError('云端配置字段无效')
        if value.startswith("'") and value.endswith("'"):
            value = value[1:-1]
        else:
            value = json.loads(value)
        if not isinstance(value, str):
            raise ValueError('云端配置值须为文本')
        values[key] = value
    return values


def write_config(path, origin, data_dir, password):
    values = {'DAYNEST_PUBLIC_URL': origin, 'DAYNEST_DATA_DIR': str(data_dir),
              'DAYNEST_PASSWORD_HASH': password_hash(password)}
    config = CloudConfig.from_env(values)
    values['DAYNEST_PUBLIC_URL'] = config.origin
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Exclusive creation prevents accidentally replacing a deployed login.
    with path.open('x', encoding='utf-8', newline='\n') as stream:
        private_file_permissions(stream.fileno())
        for key, value in values.items():
            encoded = "'" + value + "'" if key == 'DAYNEST_PASSWORD_HASH' else json.dumps(value, ensure_ascii=False)
            stream.write(f'{key}={encoded}\n')
    return path


def main():
    configure_stdio()
    parser = argparse.ArgumentParser(description='生成 Daynest 私密登录配置；不读取或上传模型 API Key')
    parser.add_argument('--url', required=True, help='HTTPS 站点地址，例如 https://daynest.example.com')
    parser.add_argument('--data-dir', default='/data', help='持久化数据目录；容器使用 /data')
    parser.add_argument('--output', type=Path, default=Path('private/cloud.env'))
    args = parser.parse_args()
    if not sys.stdin.isatty():
        parser.error('请在交互终端输入登录密码，不要通过命令行参数或管道传入密码')
    password = getpass.getpass('设置 Daynest 登录密码（至少12个字符）：')
    if password != getpass.getpass('再次输入：'):
        parser.error('两次密码不一致')
    try:
        path = write_config(args.output, args.url, args.data_dir, password)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(f'已生成私密登录配置：{path}。请留在服务器，勿提交到 GitHub。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
