import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from strataleaf import Conflict, Database, InvalidDocument


class WriteBatchTests(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.db = Database(Path(scratch.name) / "batch.sqlite")
        self.addCleanup(self.db.close)
        self.db.catalog.create("items", {"fields": {"code": "string"}, "required": ["code"]})
        self.db.indexes.create("items", "unique_code", "code", unique=True)
        self.initial = self.db.batch().put("items", "a", {"code": "A"}).commit()

    def keys_with_code(self, code):
        result = self.db.query("items", {"field": "code", "op": "eq", "value": code})
        return [record.key for record in result.records]

    def test_version_binding_error_precedes_same_operation_index_conflict(self):
        for expires_at in (1 << 63, -(1 << 63) - 1):
            with self.subTest(expires_at=expires_at):
                batch = self.db.batch("test").put("items", "prefix", {"code": "OK"})
                batch.put("items", "b", {"code": "A"}, expires_at=expires_at)
                with self.assertRaisesRegex(OverflowError, "too large to convert to SQLite INTEGER"):
                    batch.commit()
                self.assertEqual(self.db.revision, self.initial.revision)
                self.assertIsNone(self.db.get("items", "prefix"))
                self.assertEqual(self.keys_with_code("OK"), [])
                self.assertEqual(self.keys_with_code("A"), ["a"])

        result = self.db.batch("test").put("items", "b", {"code": "B"}).commit()
        self.assertFalse(result.replayed)

    def test_earlier_index_conflict_precedes_later_binding_error(self):
        batch = self.db.batch().put("items", "b", {"code": "A"})
        batch.put("items", "c", {"code": "C"}, expires_at=10**30)
        with self.assertRaisesRegex(Conflict, "unique index conflict: unique_code"):
            batch.commit()
        self.assertEqual(self.db.revision, self.initial.revision)

    def test_schema_check_precedes_binding_error_in_earlier_operation(self):
        batch = self.db.batch().put("items", "b", {"code": "B"}, expires_at=10**30)
        batch.put("items", "c", {"code": False})
        with self.assertRaises(InvalidDocument):
            batch.commit()
        self.assertEqual(self.db.revision, self.initial.revision)

    def test_index_conflict_rolls_back_and_release_allows_key_transfer(self):
        self.db.indexes.create("items", "unique_label", "label", unique=True)
        self.db.batch().put("items", "a", {"code": "A", "label": "shared"}).commit()
        self.db.batch().put("items", "b", {"code": "B"}).commit()
        before = self.db.revision
        batch = self.db.batch().put("items", "a", {"code": "C"})
        batch.put("items", "b", {"code": "A", "label": "shared"})
        batch.put("items", "c", {"code": "B", "label": "shared"})
        with self.assertRaisesRegex(Conflict, "unique index conflict: unique_label"):
            batch.commit()
        self.assertEqual(self.db.revision, before)
        self.assertEqual(self.keys_with_code("A"), ["a"])
        self.assertEqual(self.keys_with_code("B"), ["b"])
        self.assertEqual(self.keys_with_code("C"), [])

        self.db.batch().delete("items", "a").put(
            "items", "b", {"code": "A", "label": "shared"}
        ).put("items", "c", {"code": "B"}).commit()
        self.assertEqual(self.keys_with_code("A"), ["b"])
        self.assertEqual(self.keys_with_code("B"), ["c"])


if __name__ == "__main__":
    unittest.main()
