"""Shared JSON paths and field policy; data never becomes an executable expression."""

import copy
import json
import re

from .storage import StoreError


MEMORY_CATEGORIES = ("service_rule", "resource_structure", "procedure", "lesson")


def effective_resource_policy(resource):
    """Resolve optional profile limits; the approved plan still selects every field."""
    policy = resource.get("policy")
    if policy is not None and policy != {}:
        return policy
    root = {"segments": []}
    return {"allowed_model_fields": [copy.deepcopy(root)], "export_fields": [copy.deepcopy(root)],
            "retained_fields": [copy.deepcopy(root)], "masking": [],
            "memory": {"categories": list(MEMORY_CATEGORIES), "source_fields": [root],
                       "context_sources": ["request", "clarification"],
                       "retention": "long_term", "allow_model_recall": True}}


def effective_profile_memory_policy(profile):
    """Bind plan-based memory limits to this profile's current registered targets."""
    policy = profile.get("memory_policy")
    if policy is not None and policy != {}:
        return policy
    from .storage import _profile_memory_policy
    resources = profile.get("resources", [])
    return _profile_memory_policy({"categories": list(MEMORY_CATEGORIES),
        "instance_ids": [resource["instance_id"] for resource in resources],
        "model_allowed": True, "retain_allowed": True, "long_term_allowed": True,
        "exports_allowed": True}, resources)


def require(condition, message="The registered field policy cannot be satisfied."):
    if not condition:
        raise StoreError("RESULT_VALIDATION_FAILED", message)


def pointer_tokens(pointer):
    require(isinstance(pointer, str) and (not pointer or pointer.startswith("/")), "Invalid JSON pointer.")
    tokens = pointer.split("/")[1:] if pointer else []
    require(all(not re.search(r"~(?![01])", part) for part in tokens), "Invalid JSON pointer escape.")
    return [part.replace("~1", "/").replace("~0", "~") for part in tokens]


def pointer_path(value, pointer):
    path = []
    for token in pointer_tokens(pointer):
        if isinstance(value, list):
            require(bool(re.fullmatch(r"0|[1-9]\d*", token)), "Invalid array index.")
            token = int(token)
            require(token < len(value), "The referenced array item is unavailable.")
        else:
            require(isinstance(value, dict) and token in value, "The referenced field is unavailable.")
        path.append(token)
        value = value[token]
    return tuple(path)


def pointer_get(value, pointer):
    for token in pointer_path(value, pointer):
        value = value[token]
    return value


def pointer_set(value, pointer, replacement):
    tokens = pointer_tokens(pointer)
    require(bool(tokens), "An input binding cannot replace the whole parameters object.")
    # Resolve parent segments individually; target may be a new optional object field.
    current = value
    for token in tokens[:-1]:
        if isinstance(current, list):
            require(bool(re.fullmatch(r"0|[1-9]\d*", token)) and int(token) < len(current))
            token = int(token)
        require(isinstance(current, (dict, list)) and (token in current if isinstance(current, dict) else True))
        current = current[token]
    final = tokens[-1]
    if isinstance(current, list):
        require(bool(re.fullmatch(r"0|[1-9]\d*", final)) and int(final) < len(current))
        final = int(final)
    require(isinstance(current, (dict, list)))
    current[final] = copy.deepcopy(replacement)


def leaves(value, prefix=()):
    if isinstance(value, (dict, list)) and value:
        parts = value.items() if isinstance(value, dict) else enumerate(value)
        return set().union(*(leaves(item, prefix + (key,)) for key, item in parts))
    return {prefix}


def validate_selectors(selectors):
    require(isinstance(selectors, list))
    seen = set()
    for selector in selectors:
        require(isinstance(selector, dict) and set(selector) == {"segments"} and isinstance(selector["segments"], list))
        for segment in selector["segments"]:
            require(isinstance(segment, dict) and len(segment) == 1)
            require((set(segment) == {"key"} and isinstance(segment["key"], str)) or
                    (set(segment) == {"index"} and type(segment["index"]) is int and segment["index"] >= 0) or
                    (set(segment) == {"each"} and segment["each"] is True))
        normalized = json.dumps(selector, sort_keys=True)
        require(normalized not in seen, "Field selectors must be unique.")
        seen.add(normalized)
    return selectors


def selector_paths(value, selector):
    validate_selectors([selector])

    def walk(node, segments, path):
        if not segments:
            return leaves(node, path)
        segment, *tail = segments
        if "key" in segment:
            require(isinstance(node, dict), "A selected field is not an object.")
            key = segment["key"]
            return walk(node[key], tail, path + (key,)) if key in node else set()
        require(isinstance(node, list), "A selected field is not an array.")
        if "index" in segment:
            index = segment["index"]
            return walk(node[index], tail, path + (index,)) if index < len(node) else set()
        if not node:
            return {path}
        return set().union(*(walk(item, tail, path + (index,)) for index, item in enumerate(node)))
    return walk(value, selector["segments"], ())


def selected_paths(value, selectors):
    validate_selectors(selectors)
    return set().union(*(selector_paths(value, selector) for selector in selectors)) if selectors else set()


