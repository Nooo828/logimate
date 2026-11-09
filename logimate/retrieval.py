"""P5.1 in-memory BM25 / real dense vectors / RRF. Milvus is a later adapter."""
from collections import Counter
from functools import lru_cache
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / 'data/knowledge/corpus-v1.json'
INDEX = ROOT / 'data/knowledge/index-v1.json'
MODEL = 'BAAI/bge-small-zh-v1.5'


def cache_dir():
    default = ROOT.parent.parent / 'work/retrieval-cache' if os.name == 'nt' else Path(tempfile.gettempdir()) / 'logimate-retrieval'
    return Path(os.environ.get('LOGIMATE_RETRIEVAL_CACHE', str(default)))


def chunks(corpus, max_chars=220):
    """Heading boundaries + paragraph packing. Markdown tables remain atomic, with header."""
    if max_chars < 80:
        raise ValueError('chunk size too small')
    output = []
    for doc in corpus['documents']:
        for section in doc['sections']:
            units, table = [], []
            for line in section['body'].splitlines():
                if line.startswith('|'):
                    table.append(line)
                else:
                    if table:
                        units.append('\n'.join(table)); table = []
                    if line.strip(): units.append(line.strip())
            if table: units.append('\n'.join(table))
            pieces, current = [], ''
            for unit in units:
                if len(unit) > max_chars:
                    if unit.startswith('|'):
                        raise ValueError('table too large: author must split rows preserving header')
                    smaller = [unit[i:i+max_chars] for i in range(0, len(unit), max_chars)]
                else: smaller = [unit]
                for piece in smaller:
                    if current and len(current) + len(piece) + 1 > max_chars:
                        pieces.append(current); current = ''
                    current = current + '\n' + piece if current else piece
            if current: pieces.append(current)
            for number, body in enumerate(pieces):
                digest = hashlib.sha256(body.encode()).hexdigest()
                output.append({k: v for k, v in doc.items() if k != 'sections'} | {
                    'chunk_id': f"{doc['doc_id']}:{section['section_id']}:{number}:{digest[:12]}",
                    'section_id': section['section_id'], 'body': body, 'body_sha256': digest,
                    'title_path': doc['title'] + ' > ' + section['heading']})
    return output


def eligible(chunk, tenant_id, as_of, domain=None):
    return (chunk['tenant_id'] == tenant_id and chunk['published'] is True
            and chunk['valid_from'] <= as_of and (chunk['valid_to'] is None or as_of < chunk['valid_to'])
            and (domain is None or chunk['domain'] == domain))


@lru_cache(maxsize=1)
def tokenizer():
    import jieba
    cache = Path(os.environ.get('LOGIMATE_LEXICAL_CACHE', str(cache_dir() / 'lexical'))); cache.mkdir(parents=True, exist_ok=True)
    t = jieba.Tokenizer(); t.tmp_dir = str(cache)
    for word in ['未退款', '售后', '退货', '实盘', '签收', '入库', '承运商', '赔付', '拆单']:
        t.add_word(word)
    return t


def tokenize(text):
    # Preserve full identifiers; Chinese segmentation uses a small explicit business dictionary.
    identifiers = re.findall(r'[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*', text)
    chinese = re.sub(r'[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*', ' ', text)
    return [w.lower() for w in identifiers] + [w for w in tokenizer().cut(chinese, HMM=False) if re.search(r'[\u4e00-\u9fff]', w)]


def normalized(values):
    values = [float(v) for v in values]
    if len(values) != 512 or any(not math.isfinite(v) for v in values):
        raise ValueError('invalid embedding')
    norm = math.sqrt(sum(v*v for v in values))
    if norm == 0: raise ValueError('zero embedding')
    return [v/norm for v in values]


def bm25(query, candidates, top_n=10, with_title=True):
    terms = [tokenize((c['title_path'] + '\n' if with_title else '') + c['body']) for c in candidates]
    if not terms: return []
    average = sum(map(len, terms)) / len(terms) or 1
    df = Counter(w for words in terms for w in set(words))
    q = set(tokenize(query)); ranked = []
    for c, words in zip(candidates, terms):
        counts = Counter(words); score = 0
        for w in q:
            tf = counts[w]
            if tf:
                idf = math.log(1 + (len(terms) - df[w] + .5) / (df[w] + .5))
                score += idf * tf * 2.2 / (tf + 1.2 * (.25 + .75 * len(words) / average))
        if score > 0: ranked.append((c['chunk_id'], score))
    return sorted(ranked, key=lambda x: (-x[1], x[0]))[:top_n]


