# Copilot instructions for the OSPREY repo

OSPREY is a public-benefit research engineering commons. The repo contains both
planning/outreach Markdown and a working Django demo application. These
instructions cover both.

## Project context

- **Working name:** OSPREY — *Open Science Platform for Research and Engineering.* Acronym is OSPRE; the trailing Y keeps the full bird word as the brand.
- **Founder:** Jonathan A Pfeifer (research engineer, embedded systems and CO₂ sensors, currently at WHOI).
- **Legal home:** pHalkon Industries LLC, running OSPREY as a public-benefit project.
- **Starting community:** oceanographic engineering at WHOI.
- **Status:** demo application running locally in Docker. Closed beta planned with WHOI partners.

## Source-of-truth documents

Read these before suggesting changes that touch their domain:

- [planning/vision.md](../planning/vision.md) — the why, future picture, and pump anecdote. Narrative source of truth.
- [planning/concept.md](../planning/concept.md) — what OSPREY is. Spec source of truth.
- [planning/implementation-plan.md](../planning/implementation-plan.md) — phases, milestones, support, risks. Plan source of truth.
- [planning/demo-architecture.md](../planning/demo-architecture.md) — technical blueprint for the demo. Source of truth for stack and deploy.
- [planning/features/](../planning/features/) — per-feature specs (`citations.md`, `contributor-stubs.md`, `federation.md`, etc.). Source of truth for individual feature scope.
- [planning/trademark-guide.md](../planning/trademark-guide.md) — trademark strategy and class recommendations.
- [planning/pose-notes.md](../planning/pose-notes.md) — NSF POSE analysis and adjacent funding programs.
- [planning/ai-writing-tells.md](../planning/ai-writing-tells.md) — what to avoid in writing. **Always consult before producing prose.**

Composed documents (derived from sources, not sources themselves):

- [planning/one-pager.md](../planning/one-pager.md) — for cold first-contact emails.
- [planning/pitch-deck-outline.md](../planning/pitch-deck-outline.md) — the slide outline.

A document is one or the other, not both.

## Writing rules

These exist because the founder cares about not sounding like an LLM. **Apply them on every prose change.**

1. **Run the AI-tells checklist on every draft.** [planning/ai-writing-tells.md](../planning/ai-writing-tells.md) is the canonical list. Specific words and patterns to avoid: load-bearing, first-class, paved path, moat, crucially, importantly, simply, clearly, obviously, journey, unlock, leverage, robust, seamless, holistic, "in short," "bottom line," silently, quietly, "invisible tax," "the only question," "either way," "compounds" (as a verb), first-order, "message in a bottle." Cut "let me," "I'll now," and similar stage directions.
2. **First person singular.** This is one person's project. "I am building," not "we are building." The "we" appears only when literally inviting partners into the project.
3. **No em-dash overuse.** Em-dashes are reserved for headings and the rare genuine aside. Most uses should be commas, parentheses, or two sentences.
4. **No three-bullet parallelism reflex.** Vary list lengths. Some ideas are sentences, not bullets. Not every bullet starts with a bolded phrase.
5. **No "not X, it is Y" rhetorical flourishes.** No section-ending punchy one-liners. No rhetorical questions used as transitions.
6. **Invitation tone, not pitch tone.** OSPREY is asking people to help build something, not asking them to invest. "Help me build this" beats "fund my company" every time.
7. **No grand pronouncements without specifics.** "This will transform science" is forbidden. If a claim is grand, ground it.
8. **Mild hedging where honest, no over-hedging.** Real risks have partial mitigations and real mitigations carry their own risks. Say so. Don't pretend either side is tidy.
9. **Tonal variety.** A human writer shifts register. Dry, then a joke, then an aside. Even-keeled solemn paragraphs read generated.
10. **Contractions are fine.** Use them where natural.

## Document architecture rules

- A source-of-truth document is the single place a fact lives. A composed document references but does not redefine.
- Naming, branding, expansion: the current expansion is *Open Science Platform for Research and Engineering*. Do not reintroduce "Yields."
- Numbers (funding scales, milestones, runway): source is [planning/implementation-plan.md](../planning/implementation-plan.md). Pitch deck appendix may restate, never invent.
- The pump story exists in [planning/vision.md](../planning/vision.md) and the one-pager. If reused elsewhere, vary the phrasing slightly.

## Naming and language conventions

- **OSPREY** in all caps when used as the project name. The bird (lowercase "osprey") only when literally the bird.
- **pHalkon Industries LLC** (lowercase p, uppercase H). Not "Phalkon," not "PHalkon."
- **Jonathan A Pfeifer.** Last name is "Pfeifer."
- **WHOI** for Woods Hole Oceanographic Institution (no periods).
- **ORCID, DOI, OSS** capitalized.
- "Research engineering" (lowercase, two words) is the field. "Research engineer" is the role.

## Repo layout

Top-level Django project:

