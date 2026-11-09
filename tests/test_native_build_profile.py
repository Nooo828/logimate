"""Regression: copied offline wheels must be at the paths hashed by child provenance."""
from pathlib import Path, PurePosixPath
from scripts.verify_native_review_docker import build_files,parent_plan


def test_every_expected_child_hash_path_is_copied_to_app_layout(tmp_path):
    root=Path(__file__).resolve().parents[1]
    recipe,_=build_files(root,tmp_path,parent_plan(root)['tag'],'logimate-layout-test',18086)
    lines=recipe.read_text(encoding='utf-8').splitlines(); targets=[]
    for line in lines:
        if not line.startswith('COPY '):continue
        _,source,dest=line.split()
        destination=PurePosixPath(dest)
        if not destination.is_absolute():destination=PurePosixPath('/app')/destination
        if (root/source).is_file():destination=destination/PurePosixPath(source).name
        targets.append((root/source,destination))
    for pattern in ('logimate/*.py','data/**/*.json','prompts/*.txt','requirements-retrieval.lock','requirements-checkpoint.lock','vendor/p72-wheels/*'):
        for file in root.glob(pattern):
            expected=PurePosixPath('/app')/file.relative_to(root).as_posix()
            actual=[destination/(file.relative_to(source).as_posix() if source.is_dir() else '')
                for source,destination in targets if file==source or source in file.parents]
            assert expected in actual,(file,expected,actual)
