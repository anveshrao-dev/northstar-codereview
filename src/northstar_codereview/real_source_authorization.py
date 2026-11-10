"""Checks at the model-facing boundary; imports never imply transmission."""

from datetime import datetime, timezone
from langchain_core.globals import get_debug, get_verbose
from pydantic import TypeAdapter

from .local_source import CollectedSnapshot, _assemble
from .real_source_privacy import validate_contents
from .real_source_contracts import (ApprovalState, FailureCategory, IntakeRejected,
                                    RealSourceContext, TransmissionApproval, SpecialistScope, checked_import)


def checked_specialists(value):
    try:
        return TypeAdapter(SpecialistScope).validate_python(value)
    except (ValueError, TypeError):
        raise IntakeRejected(FailureCategory.AUTHORIZATION) from None


def ensure_private_execution() -> None:
    # Debug/verbose handlers can print source even with LangSmith disabled.
    # Reject the local setting; never mutate application/global configuration.
    if get_debug() or get_verbose():
        raise IntakeRejected(FailureCategory.AUTHORIZATION)


def verify_real_snapshot(snapshot: CollectedSnapshot):
    try:
        context = RealSourceContext.model_validate(snapshot.real_source.model_dump(warnings=False))
        receipt = checked_import(context.import_approval)
        validate_contents(snapshot.sources)
        if snapshot.manifest != receipt.manifest.snapshot or _assemble(snapshot.sources).manifest != snapshot.manifest:
            raise ValueError("identity")
        return receipt
    except (ValueError, TypeError, AttributeError, KeyError):
        raise IntakeRejected(FailureCategory.AUTHORIZATION) from None


def require_transmission(snapshot: CollectedSnapshot, approval: TransmissionApproval | None,
                         *, identity: tuple[str, str] | None = None, specialists=None, execution_profile=None) -> None:
    ensure_private_execution()
    receipt = verify_real_snapshot(snapshot)
    try:
        permit = TransmissionApproval.model_validate(approval.model_dump(warnings=False))
        if specialists is not None and not set(checked_specialists(specialists)) <= set(permit.specialists):
            raise ValueError("specialist_scope")
        if execution_profile is not None:
            from .execution_profiles import checked_profile, approval_profile
            if checked_profile(execution_profile)!=approval_profile(permit):
                raise ValueError('execution_profile')
        now = datetime.now(timezone.utc)
        if (permit.state != ApprovalState.APPROVED or not permit.approved_at <= now < permit.expires_at
                or permit.expires_at > receipt.manifest.retain_until
                or permit.approved_at < receipt.approved_at
                or permit.import_approval_id != receipt.approval_id
                or permit.manifest_digest != receipt.manifest.digest
                or permit.snapshot != snapshot.manifest
                or (identity is not None and identity != (permit.provider, permit.model))):
            raise ValueError("approval_binding")
    except (ValueError, TypeError, AttributeError):
        raise IntakeRejected(FailureCategory.AUTHORIZATION) from None


def authorize_tools(snapshot: CollectedSnapshot, *, approved_synthetic: bool,
                    transmission_approval: TransmissionApproval | None, specialist=None, invocation=False):
    if snapshot.real_source is not None:
        if approved_synthetic:
            raise IntakeRejected(FailureCategory.AUTHORIZATION)
        require_transmission(snapshot, transmission_approval)
        from .transmission_claims import require_execution, require_tool_execution
        state = require_execution(snapshot, transmission_approval)
        if specialist is None:
            if len(state.claim.requested_specialists) != 1:
                raise IntakeRejected(FailureCategory.AUTHORIZATION)
            specialist = state.claim.requested_specialists[0]
        require_execution(snapshot, transmission_approval, specialists=(specialist,))
        if invocation:
            require_tool_execution(state, specialist)
        return specialist
    elif approved_synthetic is not True or transmission_approval is not None:
        raise ValueError("Model-facing tools require explicit approval of synthetic fixtures")