- `osprey/` — Django settings, root URLconf.
- `core/` — base templates, home/about/license-guide views, theme cookie, shared CSS at `core/static/css/osprey.css`.
- `projects/` — `Project`, `Contribution`, `Tag`, `TagAssignment`, `ArtifactLink`, `ProjectImage`. Project list/detail/new/edit views and templates. Markdown templatetag at `projects/templatetags/osprey_md.py` (allows external `http(s)` images). Old `Citation` and `LineageEdge` models may still exist in migrations/models, but the demo UI/API does not expose them right now.
- `people/` — `Profile` (one-to-one with `auth.User`), profile edit view, person detail view, institution detail view (free-text institutions, slug-matched).
- `api/` — django-ninja API at `/api/v1/`, including a `/api/v1/export/` JSON dump for the survivable-data principle.
- `feedback/` — floating in-page feedback widget (logged-in only). Captures message, page URL, browser, OS, viewport, optional annotated screenshot. Reviewed in Django admin.
- `scripts/smoke_seed.py` — seed script for local demo data (alice, bob, two pump projects).
- `deploy/`, `Dockerfile`, `docker-compose.yml`, `docker-compose.prod.yml`, `deploy.sh` — deployment.
- `media/` — user uploads (avatars, project images, feedback screenshots). Must be writable by container UID 1000.
- `planning/` — Markdown docs (source of truth for vision/concept/architecture).

### Stack

- Python 3.12, Django 5.1.x, django-ninja for the JSON API, Pillow for image handling, Postgres 16.
- Vanilla server-rendered HTML + a small amount of plain JS for theme toggle, license-other reveal, and the feedback widget. No frontend framework.
- Vendored JS only (e.g. `feedback/static/feedback/html2canvas-pro.min.js`). No CDN dependencies at runtime.

### Auth and identity

- User-facing accounts are ORCID-only. ORCID sign-in uses django-allauth at `/accounts/orcid/login/`. Staff access is granted to ORCID-backed users.
- ORCID config lives in env vars: `ORCID_USE_SANDBOX`, `ORCID_CLIENT_ID`, `ORCID_CLIENT_SECRET`. Production ORCID is acceptable for real sign-in-only beta testing once the callback URI is registered; sandbox remains available for fake-account testing.
- Production URL is `https://osprey.phalkon.io/`. ORCID may reject `localhost` redirect URIs, so local ORCID testing needs a public HTTPS tunnel or real dev hostname.
- ORCID iDs must not be manually entered. They are collected only through ORCID OAuth. The current long-form source is [planning/features/orcid-signin.md](../planning/features/orcid-signin.md).
- Per-user `Profile` adds `display_name`, `bio`, `avatar`, `institution` (free text), `orcid_placeholder` (old field name; should be populated from ORCID sign-in, not manual profile editing).
- Local `auth.User.username` is the public @ user tag. New ORCID accounts get a full-name-based tag (e.g. `@jonathanapfeifer`); a four-digit suffix is appended only when the bare tag is already taken. The suggested tag can be adjusted exactly once, on the first-login onboarding page at `/people/me/welcome/`. Saving onboarding locks the tag, and the regular `/people/me/edit/` page never offers it as an editable field.
- User-facing account deletion should deactivate the account, not hard-delete it, so public credit records and profile pages survive. A later sign-in with the same ORCID can reactivate the account.
- Contribution rows (`projects.Contribution`) carry `display_name`, free-text `role`, optional `credit_statement`, optional `user` FK, and optional `orcid_id`. Blank ORCID is allowed for named/unverified contributors. Publishing requires the submitter to be ORCID verified.

### Institutions

- Stored as free text on `Project.institution` and `Profile.institution` (`CharField(max_length=200)`).
- The `/institutions/<slug>/` view slugifies all project institution strings and matches; the most-common spelling becomes the page heading.
- Deduping is a future admin task; do not reintroduce the `Institution` model.

### Licenses

- `RECOMMENDED_LICENSES` and `OTHER_LICENSES` in `projects/forms.py`. OSPREY is open-source-only; do **not** add proprietary or "all rights reserved" options.
- Users can pick "Other (write below)" and type a custom SPDX/OSHWA/Creative Commons identifier.
- License guide page lives at `/about/licenses/`.

### Cover images

- `Project.cover_image_url` is a free-text URL to an image hosted elsewhere (GitHub raw URL, Zenodo, lab site). OSPREY does not host cover images.
- Cards and the project hero fall back to `ProjectImage` uploads if the cover URL is empty.

### Feedback widget

- Lives in `feedback/`. Mounted in `core/templates/base.html` only when `request.user.is_authenticated` and the path is not under `/admin/`.
- Static at `feedback/static/feedback/{widget.js,widget.css,html2canvas-pro.min.js}`. Vendored html2canvas-pro 1.5.8 (MIT) — a maintained fork that handles modern CSS color functions like `color(srgb …)` and `oklch()` that the unmaintained html2canvas 1.4.1 chokes on.
- Submission endpoint `/feedback/submit/` (login-required, POST-only). Stores: user, message, page URL, page title, User-Agent, coarse browser/OS, viewport, optional PNG screenshot (data-URL decoded server-side, capped at 4 MiB).
- Review feedback at `/feedback/review/` (staff-only) or through Django admin at `feedback.Feedback`. Status field: new / triaged / resolved / wontfix. Admin notes live on the model and are not shown to submitters.

