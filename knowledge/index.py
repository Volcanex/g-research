#!/usr/bin/env python3
"""Build and query a local jselect snapshot of the held literature.

  python3 -I knowledge/index.py build
  python3 -I knowledge/index.py ask "question" --tokens 8000

G_RESEARCH_DATA selects the data directory. The snapshot stays outside the repo.
Selection is local and needs no key. Tokens are counted as bytes, a conservative
budget that also avoids downloading a tokenizer vocabulary.
"""
import argparse, os, shutil, subprocess, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import DATA, LIB


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='mode', required=True)
    commands.add_parser('build')
    ask = commands.add_parser('ask')
    ask.add_argument('question')
    ask.add_argument('--tokens', type=int, default=8000)
    args = parser.parse_args()
    command = shutil.which('jselect') or '/home/gabriel/genv/venvs/jev/bin/jselect'
    if not os.path.isfile(command):
        parser.error('jselect is not installed')
    snapshot = os.path.join(DATA, 'library.jselect')
    if args.mode == 'build':
        from pathlib import Path
        if not any(Path(LIB).glob('*/text.txt')):
            parser.error('no library text.txt files found')
        os.makedirs(DATA, exist_ok=True)
        return subprocess.call([command, 'index', LIB, '--glob', '**/text.txt', '--output', snapshot, '--force'])
    if args.tokens < 0:
        parser.error('--tokens must be zero or greater')
    if not os.path.isfile(snapshot):
        parser.error('no index found, run index.py build first')
    return subprocess.call([command, args.question, snapshot, '--local', '--encoding', 'bytes', '--tokens', str(args.tokens), '--stats'])


if __name__ == '__main__':
    sys.exit(main())
