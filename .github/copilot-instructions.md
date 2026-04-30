+# Copilot instructions for the OSPREY repo

OSPREY is a public-benefit research engineering commons. The current state of this repository is a set of Markdown planning and outreach documents. Code (a demo web application) is being scoped now and will land later. These instructions cover both phases.

## Project context

- **Working name:** OSPREY — *Open Science Platform for Research and Engineering.* Acronym is OSPRE; the trailing Y keeps the full bird word as the brand.
- **Founder:** Jonathan A Pfeifer (research engineer, embedded systems and CO₂ sensors, currently at WHOI).
- **Legal home:** pHalkon Industries LLC, running OSPREY as a public-benefit project.
- **Starting community:** oceanographic engineering at WHOI.
- **Status:** concept and early prototype. Demo system in scoping. Working solo, leaning on AI assistance.

## Source-of-truth documents

Read these before suggesting changes that touch their domain:

- [vision.md](vision.md) — the why, future picture, and pump anecdote. Narrative source of truth.
- [concept.md](concept.md) — what OSPREY is. Spec source of truth.
- [implementation-plan.md](implementation-plan.md) — phases, milestones, support, risks. Plan source of truth.
- [naming-shortlist.md](naming-shortlist.md) — naming decision and backup candidates.
- [trademark-guide.md](trademark-guide.md) — trademark strategy and class recommendations.
- [pose-notes.md](pose-notes.md) — NSF POSE analysis and adjacent funding programs.
- [ai-writing-tells.md](ai-writing-tells.md) — what to avoid in writing. **Always consult before producing prose.**

Composed documents (derived from sources, not sources themselves):

- [one-pager.md](one-pager.md) — for cold first-contact emails.
- [pitch-deck-outline.md](pitch-deck-outline.md) — the slide outline.

A document is one or the other, not both.

## Writing rules

These exist because the founder cares about not sounding like an LLM. **Apply them on every prose change.**

1. **Run the AI-tells checklist on every draft.** [ai-writing-tells.md](ai-writing-tells.md) is the canonical list. Specific words and patterns to avoid: load-bearing, first-class, paved path, moat, crucially, importantly, simply, clearly, obviously, journey, unlock, leverage, robust, seamless, holistic, "in short," "bottom line," silently, quietly, "invisible tax," "the only question," "either way," "compounds" (as a verb), first-order, "message in a bottle." Cut "let me," "I'll now," and similar stage directions.
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
- Naming, branding, expansion: source is [naming-shortlist.md](naming-shortlist.md). Anywhere else, match exactly. The current expansion is *Open Science Platform for Research and Engineering*. Do not reintroduce "Yields."
- Numbers (funding scales, milestones, runway): source is [implementation-plan.md](implementation-plan.md). Pitch deck appendix may restate, never invent.
- The pump story exists in [vision.md](vision.md) and the one-pager. If reused elsewhere, vary the phrasing slightly so a reader who sees both does not feel they've read the same paragraph twice.

## Naming and language conventions

- **OSPREY** in all caps when used as the project name. The bird (lowercase "osprey") only when literally the bird.
- **pHalkon Industries LLC** (lowercase p, uppercase H). Not "Phalkon," not "PHalkon."
- **Jonathan A Pfeifer.** Not "Jonathan Apfeifer." Last name is "Pfeifer."
- **WHOI** for Woods Hole Oceanographic Institution (no periods).
- **ORCID, DOI, OSS** capitalized.
- "Research engineering" (lowercase, two words) is the field. "Research engineer" is the role.

## Repo conventions

- Markdown files use `.md`. Soft-wrap, no hard line breaks.
- Internal links use relative paths and lowercase filenames: `[concept.md](concept.md)`.
- File names are lowercase with hyphens: `naming-shortlist.md`, not `Naming Workshop.md`.
- Two pre-existing files use spaces (`Meeting with Hunter.md`, `LICENSE`). Leave those alone.
- The `general-idea.md` file referenced in older summaries is gone; current scratch lives in concept-and-org's successors (concept.md and implementation-plan.md).

## When the demo code lands

The demo will be scoped in a separate architecture document. Until then:

- **Stack assumptions in flight:** likely Python backend (FastAPI or Django), Postgres, a JS framework for the frontend, hosted on a VPS. Subject to revision after the architecture conversation.
- **Guiding principles** for any code suggestion: survivable, thin, boring. Stable well-understood tools. No exotic dependencies the next maintainer would have to learn.
- **Founder's coding background:** strong in CS and electrical engineering, embedded systems, CO₂ sensors. **New to full-stack web development.** Explain web-specific concepts (deployment, CORS, sessions, ORMs, migrations, reverse proxies, certificate management) when they come up. Don't assume context that comes from years of building web apps.
- **Federation over hosting.** OSPREY is a curation/credit/lineage layer on top of GitHub/Codeberg/Zenodo. The demo should not become a primary file host.
- **Survivable data.** Anything OSPREY stores must be exportable as plain data. A successor community should be able to reconstruct the platform from a public export plus the upstream repositories.
- **Identity:** ORCID OAuth is the planned primary identity provider. Institutional SSO comes later.
- **DOIs:** minted via DataCite or Zenodo, not invented.

## Things to push back on

If a request would do any of the following, surface the concern before complying:

- Reintroduce dropped language ("Yields," "message in a bottle," "invisible tax," "load-bearing," etc.).
- Move a fact out of its source-of-truth document without updating the source.
- Add VC-pitch tone to documents the founder has explicitly framed as invitations.
- Build a primary file host into OSPREY rather than layering over existing repositories.
- Adopt token-based or DAO-style governance. The governance direction is pluralistic, credential-weighted, ORCID-based, and explicitly *not* a token DAO.
- Promise free read access forever. The current language is deliberately softened: access decisions happen with pilot partners during the pilot phase rather than being decided in advance.
- Pad scope with features that drift from the curation/credit/lineage core (peer review, CI runners, CAD rendering, compliance dashboards). Those are explicit non-goals for now.

## Tooling

- The user is on Windows with PowerShell 5.1. Default to PowerShell-correct commands (`Remove-Item`, not `rm`; semicolons to chain, not `&&`).
- Git author name is "Jonathan" with a noreply email. Do not change git config.

## When unsure

Ask. The founder prefers a clarifying question over a confident wrong implementation, and prefers small reversible steps over large speculative ones. After completing prose work, do an explicit AI-tells review pass before declaring done.
