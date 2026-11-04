"""Inventory TD's untouched timing edges; this does not waive constraints."""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re


def inventory(path):
    names, untouched, total = {}, [], 0
    source = target = None
    with path.open(encoding='utf-8') as stream:
        for line in stream:
            match = re.match(r'^(\d+)\t(.+)', line)
            if match:
                source = int(match[1])
                names[source] = match[2]
            match = re.match(r'\t->(\d+), (.+)', line)
            if match:
                target = int(match[1])
                names[target] = match[2]
            if 'edge type:' in line:
                total += 1
                if 'touched: no' in line:
                    untouched.append((source, target, line.strip()))
    if not total:
        raise ValueError('Empty TD timing graph')
    incoming = Counter(target for _, target, _ in untouched)
    outgoing = defaultdict(list)
    for source, target, _ in untouched:
        outgoing[source].append(target)
    roots = sorted(set(outgoing) - set(incoming))
    result = dict(total_edges=total, untouched_edges=len(untouched),
                  calculated_coverage=100*(1-len(untouched)/total),
                  roots=[names[node] for node in roots],
                  edges=[dict(source=names[s], target=names[t], metadata=m) for s, t, m in untouched])
    with path.open('rb') as stream:
        result['timing_graph_sha256'] = hashlib.file_digest(stream, 'sha256').hexdigest()
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('graph', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    result = inventory(args.graph)
    args.output.write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({key: value for key, value in result.items() if key != 'edges'}, indent=2))
