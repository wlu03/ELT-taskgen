"""Independent Python business oracles: sweep intervals, walk graphs, scan events."""
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from elt_taskgen.generation.scenario_evidence import INTERVAL,SESSION,LIFECYCLE,DOCUMENT,LEDGER,HIERARCHY


def stamp(value):
    return None if value is None else datetime.fromisoformat(str(value))


def decimal(value):
    return Decimal(str(value or 0))


def rounded(value):
    return float(decimal(value).quantize(Decimal('0.0001'),rounding=ROUND_HALF_UP))


def oracle(e, recipe, rows):
    key=lambda row,cols:tuple(row[c] for c in cols)
    parents={key(r,e.keys):r for r in rows[e.parent]}
    facts=defaultdict(list)
    for row in rows[e.fact]:
        owner=key(row,e.links)
        if None not in owner and owner in parents:
            facts[owner].append(row)
    output=[]
    for owner,parent in parents.items():
        identity=dict(zip(e.keys,owner))
        activity=facts[owner]
        if recipe==INTERVAL:
            lo,hi=stamp(parent[e.parent_start]),stamp(parent[e.parent_end])
            spans=[]
            if lo is not None and hi is not None and hi>lo:
                for row in activity:
                    a,b=stamp(row[e.start]),stamp(row[e.end])
                    if a is not None and b is not None and b>a and a<hi and b>lo:
                        spans.append((max(a,lo),min(b,hi)))
            total=0.0
            merged=[]
            for start,end in sorted(spans):
                if merged and start<=merged[-1][1]:
                    merged[-1]=(merged[-1][0],max(end,merged[-1][1]))
                else:
                    merged.append((start,end))
            total=sum((end-start).total_seconds() for start,end in merged)
            output.append({**identity,'interval_count':len(spans),'covered_seconds':total})
        elif recipe==SESSION:
            ordered=sorted((r for r in activity if r[e.start] is not None),
                key=lambda r:(stamp(r[e.start]),key(r,e.row_keys)))
            bursts=[]
            for row in ordered:
                if not bursts or stamp(row[e.start])-stamp(bursts[-1][-1][e.start])>timedelta(minutes=30):
                    bursts.append([])
                bursts[-1].append(row)
            for burst,group in enumerate(bursts,1):
                for pos,row in enumerate(group,1):
                    output.append({**dict(zip(e.row_keys,key(row,e.row_keys))),
                        'burst_number':burst,'position_in_burst':pos,'burst_event_count':len(group)})
        elif recipe==LIFECYCLE:
            joined=stamp(parent[e.parent_start])
            if joined is None:
                continue
            counts=[0]*6
            for row in activity:
                moment=stamp(row[e.start])
                if moment is not None and moment>=joined:
                    month=(moment.year-joined.year)*12+moment.month-joined.month
                    if 0<=month<6:
                        counts[month]+=1
            previous,ever=0,False
            for month,count in enumerate(counts):
                label=('new' if count>0 and not ever else 'reactivated' if count>0 and previous==0
                    else 'stopped' if count==0 and previous>0 else 'increased' if count>previous
                    else 'decreased' if count<previous else 'unchanged' if count>0 else 'inactive')
                output.append({**identity,'month_index':month,'event_count':count,'activity_state':label})
                previous,ever=count,ever or count>0
        elif recipe==LEDGER:
            debit=sum((decimal(r[e.debit]) for r in activity),Decimal(0))
            credit=sum((decimal(r[e.credit]) for r in activity),Decimal(0))
            delta=rounded(debit-credit)
            output.append({**identity,'total_debit':float(debit),'total_credit':float(credit),
                           'line_count':len(activity),'imbalance':delta,'is_balanced':abs(delta)<=0.01})
        elif recipe==DOCUMENT:
            payments=[r for r in rows[e.other] if key(r,e.other_links)==owner]
            amount=decimal(parent[e.amount])
            paid=sum((decimal(r[e.other_amount]) for r in payments),Decimal(0))
            remainder=rounded(amount-paid)
            label='settled' if abs(remainder)<=0.01 else 'overpaid' if remainder<0 else 'unpaid' if not payments else 'outstanding'
            output.append({**identity,'document_amount':float(amount),
                'line_total':float(sum((decimal(r[e.debit]) for r in activity),Decimal(0))),
                'paid_amount':float(paid),'line_count':len(activity),'payment_count':len(payments),
                'remaining_amount':remainder,'payment_status':label})
        else:
            adjacency=defaultdict(set)
            for row in rows[e.fact]:
                a,b=key(row,e.links),key(row,e.targets)
                if a in parents and b in parents and None not in (*a,*b):
                    adjacency[a].add(b)
            seen,todo=set(),list(adjacency[owner])
            while todo:
                node=todo.pop()
                if node not in seen:
                    seen.add(node)
                    todo.extend(adjacency[node]-seen)
            output.append({**identity,'reachable_count':len(seen-{owner}),'is_in_cycle':owner in seen})
    return output


def assert_oracle(task,mart,e,rows):
    from tests.test_advanced_recipes import query
    actual,expected=query(task,mart,rows),oracle(e,mart.plan.template_id,rows)
    key=lambda r:tuple(r[k] for k in mart.key_columns)
    assert len(actual)==len(expected),(mart.name,len(actual),len(expected))
    assert len({key(r) for r in actual})==len(actual),(mart.name,'duplicate output keys')
    for want,got in zip(sorted(expected,key=key),sorted(actual,key=key),strict=True):
        for c in mart.columns:
            if isinstance(want[c.name],float):
                assert abs(want[c.name]-float(got[c.name]))<0.00011,(c.name,want,got)
            else:
                assert want[c.name]==got[c.name],(c.name,want,got)
    return actual
