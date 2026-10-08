#!/usr/bin/env python3
"""Discover the newest MLTD assets, prepare a snapshot, and run localization CI.

This is the version dispatcher around the existing asset and localization
tools.  It does not invent an archive implementation: ``tools/asset_version.py``
acquires/materializes the version, ``cache_localization_gtx.py`` creates the
JP GTX snapshot, and ``run_localization_ci.py`` performs translation and
publishing.  The asset store must have one coordinated writer.
"""
from __future__ import annotations

import argparse
import json
import hashlib
import copy
import re
import shlex
import tarfile
import subprocess
import sys
from pathlib import Path
from typing import Any

import msgpack

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.cache_localization_gtx import load_rows  # noqa: E402


class LatestLocalizationError(RuntimeError):
    """Raised when latest-version dispatch cannot proceed safely."""


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def export_nas_source_map(ssh_target: str, archive_root: str, version: str | None) -> dict:
    """One indexed version query, no historical scan or bundle filesystem probes.

    This metadata-only adapter is bounded. An archive producer can supply the
    same JSON contract offline when the NAS database is unavailable.
    """
    if version is not None and not re.fullmatch(r"[0-9]+", version):
        raise LatestLocalizationError("invalid assets version")
    script = r'''
import hashlib,json,pathlib,sqlite3,sys,msgpack
root=pathlib.Path(sys.argv[1]); requested=sys.argv[2]
control=json.loads((root/'manifest.json').read_text())
ready=[r for r in control['releases'].values() if r.get('complete') is True and r.get('materialized') is True]
release=next((r for r in ready if str(r['version'])==requested),None) if requested else max(ready,key=lambda r:int(r['version']))
if release is None: raise ValueError('requested version is not complete/materialized')
version=str(release['version']); name=release['index_name']
if not version.isdigit() or pathlib.PurePosixPath(name).name!=name: raise ValueError('unsafe release identity')
data=(root/'views'/version/'jp-android'/name).read_bytes()
if hashlib.sha256(data).hexdigest()!=release['index_sha256']: raise ValueError('index SHA mismatch')
catalog=msgpack.unpackb(data,raw=False,strict_map_key=False)[0]
names=sorted({str(v[1]) for k,v in catalog.items() if str(k).lower().endswith('_jp.gtx.unity3d')})
db=sqlite3.connect('file:'+str(root/'index.sqlite3')+'?mode=ro',uri=True,timeout=5)
db.execute('CREATE TEMP TABLE wanted(name TEXT PRIMARY KEY)')
db.executemany('INSERT INTO wanted(name) VALUES (?)',((n,) for n in names))
rows=[{'remote':n,'source_sha256':h,'declared_size':s} for n,h,s in db.execute('SELECT e.name,e.sha256,e.size FROM entries e JOIN wanted w ON w.name=e.name WHERE e.version=? AND e.scope=?',(version,'jp-android'))]
if len(rows)!=len(names): raise ValueError('source map missing indexed GTX rows')
after=json.loads((root/'manifest.json').read_text())['releases'][version]
if not after.get('complete') or not after.get('materialized') or after['index_sha256']!=release['index_sha256']: raise ValueError('release changed during export')
print(json.dumps({'schema_version':1,'surface':'gtx','release':release,'index_sha256':release['index_sha256'],'objects':rows}))
'''
    command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", ssh_target,
               "timeout 45s python3 - " + shlex.quote(archive_root) + " " + shlex.quote(version or "")]
    result = subprocess.run(command, input=script, capture_output=True, text=True,
                            encoding="utf-8", timeout=55, check=False)
    if result.returncode:
        raise LatestLocalizationError("compact NAS source-map export failed: " + result.stderr[-500:])
    return json.loads(result.stdout)



