import pytest

from app.clusters import load_clusters
from app.storage import LocalStore, PreconditionFailed
from app.util import diff


def test_clusters_env_substitution(tmp_path, monkeypatch):
    f = tmp_path / "c.yaml"
    f.write_text("clusters:\n  - id: a\n    url: http://h:9200\n    auth: {type: api_key, api_key: '${K}'}\n"
                 "  - id: b\n    url: ${B_URL:-http://default:9200}\n    auth: {type: none}\n")
    monkeypatch.setenv("K", "secret")
    c = load_clusters(str(f))
    assert c["a"].api_key == "secret" and c["b"].hosts == ["http://default:9200"]
    monkeypatch.delenv("K")
    with pytest.raises(ValueError, match="K"):
        load_clusters(str(f))


def test_clusters_validation(tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text("clusters:\n  - id: Bad Id\n    url: http://h\n")
    with pytest.raises(ValueError):
        load_clusters(str(f))
    f.write_text("clusters:\n  - id: a\n    url: http://h\n    auth: {type: basic, username: u}\n")
    with pytest.raises(ValueError, match="password"):
        load_clusters(str(f))


def test_local_store_conditional_writes(tmp_path):
    s = LocalStore(str(tmp_path))
    etag = s.put_json("a/b.json", {"x": 1}, if_none_match=True)
    with pytest.raises(PreconditionFailed):
        s.put_json("a/b.json", {"x": 2}, if_none_match=True)
    with pytest.raises(PreconditionFailed):
        s.put_json("a/b.json", {"x": 2}, if_match='"nope"')
    s.put_json("a/b.json", {"x": 3}, if_match=etag)
    assert s.get_json("a/b.json")[0] == {"x": 3}
    assert s.list_keys("a/") == ["a/b.json"]
    with pytest.raises(ValueError):
        s.get_json("../escape.json")


def test_diff():
    d = diff({"a": {"b": 1, "c": 2}, "l": [1]}, {"a": {"b": 1, "c": 3, "d": 4}})
    assert d == {"added": [{"path": "a.d", "after": 4}],
                 "removed": [{"path": "l", "before": [1]}],
                 "changed": [{"path": "a.c", "before": 2, "after": 3}]}
