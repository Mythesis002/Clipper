"""Postgres-backed global provider token buckets and circuit breakers.

The rate values are deployment-local throttles, not claims about provider quotas.
Provider-specific official limits can change; configure these conservatively and
monitor Graph API/Gemini/YouTube responses.
"""
import os
import time
from contextlib import contextmanager

from db import connect


class ProviderCircuitOpen(RuntimeError):
    def __init__(self, provider, retry_after=30):
        self.provider=provider
        self.retry_after=max(1,int(retry_after))
        super().__init__(f"{provider} circuit is open; retry after about {self.retry_after}s")


def _defaults(provider):
    prefix=provider.upper()
    capacity=float(os.environ.get(f"{prefix}_RATE_CAPACITY",
                                  {"meta":"300","gemini":"10","youtube":"10"}[provider]))
    refill=float(os.environ.get(f"{prefix}_REQUESTS_PER_SECOND",
                                {"meta":"100","gemini":"5","youtube":"1"}[provider]))
    if capacity<1 or refill<=0: raise ValueError(f"invalid {provider} token bucket settings")
    return capacity,refill


def _acquire(provider):
    capacity,refill=_defaults(provider)
    while True:
        wait_seconds=0.0
        with connect() as conn,conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO provider_rate_limits(provider,capacity,tokens,refill_per_second)
                       VALUES (%s,%s,%s,%s) ON CONFLICT(provider) DO NOTHING""",
                    (provider,capacity,capacity,refill))
                cur.execute(
                    """INSERT INTO provider_circuits(provider) VALUES (%s)
                       ON CONFLICT(provider) DO NOTHING""",(provider,))
                cur.execute("SELECT now()")
                now=cur.fetchone()[0]
                cur.execute(
                    """SELECT capacity,tokens,refill_per_second,last_refill_at
                       FROM provider_rate_limits WHERE provider=%s FOR UPDATE""",(provider,))
                cap,tokens,rate,last_refill=cur.fetchone()
                elapsed=max(0.0,(now-last_refill).total_seconds())
                tokens=min(float(cap),float(tokens)+elapsed*float(rate))
                cur.execute(
                    """SELECT state,opened_until,failure_count FROM provider_circuits
                       WHERE provider=%s FOR UPDATE""",(provider,))
                state,opened_until,failures=cur.fetchone()

                if tokens<1.0:
                    wait_seconds=(1.0-tokens)/float(rate)
                    cur.execute(
                        """UPDATE provider_rate_limits SET tokens=%s,last_refill_at=%s,updated_at=now()
                           WHERE provider=%s""",(tokens,now,provider))
                else:
                    if state=="open":
                        if opened_until and opened_until>now:
                            raise ProviderCircuitOpen(provider,(opened_until-now).total_seconds())
                        state="half_open"
                        cur.execute(
                            """UPDATE provider_circuits SET state='half_open',updated_at=now()
                               WHERE provider=%s""",(provider,))
                    elif state=="half_open":
                        raise ProviderCircuitOpen(provider,10)
                    cur.execute(
                        """UPDATE provider_rate_limits SET tokens=%s,last_refill_at=%s,updated_at=now()
                           WHERE provider=%s""",(tokens-1.0,now,provider))
                    return
        # Do not hold DB row locks while waiting for a refill.
        time.sleep(min(max(wait_seconds,0.02),1.0))


def _record(provider,success,error=None):
    with connect() as conn,conn.transaction():
        with conn.cursor() as cur:
            if success:
                cur.execute(
                    """UPDATE provider_circuits SET state='closed',failure_count=0,
                       opened_until=NULL,last_error=NULL,updated_at=now() WHERE provider=%s""",
                    (provider,))
            else:
                cur.execute(
                    """UPDATE provider_circuits SET failure_count=failure_count+1,
                       state=CASE WHEN state='half_open' OR failure_count+1 >= 5 THEN 'open' ELSE state END,
                       opened_until=CASE WHEN state='half_open' OR failure_count+1 >= 5
                         THEN now()+interval '60 seconds' ELSE opened_until END,
                       last_error=%s,updated_at=now() WHERE provider=%s""",
                    (str(error or "provider call failed")[:500],provider))


@contextmanager
def provider_request(provider):
    """Acquire a global rate token and circuit permit around one provider request."""
    if provider not in ("meta","gemini","youtube"):
        raise ValueError(f"unknown provider {provider}")
    _acquire(provider)
    try:
        yield
    except Exception as exc:
        try: _record(provider,False,exc)
        except Exception: pass  # do not hide the original provider failure
        raise
    else:
        _record(provider,True)
