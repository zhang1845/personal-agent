"""Build a portable source ZIP from the reviewed release manifest."""
import argparse
from pathlib import Path, PureWindowsPath
import re
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def release_files(root):
    """Only explicitly listed, regular files inside the project can ship."""
    manifest = root / 'code/release-files.txt'
    if manifest.resolve() != manifest:
        raise ValueError('发布清单不能使用符号链接')
    for entry in manifest.read_text(encoding='utf-8').splitlines():
        if not entry or entry.startswith('#'):
            continue
        relative = Path(entry)
        if ('\\' in entry or relative.is_absolute() or PureWindowsPath(entry).drive
                or '..' in relative.parts or relative.parts[0] in {'docs', 'output', 'private', '.git'}):
            raise ValueError(f'发布清单路径无效：{entry}')
        path = root / relative
        if path.resolve() != path or not path.is_file():
            raise ValueError(f'发布文件缺失或路径含符号链接：{entry}')
        yield path


def build_release(destination=None, root=ROOT):
    root = Path(root).resolve()
    files = list(release_files(root))
    version = re.search(r'__version__ = "([^"]+)"', (root / 'code/personal_agent/__init__.py').read_text(encoding='utf-8')).group(1)
    destination = Path(destination or root / 'output' / f'daynest-v{version}.zip')
    if destination.resolve() in files:
        raise ValueError('输出 ZIP 不能覆盖发布源文件')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as tmp:
        temp_path = Path(tmp.name)
    try:
        with zipfile.ZipFile(temp_path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for path in files:
                archive.write(path, (Path(root.name) / path.relative_to(root)).as_posix())
        temp_path.replace(destination)
    finally:
        temp_path.unlink(missing_ok=True)
    return destination


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', help='可选的源代码 ZIP 输出路径')
    args = parser.parse_args()
    print(build_release(args.output))
