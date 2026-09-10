"""Server-only configuration; secrets never appear in public errors."""
import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / '.env', encoding='utf-8-sig', override=False)

def setting(name, default=''):
    return os.environ.get(name, default).strip()

def bounded_int(name, default, low, high):
    try:
        return max(low, min(high, int(setting(name, str(default)))))
    except ValueError:
        return default

def database_url():
    url = setting('DATABASE_URL')
    if not url:
        return 'sqlite:///' + str(ROOT / 'gov_tracker.db')
    if url.startswith('postgres://'):
        url = url.replace('postgres://', 'postgresql+psycopg2://', 1)
    elif url.startswith('postgresql://'):
        url = url.replace('postgresql://', 'postgresql+psycopg2://', 1)
    return url
