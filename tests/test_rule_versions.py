"""
tests/test_rule_versions.py —— 策略版本管理
"""
import unittest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.db import get_conn, init_db


class TestRuleVersionTable(unittest.TestCase):
    def test_table_exists(self):
        init_db()
        with get_conn() as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='strategy_rule_versions'"
            ).fetchone()
            self.assertIsNotNone(row)


class TestVersionCRUD(unittest.TestCase):
    def setUp(self):
        init_db()

    def test_save_rule_version_new_rule_returns_none(self):
        from strategy.rules_store import save_rule_version
        result = save_rule_version(99999)
        self.assertIsNone(result)

    def test_save_rule_version_existing_rule(self):
        from strategy.rules_store import save_rule, save_rule_version
        rule_id = save_rule({
            "rule_name": "_test_version_rule",
            "rule_type": "buy",
            "encoding": "factor",
            "conditions": '{"test": true}',
            "source": "test",
        })
        version = save_rule_version(rule_id)
        self.assertIsNotNone(version)
        self.assertEqual(version, 1)

        from strategy.rules_store import delete_rule
        delete_rule(rule_id)
        with get_conn() as conn:
            conn.execute("DELETE FROM strategy_rule_versions WHERE rule_id = ?", (rule_id,))

    def test_get_rule_versions(self):
        from strategy.rules_store import save_rule, save_rule_version, get_rule_versions, delete_rule
        rule_id = save_rule({
            "rule_name": "_test_version_list",
            "rule_type": "buy",
            "encoding": "factor",
            "conditions": '{"v": 1}',
            "source": "test",
        })
        save_rule_version(rule_id)
        versions = get_rule_versions(rule_id)
        self.assertGreaterEqual(len(versions), 1)
        self.assertEqual(versions[0]["version"], 1)

        delete_rule(rule_id)
        with get_conn() as conn:
            conn.execute("DELETE FROM strategy_rule_versions WHERE rule_id = ?", (rule_id,))

    def test_rollback_creates_new_version(self):
        from strategy.rules_store import (
            save_rule, save_rule_version, rollback_rule, get_rule_versions, delete_rule
        )
        rule_id = save_rule({
            "rule_name": "_test_rollback",
            "rule_type": "buy",
            "encoding": "factor",
            "conditions": '{"v": 1}',
            "sell_conditions": '[]',
            "source": "test",
        })
        v1 = save_rule_version(rule_id)

        with get_conn() as conn:
            conn.execute("UPDATE strategy_rules SET conditions = ? WHERE id = ?",
                         ('{"v": 2}', rule_id))

        ok = rollback_rule(rule_id, v1)
        self.assertTrue(ok)

        versions = get_rule_versions(rule_id)
        self.assertGreaterEqual(len(versions), 3)

        delete_rule(rule_id)
        with get_conn() as conn:
            conn.execute("DELETE FROM strategy_rule_versions WHERE rule_id = ?", (rule_id,))

    def test_rollback_nonexistent_returns_false(self):
        from strategy.rules_store import rollback_rule
        result = rollback_rule(99999, 999)
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
