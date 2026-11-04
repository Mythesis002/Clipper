"""Postgres scheduler integration tests; run with TEST_DATABASE_URL against migrated DB."""
import os
import threading
import unittest
import uuid
from datetime import date, time, datetime, timezone

try:
    import psycopg
except ImportError:
    psycopg=None

from scheduler.materializer import materialize_user
from scheduler.queue import claim_slots_near_due, transition_slot


DSN=os.environ.get("TEST_DATABASE_URL","").strip()


@unittest.skipUnless(DSN and psycopg,"Set TEST_DATABASE_URL and install psycopg to run Postgres integration tests")
class PostgreSQLSchedulerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn=psycopg.connect(DSN)
        cls.created_users=[]

    @classmethod
    def tearDownClass(cls):
        if cls.created_users:
            with cls.conn.cursor() as cur:
                cur.execute("DELETE FROM users WHERE id=ANY(%s)",(cls.created_users,))
            cls.conn.commit()
        cls.conn.close()

    def _tenant(self,tz="Asia/Kolkata"):
        user=str(uuid.uuid4())
        account=str(uuid.uuid4())
        with self.conn.cursor() as cur:
            cur.execute("INSERT INTO users(id,email,tz) VALUES (%s,%s,%s)",(user,f"{user}@test.invalid",tz))
            cur.execute("INSERT INTO connected_accounts(id,user_id,platform,external_id) VALUES (%s,%s,'instagram',%s)",
                        (account,user,f"ig-{user}"))
        self.conn.commit()
        self.created_users.append(user)
        return user,account

    def test_materializer_is_idempotent(self):
        user,account=self._tenant("Asia/Kathmandu")
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute("INSERT INTO schedule_rules(user_id,account_id,weekday,local_time) VALUES (%s,%s,2,'18:00')",
                            (user,account))
        now=datetime(2026,11,4,12,0,tzinfo=timezone.utc) # Wednesday in Kathmandu
        with self.conn.transaction():
            first=materialize_user(self.conn,user,now_utc=now,horizon_days=7)
        with self.conn.transaction():
            second=materialize_user(self.conn,user,now_utc=now,horizon_days=7)
        self.assertGreater(first,0)
        self.assertEqual(second,0)

    def test_duplicate_dispatchers_claim_500_due_slots_without_overlap(self):
        users=[]; accounts=[]
        with self.conn.cursor() as cur:
            for i in range(500):
                user=str(uuid.uuid4()); account=str(uuid.uuid4())
                cur.execute("INSERT INTO users(id,email,tz) VALUES (%s,%s,'UTC')",(user,f"load-{i}-{user}@test.invalid"))
                cur.execute("INSERT INTO connected_accounts(id,user_id,platform,external_id) VALUES (%s,%s,'instagram',%s)",
                            (account,user,f"ig-load-{i}"))
                cur.execute(
                    """INSERT INTO slots(user_id,account_id,local_date,local_time,tz_snapshot,run_at_utc,state)
                       VALUES (%s,%s,%s,%s,'UTC',now(),'container_ready') RETURNING id""",
                    (user,account,date(2026,11,4),time(18,i%60)),
                )
                # Same-minute burst; duplicate wall times are allowed across accounts.
                users.append(user); accounts.append(account)
        self.conn.commit(); self.created_users.extend(users)

        claimed=[]; lock=threading.Lock(); errors=[]
        def dispatcher(name):
            try:
                with psycopg.connect(DSN) as conn,conn.transaction():
                    rows=claim_slots_near_due(conn,name,seconds_ahead=1,limit=100,lease_seconds=60)
                with lock: claimed.extend(str(row[0]) for row in rows)
            except Exception as exc:
                with lock: errors.append(exc)
        threads=[threading.Thread(target=dispatcher,args=(f"dispatcher-{i}",)) for i in range(5)]
        for t in threads:t.start()
        for t in threads:t.join()
        self.assertFalse(errors,errors)
        self.assertEqual(len(claimed),500)
        self.assertEqual(len(set(claimed)),500)

    def test_publishing_intent_is_not_claimed_after_crash_or_timeout(self):
        user,account=self._tenant("UTC")
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO slots(user_id,account_id,local_date,local_time,tz_snapshot,run_at_utc,state,container_id)
                   VALUES (%s,%s,'2026-11-04','18:00','UTC',now(),'container_ready','container-123') RETURNING id""",
                (user,account))
            slot_id=cur.fetchone()[0]
        self.conn.commit()
        dispatcher="dispatcher-crash-test"
        with self.conn.transaction():
            rows=claim_slots_near_due(self.conn,dispatcher,seconds_ahead=1,limit=1)
        self.assertEqual(rows[0][0],slot_id)
        with self.conn.transaction():
            changed=transition_slot(self.conn,slot_id,"container_ready","publishing",dispatcher,
                                    publish_started_at=datetime.now(timezone.utc))
        self.assertTrue(changed)
        # Simulate timeout outcome. The state is terminal for automatic dispatch.
        with self.conn.transaction():
            changed=transition_slot(self.conn,slot_id,"publishing","publish_uncertain",
                                    last_error="simulated timeout",lease_owner=None,lease_until=None)
        self.assertTrue(changed)
        with self.conn.transaction():
            again=claim_slots_near_due(self.conn,"dispatcher-retry",seconds_ahead=1,limit=10)
        self.assertEqual(again,[])


if __name__=="__main__":
    unittest.main()
