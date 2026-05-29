"""
tests/test_serialization.py — 公共序列化函数测试
"""
import unittest

from utils.serialization import sanitize_numeric


class TestSanitizeNumeric(unittest.TestCase):
    def test_nan_returns_none(self):
        self.assertIsNone(sanitize_numeric(float('nan')))

    def test_inf_returns_none(self):
        self.assertIsNone(sanitize_numeric(float('inf')))
        self.assertIsNone(sanitize_numeric(float('-inf')))

    def test_normal_float_passthrough(self):
        self.assertEqual(sanitize_numeric(3.14), 3.14)
        self.assertEqual(sanitize_numeric(0.0), 0.0)

    def test_nested_dict(self):
        data = {"score": float('nan'), "items": [{"v": float('inf')}]}
        result = sanitize_numeric(data)
        self.assertIsNone(result["score"])
        self.assertIsNone(result["items"][0]["v"])

    def test_nested_list(self):
        data = [float('nan'), 1.0, float('inf')]
        result = sanitize_numeric(data)
        self.assertIsNone(result[0])
        self.assertEqual(result[1], 1.0)
        self.assertIsNone(result[2])

    def test_tuple_input(self):
        result = sanitize_numeric((1.0, float('nan')))
        self.assertEqual(result, [1.0, None])

    def test_bytes_decoded(self):
        result = sanitize_numeric(b"hello")
        self.assertEqual(result, "hello")

    def test_empty_container(self):
        self.assertEqual(sanitize_numeric([]), [])
        self.assertEqual(sanitize_numeric({}), {})

    def test_none_passthrough(self):
        self.assertIsNone(sanitize_numeric(None))

    def test_string_passthrough(self):
        self.assertEqual(sanitize_numeric("hello"), "hello")

    def test_int_passthrough(self):
        self.assertEqual(sanitize_numeric(42), 42)

    def test_numpy_scalar_conversion(self):
        """numpy float/int scalars should be converted to Python native via .item()"""
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy not installed")
        self.assertEqual(sanitize_numeric(np.float64(3.14)), 3.14)
        self.assertEqual(sanitize_numeric(np.int64(42)), 42)


if __name__ == "__main__":
    unittest.main()
