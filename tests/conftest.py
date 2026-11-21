from __future__ import annotations

import json
from pathlib import Path
from typing import Any

VECTORS = Path(__file__).parent / "vectors"
SIGNING: dict[str, Any] = json.loads((VECTORS / "signing.json").read_text(encoding="utf-8"))
RESPONSES: dict[str, dict[str, Any]] = {
    p.stem: json.loads(p.read_text(encoding="utf-8"))
    for p in sorted((VECTORS / "responses").glob("*.json"))
}
RESPONSE_FILES = {name: doc for name, doc in RESPONSES.items() if "scenarios" in doc}
WEBHOOKS: dict[str, Any] = RESPONSES["checkout.webhook"]
