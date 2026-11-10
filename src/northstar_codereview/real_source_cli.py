"""Metadata-only preflight and explicitly authorized fixed-scope local import.

No provider, graph, transmission approval, source execution, or environment-file
loading is started here. All failures are rendered without raw input/exception text.
"""

import argparse
import json
from uuid import UUID

from .real_source import import_sample, read_import_approval
from .real_source_contracts import SAMPLES, SampleId, IntakeRejected, FailureCategory


class _InvalidArguments(ValueError):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's normal error text echoes invalid user arguments.
        raise _InvalidArguments()


class _Once(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        if getattr(namespace, self.dest, None) is not None:
            raise _InvalidArguments()
        setattr(namespace, self.dest, values)


def _parser():
    parser = _Parser(prog="import_real_source.py", allow_abbrev=False,
        description="Preflight approval metadata or import one fixed real-source sample. No inference.")
    operations = parser.add_subparsers(dest="operation", required=True)
    for operation in ("preflight", "import"):
        child = operations.add_parser(operation, allow_abbrev=False)
        child.add_argument("--sample", required=True, choices=[s.value for s in SampleId], action=_Once)
        child.add_argument("--approval-id", type=UUID, action=_Once,
                           help="UUID of a human-approved record in the fixed private approvals inbox.")
        if operation == "import":
            child.add_argument("--allow-import", action="store_true",
                               help="Explicit local-copy opt-in; never authorizes model transmission.")
    return parser


def main(argv=None) -> int:
    """Print one sanitized JSON result; return 0 ready/committed or 2 rejected."""
    report = {"status": "rejected", "category": None,
              "authorization_metadata_valid": False, "source_verified": False,
              "transmission_authorized": False, "human_review_authorized": False,
              "publication_authorized": False}
    try:
        args = _parser().parse_args(argv)
        sample = SampleId(args.sample)
        report.update(operation=args.operation, sample=sample.value,
                      files=list(SAMPLES[sample][1]), expected_source_count=len(SAMPLES[sample][1]))
        # Missing opt-in is checked before even opening an approval document.
        if args.approval_id is None or (args.operation == "import" and not args.allow_import):
            raise IntakeRejected(FailureCategory.AUTHORIZATION)
        receipt = read_import_approval(args.approval_id)
        if receipt.manifest.sample != sample:
            raise IntakeRejected(FailureCategory.SCOPE)
        report["authorization_metadata_valid"] = True
        if args.operation == "preflight":
            report["status"] = "ready"
        else:
            result = import_sample(sample, approval=receipt)
            report.update(status=result.status, source_verified=True, approval_id=str(result.approval_id),
                          snapshot_id=str(result.snapshot.snapshot_id), manifest_digest=result.manifest_digest)
    except _InvalidArguments:
        report["category"] = "invalid_arguments"
    except IntakeRejected as error:
        report["category"] = error.category.value
    except Exception:
        # No raw traceback/provider/source payload is an acceptable CLI result.
        # No retry: a commit may have succeeded before an unexpected failure.
        report["category"] = "internal_error"
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] in ("ready", "committed") else 2
