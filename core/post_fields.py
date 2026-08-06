from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Iterable

from .errors import PostEvidenceError, SourceSchemaError, attach_source_evidence
from .parser import POST_FIELDS, Post, post_owner_identity
from .post_identity import consistent_blog_owner, post_url_identity

if TYPE_CHECKING:
    from .source_scan import ContentSource


_EVIDENCE_FIELDS = (
    "title", "summary", "content", "images", "tags", "publish_time", "author",
)
_DETAIL_SUMMARY_SOURCES = frozenset({
    "mobile_detail", "embedded_json", "html_post",
})


@dataclass
class PostEvidenceLedger:
    fields: dict[str, dict[str, object]] = field(
        default_factory=lambda: {name: {} for name in _EVIDENCE_FIELDS}
    )
    summary_roles: dict[str, dict[str, object]] = field(default_factory=dict)
    owners: dict[str, str] = field(default_factory=dict)
    urls: dict[str, str] = field(default_factory=dict)
    conflicted_ids: set[str] = field(default_factory=set)

    def observe(self, post: Post, *, collect_conflicts: bool = False) -> None:
        _remember_owner_evidence(self.owners, post)
        if post.has_fields({"url"}) and post.url:
            _remember_url_evidence(
                self.urls, post, self.conflicted_ids, collect_conflicts
            )
        _remember_known_fields(
            self.fields,
            self.summary_roles,
            post,
            self.conflicted_ids,
            collect_conflicts,
        )

    def merge(
        self, other: "PostEvidenceLedger", *, collect_conflicts: bool = False
    ) -> None:
        self.conflicted_ids.update(other.conflicted_ids)
        _merge_owner_ledgers(self.owners, other.owners)
        _merge_url_ledgers(
            self.urls, other.urls, self.conflicted_ids, collect_conflicts
        )
        _merge_summary_ledgers(
            self.fields["summary"],
            self.summary_roles,
            other.summary_roles,
            self.conflicted_ids,
            collect_conflicts,
        )
        for name in _EVIDENCE_FIELDS:
            if name == "summary":
                continue
            _merge_field_ledgers(
                name,
                self.fields[name],
                other.fields.get(name, {}),
                self.conflicted_ids,
                collect_conflicts,
            )

    def copy(self) -> "PostEvidenceLedger":
        return PostEvidenceLedger(
            fields={name: dict(values) for name, values in self.fields.items()},
            summary_roles={
                post_id: dict(values)
                for post_id, values in self.summary_roles.items()
            },
            owners=dict(self.owners),
            urls=dict(self.urls),
            conflicted_ids=set(self.conflicted_ids),
        )


def merge_post_fields(base: Post, detail: Post) -> Post:
    if base.post_id != detail.post_id:
        raise SourceSchemaError("post_id")
    validate_post_evidence((base, detail))
    values = _merged_values(base, detail)
    completeness = base.completeness | detail.completeness
    provenance = {**base.provenance, **detail.provenance}
    for field_name in {"publish_time"} & base.completeness:
        values[field_name] = getattr(base, field_name)
        provenance[field_name] = base.provenance.get(field_name, base.source)
    _prefer_summary(base, detail, values, provenance)
    _prefer_owned_url(base, detail, values, provenance)
    return replace(
        detail,
        **values,
        completeness=completeness,
        provenance=provenance,
    )


async def ensure_post_fields(
    post: Post, source: ContentSource, required: Iterable[str]
) -> Post:
    fields = frozenset(required)
    if post.has_fields(fields):
        return post
    if not post.url or not post.has_fields({"url"}):
        raise SourceSchemaError(_missing_location(post, fields))
    detail = await source.get_post(post.url)
    merged = merge_post_fields(post, detail)
    if not merged.has_fields(fields):
        raise SourceSchemaError(_missing_location(merged, fields))
    return merged


async def ensure_posts_fields(
    posts: list[Post], source: ContentSource, required: Iterable[str]
) -> list[Post]:
    fields = frozenset(required)
    result: list[Post] = []
    try:
        for post in posts:
            result.append(await ensure_post_fields(post, source, fields))
    except Exception as exc:
        attach_source_evidence(exc, result)
        raise
    return result


