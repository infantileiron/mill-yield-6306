import csv
import io
from fastapi import HTTPException
from fastapi.responses import Response


def export_review(obj):
    if obj.status not in ('completed', 'approved'):
        raise HTTPException(409, '计算完成后才可导出')
    output = io.StringIO()
    w = csv.writer(output)
    w.writerow(['复核单', obj.id, '标题', obj.title, '状态', obj.status, '范围', obj.line])
    for k in ['n', 'mean', 'std', 'q1', 'median', 'q3', 'iqr', 'lower', 'upper', 'checksum']:
        w.writerow([k, obj.result[k]])
    # 提交时刻的归并口径：保留原批号与当时归并组，归并组吨数只计一次。
    ctx = obj.context or {}
    if ctx:
        w.writerow(['归并口径-去重核实吨数', ctx.get('deduped_tonnes', ''),
                    '独立批号吨数', ctx.get('standalone_tonnes', ''),
                    '样本总数', ctx.get('sample_count', len(obj.snapshot))])
        for g in ctx.get('groups', []):
            w.writerow(['归并组', g.get('group_code'), '主批号ID', g.get('primary_batch_id'),
                        '成员批号ID', '|'.join(map(str, g.get('member_batch_ids', []))),
                        '核实吨数(只计一次)', g.get('verified_tonnes'),
                        '组内样本数', g.get('sample_count')])
    w.writerow(['签核人', obj.approved_by, '签核意见', obj.note])
    w.writerow(['样本编号', '录入入口批号', '当时归并组', '产线', '出粉率', '水分', '异常'])
    out = {r['sample_code'] for r in obj.result['outliers']}
    for row in obj.snapshot:
        entry_code = row.get('entry_batch_code') or row.get('batch_code', '')
        w.writerow([
            row['sample_code'], entry_code, row.get('group_code') or '',
            row['line'], row['flour_yield'], row['moisture'],
            '是' if row['sample_code'] in out else '否',
        ])
    return Response(
        '﻿' + output.getvalue(),
        media_type='text/csv; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename="mill-yield-review-{obj.id}.csv"'},
    )
