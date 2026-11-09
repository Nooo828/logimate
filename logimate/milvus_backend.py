"""P5.2a Milvus REST v2 dense adapter; no SDK dependency and no implicit fallback."""
import hashlib
import json
import math
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


class MilvusUnavailable(RuntimeError):
    """Operational transport/temporary HTTP failure; eligible for explicit P6 degradation."""


class MilvusIntegrityError(RuntimeError):
    """Authentication/configuration/schema/evidence violation; never bypass via fallback."""


class MilvusREST:
    def __init__(self, uri='http://127.0.0.1:19531', token=None):
        parsed = urlsplit(uri)
        if parsed.scheme not in {'http','https'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('invalid trusted Milvus endpoint')
        self.uri, self.token = uri.rstrip('/'), token

    def call(self, endpoint, body, timeout=15):
        headers={'Content-Type':'application/json'}
        if self.token: headers['Authorization']='Bearer '+self.token
        request=Request(self.uri+'/v2/vectordb/'+endpoint, data=json.dumps(body,allow_nan=False).encode(), headers=headers)
        try:
            with urlopen(request,timeout=timeout) as response: result=json.load(response)
        except HTTPError as exc:
            error = MilvusUnavailable if exc.code in {408,429,500,502,503,504} else MilvusIntegrityError
            raise error('Milvus HTTP/network/response failure; no fallback') from None
        except (URLError,OSError):
            raise MilvusUnavailable('Milvus HTTP/network/response failure; no fallback') from None
        except ValueError:
            raise MilvusIntegrityError('Milvus HTTP/network/response failure; no fallback') from None
        if not isinstance(result,dict) or result.get('code') != 0:
            raise MilvusIntegrityError('Milvus rejected operation; remote error text withheld')
        return result.get('data')


def fingerprint(index):
    return hashlib.sha256(json.dumps(index,sort_keys=True,separators=(',',':')).encode()).hexdigest()


class MilvusANN:
    name='milvus-hnsw'

    def __init__(self,index,client=None):
        self.index=index
        self.digest=fingerprint(index)
        self.client=client or MilvusREST()

    def collection(self,title=True):
        return 'lg_p52_'+self.digest[:24]+('_title' if title else '_body')

    def provision(self):
        """Explicit administration, never called from a query or against an arbitrary collection."""
        for title in [True,False]:
            name=self.collection(title)
            present=self.client.call('collections/has',{'collectionName':name})
            if not present['has']:
                self.client.call('collections/create',{'collectionName':name,'params':{'consistencyLevel':'Strong'},
                    'schema':{'autoId':False,'enableDynamicField':True,'fields':[
                        {'fieldName':'id','dataType':'VarChar','isPrimary':True,'elementTypeParams':{'max_length':'512'}},
                        {'fieldName':'vector','dataType':'FloatVector','elementTypeParams':{'dim':'512'}}]},
                    'indexParams':[{'fieldName':'vector','indexName':'dense_hnsw','metricType':'COSINE',
                                    'params':{'index_type':'HNSW','M':16,'efConstruction':128}}]})
            bank=self.index['vectors_title' if title else 'vectors_body']
            rows=[{'id':c['chunk_id'],'vector':bank[c['chunk_id']], 'tenant_id':c['tenant_id'],
                   'domain':c['domain'],'published':c['published'],'live_from':int(c['valid_from'].replace('-','')),
                   'live_to':int((c['valid_to'] or '9999-12-31').replace('-','')),'index_sha':self.digest} for c in self.index['chunks']]
            self.client.call('entities/upsert',{'collectionName':name,'data':rows})
            self.client.call('collections/load',{'collectionName':name})

    def search(self, vector, candidates, tenant_id, as_of, domain=None, title=True):
        if tenant_id not in {'demo-a','demo-b'} or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',as_of):
            raise ValueError('invalid trusted retrieval identity/time')
        if domain is not None and domain not in {'order','warehouse','logistics','aftersales'}:
            raise ValueError('invalid domain')
        if not candidates:return []
        day=int(as_of.replace('-',''))
        expression=f'tenant_id == {json.dumps(tenant_id)} and published == true and live_from <= {day} and live_to > {day} and index_sha == "{self.digest}"'
        if domain:expression+=' and domain == '+json.dumps(domain)
        result=self.client.call('entities/search',{'collectionName':self.collection(title),'annsField':'vector',
            'data':[vector],'limit':10,'filter':expression,'consistencyLevel':'Strong',
            'searchParams':{'metricType':'COSINE','params':{'ef':64}},
            'outputFields':['tenant_id','domain','published','live_from','live_to','index_sha']})
        allowed={c['chunk_id']:c for c in candidates}; ranked=[]
        if not isinstance(result,list) or len(result)!=min(10,len(allowed)):
            raise MilvusIntegrityError('Milvus index incomplete or filter/version mismatch')
        for hit in result:
            if not isinstance(hit, dict): raise MilvusIntegrityError('invalid Milvus hit envelope')
            cid=hit.get('id'); c=allowed.get(cid)
            if c is None or any(hit.get(k)!=v for k,v in {'tenant_id':tenant_id,'domain':c['domain'],
                'published':True,'live_from':int(c['valid_from'].replace('-','')),
                'live_to':int((c['valid_to'] or '9999-12-31').replace('-','')),'index_sha':self.digest}.items()):
                raise MilvusIntegrityError('Milvus returned unauthorized/stale data')
            value=hit.get('distance')
            if isinstance(value,bool) or not isinstance(value,(float,int)) or not math.isfinite(value):
                raise MilvusIntegrityError('invalid Milvus similarity')
            ranked.append((cid,value))
        if len({cid for cid,_ in ranked})!=len(ranked):raise MilvusIntegrityError('duplicate Milvus result')
        return sorted(ranked,key=lambda x:(-x[1],x[0]))
