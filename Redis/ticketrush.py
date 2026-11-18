import random
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import redis

# Connect to Redis
r = redis.Redis(host="localhost", port=6379, decode_responses=True)


def race(fn, n=50):
    """Run fn(i) from n threads at once and return the list of results."""
    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(fn, range(n)))


# ==============================================================================
# Step 1: Seat holds and the race condition
# ==============================================================================

def hold_naive(event, seat, user):
    """Naive check-then-set hold (vulnerable to race conditions)."""
    key = f"hold:{event}:{seat}"
    if r.get(key) is None:
        time.sleep(0.01)  # widen the window so the bug is easy to see
        r.set(key, user, ex=300)
        return True
    return False


def hold_nx(event, seat, user, ttl=300):
    """Atomic hold using SET ... NX EX."""
    return bool(r.set(f"hold:{event}:{seat}", user, nx=True, ex=ttl))


# ==============================================================================
# Step 2: Hold, confirm and release atomically (Lua scripts)
# ==============================================================================

HOLD = r.register_script("""
if redis.call('SISMEMBER', KEYS[2], ARGV[2]) == 1 then return 0 end
if redis.call('SET', KEYS[1], ARGV[1], 'NX', 'EX', ARGV[3]) then return 1 end
return 0
""")

CONFIRM = r.register_script("""
if redis.call('GET', KEYS[1]) == ARGV[1] then
  redis.call('SADD', KEYS[2], ARGV[2])
  redis.call('DEL', KEYS[1])
  return 1
end
return 0
""")

RELEASE = r.register_script("""
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
""")


def hold(event, seat, user, ttl=300):
    return bool(HOLD(keys=[f"hold:{event}:{seat}", f"sold:{event}"], args=[user, seat, ttl]))


def confirm(event, seat, user):
    return bool(CONFIRM(keys=[f"hold:{event}:{seat}", f"sold:{event}"], args=[user, seat]))


def release(event, seat, user):
    return bool(RELEASE(keys=[f"hold:{event}:{seat}"], args=[user]))


def seat_map(event, seats):
    sold = r.smembers(f"sold:{event}")
    return {
        s: "sold" if s in sold else "held" if r.exists(f"hold:{event}:{s}") else "free"
        for s in seats
    }


# ==============================================================================
# Step 3: Flash sale on general admission
# ==============================================================================

def buy_general(event, user):
    left = r.decr(f"stock:{event}")
    if left < 0:
        r.incr(f"stock:{event}")  # undo: nothing was left
        return False
    r.sadd(f"buyers:{event}", user)
    return True


# ==============================================================================
# Step 4: Rate limiting (Fixed window & Sliding window)
# ==============================================================================

def allow_fixed(user, limit=5, window=10):
    key = f"rl:fixed:{user}:{int(time.time() // window)}"
    pipe = r.pipeline()
    pipe.incr(key)
    pipe.expire(key, window, nx=True)  # set the TTL only the first time
    count, _ = pipe.execute()
    return count <= limit


SLIDING = r.register_script("""
local now, window, limit = tonumber(ARGV[1]), tonumber(ARGV[2]), tonumber(ARGV[3])
redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, now - window)
if redis.call('ZCARD', KEYS[1]) < limit then
  redis.call('ZADD', KEYS[1], now, ARGV[4])
  redis.call('PEXPIRE', KEYS[1], window)
  return 1
end
return 0
""")


def allow_sliding(user, limit=5, window_ms=10_000):
    now = int(time.time() * 1000)
    return bool(SLIDING(keys=[f"rl:sliding:{user}"], args=[now, window_ms, limit, uuid.uuid4().hex]))


def try_hold(event, seat, user):
    if not allow_sliding(user):
        return "rate_limited"
    return "ok" if hold(event, seat, user) else "taken"


# ==============================================================================
# Step 5: Virtual waiting room (Sorted Set Queue)
# ==============================================================================

def join_queue(event, user):
    r.zadd(f"queue:{event}", {user: time.time()}, nx=True)  # NX: re-joining keeps your place
    return r.zrank(f"queue:{event}", user) + 1


def admit(event, n):
    return [user for user, _ in r.zpopmin(f"queue:{event}", n)]


# ==============================================================================
# Extensions (Bonus features)
# ==============================================================================

EXTEND_HOLD = r.register_script("""
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return 0
""")


