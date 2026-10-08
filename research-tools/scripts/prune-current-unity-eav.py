#!/usr/bin/env python3
import argparse, json, os, sqlite3, shutil, time
from pathlib import Path

RICH=("MonoBehaviour","TextAsset","MonoScript")

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--db',type=Path,required=True)
    ap.add_argument('--ratio',type=float,default=500.0)
    ap.add_argument('--batch-bundles',type=int,default=1000)
    ap.add_argument('--skip-bundles',type=int,default=0)
    args=ap.parse_args()
    p=args.db.resolve()
    con=sqlite3.connect(p,timeout=120)
    con.execute('PRAGMA foreign_keys=ON')
    con.execute('PRAGMA synchronous=NORMAL')
    con.execute('PRAGMA temp_store=MEMORY')
    con.execute('PRAGMA cache_size=-262144')
    # Snapshot target names once: bundle_state scalar_count intentionally stays as ingest provenance
    # during pruning; actual field totals are repaired/summarized from unity_object_field later.
    names=[r[0] for r in con.execute('''SELECT logical_name FROM bundle_state
        WHERE status IN ('ok','partial') AND object_count>0
          AND 1.0*scalar_count/object_count>=?
        ORDER BY logical_name''',(args.ratio,))]
    names=names[args.skip_bundles:]
    before_free=con.execute('PRAGMA freelist_count').fetchone()[0]
    total_deleted=0
    started=time.time()
    for off in range(0,len(names),args.batch_bundles):
        batch=names[off:off+args.batch_bundles]
        ph=','.join('?'*len(batch))
        con.execute('BEGIN IMMEDIATE')
        before=con.total_changes
        con.execute(f'''DELETE FROM unity_object_field WHERE object_id IN (
              SELECT o.object_id FROM unity_object o
              WHERE o.logical_name IN ({ph})
                AND o.type_name IN ('MonoBehaviour','TextAsset','MonoScript')
            ) AND instr(field_path,'[')>0''',batch)
        deleted=con.total_changes-before
        total_deleted+=deleted
        con.commit()
        ck=con.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
        free=con.execute('PRAGMA freelist_count').fetchone()[0]
        disk=shutil.disk_usage(p.parent)
        print(json.dumps({'bundles_done':min(off+len(batch),len(names)),'bundles_total':len(names),
            'deleted_batch':deleted,'deleted_total':total_deleted,
            'freelist_pages':free,'freed_gib_equiv':round((free-before_free)*4096/2**30,3),
            'db_gb':round(os.path.getsize(p)/2**30,2),'disk_free_gb':round(disk.free/2**30,2),
            'checkpoint':ck,'elapsed_s':round(time.time()-started,1)}),flush=True)
    con.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('eav_prune_policy',?)",
                (f'rich-array EAV pruned for ingest bundle scalar/object ratio >= {args.ratio}; complete normalized_json/parsed_json authoritative',))
    con.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('eav_pruned_rows_total',?)",(str(total_deleted),))
    con.commit()
    print(json.dumps({'done':True,'bundles':len(names),'deleted_total':total_deleted,
        'freelist_pages':con.execute('pragma freelist_count').fetchone()[0]}),flush=True)

if __name__=='__main__': main()