"""Create a portable source release without private data, caches or logs."""
import argparse
from pathlib import Path
import re
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def build_release(destination=None, root=ROOT):
    version = re.search(r'__version__ = "([^"]+)"', (root / 'code/personal_agent/__init__.py').read_text(encoding='utf-8')).group(1)
    destination = Path(destination) if destination else root / 'output' / ('daynest-v%s.zip' % version)
    destination.parent.mkdir(parents=True, exist_ok=True)
    excluded = {'.git', '.venv', '__pycache__', 'output', 'logs', 'private'}
    with zipfile.ZipFile(destination, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(root.rglob('*')):
            relative = path.relative_to(root)
            if path.is_symlink() or not path.is_file() or any(part in excluded for part in relative.parts):
                continue
            if path.suffix in ('.pyc', '.sqlite3', '.db', '.jsonl', '.zip') or path.name.startswith('.env') or path.name.endswith(('-wal','-shm','-journal')) or path.name == 'ego-browser-guide.txt':
                continue
            # ZIP entry names always use '/', including when built on Windows.
            archive.write(path, (Path(root.name) / relative).as_posix())
    return destination


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', help='可选的源代码 ZIP 输出路径')
    args = parser.parse_args()
    print(build_release(args.output))