def extend_hold(event, seat, user, extra_ttl=300):
    """Extend the hold TTL only if user still owns it."""
    return bool(EXTEND_HOLD(keys=[f"hold:{event}:{seat}"], args=[user, extra_ttl]))


def hold_countdown(event, seat):
    """Get the remaining TTL in seconds for a held seat."""
    return r.ttl(f"hold:{event}:{seat}")


# ==============================================================================
# Step 6: Final stress test & Step-by-step verification runner
# ==============================================================================

def run_step1():
    print("--- Step 1: Seat holds and race conditions ---")
    r.flushdb()
    naive_wins = race(lambda i: hold_naive("c1", "A1", f"user{i}"))
    print(f"hold_naive winners (50 clicks): {sum(naive_wins)} (expect > 1 due to race condition)")

    r.flushdb()
    nx_wins = race(lambda i: hold_nx("c1", "A1", f"user{i}"))
    print(f"hold_nx winners (50 clicks): {sum(nx_wins)} (always exactly 1)")


def run_step2():
    print("\n--- Step 2: Hold, confirm and release atomically ---")
    r.flushdb()
    print("hold c1 A1 linh:     ", hold("c1", "A1", "linh"))      # True
    print("hold c1 A1 nam:      ", hold("c1", "A1", "nam"))       # False, linh holds it
    print("confirm c1 A1 nam:   ", confirm("c1", "A1", "nam"))    # False, nam does not own the hold
    print("confirm c1 A1 linh:  ", confirm("c1", "A1", "linh"))   # True, now sold
    print("hold c1 A1 nam:      ", hold("c1", "A1", "nam"))       # False, already sold
    print("seat_map c1 [A1, A2]:", seat_map("c1", ["A1", "A2"]))  # {'A1': 'sold', 'A2': 'free'}


def run_step3():
    print("\n--- Step 3: Flash sale on general admission ---")
    r.flushdb()
    r.set("stock:g1", 100)
    ok = race(lambda i: buy_general("g1", f"user{i}"), n=500)
    print(f"Tickets sold: {sum(ok)}, Remaining stock: {r.get('stock:g1')}, Unique buyers: {r.scard('buyers:g1')}")
    # expect 100 0 100


def run_step4():
    print("\n--- Step 4: Rate limiting ---")
    r.flushdb()
    fixed_res = [allow_fixed("linh") for _ in range(8)]
    print("allow_fixed linh 8 times:", fixed_res)

    print("allow_sliding bot 5 times:", [allow_sliding("bot") for _ in range(5)])
    print("allow_sliding bot 6th time:", allow_sliding("bot"))  # Should be False (limit=5)
    print("try_hold result:", try_hold("c1", "B1", "bot"))


def run_step5():
    print("\n--- Step 5: Virtual waiting room ---")
    r.flushdb()
    for name in ["u1", "u2", "u3"]:
        print(f"join_queue {name}: rank {join_queue('c1', name)}")
    print("re-join u1: rank", join_queue("c1", "u1"))
    print("admit 2 users:", admit("c1", 2))
    print("rank of u3 after admission:", join_queue("c1", "u3"))


def run_step6():
    print("\n--- Step 6: Final stress test (100 users rush 20 seats) ---")
    SEATS = [f"S{i}" for i in range(1, 21)]

    def buyer(i):
        user = f"user{i}"
        for seat in SEATS:
            if hold("c1", seat, user):
                time.sleep(random.random() * 0.05)  # paying simulation
                if confirm("c1", seat, user):
                    return seat
        return None

    all_passed = True
    for iteration in range(1, 11):
        r.flushdb()
        got = [s for s in race(buyer, n=100) if s]
        sold_count = r.scard("sold:c1")
        distinct_seats = len(set(got))
        total_got = len(got)
        is_ok = (total_got == 20 and distinct_seats == 20 and sold_count == 20)
        if not is_ok:
            all_passed = False
        print(f"Run {iteration:2d}/10: sold={total_got}, unique={distinct_seats}, redis_set={sold_count} -> {'PASS' if is_ok else 'FAIL'}")

    print("Stress test result:", "ALL 10 RUNS PASSED (20 20 20)!" if all_passed else "SOME RUNS FAILED!")


if __name__ == "__main__":
    run_step1()
    run_step2()
    run_step3()
    run_step4()
    run_step5()
    run_step6()
