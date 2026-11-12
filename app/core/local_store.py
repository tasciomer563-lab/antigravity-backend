import json
import os
import time
import uuid
from typing import Any, Optional

BASE_DATA_DIR = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".local_data")
)
FILES_DIR = os.path.join(BASE_DATA_DIR, "files")
HISTORY_FILE = os.path.join(BASE_DATA_DIR, "history.json")


def _ensure_dirs() -> None:
    os.makedirs(FILES_DIR, exist_ok=True)


def _load_db() -> dict[str, list[dict[str, Any]]]:
    _ensure_dirs()
    if not os.path.exists(HISTORY_FILE):
        return {
            "document_history": [],
            "media_history": [],
            "audio_transcriptions": [],
        }
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
            if isinstance(data, dict):
                return {
                    "document_history": list(data.get("document_history", [])),
                    "media_history": list(data.get("media_history", [])),
                    "audio_transcriptions": list(data.get("audio_transcriptions", [])),
                }
    except Exception:
        pass
    return {
        "document_history": [],
        "media_history": [],
        "audio_transcriptions": [],
    }


def _save_db(db: dict[str, list[dict[str, Any]]]) -> None:
    _ensure_dirs()
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as fh:
            json.dump(db, fh, ensure_ascii=False, indent=2)
    except Exception as exc:
        print(f"[LocalStore] Kayıt uyarısı: {exc}")


def save_local_document(
    file_bytes: bytes,
    file_name: str,
    file_type: str,
    cloud_url: Optional[str] = None,
) -> dict[str, Any]:
    """Dönüştürülen belgeyi yerel diske ve geçmiş listesine kaydeder (maksimum son 50 kayıt)."""
    _ensure_dirs()
    safe_id = uuid.uuid4().hex[:10]
    safe_ext = file_type.lower().lstrip(".")
    stored_filename = f"{safe_id}.{safe_ext}"
    file_path = os.path.join(FILES_DIR, stored_filename)

    try:
        with open(file_path, "wb") as fh:
            fh.write(file_bytes)
    except Exception as exc:
        print(f"[LocalStore] Dosya yazma uyarısı: {exc}")

    size_mb = round(len(file_bytes) / (1024 * 1024), 3)
    local_url = f"/api/document/files/{stored_filename}"
    entry = {
        "id": safe_id,
        "file_name": file_name,
        "file_type": safe_ext,
        "file_size_mb": size_mb,
        "file_url": cloud_url or local_url,
        "local_url": local_url,
        "stored_filename": stored_filename,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    db = _load_db()
    docs = db.get("document_history", [])
    docs.insert(0, entry)
    db["document_history"] = docs[:50]
    _save_db(db)
    return entry


def get_local_file_path(stored_filename: str) -> Optional[str]:
    _ensure_dirs()
    clean = os.path.basename(stored_filename)
    full = os.path.join(FILES_DIR, clean)
    if os.path.isfile(full):
        return full
    return None


def add_history_entry(table: str, payload: dict[str, Any]) -> dict[str, Any]:
    db = _load_db()
    items = db.get(table, [])
    entry = {
        "id": uuid.uuid4().hex[:10],
        **payload,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    items.insert(0, entry)
    db[table] = items[:50]
    _save_db(db)
    return entry


def get_history_entries(table: str) -> list[dict[str, Any]]:
    db = _load_db()
    return db.get(table, [])
