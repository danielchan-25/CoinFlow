#!/usr/bin/env python3
"""入口壳：实际实现在 droidfleet/apps/kuaishou.py。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from droidfleet.apps.kuaishou import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
