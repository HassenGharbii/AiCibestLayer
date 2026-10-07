from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
TX2_DIR = APP_DIR.parent
PROJECT_ROOT = TX2_DIR.parent

# Reuses the same frontend as the main backend — it's plain HTML/CSS/JS with
# no coupling to which backend variant is serving it.
FRONTEND_DIR = PROJECT_ROOT / "frontend"

UPLOAD_DIR = TX2_DIR / "uploads"
WEIGHTS_DIR = TX2_DIR / "weights"


def resolve_upload_path(file_id: str) -> Path:
    """Resolve an uploaded-file id to a path, rejecting anything outside UPLOAD_DIR."""
    candidate = (UPLOAD_DIR / file_id).resolve()
    upload_root = UPLOAD_DIR.resolve()
    if upload_root != candidate and upload_root not in candidate.parents:
        raise ValueError("invalid file reference")
    return candidate
