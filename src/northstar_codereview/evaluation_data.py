"""Bounded offline loading of pinned synthetic evaluation data only."""

from .evaluation import EvaluationError
from .evaluation_models import EvaluationDataset, EvaluationScenario
from .local_source import PROJECT_ROOT, SourceIntakeError, collect_local_snapshot
from .models import ReviewRequest
from .orchestrator import select_specialists


SUITE_PATH = 'tests/fixtures/evaluation_suite'


def _capture(directory, files):
    try:
        return collect_local_snapshot(ReviewRequest(mode='local', local_path='.'),
                                      source_root=PROJECT_ROOT / directory, selected_files=files)
    except SourceIntakeError:
        raise EvaluationError('unsafe_dataset') from None


def load_dataset() -> EvaluationDataset:
    metadata = _capture(SUITE_PATH, ('dataset.json',))
    try:
        return EvaluationDataset.model_validate_json(metadata.sources['head', 'dataset.json'])
    except ValueError:
        raise EvaluationError('invalid_dataset') from None


def load_scenario(scenario: EvaluationScenario):
    try:
        scenario = EvaluationScenario.model_validate(scenario.model_dump(mode='json'))
    except ValueError:
        raise EvaluationError('invalid_dataset') from None
    snapshot = _capture(f'{SUITE_PATH}/{scenario.source_directory}',
                        tuple(f.file_path for f in scenario.snapshot.files))
    if snapshot.manifest != scenario.snapshot:
        raise EvaluationError('snapshot_mismatch')
    for label in (*scenario.expected, *scenario.controls):
        for loc in label.locations:
            if loc.end_line > len(snapshot.sources[loc.side, loc.file_path].splitlines()):
                raise EvaluationError('invalid_location')
    if select_specialists(snapshot).selected_specialists != scenario.specialists:
        raise EvaluationError('applicability_mismatch')
    return snapshot
