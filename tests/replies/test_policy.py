"""S1 回复选择/过滤行为；只替换外部候选提供方。"""
import asyncio
import importlib.util
from pathlib import Path
import sys
import unittest

PATH = Path(__file__).resolve().parents[2] / 'common/services/reply_policy.py'


def load_policy():
    assert PATH.exists(), '缺少实际回复策略入口'
    spec = importlib.util.spec_from_file_location('reply_policy_fixture', PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class PolicyTest(unittest.IsolatedAsyncioTestCase):
    async def test_migration_default_precedes_ai_and_new_reverses(self):
        p = load_policy()
        for strategy, expected in [('legacy', 'default'), ('ai_first', 'ai')]:
            seen = []
            async def provide(name):
                seen.append(name)
                return p.ReplyDecision('body', name, name) if name in {'ai', 'default'} else p.ReplyDecision('unmatched', name)
            result = await p.select_reply(strategy, provide)
            self.assertEqual(result.source, expected)
            self.assertEqual(seen[:2], ['exclusive', 'keyword'])

    async def test_skip_terminates_and_error_falls_through(self):
        p = load_policy()
        for kind, expected in [('skip', 'default'), ('error', 'ai')]:
            async def provide(name):
                if name == 'default':
                    return p.ReplyDecision(kind, name)
                return p.ReplyDecision('body', name, 'AI') if name == 'ai' else p.ReplyDecision('unmatched', name)
            self.assertEqual((await p.select_reply('legacy', provide)).source, expected)

    def test_filter_actions_sources_scope_and_notify_is_not_block(self):
        p = load_policy()
        rules = [dict(id=1, pattern='bad', match_mode='contains', source='ai', item_id='item', actions=['notify'], enabled=True)]
        self.assertEqual(p.evaluate_filters(rules, 'bad text', 'user', 'item').actions, frozenset())
        result = p.evaluate_filters(rules, 'bad text', 'ai', 'item')
        self.assertEqual(result.actions, frozenset({'notify'}))
        self.assertFalse(result.blocks_reply)
        rules[0].update(actions=['skip_reply'], match_mode='exact')
        self.assertFalse(p.evaluate_filters(rules, 'bad text', 'ai', 'item').blocks_reply)
        self.assertTrue(p.evaluate_filters(rules, 'bad', 'ai', 'item').blocks_reply)

    def test_regex_validation_bounds(self):
        p = load_policy()
        for pattern in ['(', '(a+)+$', r'(a|aa)+$', 'a' * 257, r'(a)\1']:
            with self.subTest(pattern=pattern), self.assertRaises(ValueError):
                p.validate_filter(dict(pattern=pattern, match_mode='regex', source='user', actions=['skip_reply']))
        rule = p.validate_filter(dict(pattern='^hello [0-9]+$', match_mode='regex', source='user', actions=['skip_ai']))
        self.assertEqual(p.evaluate_filters([rule], 'hello 12', 'user', '').actions, frozenset({'skip_ai'}))
