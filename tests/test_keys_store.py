import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import keys_store


def sample_key(**overrides):
    payload = {
        "id": "kalpha00001",
        "name": "主 Key",
        "api_key": "sk-abcdefghijklmnop",
        "upstream_base_url": "https://api.example.com/v1",
        "enabled": True,
        "order": 0,
        "selected_models": None,
    }
    payload.update(overrides)
    return payload


def payload_with(*entries, priority=None):
    return {"version": 1, "keys": list(entries), "model_priority": {} if priority is None else priority}


class ValidateTests(unittest.TestCase):
    def test_orders_and_validates_keys(self):
        data = keys_store.validate_keys(payload_with(
            sample_key(id="kbeta00002", name="备用", order=9),
            sample_key(order=-3),
        ))
        self.assertEqual([entry["id"] for entry in data["keys"]], ["kalpha00001", "kbeta00002"])
        self.assertEqual([entry["order"] for entry in data["keys"]], [0, 1])
        self.assertEqual(keys_store.ordered_keys(data)[0]["name"], "主 Key")

    def test_rejects_duplicate_ids_and_names(self):
        with self.assertRaises(keys_store.KeysError):
            keys_store.validate_keys(payload_with(sample_key(), sample_key(id="kbeta00002")))
        with self.assertRaises(keys_store.KeysError):
            keys_store.validate_keys(payload_with(sample_key(), sample_key(name="主 Key", id="kbeta00002")))

    def test_rejects_bad_ids_names_and_keys(self):
        for bad in (sample_key(id="BAD ID"), sample_key(id=""), sample_key(id="中文"), sample_key(id="x" * 40)):
            with self.assertRaises(keys_store.KeysError):
                keys_store.validate_keys(payload_with(bad))
        for bad in (sample_key(name="  "), sample_key(name="n" * 65), sample_key(api_key=""), sample_key(api_key="sk\nkey")):
            with self.assertRaises(keys_store.KeysError):
                keys_store.validate_keys(payload_with(bad))

    def test_rejects_non_boolean_enabled_and_non_integer_order(self):
        with self.assertRaises(keys_store.KeysError):
            keys_store.validate_keys(payload_with(sample_key(enabled="yes")))
        with self.assertRaises(keys_store.KeysError):
            keys_store.validate_keys(payload_with(sample_key(order=True)))

    def test_upstream_is_normalised_and_validated(self):
        data = keys_store.validate_keys(payload_with(sample_key(upstream_base_url="https://example.com/v1/")))
        self.assertEqual(data["keys"][0]["upstream_base_url"], "https://example.com/v1")
        defaults = keys_store.validate_keys(payload_with({
            "id": "kalpha00001", "name": "主 Key", "api_key": "sk-abcdefghijklmnop",
        }))
        self.assertEqual(defaults["keys"][0]["upstream_base_url"], keys_store.DEFAULT_UPSTREAM)
        for bad in ("example.com/v1", "", "https://exa mple.com", "ftp://example.com"):
            with self.assertRaises(keys_store.KeysError):
                keys_store.validate_keys(payload_with(sample_key(upstream_base_url=bad)))

    def test_selected_models_none_means_everything(self):
        all_models = keys_store.validate_keys(payload_with(sample_key(selected_models=None)))["keys"][0]
        self.assertIsNone(all_models["selected_models"])
        self.assertIsNone(keys_store.selected_models_for(all_models))
        self.assertEqual(keys_store.apply_selected_models(all_models, ["a", "b"]), ["a", "b"])

        none_selected = keys_store.validate_keys(payload_with(sample_key(selected_models=[])))["keys"][0]
        self.assertEqual(none_selected["selected_models"], [])
        self.assertEqual(keys_store.apply_selected_models(none_selected, ["a", "b"]), [])

        picked = keys_store.validate_keys(payload_with(sample_key(selected_models=["b", " b ", "a"])))["keys"][0]
        self.assertEqual(picked["selected_models"], ["b", "a"])
        self.assertEqual(keys_store.apply_selected_models(picked, ["a", "b", "c"]), ["a", "b"])

        with self.assertRaises(keys_store.KeysError):
            keys_store.validate_keys(payload_with(sample_key(selected_models="all")))
        with self.assertRaises(keys_store.KeysError):
            keys_store.validate_keys(payload_with(sample_key(selected_models=[""])))

    def test_model_priority_accepts_official_and_existing_keys(self):
        data = keys_store.validate_keys(payload_with(
            sample_key(),
            sample_key(id="kbeta00002", name="备用"),
            priority={"gpt-5.1-codex": "official", "gpt-5.1": "kbeta00002", " kimi-k3 ": "kalpha00001"},
        ))
        self.assertEqual(data["model_priority"], {
            "gpt-5.1-codex": "official",
            "gpt-5.1": "kbeta00002",
            "kimi-k3": "kalpha00001",
        })
        with self.assertRaises(keys_store.KeysError):
            keys_store.validate_keys(payload_with(sample_key(), priority={"gpt-5.1": "kghost0001"}))
        with self.assertRaises(keys_store.KeysError):
            keys_store.validate_keys(payload_with(sample_key(), priority={"": "official"}))
        with self.assertRaises(keys_store.KeysError):
            keys_store.validate_keys(payload_with(sample_key(), priority=[]))

    def test_enabled_keys_skips_disabled(self):
        data = keys_store.validate_keys(payload_with(
            sample_key(enabled=False),
            sample_key(id="kbeta00002", name="备用", order=1),
        ))
        self.assertEqual([entry["id"] for entry in keys_store.enabled_keys(data)], ["kbeta00002"])
        self.assertEqual(set(keys_store.key_index(data)), {"kalpha00001", "kbeta00002"})

    def test_make_key_uses_defaults_and_unique_ids(self):
        first = keys_store.make_key("主 Key", "sk-1", existing=())
        second = keys_store.make_key("备用", "sk-2", existing=[first["id"]])
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(first["upstream_base_url"], keys_store.DEFAULT_UPSTREAM)
        self.assertTrue(first["enabled"])
        self.assertIsNone(first["selected_models"])
        with self.assertRaises(keys_store.KeysError):
            keys_store.make_key("  ", "sk-3")

    def test_mask_api_key_hides_middle(self):
        self.assertEqual(keys_store.mask_api_key("sk-abcdefghijklmnop"), "sk-ab******mnop")
        self.assertEqual(keys_store.mask_api_key("short"), "*****")
        self.assertEqual(keys_store.mask_api_key(""), "")

    def test_rejects_wrong_top_level_shape(self):
        with self.assertRaises(keys_store.KeysError):
            keys_store.validate_keys({"version": 2, "keys": []})
        with self.assertRaises(keys_store.KeysError):
            keys_store.validate_keys({"version": 1, "keys": {}})
        with self.assertRaises(keys_store.KeysError):
            keys_store.validate_keys([sample_key()])