def validate_post_evidence(posts: Iterable[Post]) -> None:
    ledger = PostEvidenceLedger()
    for post in posts:
        ledger.observe(post)


def _remember_owner_evidence(owners: dict[str, str], post: Post) -> None:
    owner = post_owner_identity(post)
    try:
        resolved = consistent_blog_owner(owners.get(post.post_id, ""), owner)
    except ValueError:
        raise SourceSchemaError("post.owner") from None
    if resolved:
        owners[post.post_id] = resolved


def _remember_known_fields(
    ledgers: dict[str, dict[str, object]],
    summary_roles: dict[str, dict[str, object]],
    post: Post,
    conflicts: set[str],
    collect_conflicts: bool,
) -> None:
    for field_name in ("title", "content", "author"):
        if post.has_fields({field_name}):
            _remember_evidence(
                field_name,
                ledgers[field_name],
                post.post_id,
                getattr(post, field_name),
                conflicts,
                collect_conflicts,
            )
    if post.has_fields({"summary"}):
        _remember_summary_evidence(
            ledgers["summary"],
            summary_roles,
            post.post_id,
            _summary_role(post),
            post.summary,
            conflicts,
            collect_conflicts,
        )
    if post.has_fields({"images"}):
        _remember_evidence(
            "images",
            ledgers["images"],
            post.post_id,
            tuple(post.images),
            conflicts,
            collect_conflicts,
        )
    if post.has_fields({"tags"}):
        value = frozenset(tag.casefold() for tag in post.tags)
        _remember_evidence(
            "tags", ledgers["tags"], post.post_id, value, conflicts, collect_conflicts
        )
    if post.has_fields({"publish_time"}) and post.publish_time:
        _remember_evidence(
            "publish_time",
            ledgers["publish_time"],
            post.post_id,
            post.publish_time,
            conflicts,
            collect_conflicts,
        )


def _summary_role(post: Post) -> str:
    source = post.provenance.get("summary", post.source)
    if source == "dwr":
        return "dwr_list"
    if source in _DETAIL_SUMMARY_SOURCES:
        return "detail"
    return "strict"


def _remember_summary_evidence(
    ledger: dict[str, object],
    roles: dict[str, dict[str, object]],
    post_id: str,
    role: str,
    value: object,
    conflicts: set[str],
    collect_conflicts: bool,
) -> None:
    observed = roles.get(post_id, {})
    for existing_role, existing_value in observed.items():
        if _compatible_summary_values(
            existing_role, existing_value, role, value
        ):
            continue
        if collect_conflicts:
            conflicts.add(post_id)
            return
        raise PostEvidenceError("field_conflict", "summary", "post_ledger")

    observed = roles.setdefault(post_id, {})
    observed[role] = value
    selected_role = _preferred_summary_role(observed)
    ledger[post_id] = observed[selected_role]


def _preferred_summary_role(observed: dict[str, object]) -> str:
    for role in ("detail", "strict", "dwr_list"):
        if observed.get(role):
            return role
    for role in ("detail", "strict", "dwr_list"):
        if role in observed:
            return role
    raise RuntimeError("summary evidence is empty")


def _compatible_summary_values(
    existing_role: str,
    existing_value: object,
    incoming_role: str,
    incoming_value: object,
) -> bool:
    if existing_value == incoming_value:
        return True
    return {existing_role, incoming_role} == {"dwr_list", "detail"}


def _remember_evidence(
    field_name: str,
    ledger: dict,
    post_id: str,
    value: object,
    conflicts: set[str],
    collect_conflicts: bool,
) -> None:
    existing = ledger.get(post_id)
    if existing is None or existing == value:
        ledger[post_id] = value
        return
    if collect_conflicts:
        conflicts.add(post_id)
        return
    raise PostEvidenceError("field_conflict", field_name, "post_ledger")


def _remember_url_evidence(
    ledger: dict[str, str],
    post: Post,
    conflicts: set[str],
    collect_conflicts: bool,
) -> None:
    canonical = _canonical_url(post.url)
    if post_url_identity(canonical)[1] != post.post_id:
        raise SourceSchemaError("post.id")
    _remember_canonical_url(
        ledger, post.post_id, canonical, conflicts, collect_conflicts
    )


