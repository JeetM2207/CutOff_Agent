# Master Profile (template)

This is the source of truth for dynamic, JD-tailored resume generation
(Section 6.4 extension). Copy this file to `config/master_profile.md`
(gitignored -- never committed, never uploaded anywhere) and fill it in with
your own real, complete information: every skill, project, and metric you
have, in as much honest detail as you can. Nothing gets invented at
generation time -- the LLM is instructed to only ever select and rephrase
what's actually written here, never to add anything that isn't. A thin
profile produces a thin (but honest) tailored resume; a thorough one lets the
agent actually pick the right 2-4 highlights for each job description instead
of guessing.

If this file is missing or empty, dynamic generation is skipped entirely and
the agent falls back to the static `resume_*.pdf` matching it already does.

## Skills

- Languages:
- Frameworks / libraries:
- Tools / platforms:

## Projects

### Project name
- What it does, in one line.
- Your specific contribution, with a real, measurable outcome if you have
  one (e.g. "reduced query latency from Xms to Yms", "used by N users").
- Tech stack used.

(repeat for each project)

## Experience / internships (if any)

### Role, Company, dates
- What you actually did, as bullet points.
- A real metric or outcome per bullet where possible.

## Achievements / certifications (if any)

-
