#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def rows_as_dicts(cur):
    cols=[d[0] for d in cur.description]
    return [dict(zip(cols,row)) for row in cur.fetchall()]


def main():
    ap=argparse.ArgumentParser(description='Query current-unity-assets.sqlite')
    ap.add_argument('--db',type=Path,default=ROOT/'build/current-unity-assets.sqlite')
    ap.add_argument('--asset')
    ap.add_argument('--class','--class-name',dest='class_name')
    ap.add_argument('--name-pattern')
    ap.add_argument('--field')
    ap.add_argument('--value')
    ap.add_argument('--path-pattern')
    ap.add_argument('--refs',type=int,metavar='OBJECT_ID')
    ap.add_argument('--hierarchy',type=int,metavar='OBJECT_ID')
    ap.add_argument('--script-class')
    ap.add_argument('--limit',type=int,default=100)
    ap.add_argument('--json',action='store_true')
    args=ap.parse_args()

    con=sqlite3.connect('file:'+args.db.resolve().as_posix()+'?mode=ro',uri=True)
    con.row_factory=sqlite3.Row
    result=[]

    if args.refs is not None:
        q='''SELECT r.field_path,r.file_id,r.path_id,r.target_serialized_file_name,r.target_object_id,
                    t.logical_name AS target_asset,t.serialized_file_name AS target_file,t.type_name AS target_type,t.object_name AS target_name
             FROM unity_object_reference r LEFT JOIN unity_object t ON t.object_id=r.target_object_id
             WHERE r.object_id=? ORDER BY r.field_path LIMIT ?'''
        result=[dict(r) for r in con.execute(q,(args.refs,args.limit))]
    elif args.hierarchy is not None:
        # Accept either a GameObject or Transform object id. Resolve the owning Transform first.
        row=con.execute('''SELECT x.object_id,x.gameobject_object_id FROM unity_transform x
                           WHERE x.object_id=? OR x.gameobject_object_id=? LIMIT 1''',(args.hierarchy,args.hierarchy)).fetchone()
        if row:
            transform_id=row['object_id']
            q='''WITH RECURSIVE chain(depth,transform_id,gameobject_id,parent_id) AS (
                   SELECT 0,x.object_id,x.gameobject_object_id,x.parent_object_id FROM unity_transform x WHERE x.object_id=?
                   UNION ALL
                   SELECT c.depth+1,p.object_id,p.gameobject_object_id,p.parent_object_id
                   FROM chain c JOIN unity_transform p ON p.object_id=c.parent_id WHERE c.depth<128
                 )
                 SELECT c.depth,c.transform_id,c.gameobject_id,g.object_name AS gameobject_name,c.parent_id,
                        g.logical_name,g.serialized_file_name
                 FROM chain c LEFT JOIN unity_object g ON g.object_id=c.gameobject_id ORDER BY c.depth'''
            result=[dict(r) for r in con.execute(q,(transform_id,))]
    else:
        joins=[]; where=[]; params=[]
        select='SELECT DISTINCT o.object_id,o.logical_name,o.serialized_file_name,o.path_id,o.type_name,o.object_name,o.parse_status,o.script_class,o.semantic_tags'
        if args.field or args.value is not None or args.path_pattern:
            joins.append('JOIN unity_object_field f ON f.object_id=o.object_id')
        if args.asset:
            where.append('o.logical_name=?'); params.append(args.asset)
        if args.class_name:
            where.append('o.type_name=?'); params.append(args.class_name)
        if args.name_pattern:
            where.append('o.object_name LIKE ?'); params.append('%'+args.name_pattern+'%')
        if args.script_class:
            where.append('o.script_class LIKE ?'); params.append('%'+args.script_class+'%')
        if args.field:
            where.append("(f.field_path LIKE ? OR f.field_path LIKE ?)")
            params.extend(['%.'+args.field,'%.'+args.field+'[%'])
        if args.path_pattern:
            where.append('f.field_path LIKE ?'); params.append('%'+args.path_pattern+'%')
        if args.value is not None:
            where.append('(f.text_value=? OR CAST(f.int_value AS TEXT)=? OR CAST(f.real_value AS TEXT)=? OR CAST(f.bool_value AS TEXT)=?)')
            params.extend([args.value,args.value,args.value,args.value])
        sql=select+' FROM unity_object o '+' '.join(joins)
        if where: sql+=' WHERE '+' AND '.join(where)
        sql+=' ORDER BY o.logical_name,o.serialized_file_name,o.path_id LIMIT ?'; params.append(args.limit)
        result=[dict(r) for r in con.execute(sql,params)]

    con.close()
    if args.json:
        print(json.dumps(result,ensure_ascii=False,indent=2))
    else:
        for row in result:
            print('\t'.join(f'{k}={v}' for k,v in row.items()))
        print(f'rows={len(result)}')

if __name__=='__main__':
    main()