def _remember_canonical_url(
    ledger: dict[str, str],
    post_id: str,
    canonical: str,
    conflicts: set[str],
    collect_conflicts: bool,
) -> None:
    existing = ledger.get(post_id)
    if existing is None or existing == canonical:
        ledger[post_id] = canonical
        return
    old_owner = _url_owner(existing)
    new_owner = _url_owner(canonical)
    if bool(old_owner) != bool(new_owner):
        ledger[post_id] = canonical if new_owner else existing
        return
    if collect_conflicts:
        conflicts.add(post_id)
        return
    raise PostEvidenceError("canonical_url_conflict", "url", "post_ledger")


def _merge_owner_ledgers(
    target: dict[str, str], incoming: dict[str, str]
) -> None:
    for post_id, owner in incoming.items():
        try:
            resolved = consistent_blog_owner(target.get(post_id, ""), owner)
        except ValueError:
            raise SourceSchemaError("post.owner") from None
        if resolved:
            target[post_id] = resolved


def _merge_url_ledgers(
    target: dict[str, str],
    incoming: dict[str, str],
    conflicts: set[str],
    collect_conflicts: bool,
) -> None:
    for post_id, canonical in incoming.items():
        _remember_canonical_url(
            target, post_id, canonical, conflicts, collect_conflicts
        )


def _merge_summary_ledgers(
    target: dict[str, object],
    target_roles: dict[str, dict[str, object]],
    incoming_roles: dict[str, dict[str, object]],
    conflicts: set[str],
    collect_conflicts: bool,
) -> None:
    for post_id, observed in incoming_roles.items():
        for role, value in observed.items():
            _remember_summary_evidence(
                target,
                target_roles,
                post_id,
                role,
                value,
                conflicts,
                collect_conflicts,
            )


def _merge_field_ledgers(
    field_name: str,
    target: dict[str, object],
    incoming: dict[str, object],
    conflicts: set[str],
    collect_conflicts: bool,
) -> None:
    for post_id, value in incoming.items():
        _remember_evidence(
            field_name, target, post_id, value, conflicts, collect_conflicts
        )


def _prefer_summary(
    base: Post, detail: Post, values: dict[str, object], provenance: dict[str, str]
) -> None:
    if not base.has_fields({"summary"}) or not detail.has_fields({"summary"}):
        return
    if _summary_role(base) != "dwr_list" or _summary_role(detail) != "detail":
        return
    if base.summary and not detail.summary:
        values["summary"] = base.summary
        provenance["summary"] = base.provenance.get("summary", base.source)


def _prefer_owned_url(
    base: Post, detail: Post, values: dict[str, object], provenance: dict[str, str]
) -> None:
    known = [post for post in (base, detail) if post.has_fields({"url"})]
    if not known:
        return
    selected = known[-1]
    if len(known) == 2:
        base_url = _canonical_url(base.url)
        detail_url = _canonical_url(detail.url)
        if base_url == detail_url or (_url_owner(base.url) and not _url_owner(detail.url)):
            selected = base
    values["url"] = selected.url
    provenance["url"] = selected.provenance.get("url", selected.source)


def _canonical_url(url: str) -> str:
    try:
        return post_url_identity(url)[0]
    except ValueError:
        raise SourceSchemaError("post.url") from None


def _url_owner(url: str) -> str:
    if not url:
        return ""
    try:
        return post_url_identity(url)[2]
    except ValueError:
        raise SourceSchemaError("post.url") from None


def _merged_values(base: Post, detail: Post) -> dict[str, object]:
    values: dict[str, object] = {}
    for field_name in POST_FIELDS:
        if field_name not in detail.completeness and field_name in base.completeness:
            values[field_name] = getattr(base, field_name)
    return values


def _missing_location(post: Post, fields: frozenset[str]) -> str:
    missing = post.missing_fields(fields)
    if "tags" in missing:
        return "tags"
    if missing & {"author", "author_username"}:
        return "author"
    if "publish_time" in missing:
        return "publishTime"
    if "url" in missing:
        return "url"
    if "images" in missing:
        return "post.images"
    return "post.content"
