from __future__ import annotations

import os
from pathlib import Path

import yaml

config_path = Path.home() / ".hermes" / "config.yaml"
config = yaml.safe_load(config_path.read_text())
entry = config["plugins"]["entries"]["ruoyu-cost-router"]
if not isinstance(entry, str):
    raise SystemExit("refusing migration: expected current entry to be a string")
parsed = yaml.safe_load(entry)
if not isinstance(parsed, dict) or parsed.get("catalog_version") != 2:
    raise SystemExit("refusing migration: expected catalog_version 2 mapping")
config["plugins"]["entries"]["ruoyu-cost-router"] = parsed
rendered = yaml.safe_dump(config, allow_unicode=True, sort_keys=False)
temporary_path = config_path.with_suffix(".yaml.ruoyu-migrate.tmp")
temporary_path.write_text(rendered)
yaml.safe_load(temporary_path.read_text())
os.replace(temporary_path, config_path)
print("migrated plugin entry to YAML mapping")
