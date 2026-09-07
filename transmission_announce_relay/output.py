"""Shared output modes: human (default), agent (deterministic plain text), json."""
import json
import os
import sys
import threading


def resolve_mode(agent=False, as_json=False, environ=None):
    if agent and as_json:
        raise ValueError('conflicting_output_modes')
    if as_json:
        return 'json'
    environment = os.environ if environ is None else environ
    if agent or environment.get('AGENT_SESSION') == '1':
        return 'agent'
    return 'human'


class Output:
    def __init__(self, mode):
        self.mode = mode
        self.lock = threading.Lock()

    def emit(self, payload, error=False):
        with self.lock:
            stream = sys.stderr if error else sys.stdout
            if self.mode == 'json':
                text = json.dumps(payload, sort_keys=True, separators=(',', ':'))
            elif self.mode == 'agent':
                text = '\t'.join('{}={}'.format(key, json.dumps(value, ensure_ascii=True))
                                 for key, value in sorted(payload.items()))
            else:
                text = ' | '.join('{}: {}'.format(key, value) for key, value in sorted(payload.items()))
            print(text, file=stream, flush=True)

    def table(self, rows, columns):
        """Lists: JSON array, tab-separated records, or an aligned human table."""
        if self.mode == 'json':
            print(json.dumps(rows, sort_keys=True, separators=(',', ':')), flush=True)
            return
        if self.mode == 'agent':
            print('\t'.join(columns), flush=True)
            for row in rows:
                print('\t'.join(str(row.get(column, '')) for column in columns), flush=True)
            return
        widths = {column: max([len(column)] + [len(str(row.get(column, ''))) for row in rows])
                  for column in columns}
        print('  '.join(column.ljust(widths[column]) for column in columns), flush=True)
        for row in rows:
            print('  '.join(str(row.get(column, '')).ljust(widths[column]) for column in columns), flush=True)
        if not rows:
            print('(nothing)', flush=True)

    def error(self, code, message=None):
        self.emit({'error': code, 'message': message or code}, error=True)