def selector_covers(parent, child):
    left, right = parent["segments"], child["segments"]
    if len(left) > len(right):
        return False
    return all(a == b or (a == {"each": True} and ("index" in b or b == {"each": True}))
               for a, b in zip(left, right))


def within_selectors(requested, ceiling):
    validate_selectors(requested)
    validate_selectors(ceiling)
    return all(any(selector_covers(parent, child) for parent in ceiling) for child in requested)


def validate_resource_policy(policy):
    if policy is None or policy == {}:
        return None
    require(isinstance(policy, dict) and not set(policy) - {
        "allowed_model_fields", "export_fields", "retained_fields", "masking", "memory"})
    for key in ("allowed_model_fields", "export_fields", "retained_fields"):
        validate_selectors(policy.get(key, []))
    masks = policy.get("masking", [])
    require(isinstance(masks, list))
    for index, rule in enumerate(masks):
        require(isinstance(rule, dict) and set(rule) == {"field", "action"}
                and isinstance(rule["action"], str) and rule["action"] in {"omit", "redact"})
        validate_selectors([rule["field"]])
        require(not any(selector_covers(other["field"], rule["field"]) or selector_covers(rule["field"], other["field"])
                        for other in masks[:index]), "Masking selectors overlap.")
    memory = policy.get("memory", {})
    require(isinstance(memory, dict) and not set(memory) - {
        "categories", "source_fields", "context_sources", "retention", "allow_model_recall"})
    if memory:
        require(set(memory) == {"categories", "source_fields", "context_sources", "retention", "allow_model_recall"})
        categories = memory["categories"]
        require(isinstance(categories, list) and all(isinstance(item, str) for item in categories)
                and len(categories) == len(set(categories)) and
                set(categories) <= set(MEMORY_CATEGORIES))
        require(isinstance(memory["context_sources"], list)
                and all(isinstance(item, str) for item in memory["context_sources"])
                and set(memory["context_sources"]) <= {"request", "clarification"})
        require(isinstance(memory["retention"], str) and memory["retention"] in {"none", "source_bound", "long_term"}
                and type(memory["allow_model_recall"]) is bool)
        validate_selectors(memory["source_fields"])
        if memory["retention"] == "none":
            require(not (categories or memory["source_fields"] or memory["context_sources"] or memory["allow_model_recall"]))
        else:
            require(bool(categories) and bool(memory["source_fields"] or memory["context_sources"]))
            require(within_selectors(memory["source_fields"], policy.get("allowed_model_fields", [])) and
                    within_selectors(memory["source_fields"], policy.get("retained_fields", [])))
    return policy


def view(value, rule, ceiling, mode):
    """Return a view and its unmasked original leaf paths for provenance checks."""
    result, usable, _ = disclosure(value, rule, ceiling, mode)
    return result, usable


def disclosure(value, rule, ceiling, mode):
    """Keep visible redaction markers distinct from usable original evidence."""
    allowed = selected_paths(value, rule.get(mode, [])) & selected_paths(value, ceiling.get(mode, []))
    omitted_a, redacted_a = mask_paths(value, rule.get("masking", []))
    omitted_b, redacted_b = mask_paths(value, ceiling.get("masking", []))
    omitted, redacted = omitted_a | omitted_b, redacted_a | redacted_b
    selectors = [{"segments": [{"index": part} if type(part) is int else {"key": part} for part in path]}
                 for path in allowed - omitted]
    masks = [{"field": {"segments": [{"index": part} if type(part) is int else {"key": part} for part in path]}, "action": "redact"}
             for path in (redacted - omitted) & allowed]
    return project(value, selectors, masking=masks), allowed - omitted - redacted, allowed - omitted


def as_pointer(path):
    return "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in path)


def mask_paths(value, masking):
    require(isinstance(masking, list))
    seen, omitted, redacted = set(), set(), set()
    for rule in masking:
        require(isinstance(rule, dict) and set(rule) == {"field", "action"} and rule["action"] in {"omit", "redact"})
        paths = selector_paths(value, rule["field"])
        require(not (paths & seen), "Masking rules overlap.")
        seen |= paths
        (omitted if rule["action"] == "omit" else redacted).update(paths)
    return omitted, redacted


def project(value, selectors, *, ceilings=(), masking=()):
    allowed = selected_paths(value, selectors)
    for ceiling in ceilings:
        allowed &= selected_paths(value, ceiling)
    omitted, redacted = mask_paths(value, list(masking))
    allowed -= omitted
    missing = object()

    def visit(node, path):
        if not isinstance(node, (dict, list)) or not node:
            return ("[REDACTED]" if path in redacted else copy.deepcopy(node)) if path in allowed else missing
        if isinstance(node, dict):
            found = {key: visit(item, path+(key,)) for key, item in node.items()}
            found = {key: item for key, item in found.items() if item is not missing}
            return found if found else missing
        result = [visit(item, path+(index,)) for index, item in enumerate(node)]
        if all(item is missing for item in result):
            return missing
        for index, item in enumerate(result):
            if item is missing:
                require(isinstance(node[index], (dict, list)), "A partially selected scalar array cannot be represented safely.")
                result[index] = {} if isinstance(node[index], dict) else []
        return result
    result = visit(value, ())
    return {} if result is missing else result
