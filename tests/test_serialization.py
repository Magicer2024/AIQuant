"""
tests/test_serialization.py — 公共序列化函数测试
"""
import math
import unittest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestSanitizeNumeric(unittest.TestCase):
    def test_nan_returns_none(self):
        from utils.serialization import sanitize_numeric
        self.assertIsNone(sanitize_numeric(float('nan')))

    def test_inf_returns_none(self):
        from utils.serialization import sanitize_numeric
        self.assertIsNone(sanitize_numeric(float('inf')))
        self.assertIsNone(sanitize_numeric(float('-inf')))

    def test_normal_float_passthrough(self):
        from utils.serialization import sanitize_numeric
        self.assertEqual(sanitize_numeric(3.14), 3.14)
        self.assertEqual(sanitize_numeric(0.0), 0.0)

    def test_nested_dict(self):
        from utils.serialization import sanitize_numeric
        data = {"score": float('nan'), "items": [{"v": float('inf')}]}
        result = sanitize_numeric(data)
        self.assertIsNone(result["score"])
        self.assertIsNone(result["items"][0]["v"])

    def test_nested_list(self):
        from utils.serialization import sanitize_numeric
        data = [float('nan'), 1.0, float('inf')]
        result = sanitize_numeric(data)
        self.assertIsNone(result[0])
        self.assertEqual(result[1], 1.0)
        self.assertIsNone(result[2])

    def test_bytes_decoded(self):
        from utils.serialization import sanitize_numeric
        result = sanitize_numeric(b"hello")
        self.assertEqual(result, "hello")

    def test_empty_container(self):
        from utils.serialization import sanitize_numeric
        self.assertEqual(sanitize_numeric([]), [])
        self.assertEqual(sanitize_numeric({}), {})

    def test_none_passthrough(self):
        from utils.serialization import sanitize_numeric
        self.assertIsNone(sanitize_numeric(None))

    def test_string_passthrough(self):
        from utils.serialization import sanitize_numeric
        self.assertEqual(sanitize_numeric("hello"), "hello")

    def test_int_passthrough(self):
        from utils.serialization import sanitize_numeric
        self.assertEqual(sanitize_numeric(42), 42)


if __name__ == "__main__":
    unittest.main()
