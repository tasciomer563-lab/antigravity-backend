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


def _ensure_ffmpeg_on_path() -> None:
    """Sistemde ffmpeg yoksa imageio-ffmpeg ikilisini PATH'e ekler (yt-dlp birlestirme/MP3 icin)."""
    import shutil
    import sys

    if shutil.which("ffmpeg"):
        return
    try:
        import imageio_ffmpeg

        src = imageio_ffmpeg.get_ffmpeg_exe()
        shim_dir = os.path.abspath(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".ffmpeg_bin")
        )
        os.makedirs(shim_dir, exist_ok=True)
        name = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
        dst = os.path.join(shim_dir, name)
        if not os.path.exists(dst):
            shutil.copy2(src, dst)
            os.chmod(dst, 0o755)
        os.environ["PATH"] = shim_dir + os.pathsep + os.environ.get("PATH", "")
    except Exception as exc:
        print(f"[ffmpeg] Uyari: {exc}")


_ensure_ffmpeg_on_path()