def rrf(rankings, k=60):
    if k <= 0: raise ValueError('RRF k must be positive')
    scores = Counter()
    for ranking in rankings:
        seen = set()
        for rank, (cid, _) in enumerate(ranking, 1):
            if cid in seen: raise ValueError('duplicate chunk in one ranking')
            seen.add(cid); scores[cid] += 1 / (k + rank)
    return sorted(scores.items(), key=lambda x: (-x[1], x[0]))


def load_encoder(*, download=False):
    from fastembed import TextEmbedding
    os.environ.setdefault('HF_HUB_DISABLE_TELEMETRY', '1')
    options = {}
    if not download:
        candidates = list((cache_dir()/'model/models--Qdrant--bge-small-zh-v1.5/snapshots').glob('*/model_optimized.onnx'))
        if len(candidates) != 1: raise RuntimeError('Missing or ambiguous cached model; explicit preparation required')
        options['specific_model_path'] = str(candidates[0].parent)
    return TextEmbedding(model_name=MODEL, cache_dir=str(cache_dir() / 'model'), threads=2,
                         local_files_only=not download, **options)


def load_verified_encoder(index):
    if not index['model_weight_sha256']: raise ValueError('missing model version manifest')
    for relative, digest in index['model_weight_sha256'].items():
        path = (cache_dir()/'model'/relative).resolve()
        if not path.is_relative_to((cache_dir()/'model').resolve()) or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError('cached model version mismatch')
    return load_encoder()


class KnowledgeSearch:
    def __init__(self, index_path=INDEX, *, encoder=None, dense_backend=None):
        self.index = json.loads(Path(index_path).read_text(encoding='utf-8'))
        if self.index['corpus_sha256'] != hashlib.sha256(CORPUS.read_bytes()).hexdigest():
            raise ValueError('stale retrieval index')
        if self.index['chunks'] != chunks(json.loads(CORPUS.read_text(encoding='utf-8'))):
            raise ValueError('chunk manifest mismatch')
        for bank in ['vectors_title','vectors_body']:
            if set(self.index[bank]) != {c['chunk_id'] for c in self.index['chunks']}: raise ValueError('incomplete vector bank')
            for values in self.index[bank].values(): normalized(values)
        self.encoder = encoder
        selected=os.environ.get('LOGIMATE_DENSE_BACKEND','exact')
        if dense_backend is None and selected=='milvus':
            from .milvus_backend import MilvusANN, MilvusREST
            dense_backend=MilvusANN(self.index,MilvusREST(os.environ.get('LOGIMATE_MILVUS_URI','http://127.0.0.1:19531'),os.environ.get('LOGIMATE_MILVUS_TOKEN')))
        elif dense_backend is None and selected!='exact':raise ValueError('unknown trusted dense backend')
        self.dense_backend=dense_backend

    def search(self, query, tenant_id, *, mode='hybrid', top_k=5, domain=None, k=60, title=True):
        if mode not in {'keyword', 'dense', 'hybrid'} or not 1 <= top_k <= 10 or not 1 <= len(query.strip()) <= 500:
            raise ValueError('invalid retrieval request')
        started = time.perf_counter()
        candidates = [c for c in self.index['chunks'] if eligible(c, tenant_id, self.index['as_of'], domain)]
        sparse = bm25(query, candidates, with_title=title) if mode != 'dense' else []
        dense = []
        if mode != 'keyword' and candidates:
            if self.encoder is None:
                self.encoder = load_verified_encoder(self.index)
            vector = normalized(list(self.encoder.query_embed([query]))[0])
            bank = self.index['vectors_title' if title else 'vectors_body']
            if self.dense_backend:
                dense = self.dense_backend.search(vector,candidates,tenant_id,self.index['as_of'],domain,title)
            else:
                dense = sorted([(c['chunk_id'], sum(x*y for x,y in zip(vector, bank[c['chunk_id']]))) for c in candidates], key=lambda x: (-x[1], x[0]))[:10]
        ranked = rrf([sparse, dense], k) if mode == 'hybrid' else sparse if mode == 'keyword' else dense
        by_id = {c['chunk_id']: c for c in candidates}
        hits = [{**by_id[cid], 'score': score, 'rank': rank} for rank, (cid, score) in enumerate(ranked[:top_k], 1)]
        return {'simulated': True, 'corpus_id': self.index['corpus_id'], 'corpus_sha256': self.index['corpus_sha256'],
                'as_of': self.index['as_of'], 'backend': self.dense_backend.name if self.dense_backend and mode!='keyword' else 'in-memory-exact', 'mode': mode, 'rrf_k': k,
                'embedding_model': MODEL if mode != 'keyword' else None, 'hits': hits,
                'elapsed_ms': round((time.perf_counter()-started)*1000, 3),
                'limitations': ['Synthetic rules only; not live order evidence. No answer generation.']}
