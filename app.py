from pathlib import Path

from agents import InvestmentAnalysisService, ServiceConfig
from evaluation.runtime import request_boundary


NOTEBOOK_PATH = Path(__file__).resolve().parent / "notebooks" / "investment_report_service.ipynb"


@request_boundary
def main() -> None:
    base_dir = InvestmentAnalysisService.resolve_base_dir(Path(__file__).resolve().parent)
    service = InvestmentAnalysisService(base_dir=base_dir, config=ServiceConfig())
    result = service.run()
    print("Notebook path:")
    print(NOTEBOOK_PATH)
    print("Policy decision:")
    print(result.policy_decision)
    print("Report path:")
    print(result.output_path)


if __name__ == "__main__":
    main()
