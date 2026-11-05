"""From the repository root: python -m scripts.export_openapi"""
import json
from pathlib import Path

from server.main import app


if __name__ == "__main__":
    path = Path(__file__).resolve().parents[1] / "api" / "openapi.json"
    path.write_text(json.dumps(app.openapi(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
