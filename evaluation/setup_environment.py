"""기존 .venv를 수정하지 않고 독립 평가 환경을 준비한다."""
import subprocess
import sys
from pathlib import Path


def main():
    repository = Path(__file__).resolve().parents[1]
    generation = repository / '.venv'
    executable = generation / 'bin' / 'python'
    if not executable.is_file():
        raise SystemExit('기존 generation .venv를 먼저 준비하세요.')
    target = repository / '.venv-evaluation'
    subprocess.run([str(executable), '-m', 'venv', str(target)], check=True)
    destination = next((target / 'lib').glob('python*/site-packages'))
    source = next((generation / 'lib').glob('python*/site-packages'))
    (destination / 'generation_environment.pth').write_text(str(source.resolve()) + '\n')
    subprocess.run([str(target / 'bin' / 'python'), '-m', 'pip', 'install',
                    '-r', str(repository / 'requirements-evaluation.txt')], check=True)
    subprocess.run([str(target / 'bin' / 'python'), '-m', 'pip', 'check'], check=True)


if __name__ == '__main__':
    main()
