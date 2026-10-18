import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import bot
from campaign_feed import export_feed

NOW = datetime.fromisoformat('2026-10-18T06:00:00+09:00')


def record():
    return dict(category=bot.CATEGORIES[0], name='書庫ポイント消費0', start='2026-10-18',
                end='2026-10-20', days=[('2026-10-18', '火')], source='公式',
                url='https://www.monster-strike.com/news/20261018_1.html', secret='DO-NOT-EXPORT')


class FeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_allowlist_and_stable_identity(self):
        export_feed([record()], [], NOW, self.root)
        text = (self.root / 'campaigns.json').read_text(encoding='utf-8')
        self.assertNotIn('DO-NOT-EXPORT', text)
        feed = json.loads(text)
        self.assertEqual(feed['schema_version'], 1)
        self.assertEqual(feed['campaigns'][0]['category'], 'archive')
        first = feed['campaigns'][0]['id']
        export_feed([record()], [], NOW, self.root)
        self.assertEqual(first, json.loads((self.root / 'campaigns.json').read_text(encoding='utf-8'))['campaigns'][0]['id'])

    def test_failure_and_empty_keep_snapshot(self):
        export_feed([record()], [], NOW, self.root)
        previous = (self.root / 'campaigns.json').read_bytes()
        for records, issues in [([], []), ([record()], ['secret exception'])]:
            export_feed(records, issues, NOW, self.root)
            self.assertEqual(previous, (self.root / 'campaigns.json').read_bytes())
            status = json.loads((self.root / 'status.json').read_text())
            self.assertEqual(status['status'], 'degraded')
            self.assertNotIn('secret', json.dumps(status))

    def test_first_failure_has_no_snapshot(self):
        export_feed([], ['failed'], NOW, self.root)
        self.assertFalse((self.root / 'campaigns.json').exists())
        self.assertFalse(json.loads((self.root / 'status.json').read_text())['has_snapshot'])

    def test_invalid_record_does_not_replace_snapshot(self):
        export_feed([record()], [], NOW, self.root)
        previous = (self.root / 'campaigns.json').read_bytes()
        for changes in [dict(end='2026-10-01'), dict(url='https://discord.com/api/webhooks/123/secret'),
                        dict(days=[('2026-10-19', 'invalid')])]:
            with self.assertRaises(ValueError):
                export_feed([{**record(), **changes}], [], NOW, self.root)
            self.assertEqual(previous, (self.root / 'campaigns.json').read_bytes())

    def test_main_preserves_check_and_notifications(self):
        with patch.object(bot, 'STATE', self.root / 'seen.json'), patch.object(bot, 'gather', return_value=[record()]), \
             patch.object(bot, 'export_feed') as export, patch.object(bot, 'publish') as publish:
            with patch.dict(os.environ, {'MODE': 'check'}):
                bot.main()
            export.assert_not_called()
            publish.assert_not_called()
            self.assertFalse(bot.STATE.exists())
            export.side_effect = OSError('export failed')
            with patch.dict(os.environ, {'MODE': 'weekly', 'DISCORD_WEBHOOK_URL': 'test'}):
                bot.main()
            self.assertEqual(publish.call_count, 1)
            self.assertTrue(publish.call_args.args[3])

    def test_delivery_resume(self):
        state = {}
        with patch.object(bot, 'STATE', self.root / 'seen.json'), \
             patch.object(bot, 'render', return_value=['one', 'two']), \
             patch.object(bot.time, 'sleep'), patch.object(bot, 'send', side_effect=[None, RuntimeError('failed')]):
            with self.assertRaises(RuntimeError):
                bot.publish(state, [], NOW.date(), False, 'webhook')
        self.assertEqual(state['deliveries']['daily:2026-10-18']['sent'], 1)
        with patch.object(bot, 'STATE', self.root / 'seen.json'), patch.object(bot.time, 'sleep'), patch.object(bot, 'send') as send:
            bot.publish(state, [], NOW.date(), False, 'webhook')
            bot.publish(state, [], NOW.date(), False, 'webhook')
        send.assert_called_once_with('webhook', 'two')


if __name__ == '__main__':
    unittest.main()
