"""Native SQL API boundary tests; native TLS acceptance is separate."""
from decimal import Decimal
import unittest
import base64
import hashlib
import json

from stardataparadise_sdk import NativeJson, NativeConnection, ProgrammingError, OutcomeUnknown
from stardataparadise_sdk._errors import server_error
from stardataparadise_sdk._native import _encode
from stardataparadise_sdk._wire import Wire


class StubWire:
    def __init__(self): self.calls=[]; self._timeout=75; self.open=True
    def call(self,op,body=None,token=None,*,uncertain=False):
        self.calls.append((op,body,uncertain))
        return {'sets':[{'columns':[{'name':'document','type':'JSON'}],
                         'rows':[[{'t':'json','v':'{"id":9007199254740993,"price":1.25}'}]],'affected_rows':'0'}],
                'in_transaction':False,'autocommit':True}
    def close(self): self.open=False


def connection():
    value=object.__new__(NativeConnection)
    value._wire=StubWire(); value._closed=False; value._transaction=False; value._autocommit=True
    value._cursor=None; value._connection_args=(); value.last_request_token=None
    return value


class NativeTests(unittest.TestCase):
    def test_json_result_preserves_all_digits_and_decimal(self):
        db=connection()
        result=db.execute('SELECT document FROM sample')
        value=result.sets[0].rows[0][0]
        self.assertIsInstance(value,NativeJson)
        self.assertEqual(9007199254740993,value.decode()['id'])
        self.assertEqual(Decimal('1.25'),value.decode()['price'])

    def test_bound_source_stays_separate_and_execution_is_never_read_retried(self):
        db=connection()
        text="'); DELETE FROM sample; --"
        db.execute('APPEND PROMPT ?',(text,))
        op,body,uncertain=db._wire.calls[0]
        self.assertEqual('native.execute',op)
        self.assertEqual('APPEND PROMPT ?',body['sql'])
        self.assertEqual([{'t':'text','v':text}],body['parameters'])
        self.assertTrue(uncertain)

    def test_commit_options_are_bound_and_validated_before_send(self):
        db=connection()
        with self.assertRaises(ProgrammingError): db.commit(asynchronous=True,wait_timeout_ms=1)
        self.assertEqual([],db._wire.calls)
        db.commit(asynchronous=True,request_key='a'*32)
        body=db._wire.calls[0][1]
        self.assertEqual('COMMIT ASYNC WITH (request_key=?)',body['sql'])
        self.assertEqual([{'t':'text','v':'a'*32}],body['parameters'])

    def test_wait_error_retains_durable_commit_information(self):
        error=server_error({'code':511003,'native_errno':61093,'sqlstate':'HYT00','message':'Wait expired',
                            'unknown_outcome':False,'phase':'wait','commit_state':'committed',
                            'run_state':'running','run_id':'b'*32,'retry':{'action':'QUERY_RUN'}},None)
        self.assertEqual(511003,error.code)
        self.assertEqual('committed',error.commit_state)
        self.assertEqual('b'*32,error.run_id)
        self.assertFalse(error.unknown_outcome)

    def test_tool_rollback_retains_cause_separate_from_wrapper_errno(self):
        error=server_error({'code':510005,'native_errno':61085,'sqlstate':'45000',
                            'message':'Tool transaction rolled back.','unknown_outcome':False,
                            'phase':'tool','commit_state':'committed','run_id':'a'*32,
                            'call_id':'b'*32,'action_commit_state':'rolled_back',
                            'cause_native_errno':3024,'cause_sqlstate':'HY000'},None)
        self.assertEqual(61085,error.native_errno)
        self.assertEqual(3024,error.cause_native_errno)
        self.assertEqual('HY000',error.cause_sqlstate)
        self.assertEqual('b'*32,error.call_id)
        self.assertEqual('committed',error.commit_state)
        self.assertEqual('rolled_back',error.action_commit_state)
        self.assertFalse(error.unknown_outcome)

    def test_native_transport_timeout_is_unknown_without_a_crud_token(self):
        class Socket:
            sends=0
            def settimeout(self,value): pass
            def sendall(self,data): self.sends+=1; raise TimeoutError()
            def close(self): pass
        socket=Socket()
        wire=object.__new__(Wire)
        wire._socket=socket; wire._last_id=0; wire._timeout=1; wire.pending=0
        with self.assertRaises(OutcomeUnknown) as caught:
            wire.call('native.execute',{'sql':'COMMIT'},uncertain=True)
        self.assertTrue(caught.exception.unknown_outcome)
        self.assertIsNone(caught.exception.request_token)
        self.assertEqual(1,socket.sends)
        self.assertFalse(wire.open)

    def test_native_values_keep_unsigned_and_extended_decimal(self):
        self.assertEqual({'t':'u64','v':str(2**64-1)},_encode(2**64-1))
        value=Decimal('1234567890123456789012345678901234567890.1234567890')
        self.assertEqual(format(value,'f'),_encode(value)['v'])

    def test_large_json_chunks_are_verified_and_decoded_once(self):
        db=connection()
        value='İğ'*200000
        doc=json.dumps({'text':value},ensure_ascii=False)
        raw=json.dumps({'sets':[{'columns':[{'name':'document','type':'JSON'}],
                        'rows':[[{'t':'json','v':doc}]],'affected_rows':'0'}],
                        'in_transaction':False,'autocommit':True},ensure_ascii=False).encode()
        digest=hashlib.sha256(raw).hexdigest()
        calls=[]
        def exchange(op,body=None,token=None,*,uncertain=False):
            calls.append(op)
            offset=0 if op=='native.execute' else body['offset']
            piece=raw[offset:offset+256*1024]
            return {'native_result':{'id':'a'*32,'offset':offset,'total_bytes':len(raw),'sha256':digest,
                     'chunk':base64.b64encode(piece).decode(),'end':offset+len(piece)==len(raw)}}
        db._wire.call=exchange
        self.assertEqual(value,db.execute('HIST SHOW LAST').sets[0].rows[0][0].decode()['text'])
        self.assertEqual(1,calls.count('native.execute'))
        self.assertGreater(calls.count('native.fetch'),0)


if __name__=='__main__': unittest.main()
