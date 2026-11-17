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
    mode.add_argument('--vault-init',action='store_true',help='v5 vault: create a new evidence vault database')
    mode.add_argument('--vault-import',type=Path,metavar='FILE',help='v5 vault: import an encrypted document batch')
    mode.add_argument('--vault-query',metavar='TENANT',help='v5 vault: export exactly one tenant\'s documents')
    mode.add_argument('--vault-rekey',metavar='KEYID',help='v5 vault: rotate the active key towards KEYID')
    parser.add_argument('--output',type=Path)
    parser.add_argument('--db',type=Path,help='v3 SQLite ledger path / v5 vault database path')
    parser.add_argument('--keyring',type=Path,help='v5 vault keyring file')
    parser.add_argument('--key-id',metavar='KEYID',help='v5 vault init active key id')
    parser.add_argument('--limit',type=int,metavar='N',help='v5 vault rekey migration limit (1..100)')
    args=parser.parse_args()
    try:
        credit_mode=args.credit_init is not None or args.credit_apply is not None or args.credit_view is not None
        vault_mode=args.vault_init or args.vault_import is not None or args.vault_query is not None or args.vault_rekey is not None
        if vault_mode:
            from . import vault
            if args.db is None:raise ValueError('vault modes require --db')
            if args.keyring is None:raise ValueError('vault modes require --keyring')
            if args.vault_init:
                if args.key_id is None:raise ValueError('--vault-init requires --key-id')
                if args.output is not None:raise ValueError('--vault-init does not take --output')
                if args.limit is not None:raise ValueError('--vault-init does not take --limit')
                report=vault.init(args.db,args.keyring,args.key_id)
            elif args.vault_import is not None:
                if args.key_id is not None:raise ValueError('--vault-import does not take --key-id')
                if args.output is not None:raise ValueError('--vault-import does not take --output')
                if args.limit is not None:raise ValueError('--vault-import does not take --limit')
                report=vault.import_documents(args.vault_import,args.db,args.keyring)
            elif args.vault_query is not None:
                if args.output is None:raise ValueError('--vault-query requires --output')
                if args.key_id is not None:raise ValueError('--vault-query does not take --key-id')
                if args.limit is not None:raise ValueError('--vault-query does not take --limit')
                report=vault.query(args.vault_query,args.db,args.keyring,args.output)
            else:
                if args.limit is None:raise ValueError('--vault-rekey requires --limit')
                if args.key_id is not None:raise ValueError('--vault-rekey does not take --key-id')
                if args.output is not None:raise ValueError('--vault-rekey does not take --output')
                report=vault.rekey(args.vault_rekey,args.db,args.keyring,args.limit)
        else:
            if args.keyring is not None or args.key_id is not None or args.limit is not None:
                raise ValueError('--keyring, --key-id and --limit are only valid with vault modes')
            if not credit_mode and args.db is not None:raise ValueError('--db is only valid with credit or vault modes')
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
