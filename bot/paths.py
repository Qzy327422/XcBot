# -*- coding: utf-8 -*-
from pathlib import Path

# bot/ is under project root
BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = str(BASE_DIR / "config.json")
DATA_DIR = BASE_DIR / "data"
