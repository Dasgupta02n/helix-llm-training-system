"""Check Resend API key + domain status (no secrets printed)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx


def main() -> None:
    env = Path("data/hostinger_deploy.env").read_text(encoding="utf-8")
    key = ""
    from_addr = ""
    for line in env.splitlines():
        if line.startswith("RESEND_API_KEY="):
            key = line.split("=", 1)[1].strip()
        if line.startswith("RESEND_FROM_EMAIL="):
            from_addr = line.split("=", 1)[1].strip()
    print("key_present", bool(key))
    print("from", from_addr)
    if not key:
        raise SystemExit(1)
    headers = {"Authorization": f"Bearer {key}"}
    r = httpx.get("https://api.resend.com/domains", headers=headers, timeout=30)
    print("domains_http", r.status_code)
    r.raise_for_status()
    domains = r.json().get("data") or []
    for d in domains:
        print(
            "domain",
            d.get("name"),
            "status",
            d.get("status"),
            "region",
            d.get("region"),
            "id",
            d.get("id"),
        )
        did = d.get("id")
        if not did:
            continue
        r2 = httpx.get(f"https://api.resend.com/domains/{did}", headers=headers, timeout=30)
        print("detail_http", r2.status_code)
        if r2.status_code >= 400:
            print("detail_err", r2.text[:300])
            continue
        det = r2.json()
        # Resend may nest under data
        body = det.get("data") if isinstance(det.get("data"), dict) else det
        print("detail_status", body.get("status"))
        records = body.get("records") or []
        for rec in records:
            print(
                "  record",
                rec.get("record"),
                rec.get("type"),
                rec.get("name"),
                "status=",
                rec.get("status"),
            )


if __name__ == "__main__":
    main()
