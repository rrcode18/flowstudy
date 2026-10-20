"""Client-level FX spot/roll attribution using sparse linear programming.

Run: python fx_matcher.py --spot data/spot_trades.csv --roll data/roll_trades.csv
Requires Python 3.10+, scipy, numpy and matplotlib. Ground truth is NOT an input.
"""
from __future__ import annotations
import argparse
import csv
import json
from collections import defaultdict, deque
from datetime import date, datetime
from pathlib import Path

import numpy as np
from scipy.optimize import linprog
from scipy.sparse import lil_matrix, csr_matrix
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch

UNIT = 1_000_000.0
TOL = 1e-7  # LP amounts are in millions of base-currency units

def load_csv(path, kind):
    required = {'client_id','pair','trade_time_utc','base_amount'}
    required |= {'spot_id','value_date'} if kind == 'spot' else {
        'roll_id','roll_trade_id','start_date','end_date'}
    with open(path, newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        if not required <= set(reader.fieldnames or []):
            raise ValueError(f'{path}: missing {required-set(reader.fieldnames or [])}')
        rows = list(reader)
    seen = set()
    for r in rows:
        ident = r[f'{kind}_id']
        if not ident or ident in seen:
            raise ValueError(f'{path}: duplicate/empty ID {ident}')
        seen.add(ident)
        if not r['client_id'].strip():
            raise ValueError(f'{ident}: empty client')
        r['pair'] = r['pair'].upper().replace('/','').replace(' ','')
        if len(r['pair']) != 6 or not r['pair'].isalpha():
            raise ValueError(f'{ident}: pair must be six letters')
        signed = float(r['base_amount'])/UNIT
        r['amount'] = abs(signed)
        r['direction'] = 'BUY' if signed > 0 else 'SELL'
        if not np.isfinite(signed) or signed == 0:
            raise ValueError(f'{ident}: invalid principal')
        r['time'] = datetime.fromisoformat(r['trade_time_utc'].replace('Z','+00:00'))
        if r['time'].utcoffset() is None or r['time'].utcoffset().total_seconds()!=0:
            raise ValueError(f'{ident}: execution time must specify UTC')
        if kind == 'spot':
            r['start'] = r['end'] = date.fromisoformat(r['value_date'])
        else:
            if not r['roll_trade_id'].strip():
                raise ValueError(f'{ident}: roll_trade_id must not be blank')
            r['start'], r['end'] = map(date.fromisoformat, (r['start_date'],r['end_date']))
            if r['start']>=r['end']:
                raise ValueError(f'{ident}: requires increasing settlement dates')
        if r['direction'] not in {'BUY','SELL'} or r['time'].date()>r['start']:
            raise ValueError(f'{ident}: invalid direction or execution after settlement')
    # Reversed pairs are rejected rather than incorrectly converting base amounts
    # without the rates needed to change the notional currency.
    pairs = {r['pair'] for r in rows}
    if any(p[3:]+p[:3] in pairs for p in pairs):
        raise ValueError('Mixed pair orientation: normalise pair, side AND principal upstream')
    return rows

def aggregate(rows, kind):
    groups = {}
    for r in sorted(rows,key=lambda r:r[f'{kind}_id']):
        key = (r['client_id'],r['pair'],r['direction'],r['start'],r['end'])
        if kind == 'roll':
            key += (r['roll_trade_id'],)
        if key not in groups:
            groups[key] = dict(client_id=key[0],pair=key[1],direction=key[2],
                               start=key[3],end=key[4],amount=0.,ids=[],
                               time_min=r['time'],time_max=r['time'],
                               roll_trade_id=r.get('roll_trade_id',''))
        g = groups[key]
        g['amount'] += r['amount']; g['ids'].append(r[f'{kind}_id'])
        g['time_min'] = min(g['time_min'],r['time'])
        g['time_max'] = max(g['time_max'],r['time'])
    result = list(groups.values())
    for i,g in enumerate(result,1):
        g['group_id'] = f'{"P" if kind=="spot" else "RG"}{i:03d}'
    return result

def partition_key(g):
    return g['client_id'],g['pair'],g['direction']

class FlowModel:
    """Source-labelled flows over an execution-aware roll transition graph.

    Each variable is either a transfer (source/roll -> roll) or an exit.
    Conservation is enforced separately by source. All sources share roll caps.
    Aggregated chronology is conservative: previous max execution <= next min.
    """
    def __init__(self, sources, rolls):
        self.sources,self.rolls = sources,rolls
        self.variables=[]; self.nodes=[]; self.incoming=defaultdict(list)
        self.balance=defaultdict(list); self.source_exit={}; self.reachable={}
        by_near=defaultdict(list)
        for e,r in enumerate(rolls): by_near[r['start']].append(e)
        for g,s in enumerate(sources):
            self.nodes.append((g,-1))
            queue=deque([-1]); visited=set()
            while queue:
                prev=queue.popleft()
                if prev in visited: continue
                visited.add(prev)
                at = s['end'] if prev==-1 else rolls[prev]['end']
                executed = s['time_max'] if prev==-1 else rolls[prev]['time_max']
                for e in by_near[at]:
                    if executed>rolls[e]['time_min']: continue
                    self.add(g,prev,e)
                    if e not in visited: queue.append(e)
            reachable=sorted(visited-{-1})
            self.reachable[g]=reachable
            for e in reachable: self.nodes.append((g,e))
            for node in [-1]+reachable:
                idx=self.add(g,node,None)
                if node==-1:self.source_exit[g]=idx
        n=len(self.variables)
        eq=lil_matrix((len(self.nodes),n)); b=[]
        for row,(g,node) in enumerate(self.nodes):
            for idx,coef in self.balance[(g,node)]:eq[row,idx]=coef
            b.append(sources[g]['amount'] if node==-1 else 0.)
        cap=lil_matrix((len(rolls),n))
        for (g,e),indices in self.incoming.items():
            for idx in indices:cap[e,idx]=1.
        self.Aeq,self.beq=eq.tocsr(),np.array(b)
        self.Acap,self.bcap=cap.tocsr(),np.array([r['amount'] for r in rolls])

    def add(self,g,prev,dest):
        idx=len(self.variables);self.variables.append((g,prev,dest))
        self.balance[(g,prev)].append((idx,1.))
        if dest is not None:
            self.balance[(g,dest)].append((idx,-1.))
            self.incoming[(g,dest)].append(idx)
        return idx

    def solve(self,c,extra_eq=None,extra_b=None,extra_ub=None,extra_u=None):
        from scipy.sparse import vstack
        ae=self.Aeq if extra_eq is None else vstack([self.Aeq,csr_matrix(extra_eq)])
        be=self.beq if extra_b is None else np.r_[self.beq,extra_b]
        au=self.Acap if extra_ub is None else vstack([self.Acap,csr_matrix(extra_ub)])
        bu=self.bcap if extra_u is None else np.r_[self.bcap,extra_u]
        res=linprog(c,A_ub=au,b_ub=bu,A_eq=ae,b_eq=be,bounds=(0,None),method='highs')
        if not res.success:raise RuntimeError(res.message)
        return res

    def attribution_vector(self,g,e):
        c=np.zeros(len(self.variables)); c[self.incoming[(g,e)]]=1.
        return c

    def representative(self):
        # Explicit illustrative policy, NOT calibrated evidence or provenance:
        # 1. Maximise unique source principal entering its first roll.
        # 2. Subject to 1, maximise days until that principal exits tracking.
        # The second objective depends on terminal date, not number of roll hops.
        first=np.zeros(len(self.variables))
        first[list(self.source_exit.values())]=1.
        r1=self.solve(first)
        terminal=np.zeros(len(first))
        for idx,(g,prev,dest) in enumerate(self.variables):
            if dest is None and prev!=-1:
                terminal[idx]=-(self.rolls[prev]['end']-self.sources[g]['end']).days
        r2=self.solve(terminal,extra_eq=[first],extra_b=[r1.fun])
        self.policy_eq,self.policy_b=first,r1.fun
        self.policy_cost,self.policy_opt=terminal,r2.fun
        return r2.x

    def bounds(self,g,e,policy=False):
        c=self.attribution_vector(g,e)
        kw={}
        if policy:
            kw=dict(extra_eq=[self.policy_eq],extra_b=[self.policy_b],
                    extra_ub=[self.policy_cost],extra_u=[self.policy_opt+TOL])
        return max(0.,self.solve(c,**kw).fun),max(0.,-self.solve(-c,**kw).fun)

    def validate(self,x):
        assert np.min(x)>=-TOL
        assert np.max(np.abs(self.Aeq@x-self.beq))<TOL
        assert np.all(self.Acap@x<=self.bcap+TOL)

def write_csv(path,rows,columns):
    with open(path,'w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=columns);w.writeheader();w.writerows(rows)

def sign(g):
    return 1 if g['direction']=='BUY' else -1

def external(g):
    return dict(group_id=g['group_id'],client_id=g['client_id'],pair=g['pair'],
                value_date=g['start'].isoformat(),roll_trade_id=g.get('roll_trade_id',''),
                start_date=g['start'].isoformat(),
                end_date=g['end'].isoformat(),base_amount=round(sign(g)*g['amount']*UNIT,6),
                trade_ids=';'.join(g['ids']),
                execution_min=g['time_min'].isoformat(),execution_max=g['time_max'].isoformat())

def plot_network(client,sources,rolls,results,out):
    # One panel per currency/direction; quantities never pooled across currencies.
    keys=sorted({(r['pair'],r['direction']) for r in sources+rolls if r['client_id']==client})
    fig,axes=plt.subplots(len(keys),1,figsize=(12,4.4*len(keys)),squeeze=False)
    for ax,(pair,direction) in zip(axes[:,0],keys):
        ss=[s for s in sources if s['client_id']==client and (s['pair'],s['direction'])==(pair,direction)]
        rr=[r for r in rolls if r['client_id']==client and (r['pair'],r['direction'])==(pair,direction)]
        dates=sorted({d for r in ss+rr for d in (r['start'],r['end'])})
        pos={d:i for i,d in enumerate(dates)}
        ax.scatter(list(pos.values()),[0]*len(pos),s=220,color='#244b68',zorder=5)
        for d,i in pos.items():ax.text(i,-.16,d.strftime('%d %b'),ha='center',fontsize=10)
        for s in ss:
            x=pos[s['end']]
            ax.annotate(f"{s['group_id']}: spot {sign(s)*s['amount']:g}m",xy=(x,0),xytext=(x,-.68),
                        ha='center',arrowprops=dict(arrowstyle='->',color='#244b68'))
        selected={r['roll_group_id']:0. for r in results}
        for r in results:selected[r['roll_group_id']]+=abs(r['allocated_amount'])/UNIT
        for i,r in enumerate(rr):
            a,b=pos[r['start']],pos[r['end']]
            lane = sum(1 for p in rr[:i] if (p['start'],p['end'])==(r['start'],r['end']))
            rad=-.3-.45*lane
            x=selected.get(r['group_id'],0.)
            edge=FancyArrowPatch((a,0),(b,0),connectionstyle=f'arc3,rad={rad}',
                 arrowstyle='-|>',mutation_scale=15,linewidth=1.8,color='#177e89' if x>TOL else '#b2b7bc',
                 shrinkA=10,shrinkB=10)
            ax.add_patch(edge)
            ax.text((a+b)/2,.14+.18*(b-a)+.38*lane,f"{r['roll_trade_id']}: {sign(r)*x:g}/{sign(r)*r['amount']:g}m",
                    ha='center',fontsize=9,bbox=dict(facecolor='white',edgecolor='none',alpha=.9))
        ax.set(xlim=(-.55,max(pos.values(),default=0)+.55),ylim=(-.95,1.3))
        ax.set_title(f'{client} | {pair} {"LONG" if direction=="BUY" else "SHORT"} | signed allocated / observed principal',loc='left')
        ax.axis('off')
    fig.suptitle('Illustrative allocation under the coverage policy; not verified provenance',fontsize=12)
    fig.tight_layout(rect=(0,0,1,.95));fig.savefig(out/f'flow_{client}.png',dpi=160);plt.close(fig)

def plot_ranges(results,out):
    if not results:return
    fig,ax=plt.subplots(figsize=(12,max(3.5,len(results)*.55+1.8)))
    for i,r in enumerate(results):
        ax.plot([r['lower_bound']/UNIT,r['upper_bound']/UNIT],[i,i],color='#b8c8d3',lw=9,solid_capstyle='round')
        ax.plot([r['policy_lower']/UNIT,r['policy_upper']/UNIT],[i,i],color='#177e89',lw=4)
        ax.scatter(r['allocated_amount']/UNIT,i,color='#c45518',s=38,zorder=3)
    ax.set_yticks(range(len(results)),[f"{r['client_id']} {r['pair']} {r['value_date']} → {r['roll_trade_id']}" for r in results])
    ax.invert_yaxis();ax.set_xlabel('Millions of base currency per row (do not sum different currencies)')
    ax.set_title('Light: feasible range | teal: policy-optimal range | orange: selected allocation',loc='left',fontsize=11)
    ax.grid(axis='x',alpha=.2);fig.tight_layout();fig.savefig(out/'allocation_ranges.png',dpi=160);plt.close(fig)

def run(spot_file,roll_file,out):
    out=Path(out);out.mkdir(parents=True,exist_ok=True)
    spots_raw,rolls_raw=load_csv(spot_file,'spot'),load_csv(roll_file,'roll')
    orientations={r['pair'] for r in spots_raw+rolls_raw}
    if any(p[3:]+p[:3] in orientations for p in orientations):
        raise ValueError('Normalise mixed pair orientations upstream')
    sources,rolls=aggregate(spots_raw,'spot'),aggregate(rolls_raw,'roll')
    results=[]; residuals=[];source_summary=[];transfers=[]
    keys=sorted({partition_key(r) for r in sources+rolls})
    for key in keys:
        ss=[s for s in sources if partition_key(s)==key]
        rr=[r for r in rolls if partition_key(r)==key]
        used=np.zeros(len(rr))
        if ss:
            m=FlowModel(ss,rr);x=m.representative();m.validate(x)
            for idx,(g,prev,dest) in enumerate(m.variables):
                if x[idx]>TOL:
                    transfers.append(dict(source_group_id=ss[g]['group_id'],client_id=key[0],pair=key[1],
                        value_date=ss[g]['end'].isoformat(),
                        roll_trade_id='' if dest is None else rr[dest]['roll_trade_id'],
                        from_node=ss[g]['group_id'] if prev==-1 else rr[prev]['group_id'],
                        to_node='EXIT' if dest is None else rr[dest]['group_id'],
                        amount=round(sign(ss[g])*x[idx]*UNIT,6)))
            for g,s in enumerate(ss):
                unrolled=x[m.source_exit[g]]
                source_summary.append(dict(source_group_id=s['group_id'],client_id=key[0],pair=key[1],
                    value_date=s['end'].isoformat(),
                    base_amount=sign(s)*s['amount']*UNIT,initially_rolled_amount=round(sign(s)*(s['amount']-unrolled)*UNIT,6),
                    initially_unrolled_amount=round(sign(s)*unrolled*UNIT,6),spot_ids=';'.join(s['ids'])))
                for e in m.reachable[g]:
                    c=m.attribution_vector(g,e);amount=float(c@x);used[e]+=amount
                    lo,hi=m.bounds(g,e);plo,phi=m.bounds(g,e,policy=True)
                    multiplier=sign(s)
                    signed_lo,signed_hi=sorted([multiplier*lo,multiplier*hi])
                    signed_plo,signed_phi=sorted([multiplier*plo,multiplier*phi])
                    results.append(dict(source_group_id=s['group_id'],roll_group_id=rr[e]['group_id'],
                        client_id=key[0],pair=key[1],value_date=s['end'].isoformat(),
                        roll_trade_id=rr[e]['roll_trade_id'],
                        roll_start_date=rr[e]['start'].isoformat(),roll_end_date=rr[e]['end'].isoformat(),
                        allocated_amount=round(multiplier*amount*UNIT,6),lower_bound=round(signed_lo*UNIT,6),
                        upper_bound=round(signed_hi*UNIT,6),policy_lower=round(signed_plo*UNIT,6),
                        policy_upper=round(signed_phi*UNIT,6),ambiguity='YES' if hi-lo>TOL else 'NO',
                        policy_ambiguity='YES' if phi-plo>10*TOL else 'NO',
                        spot_ids=';'.join(s['ids']),roll_ids=';'.join(rr[e]['ids'])))
        for e,r in enumerate(rr):
            residuals.append(dict(roll_group_id=r['group_id'],client_id=key[0],pair=key[1],
                value_date=r['start'].isoformat(),roll_trade_id=r['roll_trade_id'],
                base_amount=sign(r)*r['amount']*UNIT,allocated_amount=round(sign(r)*used[e]*UNIT,6),
                unattributed_amount=round(sign(r)*max(0,r['amount']-used[e])*UNIT,6),roll_ids=';'.join(r['ids'])))
    cols=['client_id','pair','value_date','roll_trade_id','source_group_id','roll_group_id',
          'roll_start_date','roll_end_date','allocated_amount',
          'lower_bound','upper_bound','policy_lower','policy_upper','ambiguity','policy_ambiguity','spot_ids','roll_ids']
    write_csv(out/'allocations.csv',results,cols)
    write_csv(out/'roll_residuals.csv',residuals,['client_id','pair','value_date','roll_trade_id','roll_group_id','base_amount','allocated_amount','unattributed_amount','roll_ids'])
    write_csv(out/'matching_network.csv',residuals,['client_id','pair','value_date','roll_trade_id','roll_group_id','base_amount','allocated_amount','unattributed_amount','roll_ids'])
    write_csv(out/'source_summary.csv',source_summary,['source_group_id','client_id','pair','value_date','base_amount','initially_rolled_amount','initially_unrolled_amount','spot_ids'])
    write_csv(out/'transfers.csv',transfers,['client_id','pair','value_date','roll_trade_id','source_group_id','from_node','to_node','amount'])
    groupcols=['group_id','client_id','pair','value_date','roll_trade_id','start_date','end_date','base_amount','trade_ids','execution_min','execution_max']
    write_csv(out/'spot_groups.csv',[external(g) for g in sources],groupcols)
    write_csv(out/'roll_groups.csv',[external(g) for g in rolls],groupcols)
    write_csv(out/'roll_aggregation.csv',[external(g) for g in rolls],groupcols)
    plot_ranges(results,out)
    eligible_spot_ids={ident for r in results for ident in r['spot_ids'].split(';')}
    eligible_roll_groups={r['roll_group_id'] for r in results}
    coverage=dict(spot_tickets=len(spots_raw),eligible_spot_tickets=len(eligible_spot_ids),
                  eligible_spot_percent=100*len(eligible_spot_ids)/len(spots_raw) if spots_raw else 0,
                  roll_groups=len(rolls),eligible_roll_groups=len(eligible_roll_groups))
    (out/'match_coverage.json').write_text(json.dumps(coverage,indent=2))
    fig,axs=plt.subplots(1,2,figsize=(10,4))
    for ax,title,eligible,total in [(axs[0],'Spot tickets',len(eligible_spot_ids),len(spots_raw)),
                                  (axs[1],'Aggregated roll trades',len(eligible_roll_groups),len(rolls))]:
        ax.bar(['Eligible','No candidate'],[eligible,total-eligible],color=['#177e89','#b8c8d3'])
        for j,v in enumerate([eligible,total-eligible]):ax.text(j,v+max(total*.02,.2),str(v),ha='center')
        ax.set_ylim(0,max(1,total*1.15));ax.set_title(title);ax.set_ylabel('Count')
    fig.suptitle('Candidate coverage under date, client, pair, sign and execution rules')
    fig.tight_layout();fig.savefig(out/'match_coverage.png',dpi=160);plt.close(fig)
    # Keep sample reports manageable for large customer universes.
    for client in sorted({r['client_id'] for r in results})[:10]:
        plot_network(client,sources,rolls,results,out)
    meta=dict(policy='max_initial_coverage_then_latest_exit; illustrative, not inferred truth',
              raw_spots=len(spots_raw),raw_rolls=len(rolls_raw),source_groups=len(sources),
              roll_groups=len(rolls),candidate_source_roll_links=len(results),
              chronology='conservative grouped execution: previous max <= next min',
              amount_convention='signed base currency; positive LONG, negative SHORT; roll sign refers to far leg',
              note='Bounds are independent. Ground truth is never loaded. No calibrated probabilities.')
    (out/'run_info.json').write_text(json.dumps(meta,indent=2))
    print(json.dumps(meta,indent=2))
    print(json.dumps(coverage,indent=2))
    return results,residuals,source_summary

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    here=Path(__file__).resolve().parent
    p.add_argument('--spot',default=str(here/'data'/'spot_trades.csv'))
    p.add_argument('--roll',default=str(here/'data'/'roll_trades.csv'))
    p.add_argument('--out',default=str(here/'results'))
    a=p.parse_args();run(a.spot,a.roll,a.out)
