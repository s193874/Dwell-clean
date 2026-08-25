"""Optional Web Push delivery.

The core backend remains standard-library only.  Installing ``pywebpush`` and
configuring VAPID keys enables delivery; without them subscriptions can be
stored but the API reports that no sender is available.
"""

from __future__ import annotations

import json
import os
from typing import Any

from .store import Database


def configured() -> bool:
    if not (os.environ.get("VAPID_PUBLIC_KEY") and os.environ.get("VAPID_PRIVATE_KEY")):
        return False
    try:
        import pywebpush  # noqa: F401
    except ImportError:
        return False
    return True


def send(
    db: Database,
    title: str,
    body: str,
    url: str = "./",
    only_endpoint: str = "",
) -> dict[str, Any]:
    if not configured():
        return {"configured": False, "sent": 0, "failed": 0}

    from pywebpush import WebPushException, webpush

    sql = "SELECT endpoint,payload FROM push_subscriptions"
    args: tuple[str, ...] = ()
    if only_endpoint:
        sql += " WHERE endpoint=?"
        args = (only_endpoint,)
    rows = db.query(sql, args)
    payload = json.dumps(
        {"title": title[:120], "body": body[:1000], "url": url},
        ensure_ascii=False,
    )
    sent = 0
    failed = 0
    expired: list[str] = []
    for row in rows:
        try:
            subscription = json.loads(row["payload"])
            webpush(
                subscription_info=subscription,
                data=payload,
                vapid_private_key=os.environ["VAPID_PRIVATE_KEY"],
                vapid_claims={
                    "sub": os.environ.get("VAPID_SUBJECT", "mailto:admin@localhost")
                },
                timeout=10,
            )
            sent += 1
        except WebPushException as exc:
            failed += 1
            status = getattr(getattr(exc, "response", None), "status_code", 0)
            if status in (404, 410):
                expired.append(row["endpoint"])
        except (OSError, ValueError, KeyError):
            failed += 1
    for endpoint in expired:
        db.execute("DELETE FROM push_subscriptions WHERE endpoint=?", (endpoint,))
    return {"configured": True, "sent": sent, "failed": failed}
