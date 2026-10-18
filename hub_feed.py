"""MONST HUB standalone feed runner: no Discord calls or secrets."""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from bot import gather, ISSUES
from campaign_feed import export_feed


def main():
    logging.basicConfig(level=logging.INFO)
    now = datetime.now(ZoneInfo('Asia/Tokyo'))
    try:
        records = gather(now.date())
    except Exception:
        export_feed([], ['gather_failed'], now)
        raise
    export_feed(records, ISSUES, now)
    print(f'Validated candidates: {len(records)}, warnings: {len(ISSUES)}')
    if ISSUES:
        print('Feed degraded: first partial snapshot only, or previous snapshot preserved.')


if __name__ == '__main__':
    main()
