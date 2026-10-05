from __future__ import annotations

from pathlib import Path
import json

import typer
from rich.console import Console

from evaluation.recording import RunRecorder

from .config import settings
from .graph import run_pipeline
from .pdf_export import export_markdown_to_pdf
from .report_polish import polish_report_to_korean
from .services import load_companies

app = typer.Typer(add_completion=False)
console = Console()


@app.command()
def main(
    input: Path = typer.Option(settings.default_input_path, exists=True, readable=True),
    output: Path = typer.Option(settings.default_output_path),
    domain: str = typer.Option(settings.domain_name),
    live_research: bool = typer.Option(settings.enable_live_research),
    llm_enrichment: bool = typer.Option(settings.enable_llm_enrichment),
    polish_korean: bool = typer.Option(False),
) -> None:
    recorder = RunRecorder(
        root=Path('evaluation_runs'), execution_path='investment_pipeline',
        inputs={'domain': domain, 'input': input.read_text(encoding='utf-8')},
        settings={'llm_enabled': llm_enrichment, 'live_research_enabled': live_research,
                  'model': settings.openai_model, 'temperature': settings.temperature,
                  'selective_dd_threshold': settings.selective_dd_threshold,
                  'high_priority_threshold': settings.high_priority_threshold,
                  'dense_embedding_model': settings.dense_embedding_model,
                  'hybrid_search_limit': settings.hybrid_search_limit,
                  'polish_korean_option': polish_korean, 'cache_state': 'unknown'},
        requested_formats=['md', 'pdf', 'json'], repository=Path(__file__).resolve().parents[1],
    )
    try:
        with recorder.activate():
            settings.enable_live_research = live_research
            settings.enable_llm_enrichment = llm_enrichment
            companies = load_companies(input)
            recorder.candidates = [company.name for company in companies]
            for company in companies:
                recorder.add_evidence(company.model_dump_json(), source=str(input.resolve()), company=company.name,
                                      title='input_company_profile')
            result = run_pipeline(domain=domain, companies=companies)
            report_markdown = result.report_markdown

            if settings.enable_llm_enrichment:
                polished = polish_report_to_korean(report_markdown)
                if polished:
                    report_markdown = polished
                    result.report_markdown = polished

            with recorder.span('save_outputs', {'output': str(output)}) as span:
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(report_markdown, encoding="utf-8")
                recorder.artifact(output, 'report_original.md', 'md')
                export_markdown_to_pdf(output, output.with_suffix(".pdf"))
                recorder.artifact(output.with_suffix('.pdf'), 'report_original.pdf', 'pdf')

                state_output = output.with_suffix(".json")
                state_output.write_text(
                    json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

                recorder.artifact(state_output, 'state_original.json', 'json')
                span['output'] = {'artifacts': [a.model_dump(mode='json') for a in recorder.artifacts]}

            console.print(f"Report written to {output}")
            console.print(f"State snapshot written to {state_output}")
            console.print(f"Branch selected: {result.branch}")
        recorder.finish(result=result)
    except BaseException as exc:
        recorder.finish(exc=exc)
        raise

if __name__ == "__main__":
    app()
