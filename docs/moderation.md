# Moderation tools

Staff-facing reference for keeping the site usable when someone misbehaves.
Everything here requires `is_staff=True`. None of it is visible to regular
users.

## Where to go

- `/moderation/` — main queue. Tabs for open / actioned / dismissed reports
  and a tail of the recent action log.
- `/moderation/reports/<id>/` — single report, with quick actions scoped to
  the reported object type.
- `/admin/` — Django admin. The `Report`, `ModerationLog`, `Profile`, and
  `Project` change lists have bulk actions.

## What users can report

Logged-in users see a small "Report" link on:

- Projects
- Wiki pages
- Conversation threads and replies
- Use reports
- User profiles

The report form asks for a category (spam, abuse, copyright, off-topic, other)
and an optional freeform reason. Submissions are rate-limited per user (see
`RATELIMIT_REPORT_SUBMIT`).

Reports land in `/moderation/` with status `open`. Acting on a report moves
it to `actioned`; closing one without acting moves it to `dismissed`. Both
end states record the reviewer, timestamp, and an optional note.

## Per-object actions

### Hide a project

From the report detail page or the admin Project change list, choose
"Staff-hide." This sets `Project.is_staff_hidden = True`. The project then
returns 404 for everyone except staff and disappears from listings, search,
and the API. Unhide reverses it.

Use this for projects that need to come down fast but might be legitimate
after edits. It's reversible and leaves the data intact.

### Suspend a user

`/moderation/users/<id>/suspend/` (linked from the report detail page and the
admin Profile row). The form takes a reason and an optional
"also hide their content" checkbox. Submitting it:

- Sets `User.is_active = False`.
- Stamps `Profile.suspended_at` and stores the reason.
- If the box is checked, flips `is_staff_hidden` on every project the user
  created.
- Writes a `ModerationLog` entry.

A suspended user cannot sign in through ORCID. The adapter checks
`suspended_at` directly and rejects with a flash message; it will not
auto-reactivate the account the way it does for users who deactivated
themselves.

### Reinstate a user

Same page, "Reinstate" button. Clears `suspended_at`, sets
`is_active = True`. Does not un-hide their projects; do that separately if
you want to.

### Purge a user's content

`/moderation/users/<id>/purge/`. Destructive and not reversible. The form
requires you to type the username exactly to confirm.

What it deletes:

- Projects where the user is the sole contributor and no other account is
  recorded as creator. Shared projects survive.
- The user's `Contribution` rows on any remaining projects (so their name
  disappears from contributor lists).
- The user's wiki revisions, use reports, conversation threads and replies,
  feedback submissions, and reports they filed.

What it keeps:

- The user account itself (so the audit log still resolves their name).
- Shared projects, minus the purged user's contributor row.
- `ModerationLog` entries about them.

Purge is meant for spam accounts. For real users who behaved badly once,
suspend instead.

## Bulk actions in admin

- **Profile** change list: "Suspend selected users" / "Reinstate selected
  users."
- **Project** change list: "Staff-hide selected projects" / "Unhide selected
  projects."

Both write `ModerationLog` entries for each affected row.

## The audit log

Every staff action writes a `ModerationLog` row: actor, action type, target,
reason, timestamp. View it at `/admin/moderation/moderationlog/` or in the
recent-activity panel at the bottom of `/moderation/`.

The log is append-only from the admin UI; the change/delete buttons are
disabled. If you need to remove an entry, use the Django shell.

## Rate limits

`django-ratelimit` caps how often a single user (or IP, for anonymous
requests) can hit certain write endpoints:

| Setting                       | Default | Applies to                       |
| ----------------------------- | ------- | -------------------------------- |
| `RATELIMIT_PROJECT_CREATE`    | `5/h`   | New project submissions          |
| `RATELIMIT_WIKI_EDIT`         | `30/h`  | Wiki page edits                  |
| `RATELIMIT_CONVERSATION_POST` | `20/h`  | New threads and replies          |
| `RATELIMIT_REPORT_SUBMIT`     | `10/h`  | Report submissions               |
| `RATELIMIT_LOGIN_PER_IP`      | `20/h`  | Sign-in attempts (per source IP) |

Set `RATELIMIT_ENABLE=1` in production. The limiter is off in development
and tests by default. With more than one worker process, configure a shared
cache backend (Redis, memcached, or `db.DatabaseCache`); the default
`LocMemCache` only counts per process and will undercount.

A user who hits a limit sees an HTTP 429.

## ORCID sign-in gate

A staff switch at `/staff/signup-gate/` (linked from the staff dashboard)
narrows who can sign in. When on, an ORCID sign-in is rejected unless the
iD has at least one institution-verified email address set to "Everyone"
visibility; self-asserted emails don't count. Scope is "new accounts only"
(default) or "every sign-in". The allowlist on the same page names ORCID
iDs that always get in; put your own iD there before choosing "every
sign-in". Takes effect on the next sign-in, no restart. Turning it on will
block real researchers who keep their institutional email private, so use
it sparingly.

If the ORCID lookup fails (network error, 5xx), the gate fails closed and
the user sees a "couldn't verify your ORCID record" message. Try again or
add them to the allowlist.

## Quick recipes

- **Obvious spam project, account is new and has nothing else of value:**
  staff-hide the project, suspend the user with "also hide their content,"
  then purge after a short cool-down if no appeal arrives.
- **Disagreement that turned into a flame war in a thread:** dismiss the
  reports with a note. Staff-hiding isn't the right tool for an argument.
- **Copyright claim against a single artifact:** staff-hide the project,
  contact the uploader, unhide once the artifact is removed or the claim is
  cleared.
- **Someone scraped the site and is reposting under different ORCID iDs:**
  turn on the sign-up gate until the wave passes, allowlist any legitimate
  users who get caught.

## Files of interest

- [moderation/models.py](../moderation/models.py) — `Report`, `ModerationLog`.
- [moderation/views.py](../moderation/views.py) — queue, report intake, staff
  actions, purge logic.
- [people/adapters.py](../people/adapters.py) — ORCID sign-in gate and
  suspension check.
- [projects/models.py](../projects/models.py) — `Project.is_staff_hidden`
  and `viewable_by`.
- [osprey/settings.py](../osprey/settings.py) — rate-limit and ORCID-gate
  defaults.
