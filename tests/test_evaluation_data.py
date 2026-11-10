"""Pinned fictional inputs, separated from scoring labels and model messages."""

import json

import pytest
from pydantic import ValidationError

from northstar_codereview.evaluation_data import load_dataset, load_scenario
from northstar_codereview.evaluation_models import EvaluationDataset
from northstar_codereview.evaluation import EvaluationError, fingerprint
from northstar_codereview.models import AgentName
from northstar_codereview.orchestrator import select_specialists


def test_three_pinned_scenarios_and_all_five_specialists():
    dataset = load_dataset()
    assert len(dataset.scenarios) == 3
    covered = set()
    for scenario in dataset.scenarios:
        snapshot = load_scenario(scenario)
        assert snapshot.manifest == scenario.snapshot
        assert select_specialists(snapshot).selected_specialists == scenario.specialists
        assert scenario.expected and scenario.controls and scenario.assumptions and scenario.completeness_boundary
        assert all('expected' not in p.file_path and 'dataset' not in p.file_path for p in snapshot.manifest.files)
        covered.update(scenario.specialists)
    assert covered == set(AgentName)
    assert fingerprint(dataset) == fingerprint(EvaluationDataset.model_validate_json(dataset.model_dump_json()))


@pytest.mark.parametrize('mutation', ['duplicate_scenario', 'duplicate_label', 'duplicate_control', 'bad_agent', 'bad_range', 'bad_path', 'unknown_field'])
def test_ground_truth_rejects_invalid_contracts(mutation):
    data = json.loads(load_dataset().model_dump_json())
    scenario = data['scenarios'][0]
    if mutation == 'duplicate_scenario':
        data['scenarios'].append(scenario)
    elif mutation == 'duplicate_label':
        scenario['expected'].append(scenario['expected'][0])
    elif mutation == 'duplicate_control':
        scenario['controls'].append(scenario['controls'][0])
    elif mutation == 'bad_agent':
        scenario['expected'][0]['agent_name'] = 'security'
    elif mutation == 'bad_range':
        scenario['expected'][0]['locations'][0]['end_line'] = 1
    elif mutation == 'bad_path':
        scenario['expected'][0]['locations'][0]['file_path'] = '../outside.java'
    else:
        scenario['extra'] = 'not allowed'
    with pytest.raises(ValidationError):
        EvaluationDataset.model_validate(data)


def test_loader_rejects_changed_pin_and_location_outside_source():
    scenario = load_dataset().scenarios[0]
    changed = scenario.snapshot.model_copy(update={'revision': '0' * 64})
    with pytest.raises(EvaluationError, match='snapshot_mismatch'):
        load_scenario(scenario.model_copy(update={'snapshot': changed}))
    location = scenario.expected[0].locations[0].model_copy(update={'end_line': 999})
    label = scenario.expected[0].model_copy(update={'locations': (location,)})
    with pytest.raises(EvaluationError, match='invalid_location'):
        load_scenario(scenario.model_copy(update={'expected': (label,)}))
