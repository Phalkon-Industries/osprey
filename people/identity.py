"""Identity helpers for ORCID-backed local accounts."""
from __future__ import annotations

import random
import re

from django.contrib.auth import get_user_model
from django.utils.text import slugify


USER_TAG_MAX_LENGTH = 40


def extract_orcid(extra_data: dict) -> str:
    identifier = extra_data.get("orcid-identifier") or {}
    return (identifier.get("path") or "").strip()


def extract_orcid_names(extra_data: dict) -> dict[str, str]:
    person = extra_data.get("person") or {}
    name = person.get("name") or {}
    credit_name = ((name.get("credit-name") or {}).get("value") or "").strip()
    given_names = ((name.get("given-names") or {}).get("value") or "").strip()
    family_name = ((name.get("family-name") or {}).get("value") or "").strip()
    full_name = credit_name or " ".join(part for part in [given_names, family_name] if part)
    return {
        "display_name": full_name.strip(),
        "first_name": given_names,
        "last_name": family_name,
    }


def clean_user_tag(value: str) -> str:
    tag = value.strip().lower()
    if tag.startswith("@"):
        tag = tag[1:]
    return tag


def user_tag_base(name: str) -> str:
    base = re.sub(r"[^a-z0-9]", "", slugify(name or ""))
    return base or "person"


def _orcid_suffix(orcid_id: str) -> str:
    digits = re.sub(r"\D", "", orcid_id or "")
    return digits[-4:] if len(digits) >= 4 else ""


def _is_available(tag: str, *, user_id: int | None = None) -> bool:
    User = get_user_model()
    users = User.objects.filter(username__iexact=tag)
    if user_id is not None:
        users = users.exclude(pk=user_id)
    return not users.exists()


def generate_user_tag(name: str, *, orcid_id: str = "", user_id: int | None = None) -> str:
    base = user_tag_base(name)
    bare = base[:USER_TAG_MAX_LENGTH]
    if _is_available(bare, user_id=user_id):
        return bare

    suffixes: list[str] = []
    orcid_suffix = _orcid_suffix(orcid_id)
    if orcid_suffix:
        suffixes.append(orcid_suffix)
    while len(suffixes) < 100:
        suffixes.append(f"{random.randint(0, 9999):04d}")

    for suffix in suffixes:
        max_base_length = USER_TAG_MAX_LENGTH - len(suffix)
        candidate = f"{base[:max_base_length]}{suffix}"
        if _is_available(candidate, user_id=user_id):
            return candidate

    counter = 0
    while True:
        suffix = f"{counter:04d}"
        max_base_length = USER_TAG_MAX_LENGTH - len(suffix)
        candidate = f"{base[:max_base_length]}{suffix}"
        if _is_available(candidate, user_id=user_id):
            return candidate
        counter += 1