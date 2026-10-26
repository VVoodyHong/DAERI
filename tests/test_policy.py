import pytest

from daeri.core.policy import pointer_get, pointer_set, project, selector_paths, validate_selectors
from daeri.core.errors import StoreError


def select(*parts):
    return {"segments": [{"each": True} if part is Ellipsis else {"index": part} if type(part) is int else {"key": part} for part in parts]}


def test_pointer_escaping_null_and_array_indices_are_exact():
    value = {"a/b": {"~": [None, {"*": "literal"}]}}
    assert pointer_get(value, "/a~1b/~0/0") is None
    assert pointer_get(value, "/a~1b/~0/1/*") == "literal"
    for path in ("#/a", "/a~2b", "/a~1b/~0/01", "/a~1b/~0/-", "/absent"):
        with pytest.raises(StoreError):
            pointer_get(value, path)
    pointer_set(value, "/a~1b/~0/1/*", "changed")
    assert value["a/b"]["~"][1]["*"] == "changed"


def test_parent_grant_cannot_restore_masked_child():
    source = {"rows": [{"id": "1", "name": "visible", "secret": "hidden"}]}
    output = project(source, [select("rows")], masking=[{"field": select("rows", Ellipsis, "secret"), "action": "omit"}])
    assert output == {"rows": [{"id": "1", "name": "visible"}]}
    assert source["rows"][0]["secret"] == "hidden"
    assert selector_paths(source, select("rows", Ellipsis, "id")) == {("rows", 0, "id")}


def test_policy_intersection_and_empty_arrays_never_widen_access():
    source = {"rows": [{"id": "1", "hidden": "x"}], "other": "forbidden"}
    assert project(source, [select()], ceilings=[[select("rows", Ellipsis, "id")]]) == {"rows": [{"id": "1"}]}
    assert project(source, []) == {}
    assert project({"rows": []}, [select("rows", Ellipsis, "id")]) == {"rows": []}


def test_invalid_selectors_and_overlapping_masks_are_rejected():
    for selectors in ([{"segments": [{"each": False}]}], [select("x"), select("x")], [{"segments": [{"index": True}]}]):
        with pytest.raises(StoreError):
            validate_selectors(selectors)
    with pytest.raises(StoreError):
        project({"a": {"b": "value"}}, [select()], masking=[{"field": select("a"), "action": "omit"}, {"field": select("a", "b"), "action": "redact"}])
