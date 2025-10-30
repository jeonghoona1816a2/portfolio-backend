# backend/seed_projects.py
import json
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

DOTENV_PATH = Path(__file__).resolve().parent / ".env"
print(f"🔧 .env exists: {DOTENV_PATH.exists()} → {DOTENV_PATH}")
load_dotenv(dotenv_path=DOTENV_PATH, override=True)

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
PROJECT_REF = (os.getenv("SUPABASE_PROJECT_REF") or "").strip()
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL not set in .env")


def mask_url(url: str) -> str:
    try:
        u = urlsplit(url)
        netloc = u.netloc
        if "@" in netloc:
            creds, host = netloc.split("@", 1)
            if ":" in creds:
                user, _ = creds.split(":", 1)
                masked = f"{user}:****@{host}"
            else:
                masked = f"****@{host}"
            return urlunsplit((u.scheme, masked, u.path, u.query, u.fragment))
        return url
    except Exception:
        return "[masked]"


print("🔧 DB URL:", mask_url(DATABASE_URL))
print("🔧 PROJECT_REF:", PROJECT_REF)

connect_args = {}
try:
    parsed = urlsplit(DATABASE_URL)
    has_options = "options=" in (parsed.query or "")
except Exception:
    has_options = False
if PROJECT_REF and not has_options:
    connect_args["options"] = f"project={PROJECT_REF}"
print("🔧 connect_args:", connect_args)

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    future=True,
    connect_args=connect_args,
)

with engine.connect() as conn:
    ok = conn.execute(text("select 1")).scalar() == 1
    print("✅ ping:", ok)

projects_json_path = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "components"
    / "data"
    / "projects.json"
)
if not projects_json_path.exists():
    raise FileNotFoundError(f"projects.json not found: {projects_json_path}")

data = json.loads(projects_json_path.read_text(encoding="utf-8"))


def normalize(item: dict) -> dict:
    title = (item.get("title") or "").strip()
    description = item.get("description")
    detail = item.get("detail") or item.get("details")
    skills = item.get("skills") or []
    images = item.get("images") or item.get("image") or []
    links = item.get("links") or item.get("link") or []

    if isinstance(images, str):
        images = [images]
    if isinstance(links, str):
        links = [links]

    return {
        "title": title,
        "description": description,
        "detail": detail,
        "skills": skills,
        "images": images,
        "links": links,
    }


rows = [normalize(item) for item in data]

with engine.begin() as conn:
    conn.execute(
        text(
            """
            create table if not exists public.projects (
              id bigserial primary key,
              title text not null,
              description text,
              detail text,
              skills jsonb default '[]'::jsonb,
              images jsonb default '[]'::jsonb,
              links jsonb default '[]'::jsonb,
              created_at timestamptz default now()
            );
            """
        )
    )
    conn.execute(text("alter table public.projects add column if not exists detail text"))
    conn.execute(
        text(
            """
            do $$
            begin
              if not exists (
                select 1 from pg_indexes where schemaname='public' and indexname='ux_projects_title'
              ) then
                create unique index ux_projects_title on public.projects ((lower(title)));
              end if;
            end$$;
            """
        )
    )

    for row in rows:
        conn.execute(
            text(
                """
                insert into public.projects (title, description, detail, skills, images, links)
                values (:title, :description, :detail, CAST(:skills AS jsonb), CAST(:images AS jsonb), CAST(:links AS jsonb))
                on conflict ((lower(title))) do update
                  set description = excluded.description,
                      detail      = excluded.detail,
                      skills      = excluded.skills,
                      images      = excluded.images,
                      links       = excluded.links
                """
            ),
            {
                "title": row["title"],
                "description": row["description"],
                "detail": row["detail"],
                "skills": json.dumps(row["skills"], ensure_ascii=False),
                "images": json.dumps(row["images"], ensure_ascii=False),
                "links": json.dumps(row["links"], ensure_ascii=False),
            },
        )

print(f"✅ inserted/updated: {len(rows)} rows")
