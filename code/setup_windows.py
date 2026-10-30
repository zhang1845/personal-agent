"""Create this project's Windows venv; never installs Python, a local LLM or services."""
import importlib.util
import os
from pathlib import Path
import struct
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / '.venv' / 'Scripts' / 'python.exe'


def validate_runtime():
    if sys.platform != 'win32':
        raise SystemExit('此安装入口仅用于 Windows；macOS 请按 README 安装。')
    if sys.version_info[:2] != (3, 12) or struct.calcsize('P') != 8:
        raise SystemExit('请安装 Python 3.12（64 位），再重新运行安装入口。')
    import platform
    if platform.machine().upper() not in ('AMD64', 'X86_64'):
        raise SystemExit('此安装入口面向 Windows x64；ARM64 尚未验证。')


def verify_dependencies():
    for module in ('nanobot', 'trafilatura', 'markitdown', 'apscheduler', 'apprise', 'caldav', 'dateutil', 'tzdata'):
        if importlib.util.find_spec(module) is None:
            raise SystemExit('缺少依赖：' + module)
    from zoneinfo import ZoneInfo
    from nanobot import Nanobot  # noqa: F401 — verifies the actual SDK entry point
    from markitdown.converters import PdfConverter, DocxConverter  # noqa: F401
    ZoneInfo('Asia/Shanghai')
    ZoneInfo('America/New_York')
    print('依赖与时区检查通过。')


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    validate_runtime()
    if sys.argv[1:] == ['--verify']:
        verify_dependencies()
        return 0
    if sys.argv[1:]:
        raise SystemExit('不支持的安装参数。')
    env = dict(os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
    if PYTHON.exists():
        # An environment copied from another OS or Python version is not reusable.
        subprocess.run([str(PYTHON), '-X', 'utf8', str(Path(__file__)), '--verify-runtime'],
                       check=True, cwd=ROOT, env=env)
    else:
        if (ROOT / '.venv').exists():
            raise SystemExit('现有 .venv 无法在 Windows 使用。请将其重命名备份后，再安装；不要移动 output/runtime。')
        print('正在创建项目环境……', flush=True)
        venv.EnvBuilder(with_pip=True).create(ROOT / '.venv')
    subprocess.run([str(PYTHON), '-X', 'utf8', '-m', 'pip', 'install', '-r',
                    str(ROOT / 'requirements.lock.txt')], check=True, cwd=ROOT, env=env)
    subprocess.run([str(PYTHON), '-X', 'utf8', '-m', 'pip', 'check'], check=True, cwd=ROOT, env=env)
    subprocess.run([str(PYTHON), '-X', 'utf8', str(Path(__file__)), '--verify'],
                   check=True, cwd=ROOT, env=env)
    print('安装完成。双击“启动Daynest.cmd”打开 Daynest。')
    return 0


if __name__ == '__main__':
    try:
        if sys.argv[1:] == ['--verify-runtime']:
            validate_runtime()
        else:
            raise SystemExit(main())
    except (subprocess.CalledProcessError, OSError) as exc:
        print('安装未完成。请检查上方错误、网络和目录权限，再重新运行；已有数据不会删除。', file=sys.stderr)
        raise SystemExit(1) from None