def verify_nas_source_fingerprint(ssh_target: str, archive_root: str,
                                  release: dict[str, Any], source_map: dict) -> dict:
    """Verify current NAS source identity with a compact SQL fingerprint."""
    version = str(release.get("version") or "")
    index_name = str(release.get("index_name") or "")
    expected_index = str(release.get("index_sha256") or "").lower()
    if not (version.isdigit() and index_name and re.fullmatch(r"[0-9a-f]{64}", expected_index)):
        raise LatestLocalizationError("invalid release identity for source fingerprint")
    local_rows = sorted(
        (str(item["remote"]), str(item["source_sha256"]).lower(), int(item["declared_size"]))
        for item in source_map.get("objects", [])
    )
    if len(local_rows) != len(source_map.get("objects", [])):
        raise LatestLocalizationError("source map contains duplicate or malformed rows")

    def fingerprint(rows: list[tuple[str, str, int]]) -> str:
        payload = "".join(f"{name}\t{digest}\t{size}\n" for name, digest, size in rows)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    local_digest = fingerprint(local_rows)
    script = r'''
import hashlib,json,pathlib,sqlite3,sys,msgpack
root=pathlib.Path(sys.argv[1]); version=sys.argv[2]; index_name=sys.argv[3]; expected_index=sys.argv[4]
control=json.loads((root/'manifest.json').read_text())
release=control['releases'].get(version)
if not isinstance(release,dict) or not release.get('complete') or not release.get('materialized'):
    raise ValueError('release is not complete/materialized')
if release.get('index_name') != index_name or str(release.get('index_sha256','')).lower() != expected_index:
    raise ValueError('release identity changed during fingerprint')
index=root/'views'/version/'jp-android'/index_name
data=index.read_bytes()
if hashlib.sha256(data).hexdigest() != expected_index:
    raise ValueError('index SHA mismatch')
catalog=msgpack.unpackb(data,raw=False,strict_map_key=False)[0]
names=sorted({str(v[1]) for k,v in catalog.items() if str(k).lower().endswith('_jp.gtx.unity3d')})
db=sqlite3.connect('file:'+str(root/'index.sqlite3')+'?mode=ro',uri=True,timeout=15)
db.execute('create temp table wanted(name text primary key)')
db.executemany('insert into wanted(name) values (?)', ((name,) for name in names))
rows=sorted((str(n),str(h).lower(),int(s)) for n,h,s in db.execute(
    'select e.name,e.sha256,e.size from entries e join wanted w on w.name=e.name where e.version=? and e.scope=?',
    (version,'jp-android')))
if len(rows)!=len(names): raise ValueError('current SQL rows do not cover version index')
payload=''.join(f'{n}\t{h}\t{s}\n' for n,h,s in rows).encode('utf-8')
print(json.dumps({'version':version,'index_sha256':expected_index,'object_count':len(rows),'fingerprint_sha256':hashlib.sha256(payload).hexdigest()}))
'''
    command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", ssh_target,
               "timeout 90s python3 - " + shlex.quote(archive_root) + " "
               + shlex.quote(version) + " " + shlex.quote(index_name) + " "
               + shlex.quote(expected_index)]
    result = subprocess.run(command, input=script, capture_output=True, text=True,
                            encoding="utf-8", timeout=105, check=False)
    if result.returncode:
        raise LatestLocalizationError("NAS compact source fingerprint failed: " + result.stderr[-500:])
    remote = json.loads(result.stdout)
    if remote.get("object_count") != len(local_rows):
        raise LatestLocalizationError("NAS source fingerprint row count mismatch")
    if remote.get("fingerprint_sha256") != local_digest:
        raise LatestLocalizationError("NAS source fingerprint mismatch")
    return {
        "schema_version": 1,
        "method": "nas_sql_compact_fingerprint",
        "version": version,
        "index_sha256": expected_index,
        "object_count": len(local_rows),
        "fingerprint_sha256": local_digest,
        "verified": True,
    }

def build_prior_backed_source_map(index_path: Path, release: dict[str, Any],
                                  prior_manifest: Path, project_root: Path,
                                  ssh_target: str, archive_root: str) -> dict:
    """Use the frozen source manifest for logical rows and query only new rows.

    NAS object names are version-scoped and therefore cannot be used as a
    reuse key. The prior manifest's local source file and SHA are retained as
    evidence; rows absent from it are looked up by exact SQLite primary-key
    queries. The resulting map is explicitly marked partial until current
    source hashes for inherited rows have been rechecked.
    """
    release = dict(release)
    rows = load_rows(index_path)
    manifest = json.loads(prior_manifest.read_text(encoding="utf-8"))
    old = {item["logical"]: item for item in manifest.get("bundles", [])}
    inherited = []
    missing = []
    for row in rows:
        item = old.get(row["logical"])
        if item and item.get("source_bundle_sha256"):
            source_path = Path(str(item.get("source_path", "")).replace("\\", "/"))
            if not source_path.is_absolute():
                source_path = (project_root / source_path).resolve()
            if (source_path.is_file() and sha256(source_path) == item["source_bundle_sha256"]
                    and source_path.stat().st_size == row["declared_size"]):
                inherited.append({"remote": row["remote"], "source_sha256": item["source_bundle_sha256"],
                                  "declared_size": row["declared_size"], "identity_evidence": "prior_manifest_logical"})
            else:
                # A changed declared size or missing local evidence is a
                # bounded current-SQL lookup, never an unverified reuse.
                missing.append(row)
        else:
            missing.append(row)
    found = {}
    for start in range(0, len(missing), 50):
        batch = missing[start:start + 50]
        names_json = json.dumps([safe_remote_object(row["remote"]) for row in batch])
        script = """import json,sqlite3,sys
names=json.loads(sys.argv[1])
c=sqlite3.connect('file:'+sys.argv[2]+'/index.sqlite3?mode=ro',uri=True,timeout=5)
q='select name,sha256,size from entries where version=? and scope=? and name in ('+','.join('?' for _ in names)+')'
print(json.dumps([{'remote':n,'source_sha256':h,'declared_size':s} for n,h,s in c.execute(q,[sys.argv[3],'jp-android',*names])]))
"""
        command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", ssh_target,
                   "timeout 35s python3 - " + shlex.quote(names_json) + " " + shlex.quote(archive_root) + " " + shlex.quote(str(release["version"]))]
        result = subprocess.run(command, input=script, capture_output=True, text=True,
                                encoding="utf-8", timeout=45, check=False)
        if result.returncode:
            raise LatestLocalizationError("NAS delta source-map query failed: " + result.stderr[-500:])
        for item in json.loads(result.stdout):
            found[item["remote"]] = item
    if set(found) != {row["remote"] for row in missing}:
        raise LatestLocalizationError("NAS delta source map omitted new objects")
    objects = inherited + [{**found[row["remote"]], "identity_evidence": "nas_index_sql"} for row in missing]
    return {"schema_version": 1, "surface": "gtx", "release": release,
            "index_sha256": release["index_sha256"], "objects": objects,
            "source_identity_mode": "prior_logical_sha_plus_new_nas_sql",
            "inherited_rows_current_hash_unverified": bool(inherited)}


