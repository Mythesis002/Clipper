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
from scheduler.provider_limits import provider_request,ProviderCircuitOpen
from scheduler.dispatcher import _publish_one


DSN=os.environ.get("TEST_DATABASE_URL","").strip()


@unittest.skipUnless(DSN and psycopg,"Set TEST_DATABASE_URL and install psycopg to run Postgres integration tests")
class PostgreSQLSchedulerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn=psycopg.connect(DSN)
        cls.created_users=[]
        # Keep integration-test failures bounded: a leaked row lock or blocked
        # query should fail with a useful traceback instead of consuming the
        # entire GitHub Actions job timeout.
        with cls.conn.cursor() as cur:
            cur.execute("SET statement_timeout = '30s'")
            cur.execute("SET lock_timeout = '5s'")
        cls.conn.commit()

    def setUp(self):
        # psycopg starts implicit transactions for cursor operations. Reset any
        # transaction left by a previous test before opening a new test case.
        if not self.conn.closed:
            self.conn.rollback()
            with self.conn.cursor() as cur:
                cur.execute("SET statement_timeout = '30s'")
                cur.execute("SET lock_timeout = '5s'")
            self.conn.commit()

    def tearDown(self):
        # Do not let one failed test poison the shared connection or retain
        # locks that make later provider/dispatcher tests hang.
        if not self.conn.closed:
            self.conn.rollback()

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "conn", None) is not None and not cls.conn.closed:
            cls.conn.close()

    # Test rows intentionally remain in the ephemeral CI database. The audit
    # events table is append-only, so cascading cleanup would violate its trigger.

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




    def test_stale_dispatcher_cannot_fail_slot_when_meta_circuit_is_open(self):
        from scheduler.provider_limits import ProviderCircuitOpen
        user,account=self._tenant("UTC")
        owner="current-dispatcher"
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO slots(user_id,account_id,local_date,local_time,tz_snapshot,run_at_utc,
                   state,container_id,quota_checked_at,lease_owner,lease_until)
                   VALUES (%s,%s,'2026-11-04','20:00','UTC',now(),'container_ready','container-circuit',
                   now(),%s,now()+interval '60 seconds') RETURNING id,run_at_utc,max_late_minutes,attempts""",
                (user,account,owner))
            row=cur.fetchone()
        self.conn.commit()
        slot=(str(row[0]),user,account,None,"container-circuit",row[1],row[2],row[3])
        with patch("scheduler.dispatcher.provider_request",side_effect=ProviderCircuitOpen("meta",30)):
            _publish_one(slot,"stale-dispatcher",{"external_id":"ig-test","access_token":"test-token"},
                         {"graph_version":"v25.0"},db_conn=self.conn)
        with self.conn.cursor() as cur:
            cur.execute("SELECT state,lease_owner FROM slots WHERE id=%s",(row[0],))
            self.assertEqual(cur.fetchone(),("container_ready",owner))
            cur.execute("SELECT count(*) FROM events WHERE slot_id=%s AND type='publish_blocked_circuit'",(row[0],))
            self.assertEqual(cur.fetchone()[0],0)
        self.conn.commit()

    def test_media_publish_timeout_is_uncertain_and_never_auto_retried(self):
        user,account=self._tenant("UTC")
        dispatcher_id="timeout-chaos-dispatcher"
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO slots(user_id,account_id,local_date,local_time,tz_snapshot,run_at_utc,
                   state,container_id,quota_checked_at,lease_owner,lease_until)
                   VALUES (%s,%s,'2026-11-04','20:00','UTC',now(),'container_ready','container-timeout',
                   now(),%s,now()+interval '60 seconds') RETURNING id,run_at_utc,max_late_minutes,attempts""",
                (user,account,dispatcher_id))
            row=cur.fetchone()
        self.conn.commit()
        slot=(str(row[0]),user,account,None,"container-timeout",row[1],row[2],row[3])
        with patch("scheduler.dispatcher.requests.post",side_effect=requests.Timeout("simulated lost response")) as post:
            _publish_one(slot,dispatcher_id,{"external_id":"ig-test","access_token":"test-token"},
                         {"graph_version":"v25.0"},db_conn=self.conn)
        self.assertEqual(post.call_count,1)
        with self.conn.cursor() as cur:
            cur.execute("SELECT state FROM slots WHERE id=%s",(row[0],))
            self.assertEqual(cur.fetchone()[0],"publish_uncertain")
        self.conn.commit()

    def test_dispatcher_crash_mid_claim_releases_after_lease_expiry(self):
        user,account=self._tenant("UTC")
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO slots(user_id,account_id,local_date,local_time,tz_snapshot,run_at_utc,state)
                   VALUES (%s,%s,'2026-11-04','19:00','UTC',now(),'container_ready') RETURNING id""",
                (user,account))
            slot_id=cur.fetchone()[0]
        self.conn.commit()
        with psycopg.connect(DSN) as first,first.transaction():
            first_claim=claim_slots_near_due(first,"dispatcher-crashed",seconds_ahead=1,limit=1,lease_seconds=1)
        self.assertEqual(first_claim[0][0],slot_id)
        # Simulate process death: its connection is gone, lease expires in DB.
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute("UPDATE slots SET lease_until=now()-interval '1 second' WHERE id=%s",(slot_id,))
        with psycopg.connect(DSN) as second,second.transaction():
            second_claim=claim_slots_near_due(second,"dispatcher-recovery",seconds_ahead=1,limit=1,lease_seconds=30)
        self.assertEqual(second_claim[0][0],slot_id)
        with self.conn.transaction():
            stale=transition_slot(self.conn,slot_id,"container_ready","publishing","dispatcher-crashed")
        self.assertFalse(stale)
        with self.conn.transaction():
            recovered=transition_slot(self.conn,slot_id,"container_ready","publishing","dispatcher-recovery")
        self.assertTrue(recovered)

    def test_expired_job_at_retry_limit_fails_slot_instead_of_reclaiming_forever(self):
        user,account=self._tenant("UTC")
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO slots(user_id,account_id,local_date,local_time,tz_snapshot,run_at_utc,state)
                   VALUES (%s,%s,'2026-11-04','20:00','UTC',now(),'assigned') RETURNING id""",
                (user,account))
            slot_id=cur.fetchone()[0]
            cur.execute(
                """INSERT INTO jobs(user_id,kind,payload,state,lease_owner,lease_until,attempts,max_attempts)
                   VALUES (%s,'prepare_clip',%s::jsonb,'leased','worker-crashed',now()-interval '1 minute',3,3)
                   RETURNING id""",
                (user,json.dumps({"slot_id":str(slot_id)})))
            job_id=cur.fetchone()[0]
        self.conn.commit()

        with self.conn.transaction():
            reclaimed=claim_jobs(self.conn,"recovery-worker",limit=10,lease_seconds=30)
        self.assertEqual(reclaimed,[])
        with self.conn.cursor() as cur:
            cur.execute("SELECT state,lease_owner,lease_until FROM jobs WHERE id=%s",(job_id,))
            job=cur.fetchone()
            cur.execute("SELECT state,last_error FROM slots WHERE id=%s",(slot_id,))
            slot=cur.fetchone()
        self.assertEqual(job[0],"failed")
        self.assertIsNone(job[1]); self.assertIsNone(job[2])
        self.assertEqual(slot[0],"failed")
        self.assertIn("exhausted",slot[1])

    def test_global_provider_bucket_and_circuit_are_database_shared(self):
        os.environ["GEMINI_RATE_CAPACITY"]="3"
        os.environ["GEMINI_REQUESTS_PER_SECOND"]="10"
        with provider_request("gemini"):
            pass
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute("UPDATE provider_circuits SET state='open',opened_until=now()+interval '30 seconds' WHERE provider='gemini'")
        with self.assertRaises(ProviderCircuitOpen):
            with provider_request("gemini"):
                self.fail("open circuit must not yield a provider permit")
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute("UPDATE provider_circuits SET state='closed',failure_count=0,opened_until=NULL WHERE provider='gemini'")
        os.environ.pop("GEMINI_RATE_CAPACITY",None)
        os.environ.pop("GEMINI_REQUESTS_PER_SECOND",None)

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
