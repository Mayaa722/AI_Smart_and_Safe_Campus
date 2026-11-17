"""Summarize genuine 1–5 participant feedback; empty cells are missing, never zero."""
import argparse
import csv
import json
import statistics
from pathlib import Path

HERE = Path(__file__).resolve().parent
DIMENSIONS = ['ease_starting_conversation', 'response_relevance_accuracy',
              'perceived_resistance_to_hallucination', 'overall_recommendation']


def summarize(path):
    with Path(path).open(encoding='utf-8-sig', newline='') as stream:
        rows = list(csv.DictReader(stream))
    identifiers = set()
    values = {name: [] for name in DIMENSIONS}
    for row in rows:
        pid = row.get('participant_id', '').strip()
        if not pid or pid in identifiers:
            raise ValueError('Every row needs a unique anonymous participant_id.')
        identifiers.add(pid)
        for name in DIMENSIONS:
            raw = row.get(name, '').strip()
            if not raw:
                continue
            if raw not in {'1', '2', '3', '4', '5'}:
                raise ValueError(f'{pid}: {name} must be an integer 1–5 or blank.')
            values[name].append(int(raw))
    return {'participant_count': len(rows), 'status': 'completed' if rows else 'not_collected',
        'instrument': 'Project questionnaire based on paper-reported dimensions; original wording unavailable',
        'dimensions': {name: {'n': len(v), 'missing': len(rows) - len(v),
            'mean': statistics.mean(v) if v else None,
            'median': statistics.median(v) if v else None,
            'sample_sd': statistics.stdev(v) if len(v) > 1 else None,
            'distribution': {str(i): v.count(i) for i in range(1, 6)}} for name, v in values.items()},
        'roles': {role: sum(r.get('role', '').strip() == role for r in rows)
                  for role in sorted({r.get('role', '').strip() for r in rows})},
        'qualitative': [{'participant_id': r['participant_id'],
            'most_useful_features': r.get('most_useful_features', ''), 'comments': r.get('comments', '')} for r in rows],
        'limitations': ['Self-reported perceptions are not objective factual accuracy or measured learning gains.',
                       'Do not copy the paper participant count or scores into project results.']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('csv_path', nargs='?', type=Path, default=HERE / 'participant_responses.csv')
    args = parser.parse_args()
    result = summarize(args.csv_path)
    output = HERE / 'results/survey_summary.json'
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(f"Participants: {result['participant_count']}; status: {result['status']}; report: {output}")
