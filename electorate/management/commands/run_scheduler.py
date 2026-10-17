import time

from django.core.management.base import BaseCommand

from electorate.services import close_due_elections, open_due_elections


class Command(BaseCommand):
    help = ("Open elections whose opening time has passed (freezing the electorate) and "
            "close those whose closing time has passed (sealing the ballot box). "
            "Run every minute, or with --loop.")

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true", help="Keep running.")
        parser.add_argument("--interval", type=int, default=30, help="Seconds between runs.")

    def handle(self, *args, loop: bool, interval: int, **opts):
        while True:
            for election_id, outcome in close_due_elections() + open_due_elections():
                self.stdout.write(f"election {election_id}: {outcome}")
            if not loop:
                return
            time.sleep(max(5, interval))
