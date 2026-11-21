"""Check the separately installed wheel with the pinned fixture and real Server."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import time
from zipfile import ZipFile

import pytest
import stardataparadise_sdk as db


def env(name):
    return os.environ["SDP_SDK_" + name]


@pytest.fixture(scope="session")
def login():
    if "SDP_SDK_PORT" not in os.environ:
        pytest.skip("Real Server fixture required")
    administrator = sys.stdin.readline().strip()
    guest = sys.stdin.readline().strip()
    if not administrator or not guest:
        pytest.fail("The TLS fixture did not provide two credentials")
    return (env("USER"), administrator), (env("GUEST"), guest)


def connect(login, *, port=None, autocommit=True, retry_reads=False, tls_name=None):
    return db.connect(env("HOST"), int(env("PORT")) if port is None else port, user=login[0], password=login[1],
                      tls_name=env("TLS_NAME") if tls_name is None else tls_name, ca_file=env("CA"),
                      timeout=15, autocommit=autocommit, retry_reads=retry_reads)


def query(connection, sql, bound=None):
    with connection.cursor() as cursor:
        return cursor.execute(sql, bound).fetchall()


def test_installed_and_golden():
    assert db.__version__ == "0.3.0" and db.apilevel == "2.0"
    assert db.paramstyle == "named" and db.threadsafety == 1
    assert "site-packages" in Path(db.__file__).parts
    archive = db.wire_contract()
    assert hashlib.sha256(archive).hexdigest() == db.WIRE_CONTRACT_SHA256
    with ZipFile(io.BytesIO(archive)) as pinned:
        raw = pinned.read("golden/values.json")
    if os.environ.get("SDP_SDK_GOLDEN"):
        assert Path(env("GOLDEN")).read_bytes() == raw
    with pytest.MonkeyPatch.context() as patch:
        patch.delenv("PYTHONPATH", raising=False)
        corpus = json.loads(raw)["body"]["values"]
    values = list(map(db.decode_value, corpus))
    assert values[0].as_tuple() == Decimal("-12345678901234567890123456789012.3400").as_tuple()
    assert values[1] is None and values[2] == "" and "İstanbul" in values[3]
    assert values[4] == bytes.fromhex("00017f80feff")
    assert values[5:8] == [-(2 ** 63), 2 ** 63 - 1, "a\0b"]
    assert values[8] == db.TimestampValue(-1, 999999999, 180, "Europe/Istanbul")
    assert values[9] == db.Record((("null", None), ("empty", "")))
    assert db.TimeValue(8, 7, 6, 123456789).nanos == 123456789
    with pytest.raises(db.DataError):
        values[8].to_datetime()
    assert db.TimestampFromTicks(0) == datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)


def test_real_crud_datetime_none_and_injection(login):
    owner, _ = login
    with connect(owner) as connection:
        assert connection.server_version == "0.3.0"
        assert {"sql_crud_v1", "plsql_results_v1", "history_v1", "admin_v1"} <= {
            name for name, active in connection.capabilities().items() if active
        }
        with connection.cursor() as cursor:
            values = cursor.execute("SELECT note,amount,stamp,payload FROM sdk_seed ORDER BY id").fetchall()
            assert cursor.description[0][:2] == ("note", "TEXT")
            assert cursor.description[0][1] == db.STRING and cursor.description[1][1] == db.NUMBER
            assert values[0] == ("İstanbul'un notu; a\0b", Decimal("-12345678901234567890123456789012.3400"),
                                 db.TimestampValue(-1, 999999999, 180), bytes.fromhex("00017f80feff"))
            assert values[1] == (None,) * 4 and values[0][1].as_tuple().exponent == -4
            cursor.execute("CREATE TABLE python_crud (id BIGINT PRIMARY KEY,note TEXT,reading BIGINT)")
            text = "x'); DELETE FROM sdk_seed; --"
            cursor.setinputsizes({"reading": "BIGINT"})
            cursor.execute("INSERT INTO python_crud VALUES (:key,:data,:reading)",
                           {"key": 1, "data": text, "reading": None})
            assert cursor.rowcount == 1
            assert cursor.execute("SELECT note,reading FROM python_crud WHERE id=:key",
                                  {":KEY": 1}).fetchone() == (text, None)
            cursor.execute("UPDATE python_crud SET note=:value WHERE id=:key", {"value": "changed", "key": 1})
            assert cursor.rowcount == 1
            assert cursor.execute("SELECT id FROM sdk_seed ORDER BY id").fetchall() == [(1,), (2,)]
            cursor.execute("DELETE FROM python_crud WHERE id=:key", {"key": 1})
            assert cursor.rowcount == 1 and re.fullmatch(r"[0-9a-f]{32}", cursor.last_request_token)
            assert cursor.execute("SELECT id FROM python_crud").fetchall() == []
            with pytest.raises(db.ProgrammingError):
                cursor.fetchone() if cursor.execute("DELETE FROM python_crud WHERE id=1") else None


def test_routine_sets_and_compound(login):
    owner, _ = login
    with connect(owner) as connection, connection.cursor() as cursor:
        cursor.callproc("sdk_list", (1, None))
        assert cursor.fetchone() == (1,) and cursor.set_name == "out"
        assert cursor.nextset() is True and cursor.set_name == "found" and cursor.fetchall() == [(1,)]
        assert cursor.nextset() is True and cursor.set_name == "all_rows" and cursor.fetchall() == [(1,), (2,)]
        assert cursor.nextset() is None
        with pytest.raises(db.ProgrammingError):
            cursor.callproc("sdk_list); DELETE FROM sdk_seed; --", ())
        record = db.Record((("whole", 17), ("missing", None), ("date", datetime.date(2030, 1, 2))))
        collection = db.Collection((True, record, db.TimeValue(7, 8, 9, 123456789)))
        cursor.execute("CALL sdk_echo(:record_arg,:list_arg)", {"record_arg": record, "list_arg": collection})
        assert cursor.fetchone() == (record, collection)
        assert cursor.nextset() is None


def test_rollback_and_executemany(login):
    owner, _ = login
    with connect(owner) as auto:
        with auto.cursor() as cursor:
            cursor.execute("CREATE TABLE python_tx (id BIGINT PRIMARY KEY)")
            assert cursor.executemany("INSERT INTO python_tx VALUES (:id)", ({"id": i} for i in range(1, 4))).rowcount == 3
            with pytest.raises(db.DatabaseError):
                cursor.executemany("INSERT INTO python_tx VALUES (:id)", [{"id": 8}, {"id": 3}])
        assert query(auto, "SELECT id FROM python_tx ORDER BY id") == [(1,), (2,), (3,)]
    connection = connect(owner, autocommit=False)
    try:
        with connection.cursor() as cursor:
            cursor.execute("INSERT INTO python_tx VALUES (4)")
            with pytest.raises(db.DatabaseError):
                cursor.executemany("INSERT INTO python_tx VALUES (:id)", [{"id": 5}, {"id": 3}])
            assert cursor.execute("SELECT id FROM python_tx ORDER BY id").fetchall() == [(1,), (2,), (3,), (4,)]
        connection.rollback()
        assert query(connection, "SELECT id FROM python_tx ORDER BY id") == [(1,), (2,), (3,)]
    finally:
        connection.close()


def test_stream_and_extensions(login):
    owner, _ = login
    with connect(owner) as connection:
        with connection.cursor() as cursor:
            cursor.arraysize = 17
            cursor.execute("SELECT id FROM sdk_bulk ORDER BY id")
            rows = []
            while batch := cursor.fetchmany(23):
                assert len(batch) <= 23
                rows.extend(batch)
            assert rows == [(index,) for index in range(1, int(env("BULK_ROWS")) + 1)]
            cursor2 = connection.cursor()
            try:
                assert cursor2.execute("SELECT 1").fetchone() == (1,)
            finally:
                cursor2.close()
        assert connection.history("sdk_seed")["entries"]
        assert connection.dataset(env("DATASET_ID"))["row_count"] == int(env("BULK_ROWS"))
        assert connection.job(env("JOB_ID"))["state"] == "succeeded"
        assert connection.predict(env("MODEL"), {"reading": 17})["prediction"] == pytest.approx(58, abs=1e-4)
        assert connection.validate_decision(
            "CREATE DECISION RULE observer ON sdk_seed FOR UPDATE PRIORITY 1 WHEN (NEW.id > 0) THEN DENY")["valid"]
        assert connection.administration_health()


def test_tls_access_and_secrets(login):
    owner, guest = login
    with pytest.raises(db.OperationalError) as mismatch:
        connect(owner, tls_name="wrong-fixture.invalid")
    assert mismatch.value.code == 1011 and owner[1] not in str(mismatch.value)
    with pytest.raises(db.OperationalError) as wrong:
        connect((owner[0], guest[1]))
    assert wrong.value.code == 1005 and guest[1] not in str(wrong.value)
    with connect(guest) as limited:
        assert guest[1] not in repr(limited)
        assert query(limited, "SELECT id FROM sdk_seed WHERE id=1") == [(1,)]
        for action in (lambda: limited.history("sdk_seed"), lambda: limited.predict(env("MODEL"), {"reading": 17})):
            with pytest.raises(db.OperationalError) as refused:
                action()
            assert refused.value.code == 1006 and refused.value.sqlstate == "42501"
            assert owner[1] not in str(refused.value) and guest[1] not in str(refused.value)


def test_cancel_keeps_the_server_connection(login):
    owner, _ = login
    with connect(owner, autocommit=False) as holding, connect(owner) as victim:
        with holding.cursor() as write:
            write.execute("SELECT id FROM sdk_seed").fetchall()
        def blocked():
            with victim.cursor() as waiting:
                waiting.execute("SELECT id FROM sdk_seed").fetchall()
        with ThreadPoolExecutor(max_workers=1) as worker:
            pending = worker.submit(blocked)
            for _ in range(40):
                if victim.cancel():
                    break
                time.sleep(0.05)
            else:
                pytest.fail("The Server never acknowledged cancellation")
            with pytest.raises(db.OperationalError) as stopped:
                pending.result(timeout=7)
            assert stopped.value.code == 1009 and not stopped.value.unknown_outcome
        holding.rollback()
        assert query(victim, "SELECT 7") == [(7,)]


def test_unknown_commit_is_resolved_across_sessions(login):
    owner, _ = login
    with connect(owner) as setup:
        with setup.cursor() as cursor:
            cursor.execute("CREATE TABLE python_commit (id BIGINT PRIMARY KEY)")
    faulted = connect(owner, port=int(env("FAULT_PORT")), autocommit=False)
    try:
        with faulted.cursor() as cursor:
            cursor.execute("INSERT INTO python_commit VALUES (1)")
        with pytest.raises(db.OutcomeUnknown) as lost:
            faulted.commit()
        receipt = lost.value.request_token
        assert lost.value.unknown_outcome and lost.value.code == 1010
        assert receipt == faulted.last_request_token and re.fullmatch(r"[0-9a-f]{32}", receipt)
    finally:
        faulted.close()
    with connect(owner) as recovered:
        assert recovered.outcome(receipt)["status"] == "committed"
        assert query(recovered, "SELECT id FROM python_commit") == [(1,)]
        assert recovered.outcome("0" * 32)["status"] == "unknown"

def test_explicit_autocommit_read_retry(login):
    owner, _ = login
    port = int(env("READ_FAULT_PORT"))
    with connect(owner, port=port, retry_reads=True) as connection:
        original_session = connection.session_id
        assert query(connection, "SELECT id FROM sdk_seed WHERE id=1") == [(1,)]
        assert original_session != connection.session_id
