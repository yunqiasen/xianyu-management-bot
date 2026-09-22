"""Usage: python -m tools.release init|check. stdout is redacted JSON."""
import argparse
import json
from pathlib import Path
import sys
from .checker import evaluate, invalid_report, load_catalog, load_json, scaffold, _read

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / 'docs/provenance'


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError('arguments')


def main(argv=None):
    try:
        parser = Parser(description='候选版本证据检查；只读，无网络和发布操作')
        parser.add_argument('command', choices=('init','check'))
        parser.add_argument('--repo', type=Path, default=ROOT)
        parser.add_argument('--spec', type=Path, default=ROOT/'docs/specs/XYMB-SPEC-001.md')
        parser.add_argument('--matrix', type=Path, default=ASSETS/'feature-coverage-matrix.json')
        parser.add_argument('--patches', type=Path, default=ASSETS/'local-patch-dispositions.json')
        parser.add_argument('--candidate', choices=('first','enhanced'), default='first')
        parser.add_argument('--manifest', type=Path)
        parser.add_argument('--evidence-root', type=Path)
        parser.add_argument('--gate', choices=('code','trial','acceptance'), default='acceptance')
        args = parser.parse_args(argv)
        catalog = load_catalog(args.spec, args.matrix, args.patches)
        if args.command == 'init':
            result = scaffold(args.repo, catalog, args.candidate)
            code = 0  # Creation of a pending checklist only; not a passing gate.
        else:
            if args.manifest is None or args.evidence_root is None:
                raise ValueError('missing_input')
            manifest = load_json(_read(args.manifest, 4*1024*1024))
            result = evaluate(args.repo, args.evidence_root, manifest, catalog)
            key = {'code':'code_verified','trial':'trial_ready','acceptance':'release_accepted'}[args.gate]
            code = 0 if result[key] else 1
    except Exception:
        result, code = invalid_report(), 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return code


if __name__ == '__main__':
    sys.exit(main())
