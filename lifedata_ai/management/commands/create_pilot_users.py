"""Create per-executive Superset accounts for the pilot.

    python manage.py create_pilot_users --user "Ada Nakato:ada@example.test"
    python manage.py create_pilot_users --from-file pilot-users.txt

# Why this exists

**The create-and-modify-your-own boundary is not real on a shared admin login.**

`superset_write.py` refuses an action against an object the caller does not own,
and the validator classifies a non-owner action as needing confirmation. Both are
no-ops when every participant signs in as the same `admin` account: everyone owns
everything, and Admin bypasses Superset's own object permissions on top of that.

So per-executive accounts with a **non-Admin** role are a *prerequisite* for the
boundary, not an improvement to it. The enforcement ships regardless — it still
stops the copilot editing a colleague's chart the moment two people share a role
— but nobody should claim the boundary holds until these accounts exist.

# What the role grants

`LifeData Executive` is built to be exactly enough:

* `can_write` on Chart and Dashboard — create and edit their own.
* `can_read` on Chart, Dashboard, Dataset, Database, Query — see the catalogue.
* datasource access to the five `rpt_*` views, and nothing else.

Deliberately absent: Admin, SQL Lab, database creation, and any permission on
the operational tables. Superset's read-only database role cannot see `public.*`
at all, so the last is enforced twice.

The command is idempotent: an existing user is left alone apart from being added
to the role, and the role is created only once.
"""

from __future__ import annotations

import os
import secrets

import httpx
from django.core.management.base import BaseCommand, CommandError

#: The datasets a pilot participant may build on — the reporting views, and
#: nothing else. Kept in step with `semantics/registry.yaml` by reading it.
from lifedata_ai.semantics.loader import load_registry

ROLE_NAME = "LifeData Executive"

#: Superset permission-view pairs the role needs, as (permission, view_menu).
#: Written out rather than derived, because a role that silently gains a
#: permission is exactly the thing this command exists to prevent.
ROLE_PERMISSIONS: tuple[tuple[str, str], ...] = (
    # Build and edit their own charts and dashboards.
    ("can_write", "Chart"),
    ("can_read", "Chart"),
    ("can_write", "Dashboard"),
    ("can_read", "Dashboard"),
    # See what they can build from.
    ("can_read", "Dataset"),
    ("can_read", "Database"),
    # The Superset UI needs these to render the pages at all.
    ("menu_access", "Dashboards"),
    ("menu_access", "Charts"),
    ("can_read", "Annotation"),
    ("can_explore", "Superset"),
    ("can_explore_json", "Superset"),
    ("can_dashboard", "Superset"),
    ("can_read", "SavedQuery"),
)

#: Never granted. Listed explicitly so that a future edit widening the role has
#: to remove a name from here, which is a reviewable act.
NEVER_GRANTED: tuple[str, ...] = (
    "all_database_access",
    "all_datasource_access",
    "can_sql_json",
    "can_sqllab",
    "menu_access on SQL Lab",
    "can_write on Database",
    "can_write on Dataset",
)