### Templates (gotcha)

The VS Code HTML formatter sometimes splits Django tags across two lines, which Django can't parse. **Always keep `{% endif %}`, `{% endfor %}`, `{% endblock %}`, `{% endwith %}` on a single line.** When writing inline conditionals like `{% if x.y > 3 %}{% endif %}`, prefer precomputing the comparison value in a `{% with %}` block to keep the line short and unwrappable.

## Working with the demo

### Run management commands

```bash
sg docker -c 'docker compose exec -T --user 1000:1000 -w /app web python manage.py <command>'
```

Common ones: `check`, `migrate`, `makemigrations <app>`, `shell`, `collectstatic`.

### Local tests and TDD

Use a test-driven development flow for Django application changes. Before
implementing a feature or behavior change, add or update the focused tests that
describe the intended behavior. Run the focused test or app test first when that
is practical, then implement the code, rerun the focused test, and finish with
the local suite before calling the work done.

Default local suite:

```bash
sg docker -c './scripts/test_local.sh'
```

For narrow work, run the smallest relevant test target during the loop, for
example `python manage.py test projects.tests.ProjectViewTests` inside the web
container. The fuller local testing plan lives in
[planning/testing-plan.md](../planning/testing-plan.md).

### Seed data

```bash
sg docker -c 'docker compose exec -T --user 1000:1000 -w /app web python manage.py shell < scripts/smoke_seed.py'
```

Seed creates `alice` and `bob` with unusable passwords, one parent and one child Pump project at WHOI, a few tags, contributors, artifact links, and demo images.

### Migration patterns

When changing a `ForeignKey` to `CharField` (or similar with data preservation), use the three-step pattern: add a nullable shadow field, `RunPython` to copy values forward, drop the FK, rename the shadow field. The institution change in `projects/migrations/0006_project_institution_text.py` and `people/migrations/0004_profile_institution_text.py` is the working reference.

### Media folder permissions

`media/` and its subfolders must be owned by UID 1000 (the container user) so uploads (avatars, project images, feedback screenshots) can be written. If a 500 mentions `PermissionError: '/app/media/...'`, run:

```bash
sg docker -c 'docker compose exec -T -w /app web chown -R 1000:1000 media'
```

## Coding principles

- **Survivable, thin, boring.** Stable well-understood tools. No exotic dependencies the next maintainer would have to learn.
- **Federation over hosting.** OSPREY is a curation and credit layer on top of GitHub/Codeberg/Zenodo. The demo should not become a primary file host. Cover images are URLs, not uploads.
- **Survivable data.** Anything OSPREY stores must be exportable as plain data via `/api/v1/export/`. A successor community should be able to reconstruct from a public export plus the upstream repositories.
- **DOIs/citations/lineage:** real DOI minting, citation export, and lineage graphs are paused in the demo UI/API until the design has better example projects. Do not reintroduce placeholder DOI surfaces without checking [planning/demo-architecture.md](../planning/demo-architecture.md).
- **Founder's coding background:** strong in CS and electrical engineering, embedded systems, CO₂ sensors. New to full-stack web development. Explain web-specific concepts (deployment, CORS, sessions, ORMs, migrations, reverse proxies, certificate management) when they come up.

## Things to push back on

If a request would do any of the following, surface the concern before complying:

- Reintroduce dropped language ("Yields," "message in a bottle," "invisible tax," "load-bearing," etc.).
- Add a proprietary license to the license dropdown. OSPREY is open-source-only.
- Bring back the `Institution` model. Institutions are free text now.
- Move a fact out of its source-of-truth document without updating the source.
- Add VC-pitch tone to documents the founder has explicitly framed as invitations.
- Build a primary file host into OSPREY rather than layering over existing repositories.
- Adopt token-based or DAO-style governance. The governance direction is pluralistic, credential-weighted, ORCID-based, and explicitly *not* a token DAO.
- Promise free read access forever. The current language is deliberately softened.
- Pad scope with features that drift from the curation/credit/lineage core (peer review, CI runners, CAD rendering, compliance dashboards). Those are explicit non-goals for now.
- Add CDN dependencies to the demo. Vendor JS/CSS into `static/` instead.

## Tooling

- The user runs Linux (WSL/Ubuntu) and uses `bash`. Default to bash-correct commands. Older notes mentioning PowerShell are stale.
- The `sg docker -c '...'` wrapper is needed because the user is in the `docker` group via `sg`, not the primary group.
- Git author name is "Jonathan" with a noreply email. Do not change git config.

## When unsure

Ask. The founder prefers a clarifying question over a confident wrong implementation, and prefers small reversible steps over large speculative ones. After completing prose work, do an explicit AI-tells review pass before declaring done.
