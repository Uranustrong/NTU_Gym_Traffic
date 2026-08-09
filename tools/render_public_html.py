#!/usr/bin/env python3
"""Build the public gym dashboard as a static page that fetches its own data.

This is the public-facing counterpart to the Grafana dashboard, which is only
reachable on 127.0.0.1 and so cannot be shown to outside visitors directly.

It used to run every five minutes, query Postgres eight times, and bake the
results into index.html -- which meant the page could only ever be as fresh as
its last deploy. It now writes a shell instead: the browser calls
`public.gym_heatmap()` and `public.gym_live()` on Supabase directly, so the
page is deployed once and stays live on its own. This script therefore needs no
database credentials at all and only runs when the code changes.

What it still bakes in is configuration, never data:

  - the ECharts panel code, read at runtime out of grafana/weekly-dashboard.json
    so editing a panel in Grafana flows into the public page on the next build;
  - the Supabase endpoint and its publishable key, which is public by design;
  - the Chinese-to-English venue names, which are presentation and have no
    business living in the database.

The SQL that used to be restated here now lives in
sql/migrations/005_public_api.sql, where the Grafana dashboard reads it too.

No third-party Python packages.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import shutil
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
DASHBOARD_JSON = REPO_DIR / "grafana" / "weekly-dashboard.json"
TEMPLATE_HTML = REPO_DIR / "tools" / "public_template.html"
ECHARTS_JS = REPO_DIR / "tools" / "echarts.min.js"

# Public-page-only display names. The database and every RPC use the real
# (Chinese) venue strings as keys; only the labels shown to a visitor are
# translated, so Grafana keeps showing the original names. Unmapped venues
# fall through unchanged.
VENUE_DISPLAY = {
    "健身中心": "Fitness Center",
    "室內游泳池": "Indoor Pool",
}


def _jwt_role(key: str) -> str | None:
    """Read the `role` claim out of a legacy Supabase JWT, or None if it is not one.

    No signature check and none needed: this is not authenticating anything,
    it is reading what the key says about itself so an obviously wrong one can
    be refused before it is published.
    """
    parts = key.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)          # base64url is unpadded
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    role = claims.get("role")
    return role if isinstance(role, str) else None


def check_publishable_key(key: str) -> str | None:
    """Return an error message if this key must not be baked into a public page.

    The whole point of this function is that the failure it prevents is silent
    and irreversible: a secret or service_role key bypasses every RLS policy,
    and pasting one into the wrong variable would publish it to the open web
    inside index.html. Length alone cannot tell the two apart -- a secret key
    is comfortably longer than the minimum -- so check what the key actually
    claims to be.

    Never put the key itself in the message; only its prefix or its role.
    """
    if key.startswith("sb_secret_"):
        return ("that is a SECRET key (sb_secret_...). It bypasses row-level "
                "security and must never reach a browser. Use the publishable key.")
    if key.startswith("sb_publishable_"):
        return None
    role = _jwt_role(key)
    if role == "anon":
        return None
    if role is not None:
        return (f"that JWT carries role={role!r}. Only the anon key may be "
                "published; a service_role key bypasses row-level security.")
    return (f"unrecognised key format (starts {key[:12]!r}). Expected "
            "sb_publishable_... or a legacy anon JWT.")


def extract_panel_js() -> dict[str, str]:
    """Pull the four panels' getOption JS bodies out of weekly-dashboard.json."""
    dash = json.loads(DASHBOARD_JSON.read_text())
    return {str(panel["id"]): panel["options"]["getOption"] for panel in dash["panels"]}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--output-dir", required=True,
                   help="Directory to write index.html into (e.g. _site).")
    # Defaults come from the environment rather than being required, so
    # `set -a; . ./supabase.secrets; set +a` is enough to build locally --
    # otherwise putting them in that file would have no effect.
    p.add_argument("--supabase-url", default=os.environ.get("SUPABASE_URL"),
                   help="Supabase project URL. Default: $SUPABASE_URL.")
    p.add_argument("--supabase-key",
                   default=(os.environ.get("SUPABASE_PUBLISHABLE_KEY")
                            or os.environ.get("SUPABASE_ANON_KEY")),
                   help="Publishable key, public by design. Default: "
                        "$SUPABASE_PUBLISHABLE_KEY, then $SUPABASE_ANON_KEY.")
    p.add_argument("--days", type=int, default=7,
                   help="Trailing window for the panel-3 line chart (default: 7).")
    p.add_argument("--refresh-seconds", type=int, default=300,
                   help="How often the page re-fetches live data (default: 300).")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    # Fail here rather than shipping a page that 401s in every visitor's
    # browser: an empty key still produces perfectly valid-looking HTML.
    missing = [name for name, value in
               (("--supabase-url / SUPABASE_URL", args.supabase_url),
                ("--supabase-key / SUPABASE_PUBLISHABLE_KEY", args.supabase_key))
               if not value]
    if missing:
        print("error: missing required value(s): " + ", ".join(missing), file=sys.stderr)
        return 2

    # Refuse before writing anything. Publishing the wrong key is not a bug you
    # get to fix quietly afterwards -- it is on the open web and has to be
    # rotated.
    key_problem = check_publishable_key(args.supabase_key)
    if key_problem:
        print(f"error: refusing to embed this key -- {key_problem}", file=sys.stderr)
        return 2
    if args.days < 1 or args.days > 31:
        print(f"error: --days must be between 1 and 31 (got {args.days}); "
              "the server clamps to that range anyway", file=sys.stderr)
        return 2

    config = {
        "supabaseUrl": args.supabase_url.rstrip("/"),
        "supabaseKey": args.supabase_key,
        "venueDisplay": VENUE_DISPLAY,
        "panelJs": extract_panel_js(),
        "days": args.days,
        "refreshSeconds": args.refresh_seconds,
    }

    html = TEMPLATE_HTML.read_text()
    html = html.replace("{{CONFIG_JSON}}",
                        json.dumps(config, ensure_ascii=False, separators=(",", ":")))
    if "{{" in html:
        leftover = html[html.index("{{"):html.index("{{") + 40]
        print(f"error: unsubstituted placeholder in template near {leftover!r}", file=sys.stderr)
        return 1

    output_dir = Path(args.output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / "index.html"
    # Write then rename, so a web server never serves a half-written page.
    tmp_path = output_dir / ".index.html.tmp"
    tmp_path.write_text(html, encoding="utf-8")
    tmp_path.replace(out_path)

    # Ship the ECharts bundle alongside index.html so the page depends on no
    # external CDN. Copy only when missing or a different size.
    echarts_dst = output_dir / "echarts.min.js"
    if not echarts_dst.exists() or echarts_dst.stat().st_size != ECHARTS_JS.stat().st_size:
        shutil.copy2(ECHARTS_JS, echarts_dst)
        print(f"copied {echarts_dst.name} ({ECHARTS_JS.stat().st_size:,} bytes)")

    print(f"wrote {out_path} ({len(html):,} bytes, "
          f"{len(config['panelJs'])} panels, {args.days}-day window)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
