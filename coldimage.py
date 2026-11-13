#!/usr/bin/env python3
"""Deprecated alias for coldrecall.py, kept for one release so existing hooks and scripts keep working."""
import sys

from coldrecall import *
from coldrecall import main

if __name__ == "__main__":
    sys.exit(main())
