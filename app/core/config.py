import os
from typing import Optional
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY: str = os.getenv("SUPABASE_KEY", "")
STORAGE_BUCKET: str = "student_documents"

_supabase_client: Optional[Client] = None


def get_supabase_client() -> Optional[Client]:
    """Supabase istemcisini başlatır; anahtarlar girilmemişse None döner."""
    global _supabase_client
    if _supabase_client is not None:
        return _supabase_client

    if SUPABASE_URL and SUPABASE_KEY and not SUPABASE_URL.startswith("https://PROJE_ID"):
        try:
            _supabase_client = create_client(SUPABASE_URL, SUPABASE_KEY)
        except Exception as exc:
            print(f"[Supabase] Başlatma uyarısı: {exc}")
    return _supabase_client
