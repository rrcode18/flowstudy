import unittest
from datetime import date, datetime, timezone
import numpy as np
from pathlib import Path
from tempfile import TemporaryDirectory
from fx_matcher import FlowModel, load_csv, aggregate, write_csv, partition_key

def node(amount, start, end=None, hour=9):
    return dict(amount=float(amount),start=date(2026,10,start),end=date(2026,10,end or start),
                time_min=datetime(2026,10,1,hour,tzinfo=timezone.utc),
                time_max=datetime(2026,10,1,hour,tzinfo=timezone.utc))

class FlowTests(unittest.TestCase):
    def test_sparse_fixture_has_exactly_five_percent_eligible_spots(self):
        from generate_sparse_sample import generate
        with TemporaryDirectory() as d:
            data=generate(d)
            spots=load_csv(data/'spot_trades.csv','spot')
            rolls=load_csv(data/'roll_trades.csv','roll')
            ss,rr=aggregate(spots,'spot'),aggregate(rolls,'roll')
            eligible_ids=set();eligible_rolls=set()
            for key in {partition_key(s) for s in ss}:
                sources=[s for s in ss if partition_key(s)==key]
                targets=[r for r in rr if partition_key(r)==key]
                m=FlowModel(sources,targets)
                for g,reachable in m.reachable.items():
                    if reachable:eligible_ids.update(sources[g]['ids'])
                    eligible_rolls.update(targets[e]['roll_trade_id'] for e in reachable)
            self.assertEqual(len(spots),200)
            self.assertEqual(len(rolls),60)
            self.assertEqual(len(rr),30)
            self.assertEqual(len(eligible_ids),10)
            self.assertEqual(eligible_rolls,{'RT001','RT002'})

    def test_two_sources_share_one_capacity(self):
        m=FlowModel([node(6,5),node(6,5)],[node(8,5,12,hour=10)])
        x=m.representative();m.validate(x)
        total=sum(m.attribution_vector(g,0)@x for g in range(2))
        self.assertAlmostEqual(total,8)
        for g in range(2):
            lo,hi=m.bounds(g,0,policy=True)
            self.assertAlmostEqual(lo,2,places=5)
            self.assertAlmostEqual(hi,6,places=5)

    def test_continuation_cannot_create_principal(self):
        m=FlowModel([node(20,5)],[node(15,5,12,hour=10),node(30,12,19,hour=11)])
        x=m.representative();m.validate(x)
        self.assertAlmostEqual(m.attribution_vector(0,1)@x,15)
        self.assertAlmostEqual(m.bounds(0,1)[1],15)

    def test_impossible_execution_order_is_excluded(self):
        m=FlowModel([node(10,5,hour=11)],[node(10,5,12,hour=10)])
        self.assertEqual(m.reachable[0],[])
        x=m.representative();m.validate(x)
        self.assertAlmostEqual(x[m.source_exit[0]],10)

    def test_competing_branches_and_policy(self):
        m=FlowModel([node(20,5)],[node(17,5,12,hour=10),node(10,12,19,hour=11),node(8,5,19,hour=10)])
        x=m.representative();m.validate(x)
        np.testing.assert_allclose([m.attribution_vector(0,e)@x for e in range(3)],[12,10,8])
        self.assertEqual(m.bounds(0,0),(0.,17.))

    def test_no_rolls_and_empty_sources(self):
        m=FlowModel([node(4,6)],[])
        x=m.representative();m.validate(x)
        self.assertAlmostEqual(x[m.source_exit[0]],4)

    def test_signed_amount_derives_internal_direction(self):
        with TemporaryDirectory() as d:
            p=Path(d)/'spot.csv'
            row=dict(spot_id='S1',client_id='C1',pair='GBPUSD',
                trade_time_utc='2026-10-01T09:00:00Z',value_date='2026-10-05',base_amount=-3000000)
            write_csv(p,[row],list(row))
            result=load_csv(p,'spot')[0]
            self.assertEqual(result['direction'],'SELL')
            self.assertEqual(result['amount'],3.)

    def test_parent_roll_ids_prevent_cross_trade_aggregation(self):
        with TemporaryDirectory() as d:
            p=Path(d)/'roll.csv';rows=[]
            for i,parent in enumerate(['RT1','RT1','RT2']):
                rows.append(dict(roll_id=f'R{i}',roll_trade_id=parent,client_id='C1',pair='EURUSD',
                    trade_time_utc='2026-10-02T09:00:00Z',start_date='2026-10-05',
                    end_date='2026-10-12',base_amount=1000000))
            write_csv(p,rows,list(rows[0]))
            groups=aggregate(load_csv(p,'roll'),'roll')
            self.assertEqual(len(groups),2)
            self.assertEqual([(g['roll_trade_id'],g['amount']) for g in groups],[('RT1',2.),('RT2',1.)])

    def test_same_parent_id_does_not_net_opposite_signed_amounts(self):
        with TemporaryDirectory() as d:
            p=Path(d)/'roll.csv';rows=[]
            for i,amount in enumerate([1000000,-2000000]):
                rows.append(dict(roll_id=f'R{i}',roll_trade_id='RT1',client_id='C1',pair='EURUSD',
                    trade_time_utc='2026-10-02T09:00:00Z',start_date='2026-10-05',
                    end_date='2026-10-12',base_amount=amount))
            write_csv(p,rows,list(rows[0]))
            groups=aggregate(load_csv(p,'roll'),'roll')
            self.assertEqual(len(groups),2)
            self.assertEqual({g['direction'] for g in groups},{'BUY','SELL'})

if __name__=='__main__': unittest.main()
