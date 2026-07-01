import json
import os

STATE_FILE = os.path.join(os.path.dirname(__file__), "paper_state.json")


def load_state() -> dict | None:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return None


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
