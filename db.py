from typing import Any
import requests
from config import SUPABASE_URL, SUPABASE_ANON_KEY, SUPABASE_SERVICE_ROLE_KEY, REQUEST_TIMEOUT

def supabase_enabled() -> bool:
    return bool(SUPABASE_URL and SUPABASE_ANON_KEY)

def supabase_get(
    table: str,
    params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if not supabase_enabled():
        return []

    headers = {
        "apikey": SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
    }

    url = f"{SUPABASE_URL}/rest/v1/{table}"

    response = requests.get(
        url,
        headers=headers,
        params=params or {},
        timeout=REQUEST_TIMEOUT,
    )

    response.raise_for_status()
    return response.json()

def _supabase_write_key() -> str:
    return SUPABASE_SERVICE_ROLE_KEY or SUPABASE_ANON_KEY

def supabase_write_enabled() -> bool:
    return bool(SUPABASE_URL and _supabase_write_key())

def supabase_upsert(table: str, rows: list[dict[str, Any]], on_conflict: str) -> None:
    if not rows or not supabase_write_enabled():
        return
    key = _supabase_write_key()
    response = requests.post(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
        params={"on_conflict": on_conflict},
        json=rows,
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()

def supabase_patch(table: str, filters: dict[str, str], values: dict[str, Any]) -> None:
    if not values or not supabase_write_enabled():
        return
    key = _supabase_write_key()
    response = requests.patch(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        },
        params=filters,
        json=values,
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
