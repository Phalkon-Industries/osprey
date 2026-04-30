# OSPREY

**Open Science Platform for Research and Engineering.** A public-benefit research
engineering commons run by [pHalkon Industries LLC](https://phalkon.example).

This repository holds the planning documents in `planning/` and the demo web
application that implements the architecture described in
[planning/demo-architecture.md](planning/demo-architecture.md).

## Quick start (development)

Requires Docker Engine and the Compose plugin.

```bash
cp .env.example .env
docker compose up --build
# in another terminal:
docker compose run --rm web python manage.py migrate
docker compose run --rm web python manage.py createsuperuser
```

The site is served at <http://localhost:8000/>. The Django admin is at
`/admin/`. The auto-generated OpenAPI schema is at `/api/v1/docs`.

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
projects/               Projects, contributions, artifact links, lineage
people/                 Users (auth.User), profiles, institutions
api/                    Django Ninja API at /api/v1/
deploy/                 Production runbook and nginx site config
planning/               Source-of-truth planning documents
```

## What the demo does

- Browse and search a small body of pre-loaded projects.
- View project pages with description, contributors, artifact links, citation
  exports (BibTeX and CSL JSON), and a Graphviz-rendered lineage subgraph.
- Mark project B as derived from project A through the admin; the lineage tree
  appears on both pages automatically.
- Export the entire public database as a single JSON document at
  `/api/v1/export/`. That endpoint is the survivability promise made concrete.

## What the demo does not do

ORCID OAuth, real DOI minting, federation imports from GitHub or Zenodo, the
wiki layer, and reuse attestations are deliberately out of scope. See
[planning/demo-architecture.md §1](planning/demo-architecture.md) for the full
in-scope / out-of-scope list.

## Deploy

See [deploy/README.md](deploy/README.md).
