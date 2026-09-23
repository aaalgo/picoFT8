import sqlite3
import tempfile
import unittest
from pathlib import Path

from models import FT8Message, NS_PER_SECOND, QSOPhase, SLOT_NS
from qso import RETRY_DELAYS, QSOStatus
from server import CALLSIGN2QSO, OFFER_RETENTION_NS, create_app


class NanosecondAPITests(unittest.TestCase):
    def setUp(self):
        CALLSIGN2QSO.clear()
        self.slot = 1_800_000_000 * NS_PER_SECOND
        self.now = self.slot
        self.client = create_app('sqlite:///:memory:', now_ns=lambda: self.now).test_client()

    def tearDown(self):
        CALLSIGN2QSO.clear()

    def receive(self, utc_ns, payload='EN82'):
        response = self.client.post('/api/rx/', json=[{
            'site_id': 1, 'calibrated_utc_ns': utc_ns,
            'decode': {'message': f'AC8SS W1ABC {payload}', 'snr_db': -8},
        }])
        self.assertEqual(response.status_code, 201)

    def offer(self, utc_ns):
        response = self.client.get('/api/tx/todo/', query_string={'utc_ns': utc_ns})
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def acknowledge(self, utc_ns, handle):
        return self.client.post('/api/tx/', json={'utc_ns': utc_ns, 'handle': handle})

    def test_exact_round_trip_and_one_nanosecond_slot_boundary(self):
        timestamp = self.slot + SLOT_NS - 1
        self.receive(timestamp)
        self.assertEqual(self.client.get('/api/query/').get_json()[0]['calibrated_utc_ns'], timestamp)
        self.assertFalse(FT8Message(utc_ns=timestamp).is_odd)
        self.assertTrue(FT8Message(utc_ns=timestamp + 1).is_odd)
        self.assertIsNone(self.offer(timestamp)['handle'])
        offer = self.offer(timestamp + 1)
        self.assertEqual(offer['message'], 'W1ABC AC8SS -08')
        self.assertEqual(self.acknowledge(timestamp, offer['handle']).status_code, 409)
        self.assertEqual(self.acknowledge(timestamp + 1, offer['handle']).status_code, 200)
        self.assertEqual(self.acknowledge(timestamp + 1, offer['handle']).status_code, 200)

    def test_retry_delays_remain_seconds_with_nanosecond_timestamps(self):
        self.receive(self.slot + 123456789)
        qso = CALLSIGN2QSO['W1ABC']
        sending = self.slot + SLOT_NS
        for index, delay in enumerate(RETRY_DELAYS):
            offer = self.offer(sending)
            self.assertIsNotNone(offer['handle'])
            self.assertEqual(self.acknowledge(sending, offer['handle']).status_code, 200)
            if index + 1 < len(RETRY_DELAYS):
                sending += delay * NS_PER_SECOND
                self.assertEqual(qso.next_tx_utc_ns, sending)
                self.assertIsNone(self.offer(sending - 2 * SLOT_NS)['handle'])
        self.assertEqual(qso.status, QSOStatus.EXPIRED)

    def test_exchange_and_rx_preserves_offers(self):
        self.receive(self.slot)
        stale = self.offer(self.slot + SLOT_NS)['handle']
        self.receive(self.slot + 1)
        self.assertEqual(self.acknowledge(self.slot + SLOT_NS, stale).status_code, 200)
        offer = self.offer(self.slot + SLOT_NS)
        self.assertEqual(self.acknowledge(self.slot + 2 * SLOT_NS, offer['handle']).status_code, 409)
        self.assertEqual(self.acknowledge(self.slot + SLOT_NS, offer['handle']).status_code, 200)
        self.receive(self.slot + 2 * SLOT_NS, 'R-12')
        offer = self.offer(self.slot + 3 * SLOT_NS)
        self.assertEqual(offer['message'], 'W1ABC AC8SS RR73')
        self.assertEqual(self.acknowledge(self.slot + 3 * SLOT_NS, offer['handle']).status_code, 200)
        self.receive(self.slot + 4 * SLOT_NS, '73')
        self.assertEqual(CALLSIGN2QSO['W1ABC'].status, QSOStatus.COMPLETED)
        self.assertIsNone(self.offer(self.slot + 5 * SLOT_NS)['handle'])

    def test_slot_specific_offers_and_duplicate_acknowledgments(self):
        self.receive(self.slot)
        target = self.slot + SLOT_NS
        first = self.offer(target)
        self.assertEqual(self.offer(target), first)
        later = self.offer(target + 2 * SLOT_NS)
        self.assertNotEqual(first['handle'], later['handle'])
        self.assertEqual(self.acknowledge(target + 2 * SLOT_NS, first['handle']).status_code, 409)
        qso = CALLSIGN2QSO['W1ABC']
        self.assertEqual(qso.current_stage.tx_count, 0)
        self.assertEqual(self.acknowledge(target, first['handle']).status_code, 200)
        schedule = (qso.retry_index, qso.next_tx_utc_ns, qso.scheduling_revision)
        self.assertEqual(self.acknowledge(target, first['handle']).status_code, 200)
        self.assertEqual(qso.current_stage.tx_count, 1)
        self.assertEqual((qso.retry_index, qso.next_tx_utc_ns, qso.scheduling_revision), schedule)
        self.assertEqual(self.acknowledge(target + 2 * SLOT_NS, later['handle']).status_code, 200)
        self.assertEqual(qso.current_stage.tx_count, 2)
        self.assertEqual((qso.retry_index, qso.next_tx_utc_ns, qso.scheduling_revision), schedule)

    def test_rx_phase_and_parity_changes_preserve_old_stage_accounting(self):
        self.receive(self.slot)
        target = self.slot + 3 * SLOT_NS
        old = self.offer(target)
        self.receive(self.slot + SLOT_NS, 'R-12')  # New phase AND RX parity.
        qso = CALLSIGN2QSO['W1ABC']
        schedule = (qso.retry_index, qso.next_tx_utc_ns, qso.scheduling_revision)
        self.assertEqual(self.acknowledge(target, old['handle']).status_code, 200)
        self.assertEqual(qso.phase, QSOPhase.CONFIRMED)
        self.assertEqual(qso.stages[QSOPhase.REPLIED].tx_count, 1)
        self.assertEqual(qso.current_stage.tx_count, 0)
        self.assertEqual((qso.retry_index, qso.next_tx_utc_ns, qso.scheduling_revision), schedule)

    def test_changed_offer_for_same_slot_preserves_both_handles(self):
        self.receive(self.slot)
        target = self.slot + 3 * SLOT_NS
        old = self.offer(target)
        self.receive(self.slot + 2 * SLOT_NS, 'R-12')
        new = self.offer(target)
        self.assertNotEqual(old['handle'], new['handle'])
        self.assertNotEqual(old['message'], new['message'])
        self.assertEqual(self.offer(target), new)
        # Each handle describes a separately reported transmission, not a slot reservation.
        self.assertEqual(self.acknowledge(target, old['handle']).status_code, 200)
        self.assertEqual(self.acknowledge(target, new['handle']).status_code, 200)
        qso = CALLSIGN2QSO['W1ABC']
        self.assertEqual(qso.stages[QSOPhase.REPLIED].tx_count, 1)
        self.assertEqual(qso.stages[QSOPhase.CONFIRMED].tx_count, 1)
        self.assertEqual(qso.retry_index, 1)

    def test_completion_and_expiration_do_not_revoke_offers(self):
        self.receive(self.slot, 'R-12')
        target = self.slot + 3 * SLOT_NS
        old = self.offer(target)
        self.receive(self.slot + 2 * SLOT_NS, '73')
        qso = CALLSIGN2QSO['W1ABC']
        revision = qso.scheduling_revision
        self.assertEqual(self.acknowledge(target, old['handle']).status_code, 200)
        self.assertEqual(qso.status, QSOStatus.COMPLETED)
        self.assertIsNone(qso.next_tx_utc_ns)
        self.assertEqual(qso.scheduling_revision, revision)
        self.assertEqual(qso.stages[QSOPhase.CONFIRMED].tx_count, 1)

        CALLSIGN2QSO.clear()
        self.receive(self.slot)
        old = self.offer(target)
        qso = CALLSIGN2QSO['W1ABC']
        qso.expire()
        self.assertEqual(self.acknowledge(target, old['handle']).status_code, 200)
        self.assertEqual(qso.status, QSOStatus.EXPIRED)
        self.assertIsNone(qso.next_tx_utc_ns)

    def test_out_of_order_ack_does_not_regress_last_tx(self):
        self.receive(self.slot)
        early_slot = self.slot + SLOT_NS
        late_slot = early_slot + 2 * SLOT_NS
        early, late = self.offer(early_slot), self.offer(late_slot)
        self.assertEqual(self.acknowledge(late_slot, late['handle']).status_code, 200)
        self.assertEqual(self.acknowledge(early_slot, early['handle']).status_code, 200)
        qso = CALLSIGN2QSO['W1ABC']
        self.assertEqual(qso.current_stage.last_tx_utc_ns, late_slot)
        self.assertEqual(qso.next_tx_utc_ns, late_slot + 30 * NS_PER_SECOND)
        self.assertEqual(qso.retry_index, 1)

    def test_concurrent_duplicate_acknowledgments_count_once(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier

        self.receive(self.slot)
        target = self.slot + SLOT_NS
        offer = self.offer(target)
        barrier = Barrier(8)
        app = self.client.application

        def acknowledge():
            with app.test_client() as client:
                barrier.wait(timeout=5)
                return client.post('/api/tx/', json={
                    'utc_ns': target, 'handle': offer['handle'],
                }).status_code

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: acknowledge(), range(8)))
        self.assertEqual(results, [200] * 8)
        qso = CALLSIGN2QSO['W1ABC']
        self.assertEqual(qso.current_stage.tx_count, 1)
        self.assertEqual(qso.retry_index, 1)

    def test_offer_and_receipt_retention_from_target_not_issue_time(self):
        self.receive(self.slot)
        target = self.slot + 3 * SLOT_NS
        first = self.offer(target)
        outstanding = self.offer(target + 2 * SLOT_NS)
        self.now = target + OFFER_RETENTION_NS - 1
        self.assertEqual(self.acknowledge(target, first['handle']).status_code, 200)
        self.assertEqual(self.acknowledge(target, first['handle']).status_code, 200)
        self.now += 1
        self.assertEqual(self.acknowledge(target, first['handle']).status_code, 409)
        self.assertEqual(self.client.get('/api/tx/todo/', query_string={'utc_ns': target}).status_code, 400)
        self.now = target + 2 * SLOT_NS + OFFER_RETENTION_NS
        self.assertEqual(self.acknowledge(target + 2 * SLOT_NS, outstanding['handle']).status_code, 409)
        self.assertEqual(CALLSIGN2QSO['W1ABC'].current_stage.tx_count, 1)

    def test_tx_requires_integer_ns_field(self):
        for query in ({'utc': self.slot // NS_PER_SECOND}, {'utc_ns': '1.5'}, {}):
            self.assertEqual(self.client.get('/api/tx/todo/', query_string=query).status_code, 400)
        for body in ({'utc': 1800000000, 'handle': None},
                     {'utc_ns': 1.5, 'handle': None}, {'utc_ns': True, 'handle': None}):
            self.assertEqual(self.client.post('/api/tx/', json=body).status_code, 400)
        self.assertEqual(self.acknowledge(self.slot, None).status_code, 200)

    def test_legacy_database_requires_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'legacy.sqlite3'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE ft8_message (utc INTEGER NOT NULL)')
                db.execute('INSERT INTO ft8_message VALUES (1800000000)')
            with self.assertRaisesRegex(RuntimeError, 'Legacy seconds-based database'):
                create_app(f'sqlite:///{path}')
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute('SELECT utc FROM ft8_message').fetchone()[0], 1800000000)
                db.executescript('''BEGIN TRANSACTION;
                    ALTER TABLE ft8_message RENAME COLUMN utc TO utc_ns;
                    UPDATE ft8_message SET utc_ns = utc_ns * 1000000000;
                    COMMIT;''')
                self.assertEqual(db.execute('SELECT utc_ns FROM ft8_message').fetchone()[0], self.slot)


if __name__ == '__main__':
    unittest.main()
