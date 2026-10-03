"""Reproducible physical-line counts; excludes dependencies, data and docs."""
from pathlib import Path
import json

IGNORED={'.git','.runtime','.venv','venv','node_modules','__pycache__','build','dist','work','.pytest_cache'}
def count(root):
    files=sorted(p for p in root.rglob('*') if p.is_file() and p.suffix not in {'.pyc','.pyo'}
                 and not any(part in IGNORED or part.endswith('.egg-info') for part in p.relative_to(root).parts))
    production=[p for p in files if p.relative_to(root).parts[0]=='robot_voice_patrol' and p.suffix in {'.py','.js','.html','.css'}]
    testing=[p for p in files if p.relative_to(root).parts[0] in {'tests','tests_ros','evals'} and p.suffix in {'.py','.cjs'}]
    integration=[p for p in files if p not in production+testing and p.suffix in {'.py','.sh','.ps1','.cmd','.action','.msg','.srv'}]
    def measure(paths):
        lines=[line for p in paths for line in p.read_text(encoding='utf-8-sig').splitlines()]
        return {'files':len(paths),'lines':len(lines),'nonblank_lines':sum(bool(line.strip()) for line in lines)}
    return {'production':measure(production),'tests_and_evaluators':measure(testing),
            'integration_and_launch':measure(integration),'all_delivered_files':len(files)}
if __name__=='__main__':
    print(json.dumps(count(Path(__file__).resolve().parents[1]),indent=2))
