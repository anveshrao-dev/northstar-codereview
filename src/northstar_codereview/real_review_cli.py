"""Explicit real-review operator actions; no dotenv reads or implicit inference."""
import asyncio
import json
from uuid import UUID

from . import real_review as service
from .execution_profiles import PROFILE_NAMES
from .real_source import renew_import
from .real_source_contracts import SampleId, IntakeRejected, FailureCategory
from .real_source_cli import _Parser, _Once, _InvalidArguments


def main(argv=None):
    parser = _Parser(prog='review_real_source.py', allow_abbrev=False)
    sub = parser.add_subparsers(dest='operation', required=True)
    for operation in ('catalog', 'renew', 'authorize', 'run', 'show', 'export'):
        child = sub.add_parser(operation, allow_abbrev=False)
        child.add_argument('--sample', choices=[s.value for s in SampleId], required=True, action=_Once)
        if operation != 'catalog':
            child.add_argument('--import-id', type=UUID, required=True, action=_Once)
        if operation in ('authorize', 'run'):
            child.add_argument('--execution-profile',choices=PROFILE_NAMES,action=_Once)
        if operation in ('renew', 'authorize'):
            child.add_argument('--manifest-digest', action=_Once)
        if operation == 'renew':
            child.add_argument('--days', type=int, default=30)
            child.add_argument('--allow-renewal', action='store_true')
        if operation == 'authorize':
            child.add_argument('--allow-transmission', action='store_true')
        if operation == 'run':
            child.add_argument('--transmission-id', type=UUID, action=_Once)
            child.add_argument('--allow-live', action='store_true')
            child.add_argument('--run-id', type=UUID, action=_Once)
        if operation in ('show', 'export'):
            child.add_argument('--run-id', type=UUID, required=True, action=_Once)
    try:
        args = parser.parse_args(argv)
        if args.operation == 'catalog':
            output = {'snapshots': [s.model_dump(mode='json') for s in service.snapshot_catalog(args.sample)]}
        elif args.operation == 'run':
            result = asyncio.run(service.run_review(args.sample, args.import_id, transmission_id=args.transmission_id,
                allow_live=args.allow_live, run_id=args.run_id, execution_profile=args.execution_profile or 'standard'))
            print(result.model_dump_json())
            return 0 if result.persisted and result.status in ('completed', 'partial') else 2
        elif args.operation == 'authorize':
            permit = service.authorize_transmission(args.sample, args.import_id,
                expected_digest=args.manifest_digest, allow_transmission=args.allow_transmission, execution_profile=args.execution_profile or 'standard')
            output = {'execution_profile':permit.execution_profile.model_dump(mode='json'), 'status': 'authorized', 'transmission_id': str(permit.approval_id),
                      'specialists': [name.value for name in permit.specialists],
                      'expires_at': permit.expires_at.isoformat(), 'inference_started': False}
        elif args.operation == 'renew':
            if not args.allow_renewal:
                raise IntakeRejected(FailureCategory.AUTHORIZATION)
            choices = service.snapshot_catalog(args.sample)
            if not any(c.import_id == args.import_id and c.manifest_digest == args.manifest_digest for c in choices):
                raise IntakeRejected(FailureCategory.SCOPE)
            result = renew_import(args.import_id, expected_digest=args.manifest_digest,
                                  allow_renewal=True, days=args.days)
            output = {**result.model_dump(mode='json'), 'transmission_authorized': False}
        else:
            result = service.load_review(args.import_id, args.run_id)
            if result.sample.value != args.sample:
                raise IntakeRejected(FailureCategory.SCOPE)
            if args.operation == 'export':
                print(service.export_review(args.import_id, args.run_id))
                return 0
            output = result.model_dump(mode='json')
        print(json.dumps(output, sort_keys=True))
        return 0
    except _InvalidArguments:
        category = 'invalid_arguments'
    except IntakeRejected as error:
        category = error.category.value
    except Exception:
        category = 'operation_failed'
    print(json.dumps({'status': 'rejected', 'category': category}))
    return 2
