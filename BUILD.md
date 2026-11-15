# StarDataParadise Python SDK

Release 0.2.0 is a Python 3.12+ DB-API 2.0 client for StarDataParadise Server 0.3.0 over TLS wire v1. It uses Python's standard library at runtime and ships the pinned Server wire contract. The Python package runs independently of the Native DLL.

~~~python
import os
from stardataparadise_sdk import Bind, connect

with connect(user=os.environ["SDP_USER"], password=os.environ["SDP_PASSWORD"],
             ca_file=os.environ["SDP_CA"], tls_name="localhost") as database:
    with database.cursor() as cursor:
        cursor.execute("SELECT note FROM items WHERE id = :key", {"key": 7})
        print(cursor.fetchmany(16))
        cursor.execute("UPDATE items SET note = :note WHERE id = :key",
                       {"key": 7, "note": Bind(None, "TEXT")})
~~~

Named parameters are bound separately from SQL text. A NULL parameter uses Bind(None, SQL type) or a matching setinputsizes declaration. Decimal, date, time, binary, Record and Collection retain their wire values. TimestampValue preserves epoch seconds, nanos, offset and a zone if present. The Server currently binds fixed offsets. A TimeValue represents TIME data beyond Python time's microsecond limit. Cursor description uses the Server's SQL type names. Use callproc or execute CALL to receive the OUT row and advance cursor sets with nextset.

Connections normally use manual commit, with lazy transaction creation. Connection scope commits successful work and rolls back on exception, then closes. Cursor scope releases the result. An autocommit CALL commits its short transaction when its results are exhausted or closed. executemany is atomic in autocommit and uses a savepoint within an explicit transaction. This provider advertises threadsafety=1. Use separate connections for worker threads. Cancellation is a separate request by the same authenticated user, matching the active request ID. Connections support one active result and pull rows in bounded batches. No client-side connection pool is provided.

A mutating acknowledgement may be lost even though the Server durably committed. OutcomeUnknown carries the original request_token. Call outcome(token) on a fresh authenticated connection to resolve it. A ledger status of unknown remains unresolved. Writes are never retried by the provider. Set retry_reads=True to retry one transport-disconnected autocommit query before it returned a result. A broken cursor is never restarted after receiving rows.

Connection methods expose metadata, capabilities, history, Decision validation, model inference, dataset and job state. The Server enforces capability and authorization. The example below requires an existing procedure, table and active model.

~~~python
import os
from stardataparadise_sdk import connect

with connect(user=os.environ["SDP_USER"], password=os.environ["SDP_PASSWORD"],
             ca_file=os.environ["SDP_CA"], tls_name="localhost", autocommit=True) as database:
    print(database.history("items", limit=10))
    print(database.predict("sales_forecast", {"reading": 17}))
    job = database.submit_compute(100)
    print(database.job(job["job_id"]))
    with database.cursor() as cursor:
        cursor.callproc("list_items", (7, None))
        print(cursor.fetchone())  # The OUT row.
        while cursor.nextset():
            print(cursor.set_name, cursor.fetchmany(16))
~~~

## Development and package test

Python 3.12 and setuptools 80.9.0 are pinned for the release build. Run ci.ps1 with -Wheelhouse pointing to pinned setuptools and -TestWheelhouse pointing to the locked build and pytest wheels. It produces wheel and sdist in out/packages and runs separate installed-consumer checks. To exercise real TLS, pass -Server, -OpenSsl and -FixtureHarness together. The harness uses temporary data and generated TLS credentials and can deliberately lose one commit acknowledgement. The package embeds the immutable wire archive with SHA-256 e6500900a3fa34910781ea92d5e41fe8716511fac6f6d6c689ff9cce15d578ab.