class StoreTests(unittest.TestCase):
    def test_roundtrip_writes_utf8_without_bom_and_lf(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "keys.json"
            saved = keys_store.save_keys(path, payload_with(sample_key(), priority={"gpt-5.1": "official"}))
            self.assertEqual(saved["keys"][0]["name"], "主 Key")
            raw = path.read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
            self.assertNotIn(b"\r\n", raw)
            self.assertTrue(raw.endswith(b"\n"))
            self.assertEqual(json.loads(raw.decode("utf-8"))["keys"][0]["name"], "主 Key")
            self.assertEqual(keys_store.load_keys(path), saved)
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_load_accepts_bom_and_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "keys.json"
            path.write_text(json.dumps(payload_with(sample_key()), ensure_ascii=False, indent=2), encoding="utf-8-sig")
            self.assertEqual(keys_store.load_keys(path)["keys"][0]["id"], "kalpha00001")
            missing = Path(tmp) / "missing.json"
            self.assertEqual(keys_store.load_keys(missing), keys_store.default_keys())

    def test_load_rejects_broken_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "keys.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(keys_store.KeysError):
                keys_store.load_keys(path)

    def test_save_rejects_invalid_payload_without_touching_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "keys.json"
            keys_store.save_keys(path, payload_with(sample_key()))
            before = path.read_bytes()
            with self.assertRaises(keys_store.KeysError):
                keys_store.save_keys(path, payload_with(sample_key(name="  ")))
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(sorted(item.name for item in Path(tmp).iterdir()), ["keys.json"])


if __name__ == "__main__":
    unittest.main()