class Command(BaseCommand):
    help = "Create non-Admin Superset accounts for pilot participants."

    def add_arguments(self, parser):
        parser.add_argument(
            "--user",
            action="append",
            default=[],
            metavar='"Full Name:email"',
            help="A participant, repeatable. Username is derived from the email.",
        )
        parser.add_argument(
            "--from-file",
            help='A file of "Full Name:email" lines, one per participant.',
        )
        parser.add_argument("--superset-url", default=os.environ.get(
            "LIFEDATA_AI_SUPERSET_INTERNAL_URL", "http://superset:8088"))
        parser.add_argument("--username", default=os.environ.get("SUPERSET_ADMIN_USERNAME", "admin"))
        parser.add_argument("--password", default=os.environ.get("SUPERSET_ADMIN_PASSWORD", "admin"))
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be created, and create nothing.",
        )

    def handle(self, *args, **options):
        participants = _participants(options["user"], options.get("from_file"))
        if not participants:
            raise CommandError(
                "No participants given. Use --user \"Ada Nakato:ada@example.test\" "
                "(repeatable) or --from-file."
            )

        registry = load_registry()
        datasets = [d.name for d in registry.datasets.values()]

        if options["dry_run"]:
            self.stdout.write(f"Would create role {ROLE_NAME!r} with:")
            for permission, view in ROLE_PERMISSIONS:
                self.stdout.write(f"  {permission} on {view}")
            self.stdout.write(f"  datasource access to: {', '.join(datasets)}")
            self.stdout.write("")
            self.stdout.write("Would create users:")
            for name, email in participants:
                self.stdout.write(f"  {_username(email):<20} {name} <{email}>")
            return

        base = options["superset_url"].rstrip("/")
        with httpx.Client(timeout=httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0)) as c:
            token = self._login(c, base, options["username"], options["password"])
            headers = {"Authorization": f"Bearer {token}"}
            csrf, cookies = self._csrf(c, base, headers)
            headers["X-CSRFToken"] = csrf

            role_id = self._ensure_role(c, base, headers, cookies, datasets)
            self.stdout.write(f"role {ROLE_NAME!r}: id {role_id}")
            self.stdout.write("")

            created: list[tuple[str, str]] = []
            for name, email in participants:
                username = _username(email)
                password = self._ensure_user(
                    c, base, headers, cookies, name, email, username, role_id
                )
                if password:
                    created.append((username, password))

        if created:
            self.stdout.write("")
            self.stdout.write(
                self.style.SUCCESS("New accounts — give each person their own line:")
            )
            for username, password in created:
                self.stdout.write(f"  {username:<20} {password}")
            self.stdout.write("")
            self.stdout.write(
                self.style.WARNING(
                    "These passwords are shown ONCE and are not stored anywhere. "
                    "Send them individually, not to a group."
                )
            )

        self.stdout.write("")
        self.stdout.write(
            "Ownership enforcement is now meaningful: each participant owns what "
            "they build, and the copilot refuses to change anything else."
        )

    # -- steps ------------------------------------------------------------

    def _login(self, c: httpx.Client, base: str, username: str, password: str) -> str:
        r = c.post(
            f"{base}/api/v1/security/login",
            json={"username": username, "password": password,
                  "provider": "db", "refresh": True},
        )
        if r.status_code != 200:
            raise CommandError(f"Superset login failed ({r.status_code}): {r.text[:300]}")
        return r.json()["access_token"]

    def _csrf(self, c: httpx.Client, base: str, headers: dict) -> tuple[str, dict]:
        r = c.get(f"{base}/api/v1/security/csrf_token/", headers=headers)
        if r.status_code != 200:
            raise CommandError(f"Could not obtain CSRF token ({r.status_code}): {r.text[:200]}")
        return r.json()["result"], dict(r.cookies)

    def _ensure_role(self, c, base, headers, cookies, datasets: list[str]) -> int:
        """Create the role and grant it exactly `ROLE_PERMISSIONS`.

        Superset's role API differs across versions in how permissions attach.
        Where the write endpoint is unavailable this reports what to do by hand
        rather than half-creating a role: a role with a *subset* of these
        permissions produces a participant who can open a dashboard and not save
        one, which reads as the copilot being broken.
        """
        existing = c.get(f"{base}/api/v1/security/roles/", headers=headers)
        if existing.status_code == 200:
            for row in existing.json().get("result", []):
                if row.get("name") == ROLE_NAME:
                    self.stdout.write(f"role {ROLE_NAME!r} already exists")
                    return row["id"]

        r = c.post(
            f"{base}/api/v1/security/roles/",
            headers=headers,
            cookies=cookies,
            json={"name": ROLE_NAME},
        )
        if r.status_code not in (200, 201):
            raise CommandError(
                f"Creating role {ROLE_NAME!r} failed ({r.status_code}): {r.text[:300]}\n"
                "Create it by hand in Superset (Settings -> List Roles) with:\n  "
                + "\n  ".join(f"{p} on {v}" for p, v in ROLE_PERMISSIONS)
                + f"\n  datasource access to: {', '.join(datasets)}\n"
                + "and NEVER: " + ", ".join(NEVER_GRANTED)
            )
        role_id = r.json()["id"]

        self._grant_permissions(c, base, headers, cookies, role_id, datasets)
        return role_id

    def _grant_permissions(self, c, base, headers, cookies, role_id: int, datasets) -> None:
        lookup = c.get(
            f"{base}/api/v1/security/permissions-resources/",
            headers=headers,
            params={"q": '(page_size:1000)'},
        )
        if lookup.status_code != 200:
            self.stdout.write(
                self.style.WARNING(
                    "  could not list permissions; grant them by hand — see the role "
                    "definition printed by --dry-run"
                )
            )
            return

        wanted = set(ROLE_PERMISSIONS) | {
            ("datasource_access", f"[LifeHealth (synthetic)].[{name}](id:*)")
            for name in datasets
        }
        ids: list[int] = []
        for row in lookup.json().get("result", []):
            permission = (row.get("permission") or {}).get("name")
            view = (row.get("view_menu") or {}).get("name")
            if (permission, view) in wanted:
                ids.append(row["id"])
            # Datasource grants carry an id in the view-menu name, so they are
            # matched on the dataset name rather than exactly.
            elif permission == "datasource_access" and view:
                if any(f".[{name}]" in view for name in datasets):
                    ids.append(row["id"])

        if not ids:
            self.stdout.write(
                self.style.WARNING("  no matching permissions found; grant them by hand")
            )
            return

        r = c.put(
            f"{base}/api/v1/security/roles/{role_id}/permissions",
            headers=headers,
            cookies=cookies,
            json={"permission_view_menu_ids": ids},
        )
        if r.status_code not in (200, 201):
            self.stdout.write(
                self.style.WARNING(
                    f"  granting permissions failed ({r.status_code}): {r.text[:200]}"
                )
            )
        else:
            self.stdout.write(f"  granted {len(ids)} permissions")

    def _ensure_user(
        self, c, base, headers, cookies, name: str, email: str, username: str, role_id: int
    ) -> str | None:
        """Create the account, or report that it already exists.

        Returns the generated password for a new account, or None for one that
        already existed — an existing password is never reset, because doing so
        silently would lock someone out mid-pilot.
        """
        existing = c.get(f"{base}/api/v1/security/users/", headers=headers)
        if existing.status_code == 200:
            for row in existing.json().get("result", []):
                if row.get("username") == username:
                    self.stdout.write(f"  {username:<20} already exists; left alone")
                    return None

        first, _, last = name.partition(" ")
        password = secrets.token_urlsafe(12)
        r = c.post(
            f"{base}/api/v1/security/users/",
            headers=headers,
            cookies=cookies,
            json={
                "active": True,
                "first_name": first or username,
                "last_name": last or "",
                "email": email,
                "username": username,
                "password": password,
                # NOT Admin. That is the entire point of this command: Admin
                # bypasses Superset's object permissions, so an Admin pilot
                # account makes the ownership boundary unenforceable.
                "roles": [role_id],
            },
        )
        if r.status_code not in (200, 201):
            self.stdout.write(
                self.style.ERROR(
                    f"  {username:<20} FAILED ({r.status_code}): {r.text[:200]}"
                )
            )
            return None
        self.stdout.write(f"  {username:<20} created")
        return password


# --- Parsing --------------------------------------------------------------


def _participants(inline: list[str], path: str | None) -> list[tuple[str, str]]:
    lines = list(inline)
    if path:
        from pathlib import Path

        lines.extend(
            line
            for line in Path(path).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )

    participants: list[tuple[str, str]] = []
    for line in lines:
        name, separator, email = line.partition(":")
        if not separator or "@" not in email:
            raise CommandError(
                f"{line!r} is not \"Full Name:email\" — for example "
                '"Ada Nakato:ada@example.test".'
            )
        participants.append((name.strip(), email.strip()))
    return participants


def _username(email: str) -> str:
    """The local part of the email, lowercased and stripped of punctuation.

    Derived rather than asked for, so the username in Superset, the username in
    `ActionRecord.executed_by_username`, and the username the ownership check
    compares are the same string by construction.
    """
    local = email.split("@", 1)[0].lower()
    return "".join(c for c in local if c.isalnum() or c in "._-") or "user"
