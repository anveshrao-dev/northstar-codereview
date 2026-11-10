import importlib
import json
import pytest

from .test_real_review import service, captured
from .test_real_source import local_intake


def cli():
    assert importlib.util.find_spec('northstar_codereview.real_review_cli'), 'guarded CLI missing'
    return importlib.import_module('northstar_codereview.real_review_cli')


def test_catalog_only(service, captured, capsys):
    assert cli().main(['catalog', '--sample', 'portfolio_risk']) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['snapshots'][0]['import_id'] == str(captured.approval_id)


@pytest.mark.parametrize('operation', ['run', 'authorize', 'renew'])
def test_missing_opt_in_no_changes(service, captured, operation, capsys):
    assert cli().main([operation, '--sample', 'portfolio_risk', '--import-id', str(captured.approval_id)]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload['status'] in ('blocked', 'rejected')


@pytest.mark.parametrize('args', [['catalog', '--sample', '../PRIVATE'], ['run', '--source', '/PRIVATE'],
    ['run', '--sample', 'portfolio_risk', '--max-model-requests', '100'], ['run', '--allow-liv']])
def test_cli_rejects_arbitrary_or_abbreviated_options(args, capsys):
    assert cli().main(args) == 2
    text = capsys.readouterr().out
    assert 'PRIVATE' not in text and 'invalid_arguments' in text
