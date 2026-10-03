"""Server-owned recipe source identity, independent from UI/manual version numbers."""
from .repository import request_hash


def source_snapshot(project, shot_id):
    lookup = {e["id"]: e for e in project["entities"]}
    chosen, pending = set(), [shot_id]
    incoming = {}
    for link in project["links"]:
        incoming.setdefault(link["target"], []).append(link)
    used_links = {}
    while pending:
        identity = pending.pop()
        if identity in chosen or identity not in lookup:
            continue
        chosen.add(identity)
        node = lookup[identity]
        if node.get("parentId"):
            pending.append(node["parentId"])
        for link in incoming.get(identity, []):
            used_links[link["id"]] = link
            pending.append(link["source"])
        for actor in node.get("data", {}).get("cast", []):
            actor_id = actor.get("characterId") if isinstance(actor, dict) else actor
            if isinstance(actor_id, str) and actor_id in lookup:
                pending.append(actor_id)
        for look in node.get("data", {}).get("looks", []):
            for asset_id in look.get("gallery", {}).values():
                if isinstance(asset_id, str) and asset_id in lookup:
                    pending.append(asset_id)
        # Guide sources may not also have a visible reference edge.
        for guide in node.get("data", {}).get("h3", {}).get("guides", []):
            if isinstance(guide, dict) and isinstance(guide.get("media_id"), str):
                pending.append(guide["media_id"])
    return request_hash({"shot_id": shot_id, "entities": [lookup[k] for k in sorted(chosen)],
                         "links": [used_links[k] for k in sorted(used_links)]})


def validate_source_ref(project, ref):
    lookup = {e["id"]: e for e in project["entities"]}
    shot = lookup.get(ref["shot_id"])
    if not shot or shot["type"] not in {"shot", "generation"} or shot["version"] != ref["shot_version"]:
        return False
    ancestors = {}
    node = lookup.get(shot.get("parentId"))
    while node:
        ancestors[node["type"]] = node["id"]
        node = lookup.get(node.get("parentId"))
    return all(ref.get(key) in (None, ancestors.get(kind)) for key, kind in (("chapter_id", "chapter"), ("scene_id", "scene")))
