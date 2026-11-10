"""Closed offline engine matrix, not a live accuracy benchmark."""
def test_repeatable_prepared_engine_matrix():
    from northstar_codereview.review_engine_evaluation import run_matrix
    first,second=run_matrix(),run_matrix()
    assert len(first)>=8
    stable=lambda rows:[{k:v for k,v in row.items() if k!='elapsed_seconds'} for row in rows]
    assert stable(first)==stable(second)
    rows={row['case']:row for row in first}
    assert rows['known_boundary']['evidence_validated']==1
    assert rows['clean']['evidence_validated']==0
    assert rows['fabricated']['evidence_validated']==0 and rows['fabricated']['status']=='partial'
    assert rows['truncated']['schema_valid'] is False
    assert rows['large']['status']=='partial'
    assert rows['existing_negative_control']['scripted_false_positive_retained']==1
    assert all(row['model_attempts']<=4 and row['tool_attempts']<=6 for row in first)
    assert all(row['input_reserved']<=80000 and row['output_reserved']<=4000 for row in first)
    assert all(row['network_attempts']==0 and row['independent_accuracy_established'] is False for row in first)
    assert 'return' not in str(first)
