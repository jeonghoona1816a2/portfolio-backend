# backend/db.py
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from dotenv import load_dotenv
from sqlalchemy import create_engine

DOTENV_PATH = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=DOTENV_PATH, override=True)

def get_engine():
    db_url = (os.getenv("DATABASE_URL") or "").strip()
    if not db_url:
        raise RuntimeError("DATABASE_URL not set in .env")

    # Avoid duplicating options=project in both URL and connect_args
    project_ref = (os.getenv("SUPABASE_PROJECT_REF") or "").strip()
    connect_args = {}
    try:
        u = urlsplit(db_url)
        has_options_in_url = "options=" in (u.query or "")
    except Exception:
        has_options_in_url = False
    if project_ref and not has_options_in_url:
        connect_args["options"] = f"project={project_ref}"

    # Safe log DB URL without leaking password
    try:
        u = urlsplit(db_url)
        netloc = u.netloc
        if "@" in netloc:
            creds, host = netloc.split("@", 1)
            if ":" in creds:
                user, _ = creds.split(":", 1)
                masked_netloc = f"{user}:****@{host}"
            else:
                masked_netloc = f"****@{host}"
            safe_url = urlunsplit((u.scheme, masked_netloc, u.path, u.query, u.fragment))
        else:
            safe_url = db_url
    except Exception:
        safe_url = "[masked]"
    print("🔧 Using DB URL:", safe_url)
    if connect_args:
        print("🔧 connect_args:", connect_args)

    return create_engine(
        db_url,
        pool_pre_ping=True,
        future=True,
        connect_args=connect_args,
    )

