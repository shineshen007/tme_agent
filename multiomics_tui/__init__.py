"""Interactive CLI/TUI front door for the deterministic paired multi-omics workflow.

Design boundaries (mirrors the project governance rules):
- The TUI never writes into a completed run directory and never changes frozen
  thresholds, grouping or config inputs.
- Natural language maps only onto a whitelisted action set; no arbitrary shell.
- The model never authors statistics numbers; all displayed numbers come from files.
"""
from pathlib import Path

__version__ = (Path(__file__).resolve().parents[1] / "VERSION").read_text(encoding="utf-8").strip()
