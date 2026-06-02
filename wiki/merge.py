"""Line-based 3-way merge for wiki suggestions.

The point of this module is so that two pending suggestions to
different sections of the same wiki page can both land cleanly. The
classic case: contributor A suggests editing the "Materials" section,
contributor B suggests editing "Calibration." A maintainer accepts A
first; when they come back to B's suggestion, B's diff is still based
on the pre-A body, but the change B made is in a region A never
touched. A naive "replace whole body with proposed" wipes out A's
edits. A three-way merge keeps them.

The algorithm is the standard line-based 3-way merge:

- Split base, current, and proposed into lines.
- Diff base -> proposed to get the suggester's intended hunks.
- Use a base -> current mapping to project each hunk's location onto
  current. A hunk applies cleanly when the corresponding region in
  current still equals base; otherwise both branches touched the same
  region and we flag a conflict.

Conflicts fall back to keeping the current text in that region so we
never silently lose committed content. The maintainer can still edit
the merged text in the review form before applying.
"""

from __future__ import annotations

from difflib import SequenceMatcher


def three_way_merge(base: str, current: str, proposed: str) -> tuple[str, bool]:
    """Apply the change base -> proposed onto current.

    Returns ``(merged_text, had_conflict)``. ``had_conflict`` is True
    when at least one hunk could not be applied cleanly because the
    same region had been edited in current.
    """
    if base == current:
        return proposed, False
    if base == proposed:
        return current, False
    if current == proposed:
        return current, False

    base_lines = base.splitlines()
    current_lines = current.splitlines()
    proposed_lines = proposed.splitlines()

    # Build a mapping from each base line index to its position in
    # current. Lines that survive base->current unchanged map exactly;
    # for changed regions we still record start/end anchors so a hunk
    # whose surrounding context is unchanged can find its landing spot.
    base_to_current: list[int | None] = [None] * (len(base_lines) + 1)
    sm_bc = SequenceMatcher(a=base_lines, b=current_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in sm_bc.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                base_to_current[i1 + k] = j1 + k
        else:
            # Anchor the boundaries of a changed region. The interior
            # indices stay None and will be flagged if a hunk lands there.
            if base_to_current[i1] is None:
                base_to_current[i1] = j1
            base_to_current[i2] = j2
    # End-of-base anchors at end-of-current so a trailing hunk has a stop.
    base_to_current[len(base_lines)] = len(current_lines)

    output: list[str] = []
    cursor = 0
    had_conflict = False

    sm_bp = SequenceMatcher(a=base_lines, b=proposed_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in sm_bp.get_opcodes():
        c_start = base_to_current[i1] if i1 <= len(base_lines) else None
        c_end = base_to_current[i2] if i2 <= len(base_lines) else None

        if c_start is None or c_end is None:
            # The anchor we need was changed by current. Treat as conflict
            # and skip the proposed change for this hunk.
            had_conflict = True
            continue

        if c_start < cursor:
            # Overlap with a previously emitted region. Skip.
            had_conflict = True
            continue
        if c_start > cursor:
            output.extend(current_lines[cursor:c_start])
            cursor = c_start

        if tag == "equal":
            output.extend(current_lines[c_start:c_end])
            cursor = c_end
            continue

        # Replace, delete, or insert: only safe if current still matches base here.
        if current_lines[c_start:c_end] == base_lines[i1:i2]:
            output.extend(proposed_lines[j1:j2])
            cursor = c_end
        else:
            output.extend(current_lines[c_start:c_end])
            cursor = c_end
            had_conflict = True

    if cursor < len(current_lines):
        output.extend(current_lines[cursor:])

    merged = "\n".join(output)
    # Preserve a trailing newline if either input had one.
    if (current.endswith("\n") or proposed.endswith("\n")) and not merged.endswith("\n"):
        merged += "\n"
    return merged, had_conflict
