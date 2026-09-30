"""Demonstrate the paper planner using synthetic data, with no network calls."""
import json
from pathlib import Path

from bot import plan, validate

snapshot = json.loads((Path(__file__).parent / "examples" / "snapshot.json").read_text())
result = plan(snapshot["trade"], snapshot["market"], snapshot["book"],
              validate(snapshot["config"]), snapshot["now"])
print(json.dumps({"synthetic_demo": True, "decision": result}, indent=2))
