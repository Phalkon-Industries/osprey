"""Indexed entries: resolve a DOI or URL to a source record, run the two
gates (open license, reachable files), and create or update the entry.

See planning/features/indexed-entries.md. Adapters live one per source;
`service.resolve` is the single entry point the staff page and the future
public form call.
"""
