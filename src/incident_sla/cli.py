DESCRIPTION='Elapsed-time SLA accounting, shared-responder work planning and outage credit settlement'
import argparse, json, sys
from pathlib import Path
from .common import read, save
from .model import analyze
from .plan import plan
from .reconcile import run as reconcile
from . import credit

def run(source):
    data,digest=read(source)
    return {'schema_version':1,'source_sha256':digest,'result':analyze(data)}

def run_plan(source):
    data,digest=read(source)
    return {'schema_version':2,'source_sha256':digest,'result':plan(data)}

def run_reconcile(source):
    data,digest=read(source)
    return {'schema_version':4,'source_sha256':digest,'result':reconcile(data)}

def main():
    parser=argparse.ArgumentParser(description=DESCRIPTION)
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--source',type=Path,help='v1 elapsed-time accounting input')
    mode.add_argument('--plan-source',type=Path,help='v2 shared-responder plan input')
    mode.add_argument('--reconcile-source',type=Path,metavar='FILE',help='v4 cross-system telemetry alignment input')
    mode.add_argument('--credit-init',type=Path,metavar='CONFIG',help='v3 credit ledger init config')
    mode.add_argument('--credit-apply',type=Path,metavar='BATCH',help='v3 credit batch input')
    mode.add_argument('--credit-view',type=int,metavar='SEQ',help='v3 credit audit view sequence')
    parser.add_argument('--output',type=Path)
    parser.add_argument('--db',type=Path,help='v3 SQLite ledger path')
    args=parser.parse_args()
    try:
        credit_mode=args.credit_init is not None or args.credit_apply is not None or args.credit_view is not None
        if not credit_mode and args.db is not None:raise ValueError('--db is only valid with credit modes')
        if args.source is not None or args.plan_source is not None or args.reconcile_source is not None:
            if args.output is None:raise ValueError('--output is required')
            if args.source is not None:source=args.source
            elif args.plan_source is not None:source=args.plan_source
            else:source=args.reconcile_source
            if source.resolve()==args.output.resolve():raise ValueError('input and output must differ')
            if args.source is not None:
                report=run(args.source)
            elif args.plan_source is not None:
                report=run_plan(args.plan_source)
            else:
                report=run_reconcile(args.reconcile_source)
            save(args.output,report)
        elif args.credit_init is not None:
            if args.output is not None:raise ValueError('--credit-init does not take --output')
            if args.db is None:raise ValueError('--credit-init requires --db')
            report=credit.init_db(args.credit_init,args.db)
        elif args.credit_apply is not None:
            if args.db is None:raise ValueError('--credit-apply requires --db')
            if args.output is None:raise ValueError('--credit-apply requires --output')
            report=credit.apply_batch(args.credit_apply,args.db,args.output)
        else:
            if args.db is None:raise ValueError('--credit-view requires --db')
            if args.output is None:raise ValueError('--credit-view requires --output')
            report=credit.view_report(args.credit_view,args.db,args.output)
    except (ValueError,TypeError,KeyError,OSError,OverflowError,RecursionError) as error:
        print(str(error),file=sys.stderr);return 2
    print(json.dumps(report,sort_keys=True,ensure_ascii=False,allow_nan=False));return 0

if __name__=='__main__':raise SystemExit(main())
