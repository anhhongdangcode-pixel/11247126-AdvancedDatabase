import random
import sys
from datetime import datetime, timedelta
from cassandra import ConsistencyLevel, Unavailable
from cassandra.cluster import Cluster, ExecutionProfile, EXEC_PROFILE_DEFAULT
from cassandra.policies import FallthroughRetryPolicy, WhiteListRoundRobinPolicy
from cassandra.query import SimpleStatement

# Always use cass1 (localhost:9042) as the coordinator, and show errors without retrying.
profile = ExecutionProfile(
    load_balancing_policy=WhiteListRoundRobinPolicy(["127.0.0.1"]),
    retry_policy=FallthroughRetryPolicy(),
)
cluster = Cluster(["127.0.0.1"], execution_profiles={EXEC_PROFILE_DEFAULT: profile})
session = cluster.connect()


def setup():
    session.execute("""
        CREATE KEYSPACE IF NOT EXISTS iot
        WITH replication = {'class': 'SimpleStrategy', 'replication_factor': 3}""")
    session.execute("""
        CREATE TABLE IF NOT EXISTS iot.readings_by_sensor (
            sensor_id   text,
            ts          timestamp,
            temperature double,
            humidity    double,
            PRIMARY KEY ((sensor_id), ts)
        ) WITH CLUSTERING ORDER BY (ts DESC)""")


def load(sensors=30, per_sensor=100):
    insert = session.prepare(
        "INSERT INTO iot.readings_by_sensor (sensor_id, ts, temperature, humidity) "
        "VALUES (?, ?, ?, ?)"
    )
    start = datetime(2026, 10, 22, 0, 0)
    for s in range(1, sensors + 1):
        for i in range(per_sensor):
            ts = start + timedelta(minutes=5 * i)
            session.execute(
                insert,
                (
                    f"hn-{s:02d}",
                    ts,
                    round(random.uniform(22, 32), 1),
                    round(random.uniform(55, 90), 1),
                ),
            )
    print(f"loaded {sensors * per_sensor} rows for {sensors} sensors")


def try_levels():
    """Write then read one row at each consistency level and report what happens."""
    write = (
        "INSERT INTO iot.readings_by_sensor (sensor_id, ts, temperature, humidity) "
        "VALUES ('cl-test', toTimestamp(now()), 25.0, 60.0)"
    )
    read = "SELECT count(*) FROM iot.readings_by_sensor WHERE sensor_id = 'cl-test'"
    for name in ("ONE", "QUORUM", "ALL"):
        cl = getattr(ConsistencyLevel, name)
        try:
            session.execute(SimpleStatement(write, consistency_level=cl))
            n = session.execute(SimpleStatement(read, consistency_level=cl)).one()[0]
            print(f"{name:<7} OK    (rows for cl-test: {n})")
        except Unavailable as e:
            print(
                f"{name:<7} FAIL  Unavailable: need {e.required_replicas} replicas, "
                f"{e.alive_replicas} alive"
            )
        except Exception as e:
            print(f"{name:<7} FAIL  {type(e).__name__}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "load"
    if cmd == "load":
        setup()
        load()
    elif cmd == "cl":
        try_levels()
    cluster.shutdown()