def build_candidate_plan(source_map: dict, index_path: Path,
                         reuse_manifests: list[tuple[Path, Path]], client_version: str) -> tuple[dict, bytes]:
    """Build a full GTX gap plan and partial overlay index without copying payloads.

    Reuse needs logical context + exact source SHA and verified local output
    bytes. Missing GTX and every other surface keep their original index rows
    for same-version JP fallback. This is not publication or full localization.
    """
    release = source_map["release"]
    version = str(release["version"])
    if (release.get("complete") is not True or release.get("materialized") is not True
            or release.get("scope", "jp-android") != "jp-android"
            or release.get("app_version") != client_version
            or not re.fullmatch(r"[0-9]+", version)):
        raise LatestLocalizationError("incomplete or incompatible source release")
    digest = sha256(index_path)
    if digest != release["index_sha256"] or digest != source_map["index_sha256"]:
        raise LatestLocalizationError("source index SHA-256 mismatch")
    original = msgpack.unpackb(index_path.read_bytes(), raw=False, strict_map_key=False)
    updated = copy.deepcopy(original)
    rows = load_rows(index_path)
    mapping = {}
    for item in source_map["objects"]:
        remote = safe_remote_object(item["remote"])
        if remote in mapping or not re.fullmatch(r"[0-9a-f]{64}", str(item["source_sha256"])):
            raise LatestLocalizationError("duplicate or invalid source identity")
        mapping[remote] = item
    if set(mapping) != {r["remote"] for r in rows}:
        raise LatestLocalizationError("source map does not cover exactly the full GTX index")
    reusable = {}; inputs = []
    for project_root, manifest_path in reuse_manifests:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        identity = manifest.get("version_identity") or {}
        if identity.get("client_version") != client_version or manifest.get("scope") != "jp-android":
            raise LatestLocalizationError("incompatible reuse manifest")
        inputs.append({"path": str(manifest_path.resolve()), "sha256": sha256(manifest_path)})
        for item in manifest["bundles"]:
            key = (item["logical"], item["source_bundle_sha256"])
            output = Path(item["output_path"].replace("\\", "/"))
            output = output if output.is_absolute() else project_root / output
            output = output.resolve()
            if not output.is_relative_to(project_root.resolve()):
                raise LatestLocalizationError("reuse output outside supplied project root")
            candidate = {"path": str(output), "sha256": item["output_bundle_sha256"],
                         "size": item["output_bytes"], "origin_assets_version": identity.get("assets_version"),
                         "manifest_sha256": inputs[-1]["sha256"]}
            if key in reusable and reusable[key]["sha256"] != candidate["sha256"]:
                raise LatestLocalizationError("conflicting localized outputs for identical source/context")
            reusable[key] = candidate
    objects = []; verified = {}; changed = 0
    for row in rows:
        source = mapping[row["remote"]]
        if source["declared_size"] != row["declared_size"]:
            raise LatestLocalizationError("source size disagrees with asset index")
        output = reusable.get((row["logical"], source["source_sha256"]))
        if output:
            path = Path(output["path"])
            if path not in verified:
                verified[path] = (path.stat().st_size, sha256(path))
            if verified[path] != (output["size"], output["sha256"]):
                raise LatestLocalizationError("localized output size/SHA-256 mismatch: " + str(path))
            updated[0][row["logical"]][2] = output["size"]
            changed += 1
        objects.append({**row, "source_sha256": source["source_sha256"],
                        "reuse_overlay": output,
                        "action": "reuse_local_overlay" if output else "needs_localization"})
    # Explicitly prove every untouched row and all index metadata survive.
    touched = {r["logical"] for r in objects if r["reuse_overlay"]}
    for logical, value in original[0].items():
        expected = list(value) if logical in touched else value
        if logical in touched:
            expected[2] = updated[0][logical][2]
        if updated[0][logical] != expected:
            raise LatestLocalizationError("candidate index changed a non-size field")
    packed = msgpack.packb(updated, use_bin_type=True)
    if msgpack.unpackb(packed, raw=False, strict_map_key=False) != updated:
        raise LatestLocalizationError("candidate index roundtrip failed")
    missing = [r for r in objects if not r["reuse_overlay"]]
    plan = {"schema_version": 1, "release": release, "index_sha256": digest,
            "surface": "gtx", "other_surfaces": "original_version_fallback_unassessed",
            "canonical": False, "published": False, "publication_ready": False,
            "partial_localization": bool(missing), "reuse_manifests": inputs, "objects": objects,
            "candidate_index_sha256": hashlib.sha256(packed).hexdigest(),
            "fallback": {"version": version, "scope": "jp-android", "index_rows_unchanged": len(original[0])-changed,
                         "contract": "overlay first; original same-version view second", "http_verified": False},
            "counts": {"gtx_objects": len(rows), "reusable_overlay_objects": changed,
                       "requires_processing": len(missing),
                       "download_bytes_before_local_reuse": sum(r["declared_size"] for r in missing),
                       "reused_source_bytes": sum(r["declared_size"] for r in objects if r["reuse_overlay"])}}
    return plan, packed


