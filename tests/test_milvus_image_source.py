"""Configuration contracts only: no real image pull or database calls."""
import json
import pytest
pytest.importorskip('jieba')
from scripts.verify_milvus import docker_commands
from logimate.retrieval import ROOT


def test_default_source_and_context_are_preserved(tmp_path):
    docker,prefix,images=docker_commands('test-project',tmp_path)
    assert docker==['docker']
    assert images['minio']=='minio/minio:RELEASE.2024-05-28T17-19-04Z'
    assert images['milvus']=='milvusdb/milvus:v2.6.1'
    assert not list(tmp_path.iterdir())
    assert prefix==['docker','compose','-p','test-project','-f','compose.milvus.json']


def test_direct_source_pins_versions_and_only_changes_hub_images(tmp_path):
    docker,prefix,images=docker_commands('isolated-project',tmp_path,True,True)
    assert docker==['docker','--host','npipe:////./pipe/dockerDesktopLinuxEngine']
    assert prefix[:len(docker)]==docker
    override=tmp_path/'official-registry.json'
    config=json.loads(override.read_text(encoding='utf-8'))
    assert config=={'services':{
        'minio':{'image':'registry-1.docker.io/minio/minio:RELEASE.2024-05-28T17-19-04Z'},
        'milvus':{'image':'registry-1.docker.io/milvusdb/milvus:v2.6.1'}}}
    assert images['etcd']=='quay.io/coreos/etcd:v3.5.18'
    assert prefix[-2:]==['-f',str(override)]


def test_embedded_profile_does_not_pull_or_start_minio(tmp_path):
    _,prefix,images=docker_commands('isolated-embed',tmp_path,True,True,True)
    assert images=={'milvus':'registry-1.docker.io/milvusdb/milvus:v2.6.1'}
    assert 'compose.milvus.embedded.json' in prefix
    override=json.loads((tmp_path/'official-registry.json').read_text(encoding='utf-8'))
    assert set(override['services'])=={'milvus'}
    config=json.loads((ROOT/'compose.milvus.embedded.json').read_text(encoding='utf-8'))
    assert set(config['services'])=={'milvus'}
    service=config['services']['milvus']
    assert service['environment']['ETCD_USE_EMBED']=='true'
    assert service['environment']['COMMON_STORAGETYPE']=='local'
    assert service['volumes']==['milvus-embedded:/var/lib/milvus']
    assert all(p.startswith('127.0.0.1:') for p in service['ports'])
    assert all(':2379' not in p for p in service['ports'])
    assert 'container_name' not in service
    assert 'depends_on' not in service
    for spec in config['configs'].values():
        assert (ROOT/spec['file']).is_file()
