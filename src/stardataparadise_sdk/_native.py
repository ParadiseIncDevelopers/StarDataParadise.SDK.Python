"""Native SQL v1 sessions with server compilation and exact JSON text values."""
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
import json
import math
import re
import base64
import hashlib

from ._dbapi import Connection, _key
from ._errors import DataError, InterfaceError, NotSupportedError, ProgrammingError, OutcomeUnknown


@dataclass(frozen=True)
class NativeJson:
    text: str

    def __post_init__(self):
        if not isinstance(self.text,str): raise DataError('JSON requires serialized text')
        try:
            self.text.encode('utf-8')
            json.loads(self.text,parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError,UnicodeError,RecursionError) as error:
            raise DataError('Invalid JSON text') from error

    @classmethod
    def from_value(cls,value):
        return cls(json.dumps(value,ensure_ascii=False,allow_nan=False,separators=(',',':')))

    def decode(self):
        return json.loads(self.text,parse_float=Decimal)


@dataclass(frozen=True)
class NativeTime:
    """MySQL TIME is a signed interval; it can exceed 24 hours."""
    text: str


@dataclass(frozen=True)
class NativeSet:
    columns: tuple
    rows: tuple
    affected_rows: int
    last_insert_id: int


@dataclass(frozen=True)
class NativeResult:
    sets: tuple
    in_transaction: bool
    autocommit: bool


def _encode(value):
    if value is None: return {'t':'null'}
    if isinstance(value,NativeJson): return {'t':'json','v':value.text}
    if type(value) is bool: return {'t':'bool','v':value}
    if type(value) is int:
        if not -(1<<63)<=value<(1<<64): raise DataError('Native integer is outside BIGINT/UNSIGNED BIGINT')
        return {'t':'i64' if value<(1<<63) else 'u64','v':str(value)}
    if isinstance(value,Decimal):
        if not value.is_finite(): raise DataError('Native decimal must be finite')
        sign,digits,exponent=value.as_tuple()
        if max(len(digits)+max(exponent,0),-exponent,1)>65 or exponent < -30:
            raise DataError('Native decimal is outside MySQL precision/scale')
        return {'t':'decimal','v':format(value,'f')}
    if type(value) is float:
        if not math.isfinite(value): raise DataError('Native float must be finite')
        return {'t':'float','v':repr(value)}
    if isinstance(value,str): return {'t':'text','v':value}
    if isinstance(value,(bytes,bytearray,memoryview)): return {'t':'binary','v':bytes(value).hex()}
    if isinstance(value,(date,datetime)): return {'t':'text','v':value.isoformat()}
    if isinstance(value,NativeTime): return {'t':'text','v':value.text}
    raise DataError('Unsupported native SQL binding')


def _decode(value):
    tag=value['t']
    if tag=='null': return None
    raw=value['v']
    if tag in ('i64','u64'): return int(raw)
    if tag=='decimal': return Decimal(raw)
    if tag=='float': return float(raw)
    if tag=='binary': return bytes.fromhex(raw)
    if tag=='json': return NativeJson(raw)
    if tag=='datetime': return datetime.fromisoformat(raw)
    if tag=='date': return date.fromisoformat(raw)
    if tag=='time': return NativeTime(raw)
    if tag=='text': return raw
    raise DataError('Unsupported native result tag')


