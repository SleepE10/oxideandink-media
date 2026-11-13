"""Check Buffer for newly published posts and notify a Discord webhook."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

BUFFER_API_URL = "https://api.buffer.com"
BERLIN_TZ = ZoneInfo("Europe/Berlin")

SENT_POSTS_QUERY = """
query GetSentPosts($input: PostsInput!, $first: Int) {
  posts(input: $input, first: $first) {
    edges {
      node {
        id
        status
        channelService
        sentAt
        dueAt
        externalLink
        text
      }
    }
  }
}
"""

SERVICE_ORDER = {"instagram": 0, "tiktok": 1, "youtube": 2}
SERVICE_LABELS = {
    "instagram": "Instagram",
    "tiktok": "TikTok",
    "youtube": "YouTube",
}


def load_local_env_if_needed() -> None:
    """Load secrets from brain/_secrets/ when running locally without env vars."""
    if os.environ.get("BUFFER_API_KEY") and os.environ.get("DISCORD_WEBHOOK_URL"):
        return
    root = Path(__file__).resolve().parents[4]
    for name in ("buffer.env", "discord.env"):
        path = root / "brain" / "_secrets" / name
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def within_publish_window(now_berlin: datetime) -> bool:
    """Return True if local Berlin time is between 17:25 and 20:05."""
    minutes = now_berlin.hour * 60 + now_berlin.minute
    return (17 * 60 + 25) <= minutes <= (20 * 60 + 5)


def fetch_sent_posts(api_key: str, org_id: str, limit: int = 20) -> list[dict]:
    """Fetch recent sent posts from Buffer's GraphQL API."""
    payload = json.dumps(
        {
            "query": SENT_POSTS_QUERY,
            "variables": {
                "input": {"organizationId": org_id, "filter": {"status": ["sent"]}},
                "first": limit,
            },
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        BUFFER_API_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "BufferDiscordNotify/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if data.get("errors"):
        raise RuntimeError(f"Buffer GraphQL error: {data['errors']}")
    edges = data.get("data", {}).get("posts", {}).get("edges", [])
    return [edge["node"] for edge in edges if edge.get("node")]


def parse_iso_utc(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp string into a UTC-aware datetime."""
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def load_state(state_path: Path) -> set[str]:
    """Load previously notified Buffer post IDs."""
    if not state_path.exists():
        return set()
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        return set(data.get("notified_ids", []))
    except (json.JSONDecodeError, OSError):
        return set()


def save_state(state_path: Path, notified_ids: list[str]) -> None:
    """Persist notified Buffer post IDs (capped to the most recent 200)."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"notified_ids": notified_ids[-200:]}
    state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def format_discord_message(posts: list[dict]) -> str:
    """Build a concise Discord message with direct links per platform."""
    ordered = sorted(
        posts,
        key=lambda p: (
            SERVICE_ORDER.get((p.get("channelService") or "").lower(), 99),
            p.get("sentAt") or "",
        ),
    )

    first_text = next((p.get("text") or "" for p in ordered if p.get("text")), "")
    title_line = next((line.strip() for line in first_text.splitlines() if line.strip()), "")
    if len(title_line) > 80:
        title_line = title_line[:77] + "..."

    lines = ["✅ **Obsolescence-Posts sind jetzt live!**"]
    if title_line:
        lines.append(f"> {title_line}")
    lines.append("")

    for post in ordered:
        service = (post.get("channelService") or "unknown").lower()
        label = SERVICE_LABELS.get(service, service.capitalize())
        link = post.get("externalLink")
        if link:
            lines.append(f"• **{label}:** {link}")
        else:
            lines.append(f"• **{label}:** Veröffentlicht (kein Direktlink von Buffer geliefert)")

    return "\n".join(lines)


def send_discord_webhook(webhook_url: str, content: str) -> None:
    """Post a message to a Discord webhook URL."""
    payload = json.dumps({"content": content}).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "BufferDiscordNotify/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status not in (200, 204):
                raise RuntimeError(f"Unexpected Discord status: {resp.status}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Discord webhook failed ({exc.code}): {body}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="Notify Discord when Buffer posts are published.")
    parser.add_argument("--state-file", default=".state/notified_posts.json", help="Path to JSON state file")
    parser.add_argument("--max-age-hours", type=float, default=4.0, help="Ignore sent posts older than N hours")
    parser.add_argument("--force", action="store_true", help="Ignore the 17:30-20:00 Europe/Berlin time window")
    parser.add_argument("--dry-run", action="store_true", help="Print message without posting to Discord or saving state")
    args = parser.parse_args()

    load_local_env_if_needed()

    now_berlin = datetime.now(BERLIN_TZ)
    if not args.force and not within_publish_window(now_berlin):
        print(f"Outside 17:25-20:05 Europe/Berlin window ({now_berlin.strftime('%H:%M')}); skipping.")
        return 0

    api_key = os.environ.get("BUFFER_API_KEY", "").strip()
    org_id = os.environ.get("BUFFER_ORG_ID", "").strip()
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not api_key or not org_id or not webhook_url:
        raise RuntimeError("BUFFER_API_KEY, BUFFER_ORG_ID, and DISCORD_WEBHOOK_URL must be set.")

    state_path = Path(args.state_file)
    notified = load_state(state_path)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=args.max_age_hours)

    sent_posts = fetch_sent_posts(api_key, org_id)
    fresh_posts = []
    for post in sent_posts:
        post_id = post.get("id")
        if not post_id or post_id in notified:
            continue
        sent_at = parse_iso_utc(post.get("sentAt"))
        if sent_at and sent_at < cutoff:
            continue
        fresh_posts.append(post)

    if not fresh_posts:
        print("No new published posts found.")
        return 0

    message = format_discord_message(fresh_posts)
    if args.dry_run:
        print("--- DRY RUN ---")
        print(message)
        return 0

    send_discord_webhook(webhook_url, message)
    ordered_ids = list(notified) + [p["id"] for p in fresh_posts if p["id"] not in notified]
    save_state(state_path, ordered_ids)
    print(f"Notified Discord about {len(fresh_posts)} published post(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
