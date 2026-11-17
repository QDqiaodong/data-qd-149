DESCRIPTION='Elapsed-time SLA accounting and shared-responder work planning'
import argparse, json, sys
from pathlib import Path
from .common import read, save
from .model import analyze
from .plan import plan

def run(source):
    data,digest=read(source)
    return {'schema_version':1,'source_sha256':digest,'result':analyze(data)}

def run_plan(source):
    data,digest=read(source)
    return {'schema_version':2,'source_sha256':digest,'result':plan(data)}

def main():
    parser=argparse.ArgumentParser(description=DESCRIPTION)
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--source',type=Path,help='v1 elapsed-time accounting input')
    mode.add_argument('--plan-source',type=Path,help='v2 shared-responder plan input')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    try:
        source=args.source if args.source is not None else args.plan_source
        if source.resolve()==args.output.resolve():raise ValueError('input and output must differ')
        if args.source is not None:
            report=run(args.source)
        else:
            report=run_plan(args.plan_source)
        save(args.output,report)
    except (ValueError,TypeError,KeyError,OSError,OverflowError,RecursionError) as error:
        print(str(error),file=sys.stderr);return 2
    print(json.dumps(report,sort_keys=True,ensure_ascii=False,allow_nan=False));return 0

if __name__=='__main__':raise SystemExit(main())