class NativeConnection(Connection):
    """Dedicated native session; ordinary DB-API cursors use connect()."""
    def __init__(self,*args,**options):
        super().__init__(*args,retry_reads=False,**options)
        try:
            if self._wire.call('capabilities')['features'].get('native_sql_v1') is not True:
                raise NotSupportedError('Server does not advertise native_sql_v1')
            self._wire.call('native.open',{'autocommit':self._autocommit})
        except BaseException:
            self._wire.close()
            raise

    def execute(self,sql,parameters=(),*,max_rows=1000):
        self._require_open()
        if not isinstance(sql,str) or not sql or '\0' in sql: raise ProgrammingError('SQL must be nonempty text')
        if not isinstance(parameters,(tuple,list)): raise ProgrammingError('Native bindings are positional ? markers')
        if type(max_rows) is not int or not 1<=max_rows<=10000: raise ProgrammingError('max_rows must be between 1 and 10000')
        timeout_ms=min(300000,max(1000,int((self._wire._timeout-2)*1000)))
        body={'sql':sql,'parameters':list(map(_encode,parameters)),'max_rows':max_rows,'timeout_ms':timeout_ms}
        # Native execution can mutate without a generic CRUD receipt token.
        # A transport failure is uncertain and never reconnects/replays SQL.
        result=self._wire.call('native.execute',body,uncertain=True)
        try:
            if 'native_result' in result:
                chunks=bytearray(); identity=result['native_result']['id']; digest=result['native_result']['sha256']
                size=result['native_result']['total_bytes']
                if type(size) is not int or not 0<size<=16*1024*1024: raise ValueError()
                while True:
                    chunk=result['native_result']
                    if (chunk['id'],chunk['offset'],chunk['total_bytes'],chunk['sha256'])!=(identity,len(chunks),size,digest): raise ValueError()
                    data=base64.b64decode(chunk['chunk'],validate=True)
                    if not data or len(chunks)+len(data)>size: raise ValueError()
                    chunks.extend(data)
                    if chunk['end'] is True: break
                    if chunk['end'] is not False: raise ValueError()
                    result=self._wire.call('native.fetch',{'result_id':identity,'offset':len(chunks)},uncertain=True)
                if len(chunks)!=size or hashlib.sha256(chunks).hexdigest()!=digest: raise ValueError()
                result=json.loads(chunks.decode('utf-8'))
            sets=tuple(NativeSet(tuple(part['columns']),tuple(tuple(map(_decode,row)) for row in part['rows']),
                                 int(part['affected_rows']),int(part.get('last_insert_id','0'))) for part in result['sets'])
            if type(result['in_transaction']) is not bool or type(result['autocommit']) is not bool: raise ValueError()
            self._transaction,self._autocommit=result['in_transaction'],result['autocommit']
        except (KeyError,ValueError,TypeError,DataError) as error:
            self._wire.close()
            raise OutcomeUnknown('Malformed native SQL result; query the run/outcome before retrying',
                                 unknown_outcome=True,commit_state='unknown') from error
        return NativeResult(sets,self._transaction,self._autocommit)

    def begin(self): return self.execute('START TRANSACTION')

    def commit(self,*,request_key=None,wait_timeout_ms=None,asynchronous=False):
        if type(asynchronous) is not bool: raise ProgrammingError('asynchronous must be a boolean')
        if asynchronous and wait_timeout_ms is not None: raise ProgrammingError('ASYNC does not accept a client wait deadline')
        pairs,values=[],[]
        if request_key is not None:
            pairs.append('request_key=?'); values.append(_key(request_key))
        if wait_timeout_ms is not None:
            if type(wait_timeout_ms) is not int or not 1<=wait_timeout_ms<=3600000:
                raise ProgrammingError('wait_timeout_ms must be between 1 and 3600000')
            if wait_timeout_ms>=self._wire._timeout*1000-3000:
                raise ProgrammingError('The connection timeout must exceed the SQL wait deadline by at least 3 seconds')
            pairs.append('wait_timeout_ms=?'); values.append(wait_timeout_ms)
        sql='COMMIT ASYNC' if asynchronous else 'COMMIT'
        if pairs: sql+=' WITH ('+', '.join(pairs)+')'
        return self.execute(sql,values)

    def rollback(self): return self.execute('ROLLBACK')
    def fetch_response(self,run_id): return self.execute('FETCH RESPONSE FROM RUN ?',(_key(run_id),))
    def cancel_run(self,run_id): return self.execute('CANCEL RUN ?',(_key(run_id),))

    def cursor(self): raise NotSupportedError('Use native.execute() for this session; use connect() for DB-API cursors')
    def reconnect(self): raise NotSupportedError('Open a new native session and query the run/outcome before retrying')

    def close(self):
        # Closing the dedicated Classic session rolls back its open transaction.
        # Do not issue a second SQL command after a failed/uncertain exchange.
        if not self._closed:
            self._wire.close()
            self._closed=True
            self._transaction=False
            self._connection_args=()

    @property
    def autocommit(self): return self._autocommit

    @autocommit.setter
    def autocommit(self,enabled):
        if type(enabled) is not bool: raise InterfaceError('autocommit must be a boolean')
        if self._transaction: raise ProgrammingError('Finish the transaction before changing autocommit')
        self.execute('SET autocommit='+('1' if enabled else '0'))


def connect_native(host='127.0.0.1',port=7443,*,user,password,ca_file=None,tls_name=None,timeout=75,autocommit=True):
    return NativeConnection(host,port,user,password,ca_file=ca_file,tls_name=tls_name,timeout=timeout,autocommit=autocommit)
