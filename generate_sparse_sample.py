"""Generate a reproducible fixture with 5% eligible spot tickets. No arguments needed."""
import csv
import random
from pathlib import Path

def generate(root=None):
    root=Path(root) if root is not None else Path(__file__).resolve().parent
    out=root/'data';out.mkdir(parents=True,exist_ok=True)
    rng=random.Random(20261020)
    spots=[];rolls=[];truth=[]
    def spot(client,pair,day,amount):
        i=len(spots)+1
        spots.append(dict(spot_id=f'S{i:04d}',client_id=client,pair=pair,
            trade_time_utc=f'2026-10-01T09:{(i//60)%60:02d}:{i%60:02d}Z',
            value_date=f'2026-10-{day:02d}',base_amount=amount))
    # Ten eligible tickets, pooled into two source positions.
    for _ in range(5):spot('C001','EURUSD',5,4000000)
    for _ in range(5):spot('C002','GBPUSD',6,-2000000)
    # Remaining 190 tickets have value dates absent from all roll near dates.
    for _ in range(190):
        client=f'C{rng.randint(1,20):03d}'
        pair=rng.choice(['EURUSD','GBPUSD','USDJPY'])
        spot(client,pair,rng.choice([7,8,9,13]),rng.choice([-1,1])*rng.randint(1,8)*500000)
    for j in range(1,31):
        if j==1:client,pair,near,far,amount='C001','EURUSD',5,12,3000000
        elif j==2:client,pair,near,far,amount='C002','GBPUSD',6,13,-2000000
        else:
            client=f'C{rng.randint(1,20):03d}'
            pair=rng.choice(['EURUSD','GBPUSD','USDJPY'])
            # Avoid accidental candidates for the two target source positions.
            near=6 if client=='C001' else 5 if client=='C002' else rng.choice([5,6])
            far=near+7;amount=rng.choice([-1,1])*rng.randint(1,6)*500000
        for ticket in range(2):
            rid=f'R{len(rolls)+1:04d}'
            rolls.append(dict(roll_id=rid,client_id=client,pair=pair,
                trade_time_utc=f'2026-10-02T10:{j:02d}:{ticket*20:02d}Z',
                start_date=f'2026-10-{near:02d}',end_date=f'2026-10-{far:02d}',
                base_amount=amount,roll_trade_id=f'RT{j:03d}'))
            truth.append(dict(roll_id=rid,roll_trade_id=f'RT{j:03d}',
                source_spot_ids=';'.join(f'S{k:04d}' for k in (range(1,6) if j==1 else range(6,11))) if j<=2 else '',
                spot_origin_amount=amount if j<=2 else 0,other_origin_amount=0 if j<=2 else amount))
    for name,rows in [('spot_trades.csv',spots),('roll_trades.csv',rolls),('ground_truth.csv',truth)]:
        with (out/name).open('w',newline='',encoding='utf-8') as f:
            w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
    print(f'Generated {len(spots)} spot tickets, {len(rolls)} roll tickets / 30 roll trades.')
    return out

if __name__=='__main__':generate()
