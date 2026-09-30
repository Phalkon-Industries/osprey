"""License files inside uploaded archives, and the record-level license file.

OSPREY shows one license per project, and the files it publishes must
carry the same one (decided 2026-09-29). At publish time the uploaded zip
is scanned, without unpacking, for license files at its root or one
folder down. A recognized license that differs from the chosen one blocks
publishing. A matching one, an unrecognizable one, or none at all lets it
through. Every deposit also gets a `LICENSE.txt` sibling next to the
archive stating the record's license.
"""
from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass

from .forms import COMMON_LICENSES

LICENSE_NAME_RE = re.compile(r"^(un)?licen[cs]e([._-].*)?$|^copying([._-].*)?$", re.I)
MAX_LICENSE_FILE_BYTES = 256 * 1024
MAX_DEPTH = 1  # root, or inside one top-level folder

LICENSE_LABELS = dict(COMMON_LICENSES)
LICENSE_LABELS.update({
    "BSD-2-Clause": "BSD 2-Clause", "BSD-3-Clause": "BSD 3-Clause", "LGPL-3.0": "LGPL 3.0",
    "MPL-2.0": "MPL 2.0",
    "CC-BY-NC": "a non-commercial Creative Commons license", "CC-BY-ND": "a no-derivatives Creative Commons license",
})


def identify_license_text(text: str) -> str:
    """Name the license a text is, by its distinctive phrases. Returns an
    OSPREY key, a recognized-but-not-accepted key (BSD, LGPL, MPL,
    CC-BY-NC, CC-BY-ND), or "" when the text isn't a known license."""
    t = re.sub(r"\s+", " ", (text or "").lower())
    if not t:
        return ""
    m = re.search(r"spdx-license-identifier:\s*([a-z0-9.+-]+)", t)
    if m:
        from .sources.records import normalize_license

        key = normalize_license(m.group(1)) or m.group(1).upper()
        if key:
            return key
    if "noncommercial" in t or "non-commercial" in t:
        return "CC-BY-NC"
    if "nonderivative" in t or "noderivatives" in t or "no-derivatives" in t:
        return "CC-BY-ND"
    if "gnu affero general public license" in t:
        return "AGPL-3.0"
    if "gnu lesser general public license" in t:
        return "LGPL-3.0"
    if "gnu general public license" in t:
        return "GPL-3.0" if "version 3" in t else ""
    if "apache license" in t and "version 2.0" in t:
        return "Apache-2.0"
    if "mozilla public license" in t and "2.0" in t:
        return "MPL-2.0"
    if "permission is hereby granted, free of charge, to any person obtaining a copy" in t:
        return "MIT"
    if "redistributions of source code must retain" in t:
        return "BSD-3-Clause" if ("neither the name" in t or "endorse or promote" in t) else "BSD-2-Clause"
    if "cern open hardware licence" in t or "cern-ohl" in t:
        if "strongly reciprocal" in t or "cern-ohl-s" in t:
            return "CERN-OHL-S-2.0"
        if "weakly reciprocal" in t or "cern-ohl-w" in t:
            return "CERN-OHL-W-2.0"
        if "permissive" in t or "cern-ohl-p" in t:
            return "CERN-OHL-P-2.0"
        return ""
    if "cc0 1.0 universal" in t or "creative commons zero" in t:
        return "CC0-1.0"
    if "attribution-sharealike 4.0" in t or "attribution sharealike 4.0" in t:
        return "CC-BY-SA-4.0"
    if "attribution 4.0 international" in t:
        return "CC-BY-4.0"
    return ""


@dataclass
class ArchiveLicense:
    path: str
    key: str  # "" when the text wasn't recognizable


def licenses_in_archive(fileobj) -> list[ArchiveLicense]:
    """License files at the archive root or one folder down, identified."""
    found: list[ArchiveLicense] = []
    try:
        fileobj.seek(0)
    except Exception:  # noqa: BLE001 - not every file supports seek
        pass
    try:
        with zipfile.ZipFile(fileobj) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                parts = [p for p in info.filename.replace("\\", "/").split("/") if p]
                if not parts or len(parts) - 1 > MAX_DEPTH or parts[-1].startswith("."):
                    continue
                if not LICENSE_NAME_RE.match(parts[-1]):
                    continue
                if info.file_size > MAX_LICENSE_FILE_BYTES:
                    continue
                with archive.open(info) as fh:
                    text = fh.read(MAX_LICENSE_FILE_BYTES).decode("utf-8", "ignore")
                found.append(ArchiveLicense(info.filename, identify_license_text(text)))
    except (zipfile.BadZipFile, OSError, RuntimeError):
        return []
    finally:
        try:
            fileobj.seek(0)
        except Exception:  # noqa: BLE001
            pass
    return found


def _family(key: str) -> str:
    return (key or "").replace("-or-later", "").replace("-only", "").lower()


def archive_license_conflict(fileobj, chosen: str) -> str:
    """The block message when the archive's license files disagree with the
    chosen license, else "". A dual-licensed archive passes when any of
    its license files matches the choice."""
    if not chosen:
        return ""
    found = [f for f in licenses_in_archive(fileobj) if f.key]
    if not found:
        return ""
    if any(_family(f.key) == _family(chosen) for f in found):
        return ""
    names = sorted({LICENSE_LABELS.get(f.key, f.key) for f in found})
    paths = ", ".join(sorted({f.path for f in found}))
    what = " and ".join(names)
    return (
        f"The provided zip has {what} ({paths}), but you chose "
        f"{LICENSE_LABELS.get(chosen, chosen)} in the overview. Either remove the license "
        "file from the zip before uploading again, or make sure they match."
    )


def license_sidecar_text(project) -> str:
    """`LICENSE.txt` for the Zenodo record: the license the files carry, with
    the SPDX identifier and where the full text lives."""
    key = (project.license or "").strip()
    label = LICENSE_LABELS.get(key, key)
    lines = [
        "License for the files in this record",
        "",
        f"Project: {project.title}",
        f"License: {label}",
        f"SPDX-License-Identifier: {key}",
        f"Full text: https://spdx.org/licenses/{key}.html",
        "",
        "The files in this Zenodo record are released under the license above,",
        "as set by the project on OSPREY when this version was published.",
        "Where the archive carries its own license file, it names the same license.",
        "",
    ]
    return "\n".join(lines)
