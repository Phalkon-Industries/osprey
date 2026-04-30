"""Render a small lineage subgraph (two hops out) as inline SVG via graphviz."""

from __future__ import annotations

from typing import Iterable

import graphviz

from .models import LineageEdge, Project


def _collect_edges(project: Project, depth: int = 2) -> set[tuple[int, int, str]]:
    """Walk parents and children up to `depth` hops; return edge tuples."""

    edges: set[tuple[int, int, str]] = set()
    seen: set[int] = {project.pk}

    def walk(current_ids: Iterable[int], hops_left: int) -> None:
        if hops_left <= 0:
            return
        current_ids = list(current_ids)
        if not current_ids:
            return
        next_ids: set[int] = set()
        parent_qs = LineageEdge.objects.filter(child_id__in=current_ids).select_related(
            "parent", "child"
        )
        child_qs = LineageEdge.objects.filter(parent_id__in=current_ids).select_related(
            "parent", "child"
        )
        for edge in list(parent_qs) + list(child_qs):
            edges.add((edge.parent_id, edge.child_id, edge.relation))
            for pk in (edge.parent_id, edge.child_id):
                if pk not in seen:
                    seen.add(pk)
                    next_ids.add(pk)
        walk(next_ids, hops_left - 1)

    walk([project.pk], depth)
    return edges


def render_lineage_svg(project: Project, depth: int = 2) -> str:
    """Return an SVG string of the local lineage, or empty string on failure."""

    edges = _collect_edges(project, depth=depth)
    if not edges:
        return ""

    node_ids = {project.pk}
    for parent_id, child_id, _relation in edges:
        node_ids.add(parent_id)
        node_ids.add(child_id)

    titles = dict(Project.objects.filter(pk__in=node_ids).values_list("pk", "title"))
    slugs = dict(Project.objects.filter(pk__in=node_ids).values_list("pk", "slug"))

    dot = graphviz.Digraph(format="svg")
    dot.attr("graph", rankdir="TB", bgcolor="transparent")
    dot.attr("node", shape="box", style="rounded,filled", fillcolor="#f5f5f5", fontname="sans-serif")
    dot.attr("edge", fontname="sans-serif", fontsize="10")

    for pk in node_ids:
        label = titles.get(pk, f"#{pk}")
        attrs = {"href": f"/projects/{slugs.get(pk, '')}/"}
        if pk == project.pk:
            attrs["fillcolor"] = "#dbeafe"
            attrs["penwidth"] = "2"
        dot.node(str(pk), label=label, **attrs)

    for parent_id, child_id, relation in edges:
        dot.edge(str(parent_id), str(child_id), label=relation.replace("_", " "))

    try:
        svg_bytes = dot.pipe(format="svg")
    except graphviz.backend.execute.ExecutableNotFound:
        return ""
    return svg_bytes.decode("utf-8")
