"""Load service definitions without optional deployment dependencies during bootstrap.

Only imports are replaced; tests execute the repository's service methods unchanged.
S2 HTTP and S3 DB seams are injected by individual tests.
"""
import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Set
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[2]

def load(path, **seams):
    tree = ast.parse((ROOT / path).read_text(encoding='utf-8-sig'))
    tree.body = [node for node in tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))]
    ns = dict(Any=Any, Dict=Dict, List=List, Optional=Optional, Set=Set,
              logger=Mock(), safe_isoformat=lambda x: str(x), as_bool=lambda x: x is True or x == 'true')
    ns.update(seams)
    exec(compile(ast.fix_missing_locations(tree), str(ROOT / path), 'exec', flags=__import__('__future__').annotations.compiler_flag), ns)
    return SimpleNamespace(**ns)
