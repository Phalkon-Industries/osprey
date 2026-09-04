# OSPREY

**Open Science Platform for Research and Engineering Yields.** A public-benefit research
engineering commons run by [pHalkon Industries LLC](https://phalkon.example).


## Quick start (development)

Requires Docker Engine and the Compose plugin.

```bash
cp .env.example .env
docker compose up --build
# in another terminal:
docker compose run --rm web python manage.py migrate
```

The site is served at <http://localhost:8000/>. The Django admin is at
`/admin/`. The auto-generated OpenAPI schema is at `/api/v1/docs`.
User-facing sign-in is ORCID-only; local ORCID testing needs a registered public
HTTPS callback host, usually a tunnel or the deployed beta site.

For ordinary local development, keep using <http://localhost:8000/> for public
pages, templates, forms, and API work. Anything that needs a browser session has
two paths:

- For the real ORCID flow, expose local Django through a public HTTPS tunnel or
  development hostname and register that exact callback URL with ORCID.
- For local automated checks, use Django's test client with `force_login()` and
  a fixture user that has an ORCID value on its profile. That tests OSPREY's
  logged-in views without testing ORCID's redirect screen.

There is no private-localhost version of the ORCID redirect. ORCID has to send
the browser back to a URL it accepts, so the OAuth handoff needs a public HTTPS
callback.

The standing local dev callback is:

```text
https://ospreydev.phalkon.io/accounts/orcid/login/callback/
```

When using the Cloudflare tunnel, make sure local `.env` includes:

```env
DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1,ospreydev.phalkon.io
DJANGO_CSRF_TRUSTED_ORIGINS=http://localhost:8000,https://ospreydev.phalkon.io
ORCID_USE_SANDBOX=0
ORCID_CLIENT_ID=<orcid application id>
ORCID_CLIENT_SECRET=<orcid secret>
```

After changing `.env`, restart the web container so Django sees the new values.
In Cloudflare's public hostname settings, point `ospreydev.phalkon.io` to
`http://localhost:8000`, not `https://localhost:8000`. Cloudflare provides the
public HTTPS endpoint; the local Django development server speaks HTTP.

## Zenodo sandbox smoke test

The project detail page can create or update a Zenodo draft from an OSPREY
project, upload a generated ZIP snapshot, and publish that record in the
Zenodo sandbox. It uses a personal access token from `.env`; the token is never
committed.

```env
ZENODO_USE_SANDBOX=1
ZENODO_ACCESS_TOKEN=<sandbox personal access token>
```

Restart the web container after editing `.env`, then run:

```bash
sg docker -c 'docker compose exec -T --user 1000:1000 -w /app web python manage.py zenodo_sandbox_smoke <project-slug>'
```

To publish the sandbox draft too:

```bash
sg docker -c 'docker compose exec -T --user 1000:1000 -w /app web python manage.py zenodo_sandbox_smoke <project-slug> --publish'
```

The command prints the deposition id, DOI, and sandbox URL. Do not use a real
Zenodo token with `ZENODO_USE_SANDBOX=1`; sandbox and production accounts are
separate.

## Local test suite

Run the local suite before pushing and after feature-sized changes:

```bash
sg docker -c './scripts/test_local.sh'
```

That runs Django's system checks, verifies there are no missing migrations,
executes the Django test suite under coverage, and prints a coverage report.
The fuller testing plan is in [planning/testing-plan.md](planning/testing-plan.md).

If your host UID is not 1000, prefix `run` and `exec` commands with
`--user $(id -u):$(id -g)` so files created inside the container are owned by
your host user.

## Repository layout

```
manage.py
requirements.txt
Dockerfile, docker-compose.yml, docker-compose.prod.yml
osprey/                 Django project package (settings, urls, wsgi)
core/                   Shared templates, home and about views
projects/               Projects, contributions, artifact links, cover images
people/                 Users (auth.User), profiles, free-text institutions
api/                    Django Ninja API at /api/v1/
feedback/               In-page feedback widget and staff feedback inbox
deploy/                 Production runbook and nginx site config
planning/               Source-of-truth planning documents
```

## What the demo does

- Browse and search a small body of pre-loaded projects.
- View project pages with descriptions, contributors, cover images, and artifact
  links back to the upstream repositories or archives.
- Sign in with ORCID for verified submitter identity. Contributor rows can name
  other people now; their ORCID iDs are added only after they authenticate.
- Capture closed-beta feedback from logged-in users through the floating
  feedback widget. Staff can review it at `/feedback/review/` or in the
  Django admin.

## Roadmap

What OSPREY can do today and where it is heading. If you have a
suggestion, open an issue or get in touch directly.

### What works today

- **Sign in with ORCID.** Contributions are tied to a verified researcher
  identity.
- **Project pages.** Cover image, summary, full description, contributor
  list, tags, and links to the source repository.
- **Citable DOIs.** Publishing a project deposits a snapshot to
  [Zenodo](https://zenodo.org) and mints a DOI.
- **New versions.** Significant updates get their own version DOI; the
  project DOI keeps pointing at whatever is latest.
- **Contributor credit.** Contributors are listed with a role and a credit
  statement, with an inline ORCID search to find the right person.
- **Project wikis.** Per-project documentation with suggested edits and a
  review queue with side-by-side diffs.
- **Use reports.** Structured "I used this and here's what happened" notes
  from other researchers.
- **Project discussions.** Question-and-answer threads with accepted
  answers.
- **Reporting and moderation.** Users can report content; staff have a
  real moderation queue.
- **Basic browsing and search**, plus profile pages for every
  ORCID-verified user.

### Near-term

Loosely in order: notifications and opt-in email (in-app inbox, following
creators, watching projects), contributor claiming (invite someone to
verify a credit row), tag suggestions, search and filter improvements, a
feature documentation page, and save/collect with named collections.

### Later

Live citation tracking so upstream projects get credit for downstream
citations, and visible lineage (fork, derivation, and version
relationships between projects).

### Long-term

Federation: institutions running their own OSPREY instances that
replicate publicly among themselves. Not being built yet; the near-term
work is keeping the data model from foreclosing it (stable URIs, globally
unique identifiers, origin tracking). If running a federated instance
interests you, get in touch.

## Deploy

See [docs/deployment.md](docs/deployment.md).