def write_candidate_plan(plan: dict, packed: bytes, output: Path) -> dict:
    """Immutable resumable checkpoint; detect drift before overwriting anything."""
    output.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(plan, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    files = {output / "nas-gap-plan.json": encoded, output / "candidate-index.data": packed}
    # A crash after one file is recoverable. Conflicting existing bytes fail closed.
    for path, data in files.items():
        if path.exists() and path.read_bytes() != data:
            raise LatestLocalizationError("candidate checkpoint drift: " + str(path))
    skipped = all(path.exists() for path in files)
    for path, data in files.items():
        if not path.exists():
            with path.open("xb") as stream:
                stream.write(data)
    return {"status": "skipped_verified" if skipped else "candidate_written",
            "counts": plan["counts"], "plan": str(output / "nas-gap-plan.json"), "published": False}


def run_command(command: list[str], *, stdout_path: Path | None = None) -> str:
    completed = subprocess.run(
        command,
        cwd=str(ROOT),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if stdout_path:
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stdout_path.write_text(completed.stdout, encoding="utf-8")
        (stdout_path.with_suffix(".stderr.log")).write_text(completed.stderr, encoding="utf-8")
    if completed.returncode != 0:
        raise LatestLocalizationError(
            f"command failed with exit code {completed.returncode}: {' '.join(command)}"
        )
    return completed.stdout


def discover_latest(asset_root: Path, run_dir: Path | None = None) -> dict[str, Any]:
    command = [
        sys.executable,
        str(ROOT / "tools/asset_version.py"),
        "--root",
        str(asset_root),
        "remote",
        "list",
        "--json",
        "--limit",
        "1",
    ]
    output = run_command(command, stdout_path=(run_dir / "latest-discovery.stdout.log") if run_dir else None)
    rows = json.loads(output)
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        raise LatestLocalizationError("remote asset catalogue returned no latest version")
    return dict(rows[0])


def nas_latest_release(ssh_target: str, manifest_path: str, run_dir: Path | None = None) -> dict[str, Any]:
    """Read the NAS archive manifest and select its newest complete materialized release."""
    output = run_command(
        ["ssh", ssh_target, "cat", manifest_path],
        stdout_path=(run_dir / "nas-manifest.stdout.log") if run_dir else None,
    )
    document = json.loads(output)
    releases = document.get("releases") if isinstance(document, dict) else None
    if not isinstance(releases, dict):
        raise LatestLocalizationError("NAS archive manifest has no releases")
    complete = [
        dict(row)
        for row in releases.values()
        if isinstance(row, dict) and row.get("complete") and row.get("materialized")
    ]
    if not complete:
        raise LatestLocalizationError("NAS archive has no complete materialized release")
    latest = max(complete, key=lambda row: int(str(row.get("version", "0"))))
    latest["nas_active_version"] = document.get("active_version")
    latest["source"] = "nas-archive-manifest"
    return latest


def safe_remote_object(value: str) -> str:
    remote = str(value).replace("\\", "/").lstrip("/")
    if not remote or remote == "." or ".." in remote.split("/"):
        raise LatestLocalizationError(f"unsafe NAS object path: {value!r}")
    return remote


def inspect_nas_localization(ssh_target: str, archive_root: str, version: str) -> dict[str, Any]:
    """Compare source CAS identities on NAS before transferring any bundles.

    Existing published payloads are reusable only when the original source
    SHA-256 is identical. File names/catalogue hashes are not reuse evidence.
    This probe opens SQLite read-only and hashes the reused overlay bytes.
    """
    script = r'''
import hashlib,json,pathlib,sqlite3,sys,msgpack
root=pathlib.Path(sys.argv[1]); version=sys.argv[2]; scope='jp-android'
control=json.loads((root/'manifest.json').read_text())
release=control['releases'][version]
assert release['complete'] and release['materialized']
index=root/'views'/version/scope/release['index_name']
index_bytes=index.read_bytes()
assert hashlib.sha256(index_bytes).hexdigest()==release['index_sha256']
catalog=msgpack.unpackb(index_bytes,raw=False,strict_map_key=False)[0]
db=sqlite3.connect('file:'+str(root/'index.sqlite3')+'?mode=ro',uri=True)
names=[str(v[1]) for k,v in catalog.items() if str(k).lower().endswith('_jp.gtx.unity3d')]
entries={}
for start in range(0,len(names),400):
    group=names[start:start+400]
    entries.update((n,(h,s)) for n,h,s in db.execute('select name,sha256,size from entries where version=? and scope=? and name in ('+','.join('?' for _ in group)+')',[version,scope,*group]))
reuse={}; hashes={}
versions=sorted((p.name for p in (root/'cn-version').iterdir() if p.is_dir() and p.name.isdigit() and int(p.name)<=int(version)),key=int,reverse=True)
for old in versions:
    published={p.name for p in (root/'cn-version'/old/scope).glob('*.unity3d')}
    old_rows=[]
    published=sorted(published)
    for start in range(0,len(published),400):
        group=published[start:start+400]
        old_rows.extend(db.execute('select name,sha256,size from entries where version=? and scope=? and name in ('+','.join('?' for _ in group)+')',[old,scope,*group]))
    for name,digest,size in old_rows:
        p=root/'cn-version'/old/scope/name
        if digest and digest not in reuse and name.endswith('.unity3d') and p.is_file():
            stat=p.stat(); key=(stat.st_dev,stat.st_ino)
            if key not in hashes: hashes[key]=hashlib.sha256(p.read_bytes()).hexdigest()
            reuse[digest]={'version':old,'remote':name,'path':str(p),'sha256':hashes[key],'size':stat.st_size}
rows=[]
for logical,value in sorted(catalog.items()):
    if not str(logical).lower().endswith('_jp.gtx.unity3d'): continue
    remote=str(value[1]); digest,size=entries[remote]
    assert digest and size==int(value[2]), remote
    rows.append({'logical':logical,'remote':remote,'catalog_hash':str(value[0]),'declared_size':size,'source_sha256':digest,'reuse_overlay':reuse.get(digest)})
print(json.dumps({'release':release,'index_sha256':hashlib.sha256(index_bytes).hexdigest(),'objects':rows,'surface':'gtx','other_surfaces':'not_processed'},ensure_ascii=False))
'''
    command = ["ssh", "-o", "BatchMode=yes", ssh_target,
               "timeout 175s python3 - " + shlex.quote(archive_root) + " " + shlex.quote(version)]
    process = subprocess.run(command, input=script, capture_output=True, text=True,
                             encoding="utf-8", timeout=180, check=False)
    if process.returncode:
        raise LatestLocalizationError("NAS localization inventory failed: " + process.stderr[-1000:])
    result = json.loads(process.stdout)
    rows = result["objects"]
    missing = [row for row in rows if not row.get("reuse_overlay")]
    result["counts"] = {"gtx_objects": len(rows), "reusable_overlay_objects": len(rows)-len(missing),
                        "requires_processing": len(missing),
                        "download_bytes_before_local_reuse": sum(row["declared_size"] for row in missing)}
    return result


def prepare_nas_delta(plan: dict[str, Any], run_dir: Path, ssh_target: str,
                      nas_archive_root: str) -> Path:
    """Fetch just the unresolved source bundles; leave reusable overlays on NAS.

    The partial snapshot describes exactly this delta. Its coverage must never
    be reported as coverage of all Unity assets or all sources in the version.
    """
    release = plan['release']
    version = str(release['version'])
    source_root = run_dir / 'source' / 'jp-android'
    source_root.mkdir(parents=True, exist_ok=True)
    index = source_root / safe_remote_object(release['index_name'])
    remote_root = nas_archive_root.rstrip('/') + '/views/' + version + '/jp-android'
    if not index.is_file():
        run_command(['scp', f'{ssh_target}:{remote_root}/{index.name}', str(index)])
    if hashlib.sha256(index.read_bytes()).hexdigest() != plan['index_sha256']:
        raise LatestLocalizationError('NAS index SHA-256 mismatch')
    delta = [row for row in plan['objects'] if not row.get('reuse_overlay')]
    pending = []
    for row in delta:
        destination = source_root / safe_remote_object(row['remote'])
        if destination.is_file() and hashlib.sha256(destination.read_bytes()).hexdigest() == row['source_sha256']:
            continue
        pending.append(row)
    if pending:
        names = [safe_remote_object(row['remote']) for row in pending]
        # A list on stdin avoids command-line limits, and a binary subprocess
        # output avoids PowerShell corrupting the tar stream.
        if any(not __import__('re').fullmatch(r'[0-9a-f]{40}\.unity3d', name) for name in names):
            raise LatestLocalizationError('unexpected remote bundle name')
        archive = run_dir / 'delta-transfer.tar'
        with archive.open('wb') as output:
            result = subprocess.run(['ssh', '-o', 'BatchMode=yes', ssh_target,
                                     'tar -C ' + shlex.quote(remote_root) + ' -cf - -T -'],
                                    input=('\n'.join(names)+'\n').encode(), stdout=output, stderr=subprocess.PIPE, timeout=180)
        if result.returncode:
            raise LatestLocalizationError('NAS selective transfer failed')
        expected = {row['remote']: row for row in pending}
        with tarfile.open(archive) as bundle:
            for member in bundle:
                name = member.name.removeprefix('./')
                if name not in expected or not member.isfile():
                    raise LatestLocalizationError('unexpected tar member')
                data = bundle.extractfile(member).read()
                row = expected.pop(name)
                if len(data) != row['declared_size'] or hashlib.sha256(data).hexdigest() != row['source_sha256']:
                    raise LatestLocalizationError('source bundle SHA-256 mismatch: '+name)
                (source_root / name).write_bytes(data)
        if expected:
            raise LatestLocalizationError('NAS transfer omitted objects')
        archive.unlink()  # verified, task-owned transport scratch only
    snapshot = run_dir / 'delta-snapshot.json'
    payload = {'schema_version':1, 'scope':'jp-android', 'asset_index':str(index.resolve()),
               'upstream_root':release['asset_root'], 'complete':True,
               'partial_universe':True, 'selection':'GTX without identical-source published overlay',
               'objects':delta, 'full_gtx_count':len(plan['objects']),
               'reused_on_nas':len(plan['objects'])-len(delta)}
    snapshot.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
    return snapshot


def sync_nas_gtx(
    *,
    ssh_target: str,
    nas_archive_root: str,
    release: dict[str, Any],
    cache_root: Path,
    run_dir: Path,
) -> tuple[Path, dict[str, Any]]:
    """Copy only the NAS index and JP GTX objects into a local text cache."""
    version = str(release["version"])
    scope = str(release.get("scope") or "jp-android")
    index_name = str(release.get("index_name") or "")
    if not index_name:
        raise LatestLocalizationError(f"NAS release {version} has no index_name")
    view_root = cache_root / "views" / version
    scope_root = view_root / scope
    scope_root.mkdir(parents=True, exist_ok=True)
    remote_scope = f"{nas_archive_root.rstrip('/')}/views/{version}/{scope}"
    index_path = scope_root / index_name
    if not index_path.is_file():
        run_command(["scp", f"{ssh_target}:{remote_scope}/{index_name}", str(index_path)], stdout_path=run_dir / "nas-index.stdout.log")
    rows = load_rows(index_path)
    fetched = reused = 0
    pending: list[tuple[dict[str, Any], Path, str]] = []
    for row in rows:
        remote = safe_remote_object(row["remote"])
        destination = scope_root / remote
        destination.parent.mkdir(parents=True, exist_ok=True)
        expected = int(row["declared_size"])
        if destination.is_file() and destination.stat().st_size == expected:
            reused += 1
            continue
        pending.append((row, destination, remote))
    # NAS object names are normally flat hashes.  Batch them to avoid one SSH
    # handshake per GTX bundle; nested names fall back to one-file copies.
    flat = [item for item in pending if "/" not in item[2]]
    nested = [item for item in pending if "/" in item[2]]
    for start in range(0, len(flat), 64):
        batch = flat[start:start + 64]
        command = ["scp"] + [f"{ssh_target}:{remote_scope}/{remote}" for _row, _destination, remote in batch] + [str(scope_root)]
        run_command(command, stdout_path=run_dir / "nas-gtx-batch.stdout.log")
        fetched += len(batch)
    for row, destination, remote in nested:
        run_command(
            ["scp", f"{ssh_target}:{remote_scope}/{remote}", str(destination)],
            stdout_path=run_dir / "nas-gtx-nested.stdout.log",
        )
        fetched += 1
    for row, destination, remote in pending:
        expected = int(row["declared_size"])
        if not destination.is_file() or destination.stat().st_size != expected:
            raise LatestLocalizationError(f"NAS GTX object size mismatch after copy: {remote}")
    result = {
        "status": "synced",
        "source": "nas-archive",
        "ssh_target": ssh_target,
        "nas_archive_root": nas_archive_root,
        "version": version,
        "scope": scope,
        "index": str(index_path),
        "gtx_objects": len(rows),
        "fetched": fetched,
        "reused": reused,
        "non_gtx_objects_downloaded": 0,
    }
    (run_dir / "nas-gtx-sync.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return view_root, result


def load_release(asset_root: Path, version: str) -> dict[str, Any]:
    manifest = asset_root / "manifest.json"
    if not manifest.is_file():
        raise LatestLocalizationError(f"asset store manifest missing: {manifest}")
    document = json.loads(manifest.read_text(encoding="utf-8"))
    release = (document.get("releases") or {}).get(str(version))
    if not isinstance(release, dict):
        raise LatestLocalizationError(f"asset version not registered after acquisition: {version}")
    if not release.get("complete") or not release.get("materialized"):
        raise LatestLocalizationError(f"asset version is not complete/materialized: {version}")
    return release


def acquire_latest(asset_root: Path, version: str, workers: int, min_free_gib: float, run_dir: Path) -> dict[str, Any]:
    command = [
        sys.executable,
        str(ROOT / "tools/asset_version.py"),
        "--root",
        str(asset_root),
        "remote",
        "pull",
        version,
        "--workers",
        str(workers),
        "--min-free-gib",
        str(min_free_gib),
    ]
    run_command(command, stdout_path=run_dir / "asset-pull.stdout.log")
    return load_release(asset_root, version)


def build_snapshot(asset_root: Path, release: dict[str, Any], snapshot: Path, workers: int, run_dir: Path) -> None:
    version = str(release["version"])
    scope = str(release.get("scope") or "jp-android")
    view_root = asset_root / "views" / version
    index_name = str(release.get("index_name") or "")
    index_path = view_root / scope / index_name
    if not index_path.is_file():
        raise LatestLocalizationError(f"materialized asset index missing: {index_path}")
    command = [
        sys.executable,
        str(ROOT / "scripts/cache_localization_gtx.py"),
        "--asset-index",
        str(index_path),
        "--archive-root",
        str(view_root),
        "--scope",
        scope,
        "--upstream-root",
        str(release.get("asset_root") or ""),
        "--snapshot",
        str(snapshot),
        "--workers",
        str(workers),
    ]
    run_command(command, stdout_path=run_dir / "snapshot.stdout.log")


def runner_command(args: argparse.Namespace, release: dict[str, Any], snapshot: Path, view_root: Path, run_dir: Path) -> list[str]:
    command = [
        sys.executable,
        str(ROOT / "scripts/run_localization_ci.py"),
        "run",
        "--run-dir",
        str(run_dir),
        "--auto-prepare",
        "--check-latest-remote",
        "--client-version",
        str(args.client_version),
        "--asset-version",
        str(release["version"]),
        "--asset-snapshot",
        str(snapshot),
        "--archive-root",
        str(view_root),
        "--config",
        str(args.config),
        "--batch-mode",
        str(args.batch_mode),
        "--artifact-store-root",
        str(args.artifact_store_root),
        "--nas-overlay-root",
        str(args.nas_overlay_root),
        "--publish-transport",
        str(args.publish_transport),
        "--publish-ssh-target",
        str(args.publish_ssh_target),
        "--owner-bypass-quality",
        "--publish-assets",
    ]
    if args.publish_apply:
        command.append("--publish-apply")
    for model_id in args.model_id:
        command.extend(["--model-id", model_id])
    for seed in args.prepare_translation:
        command.extend(["--prepare-translation", seed])
    for value in args.companion:
        command.extend(value)
    return command


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=("nas", "remote"), default="nas")
    parser.add_argument("--version", default=None, help="override the NAS/remote version; default is newest complete source release")
    parser.add_argument("--asset-store-root", required=True, type=Path)
    parser.add_argument("--snapshot-root", required=True, type=Path)
    parser.add_argument("--run-root", default=str(ROOT / "build/runs/text-localization/9.0.200"), type=Path)
    parser.add_argument("--client-version", default="9.0.200")
    parser.add_argument("--config", default="localization/api-models.local.json")
    parser.add_argument("--artifact-store-root", default=str(ROOT / "build/localization-artifacts"), type=Path)
    parser.add_argument("--model-id", action="append", default=[])
    parser.add_argument("--batch-mode", choices=("single", "dynamic", "dynamic-mixed"), default="dynamic")
    parser.add_argument("--prepare-translation", action="append", default=[])
    parser.add_argument("--companion", action="append", nargs=2, metavar=("OPTION", "PATH"), default=[])
    parser.add_argument("--pull-latest", action="store_true")
    parser.add_argument("--workers", type=int, default=256)
    parser.add_argument("--snapshot-workers", type=int, default=32)
    parser.add_argument("--min-free-gib", type=float, default=80.0)
    parser.add_argument("--publish-apply", action="store_true")
    parser.add_argument("--publish-transport", choices=("plan", "local", "ssh"), default="ssh")
    parser.add_argument("--publish-ssh-target", default="nas")
    parser.add_argument("--nas-overlay-root", default="/vol2/1000/imas-asset-archive/mltd/cn-version")
    parser.add_argument("--nas-ssh-target", default="nas")
    parser.add_argument("--nas-archive-root", default="/vol2/1000/imas-asset-archive/mltd")
    parser.add_argument("--nas-manifest", default="/vol2/1000/imas-asset-archive/mltd/manifest.json")
    parser.add_argument("--prepare-only", action="store_true",
                        help="compare NAS source hashes, fetch only unresolved GTX, then stop before translation/publication")
    parser.add_argument("--reuse-manifest", action="append", default=[],
                        help="localization-manifest.json to reuse by exact logical/source SHA; repeatable")
    parser.add_argument("--verify-source-fingerprint", action="store_true",
                        help="compactly verify all source (name, SHA-256, size) rows against NAS SQLite")
    args = parser.parse_args(argv)
    try:
        asset_root = args.asset_store_root.resolve()
        snapshot_root = args.snapshot_root.resolve()
        run_root = args.run_root.resolve()
        if args.source == "nas":
            latest = nas_latest_release(args.nas_ssh_target, args.nas_manifest)
        else:
            latest = discover_latest(asset_root)
        version = str(latest["version"])
        if args.version:
            version = str(args.version)
            if args.source == "nas":
                if version != str(latest["version"]):
                    # A pinned NAS version is allowed only if it is complete
                    # and materialized; re-read the manifest and select it.
                    manifest_output = run_command(["ssh", args.nas_ssh_target, "cat", args.nas_manifest])
                    releases = json.loads(manifest_output).get("releases", {})
                    pinned = releases.get(version)
                    if not isinstance(pinned, dict) or not pinned.get("complete") or not pinned.get("materialized"):
                        raise LatestLocalizationError(f"pinned NAS version is not complete/materialized: {version}")
                    latest = dict(pinned)
        run_dir = run_root / f"latest-{args.client_version}-{version}"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "latest-remote.json").write_text(json.dumps(latest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if args.prepare_only:
            if args.source != 'nas':
                raise LatestLocalizationError('--prepare-only currently requires NAS source')
            reuse_inputs = []
            for value in args.reuse_manifest:
                path = Path(value).resolve()
                if not path.is_file():
                    raise LatestLocalizationError("reuse manifest missing: " + str(path))
                project_root = path.parents[3] if path.name == "localization-manifest.json" else path.parent
                reuse_inputs.append((project_root, path))
            manifest_release = dict(latest)
            release = manifest_release
            index_path = run_dir / "source-index.data"
            if not index_path.exists():
                run_command(["scp", f"{args.nas_ssh_target}:{args.nas_archive_root.rstrip('/')}/views/{version}/jp-android/{release['index_name']}", str(index_path)])
            if reuse_inputs:
                source_map = build_prior_backed_source_map(index_path, release, reuse_inputs[0][1], reuse_inputs[0][0], args.nas_ssh_target, args.nas_archive_root)
            else:
                source_map = export_nas_source_map(args.nas_ssh_target, args.nas_archive_root, version)
            if args.verify_source_fingerprint:
                fingerprint = verify_nas_source_fingerprint(args.nas_ssh_target, args.nas_archive_root, release, source_map)
                source_map["source_fingerprint"] = fingerprint
                source_map["inherited_rows_current_hash_unverified"] = False
                source_map["source_identity_mode"] = "prior_logical_sha_plus_nas_sql_fingerprint"
                (run_dir / "source-fingerprint.json").write_text(json.dumps(fingerprint, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            plan, packed = build_candidate_plan(source_map, index_path, reuse_inputs, args.client_version)
            checkpoint = write_candidate_plan(plan, packed, run_dir / "candidate")
            (run_dir / "source-map.json").write_text(json.dumps(source_map, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({**checkpoint, "source_map": str(run_dir / 'source-map.json')}, ensure_ascii=False, indent=2))
            return 0
        if args.source == "nas":
            view_root, sync_result = sync_nas_gtx(
                ssh_target=args.nas_ssh_target,
                nas_archive_root=args.nas_archive_root,
                release=latest,
                cache_root=asset_root,
                run_dir=run_dir,
            )
            release = latest
            (run_dir / "source-release.json").write_text(json.dumps(release, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        elif args.pull_latest:
            release = acquire_latest(asset_root, version, args.workers, args.min_free_gib, run_dir)
            view_root = asset_root / "views" / version
        else:
            release = load_release(asset_root, version)
            view_root = asset_root / "views" / version
        snapshot = snapshot_root / version / "jp-gtx-cache-snapshot.json"
        if not snapshot.is_file():
            build_snapshot(asset_root, release, snapshot, args.snapshot_workers, run_dir)
        command = runner_command(args, release, snapshot, view_root, run_dir)
        result = run_command(command, stdout_path=run_dir / "localization-ci.stdout.log")
        print(result)
        return 0
    except (LatestLocalizationError, OSError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        print(f"latest localization CI blocked: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
